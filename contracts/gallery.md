# Contract: public gallery

## Purpose
Public, unauthenticated listing of submitted projects with search and filter.
Never exposes scores, judges, drafts or member emails.

## Inputs
- query params: `q` (text search over title and summary), `track` (track id), `page` (default 1)

## Outputs
- `GET /projects`: HTML page. `GET /api/projects`: same data as JSON.

## Behavior cases (input -> expected output)
| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | no params, seeded fixtures | 200, all 40 canonical projects on one page, fixture order (prj_01 first) | page size 50, so check 2 of the checker is on page one |
| 2 | `q=harbour` | Dry Harbour (prj_41) only once | duplicate hidden |
| 3 | `track=trk_03` | only trk_03 projects | |
| 4 | `track=nope` | 200, empty list, no error | |
| 5 | draft project | not listed | |
| 6 | any page | contains no score, no judge name, no email | leak check via body grep in tests |
| 7 | `page=0`, `page=-1`, `page=abc` | treated as page 1 | |
| 8 | `q` containing `<script>` | echoed escaped | |

## Edge cases that must be covered
- `q` matching is case-insensitive and treats `%` and `_` literally (no SQL LIKE wildcards leak).
- Very long `q` (10k chars): 200, not 500.

## Explicitly out of scope
- Voting and comments (T3).
- Embeddable widget (T4).

## Status
- [x] Drafted
- [ ] Reviewed by a human
- [ ] Implementation matches this contract
- [ ] Golden tests exist for every behavior case above
