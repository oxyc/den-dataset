#!/usr/bin/env python3
"""`den` — that the entry point dispatches, and that it stays the only thing you have to know.

The acceptance this covers is small and specific: the pipeline can be LISTED and INVOKED. Before it,
`docs/OPERATE.md` was the order and the commands, which is a pipeline you remember rather than one you
run.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DEN = os.path.join(HERE, "den")
V2 = os.path.join(HERE, "scripts", "v2")

sys.path.insert(0, V2)
_spec = importlib.util.spec_from_file_location("test_build_store", os.path.join(V2, "test_build_store.py"))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)

from pipeline.store_test import FIXTURE_FILES  # noqa: E402  — one spelling of the fixture's filenames


def den(*args):
    return subprocess.run([sys.executable, DEN, *args], capture_output=True, text=True, cwd=HERE)


class Listing(unittest.TestCase):
    def test_stages_answers_what_runs_in_what_order_and_what_it_touches(self):
        result = den("stages")
        self.assertEqual(result.returncode, 0, result.stderr)
        # The order, and that it is the real one: the universe is built before anything is drawn from it,
        # the articles are classified before the vectors are embedded, the vectors before the facts passes
        # are merged over their ids, the merged facts before the corpus that joins them, the corpus before
        # the store built from it, and the publish that uploads the store is last.
        expected = ("worklist", "classify", "embed", "facts", "corpus", "store", "publish")
        for position, name in enumerate(expected, start=1):
            self.assertIn(f"{position}. {name}", result.stdout)
        order = [result.stdout.index(f"{n}. {s}") for n, s in enumerate(expected, start=1)]
        self.assertEqual(order, sorted(order), "den stages printed them out of order")
        for line in ("premise_labels", "scripts/v2/build_store.py", "scripts/v2/consolidate_corpus.py",
                     "scripts/v2/run_combined.py", "scripts/publish-dataset.sh"):
            self.assertIn(line, result.stdout)
        # The optional input is marked as such: "the writer needs this" and "the writer can do without
        # it" are different answers to the same question. So is a flag that takes a set of shards.
        self.assertIn("(optional)", result.stdout)
        self.assertIn("(every shard)", result.stdout)


class Dispatch(unittest.TestCase):
    def test_a_stage_that_is_not_in_the_order_is_refused_with_the_order(self):
        # A name no stage has and none is likely to take. It used to be "worklist", which stopped testing
        # anything the day that stage landed — a refusal test has to name something that stays unknown.
        result = den("stage", "reticulate", "--dataset-version", "test")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no stage named 'reticulate'", result.stderr)
        self.assertIn("store", result.stderr)

    def test_a_missing_input_is_refused_with_the_command_that_builds_it(self):
        with tempfile.TemporaryDirectory() as out:
            result = den("stage", "store", "--out-dir", out, "--dataset-version", "test")
            self.assertEqual(result.returncode, 1)
            self.assertIn("consolidate_corpus.py", result.stderr)

    def test_run_stops_before_publishing_unless_asked(self):
        """`den run` is the exploratory command; publishing is the one step that leaves this machine.

        Every other stage writes into the out-dir and can be run again, so a wrong `den run` costs time.
        Publish uploads to the MOVING `data-latest` release, so the same mistake replaces the dataset
        people are being served. The asymmetry is the whole argument for the flag: forgetting it costs
        one more command, and not having it costs a restore.
        """
        with tempfile.TemporaryDirectory() as out:
            plain = den("run", "--out-dir", out, "--dataset-version", "test")
            # It fails on the first stage's missing inputs either way — what matters is which stage the
            # refusal names. Reaching publish at all would mean the release was in the run.
            self.assertNotIn("publish-dataset.sh", plain.stderr + plain.stdout)
            self.assertNotIn("data-latest", plain.stderr + plain.stdout)
            # It ran — it stops at the first stage on that stage's own missing input. Asserted as "some
            # stage started" rather than naming one, because which stage is first changes as the port
            # proceeds and this test is about publishing, not about the order.
            import pipeline
            banners = plain.stdout + plain.stderr
            self.assertTrue(any(f"==> {m.NAME}" in banners for m in pipeline.stages()),
                            f"no stage ran at all: {banners!r}")

    def test_the_order_still_contains_publish_even_though_run_skips_it(self):
        """The list stays truthful: `den stages` is what the pipeline IS, not what `den run` chose."""
        listing = den("stages")
        self.assertIn("publish", listing.stdout)
        self.assertEqual(listing.returncode, 0)

    def test_a_stage_must_say_whether_it_publishes(self):
        """Declared rather than defaulted, so a future publishing stage cannot be swept into `den run`
        by omission — the failure would be silent and outward-facing."""
        import pipeline
        for module in pipeline.stages():
            self.assertIsInstance(module.PUBLISHES, bool, f"{module.NAME} does not declare PUBLISHES")
        self.assertTrue(pipeline.stage("publish").PUBLISHES)
        self.assertFalse(pipeline.stage("store").PUBLISHES)

    def test_a_stage_must_say_whether_it_spends(self):
        """The same reason as PUBLISHES, for the other effect that leaves the out-dir. A stage that buys
        from a paid provider and forgot to say so would be bought by every exploratory `den run`."""
        import pipeline
        for module in pipeline.stages():
            self.assertIsInstance(module.SPENDS, bool, f"{module.NAME} does not declare SPENDS")
        self.assertTrue(pipeline.stage("classify").SPENDS)
        self.assertFalse(pipeline.stage("store").SPENDS)

    def test_den_run_leaves_out_the_stage_that_buys(self):
        """`den run` must not reach the paid pass unless asked for it by name.

        The first stage has to SUCCEED for this to say anything. An out-dir holding nothing refuses at
        stage one, and then classify is unreached whether or not the gate works — the assertion passes
        while testing nothing, which is the shape the publish gate's own test has to live with because
        publish is last. So the worklist is stubbed past, and the two runs are compared at the stage
        after it.
        """
        with tempfile.TemporaryDirectory() as out:
            stub = os.path.join(out, "stub")
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env python3\n"
                         "import json, sys\n"
                         "argv = sys.argv[1:]\n"
                         "json.dump([{'tmdbId': 1, 'mediaType': 'movie'}],\n"
                         "          open(argv[argv.index('--out') + 1], 'w'))\n")
            os.chmod(stub, 0o755)
            previous = os.environ.get("DEN_BACKFILL_BIN")
            os.environ["DEN_BACKFILL_BIN"] = stub
            self.addCleanup(os.environ.__setitem__, "DEN_BACKFILL_BIN", previous or "")
            # `discover` because it is the one mode that reads no input file — this test is about which
            # stages run, not about feeding the first one a TMDB dump.
            run = ("run", "--dataset-version", "test", "--out-dir", out, "--mode", "discover")

            gated = den(*run)
            self.assertIn("==> worklist", gated.stderr, "the stub did not get the run past stage one")
            self.assertNotIn("==> classify", gated.stderr)

            asked = den(*run, "--spend")
            self.assertIn("==> classify", asked.stderr)

    def test_set_wants_a_pair(self):
        result = den("stage", "store", "--dataset-version", "test", "--set", "corpus")
        self.assertEqual(result.returncode, 1)
        self.assertIn("NAME=PATH", result.stderr)


class EndToEnd(fixture.StoreFixture, unittest.TestCase):
    def test_den_stage_store_builds_a_store(self):
        """One command, from outside the process, over real inputs."""
        with tempfile.TemporaryDirectory() as out:
            self.build(out)
            overrides = []
            for name, filename in FIXTURE_FILES.items():
                overrides += ["--set", f"{name}={os.path.join(out, filename)}"]
            result = den("stage", "store", "--out-dir", out, "--dataset-version", "den", *overrides)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.isfile(os.path.join(out, "den-den.store")),
                            f"no store in {os.listdir(out)}")
            self.assertIn("==> store", result.stderr)


if __name__ == "__main__":
    unittest.main()
