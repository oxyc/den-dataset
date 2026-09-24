#!/usr/bin/env python3
"""The PUBLISH, behind the stage contract.

The rule lives in `pipeline/publish-dataset.sh`: the store-identity guard (`DEN_STORE_REBUILD`, which
caught a silent overwrite the first time it ran), the ownership guard, the manifest prune, the record-count
and coverage guards, the grounding ratchet, the dead-generation check, the alias gate, and the per-asset
upload retries that exist because a single multi-file `gh release upload` is all-or-nothing. Every one of
them was bought by something that shipped wrong. None of it is reimplemented here.

**This stage pre-empts nothing.** The other two resolve their inputs first, so a missing file is refused
with the command that builds it; this one hands over a directory and lets the publisher answer. It already
does: it refuses a dir with no `dataset.meta.json` ("run finalize first") and one with no store (naming
`build_store.py --stamp-meta`). A check here would be a second guard with the same job and its own drift,
in front of the one file in this repo where a guard going quiet clobbers a public release.

So what the stage contributes is the declaration below and the INVOCATION — which for this script is not
a formality:

  * it runs from the REPO ROOT, whatever directory `den` was typed in. The ownership guard resolves
    producer paths (`pipeline/…`) and `git ls-files` against the working directory, so run
    from anywhere else it looks for producers that are not there. That used to be a comment asking the
    operator to remember; it is now a property of running the stage.
  * the publish dir is therefore made ABSOLUTE first, or moving the working directory to the repo root
    would move which directory gets published.
  * the ENVIRONMENT is inherited, not rebuilt. Every override is a variable — `DEN_STORE_REBUILD`,
    `DEN_ALLOW_DROPPING_BLOBS`, `DEN_ALLOW_SHARED_PLOTS`, `DEN_ALLOW_STALE_STORE_INPUTS`,
    `DEN_ALLOW_UNOWNED_ARTIFACTS` — and so is `DEN_DATASET_REPO`, which decides which release is clobbered.

`OUTPUTS` is the release, which is the one artifact in the catalogue that is not a file. See
`artifacts.RELEASE`: the store and the manifest are what it CARRIES, and both are inputs here.
"""
import os
import subprocess

from . import artifacts
from .contract import REPO, StageError

NAME = "publish"

#: The rule this stage runs, repo-relative — the one spelling. For this stage the spelling is the whole
#: command: there are no flags to get wrong, only which file is allowed to clobber `data-latest`.
PRODUCER = "pipeline/publish-dataset.sh"
HOW = "pipeline/publish-dataset.sh <out-dir>"
#: Uploads to the moving `data-latest` release, so `den run` skips it unless asked with --publish.
#: The only stage whose effect leaves this machine, and the only one a repeat run cannot undo.
PUBLISHES = True
#: Uploads an artifact someone already paid for; it buys nothing itself.
SPENDS = False
SCRIPT = os.path.join(REPO, PRODUCER)

#: The release carries the store and the manifest that describes it (oxyc/den#113). The store is the store
#: stage's, so the registry reads that ownership off the order; the manifest is `finalize`'s until that
#: stage lands.
INPUTS = (artifacts.STORE, artifacts.MANIFEST)

OUTPUTS = (artifacts.RELEASE,)


def argv(ctx):
    """The publisher's command line: the script, and the directory to publish.

    One positional, which is the script's whole interface, plus `--check` for a `--plan` run: every gate,
    and nothing signed or uploaded (oxyc/den-dataset#27). Absolute, because `run` moves the working
    directory to the repo root for the ownership guard — a relative `--out-dir out` has to go on meaning
    the `out` the operator named, not the repo's.
    """
    return [SCRIPT, os.path.abspath(ctx.out_dir)] + (["--check"] if ctx.plan else [])


def run(ctx):
    """Publish the store and its manifest as `data-latest`. Returns the release.

    `subprocess.run` with no `env` passes this process's environment through untouched, which is how the
    guards' overrides reach them; `cwd` is the repo root because the ownership guard resolves producers
    and `git ls-files` against it. A non-zero exit is a guard that refused, and it ends the run here.
    """
    result = subprocess.run(argv(ctx), cwd=REPO)
    if result.returncode != 0:
        raise StageError(f"publish: {PRODUCER} exited {result.returncode}")
    if ctx.plan:
        return f"checked only — ready to publish {os.path.abspath(ctx.out_dir)}; nothing signed or uploaded"
    return artifacts.RELEASE.filename
