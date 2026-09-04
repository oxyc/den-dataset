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

Measured from `out-t02/` (taxonomy `t02`), the corpus currently published as `data-latest`:

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
