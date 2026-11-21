# Walkthrough: `agy` → OpenTelemetry Collector → Grafana Cloud

Implemented and verified end-to-end AI observability and ROI telemetry directly from **Antigravity CLI (`agy`)** sessions (running on **Google AI Pro**) to **Grafana Cloud**, with zero Google AI Studio API billing required.

---

## Changes Made

### 1. OTel-First Local Observability Stack
- **[otel-collector-config.yml](file:///workspaces/shed/infra/factory-observability/otel-collector-config.yml)** — Enabled the `health_check` extension (`0.0.0.0:13133`) and added the `deltatocumulative` processor on the `metrics` pipeline so per-turn delta metrics convert cleanly into Grafana Cloud Mimir monotonic cumulative counters.
- **[docker-compose.yml](file:///workspaces/shed/infra/factory-observability/docker-compose.yml)** — Exposed `127.0.0.1:13133:13133` on `otel-collector` and placed `litellm-db` and `litellm` under `profiles: ["litellm"]`, making `otel-collector` the lightweight default service.
- **[up.sh](file:///workspaces/shed/infra/factory-observability/up.sh)** — Defaults to `--otel-only` (requiring only `GRAFANA_OTLP_ENDPOINT` and `GRAFANA_OTLP_AUTH_HEADER` and waiting on `http://127.0.0.1:13133/`), with `--with-litellm` available as an opt-in profile.

### 2. Protected `agy` Telemetry Hook & ROI Pricing
- **[hooks.json](file:///workspaces/shed/.agents/hooks.json)** — Registered the `Stop` lifecycle hook inside the protected `.agents/` control plane so every interactive session, subagent, `/swarm` worker, and headless `agy -p` run emits telemetry automatically.
- **[model-pricing.json](file:///workspaces/shed/.agents/hooks/model-pricing.json)** — Added reference API-equivalent GBP per-1M-token rates across `agy` models plus the `£18.99/mo` Google AI Pro subscription baseline for ROI calculations.
- **[otel-telemetry.py](file:///workspaces/shed/.agents/hooks/otel-telemetry.py)** — Stdlib-only Python OTLP/HTTP JSON emitter that:
  - Incrementally parses new steps from `transcript.jsonl` using per-conversation watermarks in `/tmp/shed-agy-otel-state/` (zero double-counting, never touches git working tree).
  - Enforces strict privacy (never reads or exports `content`, `thinking`, `lastUserInput`, or `tool_calls[].args`).
  - Emits OpenTelemetry GenAI traces (`agy.turn` + child `gen_ai.chat <model>` spans) and FinOps/ROI metrics (`shed.ai.tokens`, `shed.ai.estimated_cost_gbp`, `shed.ai.cache_savings_gbp`, `shed.ai.turns`, `shed.ai.tool_calls`, `shed.ai.turn_duration_seconds`).
  - Fails open (`{}` on stdout, exit `0`, 1.5s HTTP timeout) so an offline collector never blocks `agy`.

### 3. Headless Factory Runner, Smoke Test & Starter Dashboard
- **[run-agent.sh](file:///workspaces/shed/infra/factory-observability/run-agent.sh)** — Wrapper around `agy -p --output-format json` that injects `SHED_STAGE`, `SHED_ISSUE`, `SHED_LANE`, and `SHED_ATTEMPT` for FinOps-tagged headless factory runs.
- **[smoke-test-agy.sh](file:///workspaces/shed/infra/factory-observability/smoke-test-agy.sh)** — Verifies `otel-collector` health, synthetic fixture emission, watermark deduplication, offline fail-open behavior, optional live `agy -p` hook execution (`--live`), and Grafana Cloud OTLP export.
- **[grafana-dashboard.json](file:///workspaces/shed/infra/factory-observability/grafana-dashboard.json)** — Importable Grafana Cloud dashboard (*Shed — AI Software Factory Observability & ROI*) with ROI multiplier (`÷ £18.99`), API-equivalent spend (GBP), cache savings (GBP), cache hit ratio, token breakdown, tool error rate, turn duration, and Tempo trace explorer panels.

### 4. CI & Documentation
- **[security-checks.yml](file:///workspaces/shed/.github/workflows/security-checks.yml)** — Extended `config-validation` to validate `.agents/` & `infra/factory-observability/` JSON files, compile-check `otel-telemetry.py`, syntax-check `run-agent.sh` & `smoke-test-agy.sh`, and validate `docker compose config` for both default OTel-only and `--profile litellm` modes.
- **[.gitignore](file:///workspaces/shed/.gitignore)** — Added `__pycache__/` and `*.py[cod]`.
- **[AGENTS.md](file:///workspaces/shed/AGENTS.md)** & **[README.md](file:///workspaces/shed/README.md)** — Updated with the primary `agy → OTel → Grafana Cloud` workflow and optional `--with-litellm` gateway mode.

---

## Validation Results

| Check | Command | Result |
| :--- | :--- | :--- |
| Secret scanning (history & working tree) | `gitleaks detect --source . --redact --verbose` & `--no-git` | **PASS** (0 leaks) |
| Static policy enforcement | `semgrep --config .semgrep.yml --validate` & `--error .` | **PASS** (11 rules valid, 0 findings) |
| JSON, YAML, Python & Bash validation | `jq empty`, `ruamel.yaml`, `py_compile`, `bash -n` | **PASS** (all files valid) |
| Compose config (OTel-only without LiteLLM env vars & LiteLLM profile) | `docker compose config --quiet` (both profiles) | **PASS** |
| OTel-only startup (with `LITELLM_*` & `GEMINI_API_KEY` unset) | `./infra/factory-observability/up.sh` | **PASS** (`otel-collector` healthy at `:13133`) |
| Synthetic fixture + dedupe + fail-open + Grafana OTLP export | `./infra/factory-observability/smoke-test-agy.sh` | **PASS** (`3 spans`, `6 metrics / 9 data points`, 0 export errors) |
| Live headless `agy -p` turn triggering `.agents/hooks.json` | `./infra/factory-observability/smoke-test-agy.sh --live` | **PASS** (`SUCCESS`, `3.22s`, `7,370` cached tokens; collector exported `2 spans` & `5 metrics / 7 data points` with 0 errors) |

---

## Next Steps in Grafana Cloud

1. Open **Grafana Cloud → Dashboards → Import** and upload [grafana-dashboard.json](file:///workspaces/shed/infra/factory-observability/grafana-dashboard.json), selecting your Prometheus/Mimir and Tempo data sources.
2. Confirm the smoke-test traces appear in **Tempo** (`{resource.service.namespace="software-factory"}`) and the ROI/token panels populate from the live `agy` turn.
