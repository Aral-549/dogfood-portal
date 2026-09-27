"""Authentication parsing and role-isolation decisions. See contracts/authz.md.

Handlers call these before loading data and act only on the returned Decision.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class Decision(Enum):
    ALLOW = 200
    UNAUTHENTICATED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404

    @property
    def allowed(self) -> bool:
        return self is Decision.ALLOW


@dataclass(frozen=True)
class Actor:
    """Who is asking. Built server-side from the session row only."""

    user_id: str | None = None
    is_admin: bool = False
    organizer_of: frozenset[str] = frozenset()
    judge_of: Mapping[str, str] = field(default_factory=dict)  # event_id -> judge_id
    team_of: Mapping[str, str] = field(default_factory=dict)  # event_id -> team_id

    @property
    def authenticated(self) -> bool:
        return self.user_id is not None

    def is_organizer(self, event_id: str) -> bool:
        return self.is_admin or event_id in self.organizer_of

    def judge_id(self, event_id: str) -> str | None:
        return self.judge_of.get(event_id)

    def team_id(self, event_id: str) -> str | None:
        return self.team_of.get(event_id)


VISITOR = Actor()


def extract_token(authorization: str | None, session_cookie: str | None) -> tuple[str | None, str | None]:
    """Return (token, source). Bearer wins over the cookie when both are present."""
    if authorization is not None:
        scheme, _, rest = authorization.strip().partition(" ")
        token = rest.strip()
        if scheme.lower() == "bearer" and token and " " not in token:
            return token, "bearer"
    if session_cookie is not None:
        token = session_cookie.strip()
        if token and " " not in token:
            return token, "cookie"
    return None, None


def _require_login(actor: Actor) -> Decision | None:
    return None if actor.authenticated else Decision.UNAUTHENTICATED


def read_own_scores(actor: Actor, event_id: str) -> Decision:
    """The caller's own scores. Data must then be filtered by actor.judge_id(event_id)."""
    if (d := _require_login(actor)) is not None:
        return d
    return Decision.ALLOW if actor.judge_id(event_id) else Decision.FORBIDDEN


def read_judge_scores(actor: Actor, event_id: str, target_judge_id: str, target_exists: bool) -> Decision:
    """Scores of a named judge. Judges may only name themselves."""
    if (d := _require_login(actor)) is not None:
        return d
    if actor.is_organizer(event_id):
        return Decision.ALLOW if target_exists else Decision.NOT_FOUND
    own = actor.judge_id(event_id)
    if own is not None and own == target_judge_id and target_exists:
        return Decision.ALLOW
    # A peer, a nonexistent judge and a non-judge all look the same: no enumeration.
    return Decision.FORBIDDEN


def export_results(actor: Actor, event_id: str) -> Decision:
    if (d := _require_login(actor)) is not None:
        return d
    return Decision.ALLOW if actor.is_organizer(event_id) else Decision.FORBIDDEN


def manage_event(actor: Actor, event_id: str) -> Decision:
    return export_results(actor, event_id)


def submit_project(actor: Actor, event_id: str, is_open: bool) -> tuple[Decision, str | None]:
    """Order matters: login, then deadline, then role. Deadline refusals stay honest."""
    if (d := _require_login(actor)) is not None:
        return d, "unauthenticated"
    if not is_open:
        return Decision.FORBIDDEN, "submissions_closed"
    if actor.team_id(event_id) is None:
        if actor.judge_id(event_id) or actor.is_organizer(event_id):
            return Decision.FORBIDDEN, "not_a_participant"
        return Decision.FORBIDDEN, "no_team"
    return Decision.ALLOW, None


def edit_project(actor: Actor, event_id: str, project_team_id: str, is_open: bool) -> tuple[Decision, str | None]:
    decision, reason = submit_project(actor, event_id, is_open)
    if not decision.allowed:
        return decision, reason
    if actor.team_id(event_id) != project_team_id:
        return Decision.FORBIDDEN, "not_your_project"
    return Decision.ALLOW, None


def score_project(actor: Actor, event_id: str, assigned: bool, judging_open: bool) -> tuple[Decision, str | None]:
    if (d := _require_login(actor)) is not None:
        return d, "unauthenticated"
    if actor.judge_id(event_id) is None:
        return Decision.FORBIDDEN, "not_a_judge"
    if not assigned:
        return Decision.FORBIDDEN, "not_assigned"
    if not judging_open:
        return Decision.FORBIDDEN, "judging_closed"
    return Decision.ALLOW, None
