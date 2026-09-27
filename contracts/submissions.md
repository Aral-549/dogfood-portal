# Contract: core/deadline + project submission

## Purpose
Decides whether a participant may create or edit a submission at time `now`,
and creates/edits drafts. Authentication is handed to `core/authz`; display is
handed to the gallery.

## Inputs
- `actor` from authz
- `event.submissions_close`: ISO 8601 UTC timestamp
- `now`: UTC datetime, injected (never read inside core)
- body: `{title: str, summary: str, repo_url?: str, track_id?: str, draft?: bool}` as JSON or form

## Outputs
- `201 {project}` on create, `200 {project}` on edit
- or an error `{error: <code>}` with status as below

## Rule
Open iff `now < submissions_close` (strict). At exactly the close instant it is closed.

## Behavior cases (input -> expected output)
| # | Actor | now | Body | Expected | Notes |
|---|-------|-----|------|----------|-------|
| 1 | participant in tm_01 | close - 1s | valid | 201, project owned by tm_01, `status=submitted` | |
| 2 | participant in tm_01 | close exactly | valid | 403 `submissions_closed` | boundary |
| 3 | participant in tm_01 | close + 1s | valid | 403 `submissions_closed` | |
| 4 | participant in tm_01 | real clock (2026-09-27, fixture closed 2026-03-01) | checker probe body | 403 `submissions_closed` | acceptance check 3 |
| 5 | participant in tm_01 | after close | invalid body (empty title) | 403 `submissions_closed` | deadline is checked before validation, so the refusal reason is honest and stable |
| 6 | visitor | before close | valid | 401 | |
| 7 | judge (no team) | before close | valid | 403 `not_a_participant` | |
| 8 | participant with no team | before close | valid | 403 `no_team` | |
| 9 | participant in tm_01 | before close | `draft: true` | 201, `status=draft`, not in gallery | |
| 10 | participant in tm_01 | before close | edit own draft -> submitted | 200 | |
| 11 | participant in tm_02 | before close | edit tm_01's project | 403 | ownership check in backend |
| 12 | participant in tm_01 | after close | edit own existing project | 403 `submissions_closed` | edits also freeze |
| 13 | participant in tm_01 | before close | empty/whitespace title, or title > 200 chars | 422 | |
| 14 | organizer | after close | edit any project | 403 | organizers do not bypass the deadline through this route; they extend the deadline explicitly (audited) instead |

## Edge cases that must be covered
- Close timestamp with `Z` vs `+00:00` parse to the same instant.
- A naive (no timezone) `now` passed into core raises, rather than being silently treated as local time.
- Server running with `TZ=America/Los_Angeles` gives identical decisions.
- Title containing HTML (`<script>`) is stored verbatim and escaped on output.
- Body sent as `application/json` (checker) and as form data (HTML form) behave the same.

## Explicitly out of scope
- Team formation and invites (next batch).
- Deadline extension UI (next batch; must write an audit entry).

## Status
- [x] Drafted
- [ ] Reviewed by a human
- [ ] Implementation matches this contract
- [ ] Golden tests exist for every behavior case above
