# Structural affinity

`pipeline/run_structural.py` asks 18 typed Jev Nouls over the article states the classify pass already
selected. It generates no text: the stable ids in `pipeline/structural_questions.py` are the closed
vocabulary, and the returned probabilities are the vector.

This is a separate `structural-v1*.jsonl` pass rather than an addition to `delta-v2*.jsonl`. The existing
delta pass's critique, depiction, audience and technique questions have already been bought for the whole
corpus; repeating those 33 questions would raise the projected backfill from about $6.40 to about $11.50.
The corpus join accepts structural shards independently and checks that each was answered from the same
article as its classify row.

## Why it exists

Literal facets and atomic premise tags do not fully express viewing affinity. For den-atlas#103, *The
Middle* and *Malcolm in the Middle* differ in pacing, focus and tone but share the structural appetite:
cash-strapped parents raising several distinct children, sibling conflict, school and recurring domestic
problems. A rail may use that profile without pretending every facet is equal.

The questions describe reusable social units and story engines, not that one pair. They are independent
Nouls because several can be true at once; no facet or answer is removed, and consumers decide which
dimensions suit a “similar” or “you might also like” promise.

## Measurements before the corpus pass

Nine-title #103 pilot, cosine from all 18 probabilities for *The Middle*:

| title | cosine |
|---|---:|
| Modern Family | 0.918585 |
| Malcolm in the Middle | 0.918492 |
| Gilmore Girls | 0.869542 |
| The King of Queens | 0.659387 |
| Succession | 0.518721 |
| The Wire | 0.371503 |
| Game of Thrones | 0.355325 |

The frozen 500-title delta pilot sample returned 500/500 valid rows for 1,601,833 input tokens (`$0.07`).
No question had probability ≥0.5 on a majority of titles. The deliberately selective #103 dimensions were:

| question | titles ≥0.5 / 500 |
|---|---:|
| family household | 46 |
| parents raising children | 15 |
| sibling group | 20 |
| everyday domestic problems | 34 |
| economic precarity | 59 |
| growing up and school | 55 |

Only romantic-pair/relationship-formation (`r=0.842`) and workplace-group/workplace-problems (`r=0.721`)
correlated above 0.70. They remain separate because a social unit and the engine operating on it answer
different questions; storage keeps both while a scorer need not use both.

A second sealed pass covered every unique title in den-atlas's 62 existing rail cases: 1,355 profiles and
1,452 judged pairs, for 6,644,100 input tokens (`$0.2791`). It rejected a global structural reranker:
probability agreement separated good from bad at AUC 0.699, but adding it to production scores did not beat
weight zero on the broad More Like This set. Structural affinity is a candidate source for an affinity rail,
not a replacement for premise/plot similarity.

On 18 cases selected before scoring because their recurring social unit or story engine is material to the
recommendation (397 judged pairs), probability agreement reached good-vs-bad AUC 0.7725. Over the sealed
1,355-title universe, unioning production's candidates with the structural top 20 raised good-title recall:

| split | production top 200 | + structural top 20 |
|---|---:|---:|
| dev (10 cases, 70 goods) | 0.7429 | 0.8429 |
| test (8 cases, 47 goods) | 0.6809 | 0.8298 |

*Malcolm in the Middle* was structural rank 6 for *The Middle* inside that deliberately small universe.
The full-corpus validation below supersedes that rank; this experiment established which similarity
function was promising, not the production candidate depth.

## Corpus backfill

The first full backfill completed on 2026-09-26 using `jev-1.13.0`:

| shard | rows | input tokens | cost | failed |
|---|---:|---:|---:|---:|
| `structural-v1.jsonl` | 52,990 | 163,444,824 | `$6.8647` | 0 |
| `structural-v1-reground-b-aka.jsonl` | 520 | 1,040,192 | `$0.0437` | 0 |

The second shard is the newest-article repair. The corpus join read both manifests, superseded its 520
older rows, and accepted 52,990 structural profiles against the newest classify article hashes.

## Full-store validation

Store v3 adds `structural` (`R × 18` hundredths), `structural_names`, and `structural_has`. The coverage
column is essential: 129 of the 53,119 corpus rows have no structural answer, and must remain unknown rather
than becoming an all-zero profile. A real build completed with 53,119 rows, 123 sections and 161,012,671
bytes.

The full 52,990-profile universe corrected the small-universe estimate. For *The Middle* → *Malcolm*:

| comparison | same-media rank | mixed-media rank |
|---|---:|---:|
| broad probability agreement, all axes equal | 34 | 67 |
| seed-focused agreement, each axis weighted by The Middle's probability | 9 | 14 |

The focused form keeps every axis in storage but stops shared negatives such as “not a mission” and “not a
crime scheme” from overwhelming the family/household engines that define this seed. Across all 18 affinity
cases it is weaker as a universal metric, so the candidate lane uses both: 50 broad plus 50 focused profiles,
deduplicated. Against the full universe, union recall with production top 200 is 0.8429 dev and 0.8085 test.
The earlier restricted-universe top-20 result (0.8429 / 0.8298) was optimistic and must not be quoted as a
full-corpus result.

The rebuilt store also explains why nomination alone does not solve the example. Malcolm is already in the
ordinary vector pool, then fails the label/tone gate at 0.34 versus its 0.35 floor. With the gate opened and
structural agreement added to the existing global scorer, it still does not reach the top 10. A 62-case run
with structural nomination at its default-off setting preserves production; enabling top-20 nomination moved
dev nDCG 0.600→0.602 and left test at 0.516, but Malcolm remained excluded. The evidence therefore supports
a separate affinity / You Might Also Like path with seed-focused axes and a soft tone signal—not a global
More Like This weight or floor change.

## Run

Use every classify shard that covers the article dump; inspect the plan before buying:

```sh
pipeline/run_structural.py --articles out-repass/articles.jsonl \
  --combined out-repass/combined-v1-r2.jsonl \
  --combined out-repass/combined-v1-r2-token-fallback.jsonl \
  --combined out-repass/combined-v1-r2-token-fallback-2.jsonl \
  --out out-repass/structural-v1.jsonl --model jev-1.13.0 --plan
```

Replace `--plan` with `--spend` only after the plan names the expected title count and acceptable spend.
When classify shards supersede older rows, pass all of them: their immutable `runStartedAt` manifests select
the newest state for each title, using the same rule as the corpus join. The article dump must contain those
newest articles; an older row that was superseded is deliberately not compared with it.
