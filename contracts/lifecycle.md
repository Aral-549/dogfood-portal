# Contract: event lifecycle (login, registration, teams, events, judges, assignment, dashboard, publish)

## Purpose
The T1/T2 features the checker does not probe but the brief requires, so an
organizer can run one full event: create -> register -> team up -> submit ->
assign judges -> score -> normalize -> publish -> export. Every decision about
who may act goes through `core/authz`; this contract fixes the observable behaviour.

## Inputs
HTML forms (cookie session, same-origin only) and JSON API (Bearer token).

## Outputs
HTML pages / JSON, plus one `audit_log` row for every state-changing organizer or judge action.

## Behavior cases (input -> expected output)
| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | `POST /login` with correct email + password | 303 to `/me`, `session` cookie HttpOnly, SameSite=Lax | |
| 2 | `POST /login` wrong password / unknown email | 401, same message for both | no account enumeration |
| 3 | `POST /logout` | session row deleted; old token -> visitor | |
| 4 | `POST /register` new email + password (>= 8 chars) | user created, logged in | participant role comes from team membership |
| 5 | `POST /register` existing email | 409 | |
| 6 | participant with no team creates a team before close | team + invite link `/join/<code>` | |
| 7 | another user opens `/join/<code>` and confirms before close | becomes a member | |
| 8 | join after close, bad code, or already in a team for that event | 403 / 404 / 409 | |
| 9 | team size would exceed 4 | 409 | brief: teams of one to four |
| 10 | admin creates an event with name, submissions_close, judging_close, tracks, prizes | event exists, creator is organizer, audit row | |
| 11 | non-admin creates an event | 403 | |
| 12 | organizer changes submissions_close | saved, audit row with old and new value | the only way to reopen submissions |
| 13 | organizer invites judge by email + tracks | judge row for that event; a one-time set-password link ONLY if the account has no password yet AND is not a member of any team (BUG-23: the organizer must never be able to claim a participant's account); one transaction with its audit row | an invite can never reset an existing password (BUG-8) |
| 14 | organizer runs auto-assign with k=3 | each canonical submitted project gets up to k judges from its track, lowest-load first, never a judge who is on the project's team; existing assignments kept | deterministic for the same input |
| 15 | organizer sets rubric weights | saved; results recomputed; weights <= 0 rejected 422 | |
| 16 | judge submits a score for an assigned project, values 1..5 for every criterion, before judging_close | saved (upsert), audit row | |
| 17 | judge scores an unassigned project / after judging_close / value 6 | 403 / 403 / 422 | |
| 17a | judge scores a project of a team they are a member of | 403 `conflict_of_interest` | re-checked at scoring time, not only at assignment (BUG-12) |
| 18 | organizer dashboard | per-judge done/assigned, per-project review count, raw vs normalized ranking with rank change, duplicate and low-confidence flags, audit log | "live" = computed on each load |
| 19 | public `/results` before publish | 404-style "not published" page, no scores | |
| 20 | organizer publishes results | `/results` shows the ranking (no per-judge scores) | audit row |
| 21 | any state-changing form POST with a cross-site `Origin` | 403 | CSRF defence for cookie sessions |

## Edge cases that must be covered
- Emails compared case-insensitively.
- Password hashing with scrypt, per-user salt; demo accounts only when `DOGFOOD_DEMO_SESSIONS=1`. Booting with demo mode off clears the demo passwords, sessions, admin flag and organizer role (BUG-1).
- `POST /logout` revokes only the caller's cookie session, never a Bearer token.
- `GET /projects/new` after the close shows the refusal, not the form.
- Fixture judges (no password) can be given one via the organizer's set-password link.

## Explicitly out of scope
- Email delivery (links are shown to the organizer to pass on; offline rule).
- Eligibility rules beyond "submitted, canonical, before deadline" (next batch).
- T3/T4.

## Status
- [x] Drafted
- [ ] Reviewed by a human
- [ ] Implementation matches this contract
- [ ] Golden tests exist for every behavior case above
