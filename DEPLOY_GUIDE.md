# 공지 분류 봇 v4.0 — 배포 완전 가이드

## 📑 목차

1. [Slack App 설정](#1-slack-app-설정)
2. [Railway 배포](#2-railway-배포)
3. [Slack ↔ Railway 연결](#3-slack--railway-연결)
4. [동작 확인](#4-동작-확인)
5. [트러블슈팅](#5-트러블슈팅)

---

## 1. Slack App 설정

### 1-1. Slack App이 이미 있는 경우

https://api.slack.com/apps 에서 기존 앱을 선택합니다.
아래 권한들이 모두 설정되어 있는지 확인하세요.

### 1-2. OAuth & Permissions (Bot Token Scopes)

**Sidebar → OAuth & Permissions → Scopes → Bot Token Scopes** 에서 아래 7개를 추가합니다:

| Scope | 용도 |
|-------|------|
| `chat:write` | 분류 결과 카드, 다이제스트, 리포트 전송 |
| `reactions:write` | 🔴🟡🟢 이모지 리액션 추가 |
| `reactions:read` | 재분류 시 기존 리액션 제거 |
| `channels:history` | 공개 채널 메시지 읽기 (분류 대상) |
| `groups:history` | 비공개 채널 메시지 읽기 (필요 시) |
| `users:read` | 재분류한 사람 이름 표시 |
| `commands` | `/notice-config` 슬래시 커맨드 |

> ⚠️ Scope를 추가한 후 **반드시 "Reinstall to Workspace"** 버튼을 눌러야 적용됩니다.

### 1-3. Event Subscriptions

**Sidebar → Event Subscriptions**

1. **Enable Events** → On
2. **Request URL** → `https://{railway-도메인}/slack/events`
   (Railway 배포 후 URL을 알 수 있으므로, 일단 비워두고 나중에 입력)
3. **Subscribe to bot events** 에서 추가:
   - `message.channels` — 공개 채널 메시지 수신
   - `message.groups` — 비공개 채널도 지원하려면 추가

### 1-4. Interactivity & Shortcuts

**Sidebar → Interactivity & Shortcuts**

1. **Interactivity** → On
2. **Request URL** → `https://{railway-도메인}/slack/events`
   (Event URL과 동일한 주소)

> 이 설정이 없으면 재분류 버튼과 /notice-config 모달이 작동하지 않습니다.

### 1-5. Slash Commands

**Sidebar → Slash Commands → Create New Command**

| 필드 | 값 |
|------|---|
| Command | `/notice-config` |
| Request URL | `https://{railway-도메인}/slack/events` |
| Short Description | 공지 봇 채널 설정 |

### 1-6. 토큰 확인

설정 완료 후 두 가지 값을 메모해 둡니다:

- **Bot User OAuth Token** (OAuth & Permissions 페이지)
  → `xoxb-` 로 시작하는 문자열
- **Signing Secret** (Basic Information → App Credentials)
  → 32자리 hex 문자열

---

## 2. Railway 배포

### 2-1. GitHub 저장소 준비

로컬에서 프로젝트 폴더를 만들고 파일들을 배치합니다:

```
notice-bot/
├── bot.py                      ← 수정된 버전 사용
├── db.py
├── migrate_json_to_sqlite.py
├── Dockerfile                  ← 새로 생성된 파일
├── Procfile
├── requirements.txt
├── .gitignore
├── LICENSE
└── README.md
```

GitHub에 push합니다:

```bash
cd notice-bot
git init
git add .
git commit -m "v4.0: 공지 분류 봇 초기 배포"
git remote add origin https://github.com/{your-username}/{repo-name}.git
git push -u origin main
```

### 2-2. Railway 프로젝트 생성

1. https://railway.app/dashboard 접속
2. **New Project** → **Deploy from GitHub repo** 선택
3. 저장소 연결 (처음이면 GitHub 계정 연동 필요)
4. 저장소 선택하면 자동으로 빌드 시작됨

### 2-3. Volume 추가 (필수!)

SQLite 파일이 재배포 시 유지되려면 Volume이 반드시 필요합니다.

1. 프로젝트 서비스 클릭
2. **Settings** 탭
3. 스크롤 내려서 **Volumes** → **Add Volume**
4. **Mount Path**: `/data`
5. Save

> ⚠️ Volume 없이 배포하면 재배포할 때마다 모든 설정과 데이터가 초기화됩니다.

### 2-4. 환경 변수 설정

서비스의 **Variables** 탭에서 4개를 추가합니다:

| 변수명 | 값 | 설명 |
|--------|---|------|
| `SLACK_BOT_TOKEN` | `xoxb-...` | 1-6에서 메모한 Bot Token |
| `SLACK_SIGNING_SECRET` | `abc123...` | 1-6에서 메모한 Signing Secret |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Anthropic API 키 |
| `DB_PATH` | `/data/notice_bot.db` | Volume 마운트 경로 |

선택 환경 변수 (기본값이 있어서 필수는 아님):

| 변수명 | 기본값 | 설명 |
|--------|--------|------|
| `MODEL_STAGE1` | `claude-haiku-4-5-20251001` | 1차 분류 모델 |
| `MODEL_STAGE2` | `claude-sonnet-4-6` | 2차 검증 모델 |

> 💡 비용을 절약하려면 MODEL_STAGE2도 Haiku로 바꿀 수 있지만, 검증 정확도가 떨어집니다.

### 2-5. 도메인 확인

배포가 완료되면:

1. 서비스 → **Settings** 탭
2. **Networking** → **Public Networking** → **Generate Domain**
3. `https://xxxxx.up.railway.app` 형태의 URL이 생성됨

이 URL을 메모합니다.

---

## 3. Slack ↔ Railway 연결

Railway 도메인이 생겼으면, Slack App 설정으로 돌아가서 URL을 채워 넣습니다.

총 3곳에 같은 URL을 입력합니다:

| 설정 위치 | URL |
|-----------|-----|
| Event Subscriptions → Request URL | `https://xxxxx.up.railway.app/slack/events` |
| Interactivity → Request URL | `https://xxxxx.up.railway.app/slack/events` |
| Slash Commands → /notice-config | `https://xxxxx.up.railway.app/slack/events` |

Event Subscriptions의 Request URL을 입력하면 Slack이 자동으로 challenge 요청을 보냅니다.
"Verified ✓" 가 뜨면 성공입니다.

> 만약 Verified가 안 뜨면 Railway 로그에서 에러를 확인하세요.

### 3-1. 봇을 채널에 초대

분류를 원하는 채널에서:

```
/invite @봇이름
```

또는 채널 설정 → Integrations → Apps → 봇 추가

---

## 4. 동작 확인

### 4-1. 헬스체크

브라우저에서:
```
https://xxxxx.up.railway.app/health
```

정상이면:
```json
{"status": "ok", "configured_channels": 0}
```

### 4-2. 첫 번째 분류 테스트

봇이 초대된 채널에서 10자 이상의 메시지를 보냅니다:

- RED 테스트: `긴급: 내일까지 보안 패치 필수 적용 바랍니다. 미적용 시 계정이 잠깁니다.`
- YELLOW 테스트: `다음 주 월요일부터 사내 주차장 구역이 재배치됩니다. 확인 부탁드립니다.`
- GREEN 테스트: `이번 주 금요일 사내 카페에서 바리스타 이벤트가 진행됩니다.`

각 메시지에 🔴/🟡/🟢 리액션이 붙으면 성공입니다.

### 4-3. 채널 설정 테스트

채널에서:
```
/notice-config
```

모달이 뜨면 설정을 조정해 봅니다.

### 4-4. 다이제스트 & 리포트 수동 테스트

```bash
# GREEN 다이제스트 즉시 전송
curl -X POST https://xxxxx.up.railway.app/digest/now

# 주간 리포트 즉시 전송
curl -X POST https://xxxxx.up.railway.app/report/now
```

---

## 5. 트러블슈팅

### "Verified" 가 안 뜰 때

- Railway 서비스가 정상 배포되었는지 확인 (Deployments 탭에서 Active 상태)
- Public Domain이 생성되었는지 확인
- URL 끝에 `/slack/events` 가 정확히 붙어 있는지 확인

### 리액션은 붙는데 카드가 안 뜰 때

- `chat:write` scope 확인
- 봇이 해당 채널에 초대되어 있는지 확인

### "not_in_channel" 에러

- 봇을 채널에 `/invite @봇이름` 으로 초대

### 재분류 버튼이 안 눌릴 때

- Interactivity가 켜져 있는지 확인
- Interactivity Request URL이 올바른지 확인

### Railway 빌드 실패

- Dockerfile이 있는지 확인
- requirements.txt가 정상인지 확인
- Railway 로그에서 에러 메시지 확인

### API 비용 초과 걱정

$4 예산 기준 대략적인 한도:
- Haiku 1차 분류만: ~4,000건 이상
- Sonnet 2차 검증 포함: ~400건 (전체의 ~10%가 2차 검증 시)
- 하루 20건 정도면 2~3주 사용 가능

threshold를 낮추면 (0.70) 2차 검증이 줄어들어 비용 절약됩니다.
`/notice-config` 에서 "낮음 — 70%"를 선택하세요.

### Railway Volume 확인

Railway Shell에서:
```bash
ls -la /data/
# notice_bot.db 파일이 있으면 정상
```

---

## 📎 참고: 수정된 파일 목록

| 파일 | 변경 내용 |
|------|----------|
| `bot.py` | 디버그 print 제거, 중복 import 정리 |
| `Dockerfile` | 신규 생성 (Python 3.12-slim 기반) |

나머지 파일(db.py, requirements.txt 등)은 원본 그대로 사용합니다.
