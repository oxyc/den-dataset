#!/usr/bin/env python3
"""The STORE build, behind the stage contract.

The rule lives in `pipeline/build_store.py` and the `store/` package it drives — the section writers,
join guards and count asserts, every one of them bought by a join bug this pipeline actually had. None of
it is reimplemented
here and none of it is wrapped in new behaviour: this stage decides which files the writer is handed and
runs it, so `den stage store` and the hand-typed command in `docs/OPERATE.md` produce the same bytes.

What it does add is the declaration below. The writer's inputs used to be described in three places — its
own `INPUT_ARGS`, the command in the docs, and `STORE_INPUTS` in `pipeline/check_producers.py` — and the
third drifted from the first twice in one day. Now the argument list is BUILT from `INPUTS`, so a
declaration that disagrees with the writer fails at its argument parser rather than in a registry nobody
runs, and `check_producers.py` reads the same tuple instead of keeping a copy.
"""
import json
import os
import shutil
import subprocess
import sys

from . import artifacts
from .contract import REPO, StageError, bind

NAME = "store"

#: The rule this stage runs, repo-relative — the one spelling. `registry()` registers the store against
#: it, so the producer the guard names is the file the stage executes.
PRODUCER = "pipeline/build_store.py"
HOW = "pipeline/build_store.py --stamp-meta"
#: Writes into the out-dir and nowhere else, so a repeat run costs only time.
PUBLISHES = False
SPENDS = False

#: In the writer's own argument order, which `store_test.py` holds against `build_store.INPUT_ARGS`. The
#: genres & moods reach the store through the corpus, which joined them from `genres-moods.json`. The two
#: labels files are read for their keys: the record each vector blob is checked against, and — for the
#: plot one, which `finalize` wrote from the same `genres-moods.json` — the count of titles that must
#: carry genres & moods. The writer's argument list is den-spec's fixture generator's too.
INPUTS = (
    artifacts.CORPUS,
    artifacts.ENTITIES,
    artifacts.FACTS,
    artifacts.VECTORS,
    artifacts.VECTOR_LABELS,
    artifacts.PREMISE_VECTORS,
    artifacts.PREMISE_LABELS,
    artifacts.FRANCHISES,
    artifacts.PREMISE_TAGS,
    artifacts.JEV_MORE_LIKE,
    artifacts.FAN_PICKS,
)

OUTPUTS = (artifacts.STORE,)

WRITER = os.path.join(REPO, PRODUCER)

#: Inputs that are committed files rather than out-dir artifacts: the path read when the out-dir holds no
#: copy and no override names one.
COMMITTED = {artifacts.PREMISE_TAGS.name: os.path.join(REPO, "data", artifacts.PREMISE_TAGS.filename)}


def materialize_committed(ctx):
    """Carry committed durable inputs into the generation before the store records what it read."""
    by_name = {artifact.name: artifact for artifact in INPUTS}
    for name, source in COMMITTED.items():
        if name in ctx.overrides:
            continue
        target = ctx.path(by_name[name])
        if os.path.exists(target):
            continue
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        shutil.copyfile(source, target)


def argv(ctx):
    """The writer's command line, built from the declaration.

    An optional input that is absent leaves its flag off, the way the hand-typed command does. A required
    one that is absent stops here, naming its producer — see `Context.require`.
    """
    command = [sys.executable, WRITER]
    supplied = set()
    for entry in (bind(e) for e in INPUTS):
        path = ctx.require(entry.artifact)
        if path is None and entry.name in COMMITTED and entry.name not in ctx.overrides:
            path = COMMITTED[entry.name]
        if path is not None:
            command += [entry.flag(), path]
            supplied.add(entry.arg)
    live = ctx.path(artifacts.PUBLISHED_META)
    if os.path.exists(live):
        try:
            with open(live, encoding="utf-8") as handle:
                record = json.load(handle).get("storeInputs") or []
        except (OSError, ValueError) as error:
            raise StageError(f"store: could not read the live manifest: {error}") from error
        if not isinstance(record, list):
            raise StageError("store: the live manifest's storeInputs is not a list")
        optional = {bind(entry).arg for entry in INPUTS if not bind(entry).artifact.required}
        live_optional = {entry.get("arg") for entry in record if isinstance(entry, dict)} & optional
        missing = sorted(live_optional - supplied)
        if missing:
            raise StageError(f"store: refusing to drop optional input(s) the live store used: "
                             f"{', '.join(missing)}. Restore them from the live generation's corpus bundle")
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
    # A stateless daily run must receive every durable input the store used. Materializing the committed
    # baseline here makes it part of this generation's corpus bundle even when no paid premise step ran.
    materialize_committed(ctx)
    result = subprocess.run(argv(ctx))
    if result.returncode != 0:
        raise StageError(f"store: {WRITER} exited {result.returncode}")
    return out
