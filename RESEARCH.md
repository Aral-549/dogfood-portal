# Research: what other hackathon platforms do, and what we took

September 2026. The question: beyond the four tiers the checker tests, what do organizers of
real hackathons need that comparable software offers (or gets wrong), and which of it fits this
portal's constraints (one container, offline-capable, explainable judging)?

## What we looked at

| Source | What it is | What we learned |
|---|---|---|
| [Devpost help: how judging works](https://help.devpost.team/article/231-how-judging-works), [setting up judging](https://help.devpost.team/article/230-how-to-set-up-judging), [judging & public voting](https://help.devpost.com/article/64-judging-public-voting) | The largest public hackathon platform | Weighted criteria (must total 100%, up to 6), judging groups, multi-round judging, **ranked** as well as scored rounds, judges can **recuse** from conflicted submissions, popular-choice voting. Aggregation is a plain (weighted) average across judges. |
| [Gavel](https://www.anishathalye.com/2016/09/19/gavel-an-expo-judging-system/) ([code](https://github.com/anishathalye/gavel)) | HackMIT's expo judging system, used by dozens of events | Judges compare pairs instead of scoring; a Crowd-BT model turns comparisons into a ranking. Motivation: absolute 1-10 scores are unreliable across judges. |
| [MadHacks: a better judging algorithm](https://ben.enterprises/hackathon-judging) | Post-mortem of Gavel at a 114-project event | In simulation Gavel found the true best project for the final round **8%** of the time (random: 6%). Rubric judging suffers from generosity differences *even after per-judge normalization*. Their fix: each judge **stack-ranks** ~7 projects; a Bayesian Plackett-Luce model fits the rankings (50% top-5 hit rate). |
| [MLH organizer guide: judging plan](https://guide.mlh.com/general-information/judging-and-submissions/judging-plan) | The standard playbook for student hackathons | Science-fair judging, **3 judges per project**, 4 minutes per review, sizing formula **J = ceil(P·n·t / T)**, stack ranking to normalize, and a **"cheating check on all winners before you announce"**. Keep exports and backups. |
| [Eventornado platform comparison](https://eventornado.com/blog/hackathon-software-comparison), [HackerEarth: enterprise platforms](https://www.hackerearth.com/blog/hackathon-platforms), [InnovationCast review](https://innovationcast.com/blog/hackathon-software) | Feature matrices across Devpost, TAIKAI, Eventornado, HackerEarth, Agorize, Unstop, Judgify and others | Table stakes: registration, teams, submissions, gallery, idea voting, weighted criteria, automatic ranking. Differentiators: built-in chat, white-labeling, live leaderboards, "score normalization" (Devpost, Judgify) with little public detail on how. |
| [DoraHacks: quadratic voting](https://hidorahacks.medium.com/what-is-quadratic-voting-funding-how-did-we-improve-it-70989e813cf9) | Community voting for web3 hackathons | Quadratic voting dampens "whales", but only works with strong sybil resistance: DoraHacks runs a 3-5 day **post-vote anti-sybil check** before results count. |
| [Hackathon commit-forensics / plagiarism tools](https://github.com/sachithr19-007/Hackjudge-AI), [copydetect](https://github.com/blingenf/copydetect) | Open-source integrity tooling | Flag first commits before the start date, last-day "dump" commits, and fingerprint similarity across submissions. |
| [On hackathons and judging them](https://www.steventammen.com/old-posts/on-hackathons-and-judging-them/), [HackPrinceton organizer notes](https://medium.com/hackprinceton/behind-the-scenes-challenges-of-organizing-hackprinceton-c5cb2d767554), [AngelHack guide](https://angelhack.com/blog/how-to-organize-a-hackathon/) | Organizer and participant experience | Judging is the organizers' biggest pain point; judges drop out on the day; conflicts of interest (professors, sponsors) and pre-written projects go unchecked. |

## Where the portal stood

| Need (from the research) | Typical platforms | This portal before | Now |
|---|---|---|---|
| Weighted rubric, auto ranking | Devpost, Eventornado, Judgify | yes | yes |
| Normalization for judge generosity | Devpost/Judgify ("normalization", undocumented) | per-judge z-scores, documented, plus prize-line confidence | + shrinkage and **ordering-only** cross-checks |
| Rank-based judging (Gavel, MadHacks, MLH) | Gavel, Devpost ranked rounds | no | **Bradley-Terry ranking from judges' orderings**, no extra judge work |
| Judge recusal | Devpost | only automatic (judge on the team) | **self-declared recusal** |
| Judge sizing / coverage | MLH spreadsheets | assignment only | **capacity plan + under-reviewed list** |
| "Cheating check on all winners" | MLH advice, 3rd-party tools | no | **offline integrity checks, contenders first** |
| Category / track winners | Devpost prize categories | overall only | **top 3 per track** on the results page |
| Popular choice vote | Devpost, TAIKAI, DoraHacks | yes, hidden tallies, abuse flags, voiding | unchanged |
| Tie-breaks for close calls | rare | yes (close-call re-review) | unchanged |

## What we built, and where

1. **Ranking from judges' orderings** (`core/ordinal.py`). Gavel, MadHacks and MLH all rank from
   comparisons because an ordering does not depend on how generous a judge is. The rubric scores
   we already collect contain each judge's ordering, so every pair of projects one judge scored
   becomes a comparison and a Bradley-Terry model is fitted (Hunter's MM algorithm, weak prior so
   unbeaten projects stay finite). This gets the robustness MadHacks wanted without changing how
   judges work. It is advisory: the dashboard's "Order rank" column, and
   `GET /api/v1/results/cross-check` returns published, shrunk and ordering ranks side by side.
   A project whose top-k membership depends on the method is flagged **prize line disputed**. On
   the fixture event, prj_16 is 2nd published (P(top 3) = 0.547) but 6th and 7th by the two
   independent readings: exactly the project to look at before announcing.
2. **Judge recusal** (`POST /api/v1/judge/recusals/{project}`, judge page). A judge declares a
   conflict with a reason; the assignment goes, any score stops counting, auto-assign and
   tie-breaks never pair them again, and the organizer sees it. All conflict logic now lives in
   one place (`services.conflict_pairs`).
3. **Integrity checks before announcing** (`core/integrity.py`, `GET /api/v1/integrity`,
   dashboard). Offline versions of the tooling above: the same repository submitted by different
   teams, near-identical title+summary across teams (word 3-shingle Jaccard >= 0.6, short
   boilerplate ignored), and projects with no repository to verify. Prize contenders are listed
   first. Flags for a human, never automatic penalties.
4. **Judging plan** (`core/planning.py`, dashboard). MLH's J = ceil(P·n·t / T) with n = 3 and
   t = 4 min: reviews needed and done, load per judge, judges needed for 60/120/180-minute
   windows, and the projects still under 3 counted reviews.
5. **Top of each track** on the public results page: Devpost-style category winners read from
   the same published ranking.

## What we chose not to build, and why

- **Live pairwise judging UI (Gavel), the brief's "Pairwise Mode" bonus.** The brief says
  to pick one bonus and nail it; ours is **Normalization Proof** (raw vs normalized vs ranking
  change on the dashboard, method defended in JUDGING.md). Our ordering-only ranking uses
  Bradley-Terry too, but it is *not* Pairwise Mode: there is no screen showing a judge two
  projects and asking which is better. MadHacks' simulation also suggests Gavel-style adaptive
  pair selection rarely finds the best project, so we took the idea (rank from comparisons) and
  applied it to the comparisons the rubric scores already contain.
- **Quadratic voting.** Its value depends on sybil resistance we do not have (no identity
  verification, offline, no email). DoraHacks needs days of anti-sybil review. Our vote stays
  one-person-limited-votes with duplicate/IP flags and organizer voiding.
- **Repository commit forensics** (first commit before the start, last-day dumps). Needs network
  access to code hosts, which the portal must not assume. The integrity panel links each flagged
  project so an organizer can check by hand; an optional online checker is future work.
- **Built-in chat, white-labeling, hiring pipelines.** Real features of the commercial platforms
  but outside the problem statement; Discord/Slack already fill the chat role, and webhooks let
  an event connect them.
- **AI-assisted scoring.** Adds an opaque judge to a process whose selling point is that every
  number can be explained; also needs the network.

## Possible next steps

- Plackett-Luce over explicit judge stack-rankings (MadHacks) as an optional finals round, using
  the tie-break assignment machinery.
- Expo logistics (MLH): table numbers and a per-judge walking order.
- An opt-in, online commit-date check against the event window.
