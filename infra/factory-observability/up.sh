#!/usr/bin/env bash
set -euo pipefail

# Start the local LiteLLM AI Gateway + PostgreSQL + OpenTelemetry Collector stack.
# Reads exclusively from runtime environment variables (never sources .env files from disk).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

missing_vars=()
for var in LITELLM_MASTER_KEY LITELLM_SALT_KEY GRAFANA_OTLP_ENDPOINT GRAFANA_OTLP_AUTH_HEADER; do
  if [ -z "${!var:-}" ]; then
    missing_vars+=("$var")
  fi
done

if [ "${#missing_vars[@]}" -gt 0 ]; then
  echo "ERROR: Missing required runtime environment variable(s): ${missing_vars[*]}" >&2
  echo "Configure them as GitHub Codespaces secrets (see .devcontainer/devcontainer.json) or export them in your shell." >&2
  exit 1
fi

if [[ "${LITELLM_MASTER_KEY}" != sk-* ]]; then
  echo "ERROR: LITELLM_MASTER_KEY must start with 'sk-'." >&2
  exit 1
fi

if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "ERROR: At least one LLM provider API key must be set (GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY)." >&2
  exit 1
fi

echo "Starting Shed factory observability stack (LiteLLM + PostgreSQL + OTel Collector)..."
docker compose -f "$COMPOSE_FILE" up -d --build --wait

echo ""
echo "Service readiness status:"
docker compose -f "$COMPOSE_FILE" ps
echo ""
echo "LiteLLM AI Gateway: http://127.0.0.1:4000"
echo "OTel Collector OTLP: 127.0.0.1:4317 (gRPC) / 127.0.0.1:4318 (HTTP)"
