"""Results CSV. See contracts/csv-export.md."""

import csv
import io
from typing import Iterable, Mapping

from .scoring import ProjectResult

COLUMNS = ["rank", "project_id", "title", "team", "track", "n_reviews",
           "raw_mean", "normalized_z", "low_confidence"]
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: str) -> str:
    """Neutralize spreadsheet formula injection."""
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def _num(x: float | None) -> str:
    if x is None:
        return ""
    text = f"{x:.4f}"
    return "0.0000" if text == "-0.0000" else text


def results_csv(results: Iterable[ProjectResult], meta: Mapping[str, Mapping[str, str]]) -> str:
    """meta[project_id] = {"title", "team", "track"}."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(COLUMNS)
    for r in results:
        m = meta.get(r.project, {})
        w.writerow([
            r.rank, r.project,
            safe_cell(m.get("title", "")), safe_cell(m.get("team", "")), safe_cell(m.get("track", "")),
            r.n_reviews, _num(r.raw_mean), _num(r.z), "true" if r.low_confidence else "false",
        ])
    return buf.getvalue()
