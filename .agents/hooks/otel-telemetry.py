#!/usr/bin/env python3
"""Antigravity CLI (agy) lifecycle hooks -> OpenTelemetry Collector (OTLP/HTTP JSON).

Handles `SessionStart`, `PreInvocation`, `PostToolUse`, `PostInvocation`, and `Stop`
hook events from stdin, incrementally parses new steps from transcript.jsonl,
computes token counts, cache savings, and API-equivalent GBP cost from
model-pricing.json, and streams OTLP traces, delta metrics, active-agent gauges,
and structured logs to the local OTel Collector (default http://127.0.0.1:4318).

Privacy invariant:
  Never reads or transmits `content`, `thinking`, `lastUserInput`,
  `tool_calls[].args`, `toolCall.args`, or raw `error` strings. Only structured
  telemetry metadata is exported.

Reliability invariant:
  Always fails open: writes `{}` to stdout and exits 0 so an unreachable
  collector never disrupts an `agy` session.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PRICING_PATH = SCRIPT_DIR / "model-pricing.json"
DEFAULT_OTLP_ENDPOINT = "http://127.0.0.1:4318"
DEFAULT_STATE_DIR = "/tmp/shed-agy-otel-state"
HTTP_TIMEOUT_SECONDS = 0.5
GIT_CACHE_TTL_SECONDS = 30.0

SEVERITY_INFO = 9
SEVERITY_ERROR = 17


def emit_empty_and_exit() -> None:
    sys.stdout.write("{}\n")
    sys.stdout.flush()
    sys.exit(0)


def parse_rfc3339_ns(value: str | None, fallback_ns: int) -> int:
    if not value:
        return fallback_ns
    try:
        iso = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = dt.datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp() * 1_000_000_000)
    except (ValueError, TypeError):
        return fallback_ns


def load_pricing() -> dict[str, Any]:
    try:
        with PRICING_PATH.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {
            "plan_name": "Google AI Pro",
            "plan_monthly_gbp": 18.99,
            "currency": "GBP",
            "models": {
                "default": {
                    "system": "google_gemini",
                    "input_per_1m": 1.19,
                    "cache_read_per_1m": 0.119,
                    "output_per_1m": 5.93,
                }
            },
        }


def resolve_model_pricing(model: str, pricing_doc: dict[str, Any]) -> dict[str, Any]:
    models = pricing_doc.get("models", {})
    lower = (model or "").lower()
    # Match longest prefix first so more specific entries win.
    for prefix in sorted(models.keys(), key=len, reverse=True):
        if prefix != "default" and lower.startswith(prefix.lower()):
            return models[prefix]
    return models.get(
        "default",
        {
            "system": "google_gemini",
            "input_per_1m": 1.19,
            "cache_read_per_1m": 0.119,
            "output_per_1m": 5.93,
        },
    )


def calc_cost_gbp(
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    rates: dict[str, Any],
) -> tuple[float, float]:
    input_rate = float(rates.get("input_per_1m", 1.19)) / 1_000_000.0
    cache_rate = float(rates.get("cache_read_per_1m", 0.119)) / 1_000_000.0
    output_rate = float(rates.get("output_per_1m", 5.93)) / 1_000_000.0

    est_cost = (
        max(0, input_tokens) * input_rate
        + max(0, cache_read_tokens) * cache_rate
        + max(0, output_tokens) * output_rate
    )
    cache_savings = max(0, cache_read_tokens) * max(0.0, input_rate - cache_rate)
    return round(est_cost, 8), round(cache_savings, 8)


def run_git(cwd: str, *args: str) -> str:
    try:
        out = subprocess.check_output(
            ["git", *args],
            cwd=cwd,
            stderr=subprocess.DEVNULL,
            timeout=0.5,
            text=True,
        )
        return out.strip()
    except Exception:
        return ""


def resolve_finops_context(cwd: str, state: dict[str, Any]) -> dict[str, str]:
    now_s = time.time()
    git_cache = state.get("git_cache")
    if (
        isinstance(git_cache, dict)
        and git_cache.get("cwd") == cwd
        and (now_s - float(git_cache.get("cached_at", 0.0))) < GIT_CACHE_TTL_SECONDS
    ):
        branch = str(git_cache.get("branch") or "unknown")
        git_repo = str(git_cache.get("repo") or "steve-hawkins/shed")
    else:
        branch = run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
        remote = run_git(cwd, "config", "--get", "remote.origin.url")
        m_repo = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
        git_repo = m_repo.group(1) if m_repo else "steve-hawkins/shed"
        state["git_cache"] = {
            "cwd": cwd,
            "branch": branch,
            "repo": git_repo,
            "cached_at": now_s,
        }

    repo = os.environ.get("SHED_REPO", "").strip() or git_repo

    issue = os.environ.get("SHED_ISSUE", "").strip()
    if not issue:
        m = re.search(r"(?:^|[/_-])(?:issue[/_-]?|#)?(\d+)(?:[/_-]|$)", branch)
        issue = m.group(1) if m else "unassigned"

    lane = os.environ.get("SHED_LANE", "").strip()
    if not lane:
        b = branch.lower()
        if b.startswith(("feat/", "feature/")):
            lane = "feature"
        elif b.startswith(("fix/", "bugfix/", "hotfix/")):
            lane = "bugfix"
        elif b.startswith(("chore/", "ci/", "docs/", "refactor/")):
            lane = "chore"
        else:
            lane = "dev"

    stage = os.environ.get("SHED_STAGE", "").strip() or "interactive"
    attempt = os.environ.get("SHED_ATTEMPT", "").strip() or "1"
    billing_mode = os.environ.get("SHED_BILLING_MODE", "").strip() or "google_ai_pro"

    return {
        "repo": repo,
        "branch": branch,
        "issue": issue,
        "lane": lane,
        "stage": stage,
        "attempt": attempt,
        "billing_mode": billing_mode,
    }


def state_file_for(conversation_id: str) -> Path:
    state_dir = Path(os.environ.get("SHED_OTEL_STATE_DIR", DEFAULT_STATE_DIR))
    state_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:32]
    return state_dir / f"{digest}.json"


@contextlib.contextmanager
def state_lock(state_path: Path) -> Iterator[None]:
    """Acquire an exclusive advisory lock around state file read-modify-write."""
    lock_path = state_path.with_suffix(".lock")
    lock_fh = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        if lock_fh is not None:
            try:
                lock_fh.close()
            except OSError:
                pass
            lock_fh = None
    try:
        yield
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                lock_fh.close()
            except OSError:
                pass


def load_state(state_path: Path) -> dict[str, Any]:
    try:
        with state_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault("last_step_index", -1)
                data.setdefault("turn_seq", 0)
                data.setdefault("last_event_ns", 0)
                return data
    except Exception:
        pass
    return {"last_step_index": -1, "turn_seq": 0, "last_event_ns": 0}


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    tmp_name: str | None = None
    try:
        state["updated_at"] = int(time.time())
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=state_path.parent,
            prefix=f".{state_path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_fh:
            tmp_name = tmp_fh.name
            json.dump(state, tmp_fh)
            tmp_fh.flush()
        os.replace(tmp_name, state_path)
        tmp_name = None
    except Exception:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def attr_str(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": str(value)}}


def attr_int(key: str, value: int) -> dict[str, Any]:
    return {"key": key, "value": {"intValue": str(int(value))}}


def attr_double(key: str, value: float) -> dict[str, Any]:
    return {"key": key, "value": {"doubleValue": float(value)}}


def make_id(seed: str, length: int) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:length]


def ensure_turn(state: dict[str, Any], conversation_id: str, now_ns: int) -> dict[str, Any]:
    turn = state.get("turn")
    if isinstance(turn, dict) and turn.get("trace_id") and turn.get("parent_span_id"):
        return turn
    seq = int(state.get("turn_seq", 0)) + 1
    state["turn_seq"] = seq
    trace_id = make_id(f"{conversation_id}:turn:{seq}:{now_ns}", 32)
    parent_span_id = make_id(f"{trace_id}:root", 16)
    turn = {
        "seq": seq,
        "trace_id": trace_id,
        "parent_span_id": parent_span_id,
        "start_ns": now_ns,
        "has_activity": False,
        "primary_model": "",
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "cost_gbp": 0.0,
        "cache_savings_gbp": 0.0,
        "llm_calls": 0,
        "tool_calls": 0,
        "tool_ok": 0,
        "tool_err": 0,
    }
    state["turn"] = turn
    return turn


def post_otlp(endpoint: str, path: str, payload: dict[str, Any]) -> bool:
    url = endpoint.rstrip("/") + path
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def build_resource_attrs(ctx: dict[str, str]) -> list[dict[str, Any]]:
    return [
        attr_str("service.name", "agy-cli"),
        attr_str("service.namespace", "software-factory"),
        attr_str("shed.repo", ctx["repo"]),
        attr_str("shed.billing_mode", ctx["billing_mode"]),
    ]


def build_common_metric_attrs(ctx: dict[str, str]) -> list[dict[str, Any]]:
    return [
        attr_str("repo", ctx["repo"]),
        attr_str("stage", ctx["stage"]),
        attr_str("lane", ctx["lane"]),
        attr_str("issue", ctx["issue"]),
        attr_str("billing_mode", ctx["billing_mode"]),
    ]


def build_active_agents_metric(
    common_metric_attrs: list[dict[str, Any]],
    conversation_id: str,
    now_ns: int,
    active_value: int,
) -> dict[str, Any]:
    return {
        "name": "shed.ai.active_agents",
        "unit": "{agent}",
        "gauge": {
            "dataPoints": [
                {
                    "attributes": [
                        *common_metric_attrs,
                        attr_str("conversation_id", conversation_id),
                    ],
                    "timeUnixNano": str(now_ns),
                    "asInt": str(int(active_value)),
                }
            ]
        },
    }


def build_log_record(
    *,
    now_ns: int,
    ctx: dict[str, str],
    conversation_id: str,
    event_name: str,
    body: str,
    severity_number: int = SEVERITY_INFO,
    trace_id: str = "",
    span_id: str = "",
    extra_attrs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    severity_text = "ERROR" if severity_number >= SEVERITY_ERROR else "INFO"
    attrs = [
        attr_str("shed.event", event_name),
        attr_str("shed.repo", ctx["repo"]),
        attr_str("shed.branch", ctx["branch"]),
        attr_str("shed.stage", ctx["stage"]),
        attr_str("shed.lane", ctx["lane"]),
        attr_str("shed.issue", ctx["issue"]),
        attr_str("shed.attempt", ctx["attempt"]),
        attr_str("shed.conversation_id", conversation_id),
    ]
    if extra_attrs:
        attrs.extend(extra_attrs)

    record: dict[str, Any] = {
        "timeUnixNano": str(now_ns),
        "observedTimeUnixNano": str(now_ns),
        "severityNumber": severity_number,
        "severityText": severity_text,
        "body": {"stringValue": body},
        "attributes": attrs,
    }
    if trace_id:
        record["traceId"] = trace_id
    if span_id:
        record["spanId"] = span_id
    return record


def wrap_traces(resource_attrs: list[dict[str, Any]], spans: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeSpans": [
                    {
                        "scope": {"name": "shed.agy.telemetry", "version": "1.1.0"},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def wrap_metrics(resource_attrs: list[dict[str, Any]], metrics_list: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeMetrics": [
                    {
                        "scope": {"name": "shed.agy.telemetry", "version": "1.1.0"},
                        "metrics": metrics_list,
                    }
                ],
            }
        ]
    }


def wrap_logs(resource_attrs: list[dict[str, Any]], log_records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeLogs": [
                    {
                        "scope": {"name": "shed.agy.telemetry", "version": "1.1.0"},
                        "logRecords": log_records,
                    }
                ],
            }
        ]
    }


def infer_hook_event(event: dict[str, Any]) -> str:
    hook_name = str(event.get("hookEventName") or "").strip()
    if hook_name:
        return hook_name
    if "terminationReason" in event or "executionNum" in event:
        return "Stop"
    if "toolCall" in event:
        return "PostToolUse"
    if "invocationNum" in event:
        return "PreInvocation"
    return "Stop"


def read_new_transcript_steps(transcript_path_str: str, last_step_index: int) -> tuple[list[dict[str, Any]], int]:
    if not transcript_path_str:
        return [], last_step_index
    transcript_path = Path(transcript_path_str)
    if not transcript_path.is_file():
        return [], last_step_index

    new_steps: list[dict[str, Any]] = []
    max_step_index = last_step_index
    try:
        with transcript_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                step_idx = entry.get("step_index")
                if not isinstance(step_idx, int) or step_idx <= last_step_index:
                    continue
                if step_idx > max_step_index:
                    max_step_index = step_idx
                # Privacy invariant: retain only structured metadata, never prompts/content/args/errors.
                tool_names = []
                for tc in entry.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("name"):
                        tool_names.append(str(tc["name"]))
                new_steps.append(
                    {
                        "step_index": step_idx,
                        "type": str(entry.get("type") or ""),
                        "status": str(entry.get("status") or ""),
                        "created_at": entry.get("created_at"),
                        "model": str(entry.get("model") or ""),
                        "input_tokens": int(entry.get("input_tokens") or 0),
                        "cache_read_tokens": int(entry.get("cache_read_tokens") or 0),
                        "output_tokens": int(entry.get("output_tokens") or 0),
                        "tool_names": tool_names,
                    }
                )
    except OSError:
        return [], last_step_index

    return new_steps, max_step_index


def process_new_steps(
    *,
    new_steps: list[dict[str, Any]],
    turn: dict[str, Any],
    ctx: dict[str, str],
    conversation_id: str,
    fallback_model: str,
    pricing_doc: dict[str, Any],
    common_metric_attrs: list[dict[str, Any]],
    now_ns: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build incremental child spans, delta metrics, and structured logs for new steps."""
    planner_steps = [s for s in new_steps if s["type"] == "PLANNER_RESPONSE"]
    tool_result_ok = sum(1 for s in new_steps if s["type"] == "GENERIC" and s["status"] == "DONE")
    tool_result_err = sum(1 for s in new_steps if s["type"] == "GENERIC" and s["status"] == "ERROR")

    if not planner_steps and tool_result_ok == 0 and tool_result_err == 0:
        return [], [], []

    step_times = [parse_rfc3339_ns(s["created_at"], now_ns) for s in new_steps]
    batch_start_ns = min(step_times) if step_times else now_ns
    batch_end_ns = max(max(step_times), now_ns)
    if batch_end_ns <= batch_start_ns:
        batch_end_ns = batch_start_ns + 1_000_000

    # Anchor the turn's start_ns to the earliest step timestamp observed in this turn.
    if not turn["has_activity"] or batch_start_ns < int(turn.get("start_ns", now_ns)):
        turn["start_ns"] = batch_start_ns

    trace_id = turn["trace_id"]
    parent_span_id = turn["parent_span_id"]

    per_model: dict[str, dict[str, Any]] = {}
    tool_counts_by_name: dict[str, int] = {}
    child_spans: list[dict[str, Any]] = []
    log_records: list[dict[str, Any]] = []

    for idx, step in enumerate(planner_steps):
        model = step["model"] or fallback_model
        rates = resolve_model_pricing(model, pricing_doc)
        inp = step["input_tokens"]
        cache_r = step["cache_read_tokens"]
        outp = step["output_tokens"]
        cost_gbp, savings_gbp = calc_cost_gbp(inp, cache_r, outp, rates)

        turn["has_activity"] = True
        turn["primary_model"] = model
        turn["input_tokens"] += inp
        turn["cache_read_tokens"] += cache_r
        turn["output_tokens"] += outp
        turn["cost_gbp"] = round(float(turn["cost_gbp"]) + cost_gbp, 8)
        turn["cache_savings_gbp"] = round(float(turn["cache_savings_gbp"]) + savings_gbp, 8)
        turn["llm_calls"] += 1
        turn["tool_calls"] += len(step["tool_names"])

        bucket = per_model.setdefault(
            model,
            {
                "input": 0,
                "cache_read": 0,
                "output": 0,
                "cost_gbp": 0.0,
                "cache_savings_gbp": 0.0,
            },
        )
        bucket["input"] += inp
        bucket["cache_read"] += cache_r
        bucket["output"] += outp
        bucket["cost_gbp"] += cost_gbp
        bucket["cache_savings_gbp"] += savings_gbp

        for tname in step["tool_names"]:
            tool_counts_by_name[tname] = tool_counts_by_name.get(tname, 0) + 1

        step_start_ns = parse_rfc3339_ns(step["created_at"], batch_start_ns)
        if idx + 1 < len(planner_steps):
            next_ns = parse_rfc3339_ns(planner_steps[idx + 1]["created_at"], batch_end_ns)
            step_end_ns = max(step_start_ns + 1_000_000, min(next_ns, batch_end_ns))
        else:
            step_end_ns = max(step_start_ns + 1_000_000, batch_end_ns)

        child_span_id = make_id(f"{trace_id}:step:{step['step_index']}", 16)
        child_spans.append(
            {
                "traceId": trace_id,
                "spanId": child_span_id,
                "parentSpanId": parent_span_id,
                "name": f"gen_ai.chat {model}",
                "kind": 3,  # SPAN_KIND_CLIENT
                "startTimeUnixNano": str(step_start_ns),
                "endTimeUnixNano": str(step_end_ns),
                "attributes": [
                    attr_str("gen_ai.system", str(rates.get("system", "google_gemini"))),
                    attr_str("gen_ai.operation.name", "chat"),
                    attr_str("gen_ai.request.model", model),
                    attr_int("gen_ai.usage.input_tokens", inp),
                    attr_int("gen_ai.usage.output_tokens", outp),
                    attr_int("gen_ai.usage.cache_read_input_tokens", cache_r),
                    attr_double("shed.cost.estimated_gbp", cost_gbp),
                    attr_double("shed.cost.cache_savings_gbp", savings_gbp),
                    attr_int("shed.step_index", step["step_index"]),
                    attr_int("shed.tool_calls.count", len(step["tool_names"])),
                ],
                "status": {"code": 1 if step["status"] != "ERROR" else 2},
            }
        )

        tools_label = ",".join(step["tool_names"]) if step["tool_names"] else "none"
        log_records.append(
            build_log_record(
                now_ns=step_end_ns,
                ctx=ctx,
                conversation_id=conversation_id,
                event_name="llm.completed",
                severity_number=SEVERITY_ERROR if step["status"] == "ERROR" else SEVERITY_INFO,
                trace_id=trace_id,
                span_id=child_span_id,
                body=(
                    f"[{ctx['stage']} #{ctx['issue']}] llm.completed {model} "
                    f"(step={step['step_index']}, in={inp}, cache={cache_r}, out={outp}, "
                    f"tools={tools_label}, cost=£{cost_gbp:.4f})"
                ),
                extra_attrs=[
                    attr_str("gen_ai.request.model", model),
                    attr_int("shed.step_index", step["step_index"]),
                    attr_int("gen_ai.usage.input_tokens", inp),
                    attr_int("gen_ai.usage.cache_read_input_tokens", cache_r),
                    attr_int("gen_ai.usage.output_tokens", outp),
                    attr_int("shed.tool_calls.count", len(step["tool_names"])),
                    attr_str("shed.tool_calls.names", tools_label),
                    attr_double("shed.cost.estimated_gbp", cost_gbp),
                ],
            )
        )

    if tool_result_ok > 0 or tool_result_err > 0:
        turn["has_activity"] = True
    turn["tool_ok"] += tool_result_ok
    turn["tool_err"] += tool_result_err

    token_points: list[dict[str, Any]] = []
    cost_points: list[dict[str, Any]] = []
    savings_points: list[dict[str, Any]] = []

    for model, stats in per_model.items():
        model_attrs = [*common_metric_attrs, attr_str("model", model)]
        for token_type, count in (
            ("input", stats["input"]),
            ("cache_read", stats["cache_read"]),
            ("output", stats["output"]),
        ):
            if count > 0:
                token_points.append(
                    {
                        "attributes": [*model_attrs, attr_str("token_type", token_type)],
                        "startTimeUnixNano": str(batch_start_ns),
                        "timeUnixNano": str(batch_end_ns),
                        "asInt": str(int(count)),
                    }
                )
        if stats["cost_gbp"] > 0:
            cost_points.append(
                {
                    "attributes": model_attrs,
                    "startTimeUnixNano": str(batch_start_ns),
                    "timeUnixNano": str(batch_end_ns),
                    "asDouble": round(float(stats["cost_gbp"]), 8),
                }
            )
        if stats["cache_savings_gbp"] > 0:
            savings_points.append(
                {
                    "attributes": model_attrs,
                    "startTimeUnixNano": str(batch_start_ns),
                    "timeUnixNano": str(batch_end_ns),
                    "asDouble": round(float(stats["cache_savings_gbp"]), 8),
                }
            )

    tool_points: list[dict[str, Any]] = []
    for tname, count in tool_counts_by_name.items():
        tool_points.append(
            {
                "attributes": [*common_metric_attrs, attr_str("tool_name", tname), attr_str("status", "invoked")],
                "startTimeUnixNano": str(batch_start_ns),
                "timeUnixNano": str(batch_end_ns),
                "asInt": str(int(count)),
            }
        )
    if tool_result_err > 0:
        tool_points.append(
            {
                "attributes": [*common_metric_attrs, attr_str("tool_name", "any"), attr_str("status", "error")],
                "startTimeUnixNano": str(batch_start_ns),
                "timeUnixNano": str(batch_end_ns),
                "asInt": str(int(tool_result_err)),
            }
        )

    metrics_list: list[dict[str, Any]] = []
    if token_points:
        metrics_list.append(
            {
                "name": "shed.ai.tokens",
                "unit": "{token}",
                "sum": {
                    "dataPoints": token_points,
                    "aggregationTemporality": 1,  # DELTA -> converted by deltatocumulative
                    "isMonotonic": True,
                },
            }
        )
    if cost_points:
        metrics_list.append(
            {
                "name": "shed.ai.estimated_cost_gbp",
                "unit": "GBP",
                "sum": {
                    "dataPoints": cost_points,
                    "aggregationTemporality": 1,
                    "isMonotonic": True,
                },
            }
        )
    if savings_points:
        metrics_list.append(
            {
                "name": "shed.ai.cache_savings_gbp",
                "unit": "GBP",
                "sum": {
                    "dataPoints": savings_points,
                    "aggregationTemporality": 1,
                    "isMonotonic": True,
                },
            }
        )
    if tool_points:
        metrics_list.append(
            {
                "name": "shed.ai.tool_calls",
                "unit": "{call}",
                "sum": {
                    "dataPoints": tool_points,
                    "aggregationTemporality": 1,
                    "isMonotonic": True,
                },
            }
        )

    return child_spans, metrics_list, log_records


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        emit_empty_and_exit()

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        emit_empty_and_exit()

    hook_event = infer_hook_event(event)
    conversation_id = str(event.get("conversationId") or "unknown")
    transcript_path_str = str(event.get("transcriptPath") or "")
    fallback_model = str(event.get("modelName") or "unknown")
    cwd = str(event.get("cwd") or os.getcwd())

    now_ns = time.time_ns()
    state_path = state_file_for(conversation_id)
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT).strip() or DEFAULT_OTLP_ENDPOINT

    # ------------------------------------------------------------------
    # 1. SessionStart
    # ------------------------------------------------------------------
    if hook_event == "SessionStart":
        with state_lock(state_path):
            state = load_state(state_path)
            ctx = resolve_finops_context(cwd, state)
            turn = ensure_turn(state, conversation_id, now_ns)
            trace_id = turn["trace_id"]
            parent_span_id = turn["parent_span_id"]
            state["last_event_ns"] = max(int(state.get("last_event_ns") or 0), now_ns)
            save_state(state_path, state)

        resource_attrs = build_resource_attrs(ctx)
        common_metric_attrs = build_common_metric_attrs(ctx)
        metrics_list = [build_active_agents_metric(common_metric_attrs, conversation_id, now_ns, 1)]
        logs_list = [
            build_log_record(
                now_ns=now_ns,
                ctx=ctx,
                conversation_id=conversation_id,
                event_name="session.started",
                trace_id=trace_id,
                span_id=parent_span_id,
                body=(
                    f"[{ctx['stage']} #{ctx['issue']}] session.started "
                    f"(branch={ctx['branch']}, model={fallback_model})"
                ),
                extra_attrs=[attr_str("gen_ai.request.model", fallback_model)],
            )
        ]
        post_otlp(endpoint, "/v1/metrics", wrap_metrics(resource_attrs, metrics_list))
        post_otlp(endpoint, "/v1/logs", wrap_logs(resource_attrs, logs_list))
        emit_empty_and_exit()

    # ------------------------------------------------------------------
    # 2. PreInvocation
    # ------------------------------------------------------------------
    if hook_event == "PreInvocation":
        with state_lock(state_path):
            state = load_state(state_path)
            ctx = resolve_finops_context(cwd, state)
            turn = ensure_turn(state, conversation_id, now_ns)
            trace_id = turn["trace_id"]
            parent_span_id = turn["parent_span_id"]
            state["last_event_ns"] = max(int(state.get("last_event_ns") or 0), now_ns)
            save_state(state_path, state)

        resource_attrs = build_resource_attrs(ctx)
        common_metric_attrs = build_common_metric_attrs(ctx)
        inv_num = int(event.get("invocationNum") or 0)
        init_steps = int(event.get("initialNumSteps") or 0)
        metrics_list = [build_active_agents_metric(common_metric_attrs, conversation_id, now_ns, 1)]
        logs_list = [
            build_log_record(
                now_ns=now_ns,
                ctx=ctx,
                conversation_id=conversation_id,
                event_name="llm.invoking",
                trace_id=trace_id,
                span_id=parent_span_id,
                body=(
                    f"[{ctx['stage']} #{ctx['issue']}] llm.invoking "
                    f"(invocation={inv_num}, steps={init_steps}, model={fallback_model})"
                ),
                extra_attrs=[
                    attr_int("shed.invocation_num", inv_num),
                    attr_int("shed.steps.count", init_steps),
                    attr_str("gen_ai.request.model", fallback_model),
                ],
            )
        ]
        post_otlp(endpoint, "/v1/metrics", wrap_metrics(resource_attrs, metrics_list))
        post_otlp(endpoint, "/v1/logs", wrap_logs(resource_attrs, logs_list))
        emit_empty_and_exit()

    # ------------------------------------------------------------------
    # 3. PostToolUse
    # ------------------------------------------------------------------
    if hook_event == "PostToolUse":
        with state_lock(state_path):
            state = load_state(state_path)
            ctx = resolve_finops_context(cwd, state)
            turn = ensure_turn(state, conversation_id, now_ns)
            trace_id = turn["trace_id"]
            parent_span_id = turn["parent_span_id"]
            prev_ns = int(state.get("last_event_ns") or 0)
            state["last_event_ns"] = max(prev_ns, now_ns)
            save_state(state_path, state)

        resource_attrs = build_resource_attrs(ctx)
        common_metric_attrs = build_common_metric_attrs(ctx)
        tool_call = event.get("toolCall") if isinstance(event.get("toolCall"), dict) else {}
        # Privacy invariant: never read toolCall.args or raw error text.
        tool_name = str(tool_call.get("name") or "unknown")
        step_idx = int(event.get("stepIdx") or -1)
        has_error = bool(event.get("error"))
        status_str = "error" if has_error else "ok"

        elapsed_ms = max(0, int((now_ns - prev_ns) / 1_000_000)) if prev_ns > 0 else 0

        metrics_list = [build_active_agents_metric(common_metric_attrs, conversation_id, now_ns, 1)]
        logs_list = [
            build_log_record(
                now_ns=now_ns,
                ctx=ctx,
                conversation_id=conversation_id,
                event_name="tool.completed",
                severity_number=SEVERITY_ERROR if has_error else SEVERITY_INFO,
                trace_id=trace_id,
                span_id=parent_span_id,
                body=(
                    f"[{ctx['stage']} #{ctx['issue']}] tool.completed {tool_name} "
                    f"({status_str}, step={step_idx}, ~{elapsed_ms}ms)"
                ),
                extra_attrs=[
                    attr_str("shed.tool.name", tool_name),
                    attr_str("shed.tool.status", status_str),
                    attr_int("shed.step_index", step_idx),
                    attr_int("shed.elapsed_ms", elapsed_ms),
                ],
            )
        ]
        post_otlp(endpoint, "/v1/metrics", wrap_metrics(resource_attrs, metrics_list))
        post_otlp(endpoint, "/v1/logs", wrap_logs(resource_attrs, logs_list))
        emit_empty_and_exit()

    # ------------------------------------------------------------------
    # 4. PostInvocation (live incremental step export)
    # ------------------------------------------------------------------
    if hook_event == "PostInvocation":
        with state_lock(state_path):
            working_state = copy.deepcopy(load_state(state_path))
            ctx = resolve_finops_context(cwd, working_state)
            resource_attrs = build_resource_attrs(ctx)
            common_metric_attrs = build_common_metric_attrs(ctx)
            turn = ensure_turn(working_state, conversation_id, now_ns)
            last_step_index = int(working_state.get("last_step_index", -1))
            new_steps, max_step_index = read_new_transcript_steps(transcript_path_str, last_step_index)

            pricing_doc = load_pricing()
            child_spans, step_metrics, step_logs = process_new_steps(
                new_steps=new_steps,
                turn=turn,
                ctx=ctx,
                conversation_id=conversation_id,
                fallback_model=fallback_model,
                pricing_doc=pricing_doc,
                common_metric_attrs=common_metric_attrs,
                now_ns=now_ns,
            )

            has_step_traces = bool(child_spans)
            has_step_metrics = bool(step_metrics)
            has_step_telemetry = has_step_traces or has_step_metrics

            metrics_to_send = [
                *step_metrics,
                build_active_agents_metric(common_metric_attrs, conversation_id, now_ns, 1),
            ]

            traces_ok = post_otlp(endpoint, "/v1/traces", wrap_traces(resource_attrs, child_spans)) if has_step_traces else True
            metrics_ok = post_otlp(endpoint, "/v1/metrics", wrap_metrics(resource_attrs, metrics_to_send))
            if step_logs:
                post_otlp(endpoint, "/v1/logs", wrap_logs(resource_attrs, step_logs))

            traces_ready = (not has_step_traces) or traces_ok
            metrics_ready = (not has_step_metrics) or metrics_ok
            if (not has_step_telemetry) or (traces_ready and metrics_ready):
                working_state["last_step_index"] = max_step_index
                working_state["last_event_ns"] = max(int(working_state.get("last_event_ns") or 0), now_ns)
                save_state(state_path, working_state)
        emit_empty_and_exit()

    # ------------------------------------------------------------------
    # 5. Stop (flush remaining steps + emit root turn span & turn totals)
    # ------------------------------------------------------------------
    termination_reason = str(event.get("terminationReason") or "UNKNOWN")
    with state_lock(state_path):
        working_state = copy.deepcopy(load_state(state_path))
        ctx = resolve_finops_context(cwd, working_state)
        resource_attrs = build_resource_attrs(ctx)
        common_metric_attrs = build_common_metric_attrs(ctx)
        turn = ensure_turn(working_state, conversation_id, now_ns)
        last_step_index = int(working_state.get("last_step_index", -1))
        new_steps, max_step_index = read_new_transcript_steps(transcript_path_str, last_step_index)

        pricing_doc = load_pricing()
        child_spans, metrics_list, logs_list = process_new_steps(
            new_steps=new_steps,
            turn=turn,
            ctx=ctx,
            conversation_id=conversation_id,
            fallback_model=fallback_model,
            pricing_doc=pricing_doc,
            common_metric_attrs=common_metric_attrs,
            now_ns=now_ns,
        )

        spans_to_send: list[dict[str, Any]] = []
        if turn.get("has_activity"):
            turn_start_ns = int(turn.get("start_ns") or now_ns)
            turn_end_ns = max(now_ns, turn_start_ns + 1_000_000)
            turn_duration_s = (turn_end_ns - turn_start_ns) / 1_000_000_000.0
            primary_model = str(turn.get("primary_model") or fallback_model)

            parent_span = {
                "traceId": turn["trace_id"],
                "spanId": turn["parent_span_id"],
                "name": f"agy.turn ({ctx['stage']})",
                "kind": 1,  # SPAN_KIND_INTERNAL
                "startTimeUnixNano": str(turn_start_ns),
                "endTimeUnixNano": str(turn_end_ns),
                "attributes": [
                    attr_str("gen_ai.request.model", primary_model),
                    attr_int("gen_ai.usage.input_tokens", int(turn["input_tokens"])),
                    attr_int("gen_ai.usage.output_tokens", int(turn["output_tokens"])),
                    attr_int("gen_ai.usage.cache_read_input_tokens", int(turn["cache_read_tokens"])),
                    attr_str("shed.repo", ctx["repo"]),
                    attr_str("shed.branch", ctx["branch"]),
                    attr_str("shed.issue", ctx["issue"]),
                    attr_str("shed.stage", ctx["stage"]),
                    attr_str("shed.lane", ctx["lane"]),
                    attr_str("shed.attempt", ctx["attempt"]),
                    attr_str("shed.billing_mode", ctx["billing_mode"]),
                    attr_str("shed.conversation_id", conversation_id),
                    attr_str("shed.termination_reason", termination_reason),
                    attr_int("shed.llm_calls.count", int(turn["llm_calls"])),
                    attr_int("shed.tool_calls.count", int(turn["tool_calls"])),
                    attr_int("shed.tool_results.ok", int(turn["tool_ok"])),
                    attr_int("shed.tool_results.error", int(turn["tool_err"])),
                    attr_double("shed.cost.estimated_gbp", round(float(turn["cost_gbp"]), 8)),
                    attr_double("shed.cost.cache_savings_gbp", round(float(turn["cache_savings_gbp"]), 8)),
                    attr_double("shed.plan.monthly_gbp", float(pricing_doc.get("plan_monthly_gbp", 18.99))),
                ],
                "status": {"code": 2 if termination_reason == "ERROR" else 1},
            }
            spans_to_send = [parent_span, *child_spans]

            metrics_list.append(
                {
                    "name": "shed.ai.turns",
                    "unit": "{turn}",
                    "sum": {
                        "dataPoints": [
                            {
                                "attributes": [
                                    *common_metric_attrs,
                                    attr_str("model", primary_model),
                                    attr_str("termination_reason", termination_reason),
                                ],
                                "startTimeUnixNano": str(turn_start_ns),
                                "timeUnixNano": str(turn_end_ns),
                                "asInt": "1",
                            }
                        ],
                        "aggregationTemporality": 1,
                        "isMonotonic": True,
                    },
                }
            )
            metrics_list.append(
                {
                    "name": "shed.ai.turn_duration_seconds",
                    "unit": "s",
                    "gauge": {
                        "dataPoints": [
                            {
                                "attributes": [*common_metric_attrs, attr_str("model", primary_model)],
                                "timeUnixNano": str(turn_end_ns),
                                "asDouble": round(turn_duration_s, 4),
                            }
                        ]
                    },
                }
            )

            logs_list.append(
                build_log_record(
                    now_ns=turn_end_ns,
                    ctx=ctx,
                    conversation_id=conversation_id,
                    event_name="turn.completed",
                    severity_number=SEVERITY_ERROR if termination_reason == "ERROR" else SEVERITY_INFO,
                    trace_id=turn["trace_id"],
                    span_id=turn["parent_span_id"],
                    body=(
                        f"[{ctx['stage']} #{ctx['issue']}] turn.completed "
                        f"({termination_reason}, llm_calls={turn['llm_calls']}, tools={turn['tool_calls']}, "
                        f"err={turn['tool_err']}, cost=£{float(turn['cost_gbp']):.4f}, duration={turn_duration_s:.1f}s)"
                    ),
                    extra_attrs=[
                        attr_str("shed.termination_reason", termination_reason),
                        attr_str("gen_ai.request.model", primary_model),
                        attr_int("shed.llm_calls.count", int(turn["llm_calls"])),
                        attr_int("shed.tool_calls.count", int(turn["tool_calls"])),
                        attr_int("shed.tool_results.error", int(turn["tool_err"])),
                        attr_double("shed.cost.estimated_gbp", round(float(turn["cost_gbp"]), 8)),
                        attr_double("shed.turn_duration_seconds", round(turn_duration_s, 4)),
                    ],
                )
            )
        else:
            logs_list.append(
                build_log_record(
                    now_ns=now_ns,
                    ctx=ctx,
                    conversation_id=conversation_id,
                    event_name="turn.completed",
                    severity_number=SEVERITY_ERROR if termination_reason == "ERROR" else SEVERITY_INFO,
                    trace_id=turn["trace_id"],
                    span_id=turn["parent_span_id"],
                    body=f"[{ctx['stage']} #{ctx['issue']}] turn.completed ({termination_reason}, no_llm_steps)",
                    extra_attrs=[attr_str("shed.termination_reason", termination_reason)],
                )
            )

        has_turn_traces = bool(spans_to_send)
        has_turn_metrics = bool(metrics_list)
        has_turn_telemetry = has_turn_traces or has_turn_metrics

        metrics_to_send = [
            *metrics_list,
            build_active_agents_metric(common_metric_attrs, conversation_id, now_ns, 0),
        ]

        traces_ok = post_otlp(endpoint, "/v1/traces", wrap_traces(resource_attrs, spans_to_send)) if has_turn_traces else True
        metrics_ok = post_otlp(endpoint, "/v1/metrics", wrap_metrics(resource_attrs, metrics_to_send))
        if logs_list:
            post_otlp(endpoint, "/v1/logs", wrap_logs(resource_attrs, logs_list))

        traces_ready = (not has_turn_traces) or traces_ok
        metrics_ready = (not has_turn_metrics) or metrics_ok
        if (not has_turn_telemetry) or (traces_ready and metrics_ready):
            working_state["last_step_index"] = max_step_index
            working_state["last_event_ns"] = max(int(working_state.get("last_event_ns") or 0), now_ns)
            working_state.pop("turn", None)
            save_state(state_path, working_state)

    emit_empty_and_exit()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        emit_empty_and_exit()
