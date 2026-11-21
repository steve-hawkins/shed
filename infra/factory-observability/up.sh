#!/usr/bin/env bash
set -euo pipefail

# Start the local Shed AI Observability stack.
# Default (--otel-only): starts OpenTelemetry Collector only (for agy -> OTel -> Grafana Cloud).
# Optional (--with-litellm): also starts PostgreSQL + LiteLLM AI Gateway.
# Reads exclusively from runtime environment variables (never sources .env files from disk).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
MODE="otel-only"

while [ $# -gt 0 ]; do
  case "$1" in
    --otel-only)
      MODE="otel-only"
      shift
      ;;
    --with-litellm)
      MODE="with-litellm"
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--otel-only | --with-litellm]"
      echo "  --otel-only     Start OTel Collector only (default; for agy -> OTel -> Grafana)"
      echo "  --with-litellm  Start OTel Collector + PostgreSQL + LiteLLM AI Gateway"
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument '$1'. Use --otel-only or --with-litellm." >&2
      exit 1
      ;;
  esac
done

missing_vars=()
for var in GRAFANA_OTLP_ENDPOINT GRAFANA_OTLP_AUTH_HEADER; do
  if [ -z "${!var:-}" ]; then
    missing_vars+=("$var")
  fi
done

if [ "$MODE" = "with-litellm" ]; then
  for var in LITELLM_MASTER_KEY LITELLM_SALT_KEY; do
    if [ -z "${!var:-}" ]; then
      missing_vars+=("$var")
    fi
  done
fi

if [ "${#missing_vars[@]}" -gt 0 ]; then
  echo "ERROR: Missing required runtime environment variable(s): ${missing_vars[*]}" >&2
  echo "Configure them as GitHub Codespaces secrets (see .devcontainer/devcontainer.json) or export them in your shell." >&2
  exit 1
fi

if [ "$MODE" = "with-litellm" ]; then
  if [[ "${LITELLM_MASTER_KEY}" != sk-* ]]; then
    echo "ERROR: LITELLM_MASTER_KEY must start with 'sk-'." >&2
    exit 1
  fi
  if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: At least one LLM provider API key must be set (GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY)." >&2
    exit 1
  fi
  echo "Starting Shed factory observability stack (OTel Collector + PostgreSQL + LiteLLM)..."
  docker compose -f "$COMPOSE_FILE" --profile litellm up -d --build --wait
else
  echo "Starting Shed factory observability stack (OTel Collector only)..."
  docker compose -f "$COMPOSE_FILE" up -d --build
fi

echo "Waiting for OTel Collector health endpoint (http://127.0.0.1:13133/)..."
ready=0
for _ in $(seq 1 30); do
  if curl -sSf http://127.0.0.1:13133/ >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done

if [ "$ready" -ne 1 ]; then
  echo "ERROR: OTel Collector health check did not become ready within 30s." >&2
  docker compose -f "$COMPOSE_FILE" logs --tail=30 otel-collector >&2 || true
  exit 1
fi

echo ""
echo "Service readiness status:"
if [ "$MODE" = "with-litellm" ]; then
  docker compose -f "$COMPOSE_FILE" --profile litellm ps
else
  docker compose -f "$COMPOSE_FILE" ps
fi
echo ""
echo "OTel Collector OTLP:   127.0.0.1:4317 (gRPC) / 127.0.0.1:4318 (HTTP)"
echo "OTel Collector Health: http://127.0.0.1:13133/"
if [ "$MODE" = "with-litellm" ]; then
  echo "LiteLLM AI Gateway:    http://127.0.0.1:4000"
fi
