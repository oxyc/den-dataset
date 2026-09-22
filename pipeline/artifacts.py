#!/usr/bin/env python3
"""Every file the pipeline reads or writes, declared once.

One entry per artifact, naming the file. A stage references the entries it reads and writes; nothing
re-spells a path. `scripts/check-producers.py` reads its registry off the stages that name these, so an
artifact reached by nobody cannot exist — being declared IS being owned.

**An entry names a producer only while nothing here builds it.** Once a stage writes it, that stage is
the answer and the fields are empty: `pipeline/corpus.py` runs the rule it is registered as, so the
registry cannot name a script the run does not use. Entries that still carry a producer are the seam —
the stages not ported yet (oxyc/den-dataset#27) — and each one empties when its stage lands.

`artifacts_test.py` refuses an entry no stage names, which is the same attrition rule the modules are
held to: a declaration nothing reaches is deleted, not kept for later.
"""
from .contract import Artifact

#: The article dump the classify pass reads: one JSON object per title carrying the whole Wikipedia article
#: text, its revision and the extractor's chosen plot headings. Every question the pass asks is about a
#: section of this file, which is why the pass hashes it into its manifest.
ARTICLES = Artifact(
    name="articles",
    filename="articles.jsonl",
)

#: TMDB's daily ID export, one per media — the universe `worklist --mode export` parses. A public static
#: file (no API key), and the only input to the full run's universe that comes from outside this repo.
#: `scripts/build-worklist.py`'s `fetch_export` is what fetches it, under exactly these names, and leaves it
#: GZIPPED; the worklist stage's parse takes text, so the decompression it does not do is part of the `how`.
EXPORT_MOVIE = Artifact(
    name="export_movie",
    filename="movie_ids.json",
    producer="scripts/build-worklist.py",
    how="python3 scripts/build-worklist.py, then gunzip out/movie_ids.json.gz",
    dedicated=False,
)

EXPORT_TV = Artifact(
    name="export_tv",
    filename="tv_series_ids.json",
    producer="scripts/build-worklist.py",
    how="python3 scripts/build-worklist.py, then gunzip out/tv_series_ids.json.gz",
    dedicated=False,
)

#: The universe, one file per media — what `enrich` drains, one media at a time, so a run can name one
#: (`--media`). `enrich` itself takes a worklist of both: a film and a series can share a tmdbId, and every
#: set, map and query in `pipeline/enrich.py` is keyed by `mediaType:tmdbId` for that reason.
#:
#: Named UNIVERSE, not worklist, because two different tools wrote `worklist-<media>.json` and they do not
#: build the same list. `scripts/build-worklist.py` enumerates the ids Den already SHIPS, ordered by
#: popularity, so a re-embed covers the current catalogue; this command enumerates everything TMDB has —
#: 1,246,659 movie ids against a corpus of 47,618. Sharing a filename meant whichever ran last decided
#: which universe the next enrich billed for, and nothing downstream could tell them apart.
UNIVERSE_MOVIE = Artifact(
    name="universe_movie",
    filename="universe-movie.json",
)

UNIVERSE_TV = Artifact(
    name="universe_tv",
    filename="universe-tv.json",
)

#: The classify pass, in shards. Three of them today, named by the run that wrote them rather than by the
#: dataset version — the pass owns that naming, and this is a glob so the set is whatever the pass left
#: behind. Reading one shard of three is what took eleven titles out of a derived blob for a day; a stage
#: that only knows the set cannot repeat it. Point `--set combined=…` at another run's shards.
#:
#: The wildcard is EMPTY for the shard a pass writes; what fills it are the capacity quarantines
#: `resume_combined_excluding.py` adds afterwards, which is why `Context.shard` derives the write target
#: from the same glob the corpus join reads the set by.
COMBINED = Artifact(
    name="combined",
    filename="combined-v1-r2*.jsonl",
    shards=True,
)

#: Each shard's sidecar: the run id, and the hashes of the input, the questions, the prompt, the taxonomy,
#: the model and the pass's own source files. `run_combined.py` derives its name from `--out` rather than
#: taking a flag for it, and `audit_combined.py` — the only thing between a corrupted bundle and a
#: published dataset — looks it up by that derived name. Declared so the stage checks it landed there.
COMBINED_MANIFEST = Artifact(
    name="combined_manifest",
    filename="combined-v1-r2*.jsonl.manifest.json",
    shards=True,
)

#: The genres & moods answers Jev was paid for: one row per title, append-only, in `run_combined`'s row
#: shape. One shard per article dump — the wildcard holds the dump's digest, because the pass's manifest
#: hashes the dump and a rebuilt one could not resume the old shard — and a title answered in any shard is
#: never asked again. They carry TMDB titles and years, so they are backed up, not published.
GENRES_MOODS_ANSWERS = Artifact(
    name="genres_moods_answers",
    filename="genres-moods-v1*.jsonl",
    shards=True,
)

#: Each answer shard's sidecar, named by `run_combined` from the shard: the run id, the model, the
#: questions and the input hashes. The derive step reads each shard's label mapping from it.
GENRES_MOODS_ANSWERS_MANIFEST = Artifact(
    name="genres_moods_answers_manifest",
    filename="genres-moods-v1*.jsonl.manifest.json",
    shards=True,
)

#: Genres & moods per title: the curated file's entries plus those derived from the answers above under
#: `data/genres-moods-rule.json`. Rebuilt on every run and never read back into its own production.
GENRES_MOODS = Artifact(
    name="genres_moods",
    filename="genres-moods.json",
)

#: The second pass — the questions `combined-v1-r2` did not ask. It buys from a paid provider, and without
#: `--spend` the script prints the estimate and stops, so the command a refusal quotes carries it.
#:
#: `v2`, not `v1`: every `delta-v1` row was answered from the title alone, because the pass patched out the
#: function `classify()` builds its state from, so Jev never saw the article (fixed in #60). The glob names
#: the generation that saw the article; a `v1` file left in an out-dir is read by nothing.
DELTA = Artifact(
    name="delta",
    filename="delta-v2*.jsonl",
    producer="scripts/v2/run_delta.py",
    how="scripts/v2/run_delta.py --spend",
    shards=True,
)

#: The enrichment's batch files, named as the DIRECTORY that holds them rather than as a shard set. The
#: embed pass reads them in batch-number order and lets the newest win, because 505 keys appear in several
#: batches disagreeing about `hasWikiPlot` — which decides whether a title embeds with its plot or without
#: one. Resolving the set here would move that rule out of the only reader that has it.
ENRICHED = Artifact(
    name="enriched",
    filename="enriched",
    dedicated=False,
)

#: What a resumed enrichment resumes FROM: the ids already processed, and the next batch number. Declared
#: rather than left as a file the command happens to keep, because it is the only thing that separates a
#: second drain from a second full scrape — and because an absent one is not proof of a first run (an
#: out-dir with 153 batches and no checkpoint restarted at batch 1 and overwrote two of them).
#:
#: Optional to its one reader, finalize, which takes three counters for `report.json` from it: a store
#: embedded from another out-dir's batches has none, and the report says zero.
ENRICH_CHECKPOINT = Artifact(
    name="enrich_checkpoint",
    filename="enrich-checkpoint.json",
    dedicated=False,
    required=False,
)

#: Wikidata's director (P57) and genre (P136) per title: the two clauses of the lean document that used to
#: come from TMDB. Scraped separately from the embed pass because it is ~770 SPARQL requests.
DOC_FACTS = Artifact(
    name="doc_facts",
    filename="doc-facts.json",
)

CORPUS = Artifact(
    name="corpus",
    filename="corpus-{version}.jsonl.gz",
)

#: The entity table, written by the same join and unreadable apart from the corpus: the Q-ids in every
#: row name entities that live here, once, rather than repeated 47,529 times.
ENTITIES = Artifact(
    name="entities",
    filename="corpus-{version}-entities.json.gz",
)

#: The embed pass's two append-only stores, written line by line and resumed from. The finalize stage turns
#: them into `labels-t02.json` and `vectors-bge-m3.bin`; until then they are the corpus's vectors. Paired by
#: position, which is why a kill between the two lines is repaired before anything appends to them.
EMBED_LABELS = Artifact(
    name="embed_labels",
    filename="index/labels.jsonl",
    dedicated=False,
)

EMBED_VECTORS = Artifact(
    name="embed_vectors",
    filename="index/vectors.jsonl",
    dedicated=False,
)

#: How the documents in those stores were composed. The embedder identity cannot see it and two runs of
#: one service differ entirely on one clause, so the shape is recorded as its own fact and appending in a
#: different one is refused.
COMPOSITION = Artifact(
    name="composition",
    filename="index/composition.json",
    dedicated=False,
)

#: Which den-embed built the store. `finalize` only ever checked that the vectors share one LENGTH, which
#: every bge-m3 generation does, so a run resumed after an upgrade produced a corpus half-embedded by each.
#: Optional to finalize: a store from before it was recorded is legitimate, and is carried as an absent key.
EMBEDDER = Artifact(
    name="embedder",
    filename="index/embedder.json",
    dedicated=False,
    required=False,
)

#: The known-answer canary's verdict for the service that embedded these rows — the space's published name,
#: which `finalize` stamps into `dataset.meta.json`. Optional there for the same reason as the embedder: a
#: store built before the canary existed names no space, and guessing one is what the canary is against.
EMBEDDING_SPACE = Artifact(
    name="embedding_space",
    filename="index/embedding-space.json",
    dedicated=False,
    required=False,
)

#: The corpus pass's scrape: the titles that have a vector, stamped `hasVector`. Named with the suffix
#: `doc-facts.pre-merge.json` and `labels-t02.pre-classify.json` already use for the copy of an artifact
#: from before the step that consumes it — the merge's output takes `facts-<version>.json`.
CORPUS_FACTS = Artifact(
    name="corpus_facts",
    filename="facts-{version}.pre-merge.json",
)

#: The ids the delta pass scrapes: titles /recommend needs facts for that have no vector — which is not the
#: same as new: a title the classify pass drops from the labels loses its vector and lands here. No stage
#: derives the list; docs/OPERATE.md step 6a gives the rule (the last published facts file's titles minus
#: the new labels', plus what atlas is missing), `movie:1` / `tv:2`, one per line or comma-separated. An old
#: list is not reusable: the 8,949 ids the last rebuild scraped as vectorless all have vectors now.
DELTA_IDS = Artifact(
    name="delta_ids",
    filename="facts-delta-ids.txt",
    producer="docs/OPERATE.md",
    how="list the ids to scrape without a vector (movie:1, tv:2 …) into facts-delta-ids.txt — "
        "docs/OPERATE.md step 6a",
)

#: The delta pass: the ids above, scraped WITHOUT `hasVector`.
DELTA_FACTS = Artifact(
    name="delta_facts",
    filename="facts-{version}.delta.json",
)

#: What ships, and what the corpus join and the store read: the two passes merged. One stage owns the
#: passes and the merge together — a facts file rebuilt from one pass is the rebuild that dropped 137 delta
#: titles.
FACTS = Artifact(
    name="facts",
    filename="facts-{version}.json",
    manifest_key="factsFile",
)

VECTORS = Artifact(
    name="vectors",
    filename="vectors-bge-m3.bin",
    manifest_key="vectorsFile",
)

#: The plot pass's key set. Not the vectors' row order any more — the blob names its own rows — but the
#: independent record that column is checked against.
VECTOR_LABELS = Artifact(
    name="vector_labels",
    filename="labels-t02.json",
    manifest_key="labelsFile",
)

#: `gzip -k` of the labels, which the manifest names as `labelsGzFile`. No manifest key is claimed here:
#: `check-producers.py` treats a `*GzFile` as a copy of its source, owned by the source's producer.
VECTOR_LABELS_GZ = Artifact(
    name="vector_labels_gz",
    filename="labels-t02.json.gz",
)

#: The finalize run's tallies — titles per primary genre, the confidence histogram, and the enrichment's
#: counters. Read by a person, never by a stage.
FINALIZE_REPORT = Artifact(
    name="finalize_report",
    filename="report.json",
)

#: Optional to the writer: a corpus with no premise embedding still builds a store.
PREMISE_VECTORS = Artifact(
    name="premise_vectors",
    filename="vectors-premise.bin",
    producer="scripts/v2/embed_tags.py",
    how="scripts/v2/embed_tags.py",
    manifest_key="premiseVectorsFile",
    required=False,
)

PREMISE_LABELS = Artifact(
    name="premise_labels",
    filename="labels-premise.json",
    producer="scripts/build-premise-tags.py",
    how="scripts/build-premise-tags.py",
    manifest_key="premiseLabelsFile",
)

#: The only artifact a release carries (oxyc/den#113). Everything above is an input to it.
STORE = Artifact(
    name="store",
    filename="den-{version}.store",
    manifest_key="storeFile",
)

#: The manifest that describes the store, and the release's commit point — it is uploaded last, so what
#: `data-latest` carries is whatever it names. The finalize stage creates it; the store writer and the
#: publisher rewrite it in place (the stamp, the prune, the record counts, `storeRebuild`, the
#: shared-article census), which is why every writer merges over it rather than replacing it.
MANIFEST = Artifact(
    name="manifest",
    filename="dataset.meta.json",
)

#: What a publish makes: the moving `data-latest` GitHub release den-atlas fetches. The only artifact here
#: that is not a file in the out-dir — `filename` is the release TAG, and `Context.path` refuses to turn it
#: into a local path. Declared because the pipeline's last stage still has to say what it produces, and
#: because "what publishes the dataset" is then read off the order like every other ownership question.
RELEASE = Artifact(
    name="release",
    filename="data-latest",
    remote=True,
)

CATALOGUE = (EXPORT_MOVIE, EXPORT_TV, UNIVERSE_MOVIE, UNIVERSE_TV, ARTICLES, COMBINED,
             COMBINED_MANIFEST, GENRES_MOODS_ANSWERS, GENRES_MOODS_ANSWERS_MANIFEST, GENRES_MOODS,
             DELTA, ENRICHED, ENRICH_CHECKPOINT, DOC_FACTS, EMBED_LABELS,
             EMBED_VECTORS, COMPOSITION, EMBEDDER, EMBEDDING_SPACE, CORPUS, ENTITIES, CORPUS_FACTS,
             DELTA_IDS, DELTA_FACTS, FACTS, VECTORS, VECTOR_LABELS, VECTOR_LABELS_GZ, FINALIZE_REPORT,
             PREMISE_VECTORS, PREMISE_LABELS, STORE, MANIFEST, RELEASE)
