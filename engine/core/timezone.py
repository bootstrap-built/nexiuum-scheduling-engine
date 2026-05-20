"""Timezone conversion between local factory time and UTC.

Monday's Date columns store time as UTC and display in the viewer's account
timezone. The engine works internally in local time (factory clock). At every
boundary to Monday Date columns, convert local → UTC. At every read, convert
UTC → local.

Uses Python's `zoneinfo` so DST transitions are handled automatically.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")


def local_tz(name: str) -> ZoneInfo:
    """Resolve a timezone name to a ZoneInfo. Cheap; ZoneInfo is cached."""
    return ZoneInfo(name)


def local_to_monday(local_dt: datetime, tz_name: str) -> dict[str, str]:
    """Convert a naive-or-aware local datetime to Monday's date+time payload.

    Monday's API expects:
        {"date": "YYYY-MM-DD", "time": "HH:MM:SS"}

    interpreted as UTC. So we localize (if naive) and convert to UTC.
    """
    tz = local_tz(tz_name)
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=tz)
    utc = local_dt.astimezone(UTC)
    return {
        "date": utc.strftime("%Y-%m-%d"),
        "time": utc.strftime("%H:%M:%S"),
    }


def monday_to_local(date_payload: dict[str, str] | None, tz_name: str) -> datetime | None:
    """Convert a Monday Date column read back into a local datetime.

    Accepts the API shape `{"date": "YYYY-MM-DD", "time": "HH:MM"}` (note
    that the API may return time as either "HH:MM" or "HH:MM:SS").

    Returns None if the column is empty.
    """
    if not date_payload or not date_payload.get("date"):
        return None
    date_str = date_payload["date"]
    time_str = date_payload.get("time") or "00:00:00"
    # Normalize HH:MM → HH:MM:SS
    parts = time_str.split(":")
    if len(parts) == 2:
        time_str = f"{time_str}:00"
    utc = datetime.fromisoformat(f"{date_str}T{time_str}").replace(tzinfo=UTC)
    return utc.astimezone(local_tz(tz_name))


def now_local(tz_name: str) -> datetime:
    """Current time in factory-local timezone."""
    return datetime.now(UTC).astimezone(local_tz(tz_name))
