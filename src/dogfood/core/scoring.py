"""Weighted rubric scoring and cross-judge normalization. See contracts/scoring.md.

Pure functions: no database, no clock.
"""

from dataclasses import dataclass
from math import isfinite, sqrt
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
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not w > 0 or not isfinite(w):
            raise ValueError(f"criterion {name!r} has invalid weight {w!r}")
    # Scale by the largest weight first so huge (finite) weights cannot overflow the sum.
    scale = float(max(criteria.values()))
    scaled = {name: w / scale for name, w in criteria.items()}
    total = sum(scaled.values())
    return {name: w / total for name, w in scaled.items()}


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


@dataclass(frozen=True)
class ScoredReview:
    """One review after normalization: raw weighted score r and weighted z."""
    judge: str
    project: str
    r: float
    z: float


def score_reviews(criteria: Mapping[str, float], reviews: Iterable[Review],
                  projects: Iterable[str]) -> tuple[list[ScoredReview], set[str]]:
    """Per-review raw and normalized scores, plus the judges informative on >= 1 criterion.

    Reviews of projects not in `projects` are ignored (callers log how many).
    """
    weights = normalize_weights(criteria)
    known = set(projects)
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
    informative = {j for j in by_judge if any(stats[(j, c)] is not None for c in weights)}

    scored = []
    for r in reviews:
        z_r = 0.0
        for c, w in weights.items():
            st = stats[(r.judge, c)]
            if st is not None:
                mu, sd = st
                z_r += w * (r.criteria[c] - mu) / sd
        scored.append(ScoredReview(r.judge, r.project, sum(w * r.criteria[c] for c, w in weights.items()), z_r))
    return scored, informative


def score(criteria: Mapping[str, float], reviews: Iterable[Review], projects: Iterable[str]) -> list[ProjectResult]:
    project_ids = list(dict.fromkeys(projects))
    scored, informative_judges = score_reviews(criteria, reviews, project_ids)

    raw: dict[str, list[float]] = {p: [] for p in project_ids}
    zs: dict[str, list[float]] = {p: [] for p in project_ids}
    informative: dict[str, int] = {p: 0 for p in project_ids}
    for s in scored:
        raw[s.project].append(s.r)
        zs[s.project].append(s.z)
        informative[s.project] += s.judge in informative_judges

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


SHRINK_PRIOR = 3  # pseudo-reviews pulling each judge's mean and spread toward everyone's


def shrunk_ranks(criteria: Mapping[str, float], reviews: Iterable[Review], projects: Iterable[str],
                 prior: float = SHRINK_PRIOR) -> dict[str, int]:
    """Advisory robustness check, never the published ranking (JUDGING.md, "Known limits").

    Same method as `score`, except each judge's per-criterion mean and variance are shrunk toward
    the pooled values of all reviews, weighted n : prior. A judge with 2 reviews then moves the
    ranking less than one with 11, and a judge who gave one score everywhere still counts a little.
    Projects whose rank differs a lot between this and the published ranking depend on noisy judges.
    """
    weights = normalize_weights(criteria)
    project_ids = list(dict.fromkeys(projects))
    known = set(project_ids)
    reviews = [r for r in reviews if r.project in known]
    by_judge: dict[str, list[Review]] = {}
    for r in reviews:
        by_judge.setdefault(r.judge, []).append(r)
    pooled = {}
    for c in weights:
        mu_g, sd_g = _mean_sd([float(r.criteria[c]) for r in reviews]) if reviews else (0.0, 0.0)
        pooled[c] = (mu_g, sd_g * sd_g)
    stats = {}
    for judge, rs in by_judge.items():
        n = len(rs)
        for c in weights:
            mu_j, sd_j = _mean_sd([float(r.criteria[c]) for r in rs])
            mu_g, var_g = pooled[c]
            mu = (n * mu_j + prior * mu_g) / (n + prior)
            # Pooled within-judge spread plus the spread of the judge mean around the pooled mean.
            var = (n * (sd_j * sd_j) + prior * var_g) / (n + prior)
            stats[(judge, c)] = (mu, sqrt(var)) if var > 0 else None
    zs: dict[str, list[float]] = {p: [] for p in project_ids}
    raw: dict[str, list[float]] = {p: [] for p in project_ids}
    for r in reviews:
        z = 0.0
        for c, w in weights.items():
            st = stats[(r.judge, c)]
            if st is not None:
                z += w * (r.criteria[c] - st[0]) / st[1]
        zs[r.project].append(z)
        raw[r.project].append(sum(w * r.criteria[c] for c, w in weights.items()))
    rows = [(p, sum(zs[p]) / len(zs[p]), sum(raw[p]) / len(raw[p])) for p in project_ids if zs[p]]
    rows.sort(key=lambda row: (-round(row[1], 12), -round(row[2], 12), row[0]))
    ranks = {row[0]: i for i, row in enumerate(rows, 1)}
    for i, p in enumerate(sorted(p for p in project_ids if not zs[p]), len(rows) + 1):
        ranks[p] = i
    return ranks


def without_projects(results: list[ProjectResult], drop: set[str]) -> list[ProjectResult]:
    """The ranking with `drop` removed and the rest renumbered, rank and raw rank alike.

    Used for projects ruled ineligible for prizes. Their reviews still count toward each judge's
    normalization (a judge's scale is measured over everything they judged), so removing one
    project never changes another project's z; only the positions close up.
    """
    if not drop:
        return results
    from dataclasses import replace
    kept = [r for r in results if r.project not in drop]
    raw_order = {r.project: i for i, r in enumerate(sorted(kept, key=lambda r: r.raw_rank), 1)}
    return [replace(r, rank=i, raw_rank=raw_order[r.project])
            for i, r in enumerate(sorted(kept, key=lambda r: r.rank), 1)]


def renumber(ranks: dict[str, int], drop: set[str]) -> dict[str, int]:
    """Same for a {project: rank} mapping (advisory rankings)."""
    kept = sorted((p for p in ranks if p not in drop), key=lambda p: (ranks[p], p))
    return {p: i for i, p in enumerate(kept, 1)}
