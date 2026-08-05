# LLM Router — public demo

The hardest of the six to show, because the real thing has no user interface: it's a service
every other app points at, plus a tray icon. This page simulates the decision it makes.

**Live at** <https://tomchap.com/demos/llm-router/>

## What it shows

- A mock Windows taskbar with the tray icon. Click it for the send / hold / reject menu —
  the same three states the real tray app writes to the flag store.
- An animated routing diagram: requests leave the client apps, hit the router, and take the
  GPU lane or the cloud lane depending on the decision.
- A live `/history` table with the **reason** for every routing decision.
- A `/status` panel whose JSON matches the real endpoint's shape.
- The six routing rules as clickable cards — press one to force a request down that path.

## What's real

The routing rules, the tray states, the single-slot GPU limit and the failure modes:

| Condition | Result |
|---|---|
| `flag=send`, GPU online, slot free | GPU |
| `flag=send`, slot busy > 15 s | cloud — degrade, never queue |
| `flag=hold` | cloud (gaming mode) |
| `flag=reject` | cloud |
| model name begins `cloud`/`claude` | cloud (per-request override) |
| embeddings + GPU unreachable | 503 — no cloud equivalent |

## What's fake

The traffic. Requests, callers, timings and latencies are generated in the browser. Traffic
pauses when the tab is hidden.

## Publishing

Copied to the website by `D:\projects\tomchap-com\scripts\sync_demos.py`. Edit **here**, then:

```bash
python D:/projects/tomchap-com/scripts/sync_demos.py
```

Then deploy tomchap.com. Never edit the copy under `tomchap-com/public/demos/`.
