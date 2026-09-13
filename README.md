# den-dataset

The **dataset producer** for Den's discovery index — extracted from the Den tvOS app so it can run and
evolve independently. It builds the shipped artifacts (a labels JSON + an int8 vector blob + a manifest). It
has **no dependency on DenKit**: it carries its own copies of the small shared types and a thin TMDB client.
The only coupling to the app is the artifact **format**.

**FP-2 (current):** movie/TV enrichment prose is sourced **live from Wikipedia** (Wikidata SPARQL → article →
plot section, ToS-clean) rather than shipping TMDB overviews, and embeddings come from the **`den-embed`**
service (**bge-m3**, 1024-dim int8) — the single embedding path shared with the app's live search queries, so
corpus and query vectors are comparable. The offline FNV embedder remains as a `--embedder fnv` fallback. See
[`docs/OPERATE.md`](docs/OPERATE.md) for the full re-embed + incremental-top-up runbooks and the alignment rule.

## The published artifacts, and which job each does

`data-latest` is a **moving release**: every publish clobbers its assets, so there is exactly one live
dataset and `dataset.meta.json` describes it. Current: **`c85c707b0b18`**, built by `den-embed/5.1.1` at
`max_tokens 1024`.

| blob | what it is | job |
|---|---|---|
| `labels-t02.json` | 38,532 titles × primaryGenre + subgenres + moods + `animated` | filtering, taste, hide-lists |
| `vectors-bge-m3.bin` | the **main index** — Wikidata facts + our tags + the Wikipedia plot | semantic **search**, neighbours, rows |
| `vectors-premise.bin` | the **premise index** — embedded structural tags, no proper nouns | **similar / recommend** |
| `facets.bin` | country / language / year facets | attribute search |
| `metadata-<ver>.json` | tmdbId → title + posterPath + year | rendering a card with no TMDB call |
| `facts-<ver>.json` | CC0 Wikidata facts per title | `/recommend` ranking; covers titles with no labels at all |

**The main index carries no TMDB Content.** It was rebuilt in September 2026: the embedded document has no
title, no year, no cast and no director, and its director/genre clauses come from Wikidata rather than TMDB.
The filename is historical — `finalize` names the blob after the embedder, not after the doc shape.

Removing the entity clauses did NOT cost search quality, which was the surprise. Measured over 12 queries
against both shapes: the old names-intact doc mostly matched words in the TITLE — "grief" returned *Good
Grief*, *Mourning Grave*, *The Grudge*; "heist gone wrong" returned films with "Heist" in the name. The
current doc returns *Mass*, *The Days of Abandonment*, *Vortex* and *The Lavender Hill Mob*, *Quick Change*,
*Takers*. Person queries ("tilda swinton") failed on BOTH — a 2-3 token name was never carried by an
850-token document — and are answered by the title and person lanes, which is where they belong.

**The premise index is not a lesser copy of the plot index.** It embeds LLM-generated structural tags
(`heist-gone-wrong`, `messages-to-the-dead`) rather than prose, its generation spec forbade proper nouns and
genre/mood words, and it **beats the plot index at recommendation by +11.3 pp on a sealed test half**
(p < 0.05 under two-judge unanimity — `scripts/v2/README.md`). Mean |cos| between the two spaces is 0.43, so
they encode genuinely different things. That same ban on proper nouns is why premise **cannot** serve
character search. One index per job, rather than one index reused for both.

The tags behind that index, the Wikipedia plots everything derives from, and the evaluation rulers are
committed under [`data/`](data/README.md) — read that before re-deriving any of it.

## What's in the shipped dataset

| | Movies | TV series | Total |
|---|---:|---:|---:|
| **Shipped** (in `labels-t02.json`) | 34,066 | 4,466 | **38,532** |
| Enriched (TMDB + Wikipedia fetched) | 49,883 | 7,832 | 57,715 |
| — of those, with a Wikipedia plot | 34,018 (68.2%) | 4,442 (56.7%) | 38,460 (66.6%) |
| — with no plot found | 15,865 | 3,390 | 19,255 |

2,469 shipped titles are animated (a *format* flag, not a genre — see DT-C).

**Why 57,715 enriched becomes 37,533 shipped.** 19,255 of the 20,182 dropped titles have no Wikipedia
plot. Those were classified from the TMDB overview *prose*, which TMDB's terms forbid us deriving from,
so `assemble --require-wiki-plot` drops them from the index entirely rather than shipping labels we are
not entitled to. That single rule accounts for 95.4% of the gap. The remaining 927 are titles that *do*
have a plot and still did not ship — 519 of them because of the media-id collision described below.

**The universe these are drawn from.** TMDB's daily exports list 1,216,343 movie ids and 225,504 TV
series ids. The enrichment worklist is not that universe: it is the ids Den already ships, ordered by
TMDB popularity, and bounded by a vote floor (default 50) — a title with almost no votes has no plot
worth classifying and would be re-billed daily until it earned some. So the numbers above are "of what
we chose to enrich", not "of everything TMDB knows about".

**Known gap:** 940 TV series share a TMDB id with a shipped movie (Buffy/Armageddon, Doctor Who, Star
Trek), and all 940 are missing because the classify checkpoint keyed on a bare id. The keying is fixed;
recovering them in the data needs a re-run, and only the 519 with a Wikipedia plot are recoverable —
the other 421 would be dropped by the ToS rule regardless.

## Layout

- `Sources/DenDataset/` — the library: the calibrated `TaxonomyClassifier`, the `t02` `Taxonomy`, the
  `TaxonomyScorer` + `GoldenSet`, the `HashingEmbedder` + `Quantizer`, the format + producer model types, the
  baked `GroundingKeywords` map, and a thin `TMDBClient` (two endpoints only).
- `Sources/taxonomy-backfill/` — the CLI that drives the resumable phases (`worklist`, `enrich`,
  `enrich-ids`, `escalation`, `assemble`, `embed-corpus`, `doc-facts`, `facts`, `finalize`, `metadata`,
  `score`, `recluster`).
- `Tests/DenDatasetTests/` — golden (embedder/quantizer determinism), conformance (artifact format), and a
  fixture-based end-to-end smoke test (no TMDB, no network).

## Build / test

```sh
swift build
swift test
```

## The tool — phases

```
taxonomy-backfill worklist  --mode discover|export|delta --media movie|tv [--count N] --out <path>
taxonomy-backfill enrich    --worklist <path> [--limit 150] --out-dir <dir>
taxonomy-backfill escalation --batch-id <n> --out-dir <dir>
taxonomy-backfill assemble  --batch-id <n> --out-dir <dir>
taxonomy-backfill finalize  --out-dir <dir>
taxonomy-backfill metadata  --out-dir <dir> [--skip-fetch]   # the poster sidecar; after EVERY finalize
taxonomy-backfill score     --labels labels-t02.json --golden golden.json [--gate]
```

`worklist`/`enrich`/`enrich-ids` hit TMDB and need `TMDB_API_KEY`. The per-title labels come from Haiku
subagents (the vote files under `out/votes/`), not an in-process LLM key. `assemble` runs the calibrated
aggregation + embeds + quantizes; `finalize` writes the shipped artifacts.

## `finalize` outputs

- `labels-<tax>.json` — the derived labels (no raw TMDB text; asserted).
- `labels-<tax>.json.gz` — gzip of the labels blob. `scripts/publish-dataset.sh` regenerates it, and the
  premise-labels and metadata variants, from the blobs on every publish (see its step 0).
- `vectors-bge-m3.bin` — `[int32 count][int32 dim]` little-endian header + `count × dim` int8 rows (dim 1024
  for the bge-m3 build; `--embedding-version` overrides the label for an FNV run).
- `dataset.meta.json` — the manifest the server reads (dataset version, hashes, byte counts, timestamps).
- `report.json` — coverage + primary-genre distribution + confidence histogram.

`datasetVersion` = first 12 hex of `sha256(labelsSha256 + ":" + vectorsSha256)`.
Quantization is `int8-symmetric-x127` (L2-normalized floats × 127, clamped to [-127, 127]).

## What the data actually IS — read this first

Every item here has been misunderstood at least once, usually more than once, by someone who had already
read this file. They are stated as measurements so they can be re-checked rather than re-argued.

**Plots are RAW Wikipedia prose. Nothing has ever summarised them.** An LLM reads plots to produce
*labels* (and, separately, the premise tags below) — it never rewrites the plot text. Measured over
`out-t02`, `hasWikiPlot=true` overviews run **median 2,537 chars, p90 4,327, max 53,299** (n = 38,460). A summariser
would leave a tight band, not a 53k outlier. Titles with `hasWikiPlot=false` sit at median 238 chars —
that is the **TMDB overview**, which is a different thing wearing the same field name.

**`labels-t02.json` and `vectors-bge-m3.bin` cover the IDENTICAL set of ids.** Verified:
`set(labels ids) == set(vectors ids)`, 37,533 each. So "has no vector" and "has no labels" are the same
population, not two overlapping gaps — a title outside the index has *no local semantic signal at all*,
only its TMDB overview. That is **20,182 of the 57,715 enriched rows (35%)**.

**`labels-premise.json` is a byte-for-byte copy of `labels-t02.json`.** The premise index's value is not
in its labels file; it is **`vectors-premise.bin`**, a genuinely separate embedding space (measured mean
|cos| 0.43 against the plot vectors for the same titles). Reading only the labels file and concluding
"premise adds nothing" is the specific mistake this paragraph exists to prevent.

**The premise index is 219 rows short of the plot index** (37,314 vs 37,533) and those rows are NOT
missing work — they are computed and unmerged, sitting in `out-t02/v2/vectors/vectors-coverage-fill.bin`
+ `keys-coverage-fill.json`. *The Dark Knight* is one of them. The live gap is derived from the index by
`build_tag_batches.py --scope uncovered`, which must select 0 after a merge; **do not** trust
`premise-tags-wip/missing.json`, which is stale DT-H-era state.

**`maxTokens: 0` in `index/embedder.json` means "the service was too old to report it"** — NOT "there was
no limit". `assertDocFits` returns early on it, so the guard is inert against the shipped store. What the
shipped vectors were actually truncated at is unknown.

**There are TWO den-embed instances and they differ.** The container answering *query* traffic runs
`max_tokens 512`; `embed-corpus-run.sh` boots its **own** container at 1024 (lines 56, 86). Embedding the
corpus against the query service is the failure mode to guard — see rule 2 below for what it costs.

**`datasetVersion` is a content hash** (`sha256(labelsSha:vectorsSha)`), not a semantic version. It moves
whenever content moves, which is what drives client re-sync.

**`topCast` is only 4 names deep.** Any rule needing two shared cast members between titles returns
essentially nothing outside a franchise.

## Running the Haiku classification — and the two ways it silently fails

`enrich` writes batches; nothing in this repo can classify them. Labels come from a Claude Code run over
`DT-classification-prompt.md` (in the den repo), writing `out-t02/votes/batch-<id>-pass<n>.json`, which
`assemble` then aggregates. Both failure modes below were hit in one session, and neither is visible to any
check that was in place at the time.

### Failure 1: a batch can be structurally perfect and still worthless

A run of 999 titles produced, for 11 of 18 batches: valid JSON, exact record counts, real tmdbIds, every
label in-vocabulary, zero fabrications — and **46-70% of titles with no subgenre at all**, one batch with no
moods whatsoever. Density by batch ran 0.35-0.97 subgenres/title against a careful reference run's **1.77**.

Nothing caught it. Counts matched, checksums matched, the JSON parsed. It surfaced only because one title
(*Blake's 7*) appeared in both a validation slice and a production batch and came back
`Science Fiction / Sci-Fi Action / Dystopian` in one and `Drama / nothing` in the other. *Star Trek* had
likewise become plain `Drama`.

So **density is a gate, not a statistic**. Refuse any batch below ~1.2 subgenres/title or above 25% empty.
The fix that worked: smaller slices (20 titles, not 60) and a prompt section stating the expected density
outright — that a reference run averages 1.77 subgenres and 2.09 moods, that repeated empty arrays mean the
plots are being under-read, and that thin runs are rejected. The redo came back at **1.94 / 2.21 with 5%
empty**.

### Failure 2: fabricated ids are invisible to every count

A prior run had Haiku invent tmdbIds in 3 of 12 batches **with correct row counts**. A fabricated id attaches
one title's labels to another; no count, checksum or schema check can see it. So a batch containing even one
is refused **whole** rather than partially salvaged.

`scripts/check-votes.py out-t02` runs all of the below; `--batch N` for one. It reads the vocabulary from
the SHIPPED labels rather than a hardcoded copy, so it cannot drift from the taxonomy. Run it before
`assemble`.

### The checks worth keeping, in order

1. Count in == count out, same order.
2. No tmdbId absent from the input batch (fabrication) and none missing.
3. Every label in the vocabulary — and note the three lists are SEPARATE. Observed confusions: `Adventure`
   and `Mystery` (primary genres) used as subgenres, `Dark Comedy` (a subgenre) filed under moods. Strip
   them; an invalid label is unusable anyway.
4. **Density** — the gate above.
5. Spot-check titles you personally know. This is what caught Failure 1 and is not optional.

### One batch id per batch, and never reuse one

Vote passes are keyed by batch id, so writing a new batch over an existing id leaves the OLD votes on disk
pointing at the new titles. Assembling that pairs each title with a stranger's labels, silently. `enrich`
now takes the highest batch on disk as its floor and refuses to overwrite an existing batch file — see
`EnrichedBatches` — but if you build batches by hand, keep one media type per batch too: vote records carry
a bare `tmdbId`, and movie/TV ids overlap.

## What this pipeline has taught, the hard way

Each of these cost real time to learn and is cheap to re-learn wrongly.

**Close the vocabulary.** The single highest-leverage finding in the project. Asking an LLM for
open-vocabulary tags gives ~11% agreement between two runs of the same model on the same plot;
asking it to pick from a closed list gives 97.5-100%, with 6 off-vocabulary values in 5,400.
Voting cannot rescue an open vocabulary — a tag must be *named identically* twice to survive, so
2-of-3 voting DELETED content (15.22 tags/title down to 6.34). With a closed list the vote picks
a winner and never empties a slot.

**A closed vocabulary is also what makes errors catchable.** Six independent agents typed a tone
word (`bleak`) into the `ending` axis. Every one was caught, because `bleak` is not in `ending`'s
list and a validator could say so. An open vocabulary would have shipped all six silently.

**Say what to do, not what to avoid.** Listing forbidden words did not stop the `bleak` error.
"Ask how it RESOLVED, not how it FELT" did.

**A correct row count proves nothing.** Observed in this pipeline: fabricated TMDB ids with exact
counts, a duplicated key silently dropping another title, a key-shift where one title carried its
neighbour's tags, and one title dropped by two independent agents on two independent passes.
Verify keys element-by-element against the input, never by length.

**Batch size is a correctness parameter, not a tuning one.** A 100-id SPARQL batch that returns in
~1 s from one client hung to a 60 s timeout from another; 25 advanced steadily.

**Checkpoint what was paid for, and check what the resume actually skips.** A facts scrape froze at
16,500 titles through 22 restarts. It was not rate limiting: an entity-resolution pass ran over the
WHOLE accumulated checkpoint on every restart — 92,036 names, minutes of work redone — so a resumed
run never reached a new batch. Resumability is not just "write as you go"; it is "do not redo what
you already have".

**An artifact whose source text is not committed will be described wrongly.** The premise index was
called stale and TMDB-derived twice in one session, by someone reading file sizes, because its tags
and spec lived in an uncommitted working directory. They are in [`data/`](data/README.md) now.

**Measure before extrapolating from the first sample.** A rate read off the first minute of a run
projected 50 hours for work that took 3; a token estimate from one agent was 40% under. Both were
sampled during a cold start.

## The TMDB rule, in one place

**Nothing TMDB-sourced ships except posters, ids and titles.** Everything else comes from Wikipedia (plot
text), an LLM over that text (labels, premise tags), or Wikidata (facts).

That rule is easy to break by accident because TMDB fields travel inside files whose names suggest
otherwise. Two that have already caught people:

- `overview` in the enriched batches is the **Wikipedia plot** when `hasWikiPlot` is true and the **TMDB
  overview** when it is false. Same field, two sources — which is why `assemble --require-wiki-plot` exists.
- The v2 Wikipedia corpus carried `genres` and `voteCount`, both TMDB. They are stripped in the committed
  copy under `data/`.

`out-t02/enriched/` is TMDB Content and must never be published. It is also cheap to refetch with a key,
which is why it is not committed.

## How retrieval actually works — and the four things that must stay true

Read this before changing the pipeline. Each rule below is here because breaking it produced a bug that
shipped and was not noticed for weeks, in most cases because the artifact still looked healthy.

### There are TWO indexes, and they answer different questions
- **`vectors-bge-m3.bin`** — the Wikipedia plot plus Wikidata facts and our tags, embedded. Carries no
  proper nouns of its own beyond what the plot prose names, since title/year/cast/director were removed.
- **`vectors-premise.bin`** — DT-H. Open-vocab premise/trope tags (`reassigned-phone-number`,
  `wise-mentor-sacrifices-himself`) generated by an LLM from the plots, embedded with the same model. The
  tags themselves are committed at [`data/premise-tags-v1.json`](data/premise-tags-v1.json) and the prompt
  that produced them at `data/premise-tags-v1.SPEC.md`.

The app's "More Like This" uses the **premise index as the primary signal**, with the plot index as a
plot-agreement bonus. That ordering was earned: on a 1,526-title bake-off, premise-tags scored **12/12** on
premise discrimination against raw plot's 8/12, and abstracting to the t02 controlled vocabulary scored
**5/12 — worse than raw plot**. Do not "improve" the plot index by making it more abstract; that experiment
was run and lost. The two indexes are complements.

The Sonnet tag strings are frozen at `out-t02/premise-tags-wip/tags-raw.json` (37,314 titles), so the premise
index can be re-embedded any time for zero LLM cost.

### 1. Corpus and query vectors must come from the same embedder
int8 dot products are only meaningful between vectors from the same model *and the same runtime*. den-embed
reports a **`vector_epoch`** that moves only when its output moves (an ONNX Runtime upgrade, a model change,
a pooling change) — deliberately not its crate version, or a log-line fix would invalidate 37.5k titles.
`embedderRuntime` / `embedderMaxTokens` in the manifest record what built the corpus, and the producer
refuses to append a different embedder to an existing store.

`embeddingModel` + `dims` are NOT sufficient: every generation reports `bge-m3` and `1024`, including the two
that return different vectors for the same text. That is exactly how the current violation went unseen — the
shipped corpus was embedded by the **Python** service on ORT 1.22 and is queried through the Rust one on 1.28
(see `tickets/FP-5` in the den repo).

### 2. The token cap is most of what the vector sees
Plot is **~87%** of the composed document by length (median 93%); facts and tags are ~204 chars. den-embed
truncates at `max_tokens` **server-side, silently** — no error, no field in the response.

Re-measured 2026-09-05 against the real plot lengths, which also pins down which metric these numbers use:

| cap | titles truncated | plot kept, per-title mean | plot kept, **total corpus text** |
|---|---|---|---|
| 512 tokens | 64% | 68% | **50%** |
| 1024 tokens | 29% | 95% | 90% |

The per-title mean is the flattering view — short plots keep 100% and lift the average. For a retrieval
index the total-text column is the honest one: at 512 the corpus loses **half its plot prose**, and what it
loses is the long plots, where the detail that distinguishes two similar titles lives.

`assertDocFits` refuses up front rather than letting the service quietly halve a document. Do not raise the
plot cap without raising the service's token cap in the same change, and vice versa.

### 3. No Wikipedia plot ⇒ the title does not ship
A title with no Wikipedia plot was classified from the **TMDB overview prose**, which TMDB's terms forbid us
deriving from, so `assemble --require-wiki-plot` drops it entirely. This is **95.4% of everything the
pipeline discards** — a deliberate policy, not attrition.

Two consequences that are easy to trip over:
- `finalize`'s ship guard is literally `s.contains("overview")`. A future field carrying prose under another
  name (`summary`, `synopsis`) sails straight into the shipped artifact. Widen the guard if you add one.
- 19,255 enriched rows carry TMDB prose in `overview`. Any pass that feeds `overview` to an LLM **must** gate
  on `hasWikiPlot`, or it sends TMDB Content to an AI application — barred by TMDb §1.C.

### 4. A bare TMDB id is ambiguous — movie and TV namespaces OVERLAP
tv 95 is Buffy the Vampire Slayer; movie 95 is Armageddon. Every per-title map must be keyed
`"movie:123"` / `"tv:123"`. This has now bitten in **three** places:

- `ClassifyCheckpoint` — 940 series enriched, paid for, then silently never classified.
- the Haiku **vote generation** upstream of it, which inherited the same checkpoint.
- `wk-confirmed.json` — one file per out-dir, both media sharing the dir.

`ClassifyCheckpoint.key(media, id)` is the shared helper. If you add a map keyed by title, use it.

### How you would know any of this broke
Two rulers now answer that, built by `scripts/v2/` and described in
`den/tickets/artifacts/2026-09-04-index-v2-rulers.md`. DT-G's *"the one thing still missing is DATA, not
code"* is closed.

- **Co-rating agreement** — MovieLens ml-32m nPMI pairs, joined to 86.5% of shipped movies and **0% of
  series** (ml-32m carries no TV ids, permanently). Behavioural, eval-only, never bundled.
- **Premise triplets** — anchor / positive / negative, blind-judged, kept only on judge unanimity.

Both are split DEV/TEST by `scripts/v2/split.py`, a pure hash of the key so a title lands in the same half
in either ruler. **TEST is for gate runs only.** Sweep on DEV, pre-register the setting in a commit, then
read TEST once. `score_triplets.py` announces a TEST run and `sweep_arm_fusion.py` refuses one.

DT-G's rule still stands and is now enforced in code rather than in prose: *a tie fails too, because
replacing a working system carries its own risk.* Two independent accuracies cannot decide that — use
`paired_triplets.py`, which discards the cases both arms agree on and tests only the discordant ones.
Measured: at ~150 triplets, one flipped case moves a rate by 0.66 pp, so a one-point lead is noise.

### Running an LLM phase
- **Coverage is checked against the manifest id-set, never against `ls out/`.** A batch that wrote the
  wrong ids must fail, not count.
- **A correct row count proves nothing.** Models invent ids that look plausible and duplicate one key while
  dropping another, both of which leave the count right. Measured on a bake-off arm: Haiku fabricated ids in
  3 of 12 batches and duplicated a key in a 4th, reporting success every time.
- **Batch size is a property of the model, not of the task.** 40 titles fits Haiku's 64,000-token output
  cap; Sonnet overran it and wrote an *empty file*, which reads as unrun rather than over-asked. Sizes live
  per-arm in `build_bakeoff.py`, and changing one after it has outputs renumbers the batches out from under
  them — the builder refuses.
- **A subagent's output file is not readable until its completion notification arrives.** Reading a
  half-written file produced three wrong claims in one session, each of which had to be retracted.
- **A batch can die of deliberation, and that failure is silent.** A judging worker spent its entire
  64,000-token output budget on internal reasoning — 63,999 thinking tokens, no answer — and wrote nothing.
  Because the file is simply absent it is indistinguishable from a batch nobody ran, so `--list-missing`
  re-queues it forever and every coverage check agrees the phase is merely incomplete. **The only signal is
  wall-clock**: a stalled batch outruns every completed one. Four batches of one phase went this way. When a
  batch is well past its siblings' duration, check the agent rather than waiting; the prompts for
  judgement-heavy tasks now carry an explicit "decide on the plain reading, do not deliberate" warning, and
  that warning belongs in the prompt file rather than in a dispatch message so it survives the next run.
