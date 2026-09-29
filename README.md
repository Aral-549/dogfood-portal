# DOGFOOD portal

A self-hosted platform for running a hackathon: registration, teams, submissions with a
deadline that holds, a public gallery, judge assignment, weighted scoring, and results that
correct for harsh and lenient judges. It also has a community vote, signed certificates and a
REST API. One command starts it on a laptop with the network off.

Built for [DOGFOOD 2026](https://dogfoodhack.com). MIT licensed.

## Where it stands

The official checker, run against a fresh `docker compose up`, prints:

```
claimed T1 T2 T3 T4, verified T1 T2
note: claimed but not verified: T3 T4
```

All seven checks pass. `run.py` only has checks for T1 and T2, and the brief says T3 and T4
are judged by hand, so that note shows up for anyone who claims them. What we did not build,
or built only partly, is under [Honest limits](#honest-limits).

## Run it

```
docker compose up
```

Then open http://localhost:8080. On first boot the portal loads `fixtures.json` (40 projects,
30 judges, 126 scores) and prints demo logins to the log:

| Role        | Login                              | Password     | API header                                |
|-------------|------------------------------------|--------------|-------------------------------------------|
| Organizer   | organizer@dogfood.local            | dogfood-demo | `Authorization: Bearer demo-organizer`    |
| Judge A     | diego.herrera@example.org (jdg_24) | dogfood-demo | `Authorization: Bearer demo-judge-a`      |
| Judge B     | jonas.vogel@example.org (jdg_26)   | dogfood-demo | `Authorization: Bearer demo-judge-b`      |
| Participant | priya1@example.org (team tm_01)    | dogfood-demo | `Authorization: Bearer demo-participant`  |

Data lives in the `dogfood-data` volume, and `docker compose down -v` resets it. Once the image
is built, nothing needs the network: there are no cloud accounts, hosted databases or external
APIs.

For a real event, set `DOGFOOD_DEMO_SESSIONS: "0"` in `docker-compose.yml`. The demo tokens and
passwords are then removed at boot.

## One event, start to finish

1. The organizer sets dates, prizes and rubric weights on `/organizer`, invites judges and runs
   auto-assign. There is no email, so the portal shows each judge's set-password link for the
   organizer to pass on.
2. Participants register, create a team, share its invite link and submit a project. Drafts
   are allowed and edits work until the deadline, after which the backend refuses both.
3. Judges score each assigned project from 1 to 5 per criterion on `/judge`. A judge only ever
   sees their own scores; asking the API for another judge's scores gets a 403.
4. The organizer watches progress, compares the raw and normalized rankings, looks at close
   calls and flagged judges, then publishes results and downloads the CSV.
5. Anyone can browse `/projects`, and `/results` once published.

## What is in each tier

**T1, core.** Accounts and sessions, five roles (visitor, participant, judge, organizer,
admin), events with tracks and prizes, teams of up to four by invite link, drafts, a deadline
the backend enforces, and a gallery with search and a track filter.

**T2, judging.** Judge invitations, automatic assignment that never pairs a judge with their
own team, a rubric with weights the organizer sets, role isolation checked in the backend
before any data is read, a live progress view, CSV export, and cross-judge normalization
(explained in `JUDGING.md`). On top of that, the dashboard shows how sure the ranking is at the
prize line and which judges disagree with everyone else, both covered below.

**T3, public.** A community vote for logged-in users in its own time window, with a ballot
order that is shuffled per voter. Tallies are hidden from everyone, organizers included, until
voting closes. There are comments on project pages, rate limits on votes, comments and failed
logins, flags for duplicate accounts and for many accounts from one IP, and an audit trail. The
organizer can void a flagged account's votes.

**T4, stretch.** Everything is under `/api/v1` with an OpenAPI description at `/openapi.json`,
including register and login, and personal API tokens can be revoked. Webhooks are signed with
HMAC-SHA256 and retried after 10, 60 and 300 seconds; nothing leaves the portal until an
organizer adds one. Certificates and judge participation records are signed with Ed25519 and
can be checked offline (see below). There is an embeddable gallery widget, and whole events
can be exported and imported without loss.

`RESEARCH.md` covers a few ideas taken from other platforms: judge recusal, a second ranking
built only from each judge's ordering of their projects, integrity checks to run before
announcing winners, and a judging-capacity plan.

## Problems we ran into

These shaped the portal more than the feature list did. Each bug mentioned has an entry in
`BUGLOG.md` and a test that reproduces it.

**Judges don't score alike.** In the fixture data some judges give mostly 2s and 3s and others
mostly 4s and 5s, so a plain average mostly measures which judges a project happened to draw.
We normalize each judge against their own scores (a per-judge z-score). That moves 39 of the 40
projects, and the project with the best raw average drops to sixth because its judges scored
everything high. The fix has its own weak spot: a judge with only two reviews always produces
exactly -1 and +1, so a 4-versus-5 judge counts as much as a 1-versus-5 judge. We show a shrunk
ranking beside the published one as a warning rather than hide it. One judge gave 4, 4, 4 to
every project; that carries no ranking information, so it contributes nothing.

**A ranked list looks more certain than it is.** Resampling each project's reviews shows that
third place is a coin flip: the projects ranked third and fourth are about equally likely to
deserve it. The dashboard flags close calls, and one button sends an extra judge to each.

**Spotting an unreliable judge without crying wolf.** Our first rule flagged any judge whose
scores ran against the others, which flagged 7 of 30 judges, one of them at -0.005. That is
noise. The rule now needs clearly opposite scores over at least four shared projects, and flags
exactly one judge. The cost is that 14 judges share too few projects to be judged at all.

**The fixture data has a duplicate.** Team tm_07 submitted "Dry Harbour" twice. We keep both
rows, treat the later one as the real project, and merge the reviews when scoring: a judge who
reviewed both counts once. Doing it at scoring time means the merge can be audited and undone.

**Keeping a vote secret is harder than hiding a number.** The tally page was locked down, but
for a while the organizer's audit log listed every vote with the voter and the project while
voting was still open (BUG-31). An organizer could also close voting early, read the tallies and
reopen it (BUG-33). Vote entries in the log are now masked until voting closes, and once any
tally has been shown the voting window can no longer move. That lock then got in the way of an
ordinary edit, because the event form always re-sends the voting dates (BUG-37), so only a real
change of dates is blocked now.

**The invite form was a way into other people's accounts.** Inviting an existing user as a
judge issued a set-password link, which let an organizer reset a judge's password and score as
them (BUG-8). Later we found the same route could claim a participant's account (BUG-23).
Set-password links are now only issued for accounts with no password that belong to no team,
and they are checked again when used.

**Concurrency.** Twenty wrong-password logins sent at once all got through a limit of five,
because the limit was checked before the password hash and counted after it (BUG-32). Earlier,
one failed request could leave a database transaction open and block every write until a
restart (BUG-17). Each request now gets its own database connection, rolled back if anything
is left open.

**Odd input.** A reason field starting with an invisible NUL character made SQLite's length
check see an empty string, so a judge's recusal was silently dropped while the audit log
recorded it (BUG-36). Requests containing NUL are now refused outright.

**The official checker has a trap.** Our first README suggested moving the fixture event's
deadline into the future to demo submissions. That makes the checker's "closed event refuses
submissions" check fail, and since T1 is the floor, the report says "verified nothing". The
organizer page now warns about it, and `./scripts/check.sh` runs on its own copy of the data.

## Checking it yourself

```
./scripts/check.sh
```

This starts a separate copy of the portal on its own volume, waits until it is healthy, runs
the unmodified `run.py`, writes `acceptance-report.txt`, and exits non-zero if T1 or T2 fails.
Stop your own portal first, because both use port 8080. The same script runs in GitHub Actions
on every push. If you run `run.py` by hand behind an HTTP proxy, set
`no_proxy=localhost,127.0.0.1` first, or every check fails to connect.

The test suite (about 530 cases, including end-to-end runs of the real `run.py` against a real
server) runs with:

```
pip install -r requirements.txt pytest httpx
PYTHONPATH=src python -m pytest tests/golden -q
```

To verify a signed record without trusting the portal:

```
curl -s http://localhost:8080/records/<id>.json > rec.json
curl -s http://localhost:8080/.well-known/dogfood-signing-key.pem > key.pem
python3 -c "import json,base64;d=json.load(open('rec.json'));open('canon.json','wb').write(json.dumps(d['record'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode());open('sig.bin','wb').write(base64.b64decode(d['signature']))"
openssl pkeyutl -verify -pubin -inkey key.pem -rawin -in canon.json -sigfile sig.bin
```

Keep `/data/signing_key` with your backups: a new key cannot verify old records.

## Moving an event in and out

`GET /api/v1/events/<id>/export.json` (organizer) returns the whole event in the
`fixtures.json` shape, plus rubric weights, exclusions, comments and, after voting closes,
votes. To load it into an empty portal:

```
DOGFOOD_FIXTURES=none docker compose up -d
docker compose exec portal python -m dogfood.cli create-admin you@example.org
# open the printed set-password link, log in, create a token, then:
curl -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
     --data-binary @evt_01.json http://localhost:8080/api/v1/import
```

To embed the gallery on another site:
`<script src="http://localhost:8080/widget.js" data-event="evt_01"></script>`

## Docs

- `ARCHITECTURE.md`: how the pieces fit together and why
- `DATA-MODEL.md`: the schema, and how data gets in and out
- `JUDGING.md`: assignment, the scoring maths, normalization and its limits
- `THREAT-MODEL.md`: sybil votes, ballot stuffing, judge collusion and deadline gaming, with
  what is stopped and what is not
- `RESEARCH.md`: what other platforms do, and what we took or left
- `contracts/`: the specs each part was built against
- `BUGLOG.md`: every bug found, and the test that now guards it

## Honest limits

- There is no email. Invite and set-password links are shown to the organizer to pass on, so
  the portal can run offline.
- There is no password reset and no two-factor login. The 91 fixture participants have no
  password, so only the demo participant can log in.
- Voting only requires a login. Without email verification or a CAPTCHA, one person with many
  mailboxes gets many votes; the portal flags likely duplicates for the organizer but cannot
  stop them.
- The published ranking uses plain per-judge z-scores, which judges with only two or three
  reviews make noisy. The shrunk ranking beside it corrects for this, but only as a warning.
- Eligibility is automatic for "submitted, not a duplicate, before the deadline", and otherwise
  an organizer's audited decision. There are no per-track rules.
- Rate-limit counters live in memory, so a restart clears them.
- The container ignores `X-Forwarded-For`. Behind a reverse proxy every visitor shares the
  proxy's IP, which weakens the per-IP limits and flags. To trust your proxy, remove
  `--no-proxy-headers` from the `Dockerfile` and set `FORWARDED_ALLOW_IPS` to its address.
- `/docs` (Swagger UI) is off because it loads from a CDN. The schema is at `/openapi.json`.
