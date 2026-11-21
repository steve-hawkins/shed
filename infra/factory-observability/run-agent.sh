#!/usr/bin/env bash
set -euo pipefail

# Headless factory runner wrapper for Antigravity CLI (`agy -p --output-format json`).
# Injects Shed FinOps attribution environment variables so `.agents/hooks.json`
# automatically tags exported OpenTelemetry traces and metrics with stage/issue/lane/attempt.

STAGE="builder"
ISSUE="unassigned"
LANE="feature"
ATTEMPT="1"
MODEL=""
AGENT=""
PROMPT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --stage)
      STAGE="$2"
      shift 2
      ;;
    --issue)
      ISSUE="$2"
      shift 2
      ;;
    --lane)
      LANE="$2"
      shift 2
      ;;
    --attempt)
      ATTEMPT="$2"
      shift 2
      ;;
    --model)
      MODEL="$2"
      shift 2
      ;;
    --agent)
      AGENT="$2"
      shift 2
      ;;
    --prompt)
      PROMPT="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 --prompt <text> [--stage builder|critic|planner] [--issue <id>] [--lane feature|bugfix|chore] [--attempt <n>] [--model <id>] [--agent <name>]"
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument '$1'." >&2
      exit 1
      ;;
  esac
done

if [ -z "$PROMPT" ]; then
  echo "ERROR: --prompt is required." >&2
  exit 1
fi

if ! command -v agy >/dev/null 2>&1; then
  echo "ERROR: 'agy' CLI is not installed or not on PATH." >&2
  exit 1
fi

export SHED_STAGE="$STAGE"
export SHED_ISSUE="$ISSUE"
export SHED_LANE="$LANE"
export SHED_ATTEMPT="$ATTEMPT"

cmd=(agy -p "$PROMPT" --output-format json)
if [ -n "$MODEL" ]; then
  cmd+=(--model "$MODEL")
fi
if [ -n "$AGENT" ]; then
  cmd+=(--agent "$AGENT")
fi

TMP_OUT="$(mktemp /tmp/shed-agy-run-XXXXXX.json)"
trap 'rm -f "$TMP_OUT"' EXIT

set +e
"${cmd[@]}" >"$TMP_OUT"
EXIT_CODE=$?
set -e

if command -v jq >/dev/null 2>&1 && jq empty "$TMP_OUT" >/dev/null 2>&1; then
  jq '{
    conversation_id,
    status,
    duration_seconds,
    num_turns,
    usage,
    stage: env.SHED_STAGE,
    issue: env.SHED_ISSUE,
    lane: env.SHED_LANE,
    attempt: env.SHED_ATTEMPT
  }' "$TMP_OUT"
else
  cat "$TMP_OUT"
fi

exit "$EXIT_CODE"
