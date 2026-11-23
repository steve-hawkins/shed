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

echo "2/4 Verifying telemetry hooks (full lifecycle, concurrency, watermark, and fail-open)..."
TMP_DIR="$(mktemp -d /tmp/shed-agy-smoke-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

NOW_ISO="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
FIXTURE_TRANSCRIPT="${TMP_DIR}/transcript.jsonl"
cat >"$FIXTURE_TRANSCRIPT" <<EOF
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "${NOW_ISO}", "content": "SECRET_SHOULD_NEVER_EXPORT"}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "${NOW_ISO}", "model": "gemini-4-neon-high", "input_tokens": 2500, "cache_read_tokens": 8000, "output_tokens": 420, "tool_calls": [{"name": "read_file", "args": {"Path": "/secret"}}]}
{"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "${NOW_ISO}"}
EOF

CONV_ID="smoke-test-$(date -u +%s)"
STATE_DIR="${TMP_DIR}/state"

run_hook() {
  local payload="$1"
  local state_dir="$2"
  local endpoint="${3:-http://127.0.0.1:4318}"
  printf '%s' "$payload" | \
    SHED_OTEL_STATE_DIR="$state_dir" \
    OTEL_EXPORTER_OTLP_ENDPOINT="$endpoint" \
    SHED_STAGE="smoke-test" \
    SHED_ISSUE="0" \
    SHED_LANE="observability" \
    python3 "$HOOK_SCRIPT"
}

SESSION_START_IN="$(jq -nc --arg cid "$CONV_ID" --arg cwd "$REPO_ROOT" \
  '{hookEventName: "SessionStart", conversationId: $cid, modelName: "gemini-4-neon-high", cwd: $cwd}')"
PRE_INV_IN="$(jq -nc --arg cid "$CONV_ID" --arg cwd "$REPO_ROOT" \
  '{hookEventName: "PreInvocation", conversationId: $cid, modelName: "gemini-4-neon-high", invocationNum: 1, initialNumSteps: 1, cwd: $cwd}')"
POST_TOOL_1_IN="$(jq -nc --arg cid "$CONV_ID" --arg cwd "$REPO_ROOT" \
  '{hookEventName: "PostToolUse", conversationId: $cid, modelName: "gemini-4-neon-high", stepIdx: 2, toolCall: {name: "read_file", args: {Path: "/secret"}}, cwd: $cwd}')"
POST_TOOL_2_IN="$(jq -nc --arg cid "$CONV_ID" --arg cwd "$REPO_ROOT" \
  '{hookEventName: "PostToolUse", conversationId: $cid, modelName: "gemini-4-neon-high", stepIdx: 3, toolCall: {name: "run_command", args: {CommandLine: "secret"}}, error: "SECRET_ERROR_TEXT", cwd: $cwd}')"
POST_INV_IN="$(jq -nc --arg cid "$CONV_ID" --arg tp "$FIXTURE_TRANSCRIPT" --arg cwd "$REPO_ROOT" \
  '{hookEventName: "PostInvocation", conversationId: $cid, transcriptPath: $tp, modelName: "gemini-4-neon-high", cwd: $cwd}')"
STOP_IN="$(jq -nc --arg cid "$CONV_ID" --arg tp "$FIXTURE_TRANSCRIPT" --arg cwd "$REPO_ROOT" \
  '{hookEventName: "Stop", conversationId: $cid, transcriptPath: $tp, modelName: "gemini-4-neon-high", terminationReason: "NO_TOOL_CALL", executionNum: 1, cwd: $cwd}')"

[ "$(run_hook "$SESSION_START_IN" "$STATE_DIR")" = "{}" ] || { echo "ERROR: SessionStart hook failed" >&2; exit 1; }
[ "$(run_hook "$PRE_INV_IN" "$STATE_DIR")" = "{}" ] || { echo "ERROR: PreInvocation hook failed" >&2; exit 1; }

# Run concurrent PostToolUse and PostInvocation hooks to verify file locking preserves state & watermark
run_hook "$POST_TOOL_1_IN" "$STATE_DIR" >"${TMP_DIR}/pt1.out" &
PID_PT1=$!
run_hook "$POST_TOOL_2_IN" "$STATE_DIR" >"${TMP_DIR}/pt2.out" &
PID_PT2=$!
run_hook "$POST_INV_IN" "$STATE_DIR" >"${TMP_DIR}/pi.out" &
PID_PI=$!
wait "$PID_PT1" "$PID_PT2" "$PID_PI"

for out_file in "${TMP_DIR}/pt1.out" "${TMP_DIR}/pt2.out" "${TMP_DIR}/pi.out"; do
  if [ "$(cat "$out_file")" != "{}" ]; then
    echo "ERROR: Expected '{}' from concurrent hook, got: $(cat "$out_file")" >&2
    exit 1
  fi
done

STATE_FILE="$(find "$STATE_DIR" -maxdepth 1 -name '*.json' | head -n 1)"
if [ -z "$STATE_FILE" ] || [ "$(jq -r '.last_step_index' "$STATE_FILE")" != "2" ]; then
  echo "ERROR: Concurrent hooks corrupted state or rolled back last_step_index (expected 2):" >&2
  cat "$STATE_FILE" >&2 || true
  exit 1
fi

# Append remaining steps and invoke Stop
cat >>"$FIXTURE_TRANSCRIPT" <<EOF
{"step_index": 3, "source": "MODEL", "type": "GENERIC", "status": "ERROR", "created_at": "${NOW_ISO}"}
{"step_index": 4, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "${NOW_ISO}", "model": "gemini-4-neon-high", "input_tokens": 1200, "cache_read_tokens": 10500, "output_tokens": 180, "tool_calls": []}
EOF

HOOK_OUT="$(run_hook "$STOP_IN" "$STATE_DIR")"
if [ "$HOOK_OUT" != "{}" ]; then
  echo "ERROR: Expected Stop hook stdout '{}' but got: ${HOOK_OUT}" >&2
  exit 1
fi

if [ "$(jq -r '.last_step_index' "$STATE_FILE")" != "4" ] || [ "$(jq -r 'has("turn")' "$STATE_FILE")" != "false" ]; then
  echo "ERROR: Stop hook did not finalize watermark/turn cleanly:" >&2
  cat "$STATE_FILE" >&2 || true
  exit 1
fi

# Re-run Stop with same transcript/state to verify watermark prevents duplicate emission
HOOK_OUT_DUP="$(run_hook "$STOP_IN" "$STATE_DIR")"
if [ "$HOOK_OUT_DUP" != "{}" ]; then
  echo "ERROR: Expected duplicate hook stdout '{}' but got: ${HOOK_OUT_DUP}" >&2
  exit 1
fi

# Verify fail-open AND that offline collector does not advance watermark for tool-error-only steps
TOOL_ONLY_TRANSCRIPT="${TMP_DIR}/tool-only-transcript.jsonl"
cat >"$TOOL_ONLY_TRANSCRIPT" <<EOF
{"step_index": 0, "source": "MODEL", "type": "GENERIC", "status": "ERROR", "created_at": "${NOW_ISO}"}
EOF
OFFLINE_STATE_DIR="${TMP_DIR}/state-offline"
OFFLINE_POST_INV="$(jq -nc --arg cid "${CONV_ID}-offline" --arg tp "$TOOL_ONLY_TRANSCRIPT" --arg cwd "$REPO_ROOT" \
  '{hookEventName: "PostInvocation", conversationId: $cid, transcriptPath: $tp, modelName: "gemini-4-neon-high", cwd: $cwd}')"
HOOK_OUT_OFFLINE="$(run_hook "$OFFLINE_POST_INV" "$OFFLINE_STATE_DIR" "http://127.0.0.1:19999")"
if [ "$HOOK_OUT_OFFLINE" != "{}" ]; then
  echo "ERROR: Expected fail-open stdout '{}' when collector is offline, got: ${HOOK_OUT_OFFLINE}" >&2
  exit 1
fi
if find "$OFFLINE_STATE_DIR" -maxdepth 1 -name '*.json' | grep -q .; then
  OFFLINE_STATE_FILE="$(find "$OFFLINE_STATE_DIR" -maxdepth 1 -name '*.json' | head -n 1)"
  if [ "$(jq -r '.last_step_index' "$OFFLINE_STATE_FILE")" != "-1" ]; then
    echo "ERROR: Offline PostInvocation advanced watermark for unexported tool-error metrics:" >&2
    cat "$OFFLINE_STATE_FILE" >&2 || true
    exit 1
  fi
fi
echo "    OK (full lifecycle emitted cleanly, concurrent state locked, watermark deduplicated, offline fail-open preserved watermark)"

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

if ! echo "$OTEL_LOGS" | grep -Eq 'Logs|ResourceLogs'; then
  echo "ERROR: Did not observe received logs in otel-collector logs since ${START_TS}." >&2
  echo "$OTEL_LOGS" >&2
  exit 1
fi

if echo "$OTEL_LOGS" | grep -Eiq 'Exporting failed|401 Unauthorized|403 Forbidden|404 Not Found|connection refused|Permanent error|temporality'; then
  echo "ERROR: Detected OTLP export error in otel-collector logs:" >&2
  echo "$OTEL_LOGS" >&2
  exit 1
fi

echo "    OK (traces, metrics, and logs received by otel-collector with zero Grafana Cloud export errors)"
echo ""
echo "All agy -> OTel -> Grafana smoke-test checks passed."
