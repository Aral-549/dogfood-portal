# Judging

How projects get judges, how scores become a ranking, and why. The code is
`src/dogfood/core/assignment.py` and `src/dogfood/core/scoring.py`. Both are
pure functions with the spec in `contracts/scoring.md` and
`contracts/lifecycle.md` case 14.

## 1. Assignment

The organizer picks `k` (judges per project, default 3) and presses Auto-assign.
For each canonical, submitted project, in id order:

1. **Candidates**: judges whose tracks include the project's track. If the
   project has no track, or no judge covers it, every judge is a candidate.
2. **Exclude**: judges already assigned to the project, and judges who are
   members of the project's team (conflict of interest).
3. **Pick** the least-loaded candidate (ties broken by judge id) until the
   project has `k` judges or the pool runs out.

Existing assignments are never removed, so re-running after inviting more
judges only fills gaps. The algorithm is deterministic: the same input always
gives the same assignment. Each run writes an audit row.

For the fixture event, assignments are imported from the scores. A judge who
scored a project counts as assigned to it, because the fixture has no separate
assignment data.

## 2. Scoring rubric

Each criterion is scored as an integer from 1 to 5. The organizer sets a
positive weight per criterion. Weights are normalized to sum to 1, so `3, 1`
and `0.75, 0.25` mean the same thing. The fixture rubric is `functionality`,
`quality` and `innovation`, all at weight 1. Changing weights recomputes the
ranking immediately and is audited with the before and after values.

The raw weighted score of one review is `r = sum_c w_c * x_c`. A project's
**raw mean** is the mean of `r` over its reviews.

## 3. Normalization

### Problem

Judges use the scale differently. On the fixtures:

- `jdg_07` gave `4, 4, 4` on every criterion to all three of their projects.
- Some judges give mostly 2s and 3s, others mostly 4s and 5s.
- Review counts per project range from 2 to 6, and per judge from 1 to 11.

A project's raw mean therefore depends as much on *which* judges it drew as on
how good it is. "We averaged the scores" rewards projects that happened to get
lenient judges.

### Method: per-judge, per-criterion z-score

For each judge `J` and criterion `c`, over all of J's reviews:

```
mu_Jc = mean of J's scores on c
sd_Jc = population standard deviation of J's scores on c
```

J is **informative on c** when J has at least 2 reviews and `sd_Jc > 0`. Then:

```
z_c   = (x_c - mu_Jc) / sd_Jc     if informative, else 0
z_r   = sum_c w_c * z_c            (per review)
z_p   = mean of z_r over the project's reviews
```

Projects are ranked by `z_p` descending. Ties go to the higher raw mean, then
the lower project id, so there are no ties in the final rank. The value is
rounded to 12 decimal places before sorting, so floating-point noise cannot
reorder two projects.

### Why uninformative judges contribute 0

A judge who gave everything the same score has told us nothing about how
their projects compare. Their `sd` is 0, so the z-score is undefined. The
options were:

| Option                                   | Problem                                                           |
|------------------------------------------|-------------------------------------------------------------------|
| Drop their reviews                       | Their projects silently lose a review and look under-reviewed     |
| Use the raw score                        | Mixes units (raw points vs standard deviations)                   |
| Use the global sd instead                | Invents a spread the judge never showed                           |
| **z = 0: "this project is average for this judge"** | Honest: no ranking information, no distortion          |

A judge with a single review is treated the same way, for the same reason.

### Confidence flag

A project is flagged `low_confidence` when fewer than 2 of its reviews come
from informative judges. On the fixtures, one project is flagged (`prj_19`,
2 reviews). The flag shows on the organizer dashboard and in the CSV, so a
human can look before publishing.

### What it does to the fixture ranking

Normalization moves 39 of the 40 projects. The biggest moves:

| Project          | Reviews | Raw mean | Raw rank | Normalized rank |
|------------------|---------|----------|----------|-----------------|
| prj_19 Small Relay  | 2    | 3.667    | 13       | 30              |
| prj_12 Open Beacon  | 3    | 3.444    | 25       | 9               |
| prj_15 Copper Orbit | 2    | 3.667    | 12       | 28              |
| prj_41 Dry Harbour  | 6    | 3.556    | 19       | 8               |

The raw number one, `prj_11 Salt Ledger` (raw mean 4.333 over 4 reviews),
drops to 6th. Its judges score high across the board, so its 4s and 5s are
closer to those judges' own averages than they look. The organizer dashboard
shows raw rank, normalized rank and the change side by side for every project.

### Ineligible projects

An organizer can rule a project out of prizes (with a reason; audited; disclosed as a count on
the public results page). It is removed *after* normalization: its reviews still count toward
each judge's mean and spread, because a judge's scale is measured over everything they judged,
and eligibility is about prizes, not judges. Consequence: ruling one project out never changes
another project's z, and the ranking simply closes up (`core/scoring.without_projects`).

### Known limits (honest)

- **Small samples.** Per-judge statistics come from 1 to 11 reviews. A judge
  with 2 reviews has an sd estimated from two points, so z-scores are noisy.
  Shrinking each judge's `mu`/`sd` toward the global values by review count
  would be more robust. We chose not to, to keep the method explainable in
  one paragraph. This is the first thing we would change. Meanwhile the
  organizer dashboard shows it as an advisory check (under "Show advanced columns"): a "Shrunk rank" column
  (each judge's mean and variance pulled toward the pooled values, weighted
  n : 3, `core/scoring.shrunk_ranks`) and a "prize line moves" flag on projects
  that are inside the top k under one normalization and outside under the
  other. The published ranking is unchanged. A third, independent reading sits
  beside it: an "Order rank" fitted by Bradley-Terry to every pair of projects a
  judge scored (who did the judge prefer?), which ignores generosity entirely
  (`core/ordinal.py`, see `RESEARCH.md`). A project is flagged "prize line disputed"
  when any of the three disagree about whether it is in the top k.
- **Two-review judges are all-or-nothing.** A judge with exactly 2 reviews always
  produces z = -1 and +1 on each criterion where the scores differ: a 4-vs-5 judge
  moves the ranking as much as a 1-vs-5 judge. Shrinkage (or a floor on `sd`)
  would soften this. It is the same fix as the small-sample point above.
- **Track confounding.** Judges mostly cover one track. A judge who only saw
  strong projects looks "harsh" and has their good scores pulled down. With
  the fixture's assignment we cannot separate judge harshness from track
  strength. More judge overlap across tracks is the real fix.
- **Duplicate merge.** `prj_07` and `prj_41` (same team, title and repo) are
  merged into the later submission. Judges who scored both count once, with
  their score on `prj_41`. Judges who scored only `prj_07` carry over. The
  merged project has 6 reviews. The raw rows are kept on the original project
  ids, so the merge is recomputed every time and can be audited.

## 4. How sure is the ranking? (prize-line confidence)

Spec: `contracts/confidence.md`. Code: `src/dogfood/core/confidence.py`.

A single ranking hides how close the prize line is. For each project we ask:
"if its reviews had come out a little differently, would it still be in the
top k?" We resample each project's own normalized reviews with replacement
(a bootstrap), re-rank, and count how often each project lands in the top k.
When the number of possible resamples is 20 000 or fewer we enumerate all of
them exactly; otherwise we draw 2 000 with a fixed seed, so the same data
always gives the same numbers.

On the fixtures, with 3 prizes:

| Rank | Project | P(top 3) |
|------|---------|----------|
| 1 | prj_34 Iron Switch | 0.887 |
| 2 | prj_16 Salt Kiln   | 0.547 (close call) |
| 3 | prj_33 Slow Trail  | 0.353 (close call) |
| 4 | prj_37 Salt Loom   | 0.354 (close call) |

Third place is a coin flip: the project ranked 4th is as likely to belong in
the top 3 as the one ranked 3rd. A plain average would have published that
as settled. The organizer dashboard flags every project between 20% and 80%
as a close call, and one button assigns one extra judge to each, preferring
judges who have already scored other contenders so the new review is
directly comparable. Confidence is advisory: it never changes the ranking.

**Limit:** judge statistics (mean and sd) are held fixed during resampling,
so uncertainty in the judges' own calibration is not included. The real
uncertainty is somewhat larger than shown.

## 5. Judge agreement, outliers and favoritism

Spec: `contracts/judge-agreement.md`. Code: `src/dogfood/core/agreement.py`.

Normalization fixes judges who are consistently harsh or lenient. It cannot
fix a judge who is biased or careless. For each review we compare the judge's
normalized score with the mean of the *other* judges on the same project
(leave-one-out consensus):

- **agreement**: correlation between a judge's scores and the consensus over
  their shared projects (needs 4+). Below -0.3 is flagged `outlier`.
- **favoritism**: one review 2+ standard units above that project's other
  reviews.
- **uninformative**: the judge's scores carry no ranking information
  (jdg_07's 4, 4, 4).

Nothing is excluded automatically. The organizer can exclude a judge's
reviews with a written reason; it is reversible, audited, and the public
results page states how many judges were excluded (no names).

On the fixtures this flags exactly one judge: jdg_04, whose scores run almost
exactly against the other judges' (agreement -0.994 over 4 shared projects).

**Why these thresholds.** The first version flagged anything below 0 from 3
shared projects. That marked 7 of 30 judges, one at -0.005, which is noise,
not disagreement. The trade-off of the stricter rule: 14 judges share fewer
than 4 projects with anyone and cannot be assessed at all. More overlap in
assignment (the tie-breaker button helps) is the real fix. The flag is a
prompt to look, not a verdict. Pairwise collusion (two judges boosting each
other's picks) is not detected.

## 6. Results and export

- **Organizer dashboard** (`/organizer`): judge progress (done / assigned,
  unfinished flagged), raw vs normalized ranking, merged duplicates, and the
  audit log.
- **CSV** (`/api/export.csv`, organizer only):
  `rank, project_id, title, team, track, n_reviews, raw_mean, normalized_z, low_confidence`.
  Cells starting with `= + - @` are prefixed with `'` so spreadsheets do not
  execute them.
- **Public results** (`/results`) show rank, title, team, track and review
  count only after the organizer publishes. Per-judge scores are never public.

## 7. Role isolation

Every protected route asks `core/authz` before it loads any data:

- A judge's own scores (`/api/judge/scores`) are filtered by the judge id in
  the SQL query, not in a template.
- A judge's scores by id (`/api/judges/{id}/scores`) are allowed only for that
  judge or an organizer. A peer judge gets 403. So does a nonexistent id, so
  judge ids cannot be enumerated.
- Participants and visitors get 403 and 401 respectively on every judging and
  organizer route.
