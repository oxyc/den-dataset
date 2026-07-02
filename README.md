# den-dataset

The **dataset producer** for Den's discovery index — extracted from the Den tvOS app so it can run and
evolve independently. It builds the shipped artifacts (a labels JSON + an int8 vector blob + a manifest) with
the **same** calibrated classifier, FNV-1a embedder, and int8 quantizer the app was built with, so a re-run
is byte-compatible. It has **no dependency on DenKit**: it carries its own copies of the small shared types
and a thin TMDB client. The only coupling to the app is the artifact **format**.

## Layout

- `Sources/DenDataset/` — the library: the calibrated `TaxonomyClassifier`, the `t01` `Taxonomy`, the
  `TaxonomyScorer` + `GoldenSet`, the `HashingEmbedder` + `Quantizer`, the format + producer model types, the
  baked `GroundingKeywords` map, and a thin `TMDBClient` (two endpoints only).
- `Sources/taxonomy-backfill/` — the CLI that drives the 7 resumable phases.
- `Tests/DenDatasetTests/` — golden (embedder/quantizer determinism), conformance (artifact format), and a
  fixture-based end-to-end smoke test (no TMDB, no network).

## Build / test

```sh
swift build
swift test
```

## The tool — phases

```
taxonomy-backfill worklist  --mode discover|export --media movie|tv [--count N] --out <path>
taxonomy-backfill enrich    --worklist <path> [--limit 150] --out-dir <dir>
taxonomy-backfill escalation --batch-id <n> --out-dir <dir>
taxonomy-backfill assemble  --batch-id <n> --out-dir <dir>
taxonomy-backfill finalize  --out-dir <dir>
taxonomy-backfill score     --labels labels-t01.json --golden golden.json [--gate]
```

`worklist`/`enrich`/`enrich-ids` hit TMDB and need `TMDB_API_KEY`. The per-title labels come from Haiku
subagents (the vote files under `out/votes/`), not an in-process LLM key. `assemble` runs the calibrated
aggregation + embeds + quantizes; `finalize` writes the shipped artifacts.

## `finalize` outputs

- `labels-<tax>.json` — the derived labels (no raw TMDB text; asserted).
- `labels-<tax>.json.gz` — gzip of the labels blob (via `/usr/bin/gzip`).
- `vectors-e02.bin` — `[int32 count][int32 dim]` little-endian header + `count × dim` int8 rows.
- `dataset.meta.json` — the manifest the server reads (dataset version, hashes, byte counts, timestamps).
- `report.json` — coverage + primary-genre distribution + confidence histogram.

`datasetVersion` = first 12 hex of `sha256(labelsSha256 + ":" + vectorsSha256)`.
Quantization is `int8-symmetric-x127` (L2-normalized floats × 127, clamped to [-127, 127]).
