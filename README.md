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
