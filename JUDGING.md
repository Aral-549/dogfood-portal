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

### Known limits (honest)

- **Small samples.** Per-judge statistics come from 1 to 11 reviews. A judge
  with 2 reviews has an sd estimated from two points, so z-scores are noisy.
  Shrinking each judge's `mu`/`sd` toward the global values by review count
  would be more robust. We chose not to, to keep the method explainable in
  one paragraph. This is the first thing we would change.
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

## 4. Results and export

- **Organizer dashboard** (`/organizer`): judge progress (done / assigned,
  unfinished flagged), raw vs normalized ranking, merged duplicates, and the
  audit log.
- **CSV** (`/api/export.csv`, organizer only):
  `rank, project_id, title, team, track, n_reviews, raw_mean, normalized_z, low_confidence`.
  Cells starting with `= + - @` are prefixed with `'` so spreadsheets do not
  execute them.
- **Public results** (`/results`) show rank, title, team, track and review
  count only after the organizer publishes. Per-judge scores are never public.

## 5. Role isolation

Every protected route asks `core/authz` before it loads any data:

- A judge's own scores (`/api/judge/scores`) are filtered by the judge id in
  the SQL query, not in a template.
- A judge's scores by id (`/api/judges/{id}/scores`) are allowed only for that
  judge or an organizer. A peer judge gets 403. So does a nonexistent id, so
  judge ids cannot be enumerated.
- Participants and visitors get 403 and 401 respectively on every judging and
  organizer route.
