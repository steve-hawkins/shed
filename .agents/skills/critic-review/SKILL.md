---
name: critic-review
description: >-
  Read-only .NET 10 code review and pre-PR audit checklist verifying domain
  invariants, Clean Architecture boundaries, security hygiene, control-plane
  integrity, and anti-gaming test quality. Use when auditing a branch diff,
  reviewing changes before opening or merging a PR, or running an independent
  critic review.
---

# Shed Independent .NET 10 Critic Review

Perform a strictly **read-only** principal-engineer and security audit of proposed changes in `steve-hawkins/shed`. Never modify files, edit tests, or commit code during this review. Your sole deliverable is a structured, evidence-backed pass/fail audit report.

## 1. Inspection Procedure

1. Run `git status` and `git diff` (or `git diff main...HEAD` if commits exist on the feature branch) to inspect the complete set of changes.
2. Read `AGENTS.md` and `.semgrep.yml` to verify the diff obeys all repository-wide rules and architectural constraints.
3. Inspect the surrounding files in `src/` and `tests/` for every modified component so you understand callers, domain invariants, and edge cases.

## 2. Audit Checklist

### A. Domain Invariants & .NET 10 Correctness
- **LTS-Only .NET Policy:** Immediately **FAIL** if the diff introduces STS (odd-numbered, e.g., `.NET 11` / `net11.0` / `11.x`) SDKs, Docker base images, `<TargetFramework>` values, or `Microsoft.*` / `System.*` / `Aspire.*` framework packages. Shed exclusively targets `.NET` LTS (even-numbered) releases (currently `.NET 10` / `net10.0`).
- **Clean Architecture Boundaries:** Verify `src/Domain` has zero dependencies on EF Core, RabbitMQ/MassTransit/Wolverine, or ASP.NET Core, and `src/Api` endpoints dispatch through `src/Application` rather than querying `DbContext` directly.
- **State & Idempotency:** Verify write/settlement/ingestion operations enforce idempotency keys, optimistic concurrency (`xmin`/row version), and valid state-machine transitions.
- **Async & Cancellation Safety:** Flag any blocking calls (`.Result`, `.Wait()`, `.GetAwaiter().GetResult()`), missing `CancellationToken` propagation, or swallowed exceptions in empty `catch` blocks.
- **Financial & Telemetry Math:** Check decimal/unit precision (e.g., kWh, gCO2eq, currency rounding), division-by-zero guards, and time-window boundary conditions (`<` vs `<=`).

### B. Security & Control-Plane Integrity
- **Control-Plane Protection:** Immediately **FAIL** if the diff modifies `.github/`, `.devcontainer/`, `.agents/`, `.semgrep.yml`, `stryker-config.json`, `CODEOWNERS`, or `AGENTS.md` unless explicitly authorized in the task prompt.
- **Input & Query Safety:** Flag interpolated raw SQL, missing request validation, unauthenticated endpoints (`[AllowAnonymous]` added without justification), TLS validation bypasses, or hardcoded secrets/connection strings.
- **Observability Hygiene:** Verify OpenTelemetry spans/logs do not leak raw secrets, authorization headers, or sensitive payloads, and that `service.namespace` tags remain intact.

### C. Test Quality & Anti-Gaming Audit
- **Meaningful Assertions:** Flag tautological assertions (`Assert.True(true)`, `Assert.Equal(x, x)`), disabled/skipped tests (`Skip = ...`), or assertions that only check HTTP `200` without verifying persisted database state or published broker messages.
- **Edge & Failure Coverage:** Verify new or modified domain logic includes boundary tests, negative/error-path tests, and integration tests covering the `API -> RabbitMQ -> Worker -> PostgreSQL` flow where applicable.
- **Diff Minimalism & File Naming:** Flag unrelated refactoring, dead code, changes outside the stated issue scope, or newly introduced filenames using underscores (`_`) instead of hyphens (`-`) without a documented testing/tooling reason.

## 3. Output Format

Return your audit using this exact structure:

```markdown
## Critic Verdict: [PASS | FAIL]

### Summary
- **Scope Assessed:** <branch / files changed>
- **Blocking Issues:** <count>
- **Non-Blocking Suggestions:** <count>

### Blocking Findings (Must Fix Before Merge)
1. **[Category — File:Line]** Description of the invariant, security, or test defect, why it fails, and what the Builder agent must change. *(Omit section or write "None" if PASS.)*

### Non-Blocking Observations
- **[Category — File:Line]** Optional improvement or edge-case note.
```
