"""
db.py — SQLite 데이터 레이어
─────────────────────────────────────────────
이 코드는 Anthropic Claude Opus의 도움을 받아 작성되었습니다.
─────────────────────────────────────────────
외부 의존성 없음 (sqlite3 은 Python 표준 라이브러리).
Railway 배포 시 Volume 마운트하여 DB 파일 영속화.

환경 변수:
  DB_PATH  — SQLite 파일 경로 (기본: /data/notice_bot.db)

테이블:
  channel_configs      — 채널별 봇 설정
  green_buffer         — GREEN 공지 다이제스트 대기열
  classification_stats — 일별·채널별·등급별 분류 통계
  classification_log   — 전체 분류 감사 로그
"""

import os, json, logging, sqlite3, threading
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# ── DB 경로 ──────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "/data/notice_bot.db")

# SQLite 는 쓰기 시 파일 잠금을 하므로, 멀티스레드 환경에서
# connection 을 스레드별로 분리하고 WAL 모드로 동시 읽기 성능 확보.
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """스레드별 커넥션 반환. 없으면 새로 생성."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    """앱 시작 시 1회 호출. 디렉토리 생성 + 테이블 초기화."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = _get_conn()
    conn.executescript(_DDL)
    conn.commit()
    logger.info(f"SQLite 초기화 완료: {DB_PATH}")


_DDL = """
CREATE TABLE IF NOT EXISTS channel_configs (
    channel_id  TEXT PRIMARY KEY,
    threshold   REAL    NOT NULL DEFAULT 0.85,
    digest_hour INTEGER NOT NULL DEFAULT 18,
    red_mention TEXT    NOT NULL DEFAULT 'here',
    admin_users TEXT,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS green_buffer (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    reason      TEXT,
    message_ts  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_green_channel ON green_buffer(channel_id);

CREATE TABLE IF NOT EXISTS classification_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT    NOT NULL,
    stat_date   TEXT    NOT NULL,
    grade       TEXT    NOT NULL CHECK(grade IN ('RED','YELLOW','GREEN')),
    cnt         INTEGER NOT NULL DEFAULT 0,
    UNIQUE(channel_id, stat_date, grade)
);
CREATE INDEX IF NOT EXISTS idx_stats_ch_date ON classification_stats(channel_id, stat_date);

CREATE TABLE IF NOT EXISTS classification_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id       TEXT NOT NULL,
    message_ts       TEXT,
    original_text    TEXT,
    grade            TEXT NOT NULL CHECK(grade IN ('RED','YELLOW','GREEN')),
    reason           TEXT,
    emoji            TEXT,
    stage2_used      INTEGER DEFAULT 0,
    overridden       INTEGER DEFAULT 0,
    override_reason  TEXT,
    reclassified_by  TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_log_channel ON classification_log(channel_id);
CREATE INDEX IF NOT EXISTS idx_log_created ON classification_log(created_at);
"""


# ═══════════════════════════════════════════════
#  channel_configs CRUD
# ═══════════════════════════════════════════════

DEFAULT_CONFIG = {
    "threshold":   0.85,
    "digest_hour": 18,
    "red_mention": "here",
    "admin_users": [],
}


def get_channel_config(channel: str) -> dict:
    conn = _get_conn()
    row = conn.execute(
        "SELECT threshold, digest_hour, red_mention, admin_users "
        "FROM channel_configs WHERE channel_id = ?",
        (channel,),
    ).fetchone()
    if not row:
        return {**DEFAULT_CONFIG}
    return {
        "threshold":   row["threshold"],
        "digest_hour": row["digest_hour"],
        "red_mention": row["red_mention"],
        "admin_users": json.loads(row["admin_users"]) if row["admin_users"] else [],
    }


def update_channel_config(channel: str, updates: dict) -> None:
    cfg = {**DEFAULT_CONFIG, **get_channel_config(channel), **updates}
    admin_json = json.dumps(cfg["admin_users"], ensure_ascii=False)
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO channel_configs
            (channel_id, threshold, digest_hour, red_mention, admin_users, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(channel_id) DO UPDATE SET
            threshold   = excluded.threshold,
            digest_hour = excluded.digest_hour,
            red_mention = excluded.red_mention,
            admin_users = excluded.admin_users,
            updated_at  = datetime('now')
        """,
        (channel, cfg["threshold"], cfg["digest_hour"],
         cfg["red_mention"], admin_json),
    )
    conn.commit()


def get_all_digest_hours() -> set[int]:
    conn = _get_conn()
    rows = conn.execute("SELECT DISTINCT digest_hour FROM channel_configs").fetchall()
    return {row["digest_hour"] for row in rows}


def get_channels_by_digest_hour(hour: int) -> list[str]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT channel_id FROM channel_configs WHERE digest_hour = ?",
        (hour,),
    ).fetchall()
    return [row["channel_id"] for row in rows]


def get_configured_channel_count() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM channel_configs").fetchone()
    return row["cnt"]


# ═══════════════════════════════════════════════
#  green_buffer CRUD
# ═══════════════════════════════════════════════

def add_green_item(channel: str, text: str, reason: str, message_ts: str) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO green_buffer (channel_id, text, reason, message_ts) "
        "VALUES (?, ?, ?, ?)",
        (channel, text, reason, message_ts),
    )
    conn.commit()


def pop_green_items(channel: str = None) -> dict[str, list[tuple]]:
    """버퍼에서 항목을 꺼내고(삭제) 반환."""
    conn = _get_conn()
    if channel:
        rows = conn.execute(
            "SELECT id, channel_id, text, reason, message_ts "
            "FROM green_buffer WHERE channel_id = ? ORDER BY id",
            (channel,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, channel_id, text, reason, message_ts "
            "FROM green_buffer ORDER BY id"
        ).fetchall()

    if not rows:
        return {}

    ids = [r["id"] for r in rows]
    chunk = 900
    for i in range(0, len(ids), chunk):
        batch = ids[i : i + chunk]
        placeholders = ",".join(["?"] * len(batch))
        conn.execute(f"DELETE FROM green_buffer WHERE id IN ({placeholders})", batch)
    conn.commit()

    result: dict[str, list] = {}
    for r in rows:
        result.setdefault(r["channel_id"], []).append(
            (r["text"], r["reason"], r["message_ts"])
        )
    return result


def restore_green_items(channel: str, items: list[tuple]) -> None:
    """전송 실패 시 버퍼에 복원."""
    if not items:
        return
    conn = _get_conn()
    conn.executemany(
        "INSERT INTO green_buffer (channel_id, text, reason, message_ts) "
        "VALUES (?, ?, ?, ?)",
        [(channel, t, r, ts) for t, r, ts in items],
    )
    conn.commit()


# ═══════════════════════════════════════════════
#  classification_stats CRUD
# ═══════════════════════════════════════════════

def increment_stat(channel: str, grade: str) -> None:
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO classification_stats (channel_id, stat_date, grade, cnt)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(channel_id, stat_date, grade) DO UPDATE SET
            cnt = cnt + 1
        """,
        (channel, str(date.today()), grade),
    )
    conn.commit()


def adjust_stat(channel: str, old_grade: str, new_grade: str) -> None:
    today = str(date.today())
    conn = _get_conn()
    conn.execute(
        "UPDATE classification_stats SET cnt = MAX(cnt - 1, 0) "
        "WHERE channel_id = ? AND stat_date = ? AND grade = ?",
        (channel, today, old_grade),
    )
    conn.execute(
        """
        INSERT INTO classification_stats (channel_id, stat_date, grade, cnt)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(channel_id, stat_date, grade) DO UPDATE SET
            cnt = cnt + 1
        """,
        (channel, today, new_grade),
    )
    conn.commit()


def get_weekly_stats() -> dict[str, dict[str, int]]:
    since = str(date.today() - timedelta(days=7))
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT channel_id, grade, SUM(cnt) AS total
        FROM classification_stats
        WHERE stat_date >= ?
        GROUP BY channel_id, grade
        """,
        (since,),
    ).fetchall()

    result: dict[str, dict[str, int]] = {}
    for r in rows:
        ch = r["channel_id"]
        result.setdefault(ch, {"RED": 0, "YELLOW": 0, "GREEN": 0})
        result[ch][r["grade"]] = int(r["total"])
    return result


def delete_old_stats(days: int = 30) -> int:
    cutoff = str(date.today() - timedelta(days=days))
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM classification_stats WHERE stat_date < ?",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


# ═══════════════════════════════════════════════
#  classification_log (감사 로그)
# ═══════════════════════════════════════════════

def insert_log(
    channel: str,
    message_ts: str,
    text: str,
    grade: str,
    reason: str,
    emoji: str = "",
    stage2_used: bool = False,
    overridden: bool = False,
    override_reason: str = None,
    reclassified_by: str = None,
) -> None:
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO classification_log
            (channel_id, message_ts, original_text, grade,
             reason, emoji, stage2_used, overridden,
             override_reason, reclassified_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (channel, message_ts, text, grade, reason, emoji,
         int(stage2_used), int(overridden), override_reason, reclassified_by),
    )
    conn.commit()
