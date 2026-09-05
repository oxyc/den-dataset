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

## What's in the shipped dataset

Measured from `out-t02/` (taxonomy `t02`), the corpus currently published as `data-latest`. Re-verified
2026-09-05 against the enriched batches on disk — **57,715 records, 38,460 with a wiki plot** — so this
table is current:

| | Movies | TV series | Total |
|---|---:|---:|---:|
| **Shipped** (in `labels-t02.json`) | 33,641 | 3,892 | **37,533** |
| Enriched (TMDB + Wikipedia fetched) | 49,883 | 7,832 | 57,715 |
| — of those, with a Wikipedia plot | 34,018 (68.2%) | 4,442 (56.7%) | 38,460 (66.6%) |
| — with no plot found | 15,865 | 3,390 | 19,255 |

2,329 shipped titles are animated (a *format* flag, not a genre — see DT-C).

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
  `enrich-ids`, `escalation`, `assemble`, `embed-corpus`, `finalize`, `metadata`, `score`, `recluster`).
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
- `labels-<tax>.json.gz` — gzip of the labels blob (via `/usr/bin/gzip`).
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

## How retrieval actually works — and the four things that must stay true

Read this before changing the pipeline. Each rule below is here because breaking it produced a bug that
shipped and was not noticed for weeks, in most cases because the artifact still looked healthy.

### There are TWO indexes, and the plot one is not the primary
- **`vectors-bge-m3.bin`** — the whole Wikipedia plot, embedded. Whole-story surface similarity.
- **`vectors-premise.bin`** — DT-H. Open-vocab premise/trope tags (`reassigned-phone-number`,
  `wise-mentor-sacrifices-himself`) generated by Claude Sonnet from the plots, embedded with the same model.

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
