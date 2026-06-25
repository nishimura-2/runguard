# sample-service — 監視対象の合成アプリ（標準ライブラリのみ・追加依存なし）
FROM python:3.12-slim

WORKDIR /app
COPY main.py .

# Cloud Run は $PORT を注入する。FAULT は実行時に上書きして障害モードを切替える。
ENV PORT=8080 \
    FAULT=ok

# 標準ライブラリのみなので pip install は不要。
CMD ["python", "main.py"]
