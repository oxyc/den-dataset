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

## Full re-embed (MacBook) — the ~58k-title universe

Both TMDB and Wikipedia are hit live; `den-embed` must be running for step 5 (not for plot-finding).

```sh
# 0. Boot the embedding service (first run downloads the ~560 MB bge-m3 ONNX model, then stays warm).
cd ~/Projects/Personal/den-embed && DEN_EMBED_PORT=8791 bash run.sh        # serves :8791
#    (health check: curl -s localhost:8791/health  ->  {"status":"ok","model":"bge-m3","dims":1024})

# 1. Secrets — copy the template and fill it (gitignored via *.env). The run wrapper sources this.
cd ~/Projects/Personal/den-dataset
cp den.env.example den.env        # then edit: TMDB_API_KEY (required) + Enterprise username/password (optional)

# 2. Worklist — the universe, ORDERED popularity-desc so we process the titles most likely to have a
#    Wikipedia article first (and can watch the plot-hit rate fall off / pick a stopping point). Build it from
#    the shipped labels (re-embeds exactly what we ship) and sort by TMDB daily-export popularity:
python3 scripts/build-worklist.py        # -> out/worklist-{movie,tv}.json (popularity-sorted)

# 3. Enrich — TMDB detail+keywords+credits, then ONE Wikidata SPARQL + live Wikipedia plot per surviving id.
#    The Wikipedia plot REPLACES the TMDB overview where found (re-grounding); each batch prints wikiPlot vs
#    tagsOnly. The wrapper logs into Enterprise (if creds present) for a fresh 24h token, then runs ONE batch.
#    Resumable via the enrich checkpoint — loop until "remaining":0.
scripts/enrich-run.sh movie 150          # next 150 un-enriched movies; repeat. Then: scripts/enrich-run.sh tv 150
#    (Observed on the popular tier: ~96% wikiPlot hit; the misses are recent/obscure titles with no enwiki article.)

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

Put `WIKIMEDIA_ENTERPRISE_USERNAME` / `WIKIMEDIA_ENTERPRISE_PASSWORD` (a free Enterprise account works) in
`den.env`. `scripts/enrich-run.sh` exchanges them at `https://auth.enterprise.wikimedia.com/v1/login` for a
24h bearer token (`access_token`) and exports it as `WIKIMEDIA_ENTERPRISE_TOKEN`, which `WikipediaSource` uses
to hit the structured-contents endpoint for the pre-sectioned plot (higher rate limit, cleaner prose — the
plot text is the section's `has_parts` paragraphs, joined). Any miss falls back to the public action API.
Leave the two fields blank to use the public `action=parse` API only (same plot coverage, slower). Fetch is
per-article/on-demand — **never** a Wikimedia dump (those are stale + hundreds of GB); the working set is a
few hundred MB total.
