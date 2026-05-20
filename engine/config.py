"""Engine configuration loaded from environment variables.

All scheduling-relevant configuration lives here. Boards are referenced by ID
(not by name) because Monday board names can change but IDs cannot.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # ── Schedule board column IDs ────────────────────────────────────────
    # Captured from the board after creation. Hardcoded because Monday's API
    # requires column IDs (not titles) for mutations.
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

    # ── Process Recipe column IDs ────────────────────────────────────────
    col_recipe_key: str = "text_mm3hbfjj"
    col_recipe_version: str = "numeric_mm3heae"
    col_recipe_status: str = "color_mm3hfww4"
    col_recipe_stages: str = "long_text_mm3hxf7h"

    # ── Blend Records column IDs (source board for press actuals) ────────
    col_blend_status: str = "color_mm1mb9cm"  # "Blend Status" — flips to "Pressing" → actual_start

    # ── Timezone ─────────────────────────────────────────────────────────
    factory_tz: str = "America/Denver"

    # ── Polling sweep ────────────────────────────────────────────────────
    polling_interval_minutes: int = 15
    drift_threshold_minutes: int = 15
    drift_suppression_minutes: int = 60

    # ── Server ───────────────────────────────────────────────────────────
    port: int = 8002
    log_level: str = "INFO"

    # ── Monitoring (optional) ────────────────────────────────────────────
    sentry_dsn: str = ""


_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy singleton — avoids parsing env at import time."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
