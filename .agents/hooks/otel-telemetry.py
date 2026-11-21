#!/usr/bin/env python3
"""Antigravity CLI (agy) Stop hook -> OpenTelemetry Collector (OTLP/HTTP JSON).

Reads the hook event JSON from stdin, incrementally parses new steps from
transcript.jsonl, computes token counts, cache savings, and API-equivalent GBP
cost from model-pricing.json, and emits OTLP traces + delta metrics to the local
OTel Collector (default http://127.0.0.1:4318).

Privacy invariant:
  Never reads or transmits `content`, `thinking`, `lastUserInput`, or
  `tool_calls[].args`. Only structured telemetry metadata is exported.

Reliability invariant:
  Always fails open: writes `{}` to stdout and exits 0 so an unreachable
  collector never disrupts an `agy` session.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PRICING_PATH = SCRIPT_DIR / "model-pricing.json"
DEFAULT_OTLP_ENDPOINT = "http://127.0.0.1:4318"
DEFAULT_STATE_DIR = "/tmp/shed-agy-otel-state"
HTTP_TIMEOUT_SECONDS = 1.5


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
            timeout=1.0,
            text=True,
        )
        return out.strip()
    except Exception:
        return ""


def resolve_finops_context(cwd: str) -> dict[str, str]:
    branch = run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"

    repo = os.environ.get("SHED_REPO", "").strip()
    if not repo:
        remote = run_git(cwd, "config", "--get", "remote.origin.url")
        m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
        repo = m.group(1) if m else "steve-hawkins/shed"

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


def load_last_step_index(state_path: Path) -> int:
    try:
        with state_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            return int(data.get("last_step_index", -1))
    except Exception:
        return -1


def save_last_step_index(state_path: Path, step_index: int) -> None:
    try:
        tmp = state_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({"last_step_index": step_index, "updated_at": int(time.time())}, fh)
        tmp.replace(state_path)
    except Exception:
        pass


def attr_str(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": str(value)}}


def attr_int(key: str, value: int) -> dict[str, Any]:
    return {"key": key, "value": {"intValue": str(int(value))}}


def attr_double(key: str, value: float) -> dict[str, Any]:
    return {"key": key, "value": {"doubleValue": float(value)}}


def make_id(seed: str, length: int) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:length]


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


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        emit_empty_and_exit()

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        emit_empty_and_exit()

    conversation_id = str(event.get("conversationId") or "unknown")
    transcript_path_str = str(event.get("transcriptPath") or "")
    fallback_model = str(event.get("modelName") or "unknown")
    termination_reason = str(event.get("terminationReason") or "UNKNOWN")
    execution_num = int(event.get("executionNum") or 0)
    cwd = str(event.get("cwd") or os.getcwd())

    if not transcript_path_str:
        emit_empty_and_exit()

    transcript_path = Path(transcript_path_str)
    if not transcript_path.is_file():
        emit_empty_and_exit()

    state_path = state_file_for(conversation_id)
    last_step_index = load_last_step_index(state_path)

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
                # Privacy invariant: retain only structured metadata, never prompts/content/args.
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
        emit_empty_and_exit()

    planner_steps = [s for s in new_steps if s["type"] == "PLANNER_RESPONSE"]
    if not planner_steps:
        if max_step_index > last_step_index:
            save_last_step_index(state_path, max_step_index)
        emit_empty_and_exit()

    now_ns = time.time_ns()
    pricing_doc = load_pricing()
    ctx = resolve_finops_context(cwd)
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT).strip() or DEFAULT_OTLP_ENDPOINT

    step_times = [parse_rfc3339_ns(s["created_at"], now_ns) for s in new_steps]
    turn_start_ns = min(step_times) if step_times else now_ns
    turn_end_ns = max(max(step_times), now_ns)
    if turn_end_ns <= turn_start_ns:
        turn_end_ns = turn_start_ns + 1_000_000  # 1ms floor
    turn_duration_s = (turn_end_ns - turn_start_ns) / 1_000_000_000.0

    trace_id = make_id(f"{conversation_id}:{last_step_index}:{max_step_index}:{execution_num}", 32)
    parent_span_id = make_id(f"{trace_id}:turn", 16)

    total_input = 0
    total_cache_read = 0
    total_output = 0
    total_cost_gbp = 0.0
    total_cache_savings_gbp = 0.0
    tool_counts_by_name: dict[str, int] = {}
    tool_result_ok = sum(1 for s in new_steps if s["type"] == "GENERIC" and s["status"] == "DONE")
    tool_result_err = sum(1 for s in new_steps if s["type"] == "GENERIC" and s["status"] == "ERROR")

    # Aggregate token/cost totals per model for metrics
    per_model: dict[str, dict[str, Any]] = {}
    child_spans: list[dict[str, Any]] = []

    for idx, step in enumerate(planner_steps):
        model = step["model"] or fallback_model
        rates = resolve_model_pricing(model, pricing_doc)
        inp = step["input_tokens"]
        cache_r = step["cache_read_tokens"]
        outp = step["output_tokens"]
        cost_gbp, savings_gbp = calc_cost_gbp(inp, cache_r, outp, rates)

        total_input += inp
        total_cache_read += cache_r
        total_output += outp
        total_cost_gbp += cost_gbp
        total_cache_savings_gbp += savings_gbp

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

        step_start_ns = parse_rfc3339_ns(step["created_at"], turn_start_ns)
        if idx + 1 < len(planner_steps):
            next_ns = parse_rfc3339_ns(planner_steps[idx + 1]["created_at"], turn_end_ns)
            step_end_ns = max(step_start_ns + 1_000_000, min(next_ns, turn_end_ns))
        else:
            step_end_ns = max(step_start_ns + 1_000_000, turn_end_ns)

        child_spans.append(
            {
                "traceId": trace_id,
                "spanId": make_id(f"{trace_id}:step:{step['step_index']}", 16),
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

    primary_model = planner_steps[-1]["model"] or fallback_model
    total_tool_calls = sum(tool_counts_by_name.values())

    parent_span = {
        "traceId": trace_id,
        "spanId": parent_span_id,
        "name": f"agy.turn ({ctx['stage']})",
        "kind": 1,  # SPAN_KIND_INTERNAL
        "startTimeUnixNano": str(turn_start_ns),
        "endTimeUnixNano": str(turn_end_ns),
        "attributes": [
            attr_str("gen_ai.request.model", primary_model),
            attr_int("gen_ai.usage.input_tokens", total_input),
            attr_int("gen_ai.usage.output_tokens", total_output),
            attr_int("gen_ai.usage.cache_read_input_tokens", total_cache_read),
            attr_str("shed.repo", ctx["repo"]),
            attr_str("shed.branch", ctx["branch"]),
            attr_str("shed.issue", ctx["issue"]),
            attr_str("shed.stage", ctx["stage"]),
            attr_str("shed.lane", ctx["lane"]),
            attr_str("shed.attempt", ctx["attempt"]),
            attr_str("shed.billing_mode", ctx["billing_mode"]),
            attr_str("shed.conversation_id", conversation_id),
            attr_str("shed.termination_reason", termination_reason),
            attr_int("shed.llm_calls.count", len(planner_steps)),
            attr_int("shed.tool_calls.count", total_tool_calls),
            attr_int("shed.tool_results.ok", tool_result_ok),
            attr_int("shed.tool_results.error", tool_result_err),
            attr_double("shed.cost.estimated_gbp", round(total_cost_gbp, 8)),
            attr_double("shed.cost.cache_savings_gbp", round(total_cache_savings_gbp, 8)),
            attr_double("shed.plan.monthly_gbp", float(pricing_doc.get("plan_monthly_gbp", 18.99))),
        ],
        "status": {"code": 2 if termination_reason == "ERROR" else 1},
    }

    resource_attrs = [
        attr_str("service.name", "agy-cli"),
        attr_str("service.namespace", "software-factory"),
        attr_str("shed.repo", ctx["repo"]),
        attr_str("shed.billing_mode", ctx["billing_mode"]),
    ]

    traces_payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeSpans": [
                    {
                        "scope": {"name": "shed.agy.telemetry", "version": "1.0.0"},
                        "spans": [parent_span, *child_spans],
                    }
                ],
            }
        ]
    }

    common_metric_attrs = [
        attr_str("repo", ctx["repo"]),
        attr_str("stage", ctx["stage"]),
        attr_str("lane", ctx["lane"]),
        attr_str("issue", ctx["issue"]),
        attr_str("billing_mode", ctx["billing_mode"]),
    ]

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
                        "startTimeUnixNano": str(turn_start_ns),
                        "timeUnixNano": str(turn_end_ns),
                        "asInt": str(int(count)),
                    }
                )
        if stats["cost_gbp"] > 0:
            cost_points.append(
                {
                    "attributes": model_attrs,
                    "startTimeUnixNano": str(turn_start_ns),
                    "timeUnixNano": str(turn_end_ns),
                    "asDouble": round(float(stats["cost_gbp"]), 8),
                }
            )
        if stats["cache_savings_gbp"] > 0:
            savings_points.append(
                {
                    "attributes": model_attrs,
                    "startTimeUnixNano": str(turn_start_ns),
                    "timeUnixNano": str(turn_end_ns),
                    "asDouble": round(float(stats["cache_savings_gbp"]), 8),
                }
            )

    tool_points: list[dict[str, Any]] = []
    for tname, count in tool_counts_by_name.items():
        tool_points.append(
            {
                "attributes": [*common_metric_attrs, attr_str("tool_name", tname), attr_str("status", "invoked")],
                "startTimeUnixNano": str(turn_start_ns),
                "timeUnixNano": str(turn_end_ns),
                "asInt": str(int(count)),
            }
        )
    if tool_result_err > 0:
        tool_points.append(
            {
                "attributes": [*common_metric_attrs, attr_str("tool_name", "any"), attr_str("status", "error")],
                "startTimeUnixNano": str(turn_start_ns),
                "timeUnixNano": str(turn_end_ns),
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

    metrics_payload = {
        "resourceMetrics": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeMetrics": [
                    {
                        "scope": {"name": "shed.agy.telemetry", "version": "1.0.0"},
                        "metrics": metrics_list,
                    }
                ],
            }
        ]
    }

    traces_ok = post_otlp(endpoint, "/v1/traces", traces_payload)
    metrics_ok = post_otlp(endpoint, "/v1/metrics", metrics_payload)

    if traces_ok or metrics_ok:
        save_last_step_index(state_path, max_step_index)

    emit_empty_and_exit()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        emit_empty_and_exit()
