#!/usr/bin/env bash
set -euo pipefail

# End-to-end smoke test for the `agy -> OTel Collector -> Grafana Cloud` pipeline.
# Runs synthetic fixture verification by default (zero LLM quota consumed).
# Pass `--live` to also execute a minimal real `agy -p` turn and verify live hook firing.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
HOOK_SCRIPT="${REPO_ROOT}/.agents/hooks/otel-telemetry.py"
RUN_LIVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --live)
      RUN_LIVE=1
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--live]"
      echo "  --live  Also execute a minimal real 'agy -p' turn to verify live hook firing"
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument '$1'." >&2
      exit 1
      ;;
  esac
done

for cmd in curl jq docker python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: Required command '$cmd' is not installed." >&2
    exit 1
  fi
done

START_TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

echo "1/4 Checking OTel Collector health (http://127.0.0.1:13133/)..."
HEALTH_STATUS="$(curl -sS -o /tmp/shed-otel-health.json -w '%{http_code}' http://127.0.0.1:13133/)"
if [ "$HEALTH_STATUS" != "200" ]; then
  echo "ERROR: OTel Collector health check returned HTTP ${HEALTH_STATUS}" >&2
  cat /tmp/shed-otel-health.json >&2 || true
  exit 1
fi
echo "    OK (HTTP 200)"

echo "2/4 Verifying telemetry hook (synthetic fixture, idempotency, and fail-open behavior)..."
TMP_DIR="$(mktemp -d /tmp/shed-agy-smoke-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

NOW_ISO="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
FIXTURE_TRANSCRIPT="${TMP_DIR}/transcript.jsonl"
cat >"$FIXTURE_TRANSCRIPT" <<EOF
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "${NOW_ISO}", "content": "SECRET_SHOULD_NEVER_EXPORT"}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "${NOW_ISO}", "model": "gemini-4-neon-high", "input_tokens": 2500, "cache_read_tokens": 8000, "output_tokens": 420, "tool_calls": [{"name": "read_file", "args": {"Path": "/secret"}}]}
{"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "${NOW_ISO}"}
{"step_index": 3, "source": "MODEL", "type": "GENERIC", "status": "ERROR", "created_at": "${NOW_ISO}"}
{"step_index": 4, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "${NOW_ISO}", "model": "gemini-4-neon-high", "input_tokens": 1200, "cache_read_tokens": 10500, "output_tokens": 180, "tool_calls": []}
EOF

CONV_ID="smoke-test-$(date -u +%s)"
HOOK_INPUT="$(jq -nc \
  --arg cid "$CONV_ID" \
  --arg tp "$FIXTURE_TRANSCRIPT" \
  --arg cwd "$REPO_ROOT" \
  '{conversationId: $cid, transcriptPath: $tp, modelName: "gemini-4-neon-high", terminationReason: "NO_TOOL_CALL", executionNum: 1, cwd: $cwd}')"

HOOK_OUT="$(printf '%s' "$HOOK_INPUT" | \
  SHED_OTEL_STATE_DIR="${TMP_DIR}/state" \
  SHED_STAGE="smoke-test" \
  SHED_ISSUE="0" \
  SHED_LANE="observability" \
  python3 "$HOOK_SCRIPT")"

if [ "$HOOK_OUT" != "{}" ]; then
  echo "ERROR: Expected hook stdout '{}' but got: ${HOOK_OUT}" >&2
  exit 1
fi

# Re-run with same transcript/state to verify watermark prevents duplicate emission
HOOK_OUT_DUP="$(printf '%s' "$HOOK_INPUT" | \
  SHED_OTEL_STATE_DIR="${TMP_DIR}/state" \
  SHED_STAGE="smoke-test" \
  python3 "$HOOK_SCRIPT")"

if [ "$HOOK_OUT_DUP" != "{}" ]; then
  echo "ERROR: Expected duplicate hook stdout '{}' but got: ${HOOK_OUT_DUP}" >&2
  exit 1
fi

# Verify fail-open when collector endpoint is unreachable
HOOK_OUT_OFFLINE="$(printf '%s' "$HOOK_INPUT" | \
  SHED_OTEL_STATE_DIR="${TMP_DIR}/state-offline" \
  OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:19999" \
  python3 "$HOOK_SCRIPT")"

if [ "$HOOK_OUT_OFFLINE" != "{}" ]; then
  echo "ERROR: Expected fail-open stdout '{}' when collector is offline, got: ${HOOK_OUT_OFFLINE}" >&2
  exit 1
fi
echo "    OK (hook emitted cleanly, watermark deduplicated, fail-open verified)"

if [ "$RUN_LIVE" -eq 1 ]; then
  echo "3/4 Running live headless agy turn via run-agent.sh..."
  "${SCRIPT_DIR}/run-agent.sh" \
    --stage smoke-test \
    --issue 0 \
    --lane observability \
    --prompt "Reply with exactly OK."
  echo "    OK (live agy turn completed)"
else
  echo "3/4 Skipping live agy turn (pass --live to include a real agy -p invocation)"
fi

echo "4/4 Waiting 6s for OTel batch flush and inspecting otel-collector logs..."
sleep 6

OTEL_LOGS="$(docker compose -f "$COMPOSE_FILE" logs --since "$START_TS" --no-color otel-collector 2>&1)"

if ! echo "$OTEL_LOGS" | grep -Eq 'Traces|ResourceSpans'; then
  echo "ERROR: Did not observe received traces in otel-collector logs since ${START_TS}." >&2
  echo "$OTEL_LOGS" >&2
  exit 1
fi

if ! echo "$OTEL_LOGS" | grep -Eq 'Metrics|ResourceMetrics'; then
  echo "ERROR: Did not observe received metrics in otel-collector logs since ${START_TS}." >&2
  echo "$OTEL_LOGS" >&2
  exit 1
fi

if echo "$OTEL_LOGS" | grep -Eiq 'Exporting failed|401 Unauthorized|403 Forbidden|404 Not Found|connection refused|Permanent error|temporality'; then
  echo "ERROR: Detected OTLP export error in otel-collector logs:" >&2
  echo "$OTEL_LOGS" >&2
  exit 1
fi

echo "    OK (traces and metrics received by otel-collector with zero Grafana Cloud export errors)"
echo ""
echo "All agy -> OTel -> Grafana smoke-test checks passed."
