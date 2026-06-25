#!/usr/bin/env bash
# RunGuard デプロイスクリプト（Git Bash / Cloud Shell で実行）。
# ★前提: 個人アカウントの新規 GCP プロジェクトで、env.sh を設定して source 済み。
#   未実行テンプレート: GCP アカウント準備後に実行・調整する。
#
# 構成:
#   A) runguard-agent           … エージェント本体（既定は sim モードで即デモ可）
#   B) Cloud Scheduler          … /api/tick を定期実行（スケールゼロ両立）
#   C) [REAL モード用] sample-service の正常/不調リビジョン + agent への IAM 付与
#      observe/rollback をライブ配線（build_observation 実装 + main.py の real モード）後に有効化。
set -euo pipefail

: "${PROJECT_ID:?env.sh を source してください（PROJECT_ID 未設定）}"
REGION="${REGION:-asia-northeast1}"
AGENT_SERVICE="${AGENT_SERVICE:-runguard-agent}"
TARGET="${TARGET_SERVICES%%,*}"; TARGET="${TARGET:-sample-service}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.5-flash}"
USE_VERTEX="${GOOGLE_GENAI_USE_VERTEXAI:-true}"
AUTO_ACT_THRESHOLD="${AUTO_ACT_THRESHOLD:-0.8}"

echo "==> アカウント/プロジェクト確認（個人アカウントであること）"
gcloud config get-value account
gcloud config set project "$PROJECT_ID"

echo "==> 必要 API 有効化"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com logging.googleapis.com monitoring.googleapis.com \
  aiplatform.googleapis.com cloudscheduler.googleapis.com

# ---- A) runguard-agent ----
echo "==> runguard-agent デプロイ（リポジトリ直下の Dockerfile を使用）"
SCHED_TOKEN="${SCHEDULER_TOKEN:-$("${PYTHON:-python}" -c 'import secrets;print(secrets.token_urlsafe(24))' 2>/dev/null || echo please-set-a-token)}"
gcloud run deploy "$AGENT_SERVICE" --source . --region "$REGION" \
  --allow-unauthenticated --min-instances 0 --memory 512Mi --quiet \
  --set-env-vars "PROJECT_ID=$PROJECT_ID,REGION=$REGION,AGENT_SERVICE=$AGENT_SERVICE,TARGET_SERVICES=$TARGET,GEMINI_MODEL=$GEMINI_MODEL,GOOGLE_GENAI_USE_VERTEXAI=$USE_VERTEX,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION,AUTO_ACT_THRESHOLD=$AUTO_ACT_THRESHOLD,STORE_BACKEND=firestore,SCHEDULER_TOKEN=$SCHED_TOKEN"

AGENT_URL=$(gcloud run services describe "$AGENT_SERVICE" --region "$REGION" --format='value(status.url)')
echo "    agent URL: $AGENT_URL"

# ---- B) Cloud Scheduler（/api/tick を5分毎） ----
echo "==> Cloud Scheduler 設定"
gcloud scheduler jobs create http runguard-tick --location "$REGION" \
  --schedule="*/5 * * * *" --uri="$AGENT_URL/api/tick" --http-method=POST \
  --headers="X-RunGuard-Token=$SCHED_TOKEN" --quiet 2>/dev/null \
  || gcloud scheduler jobs update http runguard-tick --location "$REGION" \
       --schedule="*/5 * * * *" --uri="$AGENT_URL/api/tick" --http-method=POST \
       --headers="X-RunGuard-Token=$SCHED_TOKEN" --quiet

echo "==> 完了（sim デモ）: $AGENT_URL を開く → 「障害を注入」"

# ---- C) REAL モード用（observe/rollback をライブ配線したら有効化） ----
# observe.build_observation の実装 + main.py の real モード切替が済んでから下記を有効化する。
#
# # sample-service: 正常 / 不調 の2リビジョン
# gcloud run deploy "$TARGET" --source ./sample_service --region "$REGION" \
#   --allow-unauthenticated --min-instances 0 --set-env-vars FAULT=ok --tag healthy --quiet
# gcloud run deploy "$TARGET" --source ./sample_service --region "$REGION" \
#   --allow-unauthenticated --min-instances 0 --set-env-vars FAULT=http500 --no-traffic --tag bad --quiet
#
# # agent の実行 SA に必要権限（ロールバック=run.admin / Firestore / ログ / メトリクス / Vertex）
# PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
# SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
# for ROLE in roles/run.admin roles/datastore.user roles/logging.viewer roles/monitoring.viewer roles/aiplatform.user; do
#   gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role="$ROLE" --quiet
# done
