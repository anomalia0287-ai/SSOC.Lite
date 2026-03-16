FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py db.py migrate_json_to_sqlite.py ./

# Railway 에서 Volume 을 /data 에 마운트
# DB_PATH 기본값이 /data/notice_bot.db
EXPOSE 3000

CMD ["uvicorn", "bot:api", "--host", "0.0.0.0", "--port", "3000"]
