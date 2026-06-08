"""Engine configuration loaded from environment variables.

All scheduling-relevant configuration lives here. Boards are referenced by ID
(not by name) because Monday board names can change but IDs cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Per-board column-id maps ─────────────────────────────────────────────
# Each Monday board gets fresh column IDs at creation time, so dual-instance
# parsing needs to pick the right ID set based on the board the row came
# from. These small dataclasses bundle a board's column IDs into a single
# argument the parsers can take.


@dataclass(frozen=True)
class CapacityEngineCols:
    status: str
    capacity: str
    hours_per_day: str
    window_start: str
    window_end: str
    changeover: str
    process_group: str
    dual_sided: str
    max_job_size: str
    force_route: str
    last_job_ended_at: str
    notes: str


@dataclass(frozen=True)
class ProcessRecipeCols:
    key: str
    version: str
    status: str
    stages: str


@dataclass(frozen=True)
class ScheduleCols:
    machine: str
    job_reference: str
    stage_id: str
    recipe_key: str
    recipe_version: str
    quantity: str
    capacity_mirror: str
    duration_formula: str
    planned_start: str
    planned_end: str
    actual_start: str
    actual_end: str
    dependent_on: str
    status: str
    manually_placed: str
    priority: str
    last_reflow_hash: str
    drift_last_detected_at: str
    # N# traceability text column (added to both Schedule boards in #3).
    # The engine stamps the originating PO's N# here on every SlotWrite that
    # carries one, so operators can filter/sort the Schedule board by N#.
    n_number: str
    # Flavor text column (added to both Schedule boards in #3). The engine
    # stamps the indexed flavor's name here on every SlotWrite that carries
    # one, so operators see the flavor on the Schedule board and Marey chart.
    flavor: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ── Monday API tokens ────────────────────────────────────────────────
    # Env vars use the existing convention: MONDAY_<ACCOUNT>_TOKEN
    # (matches ~/.monday_tokens layout — see ~/CLAUDE.md "Credentials & Tokens")
    gray_space_monday_token: str = Field(
        ..., alias="MONDAY_GRAYSPACE_TOKEN", description="Token for Gray Space Monday account"
    )
    nexiuum_monday_token: str = Field(
        "", alias="MONDAY_NEXIUUM_TOKEN", description="Phase 2 — Nexiuum account token"
    )

    # ── Monday board IDs (Phase 1: Gray Space) ───────────────────────────
    gray_space_capacity_engine_board: int = 18413803163
    gray_space_process_recipe_board: int = 18414126054
    gray_space_schedule_board: int = 18413802995
    gray_space_blend_records_board: int = 18404836849

    # ── Monday board IDs (Phase 2: Nexiuum, optional) ────────────────────
    # When all populated, the snapshot reader pulls Capacity Engine + Process
    # Recipe from both accounts and merges them with an `instance` tag.
    # Schedule board reads stay on Gray Space this phase — see plan §2.
    # Default 0 means "unconfigured". The model validator below enforces
    # all-or-nothing: either every Nexiuum field is set, or none of them are.
    nexiuum_capacity_engine_board: int = Field(
        0, alias="NEXIUUM_CAPACITY_ENGINE_BOARD",
        description="Phase 2 — Nexiuum Capacity Engine board id (0 = unconfigured)",
    )
    nexiuum_process_recipe_board: int = Field(
        0, alias="NEXIUUM_PROCESS_RECIPE_BOARD",
        description="Phase 2 — Nexiuum Process Recipe board id (0 = unconfigured)",
    )
    nexiuum_schedule_board: int = Field(
        0, alias="NEXIUUM_SCHEDULE_BOARD",
        description="Phase 2 — Nexiuum Schedule board id (0 = unconfigured). "
        "Slots for packaging stages get written here per Option B (2 boards, "
        "unified Marey view via /schedule.json).",
    )
    # Phase 2D — Production Schedule board on the Nexiuum instance. Holds
    # the form's per-flavor items + the Spec Sheet Payload long-text
    # column the engine reads. READ-ONLY for the engine (locked decision
    # — only the spec sheet form writes here). Default is the live board
    # ID so the env override is just for non-prod tests.
    nexiuum_production_schedule_board: int = Field(
        8196668916, alias="NEXIUUM_PRODUCTION_SCHEDULE_BOARD",
        description="Phase 2D — Nexiuum Production Schedule board id. "
        "Engine reads Spec Sheet Payload from here; NEVER writes.",
    )
    # Spec Sheet Payload long-text column on Production Schedule. Holds
    # the form's canonical structured submission JSON per item.
    col_ps_spec_sheet_payload: str = "long_text_mm3bbhcv"
    # Phase 2D — the "Nexiuum #" board_relation column on Production Schedule
    # that links each per-flavor item to its PO on the Quotes/Deals/POs board.
    # The engine reads its `display_value` (the linked PO's name, e.g.
    # "N3629") as the order's N#. It is a board_relation, not a plain mirror
    # text column — the reader pulls `display_value`, not `text`. READ-ONLY:
    # the engine never writes the Production Schedule board.
    col_ps_n_number: str = "board_relation_mktgp1ja"

    # ── Schedule board column IDs (Gray Space — Phase 1) ─────────────────
    # Captured from the board after creation. Hardcoded because Monday's API
    # requires column IDs (not titles) for mutations. Each Monday board gets
    # its own column IDs at creation time, so Nexiuum needs a separate set
    # below.
    col_schedule_machine: str = "board_relation_mm3gn2vv"
    col_schedule_job_reference: str = "board_relation_mm3hx401"
    col_schedule_stage_id: str = "text_mm3hrkha"
    col_schedule_recipe_key: str = "text_mm3hpc3p"
    col_schedule_recipe_version: str = "numeric_mm3h4h1y"
    col_schedule_quantity: str = "numeric_mm3g2z69"
    col_schedule_capacity_mirror: str = "lookup_mm3gerzn"
    col_schedule_duration_formula: str = "formula_mm3gntc2"
    col_schedule_planned_start: str = "date4"
    col_schedule_planned_end: str = "date_mm3gymys"
    col_schedule_actual_start: str = "date_mm3h1sxx"
    col_schedule_actual_end: str = "date_mm3h35ya"
    col_schedule_dependent_on: str = "dependency_mm3g3zqm"
    col_schedule_status: str = "color_mm3hjj5"
    col_schedule_manually_placed: str = "boolean_mm3hmk3w"
    col_schedule_priority: str = "color_mm3h2cm7"
    col_schedule_last_reflow_hash: str = "text_mm3hf0h5"
    col_schedule_drift_last_detected_at: str = "date_mm3h9jxp"
    col_schedule_n_number: str = "text_mm3wcm9e"  # "N#" text column (#3)
    col_schedule_flavor: str = "text_mm3w6p7b"  # "Flavor" text column (#3)

    # ── Capacity Engine column IDs ───────────────────────────────────────
    col_cap_status: str = "color_mm3gcye0"
    col_cap_capacity: str = "numeric_mm3gz5mf"
    col_cap_hours_per_day: str = "numeric_mm3gy2xe"
    col_cap_window_start: str = "numeric_mm3gnwvx"
    col_cap_window_end: str = "numeric_mm3gzkmh"
    col_cap_changeover: str = "numeric_mm3gh9ag"
    col_cap_process_group: str = "color_mm3hzm71"
    col_cap_dual_sided: str = "boolean_mm3hxqp2"
    col_cap_max_job_size: str = "numeric_mm3hhx3e"
    col_cap_force_route: str = "text_mm3h9c3f"
    col_cap_last_job_ended_at: str = "date_mm3h5h9j"
    col_cap_notes: str = "long_text_mm3ha7b6"

    # ── Process Recipe column IDs (Gray Space) ───────────────────────────
    col_recipe_key: str = "text_mm3hbfjj"
    col_recipe_version: str = "numeric_mm3heae"
    col_recipe_status: str = "color_mm3hfww4"
    col_recipe_stages: str = "long_text_mm3hxf7h"

    # ── Nexiuum column IDs (Phase 2) ─────────────────────────────────────
    # Captured from boards provisioned 2026-05-25. Different from Gray Space
    # column IDs because Monday issues fresh column IDs per board.
    # Capacity Engine (board 18414776125):
    col_nx_cap_status: str = "color_mm3pgenv"
    col_nx_cap_capacity: str = "numeric_mm3p3vf1"
    col_nx_cap_hours_per_day: str = "numeric_mm3pykc4"
    col_nx_cap_window_start: str = "numeric_mm3prxx9"
    col_nx_cap_window_end: str = "numeric_mm3ptd6g"
    col_nx_cap_changeover: str = "numeric_mm3pptyx"
    col_nx_cap_process_group: str = "color_mm3pyah"
    col_nx_cap_dual_sided: str = "boolean_mm3pyrrj"
    col_nx_cap_max_job_size: str = "numeric_mm3peq9p"
    col_nx_cap_force_route: str = "text_mm3pchdt"
    col_nx_cap_last_job_ended_at: str = "date_mm3pz2sz"
    col_nx_cap_notes: str = "long_text_mm3p8eca"
    # Process Recipe (board 18414776199):
    col_nx_recipe_key: str = "text_mm3psy0d"
    col_nx_recipe_version: str = "numeric_mm3pvgj5"
    col_nx_recipe_status: str = "color_mm3pfew0"
    col_nx_recipe_stages: str = "long_text_mm3p5bmn"
    # Schedule (board 18414776220):
    col_nx_schedule_machine: str = "board_relation_mm3qxz64"
    col_nx_schedule_job_reference: str = "board_relation_mm3qf09v"
    col_nx_schedule_stage_id: str = "text_mm3pfvk4"
    col_nx_schedule_recipe_key: str = "text_mm3ppx1w"
    col_nx_schedule_recipe_version: str = "numeric_mm3p900w"
    col_nx_schedule_quantity: str = "numeric_mm3pasam"
    col_nx_schedule_capacity_mirror: str = "lookup_mm3qbysy"
    col_nx_schedule_duration_formula: str = "formula_mm3p4axb"
    col_nx_schedule_planned_start: str = "date_mm3pc27v"
    col_nx_schedule_planned_end: str = "date_mm3pqw19"
    col_nx_schedule_actual_start: str = "date_mm3pd2vb"
    col_nx_schedule_actual_end: str = "date_mm3pb62w"
    col_nx_schedule_dependent_on: str = "dependency_mm3p48hp"
    col_nx_schedule_status: str = "color_mm3pt6kh"
    col_nx_schedule_manually_placed: str = "boolean_mm3pxeat"
    col_nx_schedule_priority: str = "color_mm3patr6"
    col_nx_schedule_last_reflow_hash: str = "text_mm3prf9j"
    col_nx_schedule_drift_last_detected_at: str = "date_mm3pdj0y"
    col_nx_schedule_n_number: str = "text_mm3w4ghr"  # "N#" text column (#3)
    col_nx_schedule_flavor: str = "text_mm3w91af"  # "Flavor" text column (#3)

    # ── Blend Records column IDs (source board for press actuals) ────────
    col_blend_status: str = "color_mm1mb9cm"  # "Blend Status" — flips to "Pressing" → actual_start
    # Display-only columns enriched into /schedule.json so the Marey view can
    # show human-meaningful labels (N# instead of pulse id, client + active
    # ingredient in the click popout). Not read by the pure-core scheduler.
    col_blend_po_number: str = "text_mm1mpz7p"  # "PO Number" — the N#
    col_blend_client: str = "text_mm1mw6j9"  # "Client"
    col_blend_active_ingredient: str = "text_mm1m9f2e"  # "Active Ingredient"

    # ── Source-board → engine mapping (E5) ───────────────────────────────
    # When Blend Status flips to this label on Blend Records, the engine
    # writes actual_start + Status=Running on the matching Schedule slot
    # (the one whose stage_id matches `blend_status_pressing_stage_id`).
    # Phase 1: single tablet-press-standard recipe with one "press" stage.
    blend_status_pressing_label: str = "Pressing"
    blend_status_pressing_stage_id: str = "press"
    # ADR-0004 — when Blend Status flips to this label, a pressing order that was
    # deferred at create_item is released onto the schedule (full press +
    # packaging chain). This is the press-scheduling trigger.
    blend_status_blending_label: str = "Blending"
    # #23 — the Blend Records text column the blend-intake workflow stamps with
    # the originating Production Schedule item id (`source_item_id`). The engine
    # resolves Blend Record → PS Order through this on every Blend Records event.
    col_blend_source_item: str = "text_mm1mjk8n"
    # Phase 2C — when Blend Status flips to this label, the engine writes
    # actual_end + Status=Done on the press slot AND adjusts dependent
    # packaging slots' planned_start via the baton-pass.
    blend_status_done_label: str = "Done"
    # Min minutes between a finished press and the start of its dependent
    # packaging stage. Used by baton-pass: if a press finishes at T, no
    # dependent stage may be planned to start before T + handoff_buffer.
    # 30 min mirrors the default changeover_minutes on Capacity Engine.
    cross_stage_handoff_buffer_minutes: int = 30

    # ── Cross-machine split (Phase 1.5) ──────────────────────────────────
    # When a packaging-class stage has multiple eligible machines and a
    # large quantity, the scheduler fans the slice across them so idle
    # packaging machines don't sit while one machine grinds through a
    # million tabs. Press stays single-machine.
    #
    # split_min_quantity: stage quantity (after the items_per_container
    #   multiplier — i.e., compared against tab count not container count)
    #   below which we never split. A 5k-tab order doesn't justify
    #   fragmenting onto two machines. 50,000 is a reasonable default for
    #   tablet/capsule volumes Makayla mentioned (orders into 7 figures).
    # split_max_machines: hard cap on how many machines one stage spreads
    #   across. Even with 8 eligible machines, splitting 6-ways is more
    #   coordination headache than it's worth.
    # split_chunk_round_to: chunk quantities round to the nearest N tabs
    #   so the numbers operators see are clean (e.g., "350k / 250k" instead
    #   of "353,847 / 246,153"). Remainder absorbed by the largest-capacity
    #   machine's chunk so the total quantity is preserved exactly.
    split_min_quantity: int = Field(50_000, ge=1)
    split_max_machines: int = Field(4, ge=1)
    split_chunk_round_to: int = Field(100, ge=1)

    # ── Timezone ─────────────────────────────────────────────────────────
    factory_tz: str = "America/Denver"

    # ── Polling sweep ────────────────────────────────────────────────────
    polling_interval_minutes: int = 15
    drift_threshold_minutes: int = 15
    drift_suppression_minutes: int = 60

    # ── Server ───────────────────────────────────────────────────────────
    port: int = 8002
    log_level: str = "INFO"

    # ── HTTP request limits ──────────────────────────────────────────────
    # /commit awaits the worker queue, which can wedge if Monday is slow or
    # an earlier event hangs. Without a per-request timeout the HTTP
    # connection could block indefinitely. Codex E4 review item.
    commit_timeout_seconds: float = 30.0

    # ── Webhook auth ─────────────────────────────────────────────────────
    # Phase 1: shared secret embedded in the webhook URL path
    # (POST /webhook/monday/<secret>). Monday's `create_webhook` mutation
    # produces webhooks that are NOT JWT-signed — JWT signing only applies
    # to Monday Apps Framework integration recipes. So we use a URL-path
    # secret as the primary auth. Generate with: `openssl rand -hex 32`.
    # TODO: when we migrate to Monday Apps Framework, replace with JWT
    # verification via the app Signing Secret.
    monday_webhook_secret: str = Field(
        "", alias="MONDAY_WEBHOOK_SECRET",
        description="Shared secret embedded in the Monday webhook URL path",
    )

    # ── Engine identity (for echo filtering) ─────────────────────────────
    # Monday webhook payloads include `userId` — the user that triggered
    # the change. All engine writes go through MONDAY_GRAYSPACE_TOKEN
    # (bound to a service user, e.g. "Gray Space Force"), so we filter
    # echoes by matching webhook.userId against the engine's user id.
    # If left blank, detected at startup via `{ me { id } }`.
    engine_monday_user_id: str = Field(
        "", alias="ENGINE_MONDAY_USER_ID",
        description="Monday user id the engine writes as. Auto-detected at startup if blank.",
    )

    # ── Monitoring (optional) ────────────────────────────────────────────
    sentry_dsn: str = ""

    # ── Phase 2 validator: Nexiuum board IDs are all-or-nothing ──────────
    @model_validator(mode="after")
    def _validate_nexiuum_config(self) -> "Settings":
        """Opt-in to Nexiuum dual-instance mode by setting board IDs.

        Board IDs (not the token) are the opt-in signal because
        `MONDAY_NEXIUUM_TOKEN` is sourced from `~/.monday_tokens` on Josh's
        shell for many other tools — its presence in env is not a Phase 2
        intent signal. Board IDs default to 0 (unset); any non-zero value
        means "the operator deliberately configured Phase 2 reads."

        Rules:
        - If ANY Nexiuum board ID > 0, ALL three (cap engine, recipe,
          schedule) must be > 0 AND the Nexiuum token must be non-empty.
        - If all board IDs are 0, the token is ignored (single-instance
          Phase 1 mode regardless of token presence).
        """
        cap_set = self.nexiuum_capacity_engine_board > 0
        rec_set = self.nexiuum_process_recipe_board > 0
        sch_set = self.nexiuum_schedule_board > 0
        flags = {
            "NEXIUUM_CAPACITY_ENGINE_BOARD": cap_set,
            "NEXIUUM_PROCESS_RECIPE_BOARD": rec_set,
            "NEXIUUM_SCHEDULE_BOARD": sch_set,
        }
        set_count = sum(flags.values())

        # No opt-in: ignore token entirely.
        if set_count == 0:
            return self

        # Partial opt-in: some boards set, others unset.
        if set_count < len(flags):
            set_names = [k for k, v in flags.items() if v]
            unset_names = [k for k, v in flags.items() if not v]
            raise ValueError(
                "Partial Nexiuum board config: "
                f"set={set_names}, unset={unset_names}. "
                "Set all three Nexiuum board IDs or leave all at 0."
            )

        # All boards set but no token: can't read them.
        if not self.nexiuum_monday_token:
            raise ValueError(
                "Nexiuum board IDs are set but MONDAY_NEXIUUM_TOKEN is empty. "
                "Set the token to enable Phase 2 dual-instance reads."
            )
        return self

    @property
    def nexiuum_enabled(self) -> bool:
        """True iff all Nexiuum-side config is populated (token + 3 boards)."""
        return (
            bool(self.nexiuum_monday_token)
            and self.nexiuum_capacity_engine_board > 0
            and self.nexiuum_process_recipe_board > 0
            and self.nexiuum_schedule_board > 0
        )

    # ── Per-instance column maps ─────────────────────────────────────────
    # Parsers call these to pick the right column-id set for the instance
    # the item came from. Keeps the parsers instance-agnostic — they just
    # consume a CapacityEngineCols / ProcessRecipeCols / ScheduleCols.

    def cap_cols(self, instance: Literal["gray_space", "nexiuum"]) -> CapacityEngineCols:
        if instance == "nexiuum":
            return CapacityEngineCols(
                status=self.col_nx_cap_status,
                capacity=self.col_nx_cap_capacity,
                hours_per_day=self.col_nx_cap_hours_per_day,
                window_start=self.col_nx_cap_window_start,
                window_end=self.col_nx_cap_window_end,
                changeover=self.col_nx_cap_changeover,
                process_group=self.col_nx_cap_process_group,
                dual_sided=self.col_nx_cap_dual_sided,
                max_job_size=self.col_nx_cap_max_job_size,
                force_route=self.col_nx_cap_force_route,
                last_job_ended_at=self.col_nx_cap_last_job_ended_at,
                notes=self.col_nx_cap_notes,
            )
        return CapacityEngineCols(
            status=self.col_cap_status,
            capacity=self.col_cap_capacity,
            hours_per_day=self.col_cap_hours_per_day,
            window_start=self.col_cap_window_start,
            window_end=self.col_cap_window_end,
            changeover=self.col_cap_changeover,
            process_group=self.col_cap_process_group,
            dual_sided=self.col_cap_dual_sided,
            max_job_size=self.col_cap_max_job_size,
            force_route=self.col_cap_force_route,
            last_job_ended_at=self.col_cap_last_job_ended_at,
            notes=self.col_cap_notes,
        )

    def recipe_cols(self, instance: Literal["gray_space", "nexiuum"]) -> ProcessRecipeCols:
        if instance == "nexiuum":
            return ProcessRecipeCols(
                key=self.col_nx_recipe_key,
                version=self.col_nx_recipe_version,
                status=self.col_nx_recipe_status,
                stages=self.col_nx_recipe_stages,
            )
        return ProcessRecipeCols(
            key=self.col_recipe_key,
            version=self.col_recipe_version,
            status=self.col_recipe_status,
            stages=self.col_recipe_stages,
        )

    def schedule_cols(self, instance: Literal["gray_space", "nexiuum"]) -> ScheduleCols:
        if instance == "nexiuum":
            return ScheduleCols(
                machine=self.col_nx_schedule_machine,
                job_reference=self.col_nx_schedule_job_reference,
                stage_id=self.col_nx_schedule_stage_id,
                recipe_key=self.col_nx_schedule_recipe_key,
                recipe_version=self.col_nx_schedule_recipe_version,
                quantity=self.col_nx_schedule_quantity,
                capacity_mirror=self.col_nx_schedule_capacity_mirror,
                duration_formula=self.col_nx_schedule_duration_formula,
                planned_start=self.col_nx_schedule_planned_start,
                planned_end=self.col_nx_schedule_planned_end,
                actual_start=self.col_nx_schedule_actual_start,
                actual_end=self.col_nx_schedule_actual_end,
                dependent_on=self.col_nx_schedule_dependent_on,
                status=self.col_nx_schedule_status,
                manually_placed=self.col_nx_schedule_manually_placed,
                priority=self.col_nx_schedule_priority,
                last_reflow_hash=self.col_nx_schedule_last_reflow_hash,
                drift_last_detected_at=self.col_nx_schedule_drift_last_detected_at,
                n_number=self.col_nx_schedule_n_number,
                flavor=self.col_nx_schedule_flavor,
            )
        return ScheduleCols(
            machine=self.col_schedule_machine,
            job_reference=self.col_schedule_job_reference,
            stage_id=self.col_schedule_stage_id,
            recipe_key=self.col_schedule_recipe_key,
            recipe_version=self.col_schedule_recipe_version,
            quantity=self.col_schedule_quantity,
            capacity_mirror=self.col_schedule_capacity_mirror,
            duration_formula=self.col_schedule_duration_formula,
            planned_start=self.col_schedule_planned_start,
            planned_end=self.col_schedule_planned_end,
            actual_start=self.col_schedule_actual_start,
            actual_end=self.col_schedule_actual_end,
            dependent_on=self.col_schedule_dependent_on,
            status=self.col_schedule_status,
            manually_placed=self.col_schedule_manually_placed,
            priority=self.col_schedule_priority,
            last_reflow_hash=self.col_schedule_last_reflow_hash,
            drift_last_detected_at=self.col_schedule_drift_last_detected_at,
            n_number=self.col_schedule_n_number,
            flavor=self.col_schedule_flavor,
        )

    def schedule_board(self, instance: Literal["gray_space", "nexiuum"]) -> int:
        """Schedule board id for the given instance."""
        return (
            self.nexiuum_schedule_board if instance == "nexiuum"
            else self.gray_space_schedule_board
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy singleton — avoids parsing env at import time."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def reset_settings_for_tests() -> None:
    """Test helper — clear the cached Settings so the next `get_settings()`
    call re-reads the environment.

    Without this, tests that monkeypatch env vars (e.g. tweaking
    `MONDAY_WEBHOOK_SECRET` or `drift_threshold_minutes`) leak the
    first-loaded values across test modules.
    """
    global _settings
    _settings = None
