"""Pure-core tests for plan_for_actual_start / plan_for_actual_end."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from engine.core.actuals import plan_for_actual_end, plan_for_actual_start
from engine.models import (
    ActualEndReported,
    ActualStartReported,
    Machine,
    MachineStatus,
    Priority,
    Recipe,
    RecipeStage,
    RecipeStatus,
    Slot,
    SlotStatus,
    Snapshot,
)

TZ = ZoneInfo("America/Denver")
NOW = datetime(2026, 5, 22, 10, 0, 0, tzinfo=TZ)
ACTUAL_AT = datetime(2026, 5, 22, 10, 5, 0, tzinfo=TZ)


def _machine(id_: str = "M1") -> Machine:
    return Machine(
        id=id_, name="Gandalf", process_group="Pressing",
        status=MachineStatus.ONLINE,
        capacity_per_hour=40000, hours_per_day=16,
        working_window_start=6, working_window_end=22,
        changeover_minutes=30, dual_sided_only=False,
        max_job_size=None, force_route_condition=None,
        last_job_ended_at=None,
    )


def _recipe() -> Recipe:
    return Recipe(
        id="R1", name="r v1",
        recipe_key="tablet-press-standard", version=1,
        status=RecipeStatus.ACTIVE,
        stages=(RecipeStage(id="press", machine_class="Pressing", depends_on=()),),
    )


def _slot(
    *,
    id_: str = "S1",
    job_reference_id: str = "J1",
    stage_id: str = "press",
    status: SlotStatus = SlotStatus.QUEUED,
    actual_start: datetime | None = None,
    actual_end: datetime | None = None,
) -> Slot:
    return Slot(
        id=id_, name=f"slot {id_}",
        job_reference_id=job_reference_id, machine_id="M1",
        stage_id=stage_id, recipe_key="tablet-press-standard", recipe_version=1,
        quantity=100000,
        planned_start=NOW, planned_end=NOW,
        actual_start=actual_start, actual_end=actual_end,
        dependent_on_ids=(), status=status,
        manually_placed=False, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=None,
    )


def _snapshot(slots: tuple[Slot, ...]) -> Snapshot:
    return Snapshot(read_at=NOW, machines=(_machine(),), recipes=(_recipe(),), slots=slots)


# ─── plan_for_actual_start ───────────────────────────────────────────────


def test_actual_start_writes_when_slot_queued_and_unstarted():
    snap = _snapshot((_slot(),))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert len(plan.slot_writes) == 1
    w = plan.slot_writes[0]
    assert w.slot_id == "S1"
    assert w.actual_start == ACTUAL_AT
    assert w.status == SlotStatus.RUNNING


def test_actual_start_noop_when_no_slot_for_job():
    snap = _snapshot((_slot(job_reference_id="J-other"),))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert plan.slot_writes == ()
    assert any("no Schedule slot" in n for n in plan.notes)


def test_actual_start_noop_when_wrong_stage():
    snap = _snapshot((_slot(stage_id="package"),))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert plan.slot_writes == ()


def test_actual_start_idempotent_when_already_started():
    snap = _snapshot((_slot(actual_start=NOW, status=SlotStatus.RUNNING),))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert plan.slot_writes == ()
    assert any("already has actual_start" in n for n in plan.notes)


def test_actual_start_skips_done_slot():
    snap = _snapshot((_slot(status=SlotStatus.DONE, actual_start=NOW, actual_end=NOW),))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert plan.slot_writes == ()


def test_actual_start_writes_all_matching_queued_slots():
    """Multi-machine parallel pressing: every Queued press slot for the job updates."""
    snap = _snapshot((
        _slot(id_="S1", job_reference_id="J1"),
        _slot(id_="S2", job_reference_id="J1"),
        _slot(id_="S3", job_reference_id="J1", status=SlotStatus.RUNNING, actual_start=NOW),
        _slot(id_="S4", job_reference_id="J-other"),
    ))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    written_ids = {w.slot_id for w in plan.slot_writes}
    assert written_ids == {"S1", "S2"}  # S3 already Running, S4 different job


# ─── plan_for_actual_end ─────────────────────────────────────────────────


def test_actual_end_writes_when_slot_running_and_unended():
    snap = _snapshot((_slot(status=SlotStatus.RUNNING, actual_start=NOW),))
    event = ActualEndReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_end(snap, event)
    assert len(plan.slot_writes) == 1
    w = plan.slot_writes[0]
    assert w.actual_end == ACTUAL_AT
    assert w.status == SlotStatus.DONE


def test_actual_end_idempotent_when_already_done():
    snap = _snapshot((_slot(status=SlotStatus.DONE, actual_start=NOW, actual_end=NOW),))
    event = ActualEndReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_end(snap, event)
    assert plan.slot_writes == ()


# ─── plan_for_actual_end — baton-pass to dependent stages (Phase 2C) ─────


def _multistage_recipe() -> Recipe:
    """Recipe: press → blister and press → lotcode (parallel) → clamshell."""
    return Recipe(
        id="R-multi", name="tablet-blister-clamshell",
        recipe_key="tablet-blister-clamshell", version=1,
        status=RecipeStatus.ACTIVE,
        stages=(
            RecipeStage(id="press", machine_class="Pressing", depends_on=()),
            RecipeStage(id="blister", machine_class="Blister", depends_on=("press",)),
            RecipeStage(id="lotcode", machine_class="Lot Coder", depends_on=("press",)),
            RecipeStage(
                id="clamshell", machine_class="Clamshell",
                depends_on=("blister", "lotcode"),
            ),
        ),
    )


def _multistage_slot(
    *,
    id_: str,
    stage_id: str,
    job_reference_id: str = "J1",
    status: SlotStatus = SlotStatus.QUEUED,
    planned_start: datetime | None = None,
    planned_end: datetime | None = None,
    actual_start: datetime | None = None,
    actual_end: datetime | None = None,
    manually_placed: bool = False,
) -> Slot:
    return Slot(
        id=id_, name=f"slot {id_}",
        job_reference_id=job_reference_id, machine_id="M1",
        stage_id=stage_id,
        recipe_key="tablet-blister-clamshell", recipe_version=1,
        quantity=100000,
        planned_start=planned_start, planned_end=planned_end,
        actual_start=actual_start, actual_end=actual_end,
        dependent_on_ids=(), status=status,
        manually_placed=manually_placed, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=None,
    )


def _multistage_snapshot(slots: tuple[Slot, ...]) -> Snapshot:
    return Snapshot(
        read_at=NOW, machines=(_machine(),),
        recipes=(_multistage_recipe(),), slots=slots,
    )


def test_baton_pass_pushes_dependent_planned_start_forward():
    """When press's actual_end is set, a dependent blister slot whose
    planned_start was earlier gets pushed to event.actual_at + buffer."""
    actual_at = NOW.replace(hour=11)
    press = _multistage_slot(
        id_="press-1", stage_id="press",
        status=SlotStatus.RUNNING, actual_start=NOW,
        planned_start=NOW, planned_end=NOW.replace(hour=11),
    )
    blister = _multistage_slot(
        id_="blister-1", stage_id="blister",
        planned_start=NOW.replace(hour=11),
        planned_end=NOW.replace(hour=14),
    )
    snap = _multistage_snapshot((press, blister))
    event = ActualEndReported(
        job_reference_id="J1", stage_id="press", actual_at=actual_at,
    )

    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    by_slot = {w.slot_id: w for w in plan.slot_writes}

    assert by_slot["press-1"].status == SlotStatus.DONE
    assert by_slot["press-1"].actual_end == actual_at

    expected_start = actual_at.replace(hour=11, minute=30)
    assert by_slot["blister-1"].planned_start == expected_start
    # Duration preserved (3hr): planned_end pushed to 14:30
    assert by_slot["blister-1"].planned_end == actual_at.replace(hour=14, minute=30)


def test_baton_pass_skips_dependent_already_past_handoff():
    """If the dependent slot was already planned to start at/after the
    handoff cutoff, the baton-pass leaves it alone (no needless write)."""
    actual_at = NOW.replace(hour=11)
    press = _multistage_slot(
        id_="press-1", stage_id="press",
        status=SlotStatus.RUNNING, actual_start=NOW,
        planned_end=NOW.replace(hour=11),
    )
    blister = _multistage_slot(
        id_="blister-1", stage_id="blister",
        planned_start=NOW.replace(hour=13),
        planned_end=NOW.replace(hour=16),
    )
    snap = _multistage_snapshot((press, blister))
    event = ActualEndReported(
        job_reference_id="J1", stage_id="press", actual_at=actual_at,
    )

    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    written = {w.slot_id for w in plan.slot_writes}
    assert written == {"press-1"}


def test_baton_pass_does_not_pull_dependent_earlier():
    """Dependent slot planned LATER than the handoff cutoff stays put."""
    actual_at = NOW.replace(hour=8)
    press = _multistage_slot(
        id_="press-1", stage_id="press",
        status=SlotStatus.RUNNING, actual_start=NOW,
        planned_end=NOW.replace(hour=11),
    )
    blister = _multistage_slot(
        id_="blister-1", stage_id="blister",
        planned_start=NOW.replace(hour=15),
        planned_end=NOW.replace(hour=18),
    )
    snap = _multistage_snapshot((press, blister))
    event = ActualEndReported(
        job_reference_id="J1", stage_id="press", actual_at=actual_at,
    )

    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    written = {w.slot_id for w in plan.slot_writes}
    assert "blister-1" not in written


def test_baton_pass_pushes_multiple_parallel_dependents():
    """Both blister and lotcode depend on press — both get pushed."""
    actual_at = NOW.replace(hour=12)
    press = _multistage_slot(
        id_="press-1", stage_id="press",
        status=SlotStatus.RUNNING, actual_start=NOW,
        planned_end=NOW.replace(hour=11),
    )
    blister = _multistage_slot(
        id_="blister-1", stage_id="blister",
        planned_start=NOW.replace(hour=11), planned_end=NOW.replace(hour=14),
    )
    lotcode = _multistage_slot(
        id_="lot-1", stage_id="lotcode",
        planned_start=NOW.replace(hour=11), planned_end=NOW.replace(hour=13),
    )
    clamshell = _multistage_slot(
        id_="cs-1", stage_id="clamshell",
        # clamshell depends on blister/lotcode only — not press directly
        planned_start=NOW.replace(hour=14), planned_end=NOW.replace(hour=16),
    )
    snap = _multistage_snapshot((press, blister, lotcode, clamshell))
    event = ActualEndReported(
        job_reference_id="J1", stage_id="press", actual_at=actual_at,
    )

    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    by_slot = {w.slot_id: w for w in plan.slot_writes}
    assert set(by_slot.keys()) == {"press-1", "blister-1", "lot-1"}
    expected_handoff = actual_at.replace(hour=12, minute=30)
    assert by_slot["blister-1"].planned_start == expected_handoff
    assert by_slot["lot-1"].planned_start == expected_handoff


def test_baton_pass_skips_immovable_dependent():
    """Running dependent slot must not be shoved around."""
    actual_at = NOW.replace(hour=12)
    press = _multistage_slot(
        id_="press-1", stage_id="press",
        status=SlotStatus.RUNNING, actual_start=NOW,
        planned_end=NOW.replace(hour=11),
    )
    blister = _multistage_slot(
        id_="blister-1", stage_id="blister",
        status=SlotStatus.RUNNING, actual_start=NOW.replace(hour=11),
        planned_start=NOW.replace(hour=11), planned_end=NOW.replace(hour=14),
    )
    snap = _multistage_snapshot((press, blister))
    event = ActualEndReported(
        job_reference_id="J1", stage_id="press", actual_at=actual_at,
    )

    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    written = {w.slot_id for w in plan.slot_writes}
    assert "blister-1" not in written


def test_baton_pass_skips_manually_placed_dependent():
    """Manually-placed dependent stays put."""
    actual_at = NOW.replace(hour=12)
    press = _multistage_slot(
        id_="press-1", stage_id="press",
        status=SlotStatus.RUNNING, actual_start=NOW,
        planned_end=NOW.replace(hour=11),
    )
    blister = _multistage_slot(
        id_="blister-1", stage_id="blister",
        planned_start=NOW.replace(hour=11), planned_end=NOW.replace(hour=14),
        manually_placed=True,
    )
    snap = _multistage_snapshot((press, blister))
    event = ActualEndReported(
        job_reference_id="J1", stage_id="press", actual_at=actual_at,
    )

    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    written = {w.slot_id for w in plan.slot_writes}
    assert "blister-1" not in written


# ─── P0 regression: SlotWrite.instance must inherit from updated Slot ────


def _nexiuum_slot(**kwargs) -> Slot:
    """Build a Nexiuum-instance Slot. Helper for the P0 regression tests."""
    defaults = dict(
        id="nx-1", name="nx-slot",
        job_reference_id="J1", machine_id="NX-M1",
        stage_id="blister", recipe_key="tablet-blister-clamshell",
        recipe_version=1, quantity=100000,
        planned_start=NOW, planned_end=NOW,
        actual_start=None, actual_end=None,
        dependent_on_ids=(), status=SlotStatus.QUEUED,
        manually_placed=False, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=None,
        instance="nexiuum",
    )
    defaults.update(kwargs)
    return Slot(**defaults)


def test_actual_start_write_inherits_nexiuum_instance_from_slot():
    """REGRESSION (P0, 2026-05-26): SlotWrite.instance must propagate from
    the updated Slot so apply_plan routes to the right Schedule board.
    Without this, a Nexiuum slot's actual_start update would silently land
    on Gray Space Schedule (SlotWrite.instance defaults to 'gray_space')."""
    nx_slot = _nexiuum_slot(stage_id="blister", status=SlotStatus.QUEUED)
    snap = Snapshot(
        read_at=NOW, machines=(_machine(),),
        recipes=(_multistage_recipe(),), slots=(nx_slot,),
    )
    event = ActualStartReported(
        job_reference_id="J1", stage_id="blister", actual_at=ACTUAL_AT,
    )
    plan = plan_for_actual_start(snap, event)
    assert len(plan.slot_writes) == 1
    assert plan.slot_writes[0].instance == "nexiuum"


def test_actual_end_write_inherits_nexiuum_instance_from_slot():
    """REGRESSION (P0, 2026-05-26): same as start, but for actual_end."""
    nx_slot = _nexiuum_slot(
        stage_id="blister", status=SlotStatus.RUNNING, actual_start=NOW,
    )
    snap = Snapshot(
        read_at=NOW, machines=(_machine(),),
        recipes=(_multistage_recipe(),), slots=(nx_slot,),
    )
    event = ActualEndReported(
        job_reference_id="J1", stage_id="blister", actual_at=ACTUAL_AT,
    )
    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    finishing_writes = [w for w in plan.slot_writes if w.slot_id == "nx-1"]
    assert len(finishing_writes) == 1
    assert finishing_writes[0].instance == "nexiuum"


def test_baton_pass_write_inherits_dependent_slot_instance():
    """REGRESSION (P0, 2026-05-26): cross-instance baton-pass — a Gray
    Space press finishing must push a Nexiuum blister slot's planned_start
    via a write tagged instance='nexiuum'. Otherwise the engine routes
    the Nexiuum-board update to the Gray Space board."""
    gs_press = _multistage_slot(
        id_="gs-press-1", stage_id="press",
        status=SlotStatus.RUNNING, actual_start=NOW,
        planned_start=NOW, planned_end=NOW.replace(hour=11),
    )
    # NB: _multistage_slot defaults instance to gray_space — override via
    # direct Slot construction for the Nexiuum dependent.
    nx_blister = _nexiuum_slot(
        id="nx-blister-1", stage_id="blister",
        planned_start=NOW.replace(hour=11),
        planned_end=NOW.replace(hour=14),
    )
    snap = Snapshot(
        read_at=NOW, machines=(_machine(),),
        recipes=(_multistage_recipe(),), slots=(gs_press, nx_blister),
    )
    actual_at = NOW.replace(hour=12)  # press finishes 1hr late
    event = ActualEndReported(
        job_reference_id="J1", stage_id="press", actual_at=actual_at,
    )
    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    by_slot = {w.slot_id: w for w in plan.slot_writes}

    # Press write tagged gray_space; baton-pass write tagged nexiuum.
    assert by_slot["gs-press-1"].instance == "gray_space"
    assert by_slot["nx-blister-1"].instance == "nexiuum"


def test_baton_pass_does_not_cascade_to_transitive_dependents():
    """PINNED BEHAVIOR (Phase 2C, 2026-05-26): baton-pass pushes ONLY
    immediate dependents — not transitive ones.

    Scenario: press finishes very late. press's push moves blister
    forward into clamshell's territory, but clamshell is NOT pushed —
    it'll be pushed when blister actually finishes and emits its own
    ActualEndReported. This boundary is intentional; the docstring on
    plan_for_actual_end explains why.

    If this test fails, EITHER the cascade behavior was deliberately
    changed (update the docstring) OR a regression introduced
    transitive pushing (revert).
    """
    actual_at = NOW.replace(hour=15)  # press finishes very late
    press = _multistage_slot(
        id_="press-1", stage_id="press",
        status=SlotStatus.RUNNING, actual_start=NOW,
        planned_end=NOW.replace(hour=11),
    )
    # Blister depends on press, planned at the original press end.
    blister = _multistage_slot(
        id_="blister-1", stage_id="blister",
        planned_start=NOW.replace(hour=11),
        planned_end=NOW.replace(hour=14),
    )
    # Clamshell depends on blister + lotcode (not on press). Currently
    # planned at 14:00 — would conflict with blister's pushed slot.
    clamshell = _multistage_slot(
        id_="clamshell-1", stage_id="clamshell",
        planned_start=NOW.replace(hour=14),
        planned_end=NOW.replace(hour=16),
    )
    lotcode = _multistage_slot(
        id_="lot-1", stage_id="lotcode",
        planned_start=NOW.replace(hour=11),
        planned_end=NOW.replace(hour=13),
    )
    snap = _multistage_snapshot((press, blister, lotcode, clamshell))
    event = ActualEndReported(
        job_reference_id="J1", stage_id="press", actual_at=actual_at,
    )

    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    by_slot = {w.slot_id: w for w in plan.slot_writes}

    # Press, blister, lotcode pushed (all are immediate dependents of press).
    assert "press-1" in by_slot
    assert "blister-1" in by_slot
    assert "lot-1" in by_slot
    # Clamshell NOT pushed — it depends on blister + lotcode, not on press.
    # Transitive cascade happens when blister/lotcode actually finish.
    assert "clamshell-1" not in by_slot


def test_baton_pass_handles_dangling_recipe_gracefully():
    """If the recipe pinned on the finishing slot isn't in the snapshot,
    note the issue but don't crash — just stamp the press actual_end."""
    actual_at = NOW.replace(hour=12)
    press = _multistage_slot(
        id_="press-1", stage_id="press",
        status=SlotStatus.RUNNING, actual_start=NOW,
        planned_end=NOW.replace(hour=11),
    )
    blister = _multistage_slot(
        id_="blister-1", stage_id="blister",
        planned_start=NOW.replace(hour=11), planned_end=NOW.replace(hour=14),
    )
    snap = Snapshot(
        read_at=NOW, machines=(_machine(),), recipes=(), slots=(press, blister),
    )
    event = ActualEndReported(
        job_reference_id="J1", stage_id="press", actual_at=actual_at,
    )

    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    written = {w.slot_id for w in plan.slot_writes}
    assert "press-1" in written
    assert "blister-1" not in written
    assert any("dangling recipe" in n for n in plan.notes)


def test_baton_pass_pushes_synthetic_packaging_slots():
    """Phase 1.5 — when a recipe terminal stage finishes, baton-pass also
    pushes synthetic packaging slots (stage_id starts with `pkg_`) for the
    same job. These slots aren't in the recipe DAG, so the recipe-only
    dependency lookup would miss them without this branch.
    """
    # Recipe = press only (terminal stage = press).
    actual_at = NOW.replace(hour=12)
    press_slot = _slot(
        id_="press-1", stage_id="press",
        status=SlotStatus.RUNNING, actual_start=NOW,
    )
    clam_slot = Slot(
        id="pkg-clam-1", name="N1 → pkg_0_Clamshell",
        job_reference_id="J1", machine_id="M2",
        stage_id="pkg_0_Clamshell",
        recipe_key="tablet-press-standard", recipe_version=1,
        quantity=250_000,
        planned_start=NOW.replace(hour=10), planned_end=NOW.replace(hour=14),
        actual_start=None, actual_end=None,
        dependent_on_ids=(), status=SlotStatus.QUEUED,
        manually_placed=False, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=None,
    )
    sach_slot = Slot(
        id="pkg-sach-1", name="N1 → pkg_1_Sachet",
        job_reference_id="J1", machine_id="M3",
        stage_id="pkg_1_Sachet",
        recipe_key="tablet-press-standard", recipe_version=1,
        quantity=250_000,
        planned_start=NOW.replace(hour=10), planned_end=NOW.replace(hour=15),
        actual_start=None, actual_end=None,
        dependent_on_ids=(), status=SlotStatus.QUEUED,
        manually_placed=False, priority=Priority.NORMAL,
        last_reflow_hash=None, drift_last_detected_at=None,
    )
    snap = _snapshot((press_slot, clam_slot, sach_slot))
    event = ActualEndReported(
        job_reference_id="J1", stage_id="press", actual_at=actual_at,
    )

    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    by_slot = {w.slot_id: w for w in plan.slot_writes}

    assert "press-1" in by_slot  # the finishing slot itself
    # Both synthetic packaging slots get pushed to actual_at + 30min.
    expected_start = actual_at.replace(hour=12, minute=30)
    assert by_slot["pkg-clam-1"].planned_start == expected_start
    assert by_slot["pkg-sach-1"].planned_start == expected_start


# ─── N# propagation: writes originating from an existing Slot copy its N# ──
# Round-trip guarantee — N# survives Snapshot → SlotWrite → board → next
# Snapshot. A silent regression here would drop N# off update writes.

from dataclasses import replace  # noqa: E402


def test_actual_start_write_preserves_n_number():
    slot = replace(_slot(status=SlotStatus.QUEUED), n_number="N3629")
    snap = _snapshot((slot,))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert plan.slot_writes
    assert all(w.n_number == "N3629" for w in plan.slot_writes)


def test_actual_end_write_preserves_n_number():
    slot = replace(
        _slot(status=SlotStatus.RUNNING, actual_start=NOW), n_number="N3629",
    )
    snap = _snapshot((slot,))
    event = ActualEndReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    assert plan.slot_writes
    # The finishing slot's write carries the N#.
    finishing = [w for w in plan.slot_writes if w.slot_id == "S1"]
    assert finishing and finishing[0].n_number == "N3629"


def test_baton_pass_write_preserves_dependent_slot_n_number():
    """A baton-pass write originates from the dependent Slot — it copies that
    Slot's N#, not the finishing slot's."""
    actual_at = NOW.replace(hour=11)
    press = replace(
        _multistage_slot(
            id_="press-1", stage_id="press",
            status=SlotStatus.RUNNING, actual_start=NOW,
            planned_start=NOW, planned_end=NOW.replace(hour=11),
        ),
        n_number="N3629",
    )
    blister = replace(
        _multistage_slot(
            id_="blister-1", stage_id="blister",
            planned_start=NOW.replace(hour=11), planned_end=NOW.replace(hour=14),
        ),
        n_number="N3629",
    )
    snap = _multistage_snapshot((press, blister))
    event = ActualEndReported(job_reference_id="J1", stage_id="press", actual_at=actual_at)
    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    by_slot = {w.slot_id: w for w in plan.slot_writes}
    assert by_slot["blister-1"].planned_start is not None  # was pushed
    assert by_slot["blister-1"].n_number == "N3629"


# ─── Flavor propagation: writes from an existing Slot copy its flavor (#5) ──
# Same round-trip guarantee as N#: flavor survives Snapshot → SlotWrite →
# board → next Snapshot on actuals + baton-pass writes.


def test_actual_start_write_preserves_flavor():
    slot = replace(_slot(status=SlotStatus.QUEUED), flavor="Strawberry Banana")
    snap = _snapshot((slot,))
    event = ActualStartReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_start(snap, event)
    assert plan.slot_writes
    assert all(w.flavor == "Strawberry Banana" for w in plan.slot_writes)


def test_actual_end_write_preserves_flavor():
    slot = replace(
        _slot(status=SlotStatus.RUNNING, actual_start=NOW), flavor="Strawberry Banana",
    )
    snap = _snapshot((slot,))
    event = ActualEndReported(job_reference_id="J1", stage_id="press", actual_at=ACTUAL_AT)
    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    finishing = [w for w in plan.slot_writes if w.slot_id == "S1"]
    assert finishing and finishing[0].flavor == "Strawberry Banana"


def test_baton_pass_write_preserves_dependent_slot_flavor():
    """A baton-pass write copies the dependent Slot's flavor (like its N#)."""
    actual_at = NOW.replace(hour=11)
    press = replace(
        _multistage_slot(
            id_="press-1", stage_id="press",
            status=SlotStatus.RUNNING, actual_start=NOW,
            planned_start=NOW, planned_end=NOW.replace(hour=11),
        ),
        flavor="Cherry Lime",
    )
    blister = replace(
        _multistage_slot(
            id_="blister-1", stage_id="blister",
            planned_start=NOW.replace(hour=11), planned_end=NOW.replace(hour=14),
        ),
        flavor="Cherry Lime",
    )
    snap = _multistage_snapshot((press, blister))
    event = ActualEndReported(job_reference_id="J1", stage_id="press", actual_at=actual_at)
    plan = plan_for_actual_end(snap, event, handoff_buffer_minutes=30)
    by_slot = {w.slot_id: w for w in plan.slot_writes}
    assert by_slot["blister-1"].planned_start is not None  # was pushed
    assert by_slot["blister-1"].flavor == "Cherry Lime"
