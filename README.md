# den-dataset

The **dataset producer** for Den's discovery index — extracted from the Den tvOS app so it can run and
evolve independently. It builds the shipped artifacts (a labels JSON + an int8 vector blob + a manifest). It
has **no dependency on DenKit**: it carries its own copies of the small shared types and a thin TMDB client.
The only coupling to the app is the artifact **format**.

Enrichment prose is sourced **live from Wikipedia** (Wikidata SPARQL → article → plot section, ToS-clean)
rather than shipping TMDB overviews, and embeddings come from the **`den-embed`** service (**bge-m3**,
1024-dim int8). The offline FNV embedder remains as a `--embedder fnv` fallback.

- [`docs/OPERATE.md`](docs/OPERATE.md) — **current state and how to run it**: the alignment rule, the
  re-embed and incremental-top-up procedures. Start there for anything you intend to execute.
- [`docs/LESSONS.md`](docs/LESSONS.md) — what this pipeline has taught the hard way, and why the procedure
  is shaped the way it is. Read before changing how classification or tagging works.
- [`data/README.md`](data/README.md) — the committed tags, plots and evaluation rulers.
- [`LICENSES.md`](LICENSES.md) — **per asset**, because they differ: the code is MIT, and the Wikipedia
  article text is CC BY-SA 4.0 and published on its own tag.

This file is reference: what the artifacts are and what is in them.

## The published artifacts, and which job each does

`data-latest` is a **moving release**: every publish clobbers its assets, so there is exactly one live
dataset and `dataset.meta.json` describes it. Current: **`5b1c3213b6a1`**. What built it, and whether that
still matches the serving embedder, is in `docs/OPERATE.md` — not repeated here.

Only what `dataset.meta.json` NAMES is published. A file the manifest does not declare has no hash, no
record count and no producer, so no publish guard can see it — it is skipped rather than uploaded.

| blob | records | raw | served | what it is | read by |
|---|---|---|---|---|---|
| `corpus-<ver>.jsonl.gz` | 47,529 | — | 40.4M | **source of truth** — every per-title signal, one JSON object per line | humans; the store build |
| `corpus-<ver>-entities.json.gz` | 162,812 | — | 3.6M | Q-id → name for every entity the corpus references | humans; the store build |
| `vectors-bge-m3.bin` | 47,539 | 48.7M | 48.7M | the **main index** — Wikidata facts + our tags + the Wikipedia plot | atlas, TV app |
| `vectors-premise.bin` | 38,532 | 45.6M | 45.6M | the **premise index** — embedded structural tags, no proper nouns | atlas |
| `rail-facets-<ver>.json` | 47,529 | 49.9M | 6.0M | 12 narrative facets, `__world`, 75 nouls, 17 critique axes | atlas (More Like This) |
| `facts-<ver>.json` | 47,618 | 43.5M | 10.8M | CC0 Wikidata facts per title | atlas (`/recommend`, people search) |
| `labels-premise.json` | 44,531 | 16.4M | 0.7M | premise tags, the row order `vectors-premise.bin` aligns to | atlas |
| `labels-t02.json` | 47,539 | 11.8M | 0.7M | primaryGenre + subgenres + moods + `animated` | atlas, TV app |
| `metadata-<ver>.json` | 47,539 | 5.9M | 1.8M | tmdbId → title + posterPath + year | atlas (a card with no TMDB call) |
| `plot-facets-<ver>.json` | 5,336 | 1.6M | 0.1M | per-title plot facets | atlas |
| `facets.bin` | 47,539 | 0.6M | 0.6M | country / language / year facets | atlas (attribute search) |

**Where this is going.** The nine per-title artifacts below the corpus are being replaced by it: they are
all one row per title, keyed identically, and nothing checked they agreed — which is how eleven titles
(House of the Dragon and Moon Knight among them) sat in `facts` and `labels` but not in `rail-facets` for a
day, with no error anywhere. The end state is the corpus JSONL as the inspectable source of truth, a
**generated binary store** that den-atlas mmaps, and the two vector blobs flat. `facts-slim` is retired: it
dropped `composers`, `cinematographers`, `narrativeLocations` and `mainSubjects` while saving only 17%.

### A corpus row

One line per title, `sort_keys`, no source prose. Model answers are kept whole — every probability and
confidence — so a later ranker can use a signal this one did not anticipate without a re-run.

```jsonc
{
  "key": "tv:1399", "mediaType": "tv", "tmdbId": 1399,
  "facts":     { "directors": [...], "creators": [...], "cast": [...], "composers": [...],
                 "cinematographers": [...], "genres": [...], "basedOn": [...], ... },  // CC0 Wikidata
  "labels":    { "primaryGenre": ..., "subgenres": [...], "moods": [...] },
  "premiseLabels": { ... },
  "applicability": { "validity": {...}, "narrative_applicability": {...} },
  "facets":    { "era": {...}, "setting": {...}, "ensemble": {...}, ... },   // the 12 axes
  "scores":    { "intensity": {...}, "humour": {...}, "emotional_weight": {...}, "complexity": {...} },
  "nouls":     { "theme__epic": {...}, "mood__dark_gritty": {...}, ... },    // the 75 taxonomy nouls
  "critique":  { "institution": {...}, "the-state": {...}, ... },            // the 17 axes
  "technique": { "live_action": {...}, "anime": {...}, ... },
  "depicts":   { ... },
  "audience":  { "intended_to_frighten": {...}, "made_for_children": {...} }
}
```

`scores`, `technique`, `depicts` and `audience` are computed by the model passes and were **never
published** before this file — `build_rail_facets.py` dropped them. The four `scores` axes (intensity,
humour, emotional weight, complexity) are the closest thing the dataset has to a register signal.

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

**Why 57,715 enriched becomes 38,532 shipped.** 19,255 of the 20,182 dropped titles have no Wikipedia
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
`set(labels ids) == set(vectors ids)`, 38,532 each. So "has no vector" and "has no labels" are the same
population, not two overlapping gaps — a title outside the index has *no local semantic signal at all*,
only its TMDB overview. That is **20,182 of the 57,715 enriched rows (35%)**.

**`labels-premise.json` is a byte-for-byte copy of `labels-t02.json`.** The premise index's value is not
in its labels file; it is **`vectors-premise.bin`**, a genuinely separate embedding space (measured mean
|cos| 0.43 against the plot vectors for the same titles). Reading only the labels file and concluding
"premise adds nothing" is the specific mistake this paragraph exists to prevent.

**The premise index covers the same 38,532 ids as the plot index.** It was 219 rows short until the DT-N
coverage-fill was merged (*The Dark Knight* was one of them). Derive the live gap from the index with
`build_tag_batches.py --scope uncovered`, which selects 0 today; **do not** trust
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

The premise tag strings are committed at [`data/premise-tags-v2.json`](data/premise-tags-v2.json) — **44,531
titles**, which is every title the index covers, so it re-embeds from a fresh checkout for zero LLM cost.
That was not true before 2026-09-19: 999 of the strings lived only in a gitignored directory and a rebuild
came up short (#13). `premise-tags-v1.json` (37,533) is kept because `vectors-premise.bin` is aligned to its
exact strings.

### 1. Corpus and query vectors must come from the same embedder
int8 dot products are only meaningful between vectors from the same model *and the same runtime*.

**No identity field can be trusted to tell you whether they are.** `embeddingModel` + `dims` are identical
across every generation (`bge-m3`, `1024`). `vectorEpoch` is meant to move when output moves and has stayed
`1` across generations that return different vectors. `embedderRuntime` records only den-embed's own crate
version, which does not change when what is underneath it does.

The only sound check is to re-embed a sample of a blob and compare bytes; `scripts/v2/embed_premise_v2.py`
does that before reusing anything and refuses on mismatch.

**The live index and live queries are NOT aligned today** — measured cost 6.3/10 top-10 overlap, and the
cause is the build host, not a version. Current state and what to do: `docs/OPERATE.md` "The alignment
rule". Evidence: oxyc/den-dataset#21. Not restated here, so there is one copy to keep true.

What the gate still cannot see is the **doc shape** and the **plot cap**. The shipped index is the CC0 lean
shape (`embed-corpus --doc-facts`), and its cap is recorded nowhere. Both differ silently from what a plain
`assemble` or a default `embed-corpus` would compose, so neither is safe to append without establishing them
first.

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
