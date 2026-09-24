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

**A daily run classifies only what moved** (oxyc/den-dataset#27). With a change set that has a live baseline
(`pipeline/changes.py`), the stage writes the article rows of the titles it lists to a dump of their own,
named by its digest, and classifies that dump into a shard named by the same digest. The shared article dump
grows and changes every day, so a run over it could never resume the shipped shard's manifest; the change
set's dump is small, fixed for the change set, and resumes its own. Its rows supersede the older shards' by
key, because its run started later (`pipeline/consolidate_corpus.py`). The critique stage then runs the
second pass over the same dump. Nothing about either pass changes: they are handed a different file.

**The stage pins nothing the pass already pins.** `--model` defaults to the immutable `jev-1.13.0` and the
pass refuses a `*-latest` alias by itself; `--prompt` and `--taxonomy` resolve from the pass's own directory
rather than from the working one, so they are right wherever `den` was typed. Naming any of them here would
be a second spelling of a constant that is already hashed into the manifest — and the manifest is what makes
a shard's provenance checkable, so a spelling that drifted would read as a different configuration and
refuse the resume rather than fail visibly.
"""
import hashlib
import json
import os
import subprocess
import sys

from lib import cache as caching

from . import artifacts, changes
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
PASSED = (
    artifacts.ARTICLES,
    artifacts.ENRICHED.called("enriched_dir"),
)
#: And the change set, which is not handed to the pass: it decides which dump the pass is given.
INPUTS = PASSED + (artifacts.CHANGES,)

#: The rows, and the sidecar that says what bought them. Both are sets: the pass writes one shard and a
#: capacity quarantine adds more under the same glob. And each change set's own article dump.
OUTPUTS = (artifacts.COMBINED, artifacts.COMBINED_MANIFEST, artifacts.CHANGED_ARTICLES)


def named(ctx, artifact, digest):
    """A member of a shard set named by a change set's digest: the declared glob with it in the wildcard."""
    return os.path.join(ctx.out_dir, artifact.filename.replace("*", f"-{digest}" if artifact is not
                                                                artifacts.CHANGED_ARTICLES else digest))


def changed_articles(ctx):
    """`(dump, digest)` for the change set's titles that have an article, the dump written if it is not
    there; None when nothing the change set lists has one.

    The rows are the shared dump's bytes, line for line, so the pass reads exactly what it would have read
    there. The digest names the dump and both passes' shards, so the same change set resumes the same
    shards however often the stage is started.
    """
    keys = changes.listed(ctx, "keys")
    kept = []
    with open(ctx.require(artifacts.ARTICLES), "rb") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                if f"{record['mediaType']}:{record['tmdbId']}" in keys:
                    kept.append(line if line.endswith(b"\n") else line + b"\n")
    if not kept:
        return None
    body = b"".join(kept)
    digest = hashlib.sha256(body).hexdigest()[:12]
    dump = named(ctx, artifacts.CHANGED_ARTICLES, digest)
    if not os.path.exists(dump):
        os.makedirs(os.path.dirname(dump), exist_ok=True)
        caching.write_atomically(dump, body)
    return dump, digest


def argv(ctx, articles=None, out=None):
    """The pass's command line, built from the declaration. `articles` and `out` are a change set's dump and
    shard (`changed_articles`); without them, the declared dump and the declared shard.

    Every input is required. A missing one stops here, naming its producer — see `Context.require` — which
    matters more on this stage than on the others: an article dump that is short by a batch is a pass that
    classifies fewer titles, reports success, and costs the same per title as the right one.
    """
    command = [sys.executable, SCRIPT]
    for entry in (bind(e) for e in PASSED):
        path = ctx.require(entry.artifact)
        if path is not None:
            command += [entry.flag(), articles if articles and entry.artifact is artifacts.ARTICLES else path]
    command += ["--out", out or ctx.shard(artifacts.COMBINED)]
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
    for artifact in (artifacts.COMBINED, artifacts.COMBINED_MANIFEST):
        ctx.require_all(artifact)


def run(ctx):
    """Classify the articles into the pass shards. Returns the shard's path.

    Resumable: a run against an out-dir that already holds the shard continues where the last one stopped,
    and one that finds every title classified buys nothing and exits clean. With a change set, only its
    titles, into their own shard; one that lists no title with an article buys nothing at all.
    """
    articles = out = None
    if changes.planned(ctx) is not None:
        found = changed_articles(ctx)
        if found is None:
            return "nothing to classify — no title the change set lists has an article"
        articles, digest = found
        out = named(ctx, artifacts.COMBINED, digest)
    out = out or ctx.shard(artifacts.COMBINED)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    result = subprocess.run(argv(ctx, articles, out))
    if result.returncode != 0:
        raise StageError(f"classify: {SCRIPT} exited {result.returncode}")
    # A plan writes nothing on purpose, so there is nothing to check for and an absent shard is the
    # expected outcome rather than a stage that lost its output.
    if ctx.plan:
        return f"planned only — nothing bought, nothing written to {out}"
    check_outputs(ctx)
    return out
