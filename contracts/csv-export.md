# Contract: core/csv (results export)

## Purpose
Serializes the scoring result for organizers as CSV. Authorization is decided
by authz before this runs; numbers come from core/scoring unchanged.

## Inputs
- scoring output (see `scoring.md`), project/team/track metadata

## Outputs
- `text/csv; charset=utf-8`, `Content-Disposition: attachment; filename="results-<event_id>.csv"`
- RFC 4180: comma separator, `\r\n` line endings, fields quoted when they contain `,` `"` or newlines

## Columns (in order)
`rank,project_id,title,team,track,n_reviews,raw_mean,normalized_z,low_confidence`

## Behavior cases (input -> expected output)
| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | upstream fixtures | header line exactly as above, then 40 rows ordered by rank | superseded `prj_07` absent |
| 2 | raw_mean 3.666666 | `3.6667` | 4 dp, fixed |
| 3 | project with zero reviews | `raw_mean` and `normalized_z` empty, `low_confidence` `true` | |
| 4 | title `Dry, Harbour "v2"` | `"Dry, Harbour ""v2"""` | quoting |
| 5 | title starting with `=`, `+`, `-`, `@`, tab or CR | cell prefixed with `'` | formula injection defence |
| 6 | title with non-ASCII (`Café`) | UTF-8 bytes, no BOM | |

## Edge cases that must be covered
- Title with an embedded newline stays inside one quoted field.
- Empty event (no projects): header line only, still 200.

## Explicitly out of scope
- Raw per-review export. The full event export in `t4-api-webhooks-bulk.md` covers it.

## Status
Implemented. Tests: test_csv_export.py. The design decisions in it were approved before implementation; the cases were not reviewed one by one.
