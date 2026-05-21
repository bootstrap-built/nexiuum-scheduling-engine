"""POST /commit — like /simulate, but actually writes the slot(s) to Monday.

The AM commit path: after /simulate returns an acceptable date, the AM
clicks "Save quote" which calls /commit. Engine schedules the order via
the worker (serialized) and returns the created Monday slot IDs.

Unlike /simulate, /commit requires a real `job_reference_id` (a Blend
Records item ID) — the simulation sentinel is rejected.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from engine.core.scheduler import (
    DanglingRecipeError,
    InactiveRecipeError,
    UnroutableStageError,
)
from engine.io.worker import submit_event
from engine.models import ScheduleNewOrder
from engine.routes.simulate import SimulateError

router = APIRouter(tags=["commit"])


class CommitRequest(BaseModel):
    job_reference_id: str = Field(
        ..., min_length=1, description="Real Blend Records item ID; cannot be the simulate sentinel"
    )
    recipe_key: str
    recipe_version: int = Field(1, ge=1)
    quantity: int = Field(..., gt=0)
    dual_sided: bool = False
    active_mg: float | None = Field(None, ge=0)
    requested_ship_by: datetime | None = None


class CommitResponse(BaseModel):
    feasible: Literal[True] = True
    job_reference_id: str
    created_slot_ids: list[str]
    reflow_hash: str
    notes: list[str] = []


@router.post("/commit", response_model=CommitResponse, responses={400: {"model": SimulateError}})
async def commit_route(req: CommitRequest) -> CommitResponse:
    """Schedule and write a real order through the worker queue."""
    if req.job_reference_id == "__simulate__":
        raise HTTPException(status_code=400, detail="job_reference_id must be a real item id")

    order = ScheduleNewOrder(
        job_reference_id=req.job_reference_id,
        recipe_key=req.recipe_key,
        recipe_version=req.recipe_version,
        quantity=req.quantity,
        dual_sided=req.dual_sided,
        active_mg=req.active_mg,
        requested_ship_by=req.requested_ship_by,
    )

    try:
        result = await submit_event(order)
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

    if result is None or not result.created_slot_ids:
        raise HTTPException(status_code=500, detail="engine returned no slot writes")

    return CommitResponse(
        job_reference_id=req.job_reference_id,
        created_slot_ids=result.created_slot_ids,
        reflow_hash=result.reflow_hash,
    )
