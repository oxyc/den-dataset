#!/usr/bin/env python3
"""The pipeline, in order. This file is the shape of the whole thing.

`docs/OPERATE.md` used to be the order — written out by hand, remembered rather than invoked. `STAGES`
is the order now, and `den run` is a loop over it.

Each name is a module beside this one declaring `INPUTS`, `OUTPUTS` and `run(ctx)`; see
`pipeline/contract.py` for what that means and why the declaration is load-bearing rather than
descriptive. To find out what a stage reads and writes, open it and read the two tuples at the top — no
other file holds that answer, and none may.

The rule that keeps this honest: a module under `pipeline/` that nothing in `STAGES` reaches is deleted.
`guards/reachable.py` fails CI on one, because `scripts/v2/` is what happens when that rule is written
down and not enforced.
"""
from .contract import (Artifact, Binding, Context, StageError, bind, load,  # noqa: F401
                       registry, validate)

#: The pipeline, in the order it runs. Embedding comes first because its stores are what `finalize` turns
#: into the vectors the store is built from; the corpus is joined before the store for the same reason;
#: and publishing is last because it uploads what the store wrote. That ordering is the whole reason a
#: stage can stop naming a producer for an artifact another stage makes.
#:
#: `den run` STOPS BEFORE PUBLISHING unless asked with --publish: every other stage writes into the
#: out-dir and can be run again, while publish replaces the moving `data-latest` release. The order below
#: is what the pipeline IS; what one command chooses to run is a separate question (see `PUBLISHES`).
#:
#: Short because the port is four stages in. The rest — worklist, fetch, classify — still live under
#: `scripts/`, run from `docs/OPERATE.md`, and join here one at a time (oxyc/den-dataset#27).
STAGES = ("embed", "corpus", "store", "publish")


def stage(name):
    """One stage module, validated. Refuses a name that is not in `STAGES`: a stage you can run and
    cannot see in the order is exactly the thing this list exists to prevent."""
    if name not in STAGES:
        raise StageError(f"no stage named {name!r}. The pipeline is: {', '.join(STAGES)}")
    return load(name)


def stages():
    """Every stage, in order."""
    return tuple(load(name) for name in STAGES)


def declared():
    """Every artifact the pipeline names, inputs and outputs, in stage order."""
    out = []
    for module in stages():
        for entry in tuple(module.INPUTS) + tuple(module.OUTPUTS):
            out.append(bind(entry).artifact)
    return tuple(out)


def producers():
    """`{artifact name: (producer, how, dedicated)}` — the producer registry, derived from `STAGES`."""
    return registry(stages())
