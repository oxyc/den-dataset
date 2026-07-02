# Operating the dataset producer (FP-2)

The producer builds Den's discovery index: **derived labels** (`labels-<tax>.json`) + an **int8 vector blob**
(`vectors-<embed>.bin`) + a manifest (`dataset.meta.json`). FP-2 changed two things about how it's built:

- **Enrichment prose comes from Wikipedia**, live, not from TMDB overviews. TMDB still supplies the facts
  (title, year, genres, keywords, credits); the *plot* the classifier reads and the embedder embeds is the
  live English-Wikipedia plot section (ToS-clean). A title with no Wikipedia plot is still processed on
  facts + tags — it is never skipped.
- **Embeddings come from `den-embed` (bge-m3, 1024-dim int8)**, not the old lexical FNV embedder. The *same*
  service embeds the corpus here and live search queries in the app, so their int8 vectors are comparable.
  The int8 quantization lives in the service and nowhere else — the producer stores the service's vector
  verbatim.

## The alignment rule (do not break this)

The corpus and every live query MUST embed through the **same `den-embed` model/version**. bge-m3 int8 dot
products are only meaningful between vectors from the same model. If you re-embed the corpus with a new model,
the app must point its query embedder at the same one. `dataset.meta.json.embeddingModel` + `dims` are how the
app detects a mismatch and re-syncs (FP-1 keys the on-device index on those two fields).

## Full 30k re-embed (MacBook)

Both TMDB and Wikipedia are hit live; `den-embed` must be running.

```sh
# 0. Boot the embedding service (first run downloads the ~560 MB bge-m3 ONNX model, then stays warm).
cd ~/Projects/Personal/den-embed && DEN_EMBED_PORT=8791 bash run.sh        # serves :8791
#    (health check: curl -s localhost:8791/health  ->  {"status":"ok","model":"bge-m3","dims":1024})

# 1. TMDB key (enrichment only). Source it into the env; never print it.
set -a; . ~/Projects/Personal/den/.env; set +a                            # exports TMDB_API_KEY

cd ~/Projects/Personal/den-dataset && swift build -c release
BIN=.build/release/taxonomy-backfill

# 2. Worklist — the universe (daily-export for the full run, or a discover seed for a pilot).
$BIN worklist --mode export --media movie --file movie_ids.json --out out/worklist-movie.json

# 3. Enrich — TMDB detail+keywords+credits, then ONE Wikidata SPARQL + live Wikipedia plot per surviving id.
#    The Wikipedia plot REPLACES the TMDB overview where found (re-grounding); reports wikiPlot vs tagsOnly.
#    Loop until "remaining":0.
$BIN enrich --worklist out/worklist-movie.json --out-dir out --limit 150

# 4. [Agent] Haiku vote passes over each scratch batch -> out/votes/batch-<id>-pass<N>.json
#    (Opus orchestrates the subagents; see DT-classification-prompt.md. Escalate the hard cases with
#    `$BIN escalation --batch-id <id> --out-dir out` before pass 2/3.)

# 5. Assemble — compose(facts + classified tags + Wikipedia plot) -> den-embed -> int8[1024]; append to index.
export DEN_EMBED_URL=http://127.0.0.1:8791     # default; set if the service is elsewhere
$BIN assemble --batch-id <id> --out-dir out    # per batch (default embedder = den-embed)

# 6. Finalize — index store -> labels-t01.json + vectors-bge-m3.bin + dataset.meta.json (+ gzip + report).
$BIN finalize --out-dir out

# 7. Publish — the moving `data-latest` GitHub release the app + den-atlas both fetch.
scripts/publish-dataset.sh out
```

`assemble --embedder fnv` falls back to the offline FNV embedder (float → local int8) for a network-free run;
`finalize --embedding-version <v>` overrides the artifact label. The default path is the bge-m3 build above.

## Incremental top-up (OptiPlex)

Don't re-embed 30k for a handful of new/changed titles:

- **New films**: discover freshly-changed entities with a Wikidata `schema:dateModified` filter *on the
  entity* (bound in the SPARQL WHERE), enrich just those ids, and assemble/finalize as an additive batch.
- **Changed plots**: a title needs re-embedding only when its Wikipedia article changed — track the article
  `revid` (`action=parse&prop=revid`) and re-enrich + re-assemble (`assemble --force`) the ids whose revid
  moved. `finalize` de-dups by `(mediaType, tmdbId)` keeping the newest record + its aligned vector.

Re-embed the changed ids through the **same** `den-embed` service the full run used (the alignment rule).

## Optional: Wikimedia Enterprise plots

If `WIKIMEDIA_ENTERPRISE_TOKEN` is set, `enrich` uses the Enterprise structured-contents endpoint for the
pre-sectioned plot (fewer requests, cleaner sections) and falls back to the public action API on any miss.
Unset, it uses the public action API only (`action=parse`), which is what the standard run above uses.
