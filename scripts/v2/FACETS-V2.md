# Jev facet census and v2 decision

Measured 2026-09-19 after the Wikipedia re-ground completed. Inputs and outputs are local under
`out-repass/`; they are generated artifacts, not committed data.

## Corpus integrity

- 47,529 article rows, 47,529 unique `(mediaType, tmdbId)` keys
- 47,529 facet rows, 47,529 unique keys
- no malformed rows, duplicates, missing keys, extra keys, or rows with the wrong axis set
- five articles were truncated: four television articles at 25,000 characters and `Blueberry` at 100,000;
  every other title used its whole article
- full-corpus Jev spend: approximately $7.52; the three vocabulary pilots added $0.0976

`validity` flagged 45,735 `single-work`, 1,221 `source-work`, 522 `franchise-overview`, 33
`season-aggregation`, and 18 `not-a-work` rows. These are candidates for review, not 1,794 confirmed grounding
defects: spot checks found ordinary series among the non-single-work labels. V2 tightens the definitions.
Publication must not suppress every flagged title without a validated probability/margin policy.

## Full-corpus v1 census

Entropy is normalized to 0...1 over each Choice distribution. A high value means the model spread probability
across the vocabulary even when it returned an argmax. This table covers every v1 row, including titles that
the same run flagged as non-`single-work`; use the single-work subset for publication thresholds.

| axis | median confidence | median entropy | does not apply | largest value | decision |
|---|---:|---:|---:|---|---|
| era | 0.96 | 0.060 | 0.3% | contemporary 62.1% | keep |
| setting | 0.85 | 0.185 | 2.6% | urban 42.4% | keep |
| scope | 0.71 | 0.350 | 1.0% | single-city 34.5% | keep |
| ending | 0.76 | 0.308 | 1.4% | happy 29.4% | keep |
| pacing | 0.48 | 0.637 | 8.1% | propulsive 29.6% | redesign |
| structure | 0.86 | 0.212 | 3.0% | linear 75.6% | redesign without dropping |
| conflict | 0.74 | 0.342 | 3.4% | person-vs-person 54.0% | keep |
| ensemble | 0.34 | 0.783 | 29.4% | does-not-apply 29.4% | redesign |
| tone | 0.49 | 0.539 | 0.8% | earnest 34.3% | keep, confidence-gated |

The two weak axes fail differently.

**Ensemble is a vocabulary failure.** No-answer stays between 23% and 33% in every useful article-length band,
is 33.0% for movies but 10.6% for TV, and its no-answer runner-up is spread across every option. “How many people
carry it?” conflates cast size, number of named characters, and narrative focus. V2 asks for narrative focus:
`single-lead`, `dual-lead`, `group-led`, `ensemble-led`. One protagonist with many supporting characters is
explicitly `single-lead`.

**Pacing is both an evidence and vocabulary failure.** No-answer is 63.5% below 1,000 article characters, 27.8%
at 1,000–2,999, 4.1% at 3,000–9,999, and 0.9% at 10,000+. The v1 vocabulary also made `episodic` compete with
tempo even though a work can be both episodic and fast. V2 moves `episodic` to continuity and uses only tempo:
`slow-burn`, `measured`, `brisk`, `relentless`.

**Structure is skewed, not low-value.** Among rows flagged `single-work`, v1 still found 2,926
`parallel-strands`, 2,804 `framed`, 2,016 `nonlinear`, 1,013 `single-day`, and 978 `anthology` works. A
proportional 5,000-title candidate set therefore contains roughly 220 nonlinear works and 300 each with a
frame or parallel strands. `nonlinear + dreamlike` is rarer but still identifies 291 works corpus-wide. The
largest normalized mutual information between structure and any other v1 axis is only 0.074 (with pacing),
so it is not redundant.

V2 separates the mixed v1 axis into `chronology` (`linear`, `nonlinear`, `framed`, `parallel-strands`),
`continuity` (`continuous`, `episodic`, `hybrid`, `anthology`), and `timespan` (including `single-day`). Keep
these for explicit query language, compound browsing, and similarity/explanation. Similarity should be
rarity-aware: a shared nonlinear or framed structure is much more informative than a shared linear one.
Whether structure deserves scarce default-chip space is a separate, unmeasured UI question.

## V2 pilot

`build_facet_pilot.py` selected 230 deterministic titles: 80 ensemble failures, 60 pacing failures split by
article length, 70 clean movie/TV controls, and 20 validity controls. A first $0.0418 run was superseded after
an audit found that its global boundary instructions never reached the API questions.

The corrected 12-axis run wrote 230/230 rows with zero failures, 1,145,858 input tokens, and $0.0481 spend.
Every row carries the exact question/prompt/article/state hashes, article revision, model alias, and run start.

| corrected-pilot measurement | median confidence | median entropy | does not apply |
|---|---:|---:|---:|
| ensemble | 0.93 | 0.141 | 2.6% |
| pacing | 0.65 | 0.457 | 64.8% |
| chronology | 0.89 | 0.197 | 9.1% |
| continuity | 0.92 | 0.158 | 0.9% |
| timespan | 0.72 | 0.374 | 30.4% |
| archetype | 0.77 | 0.283 | 22.6% |

The sample is deliberately biased toward failures, so those percentages are not corpus coverage estimates.
On the 70 clean controls, chronology was answerable for 97–100%, continuity for 100%, and timespan for 98% of
movies and 90% of television. Ensemble fixed the vocabulary failure: only 3 of the 80 deliberately selected
old ensemble failures remained unanswered. Pacing did not become broad merely because its vocabulary improved:
59 of 60 selected old pacing failures stayed unanswered, and a human audit found that conservatism preferable
to inventing tempo from sparse or foreign-language prose.

The structural rare-value controls survived a second, 35-title adversarial run: `Ten Canoes` and `Our Times`
were framed at probabilities 1.00 and 0.95; `Brimstone` and `Bloodline` nonlinear at 1.00 and 0.97; and
`Perfect Life` and `Forever` parallel-strands at 0.79 and 0.75. Intersecting stories in `That Christmas` moved
from anthology to hybrid, but 0.38 versus 0.34 is correctly too ambiguous to publish. `Forever` no longer
mistakes one immortal protagonist for a multi-generational story.

That adversarial run also made validity target-aware by putting requested media type and exact title above the
article. `Rent-a-Girlfriend` and `How Not to Summon a Demon Lord` became `source-work` at 0.89 and 0.93, while
the page covering both the Time of EVE ONA and film became `multi-work-overview` at 0.77. The six v2 validity
values are `correct-screen-work`, `source-work`, `other-screen-work`, `multi-work-overview`,
`season-or-episode`, and `not-a-work`.

## Seven-archetype candidate

V2 adds an `archetype` facet inspired by Christopher Booker's seven plots, with neutral labels that avoid genre
collisions: `overcoming-threat`, `rise`, `quest`, `voyage-and-return`, `comic-resolution`, `downfall`, `rebirth`.
`comic-resolution` explicitly means restored order or reconciliation, not comic tone.

The strengthened applicability rule fixed `Late Night with Conan O'Brien`, returning `does-not-apply` at 0.80.
It still assigned the documentary `Killer Inside: The Mind of Aaron Hernandez` to `downfall` at 0.76 versus
0.24 no-answer despite an explicit mandatory documentary exclusion. That is decisive: more wording is not a
publication guard. Collect archetype because its marginal cost is tiny, but publish it only behind a separate
deterministic content-type/applicability gate. Confidence alone cannot repair a confident category error.

The facet does not save the cost of acquiring or reading plots. It is cheap because the article state is already
paid for and one additional typed question adds little response time and no billed output.

## Pass-two contract

Do not buy a standalone full-corpus rerun just for these changes. Before a full pass, build a separate typed
combined runner: `run_facets.py` is deliberately Choice-only and cannot safely store Nouls and Scores. The
runner must hash the exact questions, model and article revision/content, validate every typed response,
exit nonzero when incomplete, and refuse to mix incompatible resumes.

`jev-latest` is a mutable provider alias. The runner records it and the UTC run start, but that cannot prove
that two calls months apart used identical weights. A full resumable corpus pass therefore also requires a
provider-resolved model/build id (recorded per response or pinned in `--model`) if TypeSafe exposes one. A
short pilot may use the alias as one bounded run; the limitation must not be hidden by the question hash.

The next corpus call should combine:

1. the twelve v2 facets, including the three structural axes and experimental `archetype`;
2. target-aware `validity` and an explicit narrative/content applicability decision;
3. primary genre, subgenres, and moods;
4. the planned score questions;
5. one typed role decision per section heading.

The section audit is the protection against embedding contamination. The whole article remains Jev's state,
but the lead (synthetic section 0) and every heading get a stable positional id. Store heading, duplicate
occurrence, text span/hash, and the full role distribution over story/premise, theme/subject, work context, and
irrelevant production/reception/navigation prose. Diff stable ids/hashes—not heading text alone—against the
extractor's recorded `plotSections`. Only high-confidence, high-margin disagreements may trigger automatic
re-extraction; ambiguous cases keep the current text and enter review.

Embedding and Haiku need separate selection policies. Embedding takes high-confidence story sections first,
then separately qualified theme/subject evidence under its fixed token budget; it must not use story probability
alone to discard themes. Haiku may receive a larger selected evidence set for free-form premise generation.
Preserve source order after selecting sections. The corpus contains 404,291 lead/section decisions (mean 8.51,
median 7 per article), so this fits naturally beside the shared article state.

Five known oversized articles need a fallback because a question must never refer to a section body absent from
the state. The combined runner must send section-level or grouped-section states for the residual content and
record that state plan; silently truncating the article while asking about unseen headings is invalid. Arbitrary
paragraph selection remains a targeted fallback for an oversized selected section after section-level error and
truncation are measured.

Store full distributions and identify the versioned question set on every row. Confidence-gate soft axes;
review validity flags rather than blanket-suppressing them; and retain rare structural matches as strongly
weighted signals instead of treating the dominant `linear` value as equally informative.

### Launch implementation and measured plan

`run_combined.py` implements that contract separately from the deliberately Choice-only facet runner. Its
default is the immutable `jev-1.13.0`; a mutable `*-latest` alias is rejected unless explicitly allowed. The
sidecar manifest stores and hashes the exact global questions, section-question template, and label mapping;
it also hashes the article artifact, enriched evidence, facet prompt, live Swift taxonomy, requested model,
state planner, and the runner/planner/question/client source files. Each result records the
model returned by the provider and per-call state/question hashes and token usage. Resume refuses a changed
manifest, malformed/duplicate rows, or output keys not present in the input.
Any exhausted API retry, typed-response violation, or pinned-model mismatch opens a shared circuit breaker:
no worker begins another paid call, and the process exits nonzero after at most the calls already in flight.
The paid path also holds a nonblocking kernel lock for the output's lifetime, so a second agent cannot read the
same resume set and duplicate the calls or append duplicate rows.

The current call has 94 global decisions: 12 facets, target-aware validity, narrative applicability, primary
genre, 75 independent Nouls for the current 19 subgenres + 40 themes + 16 moods, and four Scores. The 13
regional labels stay metadata-derived. Each lead/heading adds one four-way Choice whose full distribution is
over story/premise, theme/subject, work context, and irrelevant production/reception/navigation prose.

The frozen article dump predates two fields needed by this audit: target year and the extractor's
`plotSections`. The runner therefore requires `--enriched-dir out-repass/enriched` for that dump, folds batches
newest-first exactly like the Swift readers, and hashes the effective evidence. Future `dump-articles` rows
write `year`, `plotSections`, and `extractorArticleRevId` directly. The whole-article revision differs from the
extractor revision for 17,101 rows, and 321 rows name at least one old extractor heading absent from the newer
article. Results record both revision ids, `sectionAuditSameRevision`, and missing headings; those rows may be
measured but cannot drive automatic section repair as if the diff were same-revision.

The no-network full-corpus plan is:

```
47,529 titles             47,535 calls
94 global questions       404,291 section decisions (mean 8.51, max 90)
3 oversized titles        Blueberry + two David Copperfield mappings
```

At the documented ~150k-English-character state envelope, the runner uses a conservative 110,000 serialized
characters. Ordinary titles send the whole article and all questions once. Each oversized title needs two
complete-section audit groups followed by one global call over role-selected evidence; selection preserves
source order and every omitted section id is recorded. A section that cannot fit alone is a hard error rather
than silent paragraph truncation.

The final exact-manifest mixed-primitive smoke classified 3/3 titles with zero failures and a closed circuit;
the provider returned `jev-1.13.0`, and 23,524 input tokens cost $0.0010. The preceding identical-question
throughput sample classified 23/23 with zero failures; 228,511 tokens cost $0.0096 (median 8,947/title), and
its timed 20-title continuation took 7.5 seconds at eight workers, about 160 titles/minute over this small
sample. The full-plan character estimate is 492.8M input tokens / $20.70; extrapolating the smoke with the
corpus's longer mean article puts the likely total around $21–24. Throughput therefore suggests roughly 3–5
hours, but both figures remain planning ranges until the sustained run settles.

The corpus's largest ordinary request has 184 questions (94 global + 90 lead/section roles). A targeted live
capacity smoke on that exact record succeeded with the pinned model in one call: 43,019 input tokens, $0.0018,
zero failures, and no circuit break. This confirms that the provider accepts the maximum request shape rather
than discovering an undocumented count ceiling after the first 1,800 titles.

The first full launch proved the breaker on a benign provider-format edge after 12 calls: Score `3.89` came
with displayed probabilities whose displayed weighted mean was `3.92`. The API rounds those fields
independently. The validator now allows at most 0.051 display-rounding drift and has a regression that accepts
that exact boundary while rejecting a material mismatch. The 11 successful rows and manifest remain preserved
in the aborted `combined-v1` artifact; the corrected run uses a fresh output rather than mixing source hashes.

Exact dry run (no key, network, output, or manifest mutation):

```sh
python3 scripts/v2/run_combined.py \
  --articles out-repass/articles.jsonl \
  --enriched-dir out-repass/enriched \
  --out out-repass/combined-v1.jsonl \
  --plan
```

The paid command is the same without `--plan`. Do not launch it until the code, questions, plan, tests, and
smoke artifact have passed the final independent audit.

After the writer exits successfully, independently reconstruct and audit the stored artifact before measuring
or publishing it:

```sh
python3 scripts/v2/audit_combined.py \
  --articles out-repass/articles.jsonl \
  --enriched-dir out-repass/enriched \
  --out out-repass/combined-v1-r2.jsonl
```

This is deliberately a strict completion gate, not a progress reporter. It checks the manifest and frozen input,
requires every input key exactly once, revalidates every typed answer, reconstructs every whole/oversized state and
question hash, and verifies the section spans, revision provenance, model id, and call plan. Only then does it
report token cost, publication-gate coverage, and same-revision high-confidence section disagreements. Running it
against a growing output must fail with the number of rows still absent; use `wc -l` to watch an active writer.

### Token-capacity quarantine and manifested bundles

The full run exposed a provider-capacity edge that serialized character count cannot predict: Japanese text
has far more tokens per character than the English calibration. `tv:96451` (SSSS.DYNAZENON) and `tv:45799`
(K) returned `HTTP 400 max_tokens_exceeded` on the ordinary combined path. The shared breaker stopped both
runs without corrupting or duplicating completed rows.

Never change a frozen runner and append under its old manifest. `resume_combined_excluding.py` instead imports
the exact hashed implementation and changes only the selected record set, like `--only-key`; it requires an
existing manifest and quarantines explicit keys for separately manifested shards. Capacity shards use the
same questions/model/code with a lower state ceiling, which forces complete section-audit groups followed by
one role-selected global call. No text is silently truncated and no question refers to an unseen section.

Because each shard truthfully retains its own run/config provenance, final completeness is a bundle property.
Validate the exact, disjoint 47,529-key union before deriving or publishing anything:

```sh
python3 scripts/v2/audit_combined_bundle.py \
  --articles out-repass/articles.jsonl \
  --enriched-dir out-repass/enriched \
  --out out-repass/combined-v1-r2.jsonl \
  --out out-repass/combined-v1-r2-token-fallback.jsonl \
  --out out-repass/combined-v1-r2-token-fallback-2.jsonl
```

The bundle audit validates every shard against its own manifest and source artifact, proves each shard source
record is semantically identical to the canonical full input after enriched evidence is attached, rejects
cross-shard duplicates, requires exact full-corpus coverage, and records output/manifest/source hashes. The
ordinary `audit_combined.py` remains strict for a single complete artifact.

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
