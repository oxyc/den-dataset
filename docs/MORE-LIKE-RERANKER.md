# Jev More Like This gate

Issue #132 does not authorize a corpus precompute merely because the title-and-year prior beat plot cosine.
The article-aware question shape must first improve on that prior on the already measured step-9d ruler.

`tools/rulers/more_like_gate.py` freezes that experiment. It performs no call unless `run --spend` is given.
The preparation step requires an eval-only export of the 2,100 step-9d cases; MovieLens-derived rows stay out
of the published dataset and out of git.

The export is one JSON object with schema `issue18-step9d-export-v1`, the step-9d comment URL as
`sourceComment`, and `cases`. Each case has:

- a unique `pairId`;
- `anchor`, `positive`, and `negative`, each with `key`, `title`, and `year`;
- the stored Jev `titleYear` positive and negative Nouls;
- the measured `plot` positive and negative cosine values;
- `controls.yearGapEqual=true`, `controls.seedGenreEqual=true`, and the absolute
  `controls.popularityLogGap` (at most 0.12).

If the old per-case title prior is unavailable, rerun it honestly on the same deterministic sample; do not
invent scores from its published aggregate. `more_like_prior.py prepare` freezes two title/year-only calls
per selected case, `run` is dry without `--spend`, and `merge` writes the resulting scores into a new private
ruler export:

```sh
python3 tools/rulers/more_like_prior.py prepare --source /private/ruler.json --work /private/prior
python3 tools/rulers/more_like_prior.py run --work /private/prior --out /private/prior/answers.jsonl
# inspect the ceiling, then repeat run with --spend
python3 tools/rulers/more_like_prior.py merge --source /private/ruler.json --work /private/prior \
  --answers /private/prior/answers.jsonl --out /private/ruler-with-prior.json
```

Preparation selects exactly 512 cases by a fixed hash of `pairId`. It joins current Wikipedia articles,
uses the lead and extractor-selected story sections, caps each work's evidence equally, blinds positive and
negative as candidate A/B, and writes exact state, question, input, and selection hashes. MovieLens
title/year values remain in the ruler for the prior arm; article state uses the current catalogue title/year
for the same stable key, so harmless alias or metadata corrections do not invalidate a case:

```sh
python3 tools/rulers/more_like_gate.py prepare \
  --ruler /private/path/step9d-export.json \
  --articles out-repass/articles.jsonl \
  --enriched-dir out-repass/enriched \
  --evidence-overrides /private/path/evidence-overrides.json \
  --work /private/path/more-like-gate

python3 tools/rulers/more_like_gate.py run \
  --work /private/path/more-like-gate \
  --out /private/path/more-like-gate/answers.jsonl \
  --max-spend-usd 1
```

The second command is the dry run. Read its exact call and state-character counts before replacing it with
`--spend`. The paid path is pinned to `jev-1.13.0`, resumes append-only, and defaults to a hard $1 spend
cap. Before the first request it reserves the entire remaining run at a deliberately pessimistic ceiling:
every UTF-8 byte of each exact JSON
request counted as one token, plus 1,024 tokens for the provider wrapper (the measured fixed component was
about 265). Usage returned by completed calls is stored per row and counted again after a resume. The dry plan
reports whether all remaining calls fit even if every one reaches that ceiling; raise the explicit cap only
after reading that number. `--workers` may parallelize calls without weakening the whole-run reservation;
`--env` may point to the operator's `den.env` without copying its key into any gate artifact.

Each call carries one anchor and two blinded candidates. Jev returns six bounded Nouls and one bounded
Choice per candidate; it generates no prose. The overall Noul is preregistered as the only ranking score.
The other five axes and the verdict are diagnostics and cannot be tuned into a better result after the run.

```sh
python3 tools/rulers/more_like_gate.py score \
  --work /private/path/more-like-gate \
  --answers /private/path/more-like-gate/answers.jsonl
```

The gate passes only when the pair-bootstrap 95% lower bound is at least 0.70 for article AUC, above zero
for article minus title-and-year, and above zero for article minus plot. A failure means no full precompute.
A pass only makes that precompute eligible: `rail-eval`, `rail-ab`, row-shape cases, movies/TV reporting, and
deterministic fallback remain required. The gate itself measures movies with at least 50 MovieLens likes;
it establishes nothing about TV or the long tail.

Older article dumps contain every heading as `sections`, but not the extractor's chosen story headings as
`plotSections`. `prepare` reconstructs the latter from the newest retained enriched batch whose article
and language exactly match the frozen prose, and hashes the metadata and source-batch identity into the
preregistration. This differs deliberately from classify's newest-wins join: a later grounding may name a
different article, whose headings cannot describe the old dump. Preparation also refuses a selected
heading that no longer exists in the dumped revision. Refresh such a row from the exact dumped revision
without a model and pass a private `jev-more-like-evidence-overrides-v1` file containing `rows` with `key`,
`article`, `language`, `articleRevId`, and the refreshed `plotSections`; identity and revision must match.
The overrides file hash is part of evidence provenance. Silently sending only the lead, or mixing headings
from different article text, is not equivalent evidence.

## Compact-card arm

`tools/rulers/more_like_card.py` prices cheaper evidence on the same pairs. A card is a title's premise tags,
its plot facets under the store's own publication gates, its genres & moods, and its article lead with no
story sections. It is built once per title. `prepare` takes pairs, blinding, and title/year from a frozen
story-arm work directory, and it refuses an article whose text differs from the one the story arm sent. The
questions are the story arm's. The work directory is therefore an ordinary gate plan: `more_like_gate.py
run` and `score` run it unchanged, and `compare` reports the paired card-minus-story AUC and tokens per call.

```sh
python3 tools/rulers/more_like_card.py prepare --story-work /private/path/more-like-gate \
  --articles out-repass/articles.jsonl --premise-tags data/premise-tags-v2.json \
  --genres-moods out-repass/genres-moods.json --corpus out-repass/corpus-<ver>.jsonl.gz \
  --work /private/path/more-like-card
python3 tools/rulers/more_like_card.py compare --card-work /private/path/more-like-card \
  --card-answers /private/path/card-answers.jsonl --story-work /private/path/more-like-gate \
  --story-answers /private/path/more-like-gate/answers.jsonl
```
