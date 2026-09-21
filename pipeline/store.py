#!/usr/bin/env python3
"""The STORE build, behind the stage contract.

The rule lives in `scripts/v2/build_store.py` and the `store/` package it drives — the section writers,
join guards and count asserts, every one of them bought by a join bug this pipeline actually had. None of
it is reimplemented
here and none of it is wrapped in new behaviour: this stage decides which files the writer is handed and
runs it, so `den stage store` and the hand-typed command in `docs/OPERATE.md` produce the same bytes.

What it does add is the declaration below. The writer's inputs used to be described in three places — its
own `INPUT_ARGS`, the command in the docs, and `STORE_INPUTS` in `scripts/check-producers.py` — and the
third drifted from the first twice in one day. Now the argument list is BUILT from `INPUTS`, so a
declaration that disagrees with the writer fails at its argument parser rather than in a registry nobody
runs, and `check-producers.py` reads the same tuple instead of keeping a copy.
"""
import os
import subprocess
import sys

from . import artifacts
from .contract import REPO, StageError, bind

NAME = "store"

#: The rule this stage runs, repo-relative — the one spelling. `registry()` registers the store against
#: it, so the producer the guard names is the file the stage executes.
PRODUCER = "scripts/v2/build_store.py"
HOW = "scripts/v2/build_store.py --stamp-meta"

#: In the writer's own argument order, which `store_test.py` holds against `build_store.INPUT_ARGS`.
INPUTS = (
    artifacts.CORPUS,
    artifacts.ENTITIES,
    artifacts.FACTS,
    artifacts.VECTORS,
    artifacts.VECTOR_LABELS,
    artifacts.PREMISE_VECTORS,
    artifacts.PREMISE_LABELS,
)

OUTPUTS = (artifacts.STORE,)

WRITER = os.path.join(REPO, PRODUCER)


def argv(ctx):
    """The writer's command line, built from the declaration.

    An optional input that is absent leaves its flag off, the way the hand-typed command does. A required
    one that is absent stops here, naming its producer — see `Context.require`.
    """
    command = [sys.executable, WRITER]
    for entry in (bind(e) for e in INPUTS):
        path = ctx.require(entry.artifact)
        if path is not None:
            command += [entry.flag(), path]
    # `--out` rather than `--store`: the writer names its output by role, not by artifact.
    command += ["--dataset-version", ctx.dataset_version, "--out", ctx.path(artifacts.STORE)]
    # Without this the store is written and no manifest names it, so `publish-dataset.sh` refuses it as
    # an unowned blob and den-atlas never loads it.
    if ctx.stamp_meta:
        command += ["--stamp-meta", ctx.stamp_meta]
    return command


def run(ctx):
    """Build the store. Returns its path."""
    out = ctx.path(artifacts.STORE)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    result = subprocess.run(argv(ctx))
    if result.returncode != 0:
        raise StageError(f"store: {WRITER} exited {result.returncode}")
    return out
