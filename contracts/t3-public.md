# Contract: T3 public participation (voting, comments, hidden results, ballot order, anti-abuse)

## Purpose
Lets the public take part in an event: an authenticated community vote, comments
on projects, with tallies hidden until the voting window closes, per-voter random
ballot order, and abuse controls (rate limits, duplicate detection, audit trail).
It never changes the judged ranking (`core/scoring`); the community vote is a
separate result ("People's choice"). Authorization goes through `core/authz`.

## Inputs
- Event fields (new): `voting_open`, `voting_close` (ISO 8601 UTC, both or neither), `votes_per_voter` (int >= 1, default 1)
- Voter: any logged-in account (brief option "authenticated"). Registration is the gate.
- Comment body: text, 1..2000 chars after trimming

## Outputs
- `GET /vote` (HTML) / `GET /api/v1/ballot` (JSON): the voter's ballot
- `POST /api/v1/votes` `{project}`; `DELETE /api/v1/votes/{project}` (change of mind while open)
- `GET /api/v1/votes/results`: tallies, only after close
- `POST /projects/{id}/comments`, `DELETE /comments/{id}`
- `audit_log` rows: `vote.cast`, `vote.withdraw`, `comment.create`, `comment.delete`, `abuse.flag`

## Voting rules
- Open iff `voting_open <= now < voting_close` (half-open, like submissions).
- Eligible projects: submitted, canonical (not superseded) projects of the event.
- A voter may not vote for a project of their own team (`conflict_of_interest`).
- Judges of the event may not vote in it (they already score) (`judges_do_not_vote`).
- At most `votes_per_voter` votes per voter per event, at most 1 per project (DB unique).

## Behavior cases (input -> expected output)
| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | visitor `POST /api/v1/votes` | 401 | |
| 2 | participant of tm_01, window open, votes for prj_02 | 201, audit `vote.cast` | |
| 3 | same voter votes prj_02 again | 409 `already_voted` | per-project unique |
| 4 | votes_per_voter = 1, voter already voted prj_02, votes prj_03 | 409 `vote_limit` | |
| 5 | voter withdraws prj_02 while open, then votes prj_03 | 204, then 201 | |
| 6 | participant of tm_01 votes for tm_01's project | 403 `conflict_of_interest` | |
| 7 | judge of the event votes | 403 `judges_do_not_vote` | |
| 8 | vote at `now = voting_close` exactly, or before `voting_open` | 403 `voting_closed` | boundary, half-open |
| 9 | vote for superseded prj_07 or a draft | 404 `not_on_ballot` | |
| 10 | `GET /api/v1/votes/results` while open, as anyone including organizer | 403 `results_hidden` | nobody sees tallies early |
| 11 | same after close | 200, `[{project, votes}]` sorted by votes desc, then project id | |
| 12 | project pages, gallery, CSV, dashboard while open | contain no per-project vote count | only the total number of votes cast, on the organizer dashboard |
| 13 | ballot for voter U | every eligible project exactly once, order = sort by `sha256(event_id + ":" + user_id + ":" + project_id)` | stable across reloads for U, different across voters |
| 14 | two different voters, fixture event (40 projects) | orders differ | order is per voter, not global |
| 15 | comment by logged-in user on a submitted project | 201, shown on the project page, HTML escaped | |
| 16 | comment by visitor / empty / over 2000 chars | 401 / 422 / 422 | |
| 17 | comment deleted by its author or an organizer | 204, audit `comment.delete`; by anyone else 403 | |
| 18 | same user sends more than 10 votes+withdrawals or 5 comments in 60 s | 429 with `Retry-After` | per account, sliding window |
| 19 | more than 5 failed logins for one email from one client IP in 5 minutes (or 20 for one email from any IPs, or 50 from one IP over any emails) | 429 for that email+IP (resp. email, IP) for the rest of the window; a successful login clears that email+IP's count | slows password guessing without letting a stranger lock the owner out (amended 2026-09-27) |
| 20 | more than 3 registrations from one client IP in 1 hour | 4th and later accounts are created but flagged `abuse.flag` (`many_accounts_one_ip`) | flag, never block: shared IPs (campus NAT) are normal |
| 21 | registration whose normalized email equals an existing one (lowercase; for gmail.com/googlemail.com: dots removed and `+tag` stripped) | account created, flagged `abuse.flag` (`duplicate_email`) | `a.b+x@gmail.com` ~ `ab@gmail.com` |
| 22 | organizer dashboard | lists abuse flags with the accounts involved; organizer can void a flagged account's votes (audited `vote.void`, reversible) | voided votes do not count in case 11 |

## Edge cases that must be covered
- Window not configured: `/vote` explains voting is not open; no 500.
- `voting_close` before `voting_open`: 422 when the organizer saves it.
- Rate-limit state is in memory: a restart clears it (documented), limits never apply to the acceptance checker's 7 requests. Expired keys are swept, so memory stays bounded. `DOGFOOD_RATE_LIMITS=off` disables limits for load tests (flags stay on).
- Votes by someone who later becomes a judge of the event stop counting.
- Moving `voting_close` re-arms the `voting.closed` webhook.
- Deleting a project's team member does not delete their votes (audit trail).
- Vote audit rows name voter and project, so the organizer dashboard masks them until voting closes (case 12; BUG-31).
- Failed-login limits reserve the attempt before the password is checked, so parallel guesses cannot all pass (case 19; BUG-32).
- Once any tally has been shown (results API, `/vote` after close, dashboard top 10, export with votes), the voting window is final: 409 `voting_closed_final`. Rescheduling a window nobody has seen stays allowed (BUG-33).
- Tallies count only projects still on the ballot, and never a vote for the voter's own team, even if they joined it after voting (BUG-34).
- Comments only on canonical submitted projects; NUL bytes rejected; a comment id outside SQLite's integer range is 404, not 500 (BUG-35).

## Explicitly out of scope
- Email-gated or link-based voting (brief allows authenticated; offline rule means no email).
- CAPTCHA (needs an external service).
- Pairwise collusion detection between voters.

## Status
- [x] Drafted
- [x] Reviewed by a human (approved 2026-09-27)
- [x] Implementation matches this contract
- [x] Golden tests exist for every behavior case above
