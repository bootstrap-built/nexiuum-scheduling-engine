"""CTP `/simulate` endpoint — Capable-to-Promise lookup.

AM enters product mix + quantity → engine returns projected ship date with a
20% pad (per Jason's spec) and the binding-constraint machine.

This is the customer-facing read-only path:
- No writeback to Monday — purely a simulation
- Reads a fresh Snapshot at the start of each request
- Bypasses the IO shell's async worker queue (no contention with live writes)
- Runs `plan_for_new_order` against a hypothetical order

Error cases map to specific HTTP 4xx responses so the calling form can show
useful messages instead of "internal server error."
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from engine.config import get_settings
from engine.core.scheduler import (
    DanglingRecipeError,
    InactiveRecipeError,
    UnroutableStageError,
    plan_for_new_order,
)
from engine.core.timezone import now_local
from engine.io.snapshot import read_snapshot
from engine.models import PackagingSlice, ScheduleNewOrder, Snapshot

router = APIRouter(tags=["simulate"])


# ─────────────────────────────────────────────────────────────────────────
# FastAPI dependencies — overridable in tests via app.dependency_overrides
# ─────────────────────────────────────────────────────────────────────────


async def get_current_snapshot() -> Snapshot:
    """Production dependency: fresh Monday read on every request."""
    return await read_snapshot()


def get_current_time() -> datetime:
    """Production dependency: real wall-clock time in factory TZ."""
    return now_local(get_settings().factory_tz)

# Per Jason: padding factor for AM-facing quote dates. 20% pad on duration.
DEFAULT_PAD_FACTOR = 0.20


# ─────────────────────────────────────────────────────────────────────────
# Request / response schemas
# ─────────────────────────────────────────────────────────────────────────


class PackagingSliceIn(BaseModel):
    """One entry in the order's packaging breakdown — same shape as the
    /commit route's PackagingSliceIn but kept separate to avoid coupling
    the two endpoints' schemas.
    """

    machine_class: Literal["Sachet", "Blister", "Clamshell", "Bottle"]
    quantity: int = Field(..., gt=0)
    items_per_container: int = Field(1, ge=1)
    config_notes: str = Field("", max_length=120)


class SimulateRequest(BaseModel):
    """Hypothetical order for CTP lookup."""

    recipe_key: str = Field(..., description="e.g., 'tablet-press-standard'")
    recipe_version: int = Field(1, ge=1)
    quantity: int = Field(..., gt=0, description="Tabs / caps / units")
    dual_sided: bool = False
    active_mg: float | None = Field(None, ge=0, description="For active_mg > 80 force-route rule")
    requested_ship_by: datetime | None = Field(
        None, description="Customer-requested ship date (informational; not enforced in v1)"
    )
    pad_factor: float = Field(
        DEFAULT_PAD_FACTOR,
        ge=0.0,
        le=1.0,
        description="Pad as a fraction of duration. Default 0.20 per Jason.",
    )
    packaging_breakdown: list[PackagingSliceIn] = Field(
        default_factory=list,
        description=(
            "Same shape as /commit. Empty = recipe-only stages; non-empty "
            "= synthetic packaging stages appended after the recipe DAG."
        ),
    )


class StagePlacementOut(BaseModel):
    stage_id: str
    machine_id: str
    machine_name: str
    planned_start: datetime
    planned_end: datetime
    duration_hours: float


class SimulateResponse(BaseModel):
    feasible: Literal[True] = True
    projected_start: datetime = Field(..., description="Earliest start across all stages")
    projected_end: datetime = Field(..., description="Latest end across all stages = ship-ready date")
    padded_end: datetime = Field(
        ...,
        description="projected_end + pad_factor × (projected_end − projected_start). What AM quotes.",
    )
    binding_machine_id: str = Field(..., description="Machine determining projected_end")
    binding_machine_name: str
    stages: list[StagePlacementOut]
    notes: list[str]


class SimulateError(BaseModel):
    feasible: Literal[False] = False
    error_kind: Literal["DanglingRecipe", "InactiveRecipe", "UnroutableStage"]
    message: str
    # Populated when applicable:
    recipe_key: str | None = None
    recipe_version: int | None = None
    recipe_status: str | None = None  # for InactiveRecipe
    stage_id: str | None = None  # for UnroutableStage
    stage_machine_class: str | None = None
    unroutable_reason: str | None = None


# ─────────────────────────────────────────────────────────────────────────
# Handler (pure-function shape — caller provides Snapshot + now)
# ─────────────────────────────────────────────────────────────────────────


# Sentinel id for the hypothetical order. Engine doesn't write anything,
# so this never lands on a real Monday item.
SIMULATE_JOB_ID = "__simulate__"


def simulate_handler(req: SimulateRequest, snapshot: Snapshot, *, now: datetime) -> SimulateResponse:
    """Pure simulate function.

    Tests pass a mock Snapshot and now. Production route adapter reads the
    real Snapshot from Monday.
    """
    breakdown = tuple(
        PackagingSlice(
            machine_class=s.machine_class,  # type: ignore[arg-type]
            quantity=s.quantity,
            items_per_container=s.items_per_container,
            config_notes=s.config_notes,
        )
        for s in req.packaging_breakdown
    )
    order = ScheduleNewOrder(
        job_reference_id=SIMULATE_JOB_ID,
        recipe_key=req.recipe_key,
        recipe_version=req.recipe_version,
        quantity=req.quantity,
        dual_sided=req.dual_sided,
        active_mg=req.active_mg,
        requested_ship_by=req.requested_ship_by,
        packaging_breakdown=breakdown,
    )

    plan = plan_for_new_order(snapshot, order, now=now)

    # Map machine IDs to names for response.
    machine_name = {m.id: m.name for m in snapshot.machines}

    stages_out: list[StagePlacementOut] = []
    for w in plan.slot_writes:
        if w.planned_start is None or w.planned_end is None or w.machine_id is None:
            raise RuntimeError("Plan is incomplete — pure core invariant violated")
        duration = (w.planned_end - w.planned_start).total_seconds() / 3600.0
        stages_out.append(
            StagePlacementOut(
                stage_id=w.stage_id or "",
                machine_id=w.machine_id,
                machine_name=machine_name.get(w.machine_id, w.machine_id),
                planned_start=w.planned_start,
                planned_end=w.planned_end,
                duration_hours=duration,
            )
        )

    if not stages_out:
        # plan_for_new_order should have raised; defensive check.
        raise RuntimeError("Plan contains no slot writes")

    projected_start = min(s.planned_start for s in stages_out)
    projected_end = max(s.planned_end for s in stages_out)

    # Binding machine = the stage whose end equals projected_end.
    binding = next(s for s in stages_out if s.planned_end == projected_end)

    # 20% pad applied to total duration.
    total_duration = projected_end - projected_start
    padded_end = projected_end + total_duration * req.pad_factor

    return SimulateResponse(
        projected_start=projected_start,
        projected_end=projected_end,
        padded_end=padded_end,
        binding_machine_id=binding.machine_id,
        binding_machine_name=binding.machine_name,
        stages=stages_out,
        notes=list(plan.notes),
    )


# ─────────────────────────────────────────────────────────────────────────
# FastAPI route adapter — reads Snapshot, calls handler, maps errors
# ─────────────────────────────────────────────────────────────────────────


@router.post("/simulate", response_model=SimulateResponse, responses={400: {"model": SimulateError}})
async def simulate_route(
    req: SimulateRequest,
    snapshot: Snapshot = Depends(get_current_snapshot),
    now: datetime = Depends(get_current_time),
) -> SimulateResponse:
    """POST /simulate — CTP lookup against the current Monday state.

    Reads a fresh Snapshot on every request via the `get_current_snapshot`
    dependency. Does NOT enqueue work or write anything to Monday. Safe to
    call as often as needed without affecting the live schedule.

    Tests override the snapshot and now dependencies via
    `app.dependency_overrides` to avoid touching Monday.
    """
    try:
        return simulate_handler(req, snapshot, now=now)
    except DanglingRecipeError as e:
        raise HTTPException(
            status_code=400,
            detail=SimulateError(
                error_kind="DanglingRecipe",
                message=str(e),
                recipe_key=e.recipe_key,
                recipe_version=e.recipe_version,
            ).model_dump(),
        )
    except InactiveRecipeError as e:
        raise HTTPException(
            status_code=400,
            detail=SimulateError(
                error_kind="InactiveRecipe",
                message=str(e),
                recipe_key=e.recipe_key,
                recipe_version=e.recipe_version,
                recipe_status=e.status,
            ).model_dump(),
        )
    except UnroutableStageError as e:
        raise HTTPException(
            status_code=400,
            detail=SimulateError(
                error_kind="UnroutableStage",
                message=str(e),
                stage_id=e.stage_id,
                stage_machine_class=e.machine_class,
                unroutable_reason=e.reason,
            ).model_dump(),
        )
