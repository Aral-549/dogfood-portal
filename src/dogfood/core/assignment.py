"""Judge-to-project assignment. See contracts/lifecycle.md case 14.

Deterministic: same input, same output. Pure; the caller persists the result.
"""

from typing import Iterable, Mapping


def auto_assign(
    projects: Iterable[tuple[str, str | None]],          # (project_id, track_id)
    judge_tracks: Mapping[str, set[str]],                # judge_id -> tracks
    existing: Iterable[tuple[str, str]],                 # (judge_id, project_id)
    conflicts: set[tuple[str, str]],                     # (judge_id, project_id) that must never pair
    k: int,
) -> list[tuple[str, str]]:
    """Return NEW (judge_id, project_id) pairs so each project has up to k judges.

    Candidates are judges covering the project's track (any judge if the project has no
    track or nobody covers it), lowest current load first, ties by judge id.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    existing = set(existing)
    load = {j: 0 for j in judge_tracks}
    per_project: dict[str, set[str]] = {}
    for j, p in existing:
        load[j] = load.get(j, 0) + 1
        per_project.setdefault(p, set()).add(j)
    new: list[tuple[str, str]] = []
    for pid, track in sorted(projects, key=lambda x: x[0]):
        have = per_project.setdefault(pid, set())
        pool = [j for j, ts in judge_tracks.items() if track in ts] if track else []
        if not pool:
            pool = list(judge_tracks)
        pool = [j for j in pool if j not in have and (j, pid) not in conflicts]
        while len(have) < k and pool:
            pool.sort(key=lambda j: (load.get(j, 0), j))
            j = pool.pop(0)
            have.add(j)
            load[j] = load.get(j, 0) + 1
            new.append((j, pid))
    return new
