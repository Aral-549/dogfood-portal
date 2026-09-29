# Threat model

What someone might try against a hackathon run on this portal, what stops them, and what does
not. The kickoff brief names four attacks (sybil votes, ballot stuffing, judge collusion,
deadline gaming); they come first. Every "stopped" line points at the code and the golden test
that holds it; every "not stopped" line is a real gap, not a disclaimer.

Who we assume: **participants** want to win, **voters** may be a team's friends or throwaway
accounts, **judges** may have undisclosed ties, the **organizer** is trusted but audited, and
the **network attacker** can send any HTTP request (curl, not just the UI). Out of scope: someone
with shell access to the host or the data volume.

## 1. Sybil votes (many accounts, one person)

**Stopped or surfaced**
- Only logged-in accounts vote; judges of the event cannot vote; nobody votes for their own team
  (`core/authz.cast_vote`; `test_t3_public.py` cases 1, 6, 7).
- Registrations whose normalized email matches an existing one are flagged `duplicate_email`
  (lowercase; gmail/googlemail dots and `+tags` folded: `a.b+x@gmail.com` ~ `ab@gmail.com`)
  (`core/public.normalize_email`; case 21).
- The 4th and later registrations from one IP within an hour are flagged `many_accounts_one_ip`
  (case 20). Flagged, never blocked: campus NAT puts hundreds of real students behind one IP.
- The organizer sees every flag with the account's vote count and can **void** its votes with a
  required reason; voiding is audited (`vote.void`) and reversible (case 22,
  `test_abuse_panel_shows_votes_of_flagged_accounts`).

**Not stopped**
- Distinct email addresses from distinct IPs. There is no email verification (the portal must
  run offline, with no mail server) and no CAPTCHA (needs an external service). A determined
  person with ten mailboxes and a phone hotspot gets ten votes. Mitigation is organizational:
  treat People's choice as a small prize, and review flags before announcing.
- The per-IP registration counter is in memory; a restart clears it.

## 2. Ballot stuffing (one account, many votes)

**Stopped**
- One vote per project per voter (database primary key) and at most `votes_per_voter` per event,
  checked inside one `BEGIN IMMEDIATE` transaction: 20 parallel requests from one voter store
  exactly one vote (cases 3, 4; concurrency verified against a live server).
- 10 votes or withdrawals per minute per account, then 429 with `Retry-After` (case 18).
- **Tallies are hidden from everyone, organizers included, until the window closes** (case 10),
  so nobody can see who is losing and target them, and a late bandwagon has nothing to follow.
  That includes the organizer's audit log, whose vote rows are masked until close (BUG-31, found
  in review: before the fix the dashboard showed who voted for what).
- **No peek-and-reopen.** Once any tally has been shown, the voting window can no longer move
  (409 `voting_closed_final`, BUG-33). Closing early to read the tallies and then reopening is
  refused.
- **Ballot order is random per voter and stable across reloads**
  (`sha256(event:user:project)`, case 13): no project benefits from always being listed first.
- Every vote and withdrawal is in the audit log (`vote.cast`, `vote.withdraw`), readable by the
  organizer after voting closes.

**Not stopped**
- Brigading by real people (a team asking its friends to register and vote). That is not
  distinguishable from popularity, and it is the reason the published ranking comes from judges.

## 3. Judge collusion and favoritism

**Stopped**
- **A judge cannot read another judge's scores over the API**, not only in the UI: the check is in
  the backend (`core/authz`; the checker's "judge cannot see peer scores" and `test_authz.py`).
  Webhooks carry judge and project ids, never values (`test_api_case6`); judge records carry a
  count, never scores or project ids (records case 7). So judges cannot coordinate by watching
  each other's numbers.
- A judge on a project's team can neither score it nor be counted for it, even for a review given
  before joining (BUG-12). Judges can **recuse** themselves with a reason; the pair is then never
  assigned or counted again (`test_research_features.py`).
- **Favoritism is flagged**: a review whose normalized score is 2.0 or more above the consensus
  of the other judges on that project (`core/agreement`, `contracts/judge-agreement.md`).
- **Outlier judges are flagged**: leave-one-out agreement below -0.3 over 4+ shared projects.
- The organizer can exclude a judge's reviews; the exclusion is audited and **disclosed on the
  public results page**, so it cannot be used quietly either.
- Normalization (per-judge, per-criterion z-scores) removes a generous judge's advantage, and two
  independent re-readings (shrunk normalization, judges' orderings via Bradley-Terry) flag any
  prize placing that depends on the method ("prize line disputed").

**Not stopped**
- Two or more judges who agree to inflate the same project look like agreement, not disagreement.
  The favoritism flag only fires against the *other* judges' consensus; if every judge of a
  project colludes, nothing fires. More judges per project (the dashboard's judging plan) and
  tie-break re-reviews of close calls are the mitigation.
- A relationship the judge does not disclose. Conflicts are detected only through team
  membership or self-declared recusal.

## 4. Deadline gaming

**Stopped**
- Submissions and edits are refused at and after `submissions_close` by the **server's UTC clock**
  (half-open: open iff `now < close`), checked in the backend before the request body is read;
  a client-supplied time is never trusted (`core/deadline`, `test_submissions.py` cases 1-6, the
  checker's "closed event refuses submissions").
- Joining a team, leaving one and creating one are refused after the close too (lifecycle).
- Scores are refused after `judging_close`; votes and withdrawals outside the voting window.
- Moving a deadline is an organizer action and is audited with before and after values.

**Not stopped**
- An organizer who extends the deadline for everyone (by design, audited) or a host whose clock is
  wrong (the portal trusts the host clock; run NTP).

## 5. Other attacks we considered

| Attack | What stops it | Gap |
|---|---|---|
| Role escalation (participant acts as judge or organizer) | Every protected route asks `core/authz` first; API tokens carry only their creator's roles | none known; `test_authz*.py` |
| Password guessing | scrypt hashes; failed logins limited per email+IP (5), per email (20), per IP (50) in 5 min; each attempt is reserved before the hash, so parallel guesses cannot slip past (BUG-32); a stranger cannot lock the owner out | no 2FA |
| CSRF on cookie sessions | SameSite=Lax cookies plus an Origin/Referer same-host check on every unsafe request | |
| Clickjacking | `X-Frame-Options: DENY` and `frame-ancestors 'none'` everywhere except the embed widget | |
| Stolen session token | Only SHA-256 of tokens is stored; cookies HttpOnly, Secure over HTTPS; API tokens revocable, last use shown | no device list for cookie sessions |
| Duplicate / copied submissions | Same-team duplicates merged (importer); same repo or near-identical text across teams flagged before announcing (`core/integrity`) | pre-written code: no network access to check commit dates |
| Forged certificates | Ed25519 signatures verifiable offline with openssl; revocation shown by `/verify` | a thief with the data volume has the signing key |
| Webhook abuse (SSRF) | http(s) only; link-local (cloud metadata), multicast, unspecified hosts refused; redirects not followed; 5 s timeout, sends off the request path | DNS names resolving to internal hosts are allowed (organizers are trusted) |
| Import tampering | Admin-only; refuses ids owned by another event; malformed rows rejected; a failed import is undone | |
| Memory exhaustion | Request bodies capped at 1 MiB (32 MiB for import) before handlers read them; rate-limiter memory is swept | no protection against volumetric DDoS (put a proxy in front) |
| Organizer abuse | Every state change is in the audit log with actor and before/after | the audit log lives in the same database; a host admin could edit it |
