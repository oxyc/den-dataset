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

#: `taxonomy-backfill` builds four of these from one source file, so a staleness warning on it would fire
#: for reasons that have nothing to do with any one artifact — which is how a guard gets ignored to death.
BACKFILL = "Sources/taxonomy-backfill/main.swift"

#: The classify pass, in shards. Three of them today, named by the run that wrote them rather than by the
#: dataset version — the pass owns that naming, and this is a glob so the set is whatever the pass left
#: behind. Reading one shard of three is what took eleven titles out of a derived blob for a day; a stage
#: that only knows the set cannot repeat it. Point `--set combined=…` at another run's shards.
COMBINED = Artifact(
    name="combined",
    filename="combined-v1-r2*.jsonl",
    producer="scripts/v2/run_combined.py",
    how="scripts/v2/run_combined.py",
    shards=True,
)

#: The second pass — the questions `combined-v1-r2` did not ask.
DELTA = Artifact(
    name="delta",
    filename="delta-v1*.jsonl",
    producer="scripts/v2/run_delta.py",
    how="scripts/v2/run_delta.py",
    shards=True,
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

FACTS = Artifact(
    name="facts",
    filename="facts-{version}.json",
    producer=BACKFILL,
    how="taxonomy-backfill facts",
    dedicated=False,
    manifest_key="factsFile",
)

VECTORS = Artifact(
    name="vectors",
    filename="vectors-bge-m3.bin",
    producer=BACKFILL,
    how="taxonomy-backfill finalize",
    dedicated=False,
    manifest_key="vectorsFile",
)

#: The plot pass's key set. Not the vectors' row order any more — the blob names its own rows — but the
#: independent record that column is checked against.
VECTOR_LABELS = Artifact(
    name="vector_labels",
    filename="labels-t02.json",
    producer=BACKFILL,
    how="taxonomy-backfill finalize",
    dedicated=False,
    manifest_key="labelsFile",
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
#: `data-latest` carries is whatever it names. `finalize` writes it and the publisher rewrites it in place
#: (the prune, the record counts, `storeRebuild`, the shared-article census), which is why it is an input
#: here and not an output: the stage that CREATES it is not ported, and a refusal for a missing one has to
#: send an operator to finalize rather than back to the publisher. `dedicated` is false for the same
#: reason `facts` is — one binary writes four things, so a staleness warning on it would fire for reasons
#: that have nothing to do with the manifest.
MANIFEST = Artifact(
    name="manifest",
    filename="dataset.meta.json",
    producer=BACKFILL,
    how="taxonomy-backfill finalize",
    dedicated=False,
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

CATALOGUE = (COMBINED, DELTA, CORPUS, ENTITIES, FACTS, VECTORS, VECTOR_LABELS, PREMISE_VECTORS,
             PREMISE_LABELS, STORE, MANIFEST, RELEASE)
