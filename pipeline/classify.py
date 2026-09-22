#!/usr/bin/env python3
"""The CLASSIFY pass, behind the stage contract.

The rule lives in `pipeline/run_combined.py`: the validator that refuses all three System One response
shapes when they are malformed, the planner that never asks about a section it did not send, the shared
circuit breaker that stops every worker after one systemic provider failure, the kernel lock that stops a
second agent buying the same calls, and the sidecar manifest that hashes the input, the questions, the
prompt, the taxonomy, the model and the pass's own source files. Every one of them was bought by something
that went wrong on a paid run, and none of it is reimplemented or wrapped in new behaviour here: this stage
decides which files the pass is handed and runs it.

**This is the one stage whose cost is money.** The shipped pass was 47,529 titles, 47,535 calls and $20.47,
and it is the artifact `docs/OPERATE.md` says will not be run again. A rerun buys it all a second time
unless it RESUMES: the pass reads the rows already in `--out`, holds them against the manifest's run id and
config hash, and classifies only what is missing. So the stage points the command at the same shard the
corpus join globs rather than at a fresh name — a new name is a new manifest, and a new manifest is another
$20. `ctx.plan` is the dry run the launch procedure is written around: it parses the input, prints the call
and cost plan, and touches neither the provider nor the output.

**The stage pins nothing the pass already pins.** `--model` defaults to the immutable `jev-1.13.0` and the
pass refuses a `*-latest` alias by itself; `--prompt` and `--taxonomy` resolve from the pass's own directory
rather than from the working one, so they are right wherever `den` was typed. Naming any of them here would
be a second spelling of a constant that is already hashed into the manifest — and the manifest is what makes
a shard's provenance checkable, so a spelling that drifted would read as a different configuration and
refuse the resume rather than fail visibly.
"""
import os
import subprocess
import sys

from . import artifacts
from .contract import REPO, StageError, bind

NAME = "classify"

#: The rule this stage runs, repo-relative — the one spelling. `registry()` registers the outputs below
#: against it, so what the pipeline says classified the corpus is what the pipeline executes.
PRODUCER = "pipeline/run_combined.py"
HOW = "pipeline/run_combined.py"
#: Writes into the out-dir and nowhere else. A repeat run costs nothing it already has — see the resume
#: above — which is a different thing from costing nothing.
PUBLISHES = False
#: The only stage that buys. `den run` leaves it out unless asked with `--spend`, because the resume is a
#: mitigation and not a guarantee: an article dump rebuilt with more titles makes the difference missing,
#: and a run that was meant to rebuild a store buys it. `--plan` cannot warn about that — it estimates the
#: whole input without consulting what is done, so it reads ~$20 whether the run would buy everything or
#: nothing.
SPENDS = True
SCRIPT = os.path.join(REPO, PRODUCER)

#: In the pass's own argument order, which `classify_test.py` holds against its parser. `--enriched-dir` is
#: what supplies the target year and the extractor's plot headings for an article dump written before those
#: fields existed; the frozen dump is one, so the documented command passes it and so does this.
INPUTS = (
    artifacts.ARTICLES,
    artifacts.ENRICHED.called("enriched_dir"),
)

#: The rows, and the sidecar that says what bought them. Both are sets: the pass writes one shard and a
#: capacity quarantine adds more under the same glob.
OUTPUTS = (artifacts.COMBINED, artifacts.COMBINED_MANIFEST)


def argv(ctx):
    """The pass's command line, built from the declaration.

    Every input is required. A missing one stops here, naming its producer — see `Context.require` — which
    matters more on this stage than on the others: an article dump that is short by a batch is a pass that
    classifies fewer titles, reports success, and costs the same per title as the right one.
    """
    command = [sys.executable, SCRIPT]
    for entry in (bind(e) for e in INPUTS):
        path = ctx.require(entry.artifact)
        if path is not None:
            command += [entry.flag(), path]
    command += ["--out", ctx.shard(artifacts.COMBINED)]
    if ctx.plan:
        command.append("--plan")
    return command


def check_outputs(ctx):
    """The rows and the manifest, where the declaration says they are.

    The manifest's name is DERIVED from `--out` rather than taken as a flag, so an override that points the
    rows somewhere the declaration does not describe splits a shard from its provenance — and
    `audit_combined.py`, which is what stands between a corrupted bundle and a published dataset, looks the
    manifest up by that derived name and nowhere else.
    """
    for entry in (bind(e) for e in OUTPUTS):
        ctx.require_all(entry.artifact)


def run(ctx):
    """Classify the articles into the pass shards. Returns the shard's path.

    Resumable: a run against an out-dir that already holds the shard continues where the last one stopped,
    and one that finds every title classified buys nothing and exits clean.
    """
    out = ctx.shard(artifacts.COMBINED)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    result = subprocess.run(argv(ctx))
    if result.returncode != 0:
        raise StageError(f"classify: {SCRIPT} exited {result.returncode}")
    # A plan writes nothing on purpose, so there is nothing to check for and an absent shard is the
    # expected outcome rather than a stage that lost its output.
    if ctx.plan:
        return f"planned only — nothing bought, nothing written to {out}"
    check_outputs(ctx)
    return out
