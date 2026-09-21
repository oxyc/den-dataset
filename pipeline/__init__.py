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
from .contract import Artifact, Context, StageError, load, registry, validate  # noqa: F401

#: The pipeline, in the order it runs.
#:
#: Short because the port is one stage in. The rest — worklist, fetch, corpus, classify, embed, publish —
#: still live under `scripts/`, run from `docs/OPERATE.md`, and join here one at a time (oxyc/den-dataset#27).
STAGES = ("store",)


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
        out.extend(module.INPUTS)
        out.extend(module.OUTPUTS)
    return tuple(out)


def producers():
    """`{artifact name: (producer, how, dedicated)}` — the derived producer registry."""
    return registry(declared())
