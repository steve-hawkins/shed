# shed

**Goal:** Shedding idle compute, excess spend, and carbon intensity — a `.NET 10` cost and carbon auditing and recommendations tool built inside a policy-enforced, metered AI software factory.

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
- **Independent Critic Subagent:** [`.agents/agents/critic.md`](.agents/agents/critic.md) defines the read-only cross-family reviewer.

---

## Local AI Observability Stack (LiteLLM + OpenTelemetry)

All required secrets (`LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `GRAFANA_OTLP_ENDPOINT`, `GRAFANA_OTLP_AUTH_HEADER`, and `GEMINI_API_KEY`) are documented in [`.devcontainer/devcontainer.json`](.devcontainer/devcontainer.json) and injected exclusively via GitHub Codespaces secrets / runtime environment variables (no `.env` files on disk).

### Quick Start

```bash
# Start PostgreSQL, OpenTelemetry Collector, and LiteLLM Proxy
./infra/factory-observability/up.sh

# Run end-to-end readiness, model alias, FinOps-tagged completion, and OTel export verification
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

## How to Invoke & Verify the `critic` Subagent in `agy`

1. **Verify discovery:** Inside an interactive `agy` session in `steve-hawkins/shed`, run `/agents` to confirm `critic` appears in the loaded project subagents list with model `claude-sonnet-5-5-medium`.
2. **Delegate a review:** After a Builder turn completes a feature or bugfix, instruct the orchestrator:
   > *"Delegate a review of the current branch diff to the `critic` subagent before opening the PR."*
   Antigravity will spawn `critic` in a clean context window using `claude-sonnet-5-5-medium`, run `git diff` and file reads without write permissions, and return the `PASS | FAIL` verdict to your main session.
