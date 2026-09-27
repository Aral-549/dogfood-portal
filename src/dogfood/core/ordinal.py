"""Ranking from judges' orderings alone (Bradley-Terry). Advisory; never the published ranking.

Research (RESEARCH.md): Gavel (HackMIT, Crowd-BT), MadHacks (Plackett-Luce) and MLH's
stack-ranking advice all rank from comparisons instead of absolute scores, because an ordering
does not depend on how generous a judge is. Rubric scores already contain each judge's ordering,
so this needs no extra work from judges: every pair of projects one judge scored becomes a
comparison (higher weighted score wins, equal scores are half a win each), and a Bradley-Terry
model is fitted to all comparisons with Hunter's MM algorithm (Ann. Statist. 2004).

A weak prior keeps the fit finite for projects that never lost (or never won): each project also
plays one tie against a fixed virtual opponent of strength 1.

Pure: no database, no clock.
"""

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable

from .scoring import ScoredReview

MAX_ITER = 1000
TOL = 1e-12


@dataclass(frozen=True)
class OrdinalResult:
    strengths: dict[str, float]      # Bradley-Terry strength per compared project (virtual opponent = 1)
    ranks: dict[str, int]            # every project: compared ones by strength, then the rest by id
    comparisons: int                 # judge-level pairwise comparisons used


def comparisons(scored: Iterable[ScoredReview]) -> list[tuple[str, str, float]]:
    """(a, b, wins of a over b in [0, 1]) for every pair of projects scored by the same judge."""
    by_judge: dict[str, list[ScoredReview]] = {}
    for s in scored:
        by_judge.setdefault(s.judge, []).append(s)
    out = []
    for judge in sorted(by_judge):
        rows = sorted(by_judge[judge], key=lambda s: s.project)
        for x, y in combinations(rows, 2):
            rx, ry = round(x.r, 12), round(y.r, 12)
            out.append((x.project, y.project, 1.0 if rx > ry else 0.0 if rx < ry else 0.5))
    return out


def bradley_terry(scored: Iterable[ScoredReview], projects: Iterable[str]) -> OrdinalResult:
    project_ids = list(dict.fromkeys(projects))
    known = set(project_ids)
    pairs = [(a, b, w) for a, b, w in comparisons(s for s in scored if s.project in known)]
    items = sorted({p for a, b, _ in pairs for p in (a, b)})
    wins = {p: 0.5 for p in items}                       # the prior tie against the virtual opponent
    games: dict[str, dict[str, float]] = {p: {} for p in items}
    for a, b, w in pairs:
        wins[a] += w
        wins[b] += 1.0 - w
        games[a][b] = games[a].get(b, 0.0) + 1.0
        games[b][a] = games[b].get(a, 0.0) + 1.0
    strength = {p: 1.0 for p in items}
    for _ in range(MAX_ITER):
        new = {}
        for p in items:
            denom = 1.0 / (strength[p] + 1.0)             # the virtual opponent
            for q, n in games[p].items():
                denom += n / (strength[p] + strength[q])
            new[p] = wins[p] / denom
        delta = max((abs(new[p] - strength[p]) for p in items), default=0.0)
        strength = new
        if delta < TOL:
            break
    order = sorted(items, key=lambda p: (-round(strength[p], 9), p))
    ranks = {p: i for i, p in enumerate(order, 1)}
    for i, p in enumerate(sorted(p for p in project_ids if p not in ranks), len(order) + 1):
        ranks[p] = i
    return OrdinalResult(strength, ranks, len(pairs))
