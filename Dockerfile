# Railway에서 GitHub 저장소를 연결하면 Dockerfile을 자동 감지합니다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 공개 HTTP 포트가 필요하지 않습니다. Discord로 외부 연결하는 상주 작업입니다.
CMD ["python", "src/bot.py"]
