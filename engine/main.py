"""Nexiuum Scheduling Engine — FastAPI entry point.

Phase 1.5 — Gray Space + Nexiuum capacity & lead-time forecasting.

Current state: scaffolding only. The pure-core placement function, async worker,
polling sweep, webhook handlers, and SSE broadcaster are not yet implemented.
This module provides the FastAPI app and a /health endpoint so the container
can be deployed and probed before real logic lands.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from engine.io.worker import start_worker, stop_worker
from engine.routes.commit import router as commit_router
from engine.routes.simulate import router as simulate_router
from engine.routes.webhook import router as webhook_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the async worker on app startup; cancel on shutdown."""
    await start_worker()
    try:
        yield
    finally:
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
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {
        "status": "ok",
        "version": app.version,
    }


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "nexiuum-scheduling-engine",
        "version": app.version,
        "see": "/docs for OpenAPI, /health for liveness, /simulate for CTP, "
               "/commit to schedule + write, /webhook/monday for Monday webhooks",
    }
