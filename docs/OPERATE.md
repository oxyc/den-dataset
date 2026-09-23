# Operating the dataset producer

How to build and publish a generation, and what to check while you do. What each stage reads and writes is
in `./den stages`; this file is the order, the prerequisites, and the things the code cannot tell you.

## The alignment rule (do not break this)

**The corpus and every live query must come from the same `den-embed`.** int8 dot products are meaningless
between vectors from different ones, and nothing about the result looks wrong when they are:
oxyc/den-dataset#21 was a corpus half-embedded by two den-embed builds, measured at 6.3/10 top-10 overlap,
found by hand months later.

**1. Embed where you serve** — against the den-embed on the box that answers live queries, not a local
container. The published image cannot run on an Apple Silicon Mac at all: ONNX Runtime needs AVX2, which
emulation lacks, so `/health` answers and the first embed dies with an illegal instruction ("connection
refused" from the client). Whether architecture alone moves vectors is unsettled (#21 compared arm64 and
x86_64 without holding `MAX_TOKENS` fixed); two x86_64 hosts, AVX2 and AVX-512, are byte-identical at a
matching cap.

```sh
ssh root@pve 'incus exec den -- podman run --rm --network den -v /opt/den/embed:/w:z \
    docker.io/library/python:3.12-slim python3 /w/<script>.py --url http://den-embed:8080'
```

A `&` or redirect inside `ssh root@pve 'incus exec den -- …'` binds to the **host** shell, not the
container. Put backgrounding in a script inside the container; a second accidental launch halves throughput,
since den-embed serialises on one model lock.

**2. The canary, not a version string, says which space a service is in.** `embeddingModel`, `dims`,
`vectorEpoch` and `embedderRuntime` have all stayed the same across builds whose output moved.
`data/embed-canary.json` holds fixed texts and the exact int8 vectors the service must return; every path
that writes a vector checks them before opening its output, byte-identical or refuse:

```sh
pipeline/embed_canary.py --url http://den-embed:8080        # exit 0, or exit 2 and a report per case
```

The verified identity is stamped into `dataset.meta.json` as `embeddingSpace`. `--regenerate` is only for
when the space is MEANT to move (model change, `VECTOR_EPOCH` bump, settings changed with a full re-embed) —
never to get a failing run going, because the space id is published.

**3. `MAX_TOKENS=1024`.** den-embed defaults to 512 and truncates silently, server-side. At 512 the corpus
loses half its plot prose. This setting is what has actually caused a space difference, twice, and both
times it was first diagnosed as something else (an instruction set, then a thread pool). A comparison
against a container at the image's default cap is comparing caps.

**den-embed is held on the box** (`/etc/den/hold/den-embed`), so the daily `den-update` never moves it. Moving
it is a corpus decision: re-embed, then `den-update den-embed`.

### Current state

| | |
|---|---|
| live dataset | `5b1c3213b6a1`, built 2026-09-20: 47,539 plot vectors embedded on the box by `den-embed/5.1.2` at `max_tokens 1024`, in one run; 44,531 premise vectors, embedded on the box 2026-09-19 |
| serving box | `den-embed/5.1.2`, `max_tokens 1024`, space `canary-v1:42f4618a055103411edec2ad0f87dfa94f1e7f3d40bf768563e82a0fe692a2df` (7/7 cases) — checked 2026-09-21 |
| gap | the live manifest has no `embeddingSpace`: it was built before the canary existed. The next generation embedded through a canary-verified run will carry one. |

## Building a generation

`./den run --out-dir out` runs every stage in order. It skips the paid classify pass unless given
`--spend`, and stops before publishing unless given `--publish`. `facts`, `corpus` and `store` name their
files by the version `finalize` derives, and read it from `out/dataset.meta.json`; a `--dataset-version`
given to any of them is only checked against it. The daily delta prints the same sequence.

```sh
# 1. Secrets. den.env is gitignored; the fetch stage reads it.
cp den.env.example den.env        # TMDB_API_KEY (required); Enterprise username/password (optional)

# 2. The worklist — which titles to enrich.
./den stage worklist --mode export --out-dir out     # every id in TMDB's daily dump (gunzip
                                                     # movie_ids.json / tv_series_ids.json into out/ first)
./den stage worklist --mode delta --since YYYY-MM-DD --out-dir out \
    --set universe_movie=out/delta/universe-movie.json --set universe_tv=out/delta/universe-tv.json
#    Keep a delta's lists under delta/: written over the full ones, they END the enrich run.
#    To re-embed exactly what ships instead, `python3 scripts/build-worklist.py` writes
#    out/worklist-{movie,tv}.json, and step 3 drains those with --set universe_movie=… universe_tv=….

# 3. Fetch — TMDB detail plus the live Wikipedia plot per title, resumable.
./den stage fetch --out-dir out
#    One batch by hand, credentials already in the environment:
python3 -m pipeline.enrich --worklist out/worklist-movie.json --out-dir out --limit 150

# 3a. Classify — the one step that buys ($20.47 for 47,529 titles). --plan first. scripts/v2/FACETS-V2.md.
./den stage articles --out-dir out
./den stage classify --out-dir out --plan
./den stage classify --out-dir out

# 3b. Genres & moods for titles the curated file lacks. --plan first; --spend asks Jev, then it derives.
./den stage genres_moods --out-dir out --plan
./den stage genres_moods --out-dir out --spend

# 4. The document's director and genre clauses (Wikidata, ~770 SPARQL requests, resumable).
./den stage docfacts --out-dir out

# 5. Embed. DEN_EMBED_URL picks the service (default http://localhost:8791).
./den stage embed --out-dir out

# 5a. Or embed ON THE BOX: write the documents here, embed there, bring the vectors back.
./den stage embed --out-dir out --dump-docs out/docs.jsonl
#     Copy docs.jsonl, pipeline/embed_docs.py, pipeline/embed_canary.py and data/embed-canary.json into
#     the container's /tmp (`ssh root@pve 'incus exec den -- tee /tmp/<name>' < <file>`), then:
ssh root@pve 'incus exec den -- podman run --rm --network den \
    -v /tmp/embed_docs.py:/embed_docs.py:ro -v /tmp/embed_canary.py:/embed_canary.py:ro \
    -v /tmp/embed-canary.json:/canary.json:ro -v /tmp/box:/w:z \
    docker.io/library/python:3.12-slim python /embed_docs.py \
        --docs /w/docs.jsonl --out-dir /w/out --url http://den-embed:8080 --canary /canary.json'
python3 pipeline/import_box_vectors.py --vectors box/vectors.jsonl --labels out/genres-moods.json \
    --out-dir out/index --embed-space box/embedding-space.json \
    --embedder-health '{"model":"bge-m3","dims":1024,"vector_epoch":1,"runtime":"…","max_tokens":1024}'

# 6. Finalize — labels-t02.json (each vector title with this run's genres & moods), the vector blob and
#    dataset.meta.json. This decides <ver>.
./den stage finalize --out-dir out
```

**Step 5, what to watch for.** The embed stage composes from the `genres-moods.json` step 3b wrote, so a
title with no genres & moods is skipped as `missingLabel`. A title whose genres & moods changed keeps its old
vector until `--reembed-changed` re-embeds it; `finalize` ships the new genres & moods either way, and
refuses a vector whose title has none. The first run in an out-dir records the service's identity and the document shape
(`index/embedder.json`, `index/composition.json`); later runs refuse a service or shape that differs, and a
store with rows but no identity record refuses too. The plot cap is 3,500 characters and keeps the head
only, so a truncated plot embeds no ending — while the facets prompt keeps a tail, because `ending` needs it.
On the re-grounded corpus, 32% of plots are cut at 3,500 and 82.8% of all plot text survives. No cap above
~4,000 characters can reach the embedder at 1024 tokens, so raise the cap and `MAX_TOKENS` together or not
at all.

**6a. The facts** — two Wikidata scrape passes, merged. The corpus pass covers the ids in `labels-t02.json`
and is stamped `hasVector`; the delta pass covers `out/facts-delta-ids.txt` and is not, because den-atlas's
`/recommend` must never let a vectorless record into an ANN path. The stage refuses without the list: a merge
missing the delta pass once dropped 137 titles and only `/recommend` noticed.

The list is yours to write, fresh every rebuild: every title the LAST published facts file carries that the
new labels do not, plus any id atlas is missing, `movie:1` / `tv:2`, one per line.

```sh
python3 - out/facts-<previous ver>.json out/labels-t02.json > out/facts-delta-ids.txt <<'PY'
import json, sys
facts, labels = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:3])
vectors = {f"{r['mediaType']}:{r['tmdbId']}" for r in labels["records"]}
print("\n".join(sorted({f"{r['mediaType']}:{r['tmdbId']}" for r in facts["records"]} - vectors)))
PY
./den stage facts --out-dir out       # reads <ver> from out/dataset.meta.json
```

Take the difference, not the previous file's vectorless records: a title the classify pass dropped has lost
its vector and belongs here. On `out-repass` on 2026-09-22 this was 79 ids. A batch Wikidata fails is dropped
whole and the merge refuses a pass that skipped one; `scripts/facts-run.sh out` loops until nothing is
skipped.

About one title in 560 has its TMDB id claimed by two Wikidata items (series 2559: "Boon" and "Bonn").
Every stage that asks Wikidata by TMDB id — enrich, articles, docfacts, facts — answers from ONE of them,
chosen by `lib/wikidata.resolve`: first `data/wikidata-item-decisions.json`, then TMDB's own IMDb id and
year, the item stating no other TMDB id, the item with an English article. The row records `wikidataItem` and
`wikidataCandidates`. A title nothing chooses for ships with no Wikidata fields; the facts stage warns and
counts it (`ambiguousItems`), and the publish refuses until it is decided in that file. A decision for an id
that is no longer contested is refused as stale. facts and docfacts read the TMDB record from the
enrichment's cache; on a miss they need `TMDB_API_KEY` (`. scripts/lib/den-env.sh; den_load_env`). A
checkpointed row scraped under another choice is scraped again on the next run.

**7. The corpus** — the pass shards, facts and genres & moods joined into one JSONL, the source of truth.

```sh
./den stage corpus --out-dir out --expect <titles>
```

`--expect` refuses a short join. `--set combined=<path>` (repeatable) points at a pass under another name.

**7a. The store** — the only artifact that publishes; den-atlas mmaps it. `--stamp-meta` declares it in the
manifest; without it the publish refuses.

```sh
./den stage store --out-dir out --stamp-meta out/dataset.meta.json
```

The same writer typed by hand, which `pipeline/store_test.py` holds to the same bytes:

```sh
python3 scripts/v2/build_store.py \
    --corpus out/corpus-<ver>.jsonl.gz --entities out/corpus-<ver>-entities.json.gz \
    --facts out/facts-<ver>.json \
    --vectors out/vectors-bge-m3.bin --vector-labels out/labels-t02.json \
    --premise-vectors out/vectors-premise.bin --premise-labels out/labels-premise.json \
    --dataset-version <ver> \
    --out out/den-<ver>.store --stamp-meta out/dataset.meta.json
```

**8. Publish** — replaces the moving `data-latest` release: the store and the manifest, with every retired
blob's key pruned from the manifest first.

```sh
./den stage publish --out-dir out
```

The stage runs `scripts/publish-dataset.sh out` from the repo root. By hand it must be run from the root too,
because its ownership guard resolves producer paths and `git ls-files` against the working directory.
Overrides are environment variables and reach the guards either way (`DEN_STORE_REBUILD`,
`DEN_ALLOW_SHARED_PLOTS`, `DEN_ALLOW_DROPPING_BLOBS`).

The publish also scores the genres & moods labels against `data/eval/golden-large.json` and refuses a
score more than the tolerance under the baseline in `data/eval/quality-floors.json`. The baseline moves only
when recorded: `scripts/eval-taxonomy.py out/labels-t02.json --record` after an improvement ships, with
`--accept-drop` for a deliberate drop. Commit the result.

## The daily delta

`scripts/delta-run.sh [DAYS_BACK] [OUT_DIR]` builds a delta worklist and enriches one batch per media. It
**stops before the classify pass** — that step buys, so it never runs unattended — and prints steps 3a–8 with
the batch ids it wrote. A title below the vote floor is recorded as judged for that day and not counted as
pending, so it is judged again the next day, or at once if its count or a floor changes, until it earns
votes.

## Reading an enrich report

A title is admitted when its TMDB vote count clears its TMDB floor **or** the number of Wikipedias with an
article on it (Wikidata's sitelinks) clears its Wikipedia floor, per tier (`pipeline/floors.py`; European,
South American and AU/NZ origins get the lower tier). Reports count `admittedByTmdb` and
`admittedByWikipedias`. The Wikipedia count is asked only for titles TMDB's count leaves short, and a failed
lookup aborts the batch like a failed mapping. IMDb's datasets are not used (`LICENSES.md`).

## Wikimedia Enterprise plots (optional)

With `WIKIMEDIA_ENTERPRISE_USERNAME` / `_PASSWORD` in `den.env`, the fetch stage mints a 24h bearer and
`lib/plot.py` reads the pre-sectioned plot from Enterprise, falling back to the public `action=parse` API on
any miss. Enterprise reads are not cached and name no page, so they record no revision id and cannot see a
redirect.

The account allows 50,000 on-demand requests a month, shared by every machine holding the credentials, and
an overdrawn month answers 429 like a throttle. `lib/enterprise.py` checks the account's own count before
the first request and every 100 after, and stops at `limit - DEN_ENTERPRISE_RESERVE` (default 500). Batch
reports count `plotsFromEnterprise` / `plotsFromActionApi`; that, not the bearer being held, says which
source a run used. Never use a Wikimedia dump: stale, and hundreds of GB against a working set of a few
hundred MB.

## Recovering a store's composition

A store with rows and no `index/composition.json` refuses a top-up, because how its documents were composed
cannot be known — and guessing writes the guess down as a fact. Recover it instead; it takes minutes. The
shipped cc0b store's values are `{"docShape":"lean","dropDirector":true,"plotCap":3500}` and apply to that
store only.

Compose probe documents with `pipeline/compose.py` (`capped_plot(plot, cap)`; `lean(...)` with a
`Directed by …` clause prepended for the director variant) and embed them with `lib/denembed.embed_many`:

1. **Pick probes**: ~10 titles whose plot is far longer than any candidate cap and that carry no Wikidata
   director, plus 2 short-plot titles that do carry one (the cap cannot touch those, so they isolate the
   director flag).
2. **Settle the shape first** on the short-plot titles, with and without the director clause. Exactly one
   matches.
3. **Sweep the cap** on the long titles with the shape fixed. At a 1024-token service the answer is an
   integer in (0, 3596].
4. **Compare exact bytes** against the shipped blob, looking rows up BY KEY (`(media << 32) | tmdbId`), not
   by position. Adjacent caps sit at cosine 0.95–0.98, which reads as "about right" and is wrong in every
   byte.

`capped_plot` snaps to the last `". "`, so each probe pins an interval; intersect them.
