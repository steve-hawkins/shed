#!/usr/bin/env bash
set -euo pipefail

# Headless factory runner wrapper for Antigravity CLI (`agy -p`).
# Injects Shed FinOps attribution environment variables so `.agents/hooks.json`
# automatically tags exported OpenTelemetry traces, metrics, and logs with
# stage/issue/lane/attempt.
#
# By default, streams live privacy-safe progress to stderr via
# `--output-format stream-json` while still emitting a clean final JSON summary
# on stdout. Pass `--quiet` to use buffered `--output-format json`.

STAGE="builder"
ISSUE="unassigned"
LANE="feature"
ATTEMPT="1"
MODEL=""
AGENT=""
PROMPT=""
QUIET="false"

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
    --quiet)
      QUIET="true"
      shift 1
      ;;
    -h|--help)
      echo "Usage: $0 --prompt <text> [--stage builder|critic|planner] [--issue <id>] [--lane feature|bugfix|chore] [--attempt <n>] [--model <id>] [--agent <name>] [--quiet]"
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

if [ "$QUIET" = "true" ]; then
  cmd=(agy -p "$PROMPT" --output-format json)
else
  cmd=(agy -p "$PROMPT" --output-format stream-json)
fi

if [ -n "$MODEL" ]; then
  cmd+=(--model "$MODEL")
fi
if [ -n "$AGENT" ]; then
  cmd+=(--agent "$AGENT")
fi

if [ "$QUIET" = "true" ]; then
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
fi

# Live streaming mode: print timestamped, privacy-safe progress to stderr
# and emit the final structured summary JSON to stdout.
set +e
"${cmd[@]}" | python3 -u -c '
import datetime as dt
import json
import os
import sys

stage = os.environ.get("SHED_STAGE", "builder")
issue = os.environ.get("SHED_ISSUE", "unassigned")
lane = os.environ.get("SHED_LANE", "feature")
attempt = os.environ.get("SHED_ATTEMPT", "1")
prefix = f"[{stage} #{issue}]"

final_summary = None
conversation_id = None
step_count = 0
started_at = dt.datetime.now(dt.timezone.utc)


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    sys.stderr.write(f"[{ts}] {prefix} {msg}\n")
    sys.stderr.flush()


log(f"starting headless run (lane={lane}, attempt={attempt})")

for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    if not isinstance(ev, dict):
        continue

    cid = ev.get("conversation_id") or ev.get("conversationId") or ev.get("session_id")
    if cid and not conversation_id:
        conversation_id = str(cid)
        log(f"session {conversation_id}")

    ev_type = str(ev.get("type") or ev.get("event") or "").lower()

    # Final result payload (agy stream-json terminal event)
    if ev_type in ("result", "done", "final") or (
        "duration_seconds" in ev and "status" in ev
    ):
        final_summary = ev
        status = ev.get("status") or "done"
        dur = ev.get("duration_seconds")
        turns = ev.get("num_turns")
        dur_txt = f"{dur:.1f}s" if isinstance(dur, (int, float)) else "?"
        log(f"completed status={status} turns={turns} duration={dur_txt}")
        continue

    # Privacy-safe tool call progress (never prints tool args, prompts, or thinking)
    tool_calls = ev.get("tool_calls") or []
    if isinstance(tool_calls, list) and tool_calls:
        names = [str(tc.get("name")) for tc in tool_calls if isinstance(tc, dict) and tc.get("name")]
        if names:
            step_count += 1
            tools_joined = ", ".join(names)
            log(f"step {step_count}: calling tools [{tools_joined}]")
            continue

    tool_name = ev.get("tool_name") or ev.get("toolName")
    if not tool_name and isinstance(ev.get("tool_call"), dict):
        tool_name = ev["tool_call"].get("name")
    if not tool_name and isinstance(ev.get("toolCall"), dict):
        tool_name = ev["toolCall"].get("name")
    if tool_name:
        step_count += 1
        status = ev.get("status") or ("error" if ev.get("error") else "running")
        log(f"step {step_count}: tool {tool_name} ({status})")
        continue

    # Token usage update on model responses
    usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else None
    inp = ev.get("input_tokens") if usage is None else usage.get("input_tokens")
    out = ev.get("output_tokens") if usage is None else usage.get("output_tokens")
    cache = ev.get("cache_read_tokens") if usage is None else usage.get("cache_read_tokens")
    if any(isinstance(v, int) and v > 0 for v in (inp, out, cache)):
        step_count += 1
        model = ev.get("model") or ""
        model_txt = f" {model}" if model else ""
        log(f"step {step_count}: model{model_txt} (in={inp or 0}, cache={cache or 0}, out={out or 0})")
        continue

    if ev_type in ("error", "failed"):
        log("event: error")

elapsed = round((dt.datetime.now(dt.timezone.utc) - started_at).total_seconds(), 2)
if final_summary is None:
    log(f"stream ended without terminal summary event (duration={elapsed:.1f}s)")
summary = {
    "conversation_id": (final_summary or {}).get("conversation_id") or conversation_id,
    "status": (final_summary.get("status") or "completed") if final_summary is not None else "incomplete",
    "duration_seconds": (final_summary or {}).get("duration_seconds", elapsed),
    "num_turns": (final_summary or {}).get("num_turns"),
    "usage": (final_summary or {}).get("usage"),
    "stage": stage,
    "issue": issue,
    "lane": lane,
    "attempt": attempt,
}
sys.stdout.write(json.dumps(summary, indent=2) + "\n")
sys.stdout.flush()
'
PIPE_STATUSES=("${PIPESTATUS[@]}")
set -e

exit "${PIPE_STATUSES[0]}"
