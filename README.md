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

All seven checks pass. `run.py` only has checks for T1 and T2; T3 and T4 are judged by hand
under the brief, so that note appears for every team that claims them. What we did not build,
or built only partly, is listed under [Honest limits](#honest-limits).

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
is built, nothing needs the network: no cloud accounts, no hosted database, no external APIs.

For a real event, set `DOGFOOD_DEMO_SESSIONS: "0"` in `docker-compose.yml`. The demo tokens and
passwords are then removed at boot.

## One event, start to finish

1. The organizer sets dates, prizes and rubric weights on `/organizer`, invites judges (the
   portal shows a set-password link to pass on, since there is no email) and runs auto-assign.
2. Participants register, create a team, share its invite link, and submit a project. Drafts
   are allowed and edits work until the deadline, after which the backend refuses both.
3. Judges score each assigned project from 1 to 5 per criterion on `/judge`. A judge only ever
   sees their own scores; asking the API for another judge's scores gets a 403.
4. The organizer watches progress, compares the raw and normalized rankings, looks at close
   calls and flagged judges, then publishes results and downloads the CSV.
5. Anyone can browse `/projects`, and `/results` once published.

The fixture event closed on 2026-03-01 and the checker relies on that: its "closed event
refuses submissions" check fails if you move that deadline into the future. If you move it for
a demo, run `docker compose down -v` afterwards. `./scripts/check.sh` is unaffected because it
uses its own volume.

## What is in each tier

**T1, core.** Accounts and sessions, roles (visitor, participant, judge, organizer, admin),
events with tracks and prizes, teams by invite link (up to four people), drafts, a deadline the
backend enforces, and a gallery with search and a track filter.

**T2, judging.** Judge invitations and automatic assignment that never pairs a judge with their
own team, a rubric with weights the organizer sets, role isolation checked in the backend
before any data is read, a live progress view, CSV export, and cross-judge normalization. The
normalization is a per-judge z-score; `JUDGING.md` explains it, shows what it does to the
fixture ranking (it moves 39 of 40 projects) and lists its weaknesses.

Two things we added because the plain ranking hid real problems:

- Prize-line confidence. Each project's reviews are resampled to estimate how often it would
  still be in the top k. On the fixtures, third place is a coin flip, and one button assigns a
  tie-breaker judge to each close call.
- Judge agreement. Each judge's scores are compared with the other judges on the same
  projects. On the fixtures one judge (jdg_04) scores almost exactly against everyone else. The
  organizer can exclude a judge with a written reason; it is audited and the public results say
  how many judges were excluded.

**T3, public.** A community vote for logged-in users in its own time window, with a ballot
order that is random per voter. Tallies are hidden from everyone, organizers included, until
voting closes, and once any tally has been shown the window can no longer move. There are
comments on project pages, rate limits on votes, comments and failed logins, flags for
duplicate accounts and for many accounts from one IP, and an audit trail. The organizer can
void a flagged account's votes.

**T4, stretch.** Everything is under `/api/v1` with an OpenAPI description at `/openapi.json`,
including register and login, and personal API tokens can be revoked. Webhooks are signed with
HMAC-SHA256 and retried after 10, 60 and 300 seconds; nothing leaves the portal until an
organizer adds one. Certificates and judge participation records are signed with Ed25519 and
can be checked offline (see below). There is an embeddable gallery widget, and whole events
can be exported and imported without loss.

`RESEARCH.md` covers a few further ideas taken from other platforms: judge recusal, a second
ranking built only from each judge's ordering of their projects, integrity checks to run before
announcing winners, and a judging-capacity plan.

## Checking it yourself

```
./scripts/check.sh
```

This starts a separate copy of the portal on its own volume, waits until it is healthy, runs
the unmodified `run.py`, writes `acceptance-report.txt`, and exits non-zero if T1 or T2 fails.
Stop your own portal first, because both use port 8080. The same script runs in GitHub Actions
on every push. If you run `run.py` by hand behind an HTTP proxy, set
`no_proxy=localhost,127.0.0.1` first, or every check fails to connect.

The test suite (`tests/golden/`, about 520 cases) runs with:

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

## How this was built

Built inside the 72-hour window with AI coding agents, which the brief expects. The code was written by Claude Code (Claude Opus 5.5) under our direction, and the commit history
says so: every commit carries a `Co-Authored-By: Claude` line, and 25 of the 43 commits came
from a Claude Code session running in the cloud, which we reviewed before merging.

What kept the code honest was process, not the model:

- Every module started as a contract in `contracts/`: input and expected-output cases, written
  and approved before any code. The stack, the duplicate-submission rule, the normalization
  method and the T3/T4 designs were decided by us, not the agent.
- For most of the build, the agent that wrote the code did not write its tests. Separate agents
  wrote the tests in `tests/golden/` from the contracts alone, then tried to break the code:
  forged requests, concurrent requests, malformed input, around 30,000 fuzzed requests in the
  last pass. The exception is the cloud session, which wrote tests for its own additions
  (`test_teams.py`, `test_system.py`, `test_research_features.py`); a separate
  agent then re-checked its voting code and found the BUG-31 to BUG-35 holes. We also
  tried a second model family for review; OpenAI Codex was out of quota, and Gemini (through
  Antigravity) reviewed the core logic once.
- Every bug found went into `BUGLOG.md` with a test that reproduces it. There are 37. Some were
  serious: an organizer could reset a judge's password through the invite form (BUG-8), and for
  a while the organizer's audit log showed who voted for what while voting was still open
  (BUG-31).

## Docs

- `ARCHITECTURE.md`: how the pieces fit together and why
- `DATA-MODEL.md`: the schema, and how data gets in and out
- `JUDGING.md`: assignment, the scoring maths, normalization and its limits
- `THREAT-MODEL.md`: sybil votes, ballot stuffing, judge collusion, deadline gaming; what is
  stopped and what is not
- `RESEARCH.md`: what other platforms do, and what we took or left
- `contracts/`: the specs the code was built against
- `BUGLOG.md`: every bug found, and the test that now guards it

## Honest limits

- There is no email. Invite and set-password links are shown to the organizer to pass on, so
  the portal can run offline.
- There is no password reset and no two-factor login. The 91 fixture participants have no
  password, so only the demo participant can log in.
- Voting only requires a login. Without email verification or a CAPTCHA, one person with many
  mailboxes gets many votes; the portal flags likely duplicates for the organizer rather than
  stopping them.
- The published ranking uses plain per-judge z-scores. Judges who scored only two or three
  projects make it noisy. A shrunk ranking that corrects for this is shown next to it, but only
  as advice.
- Eligibility is automatic for "submitted, not a duplicate, before the deadline", and otherwise
  an organizer's audited call. There are no per-track rules.
- Rate-limit counters live in memory, so a restart clears them.
- The container ignores `X-Forwarded-For`. Behind a reverse proxy every visitor shares the
  proxy's IP, which weakens the per-IP limits and flags. To trust your proxy, remove
  `--no-proxy-headers` from the `Dockerfile` and set `FORWARDED_ALLOW_IPS` to its address.
- `/docs` (Swagger UI) is off because it loads from a CDN. The schema is at `/openapi.json`.
