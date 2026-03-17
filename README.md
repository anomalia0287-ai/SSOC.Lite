# SSOC.Lite v4.0

Slack 채널에 올라오는 사내 공지를 AI가 자동으로 **RED / YELLOW / GREEN** 등급으로 분류하고,
긴급도에 따라 즉시 알림 · 당일 스레드 · 주간 다이제스트로 전달하는 봇입니다.

---

## 주요 기능

| 등급 | 의미 | 동작 |
|------|------|------|
| RED | 즉시 확인 필요 | @here 멘션 + 관리자 DM |
| YELLOW | 당일 확인 권장 | 스레드 카드 |
| GREEN | 낮은 우선순위 | 다이제스트 모아서 전송 |

- **2단계 AI 분류** — 1차(Haiku) 빠른 분류 → 신뢰도 낮거나 RED이면 2차(Sonnet) 검증
- **재분류 버튼** — 관리자가 직접 등급을 올리거나 내릴 수 있음, 변경자 이름 표시
- **채널별 설정** — `/notice-config` 슬래시 커맨드로 민감도·다이제스트 시각·멘션 방식 커스터마이징
- **SQLite 영속화** — 설정, GREEN 버퍼, 분류 통계, 감사 로그 모두 DB 저장 (서버 재시작해도 유실 없음)
- **주간 리포트** — 매주 월요일 오전 9시 자동 발송

---

## 파일 구조

```
├── bot.py                      # 메인 봇 로직
├── db.py                       # SQLite 데이터 레이어
├── migrate_json_to_sqlite.py   # v3.1 → v4.0 설정 마이그레이션
├── Dockerfile                  # Railway 배포용
├── requirements.txt            # Python 의존성
├── env.example                 # 환경 변수 템플릿
└── README.md
```

---

## Railway 배포 가이드

### 1. Volume 추가 (필수)

SQLite DB 파일이 재배포해도 유지되려면 Volume이 필요합니다.

1. Railway 대시보드 → 프로젝트 → 봇 서비스 선택
2. **Settings** → **Volumes** → **Add Volume**
3. Mount Path: `/data`
4. Save

### 2. 환경 변수 설정

Railway 대시보드 → **Variables** 탭에서:

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
ANTHROPIC_API_KEY=sk-ant-...
DB_PATH=/data/notice_bot.db
```

### 3. 기존 설정 마이그레이션 (v3.1에서 업그레이드 시)

기존 `channel_configs.json`이 있다면, 배포 후 Railway Shell에서:

```bash
python migrate_json_to_sqlite.py channel_configs.json
```

또는 로컬에서 먼저 실행하고 `notice_bot.db` 파일을 Volume에 넣어도 됩니다.

### 4. 배포

GitHub 저장소를 Railway에 연결하면 자동 배포됩니다.
수동 배포 시:

```bash
git add .
git commit -m "v4.0: SQLite 영속화"
git push
```

---

## 로컬 실행

```bash
pip install -r requirements.txt

# 환경 변수 설정
cp env.example .env
# .env 파일 편집

# 기존 설정 마이그레이션 (있는 경우)
python migrate_json_to_sqlite.py

# 실행
uvicorn bot:api --host 0.0.0.0 --port 3000
```

---

## API 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | `/slack/events` | Slack 이벤트 수신 |
| GET | `/health` | 헬스체크 |
| GET | `/config/{channel_id}` | 채널 설정 조회 |
| POST | `/digest/now` | GREEN 다이제스트 즉시 전송 |
| POST | `/report/now` | 주간 리포트 즉시 전송 |

---

## 감사의 말

> *AI가 학습할 코드를 제공해주신 개발자 분들께 경의를 표합니다.*
>
> 이 프로젝트는 오픈소스 생태계 위에 서 있습니다.
> Slack Bolt, FastAPI, APScheduler —
> 그리고 수많은 Python 패키지를 만들고 유지보수하시는 분들,
> 여러분이 공유해 주신 코드 덕분에 이 봇이 존재할 수 있었습니다.
>
> 이 코드는 **Anthropic Claude Opus**의 도움을 받아 작성되었습니다.

---

## 라이선스

[MIT License](LICENSE) — 자유롭게 사용, 수정, 배포할 수 있습니다.
