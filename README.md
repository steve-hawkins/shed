# shed
goal: shedding idle compute, excess spend, and carbon intensity.

## Tagging Every Agent Call for FinOps Attribution

When an agent or CI script calls your LiteLLM endpoint (http://localhost:4000/v1/chat/completions), pass metadata in the request body or custom headers so you can break down spend by repo, stage, issue, and retry attempt in Grafana:

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

Observability tip: You can even point agy at your local LiteLLM gateway (--gateway / proxy endpoint) if you want agy's own Gemini turns metered in the exact same Grafana Cloud FinOps dashboard.

---

### How to Invoke & Verify It in `agy`

1. **Verify discovery:** Inside an interactive `agy` session in `steve-hawkins/shed`, run `/agents` to confirm `critic` appears in the loaded project subagents list with model `claude-sonnet-5-5-medium`.
2. **Delegate a review:** After a Builder turn completes a feature or bugfix, instruct the orchestrator:
   > *"Delegate a review of the current branch diff to the `critic` subagent before opening the PR."*
   Antigravity will spawn `critic` in a clean context window using `claude-sonnet-5-5-medium`, run `git diff` and file reads without write permissions, and return the `PASS | FAIL` verdict to your main session.
