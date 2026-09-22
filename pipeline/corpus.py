#!/usr/bin/env python3
"""The CORPUS join, behind the stage contract.

The rule lives in `scripts/v2/consolidate_corpus.py`: the spine that is the union of the facts and the
pass rather than the pass alone, the labels lookup that fails instead of returning something dict-like,
the shard duplicate check, the prose refusal, and the join guards that count against the ARTIFACT rather
than against a fraction of the corpus. Every one of them was bought by a failure that happened silently,
and none of it is reimplemented or wrapped in new behaviour here: this stage decides which files the join
is handed and runs it, byte for byte.

What the stage adds is the declaration below, and two things it had to teach the contract:

  * `combined` and `delta` are SETS. The reader takes each flag once per shard, and the set is resolved
    from the declared glob — so the stage cannot hand over one shard of three, which is exactly how
    eleven titles left a derived blob for a day.
  * the corpus is the STORE's input. The two stages are in one order now, so `scripts/check-producers.py`
    reads "what builds the corpus" off the stage that writes it rather than off a field on the artifact
    that could name one script while the stage ran another.

The sidecar is not a flag: the join derives `corpus-<ver>-entities.json.gz` from `--out`. The stage
declares it as an output and checks it landed where the declaration says, because the alternative is a
second copy of that derivation here, and a file the publish guard searches for in the wrong place.
"""
import os
import subprocess
import sys

from . import artifacts
from .contract import REPO, StageError, bind

NAME = "corpus"

#: The rule this stage runs, repo-relative — the one spelling. `registry()` registers the outputs below
#: against it, so what the pipeline says builds the corpus is what the pipeline executes.
PRODUCER = "scripts/v2/consolidate_corpus.py"
HOW = "scripts/v2/consolidate_corpus.py"
#: Writes into the out-dir and nowhere else, so a repeat run costs only time.
PUBLISHES = False
SPENDS = False
SCRIPT = os.path.join(REPO, PRODUCER)

#: In the join's own argument order, which `corpus_test.py` holds against `consolidate_corpus.INPUT_ARGS`.
#: `labels-t02.json` is `--vector-labels` to the store writer and `--labels` here; the file keeps one
#: name and each reader keeps its own word for it.
INPUTS = (
    artifacts.COMBINED,
    artifacts.DELTA,
    artifacts.FACTS,
    artifacts.VECTOR_LABELS.called("labels"),
    artifacts.PREMISE_LABELS,
)

OUTPUTS = (artifacts.CORPUS, artifacts.ENTITIES)


def argv(ctx):
    """The join's command line, built from the declaration.

    A shard set contributes its flag once per member, in sorted order; the join refuses a key that
    appears in two shards, so the order changes nothing it writes. A required input that is absent stops
    here, naming its producer — see `Context.require`.
    """
    command = [sys.executable, SCRIPT]
    for entry in (bind(e) for e in INPUTS):
        if entry.artifact.shards:
            for path in ctx.require_all(entry.artifact):
                command += [entry.flag(), path]
            continue
        path = ctx.require(entry.artifact)
        if path is not None:
            command += [entry.flag(), path]
    # `--expect` only when a count was declared. Passing it unasked would hold every run to a number
    # nobody chose; leaving it off when one was named would drop the guard that catches a missing shard.
    if ctx.expect is not None:
        command += ["--expect", str(ctx.expect)]
    command += ["--out", ctx.path(artifacts.CORPUS)]
    return command


def run(ctx):
    """Join the passes, facts and labels into the corpus. Returns its path."""
    out = ctx.path(artifacts.CORPUS)
    entities = ctx.path(artifacts.ENTITIES)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    result = subprocess.run(argv(ctx))
    if result.returncode != 0:
        raise StageError(f"corpus: {SCRIPT} exited {result.returncode}")
    if not os.path.exists(entities):
        raise StageError(
            f"corpus: the join wrote {out} and no entities file at {entities}. It derives the sidecar's "
            f"name from --out, so an output path that does not end corpus-<ver>.jsonl.gz puts the entity "
            f"names somewhere the store stage and the publish guard do not look.")
    return out
