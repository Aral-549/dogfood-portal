"""Weighted rubric scoring and cross-judge normalization. See contracts/scoring.md.

Pure functions: no database, no clock.
"""

from dataclasses import dataclass
from math import sqrt
from typing import Iterable, Mapping

MIN_INFORMATIVE_REVIEWS = 2


@dataclass(frozen=True)
class Review:
    judge: str
    project: str
    criteria: Mapping[str, int]


@dataclass(frozen=True)
class ProjectResult:
    project: str
    n_reviews: int
    raw_mean: float | None
    z: float | None
    rank: int
    raw_rank: int
    low_confidence: bool


def normalize_weights(criteria: Mapping[str, float]) -> dict[str, float]:
    if not criteria:
        raise ValueError("rubric has no criteria")
    for name, w in criteria.items():
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not w > 0:
            raise ValueError(f"criterion {name!r} has invalid weight {w!r}")
    total = float(sum(criteria.values()))
    return {name: w / total for name, w in criteria.items()}


def effective_reviews(reviews: Iterable[Review], superseded_by: Mapping[str, str]) -> list[Review]:
    """Fold reviews of superseded projects into their canonical project.

    A judge who reviewed both keeps only the canonical review. Chains are followed.
    """
    def canonical(pid: str) -> str:
        seen = set()
        while pid in superseded_by and pid not in seen:
            seen.add(pid)
            pid = superseded_by[pid]
        return pid

    reviews = list(reviews)
    direct = {(r.judge, r.project) for r in reviews if r.project not in superseded_by}
    out: dict[tuple[str, str], Review] = {}
    for r in reviews:
        target = canonical(r.project)
        key = (r.judge, target)
        if r.project == target:
            out[key] = r
        elif key not in direct and key not in out:
            out[key] = Review(r.judge, target, r.criteria)
    return list(out.values())


def _mean_sd(values: list[float]) -> tuple[float, float]:
    mu = sum(values) / len(values)
    return mu, sqrt(sum((v - mu) ** 2 for v in values) / len(values))


def score(criteria: Mapping[str, float], reviews: Iterable[Review], projects: Iterable[str]) -> list[ProjectResult]:
    weights = normalize_weights(criteria)
    project_ids = list(dict.fromkeys(projects))
    known = set(project_ids)
    reviews = [r for r in reviews if r.project in known]
    for r in reviews:
        missing = set(weights) - set(r.criteria)
        if missing:
            raise ValueError(f"review {r.judge}/{r.project} missing criteria {sorted(missing)}")

    # Per-judge, per-criterion location and scale.
    by_judge: dict[str, list[Review]] = {}
    for r in reviews:
        by_judge.setdefault(r.judge, []).append(r)
    stats: dict[tuple[str, str], tuple[float, float] | None] = {}
    for judge, rs in by_judge.items():
        for c in weights:
            mu, sd = _mean_sd([float(r.criteria[c]) for r in rs])
            stats[(judge, c)] = (mu, sd) if len(rs) >= MIN_INFORMATIVE_REVIEWS and sd > 0 else None

    raw: dict[str, list[float]] = {p: [] for p in project_ids}
    zs: dict[str, list[float]] = {p: [] for p in project_ids}
    informative: dict[str, int] = {p: 0 for p in project_ids}
    for r in reviews:
        raw[r.project].append(sum(w * r.criteria[c] for c, w in weights.items()))
        z_r, any_info = 0.0, False
        for c, w in weights.items():
            st = stats[(r.judge, c)]
            if st is not None:
                mu, sd = st
                z_r += w * (r.criteria[c] - mu) / sd
                any_info = True
        zs[r.project].append(z_r)
        informative[r.project] += any_info

    rows = []
    for p in project_ids:
        n = len(raw[p])
        rows.append((p, n, sum(raw[p]) / n if n else None, sum(zs[p]) / n if n else None))

    def order(key_index: int):
        reviewed = [row for row in rows if row[1]]
        reviewed.sort(key=lambda row: (-round(row[key_index], 12), -round(row[2], 12), row[0]))
        return reviewed + sorted((row for row in rows if not row[1]), key=lambda row: row[0])

    rank = {row[0]: i for i, row in enumerate(order(3), 1)}
    raw_rank = {row[0]: i for i, row in enumerate(order(2), 1)}
    results = [
        ProjectResult(p, n, rm, z, rank[p], raw_rank[p], informative[p] < MIN_INFORMATIVE_REVIEWS)
        for p, n, rm, z in rows
    ]
    return sorted(results, key=lambda r: r.rank)
