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

`embeddingModel` + `dims` are NOT enough on their own, and were the whole reason this rule went unenforced:
every generation of den-embed reports `bge-m3` and `1024`, including the two that return different vectors
for the same text (ORT 1.22 → 1.28 moved int8 output, and the Rust rewrite added a 512-token truncation the
Python service never had). So the manifest also carries `embedderRuntime` + `embedderMaxTokens`, taken from
the service's own `/health` at build time, and the corpus build refuses to append to a store that a
different embedder created. **The corpus shipping today was built by the Python/ORT-1.22 generation** and is
queried through the current one — that is a real, known violation, and the only remedy is a full re-embed.

## Full re-embed (MacBook) — the shipped 37.5k-title corpus

Both TMDB and Wikipedia are hit live; `den-embed` must be running for step 5 (not for plot-finding).

A re-embed at the defaults reproduces the current documents. The shipped corpus was built by `assemble`,
whose `--plot-cap` has been 1500 since it was introduced and was never overridden; 1500 plot chars plus
~300 of facts is ~450 tokens, inside den-embed's 512 default. So the only difference between the old corpus
and a new one is the ONNX Runtime version — which is exactly what `vector_epoch` records.

`embed-corpus` still refuses when a plot cap would not fit the service's token cap, because den-embed
truncates server-side and says nothing. Its ceiling is 1024 tokens (peak RSS 1219 MB against a 1536 MB
limit), so a longer cap needs both settings raised together.

```sh
# 0. Boot the embedding service. Run the PUBLISHED CONTAINER, not a local build — the model is pinned inside
#    the image, so the corpus is embedded by exactly the runtime that serves queries. (The old `bash run.sh`
#    here booted a Python service that was deleted in the Rust rewrite at 5cf9e72.)
podman run -d --rm --name den-embed -p 127.0.0.1:8791:8080 -e DEN_EMBED_HOST=0.0.0.0 \
    ghcr.io/oxyc/den-embed:latest     # -d: the remaining steps run in this same terminal
#    Health is a CONSTANT — it answers ok while the model is missing and every /embed 500s. Probe the real
#    thing instead:
#    curl -fsS -H 'content-type: application/json' -d '{"text":"probe"}' localhost:8791/embed | head -c 80
#
#    Or skip step 0 and 5 entirely and let scripts/embed-corpus-run.sh manage the container for you.

# 1. Secrets — copy the template and fill it (gitignored via *.env). The run wrapper sources this.
cd ~/Projects/Personal/den-dataset
swift build -c release && BIN=.build/release/taxonomy-backfill
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
#    (Opus orchestrates the subagents; see `tickets/DT-classification-prompt.md` in the **den app** repo. Escalate the hard cases with
#    `$BIN escalation --batch-id <id> --out-dir out` before pass 2/3.)

# 5. Assemble — compose(facts + classified tags + Wikipedia plot) -> den-embed -> int8[1024]; append to index.
export DEN_EMBED_URL=http://127.0.0.1:8791     # default; set if the service is elsewhere
$BIN assemble --batch-id <id> --out-dir out    # per batch (default embedder = den-embed)
#    First run in a fresh out-dir records the service's identity to out/index/embedder.json; later runs
#    refuse if the service no longer matches it. An out-dir with a store but no embedder.json also refuses —
#    what built it is unknown, and guessing is how the corpus/query drift went unnoticed in the first place.

# 6. Finalize — index store -> labels-t02.json + vectors-bge-m3.bin + dataset.meta.json (+ gzip + report).
$BIN finalize --out-dir out

# 7. Metadata sidecar — poster/title/year per shipped id, so a neighbour renders without a TMDB detail call.
#    Its filename carries the datasetVersion, which step 6 just changed, so this belongs after EVERY finalize
#    that adds titles. Skipping it leaves the manifest naming the previous version's sidecar: it still hashes
#    correctly, so both consumers accept it and never re-sync — the new titles render with no poster forever.
$BIN metadata --out-dir out

# 8. Publish — the moving `data-latest` GitHub release the app + den-atlas both fetch.
scripts/publish-dataset.sh out
```

`$BIN` is `.build/release/taxonomy-backfill` (`swift build -c release`).

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
