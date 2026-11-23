# AGENTS.md — Shed AI Software Factory Contract

This document defines the mandatory rules, architectural boundaries, security invariants, and verification gates for all AI agents operating in `steve-hawkins/shed`.

---

## 1. Project Scope & Architecture

**Shed** is a `.NET 10` cloud cost and carbon auditing and recommendations tool (*shedding idle compute, excess spend, and carbon intensity*).

The solution follows strict **Clean Architecture** boundaries:

- `src/Domain/` — Pure domain entities, value objects, emissions/cost math, and domain invariants. **Zero** dependencies on EF Core, ASP.NET Core, RabbitMQ/MassTransit/Wolverine, or external infrastructure SDKs.
- `src/Application/` — Use-case handlers, commands/queries, validation, and port interfaces. Depends only on `src/Domain`.
- `src/Infrastructure/` — EF Core `DbContext` / PostgreSQL persistence, message broker adapters, telemetry collectors, and external provider integrations.
- `src/Api/` — ASP.NET Core Minimal API / controller boundary. Must dispatch commands and queries through `src/Application` handlers — **never** inject `DbContext` directly into endpoints.
- `src/Worker/` — Background message consumers and scheduled auditing/recommendation workers.
- `src/AppHost/` — .NET Aspire orchestration and service discovery wiring.
- `tests/Architecture.Tests/` — Automated dependency-direction and layer-boundary verification.
- `tests/Domain.UnitTests/` — Fast, deterministic unit tests for domain logic, edge cases, and financial/carbon precision.
- `tests/Integration.Tests/` — End-to-end integration tests covering `API -> RabbitMQ -> Worker -> PostgreSQL` flows.

---

## 2. Protected Control Plane (Do Not Modify)

Automated Builder agents are **strictly prohibited** from creating, modifying, or deleting any file in the repository control plane unless explicitly authorized by the human repository owner:

- `.github/` (workflows, `CODEOWNERS`, `dependabot.yml`, `rulesets/`)
- `.devcontainer/` (container definitions, egress firewall scripts)
- `.agents/` (subagent definitions such as `critic.md`)
- `.semgrep.yml`
- `stryker-config.json`
- `AGENTS.md`
- `docs/FACTORY_ARCHITECTURE.md`
- `infra/factory-observability/`

Unauthorized edits to these paths are blocked automatically by `.github/workflows/path-guard.yml`, `.github/CODEOWNERS`, and the read-only `critic` subagent.

---

## 3. Secret Hygiene

- **Never** write secrets, API keys, tokens, passwords, or `.env` files (including `.env.example`) to the workspace filesystem.
- Consume secrets exclusively from runtime environment variables injected via GitHub Codespaces secrets (documented in `.devcontainer/devcontainer.json`) or .NET Aspire / environment configuration.
- Never log raw secrets, authorization headers, connection strings with credentials, or sensitive request payloads in application logs or OpenTelemetry spans.

---

## 4. Security, Reliability & Test Invariants

All changes must comply with `.semgrep.yml` and the following engineering invariants:

1. **Explicit Authorization:** Every ASP.NET Core controller must explicitly declare `[Authorize]` or `[AllowAnonymous]`. Do not add `[AllowAnonymous]` in `src/Api/**` without explicit human review.
2. **Parameterized Data Access:** Never pass interpolated strings to `ExecuteSqlRaw`, `ExecuteSqlRawAsync`, or `FromSqlRaw`. Use `ExecuteSqlInterpolated` or parameterized queries.
3. **Strict API Layering:** Minimal API endpoints in `src/Api/**` must dispatch through `src/Application` handlers rather than injecting `*DbContext`.
4. **Safe Serialization & Transport:** Never use `BinaryFormatter`, `JavaScriptSerializer`, or `NetDataContractSerializer` (use `System.Text.Json`). Never bypass TLS certificate validation (`ServerCertificateCustomValidationCallback`).
5. **No Hardcoded Credentials:** Never embed connection strings containing `Password=` / `Pwd=` in `src/**`.
6. **Async & Cancellation Safety:** Never use sync-over-async (`.Result`, `.Wait()`, `.GetAwaiter().GetResult()`) in `src/**`. Propagate `CancellationToken` end-to-end.
7. **No Swallowed Exceptions:** Never write empty `catch` blocks in `src/**`; log with structured context and rethrow or handle explicitly.
8. **Domain Precision & Concurrency:** Guard against division-by-zero, verify unit/decimal precision (`kWh`, `gCO2eq`, currency rounding), check inclusive/exclusive time-window boundaries (`<` vs `<=`), and enforce idempotency and optimistic concurrency (`xmin`/row version) on writes.
9. **Test Integrity (Anti-Gaming):** Never disable or skip tests (`[Fact(Skip = ...)]`, `[Theory(Skip = ...)]`) and never write tautological assertions (`Assert.True(true)`, `Assert.False(false)`, `Assert.Equal(x, x)`). Tests must verify meaningful persisted state, domain outcomes, and failure/boundary paths.
10. **File Naming Convention:** Always use hyphens (`-`, kebab-case) instead of underscores (`_`) in new file and directory names (e.g., `otel-telemetry.py`, `model-pricing.json`). Only use underscores if using `-` fails in testing or is strictly required by language/framework tooling (e.g., an `import`ed Python module or C# `PascalCase.cs` type names).
11. **LTS-Only .NET Policy:** Shed exclusively targets **.NET LTS (even-numbered) releases** (currently `.NET 10` / `net10.0`). Never target or upgrade Docker base images, `global.json`, or `<TargetFramework>` / `<TargetFrameworks>` to STS (odd-numbered, e.g., `.NET 11` / `net11.0`) versions. NuGet package dependency versions (direct or transitive) are not restricted by this rule as long as they support the LTS target framework.

---

## 5. AI Observability & FinOps Attribution

### Primary Path (`agy` → OTel Collector → Grafana Cloud)

Every `agy` session, subagent, `/swarm` worker, and headless `agy -p` run in this repository automatically invokes `.agents/hooks.json` (`Stop` hook → `.agents/hooks/otel-telemetry.py`) to export OpenTelemetry traces and metrics (tokens, prompt-cache savings, tool counts, and estimated API-equivalent GBP cost) to `http://127.0.0.1:4318` → Grafana Cloud (`service.namespace=software-factory`).

- When running headless factory tasks via `./infra/factory-observability/run-agent.sh`, pass `--stage`, `--issue`, `--lane`, and `--attempt` so spans and metrics carry accurate FinOps attribution.
- Never log or export raw prompts, thinking text, tool arguments, secrets, or authorization headers.

### Optional API Gateway Path (LiteLLM `--with-litellm`)

When routing direct API calls through the optional LiteLLM AI Gateway (`http://localhost:4000/v1/chat/completions`), include FinOps attribution metadata on every request:

```json
{
  "metadata": {
    "repo": "steve-hawkins/shed",
    "stage": "builder",
    "issue": "<issue-number>",
    "lane": "<feature|bugfix|chore>",
    "attempt": 1
  }
}
```

Preserve the `service.namespace=software-factory` OpenTelemetry resource attribute across all factory services and telemetry pipelines.

---

## 6. Pre-PR Verification Protocol

Before opening or updating a pull request, every agent must run and pass all local enforcement checks:

1. **Static Security & Architecture Rules:**
   ```bash
   semgrep --config .semgrep.yml --error .
   ```
2. **Secret Scanning:**
   ```bash
   gitleaks detect --source . --redact
   ```
3. **Build & Tests** *(once the `.NET 10` solution exists)*:
   ```bash
   dotnet test
   ```
4. **Independent Read-Only Critic Audit:**
   Delegate a diff review to the `critic` subagent (`.agents/agents/critic.md`) and resolve all blocking findings before opening or merging the PR.
