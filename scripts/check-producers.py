#!/usr/bin/env python3
"""Refuse to publish an artifact nothing in this repo knows how to build.

Twice now a published blob has had no committed producer, and both times it went wrong the same quiet way:

  * `facets.bin` was generated once, elsewhere, and carried forward by `finalize`'s unowned-key merge. Nothing
    rebuilt it when the corpus grew, so it fell **999 titles behind** — and a missing row is not a soft
    failure, because atlas orders browse rows by votes and a title with no row sorts by tmdbId.
  * `facts-slim-<version>.json` — the facts file atlas actually LOADS — was likewise generated once and
    carried forward, which is why it held exactly the fields atlas parsed at that moment. `basedOn` could be
    scraped, folded, published, and still be invisible to /recommend, because the full file grew and the slim
    one could not.

Neither was a coding mistake. Both were an artifact with no owner, drifting from the thing it derives from,
with nothing in the pipeline positioned to notice. The README already warns that an artifact whose source is
not committed gets described wrongly; this turns that warning into a check.

## What it checks

For every blob the manifest declares:

 1. **A producer is registered** for it here, and that producer still exists and is tracked by git. An
    unregistered blob fails: adding a new published artifact means saying what builds it.
 2. **The artifact is not older than its producer.** A producer edited after the blob was built means the blob
    was built by an older version of the rule — exactly the facts-slim case, where the KEEP list would have
    grown a field the shipped file lacked.

It deliberately does NOT check that the producer reproduces the bytes: that would mean rebuilding a corpus to
publish it. Staleness is the cheap signal that catches the real failure.

    scripts/check-producers.py <meta.json> <out-dir>
"""
import json
import os
import subprocess
import sys

# manifest key -> (producer, how to run it, is the producer DEDICATED to this artifact?).
#
# ADDING A PUBLISHED ARTIFACT MEANS ADDING A LINE HERE. That is the whole point: the registry is what makes
# "nothing builds this" a failure rather than something discovered months later.
#
# The third field gates the staleness warning only. `main.swift` holds the whole tool, so it is edited for
# reasons that have nothing to do with any one blob — warning on it would fire constantly and teach everyone
# to ignore the check, which is how a guard dies.
PRODUCERS = {
    "labelsFile": ("Sources/taxonomy-backfill/main.swift", "taxonomy-backfill finalize", False),
    "premiseLabelsFile": ("scripts/build-premise-tags.py", "scripts/build-premise-tags.py", True),
    "vectorsFile": ("Sources/taxonomy-backfill/main.swift", "taxonomy-backfill finalize", False),
    "premiseVectorsFile": ("scripts/v2/embed_tags.py", "scripts/v2/embed_tags.py", True),
    "metadataFile": ("Sources/taxonomy-backfill/main.swift", "taxonomy-backfill metadata", False),
    "factsFile": ("Sources/taxonomy-backfill/main.swift", "taxonomy-backfill facts", False),
    "factsSlimFile": ("scripts/build-facts-slim.py", "scripts/build-facts-slim.py", True),
    "facetsFile": ("scripts/build-facets-bin.py", "scripts/build-facets-bin.py", True),
    "plotFacetsFile": ("scripts/v2/aggregate_facets.py", "scripts/v2/aggregate_facets.py", True),
}

# Keys that name a DERIVED copy of another blob (the gzips publish-dataset.sh writes). They inherit their
# source's producer, so registering them separately would be noise.
DERIVED_SUFFIXES = ("GzFile",)


def tracked(repo_relative):
    """True when git knows this file — an untracked producer is the same problem one step removed."""
    result = subprocess.run(["git", "ls-files", "--error-unmatch", repo_relative],
                            capture_output=True, text=True)
    return result.returncode == 0


def main():
    meta_path, out_dir = sys.argv[1], sys.argv[2]
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)

    problems, warnings = [], []
    for key, name in sorted(meta.items()):
        if not isinstance(name, str) or not name or key.endswith(DERIVED_SUFFIXES):
            continue
        # Only keys that name a file on disk in the out-dir are artifacts.
        artifact = os.path.join(out_dir, name)
        if not os.path.exists(artifact):
            continue

        entry = PRODUCERS.get(key)
        if entry is None:
            problems.append(
                f"{key} ({name}): no producer registered. Nothing in this repo says how to build it, so "
                f"nothing will rebuild it when its inputs change — add it to PRODUCERS in "
                f"scripts/check-producers.py."
            )
            continue

        producer, how, dedicated = entry
        if not os.path.exists(producer):
            problems.append(f"{key}: its producer {producer} does not exist (build it with: {how})")
            continue
        if not tracked(producer):
            problems.append(f"{key}: its producer {producer} is not tracked by git — commit it, or the next "
                            f"machine cannot rebuild {name}")
            continue
        if dedicated and os.path.getmtime(producer) > os.path.getmtime(artifact):
            warnings.append(
                f"{key} ({name}): {producer} was edited after this artifact was built, so it may have been "
                f"built by an older version of the rule. Re-run: {how}"
            )

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if problems:
        print("error: published artifacts that nothing in this repo knows how to build:", file=sys.stderr)
        for problem in problems:
            print(f"       - {problem}", file=sys.stderr)
        print("       An artifact with no producer does not get rebuilt when its inputs change. That is how",
              file=sys.stderr)
        print("       facets.bin fell 999 titles behind the corpus, and how facts-slim kept shipping without",
              file=sys.stderr)
        print("       basedOn. Set DEN_ALLOW_UNOWNED_ARTIFACTS=1 to publish anyway.", file=sys.stderr)
        if os.environ.get("DEN_ALLOW_UNOWNED_ARTIFACTS") != "1":
            sys.exit(1)


if __name__ == "__main__":
    main()
