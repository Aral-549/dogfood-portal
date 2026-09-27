# Contract: advisory judging aids adopted from research (RESEARCH.md)

## Purpose
Help organizers judge fairly and announce safely, beyond the tested tiers: a ranking from judges'
orderings, judge recusal, pre-announcement integrity flags, a judging capacity plan and per-track
winners. Everything here is advisory except recusal: nothing changes the published ranking
(`core/scoring`), and flags are prompts for a human, never penalties.

## Inputs
- Counted reviews (same set as the published ranking), canonical projects, prize places k
- Recusal: `{reason}` from a judge for one of their assigned (or reviewed) projects
- Projects' team, title, summary, repo_url (integrity)

## Outputs
- `GET /api/v1/results/cross-check?k=` (organizer): per project `rank`, `shrunk_rank`, `ordering_rank`, `p_top_k`, `prize_line_disputed`, `disagreeing`
- `GET /api/v1/integrity?k=` (organizer): flags `shared_repo | similar_text | no_repo`, prize contenders first
- `POST /api/v1/judge/recusals/{project}` (judge); audit `judge.recuse`
- Dashboard: Order rank column, judging plan, integrity section, recusals; results page: top 3 per track

## Behavior cases (input -> expected output)
| # | Input | Expected output | Notes |
|---|-------|-----------------|-------|
| 1 | one judge scored a > b | one comparison, a wins; strengths satisfy p_a p_b = 1 and x^3 - x^2 - x - 3 = 0 | weak prior: one tie vs a virtual opponent of strength 1 |
| 2 | same orderings, one judge's scores all shifted up | identical ordering ranks | generosity invariance |
| 3 | rock-paper-scissors cycle | equal strengths, ties by id; unscored projects last by id | |
| 4 | cross-check on the fixture, k = 3 | `prize_line_disputed` iff the three methods disagree on top-3 membership; prj_34 not disputed | |
| 5 | judge recuses from an assigned project | 204; assignment removed; score no longer counted; scoring it again 403 `recused`; auto-assign never re-pairs | review row kept for the audit trail |
| 6 | recusal for an unassigned project / non-judge / visitor / blank reason | 404 / 404 / 401 / 422 | |
| 7 | two teams, same repository (URL case, `www.`, `.git`, trailing `/` ignored) | `shared_repo` on both | same-team duplicates are the importer's merge, not flagged |
| 8 | two teams, title+summary 3-shingle Jaccard >= 0.6, both >= 8 shingles | `similar_text` on both | short boilerplate never matches |
| 9 | project without repo_url | `no_repo` | |
| 10 | plan(175 projects, n = 3, t = 4, T = 120 min) | 18 judges | MLH's reference row |
| 11 | results page after publishing | top 3 of each track from the published ranking | |
| 12 | cross-check / integrity as judge or participant / visitor | 403 / 401 | |

## Explicitly out of scope
- Changing the published ranking; automatic penalties; network checks of repositories.

## Status
- [x] Drafted
- [ ] Reviewed by a human
- [x] Implementation matches this contract
- [x] Golden tests exist for every behavior case above (`tests/golden/test_research_features.py`)
