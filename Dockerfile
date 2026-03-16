FROM python:3.12-slim

WORKDIR /app

# 의존성 먼저 설치 (캐시 최적화)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY . .

# Railway가 PORT 환경변수를 주입함
EXPOSE ${PORT:-3000}

CMD ["sh", "-c", "uvicorn bot:api --host 0.0.0.0 --port ${PORT:-3000}"]
