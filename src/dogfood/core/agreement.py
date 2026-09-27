"""Judge agreement, outlier and favoritism report. See contracts/judge-agreement.md.

Uses normalized per-review scores, so a harsh-but-consistent judge is not flagged.
Pure; the organizer decides what to do with it.
"""

from dataclasses import dataclass
from math import sqrt
from typing import Iterable

from .scoring import ScoredReview

MIN_SHARED = 3
FAVORITISM_GAP = 2.0


@dataclass(frozen=True)
class JudgeAgreement:
    judge: str
    shared: int
    agreement: float | None
    mean_abs_dev: float | None
    status: str  # uninformative | insufficient | outlier | ok


@dataclass(frozen=True)
class FavoritismFlag:
    judge: str
    project: str
    gap: float


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 1e-24 or syy <= 1e-24:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sqrt(sxx * syy)


def judge_agreement(scored: Iterable[ScoredReview],
                    informative: set[str]) -> tuple[list[JudgeAgreement], list[FavoritismFlag]]:
    scored = list(scored)
    by_project: dict[str, list[ScoredReview]] = {}
    for s in scored:
        by_project.setdefault(s.project, []).append(s)
    pairs: dict[str, list[tuple[float, float]]] = {}
    flags = []
    for s in scored:
        others = [o.z for o in by_project[s.project] if o is not s]
        pairs.setdefault(s.judge, [])
        if not others:
            continue
        consensus = sum(others) / len(others)
        pairs[s.judge].append((s.z, consensus))
        if s.z - consensus >= FAVORITISM_GAP - 1e-12:
            flags.append(FavoritismFlag(s.judge, s.project, round(s.z - consensus, 3)))

    report = []
    for judge in sorted(pairs):
        ps = pairs[judge]
        shared = len(ps)
        mad = round(sum(abs(z - c) for z, c in ps) / shared, 3) if shared else None
        if judge not in informative:
            report.append(JudgeAgreement(judge, shared, None, mad, "uninformative"))
            continue
        r = _pearson([z for z, _ in ps], [c for _, c in ps]) if shared >= MIN_SHARED else None
        if r is None:
            report.append(JudgeAgreement(judge, shared, None, mad, "insufficient"))
        else:
            report.append(JudgeAgreement(judge, shared, round(r, 3), mad, "outlier" if r < 0 else "ok"))
    flags.sort(key=lambda f: (-f.gap, f.judge, f.project))
    return report, flags
