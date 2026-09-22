#!/usr/bin/env python3
"""The FACTS merge, behind the stage contract.

The rule lives in `scripts/merge-facts.py`: the corpus pass wins a collision because it carries the vector
and the fuller scrape, the two passes must agree on `schema`, and every distinct input record must appear
in the output. None of that is reimplemented here and none of it is wrapped in new behaviour: this stage
decides which files the merge is handed and runs it.

**The scrape runs twice and cannot run once.** `taxonomy-backfill facts --has-vector` covers the titles
that have a vector; a second pass covers the delta, which has no vector, no labels and no facets row.
`--has-vector` is read once per run and stamped on every record of that pass, and /recommend must never
let a vectorless record into an ANN path — so `hasVector` is per record, and one pass cannot state both.

**Nothing performed the merge, and that is what this stage is for.** The published `facts-<version>.json`
was assembled by hand, which is how a rebuild once dropped the 137 delta records: exactly the titles
nothing else covers, so the loss was invisible from every other artifact and surfaced only as /recommend
quietly losing library titles. Giving the merge a script fixed the rule; it did not give the file a
producer, because a script nothing invokes still leaves the assembling to whoever remembers to type it.

**The two passes collide on their filename.** Both write `facts-<version>.json` into `--out-dir`, taking
the version from `dataset.meta.json`, and the merge writes that name too — so left alone, one pass
overwrites the other and the merge overwrites what survives. The declaration names them apart, and a run
that finds the corpus pass still under the shipped name refuses with a message naming the move.

`--version` is passed rather than inherited. Without it the merged file takes its `datasetVersion` from
the corpus pass, which is a generation behind whenever the merge is what a new version is built on:
`out-repass/facts-5b1c3213b6a1.json` carries `"datasetVersion": "c85c707b0b18"` today, a filename and a
body naming different generations of the same artifact.
"""
import os
import subprocess
import sys

from . import artifacts
from .contract import REPO, StageError, bind

NAME = "facts"

#: The rule this stage runs, repo-relative — the one spelling. `registry()` registers the merged file
#: against it, so what the pipeline says builds the facts is what the pipeline executes.
PRODUCER = "scripts/merge-facts.py"
HOW = "scripts/merge-facts.py <corpus-facts.json> <delta-facts.json> <out.json> --version <ver>"
#: Writes into the out-dir and nowhere else, so a repeat run costs only time.
PUBLISHES = False
#: Reads two local files and writes a third. No provider, no key, nothing to buy.
SPENDS = False
SCRIPT = os.path.join(REPO, PRODUCER)

#: The two passes, in the order the merge takes them — which is not cosmetic. The FIRST file is the one
#: that wins a collision, so a stage that swapped them would resolve every overlapping title to the pass
#: with no vector and publish it as vectorless.
INPUTS = (artifacts.CORPUS_FACTS, artifacts.DELTA_FACTS)

OUTPUTS = (artifacts.FACTS,)


def argv(ctx):
    """The merge's command line, built from the declaration.

    Positional, as the script's own docstring writes it. A missing pass stops here naming what builds it —
    see `Context.require` — which is the whole guard against the failure this file exists for: a merge run
    with one pass absent is not a smaller merge, it is the published facts minus every title only that
    pass covers.
    """
    command = [sys.executable, SCRIPT]
    command += [ctx.require(bind(entry).artifact) for entry in INPUTS]
    command += [ctx.path(artifacts.FACTS), "--version", ctx.dataset_version]
    return command


def run(ctx):
    """Merge the two facts passes into the file that ships. Returns its path."""
    out = ctx.path(artifacts.FACTS)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    result = subprocess.run(argv(ctx))
    if result.returncode != 0:
        raise StageError(f"facts: {SCRIPT} exited {result.returncode}")
    return out
