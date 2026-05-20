"""Nexiuum Scheduling Engine — FastAPI entry point.

Phase 1.5 — Gray Space + Nexiuum capacity & lead-time forecasting.

Current state: scaffolding only. The pure-core placement function, async worker,
polling sweep, webhook handlers, and SSE broadcaster are not yet implemented.
This module provides the FastAPI app and a /health endpoint so the container
can be deployed and probed before real logic lands.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI(
    title="Nexiuum Scheduling Engine",
    version="0.1.0",
    description="Phase 1.5 capacity & lead-time forecasting. Scaffolding stage.",
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {
        "status": "ok",
        "version": app.version,
        "phase": "scaffolding",
    }


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "nexiuum-scheduling-engine",
        "version": app.version,
        "see": "/docs for OpenAPI, /health for liveness",
    }
