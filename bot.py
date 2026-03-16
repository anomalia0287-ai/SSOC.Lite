"""
공지 분류 봇 v4.0 — SQLite 영속화 + 버그 수정
─────────────────────────────────────────────
AI가 재현할 코드를 남겨주신 개발자 분들께 경의를 표합니다.
이 코드는 Anthropic Claude Opus의 도움을 받아 작성되었습니다.
─────────────────────────────────────────────
v3.1 → v4.0 변경 내역:

  [BREAKING] 인메모리 → SQLite 전환
    - channel_configs : JSON 파일 → channel_configs 테이블
    - green_buffer    : dict       → green_buffer 테이블
    - stats           : dict       → classification_stats 테이블
    - 신규: classification_log 감사 로그 테이블

  [BUG FIX] 재분류 리액션 대상 불일치
    - 버튼 value 에 원본 message_ts 를 포함하여 원본 메시지 이모지 교체
  [BUG FIX] 버튼 value 2,000자 초과 방지
    - 원문 대신 channel + message_ts 만 저장, 핸들러에서 원문 재조회
  [BUG FIX] threshold 라벨 반전
    - 0.95 = 높음(2차 검증 많음), 0.70 = 낮음(2차 검증 적음) 으로 교정
  [BUG FIX] send_green_digest 가 채널별 digest_hour 무시
    - 실행 시각에 해당하는 채널만 전송하도록 수정
  [BUG FIX] send_weekly_report stats 데이터 경쟁
    - DB UPSERT/SUM 으로 원자적 처리, 경쟁 조건 제거
  [BUG FIX] 빈 채널 리포트 전송 방지
    - 분류 0건 채널은 건너뜀

  [REFACTOR] 전역 상태 제거
    - threading lock 전체 제거 (DB 가 동시성 보장)
    - 전역 변수 → db 모듈 함수 호출로 대체

환경 변수 (신규):
  DB_PATH  — SQLite 파일 경로 (기본: /data/notice_bot.db)
"""

import os, json, re, time, logging
from datetime import date
from dotenv import load_dotenv

import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from slack_bolt import App
from slack_bolt.adapter.fastapi import SlackRequestHandler

import db  # ← SQLite 데이터 레이어

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── 클라이언트 초기화 ─────────────────────────────
claude  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
bolt    = App(token=os.environ["SLACK_BOT_TOKEN"],
              signing_secret=os.environ["SLACK_SIGNING_SECRET"])
handler = SlackRequestHandler(bolt)
api     = FastAPI()

# ── 상수 ─────────────────────────────────────────
MODEL_STAGE1 = os.getenv("MODEL_STAGE1", "claude-haiku-4-5-20251001")
MODEL_STAGE2 = os.getenv("MODEL_STAGE2", "claude-sonnet-4-6")

DEFAULT_CONFIDENCE_THRESHOLD = 0.85
DEFAULT_DIGEST_HOUR          = 18
DEFAULT_RED_MENTION          = "here"

EMOJI  = {"RED": "red_circle", "YELLOW": "large_yellow_circle", "GREEN": "large_green_circle"}
LABEL  = {"RED": "🔴 즉시 확인 필요", "YELLOW": "🟡 당일 확인 권장", "GREEN": "🟢 주간 다이제스트"}
COLOR  = {"RED": "#E53E3E", "YELLOW": "#D69E2E", "GREEN": "#38A169"}

# ── DB 초기화 (앱 시작 시 1회) ────────────────────
db.init_db()


# ── 프롬프트 ─────────────────────────────────────
STAGE1_SYSTEM = """사내 공지를 분류하는 전문가입니다. JSON으로만 응답하세요.
- RED   : 마감일, 법적 의무, 긴급 대응, 보안 위협, 필수 서명
- YELLOW: 업무 안내, 시스템 점검, 일정 변경, 인사 발령
- GREEN : 일반 정보, 행사 안내, 문화 소식

emoji 필드: 공지 내용을 가장 잘 나타내는 이모지 1개 (예: 📅 🔒 💻 🏢 🎉 ⚠️ 📢 등)

응답 형식 (반드시 이 형식만):
{"grade":"RED","confidence":0.95,"reason":"분류 근거","emoji":"⚠️"}"""

STAGE2_SYSTEM = """공지 분류 검증 전문가입니다. 1차 판단을 독립적으로 검토하세요.
1차 판단은 참고용입니다. 당신의 독립적 판단이 우선입니다.
- RED   : 마감일, 법적 의무, 긴급 대응, 보안 위협, 필수 서명
- YELLOW: 업무 안내, 시스템 점검, 일정 변경, 인사 발령
- GREEN : 일반 정보, 행사 안내, 문화 소식

emoji 필드: 공지 내용을 가장 잘 나타내는 이모지 1개 (예: 📅 🔒 💻 🏢 🎉 ⚠️ 📢 등)

응답 형식 (반드시 이 형식만):
{"grade":"RED","confidence":0.95,"reason":"분류 근거","emoji":"⚠️","overridden":false,"override_reason":null}"""


# ── AI 분류 파이프라인 ────────────────────────────

def extract_json(raw: str) -> dict:
    """LLM 응답에서 JSON 객체를 안전하게 추출 (depth-counting)."""
    raw = re.sub(r"```(?:json)?", "", raw).strip()

    depth = 0
    start = None
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = raw[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    start = None

    raise ValueError(f"유효한 JSON 객체를 찾을 수 없음: {raw[:200]}")


def call_llm(model: str, system: str, content: str) -> tuple[dict, int]:
    t0   = time.monotonic()
    resp = claude.messages.create(
        model=model, max_tokens=300, system=system,
        messages=[{"role": "user", "content": content}],
    )
    ms  = int((time.monotonic() - t0) * 1000)
    raw = resp.content[0].text.strip()
    return extract_json(raw), ms


def classify(text: str, channel: str = "") -> dict:
    cfg       = db.get_channel_config(channel) if channel else db.DEFAULT_CONFIG.copy()
    threshold = cfg["threshold"]

    notice   = f"공지 내용:\n{text}"
    r1, l1   = call_llm(MODEL_STAGE1, STAGE1_SYSTEM, notice)
    logger.info(f"1차: {r1['grade']} ({r1['confidence']:.2f}) {l1}ms")

    needs_verify = r1["grade"] == "RED" or r1["confidence"] < threshold
    if not needs_verify:
        return {
            "grade":       r1["grade"],
            "reason":      r1["reason"],
            "emoji":       r1.get("emoji", "📢"),
            "stage2_used": False,
        }

    prompt2  = f"{notice}\n\n---\n참고(1차): {r1['grade']} ({r1['confidence']:.0%}) - {r1['reason']}"
    r2, l2   = call_llm(MODEL_STAGE2, STAGE2_SYSTEM, prompt2)
    logger.info(
        f"2차: {r2['grade']} ({r2['confidence']:.2f}) {l2}ms"
        + (" <- 수정됨" if r2.get("overridden") else "")
    )
    return {
        "grade":           r2["grade"],
        "reason":          r2["reason"],
        "emoji":           r2.get("emoji", r1.get("emoji", "📢")),
        "stage2_used":     True,
        "overridden":      r2.get("overridden", False),
        "override_reason": r2.get("override_reason"),
    }


# ── Block Kit 카드 ────────────────────────────────
GRADE_ORDER = ["RED", "YELLOW", "GREEN"]


def build_card(
    grade: str,
    result: dict,
    text: str,
    *,
    channel: str = "",
    original_ts: str = "",
    reclassified_by: str = None,
) -> list:
    preview    = text[:80] + ("..." if len(text) > 80 else "")
    stage_info = "⚡ 2차 AI 검증 완료" if result.get("stage2_used") else "✓ 1차 고신뢰 통과"
    if result.get("overridden"):
        stage_info += " (등급 수정됨)"

    grade_emoji = {"RED": "🚨", "YELLOW": "📌", "GREEN": "📗"}.get(grade, "")
    ai_emoji    = result.get("emoji", "")
    title_text  = f"{grade_emoji} {ai_emoji}  {LABEL[grade]}"

    blocks = [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title_text}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"_{preview}_"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"📋 {result['reason']}"},
            {"type": "mrkdwn", "text": stage_info},
        ]},
    ]

    if reclassified_by:
        blocks.append({
            "type": "context", "elements": [
                {"type": "mrkdwn", "text": f"🔄 *{reclassified_by}*님이 등급을 변경했습니다."}
            ]
        })

    current_idx = GRADE_ORDER.index(grade)
    buttons = []
    btn_payload_base = {
        "channel": channel,
        "original_ts": original_ts,
        "current": grade,
    }

    if current_idx > 0:
        higher = GRADE_ORDER[current_idx - 1]
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": f"⬆️ {LABEL[higher]} 으로 변경"},
            "action_id": "reclassify_up",
            "style": "danger",
            "value": json.dumps({**btn_payload_base, "target": higher}),
        })

    if current_idx < len(GRADE_ORDER) - 1:
        lower = GRADE_ORDER[current_idx + 1]
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": f"⬇️ {LABEL[lower]} 으로 변경"},
            "action_id": "reclassify_down",
            "value": json.dumps({**btn_payload_base, "target": lower}),
        })

    if buttons:
        blocks.append({"type": "actions", "elements": buttons})

    blocks.append({"type": "divider"})
    return blocks


# ── 재분류 버튼 액션 핸들러 ──────────────────────

def _handle_reclassify(body, client, action_id):
    action  = next(a for a in body["actions"] if a["action_id"] == action_id)
    payload = json.loads(action["value"])

    target      = payload["target"]
    current     = payload["current"]
    channel     = payload["channel"]
    original_ts = payload["original_ts"]
    msg_ts      = body["container"]["message_ts"]
    user_id     = body["user"]["id"]

    admin_users = db.get_channel_config(channel).get("admin_users", [])
    if admin_users and user_id not in admin_users:
        client.chat_postEphemeral(
            channel=channel, user=user_id,
            text="⛔ 등급 변경 권한이 없습니다. 채널 관리자에게 문의하세요.",
        )
        return

    text = ""
    try:
        history = client.conversations_history(
            channel=channel, latest=original_ts,
            inclusive=True, limit=1,
        )
        if history["messages"]:
            text = history["messages"][0].get("text", "")
    except Exception as e:
        logger.warning(f"원본 메시지 조회 실패: {e}")

    try:
        user_info = client.users_info(user=user_id)
        user_name = (
            user_info["user"]["profile"].get("display_name")
            or user_info["user"]["real_name"]
            or "알 수 없음"
        )
    except Exception:
        user_name = "알 수 없음"

    result = {
        "reason":      f"'{user_name}'님의 수동 재분류 ({current} → {target})",
        "stage2_used": False,
        "overridden":  True,
    }

    try:
        new_blocks = build_card(
            target, result, text,
            channel=channel, original_ts=original_ts,
            reclassified_by=user_name,
        )
        client.chat_update(
            channel=channel, ts=msg_ts,
            text=LABEL[target],
            blocks=new_blocks,
            attachments=[{"color": COLOR[target], "fallback": LABEL[target]}],
        )
    except Exception as e:
        logger.error(f"재분류 메시지 업데이트 실패: {e}")

    try:
        client.reactions_remove(channel=channel, timestamp=original_ts, name=EMOJI[current])
    except Exception:
        pass
    try:
        client.reactions_add(channel=channel, timestamp=original_ts, name=EMOJI[target])
    except Exception:
        pass

    db.adjust_stat(channel, current, target)
    db.insert_log(
        channel=channel, message_ts=original_ts, text=text,
        grade=target, reason=result["reason"],
        reclassified_by=user_name,
    )
    logger.info(f"재분류: {current} → {target} by {user_name} ({channel})")


@bolt.action("reclassify_up")
def handle_reclassify_up(ack, body, client):
    ack()
    _handle_reclassify(body, client, "reclassify_up")


@bolt.action("reclassify_down")
def handle_reclassify_down(ack, body, client):
    ack()
    _handle_reclassify(body, client, "reclassify_down")


# ── 스케줄 작업 ───────────────────────────────────

def send_green_digest(target_hour: int = None, target_channel: str = None):
    if target_channel:
        snapshot = db.pop_green_items(channel=target_channel)
    elif target_hour is not None:
        channels = db.get_channels_by_digest_hour(target_hour)
        if target_hour == DEFAULT_DIGEST_HOUR:
            snapshot = db.pop_green_items()
            restore = {
                ch: items for ch, items in snapshot.items()
                if ch not in channels
                and db.get_channel_config(ch)["digest_hour"] != DEFAULT_DIGEST_HOUR
            }
            for ch, items in restore.items():
                db.restore_green_items(ch, items)
                del snapshot[ch]
        else:
            snapshot = {}
            for ch in channels:
                ch_items = db.pop_green_items(channel=ch)
                snapshot.update(ch_items)
    else:
        snapshot = db.pop_green_items()

    if not snapshot:
        return

    slack_client = bolt.client
    today        = date.today().strftime("%m/%d")

    for channel, items in snapshot.items():
        if not items:
            continue
        try:
            lines  = "\n".join(f"• {reason}  ({text[:40]}...)" for text, reason, _ in items)
            blocks = [
                {"type": "header", "text": {"type": "plain_text",
                    "text": f"🟢 {today} GREEN 공지 요약 ({len(items)}건)"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": lines}},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "낮은 우선순위 공지 모음"}
                ]},
            ]
            slack_client.chat_postMessage(
                channel=channel,
                text=f"🟢 {today} GREEN 공지 요약",
                blocks=blocks,
            )
        except Exception as e:
            logger.error(f"GREEN 다이제스트 전송 실패 ({channel}): {e}")
            db.restore_green_items(channel, items)


def send_weekly_report():
    weekly = db.get_weekly_stats()
    if not weekly:
        return

    slack_client = bolt.client

    for channel, totals in weekly.items():
        r = totals.get("RED", 0)
        y = totals.get("YELLOW", 0)
        g = totals.get("GREEN", 0)
        grand = r + y + g
        if grand == 0:
            continue

        try:
            blocks = [
                {"type": "header", "text": {"type": "plain_text", "text": "📊 주간 공지 분류 리포트"}},
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    f"*지난 7일 총 {grand}건 분류*\n"
                    f"🔴 RED: *{r}건* ({r/grand*100:.0f}%)\n"
                    f"🟡 YELLOW: *{y}건* ({y/grand*100:.0f}%)\n"
                    f"🟢 GREEN: *{g}건* ({g/grand*100:.0f}%)"
                }},
            ]
            slack_client.chat_postMessage(
                channel=channel,
                text="📊 주간 공지 분류 리포트",
                blocks=blocks,
            )
        except Exception as e:
            logger.error(f"주간 리포트 전송 실패 ({channel}): {e}")

    deleted = db.delete_old_stats(days=30)
    if deleted:
        logger.info(f"오래된 통계 {deleted}건 정리 완료")


# ── 스케줄러 초기화 ────────────────────────────────
scheduler = BackgroundScheduler(timezone="Asia/Seoul")
scheduler.add_job(
    send_green_digest, "cron",
    hour=DEFAULT_DIGEST_HOUR, minute=0,
    id=f"digest_{DEFAULT_DIGEST_HOUR}",
    kwargs={"target_hour": DEFAULT_DIGEST_HOUR},
)
scheduler.add_job(send_weekly_report, "cron", day_of_week="mon", hour=9, minute=0)
scheduler.start()


def _reschedule_digest():
    hours: set[int] = {DEFAULT_DIGEST_HOUR}
    hours.update(db.get_all_digest_hours())

    for job in scheduler.get_jobs():
        if job.id.startswith("digest_"):
            scheduler.remove_job(job.id)

    for h in hours:
        scheduler.add_job(
            send_green_digest, "cron",
            hour=h, minute=0,
            id=f"digest_{h}",
            replace_existing=True,
            kwargs={"target_hour": h},
        )
    logger.info(f"다이제스트 스케줄 재등록: {sorted(hours)}시")


# ── 슬래시 커맨드: /notice-config ────────────────

def _threshold_option(value: float) -> dict:
    label_map = {
        0.70: "낮음 — 70% 미만 시 2차 검증",
        0.85: "기본 — 85% 미만 시 2차 검증",
        0.95: "높음 — 95% 미만 시 2차 검증",
    }
    label = label_map.get(value, f"커스텀 ({value})")
    return {"text": {"type": "plain_text", "text": label}, "value": str(value)}


def _mention_option(value: str) -> dict:
    label_map = {
        "here":    "@here (온라인 멤버)",
        "channel": "@channel (전체 멤버)",
        "none":    "멘션 없음",
    }
    label = label_map.get(value, value)
    return {"text": {"type": "plain_text", "text": label}, "value": value}


@bolt.command("/notice-config")
def handle_config_command(ack, body, client):
    ack()
    channel = body["channel_id"]
    cfg     = db.get_channel_config(channel)

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type":             "modal",
            "callback_id":      "notice_config_modal",
            "private_metadata": channel,
            "title":  {"type": "plain_text", "text": "공지 봇 채널 설정"},
            "submit": {"type": "plain_text", "text": "저장"},
            "close":  {"type": "plain_text", "text": "취소"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*<#{channel}> 채널 설정*"},
                },
                {"type": "divider"},
                {
                    "type":     "input",
                    "block_id": "threshold_block",
                    "label":    {"type": "plain_text", "text": "🎯 2차 검증 민감도"},
                    "hint":     {"type": "plain_text",
                                 "text": "높을수록 더 많은 공지가 2차 AI 검증을 거칩니다."},
                    "element": {
                        "type":           "static_select",
                        "action_id":      "threshold_select",
                        "initial_option": _threshold_option(cfg["threshold"]),
                        "options": [
                            _threshold_option(0.70),
                            _threshold_option(0.85),
                            _threshold_option(0.95),
                        ],
                    },
                },
                {
                    "type":     "input",
                    "block_id": "digest_hour_block",
                    "label":    {"type": "plain_text", "text": "🕐 GREEN 다이제스트 전송 시각"},
                    "hint":     {"type": "plain_text", "text": "0–23 사이 정수. 예: 18 → 오후 6시 전송"},
                    "element": {
                        "type":          "plain_text_input",
                        "action_id":     "digest_hour_input",
                        "initial_value": str(cfg["digest_hour"]),
                        "placeholder":   {"type": "plain_text", "text": "18"},
                    },
                },
                {
                    "type":     "input",
                    "block_id": "red_mention_block",
                    "label":    {"type": "plain_text", "text": "🔔 RED 공지 멘션 방식"},
                    "element": {
                        "type":           "static_select",
                        "action_id":      "red_mention_select",
                        "initial_option": _mention_option(cfg["red_mention"]),
                        "options": [
                            _mention_option("here"),
                            _mention_option("channel"),
                            _mention_option("none"),
                        ],
                    },
                },
                {
                    "type":     "input",
                    "block_id": "admin_users_block",
                    "label":    {"type": "plain_text", "text": "🔑 재분류 허용 사용자 ID"},
                    "hint":     {"type": "plain_text",
                                 "text": "슬랙 사용자 ID를 쉼표로 구분. 예: U12345678, U87654321  |  비워두면 누구나 가능"},
                    "optional": True,
                    "element": {
                        "type":          "plain_text_input",
                        "action_id":     "admin_users_input",
                        "initial_value": ", ".join(cfg.get("admin_users", [])),
                        "placeholder":   {"type": "plain_text", "text": "U12345678, U87654321"},
                    },
                },
            ],
        },
    )


@bolt.view("notice_config_modal")
def handle_config_submit(ack, body, view, client):
    channel = view["private_metadata"]
    values  = view["state"]["values"]

    threshold   = float(
        values["threshold_block"]["threshold_select"]["selected_option"]["value"]
    )
    digest_str  = values["digest_hour_block"]["digest_hour_input"]["value"].strip()
    red_mention = values["red_mention_block"]["red_mention_select"]["selected_option"]["value"]
    admin_raw   = (values["admin_users_block"]["admin_users_input"].get("value") or "").strip()
    admin_users = [uid.strip() for uid in admin_raw.split(",") if uid.strip()]

    if not digest_str.isdigit() or not (0 <= int(digest_str) <= 23):
        ack(response_action="errors", errors={
            "digest_hour_block": "0에서 23 사이의 숫자를 입력하세요."
        })
        return

    ack()
    digest_hour = int(digest_str)

    db.update_channel_config(channel, {
        "threshold":   threshold,
        "digest_hour": digest_hour,
        "red_mention": red_mention,
        "admin_users": admin_users,
    })
    _reschedule_digest()

    admin_info = ", ".join(f"`{u}`" for u in admin_users) if admin_users else "전체 허용"
    client.chat_postMessage(
        channel=channel,
        text=(
            f"✅ 설정이 저장되었습니다.\n"
            f"• 2차 검증 민감도: `{threshold}`\n"
            f"• GREEN 다이제스트 전송: 매일 `{digest_hour:02d}:00`\n"
            f"• RED 멘션 방식: `{red_mention}`\n"
            f"• 재분류 허용: {admin_info}"
        ),
    )


# ── 슬랙 이벤트 핸들러 ────────────────────────────
@bolt.event("message")
def handle_message(event, client):
    if event.get("bot_id") or event.get("subtype"):
        return

    text    = event.get("text", "").strip()
    channel = event["channel"]
    ts      = event["ts"]

    if len(text) < 10:
        return

    try:
        result = classify(text, channel=channel)
        grade  = result["grade"]

        db.increment_stat(channel, grade)
        db.insert_log(
            channel=channel, message_ts=ts, text=text,
            grade=grade, reason=result["reason"],
            emoji=result.get("emoji", ""),
            stage2_used=result.get("stage2_used", False),
            overridden=result.get("overridden", False),
            override_reason=result.get("override_reason"),
        )

        try:
            client.reactions_add(channel=channel, timestamp=ts, name=EMOJI[grade])
        except Exception as e:
            logger.warning(f"리액션 추가 실패 (무시): {e}")

        cfg         = db.get_channel_config(channel)
        mention_tag = {
            "here":    "<!here> ",
            "channel": "<!channel> ",
            "none":    "",
        }.get(cfg["red_mention"], "<!here> ")

        if grade == "GREEN":
            db.add_green_item(channel, text, result["reason"], ts)

        elif grade == "YELLOW":
            client.chat_postMessage(
                channel=channel, thread_ts=ts,
                text=LABEL[grade],
                blocks=build_card(grade, result, text, channel=channel, original_ts=ts),
                attachments=[{"color": COLOR[grade], "fallback": LABEL[grade]}],
            )

        elif grade == "RED":
            client.chat_postMessage(
                channel=channel, thread_ts=ts,
                text=f"{mention_tag}{LABEL[grade]}",
                blocks=[
                    {"type": "section", "text": {
                        "type": "mrkdwn",
                        "text": f"{mention_tag}*{LABEL[grade]}*",
                    }},
                    *build_card(grade, result, text, channel=channel, original_ts=ts)[1:],
                ],
                attachments=[{"color": COLOR[grade], "fallback": LABEL[grade]}],
            )

            admin_users = cfg.get("admin_users", [])
            if admin_users:
                preview = text[:60] + ("..." if len(text) > 60 else "")
                for admin_id in admin_users:
                    try:
                        client.chat_postMessage(
                            channel=admin_id,
                            text=(
                                f"🚨 *RED 공지 발생* — <#{channel}>\n"
                                f"_{preview}_"
                            ),
                        )
                    except Exception as e:
                        logger.warning(f"관리자 DM 전송 실패 ({admin_id}): {e}")

    except Exception as e:
        logger.error(f"분류 실패: {e}", exc_info=True)
        try:
            client.reactions_add(channel=channel, timestamp=ts, name="warning")
        except Exception:
            pass


# ── FastAPI 라우터 ────────────────────────────────
@api.post("/slack/events")
async def slack_events(req: Request):
    return await handler.handle(req)


@api.get("/health")
def health():
    return {
        "status": "ok",
        "configured_channels": db.get_configured_channel_count(),
    }


@api.get("/config/{channel_id}")
def get_config(channel_id: str):
    return db.get_channel_config(channel_id)


@api.post("/digest/now")
def trigger_digest():
    send_green_digest()
    return {"status": "sent"}


@api.post("/report/now")
def trigger_report():
    send_weekly_report()
    return {"status": "sent"}
