"""Judging capacity planning. Pure arithmetic; advisory.

Research (RESEARCH.md): the MLH organizer guide sizes expo judging as
    judges needed J = ceil(P * n * t / T)
for P projects, n reviews per project (MLH recommends 3), t minutes per review (MLH: 4 = 2 min
demo + 1 min questions and scoring + 1 min moving on) and T minutes of judging time. Organizers
report judging as their biggest pain point, and judges dropping out on the day as the usual way
it goes wrong, so the dashboard shows the plan and the gap before the day, not after.
"""

from dataclasses import dataclass
from math import ceil

REVIEWS_PER_PROJECT = 3
MINUTES_PER_REVIEW = 4


@dataclass(frozen=True)
class Plan:
    projects: int
    judges: int
    reviews_per_project: int
    minutes_per_review: int
    reviews_needed: int              # P * n
    reviews_done: int
    per_judge: int                   # reviews each judge must do with the current panel (ceil)
    minutes_per_judge: int           # per_judge * t
    judges_needed_for: dict[int, int]  # judging window (minutes) -> judges needed


def plan(projects: int, judges: int, reviews_done: int = 0, n: int = REVIEWS_PER_PROJECT,
         t: int = MINUTES_PER_REVIEW, windows: tuple[int, ...] = (60, 120, 180)) -> Plan:
    if projects < 0 or judges < 0 or n < 1 or t < 1:
        raise ValueError("counts must be non-negative, n and t positive")
    needed = projects * n
    per_judge = ceil(needed / judges) if judges else 0
    return Plan(projects, judges, n, t, needed, reviews_done, per_judge, per_judge * t,
                {w: ceil(needed * t / w) for w in windows})
