# shed

**Goal:** Shedding idle compute, excess spend, and carbon intensity — a `.NET 10` cost and carbon auditing and recommendations tool built inside a policy-enforced, metered AI software factory.

We will get onto this once the base software factory is set up...

---

## Enforcement Base & Governance

Repository rules and control-plane protections ensure automated Builder agents cannot weaken security, tests, or infrastructure:

- **Agent Contract:** [`AGENTS.md`](AGENTS.md) defines Clean Architecture boundaries (`src/Domain`, `src/Application`, `src/Infrastructure`, `src/Api`, `src/Worker`, `src/AppHost`, `tests/*`), secret hygiene, and pre-PR verification requirements.
- **Static Policy Enforcement:** [`.semgrep.yml`](.semgrep.yml) blocks missing controller auth, raw SQL interpolation, `DbContext` in Minimal API endpoints, sync-over-async, empty `catch` blocks, hardcoded credentials, skipped tests, and tautological assertions.
- **Secret Scanning & Policy:** [`SECURITY.md`](SECURITY.md) documents our zero-secrets-on-disk policy; Gitleaks runs locally and in CI.
- **CI Workflows:**
  - [`.github/workflows/path-guard.yml`](.github/workflows/path-guard.yml) blocks unauthorized edits to protected control-plane files.
  - [`.github/workflows/security-checks.yml`](.github/workflows/security-checks.yml) runs `gitleaks`, `semgrep`, and `config-validation` on PRs, merge groups, and pushes to `main`.
  - [`.github/dependabot.yml`](.github/dependabot.yml) schedules weekly dependency updates for GitHub Actions and container configs.
- **Branch Protection & Code Ownership:** [`.github/CODEOWNERS`](.github/CODEOWNERS) and [`.github/rulesets/main-protection.json`](.github/rulesets/main-protection.json) protect `main` with PR review, code-owner approval, and required status checks.
- **Independent Critic Agent & Skill:** [`.agents/agents/critic.md`](.agents/agents/critic.md) and [`.agents/skills/critic-review/SKILL.md`](.agents/skills/critic-review/SKILL.md) define the read-only pre-PR reviewer.

---

## Local AI Observability & ROI Stack (`agy` → OTel Collector → Grafana Cloud)

By default, Shed ships AI telemetry directly from **Antigravity CLI (`agy`)** sessions (running on your Google AI Pro plan) via the protected [`.agents/hooks.json`](.agents/hooks.json) `Stop` hook → local **OpenTelemetry Collector (`127.0.0.1:4318`)** → **Grafana Cloud** (`service.namespace=software-factory`), with **zero Google AI Studio API billing required**.

All required secrets (`GRAFANA_OTLP_ENDPOINT` and `GRAFANA_OTLP_AUTH_HEADER` for default OTel-only mode; plus `LITELLM_*` and provider keys if using the optional `--with-litellm` profile) are documented in [`.devcontainer/devcontainer.json`](.devcontainer/devcontainer.json) and injected exclusively via GitHub Codespaces secrets / runtime environment variables (no `.env` files on disk).

Using agy with the Google AI Pro plan limits us as the agent can only run pro or flash as models, so we cannot have a different model family adversary critic agent.  This pushes us towards having to set up Google AI Studio to fully demo the software factory.

### Quick Start (Default: `agy` → OTel → Grafana Cloud)

```bash
# Start OpenTelemetry Collector only (no LiteLLM or AI Studio API key required)
./infra/factory-observability/up.sh

# Verify health, telemetry hook privacy/idempotency/fail-open, and Grafana Cloud OTLP export
./infra/factory-observability/smoke-test-agy.sh

# Optional: include a live headless `agy -p` turn in the smoke test
./infra/factory-observability/smoke-test-agy.sh --live

# Run a FinOps-tagged headless Builder or Critic task
./infra/factory-observability/run-agent.sh \
  --stage builder --issue 12 --lane feature \
  --prompt "Implement the emissions calculation handler..."
```

Imported [`infra/factory-observability/grafana-dashboard.json`](infra/factory-observability/grafana-dashboard.json) into Grafana Cloud to visualize [**Google AI Pro ROI**](https://vastmistletoe3011.grafana.net/goto/sdvlkn), estimated API-equivalent spend vs. prompt-cache savings, token breakdown by model, tool error rates, and Tempo traces.

### Optional API Gateway Mode (LiteLLM + PostgreSQL)

For unattended CI runners or multi-provider API-key metering, start the full stack with `--with-litellm`:

```bash
./infra/factory-observability/up.sh --with-litellm
./infra/factory-observability/smoke-test.sh
```

---

## Tagging Every Agent Call for FinOps Attribution

When an agent or CI script calls your LiteLLM endpoint (`http://localhost:4000/v1/chat/completions`), pass `metadata` in the request body so spend and latency break down by repo, stage, issue, and retry attempt in Grafana (`service.namespace=software-factory`):

```json
{
  "model": "factory-builder",
  "messages": [
    { "role": "user", "content": "Implement the emissions calculation handler..." }
  ],
  "metadata": {
    "repo": "steve-hawkins/shed",
    "stage": "builder",
    "issue": "12",
    "lane": "feature",
    "attempt": 1
  }
}
```

*Observability tip:* You can point `agy` at your local LiteLLM gateway (`--gateway` / proxy endpoint) if you want `agy`'s own Gemini turns metered in the same Grafana Cloud FinOps dashboard.

---

## How to Invoke & Verify the `critic` Agent & Skill in `agy`

The review checklist lives in the reusable skill [`.agents/skills/critic-review/SKILL.md`](.agents/skills/critic-review/SKILL.md) and is wrapped by the custom agent [`.agents/agents/critic.md`](.agents/agents/critic.md):

1. **Verify discovery:** Inside an interactive `agy` session in `steve-hawkins/shed`, run:
   - `/agents` to confirm `critic` appears (using `model: pro`).
   - `/skills` to confirm `critic-review` is available for interactive use (e.g., `/critic-review`).
2. **Run or delegate a review:**
   - **Interactive skill:** Run `/critic-review` inside your current `agy` session.
   - **Dedicated agent:** Run `agy --agent critic -p "Review the current branch diff"` or ask the orchestrator:
     > *"Delegate a review of the current branch diff to the `critic` subagent before opening the PR."*
     The `critic` agent starts in a clean context window, inspects `git diff` and surrounding files without modifying code, and returns a structured `PASS | FAIL` verdict.

### Known `agy` Limitation & Implications for the Full Factory Demo

- **Frontmatter model restriction (`agy 1.5.14`):** Antigravity CLI custom agent YAML frontmatter only accepts tier aliases (`pro` or `flash`) in the `model:` field. Specifying a concrete model ID (such as `claude-sonnet-5-5-medium` or `claude-sonnet-5-5`) causes `agy` to silently skip loading the agent file. For now, [`.agents/agents/critic.md`](.agents/agents/critic.md) is set to `model: pro`.
- **Why this pushes us toward Google AI Studio (and `--with-litellm`) for the full factory demo:** While `agy` on Google AI Pro is great for interactive development, true automated **cross-family verification** (e.g., an independent non-Gemini Critic auditing a Gemini Builder's PR automatically), deterministic model pinning per factory stage, and unattended CI execution require explicit API-level model routing. Setting up **Google AI Studio** (alongside Anthropic/provider API keys through the optional `--with-litellm` gateway) unlocks exact per-stage model routing, full cross-family Builder/Critic separation, and end-to-end automated factory demos.
- **Workaround for headless CLI runs:** Until `agy` supports explicit model IDs in agent frontmatter, you can override the model at invocation time via `./infra/factory-observability/run-agent.sh --stage critic --agent critic --model <model-id> --prompt "..."`.
