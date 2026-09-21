"""`build_store.py` against den-spec's committed fixture — the WRITER's half of the format contract.

store-v1 is implemented three times: this writer, `den-core/crates/den-store` and the mmap in den-atlas.
Both readers load `den-spec/vectors/store-v1.*` in their own tests and fail without it. The writer — the
side that decides what the bytes actually are — had no test at all, so a layout change here would have
been caught only by the readers failing afterwards, in other repos, on someone else's branch.

The check is a rebuild: run the real generator, and require the result to be byte-identical to the
committed fixture. Byte-identical is the right bar rather than a field-by-field comparison, because the
writer promises deterministic output (dictionaries sorted before ids are assigned, rows sorted by key) and
that promise is what makes a content hash meaningful. Anything that perturbs a single byte — a changed
section order, an unsorted dictionary, a float rounded differently — fails here.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_STORE = os.path.join(HERE, "build_store.py")


def spec_dir():
    """den-spec, as a sibling checkout or via DEN_SPEC_DIR."""
    return os.environ.get("DEN_SPEC_DIR", os.path.join(HERE, "..", "..", "..", "den-spec"))


def spec_or_fail(*parts):
    """A path inside den-spec, or a FAILURE.

    Skipping when the contract is absent is the false pass this kind of test is most prone to: the run
    reports OK and has verified nothing. DEN_SPEC_OPTIONAL=1 is the deliberate escape, matching
    den-core's `fixture_or_fail!` and den-atlas's `spec_fixture`.
    """
    path = os.path.join(spec_dir(), *parts)
    if os.path.isfile(path):
        return path
    # `== "1"`, not truthiness: DEN_SPEC_OPTIONAL=0, set to turn skipping OFF, would otherwise turn it on.
    if os.environ.get("DEN_SPEC_OPTIONAL") == "1":
        raise unittest.SkipTest("den-spec absent and DEN_SPEC_OPTIONAL=1")
    raise AssertionError(
        f"{path} not found — this test checks build_store.py against the store-v1 contract and cannot "
        "do so without it. Check out den-spec beside this repo, set DEN_SPEC_DIR, or set "
        "DEN_SPEC_OPTIONAL=1 to skip deliberately."
    )


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class FixtureRoundTrip(unittest.TestCase):
    def test_the_writer_reproduces_the_committed_fixture(self):
        committed = spec_or_fail("vectors", "store-v1.store")
        generator = spec_or_fail("tools", "store-fixture.py")
        with tempfile.TemporaryDirectory() as out:
            result = subprocess.run(
                [sys.executable, generator, "--build-store", BUILD_STORE, "--out-dir", out],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, f"the fixture generator failed:\n{result.stderr}")
            rebuilt = os.path.join(out, "store-v1.store")
            self.assertTrue(os.path.isfile(rebuilt), f"generator wrote nothing:\n{result.stdout}")
            self.assertEqual(
                sha256(rebuilt),
                sha256(committed),
                "build_store.py no longer produces the committed fixture. Either the layout changed — in "
                "which case this is a NEW format version (store-v2.md), not an edit to store-v1 — or the "
                "writer lost its determinism.",
            )

    def test_a_rebuild_is_byte_identical_to_itself(self):
        """Determinism across processes, which is what lets a content hash mean anything. Python's hash
        randomisation differs per process, so two runs disagreeing would mean a dictionary order leaked
        into the bytes."""
        generator = spec_or_fail("tools", "store-fixture.py")
        digests = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as out:
                result = subprocess.run(
                    [sys.executable, generator, "--build-store", BUILD_STORE, "--out-dir", out],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                digests.append(sha256(os.path.join(out, "store-v1.store")))
        self.assertEqual(digests[0], digests[1], "two runs of the writer disagree byte-for-byte")


class StampsTheManifest(unittest.TestCase):
    """`--stamp-meta`. Without it the store is written and nothing names it: `publish-dataset.sh`
    announces an unowned blob, `den-atlas` never sees a `storeFile`, and the rail falls back."""

    def test_the_store_declares_itself(self):
        import hashlib
        import json

        generator = spec_or_fail("tools", "store-fixture.py")
        with tempfile.TemporaryDirectory() as out:
            meta = os.path.join(out, "dataset.meta.json")
            with open(meta, "w") as fh:
                json.dump({"datasetVersion": "fixture", "labelsFile": "labels.json"}, fh)
            result = subprocess.run(
                [sys.executable, generator, "--build-store", BUILD_STORE, "--out-dir", out,
                 "--", "--stamp-meta", meta],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            with open(meta) as fh:
                stamped = json.load(fh)
            with open(os.path.join(out, stamped["storeFile"]), "rb") as fh:
                blob = fh.read()
            self.assertEqual(stamped["storeBytes"], len(blob))
            self.assertEqual(stamped["storeSha256"], hashlib.sha256(blob).hexdigest())
            self.assertEqual(
                stamped["labelsFile"], "labels.json", "stamping must not drop the keys already there"
            )
            self.assertNotIn(
                "storeGzFile", stamped, "the store is mmap'd; a compressed twin cannot be mapped"
            )


if __name__ == "__main__":
    unittest.main()
