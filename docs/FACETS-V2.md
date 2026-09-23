# The classify pass — plot facets, applicability and genres & moods from Jev

The `classify` stage asks Jev (TypeSafe's decision-only model) one typed request per title, with the whole
Wikipedia article as the state. It ran once over the corpus on 2026-09-19: 47,529 titles, `jev-1.13.0`,
**$20.47**, audited at exact coverage. Those shards (`out-repass/combined-v1-r2*.jsonl`) are the paid record;
a rerun buys it all again, so treat them as source data.

## What it asks

94 global questions per title, plus one per article section:

- **12 plot facets** (Choice): era, setting, scope, ending, pacing, chronology, continuity, timespan,
  conflict, ensemble, tone, archetype.
- **`validity`** — is this article about the requested work (`correct-screen-work`, `source-work`,
  `other-screen-work`, `multi-work-overview`, `season-or-episode`, `not-a-work`) — and
  **`narrative_applicability`**.
- **`primary_genre`**, and **75 independent Nouls** for the 19 subgenres, 40 themes and 16 moods in
  `data/genres-moods-vocabulary.json`. The 13 regional labels come from metadata, not the model.
- **4 Scores**: intensity, humour, emotional weight, complexity.
- **A role Choice per lead/section heading**: story/premise, theme/subject, work context, or irrelevant
  production/reception prose. This is the guard against embedding contamination: later passes (the
  `genres_moods` stage among them) pick story sections by the model's reading rather than by heading text.

Every answer is stored with its full distribution, so thresholds can move without buying anything again.

## Why the facet vocabulary looks like this

A v1 pass (nine axes) was censused over the whole corpus before this one was bought. What it changed:

- **Ensemble was a vocabulary failure** — 29% no-answer, spread over every option, because "how many
  people carry it" mixed cast size with narrative focus. v2 asks narrative focus: `single-lead`,
  `dual-lead`, `group-led`, `ensemble-led`. On the pilot's 80 old failures, 3 stayed unanswered.
- **Pacing is an evidence failure as much as a vocabulary one.** No-answer ran 63.5% on articles under 1,000
  characters and 0.9% over 10,000. v2 keeps tempo only (`slow-burn`, `measured`, `brisk`, `relentless`) and
  moves `episodic` to continuity; it still abstains on thin articles, which a human audit preferred to
  invented tempo.
- **Structure was skewed, not useless** — 76% `linear`, but thousands of framed, nonlinear and
  parallel-strand works, and nearly independent of every other axis. v2 splits it into `chronology`,
  `continuity` and `timespan`. Similarity should weight a shared rare structure far above a shared `linear`.
- **`archetype`** (seven Booker-style plots, neutral names) is collected because one more question is
  nearly free, and published only behind a separate applicability gate: the pilot put a documentary at
  `downfall` 0.76 despite an explicit exclusion. More wording is not a publication guard.
- **`validity` is target-aware** (the requested media type and title sit above the article), and its flags
  are candidates for review, not deletions: spot checks found ordinary series among them.

## Running it

```sh
./den stage articles --out-dir out            # the article dump it reads
./den stage classify --out-dir out --plan     # the call and cost plan; buys nothing
./den stage classify --out-dir out            # buys; resumes, so a repeat buys only what is missing
```

The stage runs `run_combined.py` with the files its declaration names. The same run typed by hand, which
`pipeline/classify_test.py` holds the stage to:

```sh
python3 pipeline/run_combined.py \
  --articles out-repass/articles.jsonl \
  --enriched-dir out-repass/enriched \
  --out out-repass/combined-v1.jsonl \
  --plan
```

What the runner guarantees, and why each matters when every call is paid:

- **A pinned model.** A mutable `*-latest` alias is refused unless explicitly allowed: an alias cannot
  prove two calls months apart used the same weights.
- **A manifest per shard** hashing the questions, prompt (`data/prompts/facets-v2.md`), vocabulary, inputs and
  the runner's own source files. Resume refuses a changed manifest, so two configurations never share a
  file. Moving or editing one of those source files therefore needs an entry in
  `data/implementation-lineage.json` saying why no answer moved.
- **A circuit breaker.** Any exhausted retry, typed-response violation or model mismatch stops every
  worker from starting another paid call.
- **A kernel lock on the output**, so a second agent cannot resume the same set and pay twice.
- **No silent truncation.** An oversized article is split into complete section groups plus one global
  call over role-selected evidence; a section that cannot fit alone is an error.

After the writer exits, audit before anything reads the result. `audit_combined.py` checks one complete
shard; the corpus as it exists is three disjoint shards, because two Japanese articles (`tv:96451`,
`tv:45799`) exceeded the provider's token limit at a character count the English calibration allowed.
They were quarantined with `resume_combined_excluding.py` and rerun under a lower state ceiling, same
questions and code. Completeness is therefore a property of the bundle:

```sh
python3 pipeline/audit_combined_bundle.py \
  --articles out-repass/articles.jsonl \
  --enriched-dir out-repass/enriched \
  --out out-repass/combined-v1-r2.jsonl \
  --out out-repass/combined-v1-r2-token-fallback.jsonl \
  --out out-repass/combined-v1-r2-token-fallback-2.jsonl
```

It fails on a growing output; watch an active writer with `wc -l`.

**What costs.** Jev bills question text as input at the article's rate. Of the pass's 487M input tokens,
~110M were article and **~377M were question text** (oxyc/den-dataset#56). The planner's estimate,
`(state chars + question chars) / 4` at the model's price, came to $20.70 against $20.47 spent for that
reason. Question
definitions are the cost to cut, not the article.

## Publication gates

The pilots support collection, not unconditional argmax publication.

1. Accept only complete rows matching the stored article, state, question, prompt, and model provenance.
2. Publish plot facets only when `validity=correct-screen-work` has probability at least 0.80. Quarantine other
   rows for repair/review; never delete them automatically.
3. Suppress plot facets for talk, variety, game, news, or reality programs without a bounded narrative.
   Suppress archetype additionally for documentaries, anthologies, open-ended series, and multi-arc works.
   Implement this from deterministic metadata/content type or a separate typed applicability result, not from
   archetype's own no-answer probability.
4. For every published value, require probability at least 0.70 and a top-minus-runner-up margin of at least
   0.25. Exclude `does-not-apply` and `ending=unknown`.
5. Use chronology, continuity, and timespan in query language and compound browsing. In similarity, rare
   structural matches must weigh much more than `linear` or `continuous`; default chip placement remains a UI
   experiment, not a consequence of corpus prevalence.

`store/facets.py` applies clauses 2–4 when the store is written; the published manifest's `facetGates`
counts what each clause withheld, per axis.

### The tentative tier

A value clause 4 refuses only for its probability is still the model's best guess, and a filter that
drops it loses most of an axis's minority values (oxyc/den-atlas#35). Such a value ships in the store's
separate `facet_tv` / `facet_tp` sections (den-spec `wire/store-v2.md`), never in `facet_v`, when it is
the distribution's strict argmax at probability 0.50 or more. `scope` has no tentative tier, and nor does
`tone=clinical`. A reader lists these after the published values and says they are tentative. Clauses 2
and 3, `does-not-apply` and `ending=unknown` are not uncertainty and give no tentative value.

The floor and the two exclusions come from a blind grading of 216 tentative values on the 2,000 most-voted
films and series, graded from knowledge of the works:

| probability | defensible | exact |
|---|---|---|
| 0.40–0.50 | 60% (29/48) | 40% |
| 0.50–0.60 | 71% (60/84) | 56% |
| 0.60–0.70 | 80% (67/84) | 65% |
| ≥ 0.50 without `scope` and `tone=clinical` | 80% (120/150) | 65% |

`scope` graded 7 of 14 at or above 0.50. `tone=clinical` was wrong on all five sampled titles. The
published tier grades 9–10 of 10 by the same method. `facetGates` counts the tentative values per axis.
