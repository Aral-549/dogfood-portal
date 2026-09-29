# Contract: CSV export at every stage

## Purpose
The brief's T2 list asks for "CSV export at every stage". The results CSV (`csv-export.md`) and
the confidence CSV (`confidence.md`) cover the end of the pipeline; this contract covers the
stages before and around it, so an organizer can take any stage into a spreadsheet without a
database client. Authorization and CSV formatting are reused, not redefined.

## Inputs
- `GET /api/v1/exports/{kind}.csv?event=<id>`, `kind` one of the names below
- the caller's credentials (organizer of the event, or admin)

## Outputs
`text/csv; charset=utf-8`, RFC 4180 (`\r\n`, quoting as in `csv-export.md`), every text cell
passed through the same formula-injection guard as the results CSV,
`Content-Disposition: attachment; filename="<kind>-<event_id>.csv"`.

| kind | Columns, in order | One row per |
|---|---|---|
| `projects` | `project_id,title,team_id,team,track,status,submitted_at,superseded_by,eligible,repo_url` | project of the event (drafts included, duplicates included) |
| `teams` | `team_id,team,member_email` | team membership |
| `judges` | `judge_id,name,email,tracks,assigned,reviewed,excluded` | judge of the event (`tracks` joined with `;`) |
| `assignments` | `judge_id,project_id,reviewed` | assignment |
| `reviews` | `judge_id,project_id,<each rubric criterion>,comment,updated_at` | review, raw values, before any normalization |
| `votes` | `voter_email,project_id,at,voided` | vote; only after voting has closed |
| `audit` | `at,actor,action,subject,detail` | audit row, newest first, all of them |

## Behavior cases (input -> expected output)
| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | organizer, `projects`, fixture event | 200, header as above, 41 data rows; `prj_07` has `superseded_by` = `prj_41` | 40 canonical + the duplicate |
| 2 | organizer, `teams` | 200, 91 data rows | one per fixture member email |
| 3 | organizer, `judges` | 200, 30 data rows | |
| 4 | organizer, `assignments` | 200, 126 data rows, all `reviewed` = `true` | fixture assignments come from its scores |
| 5 | organizer, `reviews` | 200, 126 data rows; header criteria are `functionality,quality,innovation` in rubric order | raw fixture values, e.g. jdg_08 on prj_01 = 2,4,2 |
| 6 | organizer, `votes` while voting is open or not set up | 403 `results_hidden` | same rule as the tally (`t3-public.md` case 10) |
| 7 | organizer, `votes` after close | 200; one row per vote; `voided` true for voided voters; counts as showing a tally (the window becomes final) | `t3-public.md`, BUG-33 |
| 8 | organizer, `audit` while voting is open | 200; `vote.*` rows have actor and subject masked exactly as on the dashboard | BUG-31 |
| 9 | judge / participant / visitor, any kind | 403 / 403 / 401 | same as the results CSV |
| 10 | unknown `kind` | 404 | |
| 11 | a title starting with `=` | cell starts with `'` | same guard as `csv-export.md` case 5 |
| 12 | organizer of another event | 403 | exports are per event |

## Edge cases that must be covered
- An event with no projects: every kind returns its header line only, still 200.
- A comment containing commas, quotes and newlines stays inside one quoted field.
- Nothing here changes the existing `/api/export.csv` and `/api/confidence.csv` (their headers are pinned by tests).

## Explicitly out of scope
- Import from these CSVs (JSON export and import already round-trip an event: `t4-api-webhooks-bulk.md`).
- Per-judge exports for judges themselves.

## Where it shows up
The organizer page lists the seven exports as plain download links next to the existing results
and confidence CSVs.

## Status
Drafted, waiting for approval before implementation.
