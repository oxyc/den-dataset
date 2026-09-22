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

**It also audits the bundles before it joins them.** Three files in this repo call `audit_combined.py`
"the only thing between a corrupted bundle and a published dataset" and nothing ran it, which made the
claim a comment. This is where it belongs: the join is what turns the Jev shards into the corpus every
later stage derives from, so a shard whose provenance does not check out has to stop here rather than
be discovered after a store is built on it.

What runs per shard is `audit_combined.validate_implementation` — the manifest half of the audit, which
reads the sidecar and the four source files the pass hashes into it. The ROW half (`audit_combined.audit`)
is not a precondition of a join and is not run here: it re-reads the 445 MB article dump and reconstructs
every state and question set, which is the readback you do once per paid pass, not once per join. The
commands for it are in `scripts/v2/FACETS-V2.md`.
"""
import json
import os
import subprocess
import sys

from . import artifacts
from .contract import REPO, StageError, bind

sys.path.insert(0, os.path.join(REPO, "scripts", "v2"))
import audit_combined  # noqa: E402  — the bundle auditor, run against each shard's sidecar manifest

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

#: The inputs that are PAID Jev passes, each shard carrying a sidecar manifest. The other three inputs are
#: derived locally and carry no such record, so there is nothing here to check them against.
AUDITED = (artifacts.COMBINED, artifacts.DELTA)


def audit_bundles(ctx):
    """Check every bundled shard's provenance. Returns the recorded allowances it ran on.

    The sidecar's name is derived from the shard's, exactly as the pass writes it and the auditor reads
    it; a shard without one is a bundle nothing can answer for, which is the case worth stopping at.
    """
    granted = []
    for artifact in AUDITED:
        for shard in ctx.require_all(artifact):
            manifest_path = shard + ".manifest.json"
            if not os.path.exists(manifest_path):
                raise StageError(
                    f"corpus: {shard} has no manifest at {manifest_path}, so nothing says which run, "
                    f"model or source files produced its rows. The pass derives that name from --out — a "
                    f"shard written under a name of its own is split from its provenance.")
            with open(manifest_path, encoding="utf-8") as fh:
                config = json.load(fh).get("config", {})
            try:
                allowed = audit_combined.validate_implementation(os.path.basename(shard), config)
            except ValueError as refusal:
                raise StageError(f"corpus: {refusal}") from None
            granted.extend({"shard": os.path.basename(shard), **entry} for entry in allowed)
    # One line per recorded allowance rather than per shard: three shards of one pass were bought on one
    # version of the pass, and saying so three times buries which exception is actually in play.
    by_exception = {}
    for entry in granted:
        by_exception.setdefault((entry["file"], entry["sha256"]), []).append(entry)
    for (name, digest), entries in by_exception.items():
        first = entries[0]
        print(f"    {len(entries)} shard(s) bought on {name} {digest[:12]} ({first.get('commit', '?')}), "
              f"superseded by {first.get('supersededBy', '?')[:7]}: "
              f"{first.get('why', 'no reason recorded')}", file=sys.stderr)
    return granted


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
    audit_bundles(ctx)
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
