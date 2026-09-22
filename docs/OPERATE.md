# Operating the dataset producer

The producer builds Den's discovery index: **derived labels** (`labels-<tax>.json`) + an **int8 vector blob**
(`vectors-<embed>.bin`) + a manifest (`dataset.meta.json`).

- **Prose comes from Wikipedia**, live, not TMDB overviews. TMDB supplies the facts (title, year, genres,
  keywords, credits); the *plot* the classifier reads and the embedder embeds is the live English-Wikipedia
  plot section (ToS-clean). A title with no Wikipedia plot is processed on facts + tags, never skipped.
- **Embeddings come from `den-embed`** (bge-m3, 1024-dim int8). The producer stores the service's vector
  verbatim; the int8 quantization lives in the service and nowhere else.

## The alignment rule (do not break this)

**The corpus and every live query must come from the same `den-embed`.** int8 dot products are meaningless
between vectors from different ones, and nothing about the result looks wrong when they are — see
`README.md` "How retrieval actually works" for what that costs.

Three things follow, in order of how often they have bitten:

**1. Embed where you serve.** Run the embed against the `den-embed` that answers live queries — the one on
the box — not a local container. This laptop is `arm64`, the box is `x86_64`, and ONNX Runtime picks
architecture-specific kernels:

```
ssh root@pve 'incus exec den -- podman run --rm --network den -v /opt/den/embed:/w:z \
    docker.io/library/python:3.12-slim python3 /w/<script>.py --url http://den-embed:8080'
```

**2. A version string is not evidence — the canary is.** `embeddingModel` + `dims` are identical across
every generation, `vectorEpoch` has stayed `1` across generations whose output moved, and `embedderRuntime`
only records den-embed's own crate version. So `data/embed-canary.json` holds a handful of fixed texts and
the exact int8 vectors the service is supposed to return for them, and **every path that writes a vector
verifies them before opening its output**:

```sh
scripts/v2/embed_canary.py --url http://den-embed:8080      # exit 0, or exit 2 and a report per case
```

Byte-identical is the bar; cosine is reported so a reader of a failure can tell a rounding difference from
a different space, but both fail. The verified identity — `<canarySet>:<digest over the expected vectors>`
— is recorded beside the vectors and stamped into `dataset.meta.json` as `embeddingSpace`, so a published
dataset NAMES the space it is in rather than leaving it to be inferred from a version that cannot express
it. A consumer checks alignment by embedding the same committed texts through its own den-embed.

The check is deliberately cause-agnostic. It does not test for a configuration, an architecture or a
runtime version; it tests the answers, so a cause nobody has thought of yet moves them too.

Regenerating (`--regenerate`, which re-embeds the texts already in the file and never invents one) is
legitimate only when the space is MEANT to move: a `VECTOR_EPOCH` bump, a model change, or a settings
change made together with a full re-embed. It is not a way past a failing check — the `spaceId` is
published, so regenerating to get a run going renames the space the corpus claims to be in.

`embed_premise_v2.py`'s reuse guard stays, and answers the narrower question the canary cannot: whether one
specific base blob, which may predate the canary, is still reproduced by the service about to extend it.

**3. Set `MAX_TOKENS=1024`.** The default is 512 (`env_clamped("MAX_TOKENS", 512, 16, 1024)`, 1024 is the
ceiling). At 512 the corpus loses **half its total plot prose** and 64% of titles truncate — the table in
`README.md` §2 has the measurements. It makes no difference to short documents such as premise tags
(0 dims differ, measured), so it matters for the plot corpus and not the premise index.

This is the setting that has actually caused a space difference, twice, and both times it was diagnosed as
something else first — an instruction-set difference, then a thread-pool setting. Neither was real: an
AVX2 host and an AVX-512 one return byte-identical vectors once their caps match, and ONNX Runtime's
`INTRA_THREADS` makes no difference at all. A comparison run against a container at the image's DEFAULT cap
is comparing caps, whatever else it looks like. The canary refuses on a cap mismatch before it embeds
anything, and says so, rather than failing on the long cases and leaving the pattern to be interpreted.

### Current state

| | |
|---|---|
| serving box | `den-embed/5.1.2`, `dims 1024`, `vector_epoch 1`, `max_tokens 1024` — checked 2026-09-21 |
| the space it serves | `canary-v1:42f4618a055103411edec2ad0f87dfa94f1e7f3d40bf768563e82a0fe692a2df` (7/7 cases byte-identical) |
| live corpus `vectors-bge-m3.bin` | built 2026-09-13, `embedderRuntime den-embed/5.1.1`, `embedderMaxTokens 1024` |
| live `vectors-premise.bin` | built 2026-09-13; **does not reproduce on the box** (462–567 of 1024 dims differ) |
| `out-premise-v2/vectors/vectors-premise-v2.bin` | built on the box 2026-09-19, 44,531 × 1024, reproduces exactly |

Tracked in oxyc/den-dataset#21, which holds the evidence for the 6.3/10 top-10 overlap; do not re-derive it
here. The canary says the box is in the space this repo has answers for; it says **nothing** about the
shipped blobs, which were built before it existed and therefore carry no `embeddingSpace`. Only a corpus
embedded through a run that verified the canary can claim one, which is the point of publishing it.

## Full re-embed

Both TMDB and Wikipedia are hit live; `den-embed` must be running for step 5 (not for plot-finding).

**Run at `MAX_TOKENS=1024` and `--plot-cap 3500`, not at the defaults**, and against the box's service —
see the alignment rule above.

Plot is 87% of the composed document by length (median 93%), so the cap matters. Per title, which is what
retrieval sees:

| | titles truncated | plot text kept, per title |
|---|---:|---:|
| 512 tokens (default) | ~61% | ~73% (median 72%) |
| **1024 tokens** | **~21%** | **~97% (median 100%)** |

Those two rows were measured on the PRE-re-ground corpus and no longer describe this one. The 2026-09
Wikipedia re-ground reads whole per-season plots, which moved the tail hard — p99 7,240 → 10,962 chars,
max 53,299 → 86,443 — while the median barely moved (2,526 → 2,565). Re-measured on `out-repass`
(47,529 grounded titles), against the char cap actually passed rather than the token budget:

| `--plot-cap` | titles truncated | of all plot text, kept |
|---|---:|---:|
| 1500 | 68.1% | 46.3% |
| **3500** (what shipped) | **32.0%** | **82.8%** |
| 7200 (head+tail, the facets clamp) | 2.9% | 95.6% |

So at 1024 tokens **68% of plots now survive uncut, not 79%**. Memory still peaks ~1219 MB against the
1536 MB limit, and the real cost is still wall-clock: `max_request_tokens` is 8192, so `CHUNK` drops from
15 to ~7 and the run takes roughly 2-3x as long. That is still the right trade — and 3500 is close to the
ceiling regardless, since `MAX_TOKENS` is hard-clamped at 1024 (~4,096 chars), so no cap above ~4,000 can
reach the embedder.

One asymmetry worth knowing before changing either: `cappedPlot` keeps the HEAD only, so for the 15,189
truncated titles no ending is embedded — while the facets prompt deliberately keeps a tail because the
`ending` axis depends on it. Defensible for similarity, where setup discriminates more than resolution,
but it is two stages evolving apart rather than a decision anyone made.

Do NOT summarise the plots to fit a smaller budget. It was considered and rejected: a ~1,200-char summary
compresses harder than a 1,844-char truncation (30% of plots are already shorter than the summary target,
and 39% survive truncation uncut), it cannot carry the ~25 distinct proper nouns a median plot uses and a
dense retriever matches on, and a sentence-length synopsis of a Wikipedia plot is an abridgement under
CC BY-SA where the premise tags were deliberately kept terse to avoid exactly that. The arc-level signal is
already covered twice over — the `Themes:` clause precedes `Plot:` and is never truncated, and the premise
index (DT-H) is the primary "More Like This" signal, having beaten raw plot 12/8 on premise discrimination.

**Re-embed `vectors-premise.bin` in the same pass.** It is bge-m3 too, so the epoch change applies to it
identically, and the premise tag strings are frozen on disk — zero LLM cost. Embed from committed
`data/premise-tags-v2.json` (44,531 rows), which is complete: all 999 tags that once lived only in the
gitignored `out-premise-999/tags.json` are in it, verified by set comparison. This used to say a fresh
checkout would come up 999 short. It would not, and `build_premise_worklist.py` no longer refuses to run
without that directory.

**Check the vector against the labels file beside it, not against a tags file.** `vectors-premise.bin` is
aligned to its generation's `labels-premise.json`: `out-repass` is 44,531 rows, the last published
generation 38,532. Neither number is `premise-tags-v1.json`'s 37,533.

**Deploy the env with the corpus.** `maxTokens` is part of the embedder identity, so the serving box must
run den-embed with `MAX_TOKENS=1024` permanently or the manifest and the service will disagree.

`embed-corpus` still refuses when a plot cap would not fit the service's token cap, because den-embed
truncates server-side and says nothing. Its ceiling is 1024 tokens (peak RSS 1219 MB against a 1536 MB
limit), so a longer cap needs both settings raised together.

```sh
# 0. Point at the embedding service. Use the one ON THE BOX — the same instance den-atlas queries. A local
#    container is a DIFFERENT embedder even on the same image tag (arm64 here, x86_64 there); see the
#    alignment rule above. Confirm it before a long run, and confirm MAX_TOKENS took:
ssh root@pve 'incus exec den -- podman run --rm --network den docker.io/curlimages/curl:latest \
    -s http://den-embed:8080/health'   # expect max_tokens 1024 — it is 512 unless the unit sets it
#    Health is a CONSTANT — it answers ok while the model is missing and every /embed 500s, so probe
#    /embed/batch with a real string rather than trusting /health.

# 1. Secrets — copy the template and fill it (gitignored via *.env). The run wrapper sources this.
cd ~/Projects/Personal/den-dataset
swift build -c release && BIN=.build/release/taxonomy-backfill
cp den.env.example den.env        # then edit: TMDB_API_KEY (required) + Enterprise username/password (optional)

# 2. Worklist — the universe, ORDERED popularity-desc so we process the titles most likely to have a
#    Wikipedia article first (and can watch the plot-hit rate fall off / pick a stopping point). Build it from
#    the shipped labels (re-embeds exactly what we ship) and sort by TMDB daily-export popularity:
python3 scripts/build-worklist.py        # -> out/worklist-{movie,tv}.json (popularity-sorted)
#    THIS IS THE RE-EMBED'S UNIVERSE — the ids we already ship, reordered. It is not the same universe as
#    the worklist stage, which is where new titles come from:
#      ./den stage worklist --mode export --out-dir out --dataset-version <ver>
#        every id in TMDB's daily dump (put movie_ids.json / tv_series_ids.json in the out-dir first —
#        `fetch_export` above leaves them gzipped, so gunzip them). The stage refuses a run that did not
#        name a mode, and one whose dump lost lines to the parse.
#      ./den stage worklist --mode delta --since YYYY-MM-DD --out-dir out --dataset-version <ver> \
#          --set universe_movie=out/delta/universe-movie.json --set universe_tv=out/delta/universe-tv.json
#        what `scripts/delta-run.sh` builds daily — new titles only, skipping out/labels-t02.json. The two
#        --set flags are why it keeps its lists in delta/: a delta written over the full one does not
#        corrupt the enrich run, it ENDS it, as a batch that reports nothing remaining.

# 3. Enrich — TMDB detail+keywords+credits, then ONE Wikidata SPARQL + live Wikipedia plot per surviving id.
#    The Wikipedia plot REPLACES the TMDB overview where found (re-grounding); each batch prints wikiPlot vs
#    tagsOnly. The wrapper logs into Enterprise (if creds present) for a fresh 24h token, then runs ONE batch.
#    Resumable via the enrich checkpoint — loop until "remaining":0.
#    `./den stage fetch --out-dir out --dataset-version <ver>` runs the whole drain — both worklists, batch
#    after batch, until nothing remains — which is what `scripts/enrich-all.sh` did per media. The loop is
#    the stage's now, with the same three stopping rules: an aborted batch is retried, a batch that exits
#    clean having moved `remaining` not at all is a stall, and a batch where every title fell below the vote
#    floor says so instead of blaming the upstream. `--media movie|tv` does one; `--vote-floor 0` re-includes
#    the low-vote tail. One batch by hand is still scripts/enrich-run.sh:
scripts/enrich-run.sh movie 150          # next 150 un-enriched movies; repeat. Then: scripts/enrich-run.sh tv 150
#    (Observed on the popular tier: ~96% wikiPlot hit; the misses are recent/obscure titles with no enwiki article.)

# 3a. Classify — the Jev pass that produced the shipped labels and facets: one typed request per title over
#     the dumped article, into the `combined-v1-r2*.jsonl` shards the corpus join reads. It is the only step
#     here that costs money ($20.47 for 47,529 titles), so run it with --plan first; it resumes, so a repeat
#     buys only what is missing. The questions, the planner and the audit are in `scripts/v2/FACETS-V2.md`.
./den stage articles --out-dir out --dataset-version <ver>            # the whole article per grounded title
./den stage classify --out-dir out --dataset-version <ver> --plan     # then again without --plan

# 4. embed-corpus — compose(facts + already-decided tags + Wikipedia plot) -> den-embed -> int8[1024];
#     append to the index store. Reads labels-t02.json, so run it after a classification pass that wrote
#     labels (step 3a, or `scripts/v2/merge_classify_labels.py`), or to re-embed a corpus whose labels did
#     not change. The flags must match out/index/composition.json or the run refuses — two doc
#     shapes in one vector space is the failure that record exists to prevent.
#     First run in a fresh out-dir records the service's identity to out/index/embedder.json; later runs
#     refuse if the service no longer matches it. An out-dir with a store but no embedder.json also refuses —
#     what built it is unknown, and guessing is how the corpus/query drift went unnoticed in the first place.
#     The same holds for out/index/composition.json, which records how the DOCUMENT was composed —
#     docShape, dropDirector, plotCap. The embedder identity cannot see any of those, and they change the
#     vector completely.
export DEN_EMBED_URL=http://127.0.0.1:8791     # default; set if the service is elsewhere
#     `./den stage embed --out-dir out --dataset-version <ver>` runs exactly this, with the composition
#     pinned rather than typed — `--doc-facts`, `--doc-drop-director` and `--plot-cap 3500` are what the
#     shipped index was built with, and the stage checks the run's own index/composition.json against them
#     afterwards, which is the proof that the composition asked for is the one recorded. (A misspelling is
#     no longer a silently different document: embed-corpus refuses a flag it does not declare, and
#     `--help` lists the ones it does.) It resumes the same way, adds `--pause-ms` and `--limit` for a long
#     run, and refuses a run that recorded no verified space.
#     `doc-facts.json` is two of the document's clauses — director and genre, from Wikidata — and without
#     it the command composes the FULL shape instead, which is a different vector space with nothing in
#     the output saying so. `./den stage embed` runs the scrape as its own stage first; by hand it is
#     `./den stage docfacts --out-dir out --dataset-version <ver>` (~770 SPARQL requests, resumable), or
#     `scripts/v2/derive_doc_facts.py --facts out/facts-<ver>.json --out out/doc-facts.json` when a facts
#     sidecar already exists, which needs no requests at all.
$BIN embed-corpus --out-dir out --labels out/labels-t02.json \
    --doc-facts out/doc-facts.json --doc-drop-director --plot-cap 3500
#     `--dump-docs <path>` writes the composed documents and embeds NOTHING, for embedding elsewhere — the
#     arm64/x86_64 split means the documents travel to the serving box rather than the vectors coming back.
#     It needs no embedder: gating it on one would mean standing up a service purely to write text.

# 4a. The other half of --dump-docs: embed the documents ON THE BOX, against the service that answers live
#     queries. The canary is mounted alongside the script because it is the thing that decides whether any
#     of this may be written — embed_docs.py verifies it and writes nothing if it fails.
#     Copy docs.jsonl, embed_docs.py, embed_canary.py and data/embed-canary.json into the container's /tmp
#     first (`ssh root@pve 'incus exec den -- tee /tmp/<name>' < <file>`), then:
ssh root@pve 'incus exec den -- podman run --rm --network den \
    -v /tmp/embed_docs.py:/embed_docs.py:ro -v /tmp/embed_canary.py:/embed_canary.py:ro \
    -v /tmp/embed-canary.json:/canary.json:ro -v /tmp/box:/w:z \
    docker.io/library/python:3.12-slim python /embed_docs.py \
        --docs /w/docs.jsonl --out-dir /w/out --url http://den-embed:8080 --canary /canary.json'
#     It writes /w/out/vectors.jsonl, keys.json, and embedding-space.json — the verified space. Bring all
#     three back, then join them to the labels and carry the space into the index dir:
python3 scripts/v2/import_box_vectors.py --vectors box/vectors.jsonl --labels out/labels-t02.json \
    --out-dir out/index --embed-space box/embedding-space.json \
    --embedder-health '{"model":"bge-m3","dims":1024,"vector_epoch":1,"runtime":"…","max_tokens":1024}'
#     --embed-space is checked against THIS checkout's data/embed-canary.json before anything is written:
#     vectors verified against a different set of known answers cannot be said to be in the space we ship.
#     finalize (step 6) then stamps it into dataset.meta.json as `embeddingSpace`.

# 6. Finalize — index store -> labels-t02.json + vectors-bge-m3.bin + dataset.meta.json (+ gzip + report).
$BIN finalize --out-dir out

# 6a. The FACTS the store ranks on — the two scrape passes merged. The scrape runs TWICE and cannot run
#     once: the corpus pass covers the ids in labels-t02.json and is stamped --has-vector, the delta pass
#     covers ids given outright and is not, and /recommend must never let a vectorless record into an ANN
#     path. Both passes and the merge all write facts-<ver>.json, so move the corpus pass aside first —
#     without that the delta pass overwrites it and the merged file is short by every delta title, which
#     is how a rebuild once dropped 137 of them and nothing but /recommend noticed.
$BIN facts --out-dir out --labels out/labels-t02.json --has-vector
mv out/facts-<ver>.json out/facts-<ver>.pre-merge.json
$BIN facts --out-dir out --ids <the delta ids>     # writes out/facts-unversioned.json
./den stage facts --out-dir out --dataset-version <ver>
#     The stage runs scripts/merge-facts.py with those two files and --version, built from
#     `pipeline/facts.py`'s declaration rather than retyped; `pipeline/facts_test.py` holds the two to the
#     same bytes. A missing pass is a refusal naming what writes it, not a smaller merge.

# 7. Metadata sidecar — poster/title/year per shipped id, so a neighbour renders without a TMDB detail call.
#    Its filename carries the datasetVersion, which step 6 just changed, so this belongs after EVERY finalize
#    that adds titles. Skipping it leaves the manifest naming the previous version's sidecar: it still hashes
#    correctly, so both consumers accept it and never re-sync — the new titles render with no poster forever.
./den stage metadata --out-dir out --dataset-version <ver>   # <ver> = what step 6 just wrote
#    The stage refuses a run whose --dataset-version is not the one dataset.meta.json names, which is that
#    same failure caught one step earlier. `--limit N` is a PROBE: it fetches N, reports, and writes
#    nothing — the cheap way to check the credential without re-syncing every device onto a gutted file.

# 7a. The CORPUS — the source of truth the store is built from, joining the pass shards, the facts and
#     both label sets into one inspectable JSONL plus its entity sidecar. It was never written down here,
#     which is the problem oxyc/den-dataset#27 is about; it is a stage now, so the shards come from the
#     declared glob rather than from three paths retyped, and a missing one is a refusal.
./den stage corpus --out-dir out --dataset-version <ver> --expect <titles>
#     `--expect` is the guard that refuses a short run. Use `--set combined=<path>` (repeat it) to point
#     at a pass written under another run's name.

# 7b. The STORE — the only artifact that publishes (oxyc/den#113). Everything above is an INPUT to it: it
#     carries facts, labels, cards, facets, rail facets, the entity table, alias titles and both vector
#     matrices as sections, and den-atlas mmaps it. `--stamp-meta` is what DECLARES it; without that flag
#     the file is written, no manifest names it, and the publish refuses.
python3 scripts/v2/build_store.py \
    --corpus out/corpus-<ver>.jsonl.gz --entities out/corpus-<ver>-entities.json.gz \
    --facts out/facts-<ver>.json \
    --vectors out/vectors-bge-m3.bin --vector-labels out/labels-t02.json \
    --premise-vectors out/vectors-premise.bin --premise-labels out/labels-premise.json \
    --dataset-version <ver> \
    --out out/den-<ver>.store --stamp-meta out/dataset.meta.json
#     `./den stage store --out-dir out --dataset-version <ver> --stamp-meta out/dataset.meta.json` runs
#     the same writer with the same arguments, built from `pipeline/store.py`'s declaration rather than
#     retyped — `pipeline/store_test.py` holds the two to the same bytes. It is the first stage behind
#     the single entry point (oxyc/den-dataset#27); everything else here is still typed by hand.
#     No --metadata and no --enriched: the card's title and year come from the corpus's own `facts`
#     (`titles.en` and `released`), the poster path is not published, and the vote count is gone — a
#     browse row is ordered by IMDb's public ratings dump, which den-atlas joins on `imdb` at run time
#     (oxyc/den#118). Both metadata-<ver>.json and out/enriched are still BUILT and read by other things;
#     they are simply no longer inputs to the store, which now reads no TMDB artifact at all.

# 8. Publish — the moving `data-latest` GitHub release den-atlas fetches. It uploads the store and the
#    manifest, and prunes every retired blob's keys out of that manifest first.
./den stage publish --out-dir out --dataset-version <ver>
#     The stage runs `scripts/publish-dataset.sh out`, which is still the rule and still runnable by
#     hand — but BY HAND it must be run FROM THE REPO ROOT, because the ownership guard resolves producer
#     paths and `git ls-files` against the working directory. The stage runs it there whatever directory
#     you typed it in. `pipeline/publish_test.py` holds the two invocations to the same argv, cwd and
#     environment, and `scripts/publish-dataset.test.sh` runs its whole case list through both.
#     Every override is an environment variable and reaches the guards either way (DEN_STORE_REBUILD,
#     DEN_ALLOW_SHARED_PLOTS, DEN_ALLOW_DROPPING_BLOBS).
```

`$BIN` is `.build/release/taxonomy-backfill` (`swift build -c release`).

`finalize --embedding-version <v>` overrides the artifact label. The default path is the bge-m3 build above.

## Recovering a store's composition

A store with rows and no `index/composition.json` refuses a top-up, because how its documents were composed
cannot be known — and guessing writes the guess down as a fact. Recover it instead; it takes minutes.

The shipped store's values are already established: **`{"docShape":"lean","dropDirector":true,"plotCap":3500}`**
for `out-t02-cc0b`. They apply to that store and no other. For any other store:

1. **Pick probes.** ~10 titles whose plot is far longer than any candidate cap, that carry **no** Wikidata
   director (so the director flag cannot confound the cap), and that appear in exactly one batch. Plus 2
   short-plot titles that **do** carry a director — the cap cannot touch those, so they isolate the flag and
   double as a rig control.
2. **Target them with a subset `--labels` file.** `--limit` is a counter, not a selector; it takes the first
   N in read order. A `LabelsArtifact` holding only the probe records works — every other title is skipped
   as `missingLabel`.
3. **Settle the shape first**, at any cap, on the short-plot director titles: run with and without
   `--doc-drop-director`. Exactly one matches.
4. **Then sweep the cap** on the long titles, holding the shape fixed. `assertDocFits` caps it at 3596, so
   the answer is an integer in (0, 3596].
5. **Compare exact bytes**, from the store's own `index/vectors.jsonl` against the shipped
   `vectors-bge-m3.bin` (a `DENVEC02` blob: 16-byte header, a u64 key per row, then row-major int8 — look
   the row up BY KEY, `(media << 32) | tmdbId`, rather than by position in `labels-t02.json`). Judge
   on byte equality only: adjacent caps sit at cosine 0.95–0.98, which reads as "about right" and is wrong
   in every byte.

`cappedPlot` snaps to the last `". "`, so one probe pins an interval rather than a value; intersect across
probes. For cc0b that window was [3479..3534], and 3500 is the value because it is the only round number in
it and this document already prescribed it.

## Incremental top-up (OptiPlex)

Don't re-embed 30k for a handful of new/changed titles:

- **New films**: discover freshly-changed entities with a Wikidata `schema:dateModified` filter *on the
  entity* (bound in the SPARQL WHERE), enrich just those ids, then classify/embed/finalize them as an
  additive batch.
- **Changed plots**: a title needs re-embedding only when its Wikipedia article changed — track the article
  `revid` (`action=parse&prop=revid`) and re-enrich + re-embed the ids whose revid moved. `finalize`
  de-dups by `(mediaType, tmdbId)` keeping the newest record + its aligned vector.

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
