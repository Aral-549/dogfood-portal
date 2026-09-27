"""UTC timestamp handling shared by every core module."""

from datetime import datetime, timezone


def parse_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp that carries a zone, returning it in UTC.

    Timestamps without a zone are rejected: guessing a zone is how deadlines drift.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"not a timestamp: {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    try:
        return dt.astimezone(timezone.utc)
    except OverflowError as e:
        raise ValueError(f"timestamp out of range: {value!r}") from e


def require_aware(dt: datetime, name: str = "datetime") -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got {dt!r}")
    return dt.astimezone(timezone.utc)


def format_utc(dt: datetime) -> str:
    """Canonical storage form: 2026-03-01T18:00:00Z."""
    d = require_aware(dt)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z"
