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

#: The pipeline, in the order it runs. The worklist comes first because it is the universe everything after
#: it is drawn from; the enrichment drain next, because the batches it fetches are what every later pass
#: reads the plots and the evidence out of; the article dump after it, because it is what the classify
#: pass reads; classification then, because nothing else here reads the articles; genres & moods after it,
#: because the classify pass's section roles choose the premise text they are asked about; the doc facts
#: before the embed pass, because they are two clauses of the document it composes and a run without them builds a
#: different vector space; finalize straight after the embedding, because it turns the embed stores into
#: `labels-t02.json`, the vector blob and the manifest; the facts after that, because the corpus facts pass
#: scrapes the ids in that labels file; the facts before the corpus join and the store, because both of
#: them read the merged facts; and publishing is last because it uploads what the store wrote. That
#: ordering is the whole reason a stage can stop naming a producer for an artifact another stage makes.
#:
#: `den run` STOPS BEFORE PUBLISHING unless asked with --publish: every other stage writes into the
#: out-dir and can be run again, while publish replaces the moving `data-latest` release. The order below
#: is what the pipeline IS; what one command chooses to run is a separate question (see `PUBLISHES`).
#:
#: `embed` READS `labels-t02.json` for each title's tags and `finalize` rewrites it from the stores `embed`
#: appended to, so the file an embed composes from is the previous finalize's. That is the loop a re-embed
#: has always been — the labels are decided by the classify pass, not by the vectors — and it is why the
#: labels artifact has one owner and two readers ahead of it. `genres-moods.json` is not read by any of them
#: yet; moving those readers onto it is what ends that loop.
STAGES = ("worklist", "fetch", "articles", "classify", "genres_moods", "docfacts", "embed", "finalize",
          "facts", "corpus", "store", "publish")


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
