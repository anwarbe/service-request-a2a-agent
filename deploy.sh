#!/usr/bin/env bash
# Automated Cloud Run deployment script for Service Request A2A Agent
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-service-request-a2a-agent}"
BUCKET_NAME="${BUCKET_NAME:-${PROJECT_ID}-oracle-hackathon-tickets}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "Error: PROJECT_ID is not set. Run: export PROJECT_ID='your-gcp-project-id'"
  exit 1
fi

echo "==> Using GCP Project: ${PROJECT_ID} | Region: ${REGION}"
gcloud config set project "${PROJECT_ID}"

echo "==> 1. Enabling required Google Cloud APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com

echo "==> 2. Ensuring GCS bucket gs://${BUCKET_NAME} exists..."
if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET_NAME}" --location="${REGION}"
else
  echo "    Bucket gs://${BUCKET_NAME} already exists."
fi

echo "==> 3. Deploying ${SERVICE_NAME} to Cloud Run from source..."
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region="${REGION}" \
  --platform=managed \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GCS_BUCKET_NAME=${BUCKET_NAME},GEMINI_MODEL=${GEMINI_MODEL}"

echo "==> 4. Updating CLOUD_RUN_URL so A2A Agent Card self-reports its live HTTPS URL..."
SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" --format='value(status.url)')"

# IMPORTANT: Use --update-env-vars (NOT --set-env-vars) so existing env vars are preserved!
gcloud run services update "${SERVICE_NAME}" \
  --region="${REGION}" \
  --update-env-vars="CLOUD_RUN_URL=${SERVICE_URL}"

echo ""
echo "✅ Deployment complete!"
echo "   Service URL:     ${SERVICE_URL}"
echo "   Health Check:    ${SERVICE_URL}/health"
echo "   A2A Agent Card:  ${SERVICE_URL}/.well-known/agent-card.json"
