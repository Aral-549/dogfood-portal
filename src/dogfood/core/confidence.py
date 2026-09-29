"""Prize-line confidence and tie-breaker assignment. See contracts/confidence.md.

Advisory only: never changes the published ranking. Pure; no database, no clock.
"""

import itertools
import random
from dataclasses import dataclass
from math import prod
from typing import Iterable, Mapping

from .scoring import ProjectResult, ScoredReview

EXACT_LIMIT = 20_000
CLOSE_LOW, CLOSE_HIGH = 0.2, 0.8


@dataclass(frozen=True)
class ProjectConfidence:
    project: str
    p_top_k: float
    close_call: bool
    unreviewed: bool


@dataclass(frozen=True)
class ConfidenceReport:
    k: int
    method: str  # "exact" | "monte_carlo"
    replicates: int
    projects: dict[str, ProjectConfidence]


def _check_k(k) -> int:
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError(f"k must be an integer >= 1, got {k!r}")
    return k


def _resample_means_reference(rng: random.Random, reviewed: list[str], zs: Mapping[str, list[float]]) -> dict:
    """The definition: per project in id order, n draws with replacement from its n values."""
    return {p: sum(rng.choice(zs[p]) for _ in zs[p]) / len(zs[p]) for p in reviewed}


def _resample_means(rng: random.Random, reviewed: list[str], zs: Mapping[str, list[float]]) -> dict:
    """Same draws and same floats as _resample_means_reference, about 3x faster.

    random.Random.choice(v) is v[_randbelow(len(v))], and _randbelow(n) is getrandbits(n.bit_length())
    repeated until < n. Inlining that consumes the identical random stream; sum() over a list keeps
    the float arithmetic identical too. A test pins the equivalence, so a Python whose
    Random works differently fails loudly instead of changing published confidence silently.
    """
    getrandbits = rng.getrandbits
    means = {}
    for p in reviewed:
        values = zs[p]
        n = len(values)
        bits = n.bit_length()
        picks = []
        for _ in range(n):
            r = getrandbits(bits)
            while r >= n:
                r = getrandbits(bits)
            picks.append(values[r])
        means[p] = sum(picks) / n
    return means


def prize_confidence(scored: Iterable[ScoredReview], ranking: Iterable[ProjectResult], k: int,
                     seed: int = 0, replicates: int = 2000) -> ConfidenceReport:
    k = _check_k(k)
    ranking = list(ranking)
    published = {r.project: r.rank for r in ranking}
    zs: dict[str, list[float]] = {p: [] for p in published}
    for s in scored:
        if s.project in zs:
            zs[s.project].append(s.z)
    reviewed = sorted(p for p, v in zs.items() if v)
    wins = {p: 0 for p in reviewed}

    def tally(means: Mapping[str, float]) -> None:
        order = sorted(reviewed, key=lambda p: (-round(means[p], 12), published[p]))
        for p in order[:k]:
            wins[p] += 1

    space = prod(len(zs[p]) ** len(zs[p]) for p in reviewed) if reviewed else 1
    if space <= EXACT_LIMIT:
        method, total = "exact", space
        # Every equally likely draw: per project, all n^n ordered resamples of its n reviews.
        per_project = [[sum(draw) / len(draw) for draw in itertools.product(zs[p], repeat=len(zs[p]))]
                       for p in reviewed]
        for combo in itertools.product(*per_project):
            tally(dict(zip(reviewed, combo)))
    else:
        method, total = "monte_carlo", replicates
        rng = random.Random(seed)
        for _ in range(replicates):
            tally(_resample_means(rng, reviewed, zs))

    out = {}
    for p in published:
        if p in wins:
            prob = wins[p] / total
            shown = round(prob, 3)  # p is reported to 3 dp; the flag must agree with what is shown
            out[p] = ProjectConfidence(p, prob, CLOSE_LOW <= shown <= CLOSE_HIGH, False)
        else:
            out[p] = ProjectConfidence(p, 0.0, False, True)
    return ConfidenceReport(k, method, total, out)


def tiebreak_assign(
    close_calls: Iterable[tuple[str, float]],       # (project_id, p_top_k)
    project_tracks: Mapping[str, str | None],
    judge_tracks: Mapping[str, set[str]],
    assignments: Iterable[tuple[str, str]],         # existing (judge_id, project_id)
    reviewed_by: Mapping[str, set[str]],            # judge_id -> projects reviewed
    conflicts: set[tuple[str, str]],
    already_tiebroken: set[str],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (new (judge, project) pairs, close-call projects with no candidate)."""
    close = sorted(close_calls, key=lambda x: (abs(x[1] - 0.5), x[0]))
    close_ids = {p for p, _ in close}
    assigned: dict[str, set[str]] = {}
    load: dict[str, int] = {j: 0 for j in judge_tracks}
    for j, p in assignments:
        assigned.setdefault(p, set()).add(j)
        load[j] = load.get(j, 0) + 1
    new, stuck = [], []
    for pid, _ in close:
        if pid in already_tiebroken:
            continue
        track = project_tracks.get(pid)
        pool = [j for j, ts in judge_tracks.items() if track and track in ts] or list(judge_tracks)
        pool = [j for j in pool if j not in assigned.get(pid, set()) and (j, pid) not in conflicts]
        if not pool:
            stuck.append(pid)
            continue
        best = min(pool, key=lambda j: (-len((reviewed_by.get(j, set()) - {pid}) & close_ids), load.get(j, 0), j))
        new.append((best, pid))
        assigned.setdefault(pid, set()).add(best)
        load[best] = load.get(best, 0) + 1
    return new, stuck
