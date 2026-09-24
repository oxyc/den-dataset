#!/usr/bin/env python3
"""`den daily` — which stages a missing credential skips, what it refuses before anything runs, the delta
ids it writes and the report it leaves. The stages are replaced by recorders here; `den_daily_test.py` runs
the real ones over the fixture corpus."""
import argparse
import contextlib
import datetime
import io
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from . import STAGES, artifacts, daily
from .contract import Context, StageError

NOW = datetime.datetime(2026, 9, 24, 3, 23, tzinfo=datetime.timezone.utc)


def args(out, **kwargs):
    return argparse.Namespace(**dict({"out_dir": out, "mode": "export", "since": None, "revisit_weeks": None,
                                      "spend": False}, **kwargs))


class Recorded(unittest.TestCase):
    """Each stage a recorder: its name, and whether it was asked to spend or only to plan."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name
        self.calls = []
        self.plan = {"baseline": {"datasetVersion": "live", "maxBatchId": 1}, "counts": {"added": 1},
                     "added": ["movie:1"], "changed": {}, "withdrawn": {}, "revisit": None}
        self.check_refuses = False
        patch = mock.patch.object(daily, "load", self.stage)
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("write_delta_ids", "finalize_ctx"):
            stub = mock.patch.object(daily, name, (lambda *a: 0) if name == "write_delta_ids" else (lambda c: c))
            stub.start()
            self.addCleanup(stub.stop)
        refresh = mock.patch.object(daily.refresh, "run", lambda ctx: self.calls.append(("refresh",)) or {})
        refresh.start()
        self.addCleanup(refresh.stop)

    def stage(self, name):
        def run(ctx):
            self.calls.append((name, ctx.spend, ctx.plan))
            if name == "changes":
                os.makedirs(os.path.join(ctx.out_dir, "changes"), exist_ok=True)
                with open(os.path.join(ctx.out_dir, "changes", "plan.json"), "w", encoding="utf-8") as fh:
                    json.dump(self.plan, fh)
            if name == "publish" and self.check_refuses:
                raise StageError("publish: pipeline/publish-dataset.sh exited 1")
            return name
        return types.SimpleNamespace(NAME=name, INPUTS=(), OUTPUTS=(), run=run)

    def day(self, environ=None, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            code = daily.run(args(self.out, **kwargs), environ or {}, NOW)
        with open(os.path.join(self.out, daily.REPORT), encoding="utf-8") as fh:
            return code, json.load(fh)

    def ran(self):
        return [call[0] for call in self.calls]


class Skips(Recorded):
    def test_with_no_credential_every_stage_that_needs_none_runs_and_the_rest_are_named(self):
        code, report = self.day(mode="delta")
        self.assertEqual(code, 0)
        self.assertEqual(self.ran(), ["refresh", "changes", "articles", "genres_moods", "docfacts", "finalize",
                                      "facts", "corpus", "store", "publish"])
        self.assertEqual([s["stage"] for s in report["skipped"]],
                         ["worklist", "fetch", "classify", "critique", "genres_moods (ask)", "embed"])
        self.assertTrue(report["ready"])

    def test_with_every_credential_and_spend_every_stage_runs_and_the_paid_ones_buy(self):
        env = {"TMDB_API_KEY": "t", "TYPESAFE_API_KEY": "j", "DEN_EMBED_URL": "http://embed.invalid"}
        self.day(env, spend=True)
        # No universe was written by the recorded worklist, so the fetch is its refresh alone.
        self.assertEqual(self.ran(), ["worklist", "refresh", *STAGES[STAGES.index("changes"):]])
        spent = {name for name, spend, _plan in (c for c in self.calls if len(c) == 3) if spend}
        self.assertEqual(spent, {"classify", "critique", "genres_moods"})
        self.assertEqual([c for c in self.calls if c[0] == "publish"], [("publish", False, True)],
                         "the publish stage only checks")

    def test_spend_without_the_key_buys_nothing(self):
        self.day({"DEN_EMBED_URL": "http://embed.invalid"}, spend=True)
        self.assertNotIn("classify", self.ran())
        self.assertEqual([c[1] for c in self.calls if c[0] == "genres_moods"], [False])

    def test_the_weekly_slice_reaches_the_change_set(self):
        seen = []
        original = self.stage

        def stage(name):
            module = original(name)
            inner = module.run
            module.run = lambda ctx: seen.append(ctx.revisit_weeks) or inner(ctx) if name == "changes" else inner(ctx)
            return module
        with mock.patch.object(daily, "load", stage):
            self.day(revisit_weeks=8)
        self.assertEqual(seen, [8])


class Refusals(Recorded):
    def test_an_out_dir_with_batches_and_no_live_manifest_runs_nothing(self):
        os.makedirs(os.path.join(self.out, "enriched"))
        open(os.path.join(self.out, "enriched", "batch-1.json"), "w").close()
        code, report = self.day()
        self.assertEqual((code, self.calls), (1, []))
        self.assertIn("every title would be new", report["verdict"])

    def test_spend_with_no_live_baseline_stops_before_anything_is_bought(self):
        self.plan["baseline"] = None
        env = {"TYPESAFE_API_KEY": "j", "DEN_EMBED_URL": "http://embed.invalid"}
        code, report = self.day(env, spend=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.ran()[-1], "changes")
        self.assertIn("first generation", report["verdict"])

    def test_a_refused_check_is_not_ready_and_says_so(self):
        self.check_refuses = True
        code, report = self.day({"DEN_EMBED_URL": "http://embed.invalid"})
        self.assertEqual((code, report["ready"]), (1, False))
        with open(os.path.join(self.out, daily.SUMMARY), encoding="utf-8") as fh:
            self.assertIn("Not ready", fh.read())


class DeltaIds(unittest.TestCase):
    """docs/OPERATE.md step 6a: the live facts' titles and the ids listed, less the new labels'."""

    def write(self, out, name, keys):
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            json.dump({"records": [{"mediaType": k.split(":")[0], "tmdbId": int(k.split(":")[1])} for k in keys]},
                      fh)

    def test_the_rule(self):
        with tempfile.TemporaryDirectory() as out:
            self.write(out, artifacts.VECTOR_LABELS.filename, ["movie:1", "movie:2"])
            self.write(out, "facts-live.json", ["movie:1", "movie:3", "tv:4"])
            with open(os.path.join(out, artifacts.DELTA_IDS.filename), "w", encoding="utf-8") as fh:
                fh.write("movie:2\ntv:9\n")
            self.assertEqual(daily.write_delta_ids(Context(out_dir=out), "live"), 3)
            with open(os.path.join(out, artifacts.DELTA_IDS.filename), encoding="utf-8") as fh:
                self.assertEqual(fh.read().split(), ["movie:3", "tv:4", "tv:9"],
                                 "a hand-added id stays until it has a vector; one that has one goes")

    def test_a_live_dataset_whose_facts_are_not_here_is_refused(self):
        with tempfile.TemporaryDirectory() as out:
            self.write(out, artifacts.VECTOR_LABELS.filename, ["movie:1"])
            with self.assertRaisesRegex(StageError, "did not build the live dataset"):
                daily.write_delta_ids(Context(out_dir=out), "live")


if __name__ == "__main__":
    unittest.main()
