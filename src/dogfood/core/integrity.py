"""Pre-announcement integrity checks. Advisory: flags for a human, never automatic penalties.

Research (RESEARCH.md): MLH's organizer guide says to run "a cheating check on all winners
before you announce"; plagiarism tools for hackathons fingerprint submissions and compare them
across teams. Checking repository history needs the network, which the portal must not assume
(offline rule), so these checks use only what the portal holds:

- shared_repo:   two different teams submitted the same repository (normalized URL)
- similar_text:  two different teams' title + summary are near-identical (word 3-shingle Jaccard
                 >= SIMILAR_JACCARD, only for texts with at least MIN_SHINGLES shingles, so
                 boilerplate one-liners do not all match each other)
- no_repo:       nothing to verify originality against

Pure: no database, no clock.
"""

import re
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable
from urllib.parse import urlsplit

SIMILAR_JACCARD = 0.6
MIN_SHINGLES = 8


@dataclass(frozen=True)
class Flag:
    project: str
    kind: str          # shared_repo | similar_text | no_repo
    other: str | None  # the other project, for pairwise flags
    detail: str


def normalize_repo(url: str) -> str:
    """https://www.GitHub.com/Org/Repo.git/ -> github.com/org/repo (empty if not a URL)."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/").removesuffix(".git").lower()
    return f"{host}{path}" if host else ""


def shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def check(projects: Iterable[dict]) -> list[Flag]:
    """projects: dicts with id, team, title, summary, repo_url (canonical submitted projects)."""
    rows = sorted(projects, key=lambda p: p["id"])
    flags: list[Flag] = []
    repos = {p["id"]: normalize_repo(p.get("repo_url") or "") for p in rows}
    texts = {p["id"]: shingles(f"{p.get('title') or ''} {p.get('summary') or ''}") for p in rows}
    for p in rows:
        if not repos[p["id"]]:
            flags.append(Flag(p["id"], "no_repo", None, "no repository link to check originality against"))
    for a, b in combinations(rows, 2):
        if a["team"] == b["team"]:
            continue  # same-team duplicates are merged by the importer (superseded_by), not flagged
        if repos[a["id"]] and repos[a["id"]] == repos[b["id"]]:
            for x, y in ((a, b), (b, a)):
                flags.append(Flag(x["id"], "shared_repo", y["id"], f"same repository as {y['id']}: {repos[x['id']]}"))
        ta, tb = texts[a["id"]], texts[b["id"]]
        if min(len(ta), len(tb)) >= MIN_SHINGLES:
            sim = jaccard(ta, tb)
            if sim >= SIMILAR_JACCARD:
                for x, y in ((a, b), (b, a)):
                    flags.append(Flag(x["id"], "similar_text", y["id"],
                                      f"title and summary {sim:.0%} similar to {y['id']}"))
    return sorted(flags, key=lambda f: (f.project, f.kind, f.other or ""))
