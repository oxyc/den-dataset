"""`recluster.py` — the weekly re-cluster, held to the report the Swift binary wrote.

The golden below is the merge-base `taxonomy-backfill recluster` (4169e60) over `fixture()`, pasted in. Over
the real corpus at `scripts/recluster-run.sh`'s settings the two reports were byte-identical too
(oxyc/den-dataset#27).
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from . import recluster
from .contract import REPO

sys.path.insert(0, os.path.join(REPO, "scripts", "v2"))
import vector_blob  # noqa: E402

LABELS = ("Heist", "Neo-Noir", "Slasher", "Time Travel")

#: `fixture()` through the Swift binary with `ARGS`.
ARGS = ["--k", "6", "--iterations", "4", "--min-size", "3", "--max-purity", "0.9", "--min-cohesion", "0.1"]
GOLDEN_SHA = "8cc76013d96bcb158dfcda6019704776d8989a7e6163d1f895d18186068c452a"
#: The same, stopped after ONE iteration. The fixture settles at the second, so every count from 2 up is
#: `GOLDEN_SHA` — the Swift binary agrees at each — and only a run at 1 or 2 can tell them apart.
ONE_ITERATION_SHA = "413331bc761cef25699d671d2ecb89458dde4f7cb9e48768e8358ac94b739773"


def vector(i, dim=16):
    """Six loose groups with noise, so k-means has real work and near ties to settle."""
    group = i % 6
    return [max(-127, min(127, ((group * 37 + d * 11) % 200) - 100 + ((i * 7919 + d * 104729) % 23) - 11))
            for d in range(dim)]


def fixture(out, count=90):
    records = [{"mediaType": "tv" if i % 5 == 0 else "movie", "tmdbId": 1000 + i, "primaryGenre": "Drama",
                "source": "llm", "animated": False, "moods": [],
                "subgenres": [{"label": LABELS[(i // 6 + (i % 6)) % 4], "confidence": 0.9}] if i % 4 else []}
               for i in range(count)]
    labels = os.path.join(out, "labels-t02.json")
    with open(labels, "w", encoding="utf-8") as fh:
        json.dump({"taxonomyVersion": "t02", "count": count, "records": records}, fh)
    blob = os.path.join(out, "vectors-bge-m3.bin")
    rows = bytes(x & 0xFF for i in range(count) for x in vector(i))
    vector_blob.write(blob, [f"{r['mediaType']}:{r['tmdbId']}" for r in records], rows, 16)
    return labels, blob


class Report(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.labels, self.blob = fixture(self.directory.name)
        self.out = os.path.join(self.directory.name, "report.json")

    def run_it(self, *extra):
        return recluster.main(["--labels", self.labels, "--vectors", self.blob, "--out", self.out, *ARGS, *extra])

    def test_the_report_is_the_swift_binarys_bytes(self):
        self.run_it()
        with open(self.out, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), GOLDEN_SHA)

    def test_every_iteration_asked_for_is_run(self):
        """At 4 the fixture has long converged, so a run one iteration short reads the same. At 2 it has
        not: the second iteration still moves members."""
        for iterations, digest in (("1", ONE_ITERATION_SHA), ("2", GOLDEN_SHA)):
            with self.subTest(iterations=iterations):
                self.run_it("--iterations", iterations)
                with open(self.out, "rb") as fh:
                    self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest)

    def test_it_is_deterministic(self):
        """Stride-seeded, so this week's cluster ids are comparable to last week's."""
        self.run_it()
        with open(self.out, "rb") as fh:
            first = fh.read()
        self.run_it()
        with open(self.out, "rb") as fh:
            self.assertEqual(fh.read(), first)

    def test_tightest_first(self):
        self.run_it()
        with open(self.out, encoding="utf-8") as fh:
            cohesion = [row["cohesion"] for row in json.load(fh)]
        self.assertEqual(cohesion, sorted(cohesion, reverse=True))

    def test_a_blob_that_names_other_titles_is_refused(self):
        """Purity is per label: vectors of the wrong titles would report clean-looking purity."""
        with open(self.labels, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["records"][3]["tmdbId"] = 1
        with open(self.labels, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        with self.assertRaises(SystemExit) as refused:
            self.run_it()
        self.assertIn("row 3", str(refused.exception.code))


class Candidates(unittest.TestCase):
    def test_purity_at_the_limit_is_a_candidate(self):
        """`--max-purity` is the most dominant a label may be and still count as unexplained; the Swift
        dropped a cluster only above it. Two of four members share a label: purity 0.5 at a limit of 0.5."""
        records = [{"mediaType": "movie", "tmdbId": i, "subgenres": [{"label": label}]}
                   for i, label in enumerate(("Heist", "Heist", "Slasher", "Neo-Noir"))]
        vectors = [[1.0, 0.0]] * 4
        found = recluster.candidates(records, vectors, [0] * 4, [[1.0, 0.0]], 1, 0.5, 0.0)
        self.assertEqual([(row["purity"], row["dominantLabel"]) for row in found], [(0.5, "Heist")])


class Interpreter(unittest.TestCase):
    """3.12's `math.sumprod` is what keeps the weekly run at ~15 minutes; an older interpreter is refused by
    name, and the runner looks for one that is new enough rather than trusting `python3`."""

    def test_an_older_interpreter_is_refused_by_name(self):
        pretend = ("import runpy, sys; sys.version_info = (3, 11, 9); "
                   "runpy.run_path(sys.argv[1], run_name='__main__')")
        done = subprocess.run([sys.executable, "-c", pretend, os.path.join(REPO, "pipeline", "recluster.py")],
                              capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("needs Python 3.12 or newer", done.stderr)

    def run_runner(self, directory, env):
        return subprocess.run(["bash", os.path.join(REPO, "scripts", "recluster-run.sh"), directory],
                              capture_output=True, text=True, env=env)

    def test_the_runner_refuses_when_no_interpreter_on_path_is_new_enough(self):
        with tempfile.TemporaryDirectory() as shims:
            for name in ("python3", "python3.12", "python3.13", "python3.14"):
                path = os.path.join(shims, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("#!/bin/sh\nexit 1\n")   # every version check fails, as 3.9's does
                os.chmod(path, 0o755)
            labels, _ = fixture(shims)
            env = dict(os.environ, PATH=f"{shims}:/usr/bin:/bin")
            env.pop("PYTHON", None)
            done = self.run_runner(shims, env)
        self.assertEqual(done.returncode, 1)
        self.assertIn("needs Python 3.12 or newer", done.stderr)

    def test_the_runner_uses_the_interpreter_it_is_given(self):
        with tempfile.TemporaryDirectory() as out:
            fixture(out)
            done = self.run_runner(out, dict(os.environ, PYTHON=sys.executable, K="6", MIN_SIZE="3"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("candidate(s)", done.stdout)

    def test_the_runner_summarises_a_candidate_with_no_dominant_label(self):
        """No member carries a subgenre, so the report has no `dominantLabel` for it — which the summary
        used to index, dying after the report was written."""
        with tempfile.TemporaryDirectory() as out:
            labels, _ = fixture(out)
            with open(labels, encoding="utf-8") as fh:
                doc = json.load(fh)
            for record in doc["records"]:
                record["subgenres"] = []
            with open(labels, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            done = self.run_runner(out, dict(os.environ, PYTHON=sys.executable, K="6", MIN_SIZE="3"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("nearest existing label: None", done.stdout)


class Arithmetic(unittest.TestCase):
    def test_the_exact_dot_is_left_to_right(self):
        """`sum()` compensates since 3.12; the Swift accumulated plainly, and the bits are the report."""
        a = [1e16, 1.0, -1e16]
        self.assertEqual(recluster.exact_dot(a, [1.0, 1.0, 1.0]), 0.0)

    def test_where_the_fast_score_ties_the_exact_one_decides(self):
        """The two centroids differ in one last bit. `sumprod` scores them equal; the Swift's left-to-right
        sum does not, and the Swift took the second."""
        row = __import__("array").array("d", [-0.05301413507608732, 0.4503865408947558, 0.1129512498044265,
                                              -0.34803569790227185, 0.03669742540607368, 0.11088374976049375])
        first = [0.568544950730951, -0.7877811657901435, 0.1205922671679045, -0.50301135791382,
                 -0.44616585907043693, 0.5445221975109766]
        second = first[:4] + [-0.4461658590704369, 0.5445221975109766]
        self.assertEqual(recluster.nearest(row, [first, second]), 1)

    def test_the_winner_is_decided_exactly_and_a_tie_goes_to_the_lowest_index(self):
        row = __import__("array").array("d", [0.6, 0.8])
        self.assertEqual(recluster.nearest(row, [[0.0, 1.0], [0.6, 0.8], [0.6, 0.8]]), 1)


if __name__ == "__main__":
    unittest.main()
