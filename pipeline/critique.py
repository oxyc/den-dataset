#!/usr/bin/env python3
"""The CRITIQUE pass — the delta question set — behind the stage contract.

The rule lives in `pipeline/run_delta.py`: the questions `combined-v1-r2` did not ask (critique, technique,
depiction, audience), sent with the SAME state the classify pass sent each title, read from that pass's
rows, over `run_combined`'s machinery unchanged. None of it is reimplemented here: this stage decides which
article dump and which classify shards the pass is handed, and runs it.

**It runs after the classify stage, over what that stage just classified.** The corpus join refuses a title
whose kept critique row read a different article from its kept classify row
(`pipeline/consolidate_corpus.py`), so a title classified again must be critiqued again from the same
dump. With a change set that has a live baseline (`pipeline/changes.py`) that is the change set's own dump
and the classify shard named by its digest, into a delta shard named by the same digest, which supersedes
the older ones by key. Without one, the shared dump and every classify shard into `delta-v2.jsonl` — the
documented whole-corpus pass.

**It buys.** `den run` leaves it out without `--spend`, as it leaves out the classify stage. The pass keeps
its own opt-in too: the stage hands it `--spend` only when the run was given `--spend`, so `den stage
critique` alone prints the pass's estimate and stops. `--plan` is the pass's own dry run.
"""
import os
import subprocess
import sys

from . import artifacts, changes, classify
from .contract import REPO, StageError, bind

NAME = "critique"
PRODUCER = "pipeline/run_delta.py"
HOW = "./den stage critique --spend --out-dir <dir>"
#: Writes into the out-dir and nowhere else.
PUBLISHES = False
#: The second of the two passes that buy.
SPENDS = True
SCRIPT = os.path.join(REPO, PRODUCER)
#: What the pass exits with when it would buy and was not told to.
REFUSED_WITHOUT_SPEND = 2

#: What the pass parses, in its own argument order, and the change set, which decides what it is handed.
PASSED = (
    artifacts.ARTICLES,
    artifacts.ENRICHED.called("enriched_dir"),
    artifacts.COMBINED,
)
INPUTS = PASSED + (artifacts.CHANGED_ARTICLES, artifacts.CHANGES)
OUTPUTS = (artifacts.DELTA, artifacts.DELTA_MANIFEST)


def argv(ctx, articles=None, combined=None, out=None):
    """The pass's command line. `articles`, `combined` and `out` are a change set's dump, its classify shard
    and its delta shard; without them, the declared dump, every classify shard and the declared shard."""
    command = [sys.executable, SCRIPT]
    for entry in (bind(e) for e in PASSED):
        if entry.artifact is artifacts.COMBINED:
            for shard in ([combined] if combined else ctx.require_all(entry.artifact)):
                command += [entry.flag(), shard]
            continue
        path = ctx.require(entry.artifact)
        command += [entry.flag(), articles if articles and entry.artifact is artifacts.ARTICLES else path]
    command += ["--out", out or ctx.shard(artifacts.DELTA)]
    if ctx.plan or ctx.spend:
        command.append("--plan" if ctx.plan else "--spend")
    return command


def run(ctx):
    """Critique what the classify stage classified. Returns the delta shard."""
    articles = combined = out = None
    if changes.planned(ctx) is not None:
        found = classify.changed_articles(ctx)
        if found is None:
            return "nothing to critique — no title the change set lists has an article"
        articles, digest = found
        combined = classify.named(ctx, artifacts.COMBINED, digest)
        if not os.path.exists(combined):
            if ctx.plan:
                return f"nothing to plan — {combined} is not classified yet, and each title is sent its state"
            raise StageError(f"critique: {combined} does not exist, and each title is sent the state the "
                             f"classify pass sent it. Run ./den stage classify --spend first.")
        out = classify.named(ctx, artifacts.DELTA, digest)
    out = out or ctx.shard(artifacts.DELTA)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    result = subprocess.run(argv(ctx, articles, combined, out))
    if result.returncode == REFUSED_WITHOUT_SPEND and not (ctx.spend or ctx.plan):
        raise StageError(f"critique: the pass buys, and this run was not given --spend. The estimate is "
                         f"above; `{HOW}` buys it.")
    if result.returncode != 0:
        raise StageError(f"critique: {SCRIPT} exited {result.returncode}")
    if ctx.plan:
        return f"planned only — nothing bought, nothing written to {out}"
    for suffix in ("", ".manifest.json"):
        if not os.path.exists(out + suffix):
            raise StageError(f"critique: the pass exited clean and wrote no {out + suffix}")
    return out
