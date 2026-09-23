# den-dataset

The producer of Den's discovery dataset: per-title facts, genres & moods, plot facets, and two vector
indexes, built into one store that den-atlas serves. Plots come live from Wikipedia — the title's own
English article first, then its own articles in other languages — facts from
Wikidata, labels and facets from typed model passes over the article, and vectors from the `den-embed`
service (bge-m3, 1024-dim int8).

| Read | For |
|---|---|
| [`docs/OPERATE.md`](docs/OPERATE.md) | building and publishing a generation, the embedder rule, current state |
| [`AGENTS.md`](AGENTS.md) | where each question about the code is answered; the hand enrichment of genres & moods |
| [`docs/LESSONS.md`](docs/LESSONS.md) | why the pipeline is shaped the way it is |
| [`docs/FACETS-V2.md`](docs/FACETS-V2.md) | the classify pass: what it asks, how it is run and audited, what may be published |
| [`data/README.md`](data/README.md) | the committed inputs: tags, vocabularies, evaluation rulers |
| [`LICENSES.md`](LICENSES.md) | licences per asset — the code is MIT, the source text is not |

## What is published

`data-latest` is a moving release: each publish replaces it, so there is one live dataset, currently
**`5b1c3213b6a1`**. Only what `dataset.meta.json` names is uploaded.

| asset | what it is |
|---|---|
| `den-<ver>.store` | everything den-atlas reads — facts, genres & moods, cards, facets, the entity table, alias titles and both vector matrices — 47,618 rows, 133 MB, mmapped |
| `dataset.meta.json` | version, embedder, dims, quantization, and the store's name, hash, size and row count |

The **corpus** ships separately under `corpus-<ver>`, and is kept out of the serving manifest so the box
never downloads it: `corpus-<ver>.jsonl.gz` (47,529 titles, one JSON object per line) is the source of truth
the store is built from, and `corpus-<ver>-entities.json.gz` maps every referenced Wikidata Q-id to a name.
A corpus row keeps every model answer whole — each probability and confidence — so a later ranker can use a
signal nobody anticipated without paying for a rerun:

```jsonc
{
  "key": "tv:1399", "mediaType": "tv", "tmdbId": 1399,
  "facts":     { "directors": [...], "cast": [...], "basedOn": [...], ... },     // Wikidata (CC0)
  "labels":    { "primaryGenre": ..., "subgenres": [...], "moods": [...] },    // genres & moods
  "applicability": { "validity": {...}, "narrative_applicability": {...} },
  "facets":    { "era": {...}, "chronology": {...}, "ensemble": {...}, ... },  // 12 axes
  "scores":    { "intensity": {...}, "humour": {...}, ... },
  "nouls":     { "theme__epic": {...}, "mood__dark_gritty": {...}, ... },
  "critique":  { ... }, "technique": { ... }, "depicts": { ... }, "audience": { ... }
}
```

Until 2026-09 the release carried a dozen separate blobs (labels, vectors, facts, facets). The store replaced
them because nothing checked that those files agreed: eleven titles sat in two of them and not a third for
a day with no error anywhere. They are still built as the store's inputs; the publish prunes their keys.

## Two indexes, two jobs

- **The plot index** embeds the Wikipedia plot with a few Wikidata facts and labels — no title, year, cast
  or director. Removing those did not cost search quality: with names in the document, "grief" matched
  titles containing the word (*Good Grief*); without them it returns *Mass* and *Vortex*. Person queries are
  answered by the title and person lanes, not by a vector.
- **The premise index** embeds LLM-written structural tags (`heist-gone-wrong`, `messages-to-the-dead`),
  generated with proper nouns and genre words forbidden. It is the primary "more like this" signal: it beats
  the plot index by +11.3 pp on a sealed test half, and abstracting plots to the genres & moods vocabulary
  scored *worse* than raw plot. The two spaces are complements (mean |cos| 0.43), not copies.

## What must stay true

- **No Wikipedia plot, no title.** A title without one could only be described from TMDB's overview, which
  TMDB's terms forbid deriving from. Any pass that sends `overview` to a model must gate on `hasWikiPlot`:
  in the enriched batches that field is the Wikipedia plot when true and TMDB's prose when false.
- **Plots are raw Wikipedia prose** — nothing summarises them. Summarising to fit a smaller budget was
  rejected: it drops the proper nouns a dense retriever matches on, and a synopsis is an abridgement under
  CC BY-SA.
- **Every per-title key is `movie:123` / `tv:123`.** The two TMDB id spaces overlap (tv 95 is *Buffy*,
  movie 95 is *Armageddon*); a bare-id checkpoint once silently skipped 940 series.
- **Nothing licensed ships.** `pipeline/build_store.py` declares the source of every store section and
  refuses a vendor-sourced one; `finalize`'s `SHIP_GUARD` refuses prose fields by name, so a new prose field
  under a new name must be added to it. Details in `LICENSES.md`.
- **Corpus and queries use the same embedder** — `docs/OPERATE.md`, "The alignment rule".

## Layout

- `den` — the entry point: `./den stages`, `./den stage <name>`, `./den run`.
- `pipeline/` — one module per stage, in the order `pipeline/__init__.py` gives, each declaring what it
  reads and writes; beside them the passes and tools those stages run: the classify and delta passes,
  their auditor, the corpus join, the embed box kit.
- `store/` — the store writer, one module per section group of den-spec's `wire/store-v1.md`.
- `lib/` — HTTP, the response cache, and the TMDB, Wikidata, Wikipedia and Jev clients.
- `data/` — committed inputs. `guards/` — CI checks on the tree itself.
- `tools/` — standalone tools with dependencies the pipeline does not take: the plot translator (torch) and
  the recommendation-quality rulers (numpy, scipy).
- `scripts/` — the publisher and its guards, the shell runners, and tools not yet moved into a package
  (oxyc/den-dataset#73).

## Build / test

Nothing to build. The tests are the steps in `.github/workflows/ci.yml` — `python3 -m unittest` over each
suite it names, and `bash scripts/publish-dataset.test.sh` — on Python 3.12. The stages need 3.11 or newer;
`./den` refuses older.
