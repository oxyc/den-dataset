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
                                      "spend": False, "fan_picks_max_spend_usd": 1.0}, **kwargs))


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
        self.migration_refuses = False
        patch = mock.patch.object(daily, "load", self.stage)
        patch.start()
        self.addCleanup(patch.stop)
        changed = mock.patch.object(daily.classify, "changed_articles", return_value=("articles", "digest"))
        changed.start()
        self.addCleanup(changed.stop)
        fan = mock.patch.object(daily.model_steps, "fan_picks_step", lambda day: {
            "asked": 0, "answered": 0, "emptyAnswers": 0, "anchors": 1, "picks": 1, "costUSD": 0.0})
        fan.start()
        self.addCleanup(fan.stop)
        for name in ("write_delta_ids", "finalize_ctx"):
            stub = mock.patch.object(daily, name, (lambda *a: 0) if name == "write_delta_ids" else (lambda c: c))
            stub.start()
            self.addCleanup(stub.stop)

    def stage(self, name):
        def run(ctx):
            self.calls.append((name, ctx.spend, ctx.plan))
            if name == "changes":
                os.makedirs(os.path.join(ctx.out_dir, "changes"), exist_ok=True)
                with open(os.path.join(ctx.out_dir, "changes", "plan.json"), "w", encoding="utf-8") as fh:
                    json.dump(self.plan, fh)
                for listed, keys in (("new", self.plan["added"]), ("keys", self.plan["added"]), ("withdrawn", []),
                                     ("items", [])):
                    with open(os.path.join(ctx.out_dir, "changes", f"{listed}.txt"), "w", encoding="utf-8") as fh:
                        fh.writelines(f"{key}\n" for key in keys)
            if name == "publish" and self.check_refuses:
                raise StageError("publish: pipeline/publish-dataset.sh exited 1")
            if name == "plot_length" and self.migration_refuses:
                raise StageError("plot_length: pre-transform-baseline: live baseline live predates the transform")
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
        self.assertEqual(self.ran(), ["changes", "articles", "genres_moods", "docfacts", "plot_length", "finalize",
                                      "facts", "franchises", "corpus", "store", "publish"])
        self.assertEqual([s["stage"] for s in report["skipped"]],
                         ["worklist", "fetch", "classify", "critique", "genres_moods (ask)", "premise_tags", "embed",
                          "franchises (ask)"])
        self.assertTrue(report["ready"])

    def test_with_every_credential_and_spend_every_stage_runs_and_the_paid_ones_buy(self):
        env = {"TMDB_API_KEY": "t", "TYPESAFE_API_KEY": "j", "DEN_EMBED_URL": "http://embed.invalid"}
        self.day(env, spend=True)
        # No universe was written by the recorded worklist, so the fetch is its refresh alone.
        self.assertEqual(self.ran(), ["worklist", *STAGES[STAGES.index("changes"):]])
        spent = {name for name, spend, _plan in (c for c in self.calls if len(c) == 3) if spend}
        self.assertEqual(spent, {"classify", "critique", "genres_moods", "franchises"})
        self.assertEqual([c for c in self.calls if c[0] == "publish"], [("publish", False, True)],
                         "the publish stage only checks")

    def test_spend_without_the_key_buys_nothing(self):
        self.day({"DEN_EMBED_URL": "http://embed.invalid"}, spend=True)
        self.assertNotIn("classify", self.ran())
        self.assertEqual([c[1] for c in self.calls if c[0] == "genres_moods"], [False])

    def test_a_new_title_without_an_article_skips_every_paid_ask_and_still_derives(self):
        env = {"TYPESAFE_API_KEY": "j", "OPENAI_API_KEY": "o", "DEN_EMBED_URL": "http://embed.invalid"}
        with mock.patch.object(daily.classify, "changed_articles", return_value=None):
            code, report = self.day(env, spend=True)
        self.assertEqual(code, 0)
        self.assertNotIn("classify", self.ran())
        self.assertNotIn("critique", self.ran())
        self.assertEqual([c[1] for c in self.calls if c[0] in ("genres_moods", "franchises")], [False, False])
        self.assertEqual(
            [entry["stage"] for entry in report["skipped"]],
            ["fetch", "classify", "critique", "genres_moods (ask)", "premise_tags", "franchises (ask)"],
        )

    def test_each_step_switch_can_disable_its_provider_under_the_master(self):
        day = daily.Day(args(self.out, spend=True, spend_typesafe=False, spend_fan_picks=False,
                             spend_premise=False),
                        {"TYPESAFE_API_KEY": "t", "GEMINI_API_KEY": "g", "OPENAI_API_KEY": "o",
                         "DEN_EMBED_URL": "http://embed.invalid"}, NOW)
        self.assertFalse(day.can_buy)
        self.assertFalse(day.can_buy_fan_picks)
        self.assertFalse(day.can_buy_premise)

    def test_premise_needs_master_switch_the_configured_providers_key_embedder_and_its_own_switch(self):
        enabled = daily.Day(args(self.out, spend=True, spend_premise=True),
                            {"OPENAI_API_KEY": "o", "DEN_EMBED_URL": "http://embed.invalid"}, NOW)
        self.assertTrue(enabled.can_buy_premise)
        disabled = daily.Day(args(self.out, spend=False, spend_premise=True),
                             {"OPENAI_API_KEY": "o", "DEN_EMBED_URL": "http://embed.invalid"}, NOW)
        self.assertFalse(disabled.can_buy_premise)
        fallback_only = daily.Day(args(self.out, spend=True, spend_premise=True),
                                  {"ANTHROPIC_API_KEY": "a", "DEN_EMBED_URL": "http://embed.invalid"}, NOW)
        self.assertFalse(fallback_only.can_buy_premise, "the fallback's key alone does not buy the step")

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

    def test_the_weekly_run_uses_the_full_catalogue_diff(self):
        modes = []
        original = self.stage

        def stage(name):
            module = original(name)
            inner = module.run
            if name == "worklist":
                module.run = lambda ctx: modes.append(ctx.mode) or inner(ctx)
            return module

        with mock.patch.object(daily, "load", stage):
            self.day({"TMDB_API_KEY": "t"}, mode=None, revisit_weeks=8)
        self.assertEqual(modes, ["catalogue"])

    def test_the_weekly_run_does_not_refresh_existing_wikipedia_sources(self):
        self.day(mode=None, revisit_weeks=8)
        self.assertNotIn(("refresh",), self.calls)


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

    def test_the_pre_transform_boundary_is_machine_readable_in_both_reports(self):
        self.migration_refuses = True
        code, report = self.day({"DEN_EMBED_URL": "http://embed.invalid"})
        self.assertEqual((code, report["ready"]), (1, False))
        self.assertEqual(report["migrationBoundary"], {
            "kind": "pre-transform-baseline", "blockingStage": "plot_length",
            "baselineDatasetVersion": "live",
            "requiredMigration": ("one combined full rebuild that creates and bundles "
                                  "index/plot-length-transform-v1.json and vectors-bge-m3.raw.bin"),
            "afterMigration": "rerun the no-spend smoke before enabling DEN_DAILY_ENABLED",
        })
        with open(os.path.join(self.out, daily.SUMMARY), encoding="utf-8") as handle:
            self.assertIn("**Migration boundary**", handle.read())


class PremiseCorrections(unittest.TestCase):
    """Committed premise corrections reach the run only with their vectors re-embedded (#184)."""

    def day(self, environ):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        return daily.Day(args(directory), environ, NOW)

    def test_without_an_embedder_the_corrections_wait_and_nothing_is_written(self):
        day = self.day({})
        with mock.patch.object(daily.premise_daily, "committed_corrections", return_value=["movie:103"]), \
             mock.patch.object(daily.premise_daily, "apply_corrections") as apply, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(daily.correct_premise(day), {"corrected": 0, "pending": 1})
        apply.assert_not_called()
        self.assertEqual(day.skipped[0]["stage"], "premise_corrections")

    def test_with_an_embedder_the_corrections_apply_and_are_reported(self):
        day = self.day({"DEN_EMBED_URL": "http://embed.invalid"})
        result = {"corrected": 2, "reembedded": 2, "appended": 0, "keys": ["movie:103", "movie:114"]}
        with mock.patch.object(daily.premise_daily, "committed_corrections",
                               return_value=["movie:103", "movie:114"]), \
             mock.patch.object(daily.premise_daily, "apply_corrections", return_value=result) as apply, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(daily.correct_premise(day), result)
        apply.assert_called_once_with(day.ctx, "http://embed.invalid", ["movie:103", "movie:114"])
        self.assertEqual(day.ran, ["premise_corrections"])

    def test_nothing_to_correct_is_silent(self):
        day = self.day({"DEN_EMBED_URL": "http://embed.invalid"})
        with mock.patch.object(daily.premise_daily, "committed_corrections", return_value=[]):
            self.assertIsNone(daily.correct_premise(day))
        self.assertEqual((day.ran, day.skipped), ([], []))

    def test_the_summary_reports_generated_tags_and_corrections_together(self):
        """The premise result's shape changed with the provider layer (#200): the summary reads what it has."""
        day = self.day({})
        day.premise = {"titles": 3, "generated": 2, "byModel": {"gpt-5.6-luna": 2}, "untagged": ["movie:9"],
                       "refused": [], "short": ["movie:9"], "costUSD": 0.0007}
        day.premise_corrections = {"corrected": 1, "reembedded": 1}
        with contextlib.redirect_stderr(io.StringIO()):
            daily.report(day, False, "test", 0)
        with open(os.path.join(day.ctx.out_dir, daily.SUMMARY), encoding="utf-8") as fh:
            summary = fh.read()
        self.assertIn("premise tags: 3 title(s), 2 tagged (gpt-5.6-luna 2), 1 left for a later run", summary)
        self.assertIn("premise corrections: 1 row(s) corrected", summary)


class PaidLedger(Recorded):
    """#187 item 1: what a day bought survives a day that fails, and the next day starts with it."""

    def stage(self, name):
        module = super().stage(name)
        inner = module.run

        def run(ctx):
            if name == "classify":
                self.saw_restored = os.path.exists(os.path.join(ctx.out_dir, "combined-v1-r2-abc.jsonl"))
                if not self.saw_restored:
                    for suffix, body in (("", '{"mediaType": "movie", "tmdbId": 1}\n'),
                                         (".manifest.json", '{"runId": "r"}')):
                        with open(os.path.join(ctx.out_dir, "combined-v1-r2-abc.jsonl" + suffix), "w") as fh:
                            fh.write(body)
            if name == "store" and self.store_fails:
                raise StageError("store: killed")
            return inner(ctx)
        module.run = run
        return module

    def test_a_day_that_fails_after_buying_keeps_it_for_the_next_day(self):
        ledger = os.path.join(self.out, "paid", "paid-state.json.gz")
        env = {"TYPESAFE_API_KEY": "j", "DEN_EMBED_URL": "http://embed.invalid"}
        self.store_fails = True
        code, _ = self.day(env, spend=True, paid_state=ledger)
        self.assertEqual((code, self.saw_restored), (1, False))
        self.assertIn("combined-v1-r2-abc.jsonl", daily.paid.Ledger(ledger).data["shards"])
        self.out = os.path.join(self.out, "next-day")
        os.makedirs(self.out)
        self.store_fails = False
        self.day(env, spend=True, paid_state=ledger)
        self.assertTrue(self.saw_restored, "the shard was laid back before the classify stage ran")


class FanPicks(unittest.TestCase):
    def day(self, spend=True, key="g"):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        environ = {"GEMINI_API_KEY": key} if key else {}
        return daily.Day(args(directory, spend=spend), environ, NOW)

    def test_added_titles_are_the_only_online_fan_pick_asks(self):
        day = self.day()
        ctx = types.SimpleNamespace(path=lambda artifact: os.path.join(day.ctx.out_dir,
                                                                      artifact.filename.replace("{version}", "v")))
        result = {"asked": 2, "answered": 2, "emptyAnswers": 0,
                  "anchors": 10, "picks": 40, "costUSD": 0.01}
        with mock.patch.object(daily.model_steps, "finalize_ctx", return_value=ctx), \
             mock.patch.object(daily.changes, "planned", return_value={"added": ["movie:7", "tv:8"]}), \
             mock.patch.object(daily.fan_picks, "daily_update", return_value=result) as update:
            self.assertEqual(daily.model_steps.update_fan_picks(day), result)
        self.assertEqual(update.call_args.kwargs["keys"], ["movie:7", "tv:8"])
        self.assertEqual(update.call_args.kwargs["max_spend"], 1.0)
        self.assertEqual(update.call_args.kwargs["backfill"], daily.fan_picks.load_backfill(),
                         "answers bought outside the job are merged every day")
        self.assertEqual(day.ran, ["fan_picks"])

    def test_no_key_carries_the_input_without_asking(self):
        day = self.day(key="")
        ctx = types.SimpleNamespace(path=lambda artifact: os.path.join(day.ctx.out_dir,
                                                                      artifact.filename.replace("{version}", "v")))
        with open(ctx.path(artifacts.FAN_PICKS), "w", encoding="utf-8") as handle:
            handle.write('{"anchors":{"movie:1":[]}}')
        result = {"asked": 0, "answered": 0, "emptyAnswers": 0,
                  "anchors": 8, "picks": 30, "costUSD": 0.0}
        with mock.patch.object(daily.model_steps, "finalize_ctx", return_value=ctx), \
             mock.patch.object(daily.changes, "planned", return_value={"added": ["movie:7"]}), \
             mock.patch.object(daily.fan_picks, "daily_update", return_value=result) as update, \
             contextlib.redirect_stderr(io.StringIO()):
            daily.model_steps.update_fan_picks(day)
        self.assertEqual(update.call_args.kwargs["keys"], [])
        self.assertEqual(day.skipped[0]["stage"], "fan_picks")

    def test_no_existing_input_and_nothing_to_ask_is_a_compatible_no_op(self):
        day = self.day(key="")
        ctx = types.SimpleNamespace(path=lambda artifact: os.path.join(day.ctx.out_dir,
                                                                      artifact.filename.replace("{version}", "v")))
        with mock.patch.object(daily.model_steps, "finalize_ctx", return_value=ctx), \
             mock.patch.object(daily.changes, "planned", return_value={"added": []}), \
             mock.patch.object(daily.fan_picks, "daily_update") as update, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(daily.model_steps.update_fan_picks(day))
        update.assert_not_called()
        self.assertEqual(day.skipped[0]["stage"], "fan_picks")

    def test_a_live_store_with_fan_picks_refuses_a_bundle_that_lost_the_input(self):
        day = self.day(key="")
        ctx = types.SimpleNamespace(path=lambda artifact: os.path.join(day.ctx.out_dir,
                                                                      artifact.filename.replace("{version}", "v")))
        os.makedirs(os.path.dirname(day.ctx.path(artifacts.PUBLISHED_META)), exist_ok=True)
        with open(day.ctx.path(artifacts.PUBLISHED_META), "w", encoding="utf-8") as handle:
            json.dump({"storeInputs": [{"arg": "fan_picks"}]}, handle)
        with mock.patch.object(daily.model_steps, "finalize_ctx", return_value=ctx), \
             mock.patch.object(daily.changes, "planned", return_value={"added": []}):
            with self.assertRaisesRegex(StageError, "refusing to build a store that drops"):
                daily.model_steps.update_fan_picks(day)


class DeltaIds(unittest.TestCase):
    """The delta-pass rule (docs/OPERATE.md, "Wikidata"): the live facts' titles and the ids listed, less the new labels'."""

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
