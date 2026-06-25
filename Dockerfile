# runguard-agent — FastAPI + ADK エージェント本体（Cloud Run）
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/
COPY dashboard/ ./dashboard/

ENV PORT=8080
# Cloud Run は $PORT を注入する。
CMD ["sh", "-c", "uvicorn agent.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
