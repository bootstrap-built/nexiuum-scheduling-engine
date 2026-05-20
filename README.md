# Nexiuum Scheduling Engine

Phase 1.5 — Capacity & Lead-Time Forecasting engine for Gray Space + Nexiuum.

**Status:** Scaffolding. Engine code starts after the SSE-iframe CSP spike completes.

## Architecture

See `~/projects/clients/nexiuum/workflows/gray_space_scheduling/gray-space-scheduling-plan.md` for the canonical build plan (v3).

TL;DR: pure-core placement function + async IO shell. Single worker, serialized write queue. Source-board webhooks bridge user-originated status flips into Schedule writes. CTP `/simulate` reads bypass the worker.

## Monday boards

| Board | ID | Purpose |
|---|---|---|
| Capacity Engine | 18413803163 | Machine metadata (capacity, hours, routing flags) |
| Process Recipe | 18414126054 | Versioned stage DAGs |
| Schedule | 18413802995 | Flat slot items, one per machine-job pairing |
| Blend Records (source) | 18404836849 | Upstream order board; `Job Reference` target |

## Repo layout

```
engine/
  main.py         FastAPI entry — webhooks, /simulate, /sse, /health
  config.py       env loading
  models.py       dataclasses (snapshot, event, plan)
  core/           pure functions (placement, routing, recipes, timezone)
  io/             IO shell (monday client, worker, polling, SSE broadcaster)
  routes/         FastAPI route modules
spike/
  sse-test.html   1.5B0 Monday Apps Framework CSP probe
tests/
.github/workflows/
```

## Hosting

Target: Nexiuum EC2 (access TBD).
Interim: bb-infra-01 for testing if needed before Nexiuum EC2 is available.
Deploy via GitHub Actions (`.github/workflows/deploy.yml`).

## Environment

Copy `.env.example` to `.env` and populate. Secrets never committed.

## Spike: SSE-from-iframe CSP probe

See `spike/sse-test.html`. Phase 1.5B0 task — validates that Monday's Apps Framework allows board-view iframes to open external EventSource connections. If this fails, the v3 embedded-view architecture (engine → SSE → Marey renderer) needs to pivot before any engine code is written.

## Plan & background

- v3 build plan: `~/projects/clients/nexiuum/workflows/gray_space_scheduling/gray-space-scheduling-plan.md`
- Renderer prototype: `~/projects/clients/nexiuum/workflows/gray_space_scheduling/scheduling-flow-view.html`
- Codex adversarial review: `~/projects/clients/nexiuum/workflows/gray_space_scheduling/codex-review-v2.md`
- Notion: https://www.notion.so/364347ea284d81aba6afd5484479b9db
