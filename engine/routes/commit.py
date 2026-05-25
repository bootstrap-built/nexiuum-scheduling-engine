"""POST /commit — like /simulate, but actually writes the slot(s) to Monday.

The AM commit path: after /simulate returns an acceptable date, the AM
clicks "Save quote" which calls /commit. Engine schedules the order via
the worker (serialized) and returns the created Monday slot IDs.

Unlike /simulate, /commit requires a real `job_reference_id` (a Blend
Records item ID) — the simulation sentinel is rejected.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from engine.config import get_settings
from engine.core.scheduler import (
    DanglingRecipeError,
    InactiveRecipeError,
    UnroutableStageError,
)
from engine.io.worker import submit_event
from engine.models import ScheduleNewOrder
from engine.routes.simulate import SimulateError

router = APIRouter(tags=["commit"])


# Recipe Key is a kebab-case identifier written by upstream Monday automation
# (locked decision #2). Constraining the charset here catches accidental
# whitespace, smart quotes, or unicode that the Monday columns would happily
# accept but downstream scheduler joins would silently miss.
_RECIPE_KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class CommitRequest(BaseModel):
    job_reference_id: str = Field(
        ...,
        pattern=r"^\d+$",
        min_length=1,
        max_length=20,
        description=(
            "Real Blend Records item ID — must be a numeric string. "
            "Rejects the '__simulate__' sentinel and any non-digit text "
            "before the worker ever sees it."
        ),
    )
    recipe_key: str = Field(
        ...,
        pattern=_RECIPE_KEY_PATTERN,
        min_length=1,
        max_length=64,
        description="Lower-case kebab-case identifier (e.g. 'tablet-press-standard')",
    )
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
    # Defence-in-depth: the numeric-string pattern on the field already
    # rejects "__simulate__", but make the intent explicit and a future
    # schema relaxation safe.
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

    timeout = get_settings().commit_timeout_seconds
    try:
        result = await asyncio.wait_for(submit_event(order), timeout=timeout)
    except asyncio.TimeoutError:
        # Worker queue is wedged or Monday is too slow. Don't leave the
        # HTTP connection hanging; let the caller retry. The submitted
        # event remains in the queue and will run when the worker drains.
        raise HTTPException(
            status_code=504,
            detail=f"engine worker did not respond within {timeout}s",
        )
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
