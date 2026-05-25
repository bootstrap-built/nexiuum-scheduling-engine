"""Nexiuum Scheduling Engine — FastAPI entry point.

Phase 1.5 — Gray Space + Nexiuum capacity & lead-time forecasting.

Current state: scaffolding only. The pure-core placement function, async worker,
polling sweep, webhook handlers, and SSE broadcaster are not yet implemented.
This module provides the FastAPI app and a /health endpoint so the container
can be deployed and probed before real logic lands.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from engine.io import sweep as sweep_module
from engine.io import worker as worker_module
from engine.io.sweep import start_sweep, stop_sweep
from engine.io.worker import start_worker, stop_worker
from engine.routes.commit import router as commit_router
from engine.routes.simulate import router as simulate_router
from engine.routes.webhook import router as webhook_router

# Configure application logging early so engine.* loggers emit at the
# level configured in LOG_LEVEL (default INFO). Without this Python's root
# logger stays at WARNING and our log.info() messages disappear, leaving
# only uvicorn access logs in container output.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the async worker + polling sweep on startup; cancel on shutdown.

    Order: worker before sweep (sweep enqueues into the worker — start it
    second, stop it first so the worker is still draining when the last
    sweep batch lands).
    """
    await start_worker()
    await start_sweep()
    try:
        yield
    finally:
        await stop_sweep()
        await stop_worker()


app = FastAPI(
    title="Nexiuum Scheduling Engine",
    version="0.1.0",
    description="Phase 1.5 capacity & lead-time forecasting.",
    lifespan=lifespan,
)

app.include_router(simulate_router)
app.include_router(commit_router)
app.include_router(webhook_router)


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness + per-component health.

    `status` is "ok" iff both the worker and sweep tasks are alive.
    Per-component blocks include:
      - alive: task running and not done
      - last_error: most recent exception text (None if last run succeeded)
      - queue_depth (worker only): pending submissions on the queue

    Used by deploy smoke tests and (eventually) external monitoring.
    """
    worker_alive = worker_module.is_worker_alive()
    sweep_alive = sweep_module.is_sweep_alive()
    overall = "ok" if (worker_alive and sweep_alive) else "degraded"
    return {
        "status": overall,
        "version": app.version,
        "worker": {
            "alive": worker_alive,
            "queue_depth": worker_module.queue_depth(),
            "last_error": worker_module.last_error(),
        },
        "sweep": {
            "alive": sweep_alive,
            "last_error": sweep_module.last_error(),
        },
    }


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "nexiuum-scheduling-engine",
        "version": app.version,
        "see": "/docs for OpenAPI, /health for liveness, /simulate for CTP, "
               "/commit to schedule + write, /webhook/monday for Monday webhooks",
    }
