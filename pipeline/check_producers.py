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

 3. **The store was built from what is in the tree.** `build_store.py --stamp-meta` records every input
    it read into `storeInputs`; this re-hashes each one and refuses a store whose input has changed
    since, and asks question 2 of each input's producer against the recorded mtime.

    pipeline/check_producers.py <meta.json> <out-dir>
"""
import glob
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

sys.path.insert(0, REPO)
import pipeline  # noqa: E402  — the stage declarations this file's registries are derived from

# WHERE THESE REGISTRIES COME FROM.
#
# They used to be written out here by hand, and that is what went wrong: a registry nothing executes is a
# copy, and a copy drifts. `STORE_INPUTS` listed `metadata` after the store stopped reading it, then
# `enriched` after that stopped too — twice in one day, each caught by a test rather than by the guard.
#
# So the entries below are read off the pipeline's own ORDER. An artifact a stage writes is owned by that
# stage, which names the rule it runs once, beside the code that runs it — so `corpus`'s producer here is
# the script `pipeline/corpus.py` actually executes, not a field that could name a different one. An
# artifact only read comes from a stage that is not ported yet and answers for itself in
# `pipeline/artifacts.py`, until that stage lands. Either way the declaration is not descriptive:
# `pipeline/store.py` and `pipeline/corpus.py` build their command lines out of it, so a declaration that
# has drifted from what runs fails at an argument parser, not here, and there is no second list to forget.
#
# The third field gates the staleness warning only. A producer that writes several blobs is edited for
# reasons that have nothing to do with any one of them — warning on it would fire constantly and teach
# everyone to ignore the check, which is how a guard dies.
DECLARED = {a.name: a for a in pipeline.declared()}
REGISTRY = pipeline.producers()

# manifest key -> (producer, how to run it, is the producer DEDICATED to this artifact?).
#
# ADDING A PUBLISHED ARTIFACT MEANS DECLARING IT: an entry in `pipeline/artifacts.py` carrying a
# `manifest_key`. `metadataFile` and `facetsFile` have no entry: nothing builds either, and
# `prune_manifest.py` strips both keys from a manifest that still carries one before this check sees it.
# `facetsFile`'s producer, `scripts/build-facets-bin.py`, was deleted with the TMDB language, year and vote
# count it read off the enriched batches (oxyc/den-dataset#53).
PRODUCERS = {a.manifest_key: REGISTRY[a.name] for a in DECLARED.values() if a.manifest_key}

# `factsSlimFile`, `plotFacetsFile` and `railFacetsFile` were registered here until the store carried what
# they held. Their entries went with their producers: an entry naming a script this repo no longer has is a
# guard that fails on a key nothing publishes, and `check_producers_test.py` asserts every registered
# producer is a real tracked file — so a stale entry breaks the test rather than protecting anything.
#
# `factsFile` stays, now as the `manifest_key` on the `facts` entry in `pipeline/artifacts.py`. Its producer
# is the facts stage, which scrapes both halves and merges them — one owner for the whole file, which is the
# distinction the 137 lost delta records were bought with. The key is
# merely unpublished, not unbuildable: the loop below only visits keys the manifest actually names, so the
# entry costs nothing and covers a generation that publishes facts again.
#
# WHAT THIS GUARD NOW SEES, AND WHAT IT DOES NOT. `publish-dataset.sh` prunes the manifest down to the
# store (oxyc/den#113), so the only registered key the loop visits is `storeFile` — plus the corpus below,
# which is matched by filename rather than by key. Every other entry is dormant for the same reason
# `factsFile` is: the artifact is still BUILT, as an INPUT to the store, it is just no longer declared. So
# this loop cannot see it, and cannot warn that its producer was edited after it was made.
#
# That narrowing is closed BELOW rather than here: `build_store.py --stamp-meta` records every input it
# read, and `check_store_inputs` asks the same two questions of each one. This loop stays as it is — it
# answers for what the manifest DECLARES, and the record answers for what the store was built from.

# Keys that name a DERIVED copy of another blob (the gzips an earlier publisher wrote — nothing publishes
# one now, and the store must never have one: atlas mmaps it). They inherit their source's producer, so
# registering them separately would be noise.
DERIVED_SUFFIXES = ("GzFile",)

# Published artifacts that NO manifest key names, matched by filename instead.
#
# The loop below only sees keys in `dataset.meta.json`, so an artifact published on its own tag is
# invisible to it — and the corpus is exactly that: the source of truth every other artifact is built
# from, released as `corpus-<ver>`, owned by nothing as far as this guard could tell. Putting it in the
# serving manifest is not the fix: `fetch-dataset.sh` pulls every `*File` key, so the box would download
# 44 MB of corpus it never reads.
#
# The glob comes from the declared filename with the version wild, so the pattern searched for here and
# the name the pipeline writes cannot disagree.
#
# (glob, producer, how to run it)
UNMANIFESTED = tuple(
    (DECLARED[name].glob(), REGISTRY[name][0], REGISTRY[name][1])
    for name in ("corpus", "entities")
)

# ---- what the STORE was built FROM -------------------------------------------------------------------
#
# `build_store.py` argument -> the producer that builds what it points at, in the same
# `(producer, how, dedicated)` shape as PRODUCERS.
#
# This closes the narrowing the comment above describes. The loop over the manifest sees `storeFile` and
# nothing else, so the store's inputs — labels, vectors, facts and the corpus — are built but not
# declared, and a store built from a STALE input published clean: every
# guard passed, and none of them was looking at the thing that had not been rebuilt.
#
# Moving them into UNMANIFESTED was the other option and it is worse. Those entries carry no `dedicated`
# flag and `pipeline/finalize.py` produces two of them, so every publish would warn about a file edited for
# reasons that have nothing to do with any one artifact — which is how a guard dies.
#
# It is the STORE STAGE's declared inputs, verbatim. `pipeline/store.py` hands the writer exactly these
# and `pipeline/store_test.py` holds them against `build_store.INPUT_ARGS`, so the set here is the set
# the writer reads, by construction rather than by remembering — that was `metadata` and `enriched`,
# registered as store inputs after the writer had stopped taking them.
#
# Keyed by the WRITER's argument name, because that is what `--stamp-meta` records; the registry is keyed
# by the artifact's pipeline-wide name. The two are the same word for every store input and are not
# required to be — `labels-t02.json` is `--vector-labels` here and `--labels` to the corpus join.
#
# What is asked of each recorded input, in `check_store_inputs` below:
#
#   * its BYTES, against the record — a store built from an input that has since changed is not built
#     from what is in the tree, and that is a refusal.
#   * its PRODUCER, against the RECORDED mtime — question 2 above, asked one level down. Recorded rather
#     than read off the file, so it is still answerable in a publish dir holding only the store and the
#     manifest. A warning, for the same reason question 2 is.
STORE_INPUTS = {b.arg: REGISTRY[b.name]
                for b in (pipeline.bind(e) for e in pipeline.stage("store").INPUTS)}

_build_store = None


def build_store():
    """`build_store.py` as a module, for its input digest.

    Imported rather than reimplemented: "what this input hashes to" having two definitions is precisely
    the drift this file exists to refuse.
    """
    global _build_store
    if _build_store is None:
        spec = importlib.util.spec_from_file_location(
            "build_store", os.path.join(REPO, "pipeline", "build_store.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _build_store = module
    return _build_store


def find_input(path, out_dir):
    """The recorded input in this tree, or None.

    As recorded first — the paths are how the build was invoked, from the repo root, which is also where
    the publisher runs. Then by basename in the publish dir, so a dir that was renamed or copied still
    gets its inputs checked rather than silently skipped.
    """
    for candidate in (path, os.path.join(out_dir, os.path.basename(path))):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def check_store_inputs(meta, out_dir):
    """The store's record of what it read, against the producers and against the tree.

    Returns `(problems, warnings, changed)` — `changed` kept apart from `problems` because it is a
    different decision with a different override: "I meant to publish an artifact nothing builds" is not
    "I meant to publish a store built from an input that has since moved".
    """
    problems, warnings, changed = [], [], []
    if not meta.get("storeFile"):
        return problems, warnings, changed

    record = meta.get("storeInputs")
    if not record:
        warnings.append(
            "the manifest records no storeInputs, so NOTHING checked what this store was built from. Its "
            "inputs are no longer published, so no other guard here can see them either. Rebuild it with "
            "pipeline/build_store.py --stamp-meta, which records them.")
        return problems, warnings, changed

    for entry in record:
        arg = entry.get("arg")
        path = entry.get("path") or ""
        name = f"store input {arg} ({path})"
        registered = STORE_INPUTS.get(arg)
        if registered is None:
            problems.append(
                f"{name}: no producer registered. The store reads it and nothing here says how to build "
                f"it, so nothing will rebuild it when its own inputs change — declare it in "
                f"pipeline/artifacts.py and name it in pipeline/store.py's INPUTS.")
            continue

        producer, how, dedicated = registered
        if not os.path.exists(producer):
            problems.append(f"{name}: its producer {producer} does not exist (build it with: {how})")
            continue
        if not tracked(producer):
            problems.append(f"{name}: its producer {producer} is not tracked by git — commit it, or the "
                            f"next machine cannot rebuild it")
            continue

        recorded_mtime = entry.get("mtime")
        if dedicated and recorded_mtime is not None and os.path.getmtime(producer) > recorded_mtime:
            warnings.append(
                f"{name}: {producer} was edited after this input was made, so the store may be built from "
                f"an input an older version of the rule produced. Re-run: {how} — then rebuild the store.")

        found = find_input(path, out_dir)
        if found is None:
            warnings.append(
                f"{name}: not in this tree, so its bytes were not checked against the record. The store "
                f"is the only thing a publish dir has to hold, so this is expected there — and it means "
                f"the only evidence this store matches its inputs is the record itself.")
            continue
        recorded_sha = entry.get("sha256")
        if not recorded_sha:
            warnings.append(f"{name}: the record carries no sha256, so nothing could be compared")
            continue
        actual, size, _ = build_store().input_digest(found)
        if actual != recorded_sha:
            changed.append(
                f"{name}: the store was built from {recorded_sha[:12]}…, {found} now holds {actual[:12]}… "
                f"({size} bytes). Rebuild the store from it, or publish the store that matches.")

    return problems, warnings, changed


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
                f"pipeline/check_producers.py."
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

    # The artifacts no manifest key names. Same question — "does anything in this repo build it" — asked
    # of the out-dir directly, because the manifest cannot answer it for a separately-tagged release.
    for pattern, producer, how in UNMANIFESTED:
        found = sorted(glob.glob(os.path.join(out_dir, pattern)))
        if not found:
            # Nothing matched. The registry says this out-dir SHOULD hold one, and an empty glob is the
            # one way this loop could pass while asserting nothing — which is what it did before this
            # branch existed. It matters because the corpus is the source of truth, and a tidied or
            # freshly-built out-dir is exactly when it goes missing.
            warnings.append(f"{pattern}: nothing in {out_dir} matches, so its producer ({producer}) was "
                            f"not checked. A publish dir should hold one; build it with: {how}")
            continue
        for artifact in found:
            name = os.path.basename(artifact)
            if not os.path.exists(producer):
                problems.append(f"{name}: its producer {producer} does not exist (build it with: {how})")
            elif not tracked(producer):
                problems.append(f"{name}: its producer {producer} is not tracked by git — commit it, or "
                                f"the next machine cannot rebuild it")
            elif os.path.getmtime(producer) > os.path.getmtime(artifact):
                warnings.append(f"{name}: {producer} was edited after this artifact was built, so it may "
                                f"have been built by an older version of the rule. Re-run: {how}")

    # And what the STORE was built from — the artifacts the manifest stopped naming.
    input_problems, input_warnings, changed = check_store_inputs(meta, out_dir)
    problems += input_problems
    warnings += input_warnings

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

    if changed:
        print("error: the store was not built from the inputs in this tree:", file=sys.stderr)
        for line in changed:
            print(f"       - {line}", file=sys.stderr)
        print("       The store is the only artifact this release carries, so its record of what it",
              file=sys.stderr)
        print("       read is the only thing standing between a re-run input and a store that silently",
              file=sys.stderr)
        print("       predates it. Publishing this one ships a generation of the input nothing else holds.",
              file=sys.stderr)
        print("       If the difference is deliberate, set DEN_ALLOW_STALE_STORE_INPUTS=1.", file=sys.stderr)

    if problems and os.environ.get("DEN_ALLOW_UNOWNED_ARTIFACTS") != "1":
        sys.exit(1)
    if changed and os.environ.get("DEN_ALLOW_STALE_STORE_INPUTS") != "1":
        sys.exit(1)


if __name__ == "__main__":
    main()
