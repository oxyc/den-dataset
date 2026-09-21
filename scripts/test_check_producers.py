"""`check-producers.py` — every published artifact must have a producer committed in this repo.

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
        "check_producers", os.path.join(HERE, "check-producers.py"))
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
    sys.argv = ["check-producers.py", path, out_dir]
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

    def test_a_missing_corpus_producer_is_reported(self):
        """Proved by running from a directory where the producer path does not resolve, which is what a
        deleted or renamed script looks like to this check."""
        with tempfile.TemporaryDirectory() as dir:
            touch(dir, "corpus-abc.jsonl.gz")
            cwd = os.getcwd()
            os.chdir(dir)  # nothing named scripts/v2/... resolves from here
            try:
                code, err = run({}, dir)
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 1)
            self.assertIn("consolidate_corpus.py", err)


if __name__ == "__main__":
    unittest.main()
