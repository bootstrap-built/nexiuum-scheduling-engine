"""Domain models — pure dataclasses, no IO.

Snapshot is the canonical in-memory representation of the Monday-side state
the engine reasons about. Built by `engine.io.snapshot.read_snapshot()`.

The pure-core placement function takes a Snapshot and (optionally) a new
order, and returns a Plan — a set of intended writes. The IO shell applies
the Plan to Monday.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


# ─────────────────────────────────────────────────────────────────────────
# Domain enums
# ─────────────────────────────────────────────────────────────────────────


class MachineStatus(str, Enum):
    ONLINE = "Online"
    DOWN = "Down"
    MAINTENANCE = "Scheduled Maintenance"


class SlotStatus(str, Enum):
    QUEUED = "Queued"
    RUNNING = "Running"
    DONE = "Done"
    BLOCKED = "Blocked"


class Priority(str, Enum):
    NORMAL = "Normal"
    EXPEDITE = "Expedite"


class RecipeStatus(str, Enum):
    DRAFT = "Draft"
    ACTIVE = "Active"
    RETIRED = "Retired"


ProcessGroup = Literal[
    "Pressing", "Capsule", "Sachet", "Blister", "Clamshell", "Bottle", "Lot Coder", "Hand-pack"
]


# Process groups whose Capacity Engine "units/hr" is the *packaging container*
# rate, not the tablet/capsule rate. A Clamshell-1 at 3,200 units/hr packs
# 3,200 *clamshells* per hour — if each clamshell holds 5 tabs, real tab
# throughput is 16,000/hr. Scheduler multiplies capacity by
# `PackagingSlice.items_per_container` when computing duration for these.
#
# Lot Coder and Hand-pack are intentionally NOT in this set (item-rate for
# now). VERIFY WITH MAKAYLA before either runs a real order — Lot Coder
# likely codes *containers* after a packaging stage, so it probably belongs
# here; Hand-pack semantics depend on whether ops counts containers or tabs.
CONTAINER_CAPACITY_GROUPS: frozenset[ProcessGroup] = frozenset(
    {"Sachet", "Blister", "Clamshell", "Bottle"}
)


# Process groups that the scheduler will fan a single stage across multiple
# eligible machines for (cross-machine split). Press stays single-machine —
# Makayla confirmed splitting only happens on the packaging side. Same set
# as CONTAINER_CAPACITY_GROUPS today but kept separate so packaging-rate
# vs. parallel-fan-out decisions can drift independently later.
SPLITTABLE_PROCESS_GROUPS: frozenset[ProcessGroup] = frozenset(
    {"Sachet", "Blister", "Clamshell", "Bottle"}
)


# ─────────────────────────────────────────────────────────────────────────
# Domain entities
# ─────────────────────────────────────────────────────────────────────────


MondayInstance = Literal["gray_space", "nexiuum"]


@dataclass(frozen=True)
class Machine:
    """One row of the Capacity Engine board."""

    id: str  # Monday item id (string for forward compat with Monday)
    name: str
    process_group: ProcessGroup | None
    status: MachineStatus
    capacity_per_hour: float
    hours_per_day: float
    working_window_start: int  # hour-of-day 0..23
    working_window_end: int  # hour-of-day 0..24 (24 = midnight next day)
    changeover_minutes: int
    dual_sided_only: bool
    max_job_size: int | None  # None = no cap
    force_route_condition: str | None  # free-text rule, engine parses
    last_job_ended_at: datetime | None  # local time
    # Phase 2 — which Monday account this machine row came from. Default
    # "gray_space" preserves Phase 1 behavior and existing test fixtures.
    instance: MondayInstance = "gray_space"

    @property
    def is_available(self) -> bool:
        """A machine is available for new placements when it is Online, has
        nonzero daily hours, AND has nonzero per-hour capacity.

        The capacity check protects the scheduler's `quantity / capacity`
        duration math from divide-by-zero on machines whose capacity hasn't
        been filled in yet (e.g., the Nexiuum machines flagged
        VERIFY WITH MAKAYLA at provisioning time, which default to 0).
        Such machines surface as "all_machines_down" in routing diagnostics
        — semantically correct: they CAN'T do work until ops fills the
        Capacity column.
        """
        return (
            self.status == MachineStatus.ONLINE
            and self.hours_per_day > 0
            and self.capacity_per_hour > 0
        )


@dataclass(frozen=True)
class RecipeStage:
    """One stage within a Process Recipe's DAG."""

    id: str  # stage identifier (e.g., "press", "blister", "lotcode")
    machine_class: ProcessGroup
    depends_on: tuple[str, ...]  # stage ids this stage depends on


@dataclass(frozen=True)
class Recipe:
    """One row of the Process Recipe board (one version of a recipe)."""

    id: str  # Monday item id
    name: str
    recipe_key: str
    version: int
    status: RecipeStatus
    stages: tuple[RecipeStage, ...]
    # Phase 2 — which Monday account this recipe row came from. Default
    # "gray_space" preserves Phase 1 behavior and existing test fixtures.
    instance: MondayInstance = "gray_space"

    @property
    def composite_key(self) -> tuple[str, int]:
        return (self.recipe_key, self.version)


@dataclass(frozen=True)
class Slot:
    """One row of the Schedule board — one machine-job pairing."""

    id: str
    name: str
    job_reference_id: str | None  # Blend Records item id
    machine_id: str | None
    stage_id: str | None
    recipe_key: str | None
    recipe_version: int | None
    quantity: int
    planned_start: datetime | None  # local time
    planned_end: datetime | None
    actual_start: datetime | None
    actual_end: datetime | None
    dependent_on_ids: tuple[str, ...]
    status: SlotStatus
    manually_placed: bool
    priority: Priority
    last_reflow_hash: str | None
    drift_last_detected_at: datetime | None
    instance: MondayInstance = "gray_space"

    @property
    def is_immovable(self) -> bool:
        """Engine cannot move this slot during reflow."""
        return self.manually_placed or self.status in {SlotStatus.RUNNING, SlotStatus.DONE}

    @property
    def is_active(self) -> bool:
        """Slot is in flight (Queued or Running) and visible to reflow."""
        return self.status in {SlotStatus.QUEUED, SlotStatus.RUNNING}


# ─────────────────────────────────────────────────────────────────────────
# Snapshot — what the engine reads
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Snapshot:
    """Point-in-time view of all engine-relevant Monday board state.

    Built fresh at the start of every event handled by the worker. Engine
    never caches state across events — Monday is system of record.
    """

    read_at: datetime  # local time
    machines: tuple[Machine, ...]
    recipes: tuple[Recipe, ...]
    slots: tuple[Slot, ...]

    def machine_by_id(self, machine_id: str) -> Machine | None:
        for m in self.machines:
            if m.id == machine_id:
                return m
        return None

    def recipe_by_composite_key(self, key: str, version: int) -> Recipe | None:
        for r in self.recipes:
            if r.recipe_key == key and r.version == version:
                return r
        return None

    def slots_on_machine(self, machine_id: str) -> tuple[Slot, ...]:
        return tuple(s for s in self.slots if s.machine_id == machine_id and s.is_active)

    def slots_for_job(self, job_reference_id: str) -> tuple[Slot, ...]:
        return tuple(s for s in self.slots if s.job_reference_id == job_reference_id)


# ─────────────────────────────────────────────────────────────────────────
# Engine events and plans
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PackagingSlice:
    """One packaging configuration within an order's packaging breakdown.

    Captures a portion of the order that's packaged in a particular machine
    class with a particular container fill count. Drives:

    - Generation of a synthetic packaging stage hung off the recipe's
      terminal stage(s).
    - Per-slice cross-machine splitting (a 500k-tab slice may fan across
      multiple Clamshell machines).
    - Container-rate capacity math via `items_per_container`.

    Example: an order for 1,000,000 tablets packaged as 50% 3-count
    clamshells + 50% 5-count sachets becomes:

        packaging_breakdown=(
            PackagingSlice("Clamshell", 500_000, 3, "3ct"),
            PackagingSlice("Sachet",    500_000, 5, "5ct"),
        )

    `config_notes` is operator-facing — appears in slot names and on the
    Marey chart. Free-text per the Spec Sheet form decision (blister
    configs like "3 count diamond" don't fit a dropdown).
    """

    machine_class: ProcessGroup
    quantity: int
    items_per_container: int = 1
    config_notes: str = ""


@dataclass(frozen=True)
class ScheduleNewOrder:
    """Event: a new upstream order needs to be scheduled."""

    job_reference_id: str
    recipe_key: str
    recipe_version: int
    quantity: int
    # Extension hooks for hard-routing rules; engine reads these to apply rules.
    dual_sided: bool = False
    active_mg: float | None = None
    requested_ship_by: datetime | None = None
    # Phase 1.5 — packaging breakdown for split-packaging orders. Empty tuple
    # preserves Phase 1/2C behavior: only the recipe-defined stages run.
    # When non-empty, the scheduler appends one synthetic packaging stage per
    # slice, each depending on the recipe DAG's terminal stage(s). Slices can
    # share a machine_class (rare — two clamshell configs) or differ
    # (common — clamshell + sachet split).
    packaging_breakdown: tuple[PackagingSlice, ...] = ()


@dataclass(frozen=True)
class CapacityChanged:
    """Event: a Capacity Engine machine's status or capacity changed."""

    machine_id: str


@dataclass(frozen=True)
class ActualStartReported:
    """Event: a source board posted an actual_start for one or more slots."""

    job_reference_id: str
    stage_id: str
    actual_at: datetime  # local time


@dataclass(frozen=True)
class ActualEndReported:
    """Event: a source board posted an actual_end for one or more slots."""

    job_reference_id: str
    stage_id: str
    actual_at: datetime


@dataclass(frozen=True)
class ManualReschedule:
    """Event: an operator dragged a slot to a new time."""

    slot_id: str


@dataclass(frozen=True)
class ExpediteRequested:
    """Event: a slot was marked Expedite priority."""

    slot_id: str


@dataclass(frozen=True)
class DriftDetected:
    """Event: polling sweep detected a slot exceeding its drift threshold."""

    slot_id: str
    kind: Literal["late_start", "late_end"]


Event = (
    ScheduleNewOrder
    | CapacityChanged
    | ActualStartReported
    | ActualEndReported
    | ManualReschedule
    | ExpediteRequested
    | DriftDetected
)


@dataclass(frozen=True)
class SlotWrite:
    """A pending write to a Schedule item.

    Engine produces these as part of a Plan; IO shell applies them via
    GraphQL mutations.

    Field semantics:
    - `slot_id=None` → create a new slot. Otherwise → update the existing slot.
    - A field set to a non-None value → write that value to the Schedule item.
    - A field set to None → don't touch (no write for that column).
    - To CLEAR a field (e.g., reset `actual_start` to empty), include its name
      in `fields_to_clear`. The IO shell will send a Monday "clear column"
      mutation for those columns regardless of any other value.

    `fields_to_clear` is the explicit way to express "make this column empty"
    because Python `None` is ambiguous (don't-touch vs make-empty). Without
    this, the engine couldn't undo a false-positive actual_start write, for
    example. Field names match SlotWrite attribute names (e.g., 'actual_start').
    """

    slot_id: str | None  # None means create a new slot
    name: str | None = None
    machine_id: str | None = None
    job_reference_id: str | None = None
    stage_id: str | None = None
    recipe_key: str | None = None
    recipe_version: int | None = None
    quantity: int | None = None
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    dependent_on_ids: tuple[str, ...] | None = None
    status: SlotStatus | None = None
    manually_placed: bool | None = None
    priority: Priority | None = None
    last_reflow_hash: str | None = None
    drift_last_detected_at: datetime | None = None
    fields_to_clear: frozenset[str] = field(default_factory=frozenset)
    # Phase 2: which instance owns the target Schedule board for this write.
    # Pure-core sets this based on the placed machine's instance. IO shell
    # routes each write to the right Schedule board + Monday client.
    instance: MondayInstance = "gray_space"


@dataclass(frozen=True)
class MachineWrite:
    """A pending write to a Capacity Engine item."""

    machine_id: str
    last_job_ended_at: datetime | None = None


@dataclass(frozen=True)
class Plan:
    """Output of the pure core: a set of intended writes plus rationale.

    The IO shell diffs this against the source snapshot and emits only the
    writes that represent actual changes.
    """

    slot_writes: tuple[SlotWrite, ...] = field(default_factory=tuple)
    machine_writes: tuple[MachineWrite, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)  # human-readable rationale
