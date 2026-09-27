# Bug Log

Every entry here must result in a permanent case added to `tests/golden/`
before it's marked resolved. A patched bug without a regression case is not
resolved -- it's just hidden until the next rewrite.

Entries below come from the 2026-09-27 adversarial pass. Regression cases are in
`tests/golden/test_regressions.py` (named `test_bugN_*`) and passed after the fixes of 2026-09-27 (174/175 golden tests; see the note on
`test_case4_body_is_a_json_list` in `contracts/acceptance.md`).

---

## 2026-09-27 -- BUG-1 demo password survives turning demo mode off
- **Symptom:** after one boot with `DOGFOOD_DEMO_SESSIONS=1`, a reboot on the same volume without it still accepts `organizer@dogfood.local` / `dogfood-demo` at `POST /login` (303 + session, admin), and the same for the demo judge/participant accounts. Only the demo bearer tokens are revoked.
- **Root cause:** `boot.py:50` deletes demo sessions only; `boot.py:70` wrote a known password hash (and `is_admin=1` user) that is never cleared.
- **Stage/module:** boot / auth
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug1_demo_password_dead_after_demo_mode_disabled`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-2 one bad criterion name rejects every score
- **Symptom:** a fixture with one misspelled criterion (`qualty`), or one unknown-judge row carrying an extra key, imports 0 of 126 scores (all rejected "missing rubric criteria"); the bad name is stored as a rubric criterion.
- **Root cause:** `core/importer.py:165` builds the rubric from the union of keys in every score row, including rows that are later rejected.
- **Stage/module:** importer
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug2_*` (2 cases)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-3 non-string reference crashes the whole import
- **Symptom:** a score with `"judge": ["jdg_01"]` or `"project": {...}` raises `TypeError: unhashable type` and aborts boot, instead of rejecting the row. Same class: project `team` as a list, member email as an int, judge `tracks` nested list, non-string title in a same-team pair, out-of-range offset timestamps (`OverflowError`).
- **Root cause:** `core/importer.py:144,184` use `in set` on unvalidated values; `except ValueError` at `:149` misses `OverflowError`.
- **Stage/module:** importer
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug3_*` (2 cases)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-4 CSV prints -0.0000 for a zero z
- **Symptom:** a project whose normalized z is exactly 0 (hand-computed) is exported as `-0.0000`.
- **Root cause:** `core/csvexport.py:20` formats float noise (-5.55e-17) with `:.4f`.
- **Stage/module:** csv export
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug4_zero_z_is_not_printed_negative`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-5 large weights collapse to zero
- **Symptom:** rubric weights 1.7e308 x3 are accepted; the sum overflows to inf, every normalized weight becomes 0, every raw_mean and z is 0.0000 and the ranking falls back to project id.
- **Root cause:** `core/scoring.py:37` sums weights without an overflow check; `/organizer/rubric` accepts any finite float.
- **Stage/module:** scoring
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug5_large_valid_weights_do_not_collapse_to_zero`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-6 malformed input returns 500
- **Symptom:** 500 on: public `GET /projects?page=99999999999999999999` (also reproduced read-only on the running container), `POST /login` / `/register` / `/set-password/*` with a non-string JSON password, `POST /organizer/assign {"k":[1]}`, `POST /organizer/judges {"tracks":[1]}`, `POST /organizer/event` and `/organizer/events` with an out-of-range offset date.
- **Root cause:** `services.py:144` OFFSET overflow (`app.py:133` has no upper bound); `services.py:35-38` calls `.encode()` on non-str; `app.py:662` catches only ValueError; `app.py:648` `.strip()` on non-str; `app.py:564,586` miss OverflowError.
- **Stage/module:** http handlers
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug6_no_500_on_malformed_input` (10 params)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-7 failed judge invite leaves an unaudited judge
- **Symptom:** the 500 from `POST /organizer/judges {"tracks":[1]}` has already inserted the user and judge rows (autocommit), with no `judge.invite` audit row.
- **Root cause:** `app.py:629-652` performs several writes without a transaction; the audit row is written last.
- **Stage/module:** http handlers / audit
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug7_failed_judge_invite_leaves_no_unaudited_judge`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-8 "invite judge" resets any existing account's password
- **Symptom:** an organizer who posts an existing user's email (a judge, a participant, another admin) to `/organizer/judges` gets a set-password link for that account; using it replaces the victim's password, so the organizer can log in as a judge and submit scores that the audit log attributes to that judge. Existing sessions stay valid.
- **Root cause:** `app.py:638-650` issues `create_password_link` for any matched user, including ones that already have a `password_hash`.
- **Stage/module:** lifecycle / auth
- **Regression case added:** `tests/golden/test_regressions.py` -- `test_bug8_judge_invite_cannot_reset_an_existing_password`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-9 portal fails to boot: logger kwarg collision
- **Symptom:** first `docker compose up` exited at startup with `TypeError: stage() got multiple values for argument 'event'`; run.py reported "no response" on all 7 checks.
- **Root cause:** `logs.stage(stage_name, event, **fields)` had a positional parameter named `event`; the importer passed `event=<id>` as a field. Parameters are now positional-only.
- **Stage/module:** importer -> logging boundary (`src/dogfood/logs.py`)
- **Regression case added:** covered by `tests/golden/test_importer.py` case 1 and `test_acceptance.py` (both boot through the importer)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-10 `GET /projects/new` shadowed by `/projects/{project_id}`
- **Symptom:** found reading code before the first run: the submit form route would have been treated as a project id (404).
- **Root cause:** FastAPI matches in registration order; the parameterized route was registered first.
- **Stage/module:** HTTP routing (`src/dogfood/app.py`)
- **Regression case added:** `tests/golden/test_regressions_2.py` (added by the second verification pass)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-11 `GET /projects/new` shows the form after the deadline (Gemini review)
- **Symptom:** the form rendered after `submissions_close`; the POST was correctly refused, so nothing could be saved, but the page misled the user.
- **Root cause:** the GET handler did not ask `authz.submit_project`.
- **Stage/module:** HTTP handlers
- **Regression case added:** `tests/golden/test_regressions_2.py` (added by the second verification pass)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-12 judge can score a project of their own team (adversarial pass, code reading)
- **Symptom:** a judge who joins an assigned project's team before the close could then score it; assignment excludes conflicts but scoring did not re-check.
- **Root cause:** `authz.score_project` had no conflict-of-interest check.
- **Stage/module:** core/authz
- **Regression case added:** `tests/golden/test_regressions_2.py` (added by the second verification pass)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-13 logout with a Bearer token revoked the shared demo token
- **Symptom:** `POST /logout` with `Authorization: Bearer demo-judge-a` deleted that session for every user of the token until restart.
- **Root cause:** logout deleted whatever token authenticated the request; now it only revokes cookie sessions.
- **Stage/module:** HTTP handlers / sessions
- **Regression case added:** `tests/golden/test_regressions_2.py` (added by the second verification pass)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-14 minor contract gaps (Gemini + adversarial pass)
- **Symptom:** (a) Bearer/cookie pointing at different users was not logged (authz.md edge case); (b) reviews not counted (merged duplicates, incomplete) were dropped silently (scoring.md edge case); (c) a fixture without `event` booted an empty portal; (d) `/docs` loaded Swagger UI from a CDN (offline rule).
- **Root cause:** not implemented. Now: `auth.credential_conflict` log line; `scoring.reviews_not_counted` log line; boot raises FixtureError; docs UI disabled (`/openapi.json` remains).
- **Stage/module:** auth, scoring, boot, app
- **Regression case added:** `tests/golden/test_regressions_2.py` (added by the second verification pass)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-15 anyone can revoke a demo token via logout with it as a cookie
- **Symptom:** `POST /logout` with `Cookie: session=demo-judge-a` deleted the shared demo session; the checker's token then got 401 until restart.
- **Root cause:** the BUG-13 fix only skipped Bearer-authenticated logouts; `delete_session` would delete any session, demo included.
- **Stage/module:** sessions (`services.delete_session`, `app.logout`)
- **Regression case added:** `tests/golden/test_regressions_2.py` -- `test_bug13_logout_with_demo_token_as_cookie_does_not_revoke_it`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-16 500s on lone-surrogate strings, deep JSON nesting, infinite k
- **Symptom:** `{"password":"\ud800..."}` on /login, /register and every write route; `[` x100000 on any JSON route; `{"k":1e999}` on /organizer/assign: all 500.
- **Root cause:** strings that cannot be UTF-8 encoded reached `.encode()`/sqlite; `RecursionError` and `OverflowError` were not caught.
- **Stage/module:** HTTP input boundary (`app.body_of`)
- **Regression case added:** `tests/golden/test_regressions_3.py` (third verification pass; fails on the pre-fix tree 59b94a3)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-17 failed request left a transaction open, poisoning every later write
- **Symptom:** a 500 inside `POST /organizer/events` left `in_transaction` true; every later `BEGIN IMMEDIATE` failed until restart. Under concurrency, one shared connection across threads gave "cannot start a transaction within a transaction" and rollbacks of other requests' work.
- **Root cause:** one process-wide sqlite connection shared by the threadpool; some handlers had no rollback path.
- **Stage/module:** db / request plumbing
- **Regression case added:** pending (third verification pass)
- **Status:** fixed (one connection per request, rolled back if left open; writes use `db.transaction`)

## 2026-09-27 -- BUG-18 event update committed without its audit row
- **Symptom:** `POST /organizer/event` that failed after the UPDATE left a changed close date and no `event.update` audit row.
- **Root cause:** autocommit writes before the audit call. Now one transaction (also event create, rubric, exclusions).
- **Stage/module:** HTTP handlers / audit
- **Regression case added:** `tests/golden/test_regressions_3.py` (third verification pass; fails on the pre-fix tree 59b94a3)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-19 demo teardown missed volumes created by the first release
- **Symptom:** volume from commit 0a43fb2 with demo on, rebooted on the new code with demo off: demo passwords still logged in.
- **Root cause:** teardown only cleared the new `demo_accounts` table, which old volumes never filled. Now it backfills from demo sessions first.
- **Stage/module:** boot
- **Regression case added:** `tests/golden/test_regressions_3.py` (third verification pass; fails on the pre-fix tree 59b94a3)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-20 importer rubric chosen by rows that are rejected anyway
- **Symptom:** 127 rows with unknown judges and criteria `{x}` made the rubric `['x']` and rejected all 126 real scores; all-empty criteria imported 126 reviews with no scores.
- **Root cause:** every row voted. Now only rows whose judge and project resolve vote; ties are reported; no rubric rejects the scores.
- **Stage/module:** importer
- **Regression case added:** `tests/golden/test_regressions_3.py` (third verification pass; fails on the pre-fix tree 59b94a3)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-21 close date before year 1000 stored unpadded, then 500s
- **Symptom:** `0999-01-01T00:00:00Z` stored as `999-01-01...`; /me and submissions then 500.
- **Root cause:** glibc `strftime("%Y")` does not zero-pad. `format_utc` now pads explicitly.
- **Stage/module:** core/timeutil
- **Regression case added:** `tests/golden/test_regressions_3.py` (third verification pass; fails on the pre-fix tree 59b94a3)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-22 smaller boot and scoring gaps
- **Symptom:** (a) demo boot crashed if someone registered `organizer@dogfood.local`; (b) a review given before the judge joined the project's team still counted; (c) non-string `event.name` in fixtures aborted boot with a raw ProgrammingError; `event.id` of spaces was accepted.
- **Root cause:** (a) only an id conflict was handled, now the demo organizer is skipped and logged; (b) conflicts were only checked at scoring time, now also when computing results; (c) unvalidated fixture fields.
- **Stage/module:** boot, services, importer
- **Regression case added:** `tests/golden/test_regressions_3.py` (third verification pass; fails on the pre-fix tree 59b94a3)
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-23 organizer can take over a password-less fixture participant
- **Symptom:** inviting a fixture participant's email as a judge issued a set-password link the organizer could use themselves, then submit as that team.
- **Root cause:** without email delivery the organizer carries set-password links by hand, so any password-less account was claimable by them. Decision (2026-09-27): team members never get a set-password link; judges who are not participants still do.
- **Stage/module:** lifecycle / auth (`services.create_password_link`)
- **Regression case added:** `tests/golden/test_regressions_4.py`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-24 excluded judge without reviews invisible on the dashboard, but counted publicly
- **Symptom:** excluding a freshly invited judge (no reviews) left no row or re-include button on /organizer, while /results said "Reviews from 1 judge were excluded".
- **Root cause:** exclusions were shown only inside the agreement table (judges with reviews); the public count included judges with no reviews.
- **Stage/module:** organizer template, public results
- **Regression case added:** `tests/golden/test_agreement.py` -- `test_case8_dashboard_lists_exclusion_of_judge_without_reviews`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-25 close-call flag disagreed with the probability shown
- **Symptom:** `0.200,false` in the confidence CSV: raw p 0.1995 is shown as 0.200 but was not flagged.
- **Root cause:** the flag compared the unrounded p. Now it compares the 3 dp value that is shown; p itself stays exact (sum over projects is exactly k).
- **Stage/module:** core/confidence
- **Regression case added:** `tests/golden/test_confidence.py` -- `test_http_close_call_flag_consistent_with_reported_p`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-26 outlier status could disagree with the agreement shown
- **Symptom:** (found by reading) r = -0.3004 would print as -0.300 yet be flagged outlier.
- **Root cause:** status compared the unrounded r. Now it compares the 3 dp value shown.
- **Stage/module:** core/agreement
- **Regression case added:** `tests/golden/test_regressions_4.py`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-27 logins block the whole server
- **Symptom:** 40 concurrent logins pushed /healthz from 2 ms to 0.81 s.
- **Root cause:** scrypt ran on the async event loop in login, register and set-password. Now it runs in the threadpool.
- **Stage/module:** HTTP handlers
- **Regression case added:** `tests/golden/test_regressions_4.py`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-28 set-password link issued before the BUG-23 fix still claims a participant
- **Symptom:** a `password_links` row for a team member, created before BUG-23 was fixed, still set that participant's password when used (up to 7 days).
- **Root cause:** the team-member rule was only checked when a link was issued, not when it was used.
- **Stage/module:** lifecycle / auth (`services.consume_password_link`)
- **Regression case added:** `tests/golden/test_regressions_4.py` -- `test_bug23_link_issued_before_the_fix_cannot_claim_a_participant`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-29 organizer page described the old outlier rule
- **Symptom:** the judge-agreement section said "below 0 over 3+ shared projects" after the rule became below -0.3 over 4+.
- **Root cause:** template text not updated with the contract amendment.
- **Stage/module:** organizer template
- **Regression case added:** `tests/golden/test_regressions_4.py` -- `test_bug26_dashboard_states_the_amended_outlier_rule`
- **Status:** fixed (regression case passes)

## 2026-09-27 -- BUG-30 bulk import could rewrite another event's projects
- **Symptom:** `POST /api/v1/import` of a file with a new event id but a project, team or track id already used by another event returned 201 and overwrote that event's titles and names (reproduced: evt_01's prj_01 renamed "HIJACKED").
- **Root cause:** the route reuses the boot importer, which upserts by id so boot re-imports stay idempotent. The route's `IntegrityError -> 409` never fired because upserts never raise.
- **Stage/module:** T4 import route (`app.import_event`)
- **Regression case added:** `tests/golden/test_t4.py` -- `test_api_case12_13_import_conflict_malformed_and_authz`
- **Status:** fixed (409 `ids_in_use` before anything is written; regression case passes)
