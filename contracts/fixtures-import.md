# Contract: core/importer (fixtures.json -> database)

## Purpose
Loads a fixtures file into the database at boot, idempotently, and reports
what it did. It does not compute scores (scoring) or render anything.

## Inputs
- `fixtures.json` with keys `event, tracks, judges, teams, projects, scores`
- existing database (possibly already seeded)

## Outputs
- database rows for events, tracks, users, judges, teams, memberships, projects, scores
- an import report: counts per entity, list of rejected rows with reasons, list of flagged duplicates
- one structured log line per entity type at the importer -> db boundary

## Behavior cases (input -> expected output)
| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | upstream fixtures.json, empty db | 1 event, 8 tracks, 30 judges, 40 teams, 41 project rows, 126 scores, 0 rejected | |
| 2 | same file imported twice | identical row counts, no duplicates, report says `unchanged` | idempotent on fixture ids |
| 3 | event `submissions_close` | stored as `2026-03-01T18:00:00Z` exactly | not replaced by any "demo" date |
| 4 | team member emails | one user per distinct email, role participant, member of that team | 91 distinct member emails in upstream file |
| 5 | judge emails | one user per judge email, role judge, judge_id = fixture id, tracks linked | |
| 6 | `prj_07` and `prj_41` (same team `tm_07`, same title `Dry Harbour`, same repo_url) | both rows kept; `prj_41` (later `submitted_at`) is canonical; `prj_07.superseded_by = prj_41`; report lists the pair | detection: same team AND (same case-folded title OR same repo_url) |
| 7 | scores on the duplicate pair | raw review rows stay on their original project; at scoring time (`core/scoring.effective_reviews`) judges who scored both (jdg_19, jdg_21, jdg_26) count once, using their score on the canonical project; judges who scored only `prj_07` (jdg_01, jdg_12) carry over to the canonical project | merged set = 6 reviewers: jdg_19, jdg_21, jdg_26, jdg_01, jdg_12, jdg_18. Needs reviewer sign-off |
| 8 | gallery after import | shows `prj_41`, hides `prj_07` | |
| 9 | score with a criterion value outside 1..5, or non-integer | row rejected with reason, import continues | none in upstream file |
| 10 | score missing a rubric criterion | row rejected with reason, import continues | none in upstream file |
| 11 | score referencing unknown judge or project | row rejected with reason | |
| 12 | two score rows for the same (judge, project) | later row in file wins, earlier logged as replaced | none in upstream file |
| 13 | empty `comment` | stored as empty string, not null | 51 in upstream file |

## Edge cases that must be covered
- File missing, not JSON, or top-level not an object: boot fails loudly with the path and reason; portal does not start half-seeded.
- Missing optional top-level key (e.g. no `scores`): imports the rest, report notes it.
- Timestamps without `Z` / with offset: normalized to UTC; timestamps with no zone are rejected.
- Rubric criteria are derived from the fixture (`functionality, quality, innovation`), default weight 1 each.
- Whole import runs in one transaction.

## Explicitly out of scope
- Bulk import of arbitrary CSV (T4).
- Judge assignment beyond "a judge who has a score on a project is assigned to it" (assignment contract, next batch).

## Status
- [x] Drafted
- [ ] Reviewed by a human
- [ ] Implementation matches this contract
- [ ] Golden tests exist for every behavior case above
