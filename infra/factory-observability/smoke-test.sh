#!/usr/bin/env bash
set -euo pipefail

# End-to-end smoke test for the local LiteLLM AI Gateway + OTel Collector + Grafana Cloud pipeline.
# Reads exclusively from runtime environment variables (never sources .env files from disk).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
LITELLM_URL="${LITELLM_URL:-http://127.0.0.1:4000}"

if [ -z "${LITELLM_MASTER_KEY:-}" ]; then
  echo "ERROR: LITELLM_MASTER_KEY must be set in the runtime environment." >&2
  exit 1
fi

for cmd in curl jq docker; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: Required command '$cmd' is not installed." >&2
    exit 1
  fi
done

START_TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

echo "1/4 Checking LiteLLM readiness (${LITELLM_URL}/health/readiness)..."
READINESS_STATUS="$(curl -sS -o /tmp/shed-litellm-readiness.json -w '%{http_code}' "${LITELLM_URL}/health/readiness")"
if [ "$READINESS_STATUS" != "200" ]; then
  echo "ERROR: Readiness check returned HTTP ${READINESS_STATUS}" >&2
  cat /tmp/shed-litellm-readiness.json >&2 || true
  exit 1
fi
echo "    OK (HTTP 200)"

echo "2/4 Checking configured model aliases (${LITELLM_URL}/v1/models)..."
MODELS_STATUS="$(curl -sS -o /tmp/shed-litellm-models.json -w '%{http_code}' \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  "${LITELLM_URL}/v1/models")"
if [ "$MODELS_STATUS" != "200" ]; then
  echo "ERROR: /v1/models returned HTTP ${MODELS_STATUS}" >&2
  cat /tmp/shed-litellm-models.json >&2 || true
  exit 1
fi

for alias in factory-fast factory-builder factory-complex; do
  if ! jq -e --arg m "$alias" '.data[] | select(.id == $m)' /tmp/shed-litellm-models.json >/dev/null; then
    echo "ERROR: Expected model alias '${alias}' was not returned by /v1/models." >&2
    jq -r '.data[].id' /tmp/shed-litellm-models.json >&2 || true
    exit 1
  fi
done
echo "    OK (found factory-fast, factory-builder, factory-complex)"

echo "3/4 Sending FinOps-tagged completion request to factory-fast..."
COMPLETION_STATUS="$(curl -sS -o /tmp/shed-litellm-completion.json -w '%{http_code}' \
  -X POST "${LITELLM_URL}/v1/chat/completions" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "factory-fast",
    "max_tokens": 16,
    "messages": [
      { "role": "user", "content": "Reply with exactly OK." }
    ],
    "metadata": {
      "repo": "steve-hawkins/shed",
      "stage": "smoke-test",
      "issue": "0",
      "lane": "observability",
      "attempt": 1
    }
  }')"

if [ "$COMPLETION_STATUS" != "200" ]; then
  echo "ERROR: /v1/chat/completions returned HTTP ${COMPLETION_STATUS}" >&2
  cat /tmp/shed-litellm-completion.json >&2 || true
  exit 1
fi

ASSISTANT_CONTENT="$(jq -r '.choices[0].message.content // empty' /tmp/shed-litellm-completion.json)"
if [ -z "$ASSISTANT_CONTENT" ]; then
  echo "ERROR: Completion response contained empty assistant content." >&2
  cat /tmp/shed-litellm-completion.json >&2 || true
  exit 1
fi
echo "    OK (HTTP 200, non-empty response received)"

echo "4/4 Waiting 6s for OTel batch flush and inspecting otel-collector logs..."
sleep 6

OTEL_LOGS="$(docker compose -f "$COMPOSE_FILE" logs --since "$START_TS" --no-color otel-collector 2>&1)"

if ! echo "$OTEL_LOGS" | grep -Eq 'Traces|spans|ResourceSpans'; then
  echo "ERROR: Did not observe received trace spans in otel-collector logs since ${START_TS}." >&2
  echo "$OTEL_LOGS" >&2
  exit 1
fi

if echo "$OTEL_LOGS" | grep -Eiq 'Exporting failed|401 Unauthorized|403 Forbidden|404 Not Found|connection refused|Permanent error'; then
  echo "ERROR: Detected OTLP export error in otel-collector logs:" >&2
  echo "$OTEL_LOGS" >&2
  exit 1
fi

echo "    OK (spans received by otel-collector with zero Grafana Cloud export errors)"
echo ""
echo "All smoke-test checks passed."
