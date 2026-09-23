#!/usr/bin/env python3
"""`den` — that the entry point dispatches, and that it stays the only thing you have to know.

The acceptance this covers is small and specific: the pipeline can be LISTED and INVOKED. Before it,
`docs/OPERATE.md` was the order and the commands, which is a pipeline you remember rather than one you
run.
"""
import importlib.util
import json
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
        # the titles are enriched before anything reads their plots, the articles dumped before the pass
        # that reads them, classified before the vectors are embedded, the vectors finalized into the
        # labels file the facts passes scrape the ids of, the merged facts before the corpus that joins
        # them, the corpus before the store built from it, and the publish that uploads the store is last.
        expected = ("worklist", "fetch", "articles", "classify", "genres_moods", "docfacts", "embed",
                    "finalize", "facts", "corpus", "store", "publish")
        for position, name in enumerate(expected, start=1):
            self.assertIn(f"{position}. {name}", result.stdout)
        order = [result.stdout.index(f"{n}. {s}") for n, s in enumerate(expected, start=1)]
        self.assertEqual(order, sorted(order), "den stages printed them out of order")
        for line in ("premise_labels", "universe-movie.json", "scripts/v2/build_store.py",
                     "pipeline/consolidate_corpus.py", "pipeline/run_combined.py",
                     "scripts/publish-dataset.sh"):
            self.assertIn(line, result.stdout)
        # The optional input is marked as such: "the writer needs this" and "the writer can do without
        # it" are different answers to the same question. So is a flag that takes a set of shards.
        self.assertIn("(optional)", result.stdout)
        self.assertIn("(every shard)", result.stdout)


class Dispatch(unittest.TestCase):
    def test_an_interpreter_older_than_the_floor_is_refused_by_name(self):
        """macOS's `python3` is 3.9, where the stages die importing a `int | None` annotation."""
        pretend = ("import runpy, sys; sys.version_info = (3, 10, 14); sys.argv = sys.argv[1:]; "
                   "runpy.run_path(sys.argv[0], run_name='__main__')")
        result = subprocess.run([sys.executable, "-c", pretend, DEN, "stages"], capture_output=True, text=True,
                                cwd=HERE)
        self.assertEqual(result.returncode, 1)
        self.assertIn("needs Python 3.11 or newer", result.stderr)

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

    def test_the_version_is_asked_only_of_what_names_a_file_by_it(self):
        """finalize names none of its files by it, so it is not asked for one; the store does, and with no
        manifest to read one from and none given it is refused naming the flag, not written as
        `den-.store`."""
        with tempfile.TemporaryDirectory() as out:
            unversioned = den("stage", "finalize", "--out-dir", out)
            self.assertNotIn("--dataset-version", unversioned.stderr)
            self.assertIn("embed_labels", unversioned.stderr, "it got as far as its own inputs")
            versioned = den("stage", "store", "--out-dir", out)
            self.assertEqual(versioned.returncode, 1)
            self.assertIn("pass --dataset-version", versioned.stderr)

    def test_the_version_is_the_manifests_and_a_given_one_is_only_a_check(self):
        """`finalize` derives the version from what it writes, so nobody can pass the right value before a
        run: `den run` does not ask for one, and a stage after `finalize` reads it from the manifest."""
        with tempfile.TemporaryDirectory() as out:
            self.assertNotEqual(den("run", "--out-dir", out).returncode, 2, "argparse demanded the version")
            with open(os.path.join(out, "dataset.meta.json"), "w", encoding="utf-8") as fh:
                json.dump({"datasetVersion": "abc123def456"}, fh)
            derived = den("stage", "store", "--out-dir", out)
            self.assertIn("corpus-abc123def456.jsonl.gz is missing", derived.stderr)
            agreeing = den("stage", "store", "--out-dir", out, "--dataset-version", "abc123def456")
            self.assertIn("corpus-abc123def456.jsonl.gz is missing", agreeing.stderr)
            wrong = den("stage", "corpus", "--out-dir", out, "--dataset-version", "fixture")
            self.assertEqual(wrong.returncode, 1)
            self.assertIn("--dataset-version fixture is not this out-dir's generation", wrong.stderr)

    def test_run_stops_before_publishing_unless_asked(self):
        """`den run` is the exploratory command; publishing is the one step that leaves this machine.

        Every other stage writes into the out-dir and can be run again, so a wrong `den run` costs time.
        Publish uploads to the MOVING `data-latest` release, so the same mistake replaces the dataset
        people are being served. The asymmetry is the whole argument for the flag: forgetting it costs
        one more command, and not having it costs a restore.
        """
        with tempfile.TemporaryDirectory() as out:
            plain = den("run", "--out-dir", out)
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

        EVERY unpaid stage before it has to SUCCEED for this to say anything. An out-dir holding nothing
        refuses at stage one, and then classify is unreached whether or not the gate works — the assertion
        passes while testing nothing, which is the shape the publish gate's own test has to live with
        because publish is last. This test has now been falsified twice that way, each time a stage landed
        ahead of classify, so it seeds every one of them and asserts it got past the last.

        Three stages run first and they need different things. `worklist` and `articles` are Python and
        read files, so they are given files: a one-row TMDB dump apiece, and an enriched batch naming a
        grounded title plus an `articles.jsonl` row for it, because a row already in the dump's output is
        a row it resumes past rather than fetches. `fetch` drains in-process, so it gets a checkpoint that
        already holds both universes, with credentials in the environment so the drain does not go hunting
        for a `den.env` this checkout has no reason to own. Nothing here reaches TMDB.
        """
        with tempfile.TemporaryDirectory() as out:
            for name, value in (("TMDB_API_KEY", "stub-key-nothing-here-calls-tmdb"),
                                ("WIKIMEDIA_ENTERPRISE_TOKEN", "stub-bearer")):
                previous = os.environ.get(name)
                os.environ[name] = value
                self.addCleanup(os.environ.__setitem__, name, previous or "")
            # `export` because it is the one mode that touches no network — this test is about which
            # stages run, not about what TMDB answers.
            for name in ("movie_ids.json", "tv_series_ids.json"):
                with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
                    fh.write('{"id":11,"popularity":1.0}\n')
            os.makedirs(os.path.join(out, "enriched"), exist_ok=True)
            with open(os.path.join(out, "enriched", "batch-1.json"), "w", encoding="utf-8") as fh:
                json.dump([{"tmdbId": 11, "mediaType": "movie", "title": "t", "year": 1977,
                            "hasWikiPlot": True, "plotArticle": "Star Wars (film)"}], fh)
            with open(os.path.join(out, "articles.jsonl"), "w", encoding="utf-8") as fh:
                fh.write('{"mediaType":"movie","tmdbId":11,"text":"prose"}\n')
            # Both universes already drained, so the in-process drain returns before it builds a TMDB
            # client. Without it the drain asked TMDB about id 11 with the stub key.
            with open(os.path.join(out, "enrich-checkpoint.json"), "w", encoding="utf-8") as fh:
                json.dump({"processed": ["movie:11", "tv:11"], "nextBatch": 2}, fh)
            run = ("run", "--out-dir", out, "--mode", "export")

            gated = den(*run)
            self.assertIn("==> worklist", gated.stderr, "the stub did not get the run past stage one")
            self.assertIn("==> fetch: ", gated.stderr, "the drain did not finish, so classify is "
                                                       "unreached for a reason that is not the gate")
            self.assertNotIn("==> classify", gated.stderr)
            # The genres & moods stage buys too, but only in one of its steps, so it still runs: what it
            # does without `--spend` is derive from the answers already bought.
            self.assertIn("==> genres_moods", gated.stderr)

            asked = den(*run, "--spend")
            self.assertIn("==> classify", asked.stderr)

    def test_set_wants_a_pair(self):
        result = den("stage", "store", "--dataset-version", "test", "--set", "corpus")
        self.assertEqual(result.returncode, 1)
        self.assertIn("NAME=PATH", result.stderr)

    def test_every_admission_floor_reaches_the_run(self):
        """A flag the parser accepts and `Context` never hears of is a floor an operator believes they set."""
        import argparse
        import importlib.machinery
        loader = importlib.machinery.SourceFileLoader("den_entry", DEN)
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader("den_entry", loader))
        loader.exec_module(module)
        args = argparse.Namespace(set=[], out_dir="out", dataset_version="v", stamp_meta=None, mode=None,
                                  since=None, expect=None, pause_ms=0, limit=None, media=None, vote_floor=40,
                                  regional_vote_floor=10, wikipedia_floor=7, regional_wikipedia_floor=4,
                                  plan=False, spend=False, dump_docs=None, reembed_keys=None,
                                  reembed_changed=False)
        ctx = module.context(args)
        self.assertEqual((ctx.vote_floor, ctx.regional_vote_floor, ctx.wikipedia_floor,
                          ctx.regional_wikipedia_floor), (40, 10, 7, 4))
        listed = den("stage", "fetch", "--help").stdout
        for flag in ("--vote-floor", "--regional-vote-floor", "--wikipedia-floor", "--regional-wikipedia-floor"):
            self.assertIn(flag, listed)

    def test_the_reembed_selection_reaches_the_run(self):
        """A re-embed list the parser accepts and `Context` drops is a run that leaves every listed title on
        its old vector and reports success."""
        import importlib.machinery
        loader = importlib.machinery.SourceFileLoader("den_entry", DEN)
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader("den_entry", loader))
        loader.exec_module(module)
        import argparse
        args = argparse.Namespace(set=[], out_dir="out", dataset_version="",stamp_meta=None, mode=None,
                                  since=None, expect=None, pause_ms=0, limit=None, media=None, vote_floor=None,
                                  regional_vote_floor=None, wikipedia_floor=None, regional_wikipedia_floor=None,
                                  plan=True, spend=False, dump_docs=None, reembed_keys="keys.txt",
                                  reembed_changed=True)
        ctx = module.context(args)
        self.assertEqual((ctx.reembed_keys, ctx.reembed_changed, ctx.plan), ("keys.txt", True, True))
        listed = den("stage", "embed", "--help").stdout
        for flag in ("--reembed-keys", "--reembed-changed"):
            self.assertIn(flag, listed)


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
