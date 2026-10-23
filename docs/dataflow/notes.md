# Surprises found while tracing den-dataset

The state drawn is `origin/main` plus `origin/port-enrich` plus `origin/port-tranche-3`, merged with
`git merge-tree` in a scratch clone. Citations are `<ref>:<path>:<line>`. The line numbers match the ref
named, or the merged tree where the file is unchanged.

"Genre & mood labels" below means the per-title primary genre, subgenres, moods and themes, stored in
`labels-t02.json`.

## The merge itself

- **`pipeline/` merges cleanly. Six files conflict:** `.github/workflows/ci.yml`, `README.md`,
  `Sources/DenDataset/Transport.swift`, `Sources/taxonomy-backfill/main.swift`,
  `Tests/DenDatasetTests/ArgsTests.swift`, `pipeline/fetch_test.py`.
- **The declarations agree.** port-tranche-3 declares exactly the merged stage→artifact edge set.
  port-enrich changes only `fetch`'s producer (to `pipeline/enrich.py`). After the merge, no registered
  producer names Swift.
- **Swift files remain, and one of them is data.** `Package.swift` and `main.swift` are still in the tree.
  So is `Sources/DenDataset/Taxonomy.swift`, which the Jev classify pass parses as its genre & mood
  vocabulary and hashes into its manifest (`scripts/v2/combined_questions.py:14`). Deleting "the Swift"
  would delete an input of the paid pass.
- **Swift leftovers survive the merge:**
  - `pipeline/artifacts.py:21` `BACKFILL` is unused.
  - `pipeline/embed.py:97-110` (`BUILT`, `binary()`) has no caller. Its comment says "the fetch stage still
    drains `enrich` through the binary". The same stale claim is at `pipeline/compose.py:166-167`.
  - `scripts/delta-run.sh:42,48` still runs `swift build -c release` and sets a `BIN` it never uses (both
    branches), so it breaks once `Package.swift` is deleted.
  - The auto-merged `docs/OPERATE.md:157` still says `swift build -c release` (with no `BIN`).

## Genre & mood labels: where they come from

- **They come from the previous generation, not from this run's Jev pass.**
  - Jev (`classify`) does ask primary genre and a yes/no per subgenre, theme and mood
    (`scripts/v2/combined_questions.py:153-182`). Those answers reach only the corpus rows, as `nouls`
    (`scripts/v2/consolidate_corpus.py:191`).
  - The store's label sections read the corpus's `labels`/`premiseLabels` fields (`store/labels.py:27`),
    which are copied from `labels-t02.json`.
  - That file is carried forward generation to generation. `embed` copies each label record from the
    existing file into its store (`pipeline/compose.py:178-187`, `pipeline/embed.py:283`), and `finalize`
    re-emits it.
  - The only thing that adds labels is `scripts/v2/merge_classify_labels.py`, which folds in ~9,010 titles
    from a Claude-subagent labelling phase (`:7`, `scripts/v2/classify/AGENT.md`). That makes it a second,
    undeclared writer of the file (`:136`).
  - Everything else is inherited from the retired generations (`pipeline/finalize.py:221`,
    `scripts/v2/typesafe_client.py:4-5`).
- **Live defect: titles from the daily pass are never labelled or embedded.** `scripts/delta-run.sh:125-126`
  tells the operator that `docfacts`/`embed` pick up a new id "once the classify pass's rows have reached"
  the labels. Classify's rows never reach the labels. A title enriched by the daily pass therefore has no
  label record, so `embed` skips it (`compose.py:178-181`, counted as `missingLabel`), and it never gets a
  vector or labels.
- **The labelling phase has code on both sides, but not at the start.**
  - `build_classify_backfill.py:68` re-batches rows a batch dropped.
  - `validate_classify_batch.py` checks each batch, and the merge acts on its report
    (`merge_classify_labels.py:12`).
  - The merge reads `vocab.json` (`:66`).
  - The first `in/` batches, `vocab.json` and `SPEC.md` have no code producer.

## The labels read looks like a loop; it is the previous generation

- **`worklist`, `docfacts` and `embed` read `labels-t02.json` before `finalize` writes it.** Each reads
  what the previous run's `finalize` left (`pipeline/__init__.py:34-35`):
  - `worklist` uses it as the published labels, the set a daily pass must skip (`pipeline/worklist.py:131-144`).
  - `docfacts` uses it only for its id list (`pipeline/docfacts.py:79-92`).
  - `embed` copies its records (above).
  The chart draws this as a separate "previous generation" node. `derive_doc_facts.py` likewise reads the
  previous generation's facts file.
- **It is a real bootstrap cycle in a fresh out-dir.** A missing required input refuses with "Build it
  with: ./den stage finalize" (`pipeline/contract.py:234`, via `how_to_build` at `:258`), but `finalize`
  needs `embed`'s stores, and `embed` needs the labels. A new out-dir has to be seeded with an existing
  `labels-t02.json`.

## Registry entries that name the wrong file, and a second premise writer

- **`premise_labels`'s registered producer, `scripts/build-premise-tags.py`, reads `labels-premise.json`**
  (`:27`) and writes `data/premise-tags-v1.json` (`:23`). The real writers are
  `scripts/v2/build_premise_labels.py:68` and `scripts/merge-premise-coverage.py:101`.
- **`premise_vectors` is registered to `scripts/v2/embed_tags.py`.** That script writes
  `vectors-{label}.bin` (`:164`), the coverage-fill blob.
  - The actual writers of `vectors-premise.bin` are `scripts/merge-premise-coverage.py:83`, and
    `embed_premise_v2.py`'s `vectors-premise-v2.bin` (`:156`) copied into place by hand.
  - `merge-premise-coverage.py` also writes `premise-ids.json` (`:97`) and reads the genre & mood labels (`:87`).
  - `check-producers.py`'s staleness check therefore watches the wrong files for both premise inputs.
- **The premise v1 tag file has readers:** `build_premise_worklist.py:69,148` and
  `embed_premise_v2.py:87,103`. `embed_premise_v2.py` also extends a base blob,
  `vectors-premise-v1-realigned.bin` plus keys (`:85-86`), that no code produces.

## Declared, but read or written by nothing in STAGES

- **Declared inputs that no stage writes:**
  - `export_movie`/`export_tv`: from `scripts/build-worklist.py`.
  - `delta`, the Jev critique pass (`scripts/v2/run_delta.py`).
  - `delta_ids`: written by hand.
  - `premise_labels` and `premise_vectors`.
- **Outputs that no stage reads:**
  - `composition`: only `embed` reads it back.
  - `combined_manifest`: read by `audit_combined.py`.
  - `corpus_facts` and `delta_facts`: consumed inside `facts`, by `merge-facts.py`.
- **Undeclared inputs to `finalize`:**
  - It merges over the existing `dataset.meta.json` (`pipeline/finalize.py:308`).
  - It reads `enrich-checkpoint.json` and the retired `classify-checkpoint.json` by hardcoded name
    (`:224`).
- **Written, read by nothing:** `report.json` and `labels-t02.json.gz` (`pipeline/finalize.py:5`).
- **`store` rewrites `dataset.meta.json`** only when `--stamp-meta` is given (`pipeline/store.py:65`).
  `den` defaults it to empty (`pipeline/contract.py:137`), and `MANIFEST` is not in the stage's `OUTPUTS`.
  `publish` also rewrites the manifest: it prunes keys and stamps record counts (`publish-dataset.sh:275`),
  the grounding census (`:322`) and `storeRebuild` (`:256`).
- **Committed files that no code reads:** `data/classify-queue.json`, `data/plots-lost-on-reground.json`,
  `data/plots-sidecar-v1.json`, `data/corpus-ids.json`.

## Spending and gates

- **`scripts/v2/run_delta.py:69` pays Jev (`rc.paid_run`) with no `--spend` gate.** It is outside
  `STAGES`, yet its output is a required input of `corpus`. Like `classify`, it takes `TYPESAFE_API_KEY`
  from `den.env` (`typesafe_client.py:50`).
- **Premise tagging and genre & mood labelling are agent phases.** Their dispatch is not in code. Those
  edges are drawn dotted, citing the line that documents them.
- **The publish quality gate is report-only.** `scripts/publish-dataset.sh:429-430` runs
  `eval-taxonomy.py … || true`. origin/main `README.md:174` says it "gates a publish".
- **Consumer-side gates exist and are drawn:**
  - `atlas-dataset-sync.sh` checks the sha256 of every blob (`:213`) and runs `den-atlas check` on the
    staged set (`:240`).
  - den-atlas's `fetch-dataset.sh` verifies each blob (`:136`).
  - The TV skips a pinned provider whose signature fails (`AppModel+Indexes.swift:132`; the audit cited
    `:103`, which is the comment above it).

## Worklist source is broken by default

- **`scripts/build-worklist.py:27-30` defaults `LABELS` to the den app's `labels-t02.json`.** den commit
  `fce66d6` deleted that file, and `main()` reads it (`:66`) before fetching the dumps. So the documented
  way to build `export_movie`/`export_tv` fails unless `LABELS` is set. It also fetches over plain
  `http://files.tmdb.org` (`:41`).

## Licensing: what the releases carry

- **`data-latest` (checked 2026-09-22) holds exactly `den-5b1c3213b6a1.store` and `dataset.meta.json`.**
  - The meta's `storeRebuild` says the poster and vote columns were removed and card title/year now come
    from Wikidata. That matches `build_store.py`'s `PROVENANCE`, where `VENDOR_ALLOWED = set()` (`:158`).
  - The meta has no `embeddingSpace` (the store predates the canary) and no `signature`.
- **`LICENSES.md:51-62` (all refs) and origin/main `README.md:243-255` are stale.** They still say the store
  carries TMDB `card_title`, `card_year`, `votes` and `card_poster`.
- **`articles-2026-09-19` and `raw-2026-09-20` were deleted on 2026-09-22 because they carried TMDB fields.**
  The files they were built from still carry them:
  - Wikipedia article dump rows copy TMDB `title`/`year` (`pipeline/articles.py:157-158` ←
    `lib/tmdb.py:164-165`).
  - Jev answer rows copy them again (`scripts/v2/run_combined.py:357-358`).
  So a future hand upload of either file would reintroduce them. The corpus release `corpus-<ver>` stays
  and carries no TMDB fields (`consolidate_corpus.py:179-196`). No code creates it, and no publish guard
  checks it.
- **`.cache/tmdb` holds full TMDB bodies, overviews included.**
  - Its 180-day TTL is a compliance boundary (`lib/cache.py:162-171`).
  - `/discover` is never cached (`lib/cache.py:185-192`).
  - Enterprise is never cached (`lib/plot.py:13`).
  - Wikidata's title, entity and instance-of queries are uncached and checkpointed instead
    (`lib/wikidata_facts.py:7-10`).

## Consumers: prose that contradicts code

- **The TV app no longer reads data-latest.** It fetches den-atlas's `/dataset.json` and queries
  `/index/*`, `/recommend` and `/embed` (den `App/Den/Shell/AppModel+Indexes.swift:94-96`).
  `scripts/publish-dataset.sh:524` still tells consumers "Den app 'make sync-dataset'", which den deleted
  in `fce66d6`.
- **Signing is manual.** `den/scripts/sign-dataset.swift:9-10` prints a signature that a person pastes into
  `dataset.meta.json`. It reaches den-atlas only through a republish; den-atlas then passes it through
  verbatim (`src/dataset.rs:33`).
- **den-atlas reaches den-embed for query vectors** (`EMBED_URL`, `src/main.rs:290`). Because the
  published meta has no `embeddingSpace`, nothing can check corpus/query alignment.

## Naming

- **"delta" names four unrelated things:**
  - `delta`: the Jev critique pass.
  - `delta_ids`/`delta_facts`: facts for titles with no vector.
  - `delta-run.sh` and `worklist --mode delta`: the daily freshness pass.
  - `delta_facts`' filename moved from `facts-unversioned.json` (main) to `facts-{version}.delta.json`
    (tranche-3).
- **The chart leads with plain names.** A version-coded name (`labels-t02.json`, `combined-v1-r2*.jsonl`,
  `DENVEC02`, `nouls`) appears only as the filename or term after it.
