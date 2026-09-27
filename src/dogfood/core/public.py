"""T3 public participation rules. See contracts/t3-public.md.

Pure: no database, no clock. Callers pass `now` and plain data.
"""

import hashlib
from datetime import datetime

from .timeutil import parse_utc, require_aware

_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def voting_state(voting_open: str | None, voting_close: str | None, now: datetime) -> str:
    """'unset' | 'before' | 'open' | 'closed'. Window is half-open: open <= now < close."""
    if not voting_open or not voting_close:
        return "unset"
    now = require_aware(now, "now")
    if now < parse_utc(voting_open):
        return "before"
    return "open" if now < parse_utc(voting_close) else "closed"


def ballot_order(event_id: str, user_id: str, project_ids) -> list[str]:
    """Per-voter order: stable across reloads for one voter, different between voters."""
    def key(pid: str) -> str:
        return hashlib.sha256(f"{event_id}:{user_id}:{pid}".encode()).hexdigest()
    return sorted(dict.fromkeys(project_ids), key=key)


def normalize_email(email: str) -> str:
    """Canonical mailbox for duplicate detection. Gmail ignores dots and +tags."""
    local, _, domain = email.strip().lower().rpartition("@")
    if not local:
        return email.strip().lower()
    if domain in _GMAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def tally(votes, voided_voters: set[str]) -> list[dict]:
    """votes: iterable of (user_id, project_id). Voided voters' votes do not count.

    Sorted by votes desc, then project id.
    """
    counts: dict[str, int] = {}
    for user, project in votes:
        if user not in voided_voters:
            counts[project] = counts.get(project, 0) + 1
    return [{"project": p, "votes": n} for p, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
