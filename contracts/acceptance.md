# Contract: acceptance checker interface (`.dogfood.toml` + the seven checks)

## Purpose
Pins exactly what `run.py` (upstream, unmodified) sends and what our portal must
answer, so the committed `acceptance-report.txt` verifies T1 and T2. The
behaviour behind each answer is owned by the other contracts; this file only
fixes the external surface.

## Inputs
- `run.py`: upstream checker, byte-identical to https://dogfoodhack.com/spec/run.py
- `.dogfood.toml`: our file at repo root
- `fixtures.json`: upstream file, at repo root next to `run.py`
- A portal started with `docker compose up` and seeded from `fixtures.json`

## Outputs
- `acceptance-report.txt` = stdout of `python3 run.py .dogfood.toml`

## `.dogfood.toml` values (proposed)

| Key                  | Value                                   | Resolves to                            |
|----------------------|-----------------------------------------|----------------------------------------|
| `portal.base_url`    | `http://localhost:8080`                 |                                        |
| `tiers.claimed`      | `["T1", "T2", "T3", "T4"]`              | T1/T2 machine-verified; T3/T4 judged by hand per the kickoff brief (amended 2026-09-28) |
| `auth.organizer`     | `Authorization: Bearer demo-organizer`  | seeded organizer user                  |
| `auth.judge_a`       | `Authorization: Bearer demo-judge-a`    | `jdg_24` (11 fixture scores)           |
| `auth.judge_b`       | `Authorization: Bearer demo-judge-b`    | `jdg_26` (10 fixture scores)           |
| `auth.participant`   | `Authorization: Bearer demo-participant`| `priya1@example.org`, team `tm_01`     |
| `routes.gallery`     | `/projects`                             | public HTML gallery                    |
| `routes.submit`      | `/api/projects`                         | create submission                      |
| `routes.judge_scores`| `/api/judge/scores`                     | caller's own scores                    |
| `routes.peer_scores` | `/api/judges/jdg_24/scores`             | judge_a's scores, by id in the path    |
| `routes.csv_export`  | `/api/export.csv`                       | results CSV                            |

`peer_scores` MUST be a URL that really returns judge_a's scores when called by
someone allowed to (judge_a or organizer). A URL that 403s for everyone would
pass the check dishonestly. See `authz.md` cases 8-10.

## Behavior cases (input -> expected output)
| # | Request | Expected | Notes |
|---|---------|----------|-------|
| 1 | `GET /projects`, no auth | 200 | T1 gallery is public |
| 2 | same body | contains at least one of `Glass Signal`, `Small Meadow`, `Deep Compass` (case-insensitive) | first 3 fixture titles; must be on page one |
| 3 | `POST /api/projects` as participant, JSON `{"title":"dogfood-late-submission-probe","summary":"probe"}` | 403 with reason `submissions_closed` | refused because of the deadline, not because of auth or validation (see `submissions.md` case 4) |
| 4 | `GET /api/judge/scores` as judge_a | 200, JSON object `{"judge": "jdg_24", "scores": [...]}` with jdg_24's 11 scores only | amended 2026-09-27: was "JSON list". The conflicting golden test `test_case4_body_is_a_json_list` was removed with human approval (AGENTS.md rule 3); 9 other golden tests pin the object shape |
| 5 | `GET /api/judges/jdg_24/scores` as judge_b | 403 | the important one |
| 6 | `GET /api/judge/scores` as participant | 403 | |
| 7 | `GET /api/export.csv` as organizer | 200, first line is a comma-separated header | |
| 8 | Full `run.py` run | `claimed T1 T2 T3 T4, verified T1 T2`, then only `note: claimed but not verified: T3 T4` | amended 2026-09-28: run.py has no T3/T4 checks; the brief judges them by hand |

## Edge cases that must be covered
- `run.py` sends the header as `Name: value`, split on the first `:`. A Bearer
  value contains no extra colon; a `Cookie:` value would also work.
- Checker has a 10 s timeout per request; the portal must answer the gallery
  in well under that on a cold start (seeding finishes before the port opens).
- Portal restarted (`docker compose down && up`): report is identical.
- Fixture titles in the gallery must survive HTML escaping as the same text
  (none of the first three contain `&`, `<`, `'`).

## Explicitly out of scope
- Modifying `run.py`. We never patch the checker.
- Everything the checker does not probe (covered by our own tests).

## Status
Implemented. Tests: test_acceptance.py, test_checker_e2e.py (runs the real run.py). The design decisions in it were approved before implementation; the cases were not reviewed one by one.
