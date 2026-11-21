# Security Policy

## Secret Handling Policy

- **Zero Secrets on Disk:** This repository must never contain plaintext secrets, API keys, credentials, or `.env` / `.env.example` files.
- **Runtime Injection Only:** All required secrets are documented in `.devcontainer/devcontainer.json` and supplied exclusively through GitHub Codespaces secrets or runtime environment variables.
- **Defense-in-Depth:** `.gitignore` ignores `.env`, `.env.*`, and `*.env`, and CI enforces secret detection on every pull request, merge group, and push to `main` via Gitleaks (`.github/workflows/security-checks.yml`).

## Headless Builder Agent Egress Firewall

Headless Builder agent devcontainers (`.devcontainer/builder-agent/devcontainer.json`) execute `.devcontainer/automation/init-firewall.sh` on startup to enforce a default-drop outbound firewall (`iptables` + `ipset`):

- Allows only loopback, DNS, host bridge communication, official GitHub IP ranges, NuGet / .NET SDK feeds, approved LLM provider APIs, and an optional configured LiteLLM proxy host.
- Drops all other outbound network traffic and verifies both denied (`example.com`) and permitted (`api.github.com`) egress immediately on container start.

## Control-Plane Governance

Files governing CI, security rules, agent contracts, devcontainers, and AI observability infrastructure are treated as a protected control plane:

- Protected paths: `.github/`, `.devcontainer/`, `.agents/`, `infra/factory-observability/`, `.semgrep.yml`, `stryker-config.json`, `AGENTS.md`, and `docs/FACTORY_ARCHITECTURE.md`.
- Enforced by `.github/CODEOWNERS`, `.github/workflows/path-guard.yml`, `.github/workflows/security-checks.yml`, `.github/rulesets/main-protection.json`, and the read-only `critic` subagent (`.agents/agents/critic.md`).
- Direct pushes to `main` are blocked; automated agents cannot approve or bypass control-plane protections.

## Reporting a Vulnerability

If you discover a security vulnerability in `steve-hawkins/shed`:

1. **Do not** open a public GitHub issue with exploit details or exposed credentials.
2. Use GitHub **Security Advisories** (*Security → Advisories → Report a vulnerability*) on `steve-hawkins/shed` to disclose the finding privately to `@steve-hawkins`.
3. Include reproduction steps, affected files or workflows, and potential impact. If credentials may have been exposed, rotate them immediately before reporting.
