#!/usr/bin/env python3
"""Every file the pipeline reads or writes, declared once.

One entry per artifact, naming the file and what builds it. A stage references the entries it reads and
writes; nothing re-spells a path or a producer. `scripts/check-producers.py` reads its registry straight
off these, so an artifact that is here and owned by nobody cannot exist — the entry IS the ownership.

Producers outside `pipeline/` are the stages not ported yet. When a stage lands, the entry stays where it
is and its `producer` moves to that stage's own module; the entry is the seam, so nothing else has to
change at the same time. `artifacts_test.py` refuses an entry no stage names, which is the same attrition
rule the modules are held to: a declaration nothing reaches is deleted, not kept for later.
"""
from .contract import Artifact

#: `taxonomy-backfill` builds four of these from one source file, so a staleness warning on it would fire
#: for reasons that have nothing to do with any one artifact — which is how a guard gets ignored to death.
BACKFILL = "Sources/taxonomy-backfill/main.swift"

CORPUS = Artifact(
    name="corpus",
    filename="corpus-{version}.jsonl.gz",
    producer="scripts/v2/consolidate_corpus.py",
    how="scripts/v2/consolidate_corpus.py",
)

#: The entity table, released beside the corpus under the same version and by the same producer.
ENTITIES = Artifact(
    name="entities",
    filename="corpus-{version}-entities.json.gz",
    producer="scripts/v2/consolidate_corpus.py",
    how="scripts/v2/consolidate_corpus.py",
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
    producer="scripts/v2/build_store.py",
    how="scripts/v2/build_store.py --stamp-meta",
    manifest_key="storeFile",
)

CATALOGUE = (CORPUS, ENTITIES, FACTS, VECTORS, VECTOR_LABELS, PREMISE_VECTORS, PREMISE_LABELS, STORE)
