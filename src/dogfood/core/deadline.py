"""Deadline enforcement. See contracts/submissions.md."""

from datetime import datetime

from .timeutil import require_aware


def submissions_open(submissions_close: datetime, now: datetime) -> bool:
    """Open iff now is strictly before the close instant."""
    close = require_aware(submissions_close, "submissions_close")
    return require_aware(now, "now") < close
