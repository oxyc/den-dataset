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

`data-latest` carries **two files** (oxyc/den#113):

| blob | records | bytes | what it is | read by |
|---|---|---|---|---|
| `den-<ver>.store` | 47,618 | 135M | EVERYTHING: facts, labels, cards, facets, rail facets, the entity table, alias titles, and both vector matrices as sections | den-atlas, which mmaps it |
| `dataset.meta.json` | — | <1K | the descriptor: version, embedder, dims, count, quantization, and the store's name/sha/size/rows | den-atlas |

The corpus ships separately, under its own `corpus-<ver>` tag, and is deliberately NOT in the serving
manifest — `fetch-dataset.sh` pulls every `*File` key, so naming it there would make the box download 44 MB
it never reads:

| blob | records | bytes | what it is | read by |
|---|---|---|---|---|
| `corpus-<ver>.jsonl.gz` | 47,529 | 40.4M | **source of truth** — every per-title signal, one JSON object per line | humans; the store build |
| `corpus-<ver>-entities.json.gz` | 162,812 | 3.6M | Q-id → name for every entity the corpus references | humans; the store build |

**What was published until 2026-09, and is not any more.** `vectors-bge-m3.bin`, `vectors-premise.bin`,
`labels-t02.json`, `labels-premise.json`, `metadata-<ver>.json`, `facets.bin`, `facts-<ver>.json`,
`rail-facets-<ver>.json`, `plot-facets-<ver>.json`, every `.gz` twin. The store carries what they held.
They are still BUILT — they are the store's inputs and they stay in the out-dir — but `publish-dataset.sh`
prunes their keys out of the manifest, so nothing fetches them. The exception is `metadata-<ver>.json`, the
TMDB poster sidecar: nothing builds it any more, because posters are no longer fetched from TMDB at all. They were all one row per title, keyed
identically, and nothing checked they agreed: eleven titles (House of the Dragon and Moon Knight among
them) sat in `facts` and `labels` but not in `rail-facets` for a day, with no error anywhere. `facts-slim`
is separately retired: it dropped `composers`, `cinematographers`, `narrativeLocations` and `mainSubjects`
while saving only 17%.

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
published** before this file — the rail-facets producer dropped them (it is deleted; the store carries
them now). The four `scores` axes (intensity,
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
so the shipped generation dropped them from the index entirely rather than shipping labels we are not
entitled to. That single rule accounts for 95.4% of the gap. The remaining 927 are titles that *do*
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

- `pipeline/` — the pipeline, in order (`pipeline/__init__.py`). One module per stage, each declaring what
  it reads and writes; `./den stages` prints it.
- `lib/` — what a stage needs from outside the machine: HTTP with retry, the response cache, and the
  upstream clients.
- `Sources/DenDataset/` — the library the remaining Swift phases still need: the `t02` `Taxonomy`, the
  `HashingEmbedder` + `Quantizer`, the format + producer model types, and a thin `TMDBClient`.
- `Sources/taxonomy-backfill/` — the CLI that drives the phases not ported yet (`enrich`, `embed-corpus`,
  `facts`, `finalize`, `recluster`).
- `Tests/DenDatasetTests/` — golden (embedder/quantizer determinism), conformance (artifact format), and a
  fixture-based end-to-end smoke test (no TMDB, no network).

## Build / test

```sh
swift build
swift test
```

## The tool — phases

```
./den stage worklist  --mode discover|export|delta --out-dir <dir> --dataset-version <ver>
./den stage fetch     --out-dir <dir> --dataset-version <ver> [--media movie|tv]
./den stage articles  --out-dir <dir> --dataset-version <ver>
./den stage classify  --out-dir <dir> --dataset-version <ver> [--plan]
./den stage docfacts  --out-dir <dir> --dataset-version <ver>
taxonomy-backfill embed-corpus  --out-dir <dir> --labels labels-t02.json [--doc-facts …]
taxonomy-backfill finalize      --out-dir <dir>
```

Or `./den run`, which is the whole order — see `./den stages`.

The worklist builds BOTH media in one call: `enrich` refuses a list that mixes them, so it writes one per
media rather than taking a `--media`. It and the drain hit TMDB and need `TMDB_API_KEY`. The per-title
labels and facets come from the `classify` stage — the decision-only pass in `scripts/v2/run_combined.py`,
which reads the dumped articles and writes the `combined-v1-r2*.jsonl` shards the corpus join consumes.
`embed-corpus` composes and embeds those already-decided labels; `finalize` writes the shipped artifacts.
Label quality is scored by `scripts/eval-taxonomy.py`, which CI runs and which gates a publish.

## `finalize` outputs

These are now INPUTS to the store build, not published artifacts (see the table above). They stay in the
out-dir; `publish-dataset.sh` prunes their keys out of the manifest.

- `labels-<tax>.json` — the derived labels (no raw TMDB text; asserted). Also what the publish-time quality
  gate scores against the golden set — it reads this file by name, not from the manifest.
- `vectors-bge-m3.bin` — a `DENVEC02` blob: magic, little-endian `[u32 count][u32 dim]`, a `u64` key per row
  (`(media << 32) | tmdbId`, media 0 = movie), then `count × dim` int8 rows (dim 1024 for the bge-m3 build;
  `--embedding-version` overrides the label for an FNV run). The keys are the format's point: row order used
  to live only in `labels-<tax>.json`'s record order, so a regenerated labels file moved every vector onto
  the wrong title with nothing able to see it. Layout and rationale: `Sources/DenDataset/VectorBlob.swift`
  and `scripts/v2/vector_blob.py`. An older blob is converted, never re-embedded, by
  `scripts/v2/migrate_vector_blob.py` — den-embed's output differs by build host, so re-running it changes
  the values and not just the layout.
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

## The licence rule, in one place

**The rule: the published store carries no third-party licensed content — only identifiers, and facts we
or a free source produced.** Plot text comes from Wikipedia, labels and premise tags from an LLM over that
text, and the facts from Wikidata.

**Not yet true, and tracked in oxyc/den#118.** Three columns still carry TMDB data — `card_title`,
`card_year` and `votes` — and are being replaced by Wikidata and the English Wikipedia article title.
`card_poster` was the fourth and **the writer no longer emits it** — den-edge's `/metadata/title/query`
batches posters 100 titles to a request and den-atlas's Stremio metas already carry a metahub `poster`
URL, so it had no replacement to wait for. It is still in the *published* store, and must stay there until
den-atlas stops requiring the section: `cards_from_store` reads it with `?` before building any card, so a
poster-less store empties every plot row and leaves search with no display titles. Until all four land,
this section is the rule and the list is the exception; `LICENSES.md` names them and their counts.

**The rule is enforced in the writer, not just written here.** `scripts/v2/build_store.py` declares the
source of every store section in `PROVENANCE` and refuses to assemble a store whose sections are not
exactly that set — so a new column carrying vendor content cannot be added without the declaration being
edited too. `VENDOR_ALLOWED` is the three columns above and shrinks to nothing when #118 completes.

The rule follows from one property of this artifact: **the store is a public release asset on a public
repo**, so publishing it is redistribution to anyone, not use by us. Both catalogue licences we might have
leaned on say the same thing about that:

- **TMDB** — its terms define TMDb Content broadly and draw no distinction between a rating score and a
  rating count.
- **IMDb** — grants *"a limited, non-exclusive, **non-transferable, non-sublicenseable** license to access
  and make personal and non-commercial use"*. Non-transferable is the operative word: we cannot hand our
  licence to whoever downloads a release asset.

So the choice between vendors never had to be made — neither one's data can live in a public artifact.
What ships is the **IMDb id** for 99.96% of rows: an identifier, not content, and the join key below.

### Popularity: bring your own, in one command

Once `votes` is gone the store will carry no popularity column at all. For personal, non-commercial use
you can join IMDb's own public dataset yourself — it is one file, and our `imdb` column is the key:

```sh
curl -sfLO https://datasets.imdbws.com/title.ratings.tsv.gz    # ~8.6 MB, 1.71 M rows, daily
```

`title.ratings.tsv.gz` is `tconst  averageRating  numVotes`. Join `tconst` against the store's `imdb`
column and you have a vote count for **99.9%** of the corpus (47,562 of 47,618 rows matched on the
2026-09-21 dump). It correlates with TMDB's count at Spearman **0.85** and orders browse rows slightly
better — re-ranking the live rows by it keeps 7–9 of every top 10 and promotes *Heat*, *Chinatown* and
*Amélie* over more recent titles.

Doing the join yourself is what keeps you inside IMDb's licence and us inside ours: you hold your own copy
under your own personal non-commercial use, and nothing licensed passes through this repo. Read
[IMDb's terms](https://www.imdb.com/conditions) before relying on it; commercial use needs a licence from
IMDb directly.

### Where it breaks by accident

A vendor's fields travel inside files whose names suggest otherwise, which is how the old, looser rule
("nothing TMDB-sourced ships except posters, ids and titles") was broken twice without anyone noticing:

- `overview` in the enriched batches is the **Wikipedia plot** when `hasWikiPlot` is true and the **TMDB
  overview** when it is false. Same field, two sources — which is why the no-wiki-plot rule below exists.
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

The only sound check is to embed something whose answer is already known and compare bytes.
[`data/embed-canary.json`](data/embed-canary.json) is that: a handful of fixed texts and the exact int8
vectors den-embed is supposed to return for them. `scripts/v2/embed_canary.py` verifies them, every path
that writes a vector runs it before opening its output, and the verified identity is published in
`dataset.meta.json` as `embeddingSpace` — so a dataset names the space it is in instead of leaving it to be
inferred from a version that cannot express one. The check names no cause, which is the point: the two
differences that have actually bitten were both diagnosed as something else first.

**The live index and live queries are NOT aligned today** — measured cost 6.3/10 top-10 overlap. Current
state and what to do: `docs/OPERATE.md` "The alignment rule". Evidence: oxyc/den-dataset#21. Not restated
here, so there is one copy to keep true.

What the gate still cannot see is the **doc shape** and the **plot cap**. The shipped index is the CC0 lean
shape (`embed-corpus --doc-facts`), and its cap is recorded nowhere. Both differ silently from what a
default `embed-corpus` would compose, so neither is safe to append without establishing them first.

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
deriving from, so it is dropped entirely. This is **95.4% of everything the pipeline discards** — a
deliberate policy, not attrition.

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
read TEST once. Every scorer that can read it — `score_triplets.py`, `score_reco.py`, `paired_triplets.py`
— announces a TEST run on stderr before it prints a number. The sweeps that used to refuse one outright are
retired, so nothing mechanically stops a second read: the seal is the pre-registration commit.

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
