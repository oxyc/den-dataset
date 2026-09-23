"""`check_producers.py` — every published artifact must have a producer committed in this repo.

The failure it exists for is an artifact that was generated once, elsewhere, and carried forward by every
publish since: `facets.bin` fell 999 titles behind the corpus that way, and `facts-slim` kept shipping a
field set frozen years earlier. Both had a file, a sha and a record count; neither had anything in this
repo that would rebuild them.

It had no test of its own, which is the same shape of problem one level up.
"""
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def load():
    spec = importlib.util.spec_from_file_location(
        "check_producers", os.path.join(HERE, "check_producers.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cp = load()


def run(meta, out_dir):
    """`main()` with a written manifest, returning (exit code, stderr)."""
    import contextlib
    import io
    import sys

    path = os.path.join(out_dir, "meta.json")
    with open(path, "w") as fh:
        json.dump(meta, fh)
    err = io.StringIO()
    old = sys.argv
    sys.argv = ["check_producers.py", path, out_dir]
    try:
        with contextlib.redirect_stderr(err):
            try:
                code = cp.main()
            except SystemExit as exit:
                code = exit.code
    finally:
        sys.argv = old
    return code or 0, err.getvalue()


def touch(dir, name, body="{}"):
    path = os.path.join(dir, name)
    with open(path, "w") as fh:
        fh.write(body)
    return path


class Registry(unittest.TestCase):
    def test_every_registered_producer_exists_and_is_tracked(self):
        """The registry is only worth anything if its entries are real. A typo'd path, or a producer
        someone forgot to commit, makes the guard pass while naming a file that cannot be run."""
        cwd = os.getcwd()
        os.chdir(REPO)
        try:
            for key, (producer, how, _) in cp.PRODUCERS.items():
                self.assertTrue(os.path.exists(producer), f"{key}: {producer} is registered and missing")
                self.assertTrue(cp.tracked(producer), f"{key}: {producer} is not tracked by git")
            for pattern, producer, _ in cp.UNMANIFESTED:
                self.assertTrue(os.path.exists(producer), f"{pattern}: {producer} is missing")
                self.assertTrue(cp.tracked(producer), f"{pattern}: {producer} is not tracked")
        finally:
            os.chdir(cwd)

    def test_every_store_input_producer_exists_and_is_tracked(self):
        cwd = os.getcwd()
        os.chdir(REPO)
        try:
            for arg, (producer, how, _) in cp.STORE_INPUTS.items():
                self.assertTrue(os.path.exists(producer), f"{arg}: {producer} is registered and missing")
                self.assertTrue(cp.tracked(producer), f"{arg}: {producer} is not tracked by git")
        finally:
            os.chdir(cwd)

    def test_the_registry_covers_every_input_the_store_records(self):
        """The record and the registry have to name the same set. An input `build_store.py` records with
        no entry here is an input nothing owns — which is the gap this closes, reopened one argument at a
        time — and an entry here for an argument that no longer exists is a registration protecting
        nothing."""
        self.assertEqual(set(cp.STORE_INPUTS), set(cp.build_store().INPUT_ARGS))

    def test_the_store_and_the_corpus_are_registered(self):
        """Both were unowned until recently: the store had no entry at all, and the corpus is published on
        its own tag so no manifest key names it."""
        self.assertIn("storeFile", cp.PRODUCERS)
        self.assertIn("corpus-*.jsonl.gz", [p for p, _, _ in cp.UNMANIFESTED])


class Unowned(unittest.TestCase):
    def test_an_artifact_with_no_producer_is_refused(self):
        with tempfile.TemporaryDirectory() as dir:
            touch(dir, "mystery.json")
            code, err = run({"mysteryFile": "mystery.json"}, dir)
            self.assertEqual(code, 1)
            self.assertIn("no producer registered", err)

    def test_a_key_naming_no_file_is_not_an_artifact(self):
        """A manifest may name a blob this out-dir does not hold — a gz twin from another run, say. The
        guard asks about what is THERE, so it must not invent a problem from a name alone."""
        with tempfile.TemporaryDirectory() as dir:
            code, err = run({"mysteryFile": "not-on-disk.json"}, dir)
            self.assertEqual(code, 0, err)

    def test_the_gz_twins_inherit_rather_than_needing_their_own_entry(self):
        with tempfile.TemporaryDirectory() as dir:
            touch(dir, "labels-t02.json")
            touch(dir, "labels-t02.json.gz")
            cwd = os.getcwd()
            os.chdir(REPO)
            try:
                code, err = run({"labelsFile": "labels-t02.json",
                                 "labelsGzFile": "labels-t02.json.gz"}, dir)
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 0, err)


class Corpus(unittest.TestCase):
    def test_a_corpus_in_the_out_dir_is_checked_although_no_key_names_it(self):
        """The gap this closes. `consolidate_corpus.py` builds the source of truth every other artifact
        comes from, it is released on its own `corpus-<ver>` tag, and the manifest-driven loop could never
        see it — so nothing in the repo was required to be able to rebuild it."""
        with tempfile.TemporaryDirectory() as dir:
            touch(dir, "corpus-5b1c3213b6a1.jsonl.gz")
            touch(dir, "corpus-5b1c3213b6a1-entities.json.gz")
            cwd = os.getcwd()
            os.chdir(REPO)
            try:
                code, err = run({}, dir)
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 0, err)

    def test_an_ABSENT_corpus_is_reported_rather_than_passing_silently(self):
        """The one way this loop could pass while asserting nothing: an empty glob. It matters because a
        tidied or freshly-built out-dir is exactly when the corpus goes missing, so the guard would go
        quiet at the moment the tree stops holding the source of truth."""
        with tempfile.TemporaryDirectory() as dir:
            touch(dir, "labels-t02.json")
            cwd = os.getcwd()
            os.chdir(REPO)
            try:
                code, err = run({}, dir)
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 0, "absent is a warning, not a refusal — a publish dir may predate it")
            self.assertIn("corpus-*.jsonl.gz", err)
            self.assertIn("not checked", err)

    def test_a_missing_corpus_producer_is_reported(self):
        """Proved by running from a directory where the producer path does not resolve, which is what a
        deleted or renamed script looks like to this check."""
        with tempfile.TemporaryDirectory() as dir:
            touch(dir, "corpus-abc.jsonl.gz")
            cwd = os.getcwd()
            os.chdir(dir)  # nothing named pipeline/... resolves from here
            try:
                code, err = run({}, dir)
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 1)
            self.assertIn("consolidate_corpus.py", err)


class StoreInputs(unittest.TestCase):
    """What the store was built FROM.

    `data-latest` carries one blob, so the manifest names one blob, so the loop above checks one blob.
    The labels, vectors, metadata, facts, corpus and enriched batches the store is built from are still
    produced — they are just no longer declared — and until the store recorded them, a store built from
    an input its producer had outgrown published with every guard green.
    """

    INPUT = "labels-premise.json"

    def entry(self, dir, **over):
        """A truthful `storeInputs` entry for a real file, unless a test makes it lie."""
        path = touch(dir, self.INPUT, '{"records":[]}')
        sha, size, mtime = cp.build_store().input_digest(path)
        return {**{"arg": "premise_labels", "path": path, "sha256": sha, "bytes": size, "mtime": mtime},
                **over}

    def meta(self, entries):
        return {"storeFile": "den-aaaaaaaaaaaa.store", "storeInputs": entries}

    def check(self, dir, meta):
        """`main()` from the repo root, which is where the publisher runs it."""
        cwd = os.getcwd()
        os.chdir(REPO)
        try:
            return run(meta, dir)
        finally:
            os.chdir(cwd)

    def test_an_input_that_changed_since_the_build_is_refused(self):
        """THE FAILURE. Re-run the labelling producer, do not rebuild the store, publish: the store is
        now a generation behind an input that is sitting right there, and nothing said so."""
        with tempfile.TemporaryDirectory() as dir:
            entry = self.entry(dir)
            touch(dir, self.INPUT, '{"records":[{"tmdbId":1,"mediaType":"movie"}]}')
            code, err = self.check(dir, self.meta([entry]))
            self.assertEqual(code, 1)
            self.assertIn("not built from the inputs in this tree", err)
            self.assertIn(entry["sha256"][:12], err, "a refusal has to name both sides")

    def test_the_refusal_has_a_deliberate_override_of_its_own(self):
        """Not DEN_ALLOW_UNOWNED_ARTIFACTS: that one means "I meant to publish an artifact nothing
        builds", which is a different decision from "I meant to publish a store built from an input that
        has since moved"."""
        with tempfile.TemporaryDirectory() as dir:
            entry = self.entry(dir)
            touch(dir, self.INPUT, '{"records":[{"tmdbId":1,"mediaType":"movie"}]}')
            os.environ["DEN_ALLOW_STALE_STORE_INPUTS"] = "1"
            try:
                code, err = self.check(dir, self.meta([entry]))
            finally:
                del os.environ["DEN_ALLOW_STALE_STORE_INPUTS"]
            self.assertEqual(code, 0, err)
            self.assertIn("not built from the inputs in this tree", err, "allowed, but still announced")

    def test_an_input_older_than_its_producer_is_flagged(self):
        """The same question the loop above asks of a published artifact, one level down: a producer
        edited after the input means the input was made by an older version of the rule, and the store
        carries it. Answered against the RECORDED mtime, so it works with the input absent."""
        with tempfile.TemporaryDirectory() as dir:
            code, err = self.check(dir, self.meta([self.entry(dir, mtime=0)]))
            self.assertEqual(code, 0, "stale is a warning here, as it is for a published artifact")
            self.assertIn("build_premise_labels.py was edited after this input was made", err)

    def test_an_input_the_tree_no_longer_holds_is_reported_rather_than_skipped(self):
        """A publish dir may hold only the store and the manifest — that is the point of the cutover —
        so an absent input is expected, not a fault. It is also the one way this loop could pass while
        checking nothing, so it says so."""
        with tempfile.TemporaryDirectory() as dir:
            entry = self.entry(dir)
            os.remove(entry["path"])
            code, err = self.check(dir, self.meta([entry]))
            self.assertEqual(code, 0)
            self.assertIn("not in this tree", err)

    def test_a_store_with_no_record_at_all_says_so(self):
        """A store built before this existed. A refusal would block every publish until someone rebuilt
        135 MB, which is how a guard gets switched off; a silence would leave the gap exactly as it was."""
        with tempfile.TemporaryDirectory() as dir:
            code, err = self.check(dir, {"storeFile": "den-aaaaaaaaaaaa.store"})
            self.assertEqual(code, 0)
            self.assertIn("records no storeInputs", err)

    def test_an_input_no_producer_is_registered_for_is_refused(self):
        with tempfile.TemporaryDirectory() as dir:
            code, err = self.check(dir, self.meta([self.entry(dir, arg="mystery")]))
            self.assertEqual(code, 1)
            self.assertIn("no producer registered", err)

    def test_an_unchanged_input_is_not_a_finding(self):
        """So the refusals above mean something. Nothing has moved: the record describes the file, and
        the producer is older than it."""
        with tempfile.TemporaryDirectory() as dir:
            entry = self.entry(dir)
            code, err = self.check(dir, self.meta([entry]))
            self.assertEqual(code, 0, err)
            self.assertNotIn("not built from the inputs", err)
            self.assertNotIn("was edited after this input", err)


if __name__ == "__main__":
    unittest.main()
