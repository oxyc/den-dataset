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
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from . import STAGES, artifacts, daily, enrich
from .contract import Context, StageError

NOW = datetime.datetime(2026, 9, 24, 3, 23, tzinfo=datetime.timezone.utc)
PER_TITLE = daily.TYPESAFE_PROJECTED_PER_TITLE


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
        for name, value in (("changed_articles", ("articles", "digest")), ("reclassified", set())):
            changed = mock.patch.object(daily.classify, name, return_value=value)
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
                                      "facts", "maturity", "franchises", "corpus", "store", "publish"])
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

    def published(self, name, **report):
        directory = os.path.join(self.out, "published-reports")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
            json.dump({"date": "2026-09-21", "spend": {"totalUSD": 0.0}, **report}, fh)
        return directory

    def weeks_asked(self, **kwargs):
        seen = []
        original = self.stage

        def stage(name):
            module = original(name)
            inner = module.run
            module.run = lambda ctx: seen.append(ctx.revisit_weeks) or inner(ctx) if name == "changes" else inner(ctx)
            return module
        with mock.patch.object(daily, "load", stage):
            _code, report = self.day(revisit_weeks=8, **kwargs)
        return seen, report

    def test_a_slice_a_ready_run_carried_this_week_is_not_revisited_again(self):
        # NOW is Thursday 2026-09-24; Monday 2026-09-21 is the same week.
        reports = self.published("monday.json", ready=True, revisit={"weeks": 8, "slice": 3, "date": "2026-09-21"})
        seen, report = self.weeks_asked(published_reports_dir=reports)
        self.assertEqual(seen, [None])
        self.assertIn("revisit", [s["stage"] for s in report["skipped"]])

    def test_a_slice_no_ready_run_carried_is_revisited_on_a_later_day_of_the_week(self):
        reports = self.published("monday.json", ready=False, revisit={"weeks": 8, "slice": 3, "date": "2026-09-21"})
        self.published("last-week.json", ready=True, revisit={"weeks": 8, "slice": 2, "date": "2026-09-18"})
        seen, _report = self.weeks_asked(published_reports_dir=reports)
        self.assertEqual(seen, [8])

    def test_the_weekly_run_does_not_refresh_existing_wikipedia_sources(self):
        self.day(mode=None, revisit_weeks=8)
        self.assertNotIn(("refresh",), self.calls)

    def test_a_ready_run_that_held_titles_has_not_carried_the_week(self):
        """The catalogue pass is what finds a held title again, so the day after stays one."""
        reports = self.published("monday.json", ready=True, heldForSpend=3461,
                                 revisit={"weeks": 8, "slice": 3, "date": "2026-09-21"})
        seen, _report = self.weeks_asked(published_reports_dir=reports)
        self.assertEqual(seen, [8])


class Fits(Recorded):
    """A day whose TypeSafe backlog projects over its cap labels what the cap buys and holds the rest, instead
    of refusing the whole day (daily run 38174158772: 5.13 projected against a 1.00 cap, nothing bought, and
    the backlog could never drain)."""

    ENV = {"TYPESAFE_API_KEY": "j", "DEN_EMBED_URL": "http://embed.invalid"}

    def backlog(self, titles, plotless=0):
        keys = [f"movie:{n}" for n in range(1, titles + 1)]
        self.plan.update(added=keys, addedPlotless=keys[:plotless],
                         counts={"added": titles, "addedPlotless": plotless, "addedPlotted": titles - plotless,
                                 "keys": titles})
        return keys

    def listed(self, name):
        with open(os.path.join(self.out, "changes", f"{name}.txt"), encoding="utf-8") as fh:
            return fh.read().split()

    def spent(self, usd):
        directory = os.path.join(self.out, "published-reports")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "earlier.json"), "w", encoding="utf-8") as fh:
            json.dump({"date": "2026-09-21", "spend": {"totalUSD": usd}}, fh)
        return directory

    def test_a_backlog_over_the_daily_cap_labels_a_prefix_and_holds_the_rest(self):
        keys = self.backlog(3000)
        code, report = self.day(self.ENV, spend=True)
        self.assertEqual(code, 0, report["verdict"])
        self.assertTrue(report["ready"])
        # $1.00 at the projected cost of a title, in the store's row order.
        fits = int(1.0 / PER_TITLE + 1e-9)
        self.assertEqual(self.listed("new"), keys[:fits])
        self.assertEqual(self.listed("keys"), keys[:fits])
        self.assertEqual(report["heldForSpend"], 3000 - fits)
        self.assertEqual(report["added"], keys[:fits])
        self.assertEqual(report["counts"]["heldForSpend"], 3000 - fits)
        self.assertAlmostEqual(report["spend"]["steps"]["typesafe"]["projectedUSD"], fits * PER_TITLE)
        self.assertIn("classify", self.ran(), "the paid stages still run, over the prefix")

    def test_the_projection_prices_every_step_a_title_goes_through(self):
        """Daily run 38201604704 labelled 2,500 titles under a $1.00 cap and spent $2.0064: classify $1.1797,
        genres & moods $0.8267 and franchises $0.0055. One call a title ($0.0004) projected half of it."""
        measured = 1.1797 + 0.8267 + 0.0049 + 0.0006
        projected = 2500 * PER_TITLE
        self.assertGreaterEqual(projected, measured, "a day must not spend more than it projected")
        self.assertLess(projected, measured * 1.1, "and not hold titles the cap could have bought")
        self.assertEqual(set(daily.TYPESAFE_PROJECTED_PER_STEP), {"classify", "genres_moods", "franchises"})

    def test_a_parked_title_is_not_billed_and_a_backlog_that_fits_holds_nothing(self):
        self.backlog(5000, plotless=3900)
        code, report = self.day(self.ENV, spend=True)
        self.assertEqual(code, 0, report["verdict"])
        self.assertEqual(report["heldForSpend"], 0)
        self.assertEqual(len(self.listed("new")), 5000, "the parked ones stay listed, as they always were")
        self.assertAlmostEqual(report["spend"]["steps"]["typesafe"]["projectedUSD"], 1100 * PER_TITLE)

    def test_the_month_can_cut_the_day_shorter_than_the_cap(self):
        self.backlog(3000)
        reports = self.spent(9.96)
        code, report = self.day(self.ENV, spend=True, published_reports_dir=reports)
        self.assertEqual(code, 0, report["verdict"])
        fits = int(0.04 / PER_TITLE + 1e-9)
        self.assertEqual(len(self.listed("new")), fits, "$0.04 left in the month")
        self.assertAlmostEqual(report["spend"]["steps"]["typesafe"]["projectedUSD"], fits * PER_TITLE)

    def test_a_month_with_no_room_for_the_day_still_refuses(self):
        self.backlog(100)
        reports = self.spent(9.9998)
        code, report = self.day(self.ENV, spend=True, published_reports_dir=reports)
        self.assertEqual(code, 1)
        self.assertIn("monthly cap", report["verdict"])
        self.assertEqual(report["heldForSpend"], 0)

    def test_a_cap_below_one_titles_cost_still_refuses(self):
        self.backlog(3000)
        code, report = self.day(self.ENV, spend=True, typesafe_max_spend_usd=0.0003)
        self.assertEqual(code, 1)
        self.assertIn("crossing its $0.00 daily cap", report["verdict"])

    def test_a_malformed_cap_still_refuses(self):
        self.backlog(3000)
        code, report = self.day(self.ENV, spend=True, typesafe_max_spend_usd=float("nan"))
        self.assertEqual(code, 1)
        self.assertIn("must be a positive finite", report["verdict"])

    def test_a_title_an_earlier_run_of_the_day_already_bought_stays_and_costs_nothing(self):
        keys = self.backlog(6000)
        with mock.patch.object(daily.classify, "answered", return_value={key: "sha" for key in keys[:2500]}):
            _code, report = self.day(self.ENV, spend=True)
        fits = 2500 + int(1.0 / PER_TITLE + 1e-9)
        self.assertEqual(self.listed("new"), keys[:fits], "the 2,500 bought, then what the cap buys fit")
        self.assertEqual(report["heldForSpend"], 6000 - fits)


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


class AddedSplit(Recorded):
    def test_the_report_separates_plotted_additions_from_parked_plotless_ones(self):
        """`added` stays every new title (the stages read it); the report says how many of them can ship."""
        self.plan.update(counts={"added": 3, "addedPlotted": 1, "addedPlotless": 2},
                         added=["movie:1", "movie:2", "tv:3"], addedPlotless=["movie:2", "tv:3"])
        _code, report = self.day()
        self.assertEqual(report["added"], ["movie:1", "movie:2", "tv:3"])
        self.assertEqual(report["addedPlotless"], ["movie:2", "tv:3"])
        with open(os.path.join(self.out, daily.SUMMARY), encoding="utf-8") as fh:
            summary = fh.read()
        self.assertIn("| added with a plot | added without a plot |", summary)
        self.assertIn("| 1 | 2 |", summary)
        self.assertIn("**Added with a plot** (1): movie:1", summary)
        self.assertIn("**Added without a plot (parked)** (2): movie:2, tv:3", summary)


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


class JudgedBelow(unittest.TestCase):
    def test_the_verdicts_a_drain_reached_are_saved_between_batches_not_only_at_the_end(self):
        """A weekly drain can be cancelled by the job's time limit, which runs no `finally`."""
        directory = self.enterContext(tempfile.TemporaryDirectory())
        ledger = os.path.join(directory, "paid", "paid-state.json.gz")
        day = daily.Day(args(directory, paid_state=ledger), {}, NOW)
        floors = [50, 15, 10, 3]
        with open(enrich.checkpoint_path(directory), "w") as fh:
            fh.write(enrich.compact({"nextBatch": 2, "processed": [], "totals": {}, "judgedBelow": {
                "movie:1": {"on": "2026-10-11", "votes": 4, "floors": floors}}}))
        day.paid.restore(day.ctx)
        day.persist_judged()
        self.assertEqual(daily.paid.Ledger(ledger).data["judged"],
                         {"movie:1": {"on": "2026-10-11", "floors": floors, "side": 0}})


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


class ReportArtifact(unittest.TestCase):
    """The daily workflow's report artifact is public (a public repo's artifacts are downloadable by anyone
    signed in): it must carry no article text and no classify-row title. Held against the files a real premise
    worklist and a collected Batch write, not against a list of names someone remembered."""

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    WORKFLOW = os.path.join(REPO, ".github", "workflows", "daily.yml")

    def uploaded(self):
        """The report step's `path:` patterns, relative to the out-dir."""
        with open(self.WORKFLOW, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        start = lines.index("      - name: Upload the report")
        at = next(i for i in range(start, len(lines)) if lines[i].strip() == "path: |")
        patterns = []
        for line in lines[at + 1:]:
            if not line.startswith(" " * 12):
                break
            patterns.append(line.strip().replace("${{ env.OUT }}/", ""))
        return patterns

    @staticmethod
    def matches(pattern, path):
        """upload-artifact's glob: `**/` any depth (none included), `*` within a segment, a trailing `/` the
        whole directory."""
        if pattern.endswith("/"):
            return path.startswith(pattern)
        regex = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*", "[^/]*")
        return re.fullmatch(regex, path) is not None

    def test_no_uploaded_file_carries_article_text_or_a_title(self):
        title, evidence = "A Distinctive Title", "Twin sisters swap places to expose a smuggling ring."
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "articles.jsonl"), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"mediaType": "movie", "tmdbId": 990000001, "text": evidence}) + "\n")
            with open(os.path.join(out, "combined.jsonl"), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "mediaType": "movie", "tmdbId": 990000001, "title": title, "year": 2026, "language": "en",
                    "article": title, "answers": {"validity": {"choice": "correct-screen-work"},
                                                  "narrative_applicability": {"choice": "narrative"}},
                    "sections": [{"heading": "Plot", "start": 0, "end": len(evidence),
                                  "role": {"value": "story-premise"}}]}) + "\n")
            plan = os.path.join(out, "plan.json")
            with open(plan, "w", encoding="utf-8") as fh:
                json.dump({"baseline": {"datasetVersion": "live"}, "added": ["movie:990000001"], "changed": {}}, fh)
            work = os.path.join(out, "premise-increment", "weekly")
            subprocess.run([sys.executable, os.path.join(self.REPO, "pipeline", "build_premise_worklist.py"),
                            "--combined", os.path.join(out, "combined.jsonl"),
                            "--articles", os.path.join(out, "articles.jsonl"), "--changes", plan,
                            "--token-ceiling", "100000", "--out-dir", work], check=True, capture_output=True)
            daily.premise_daily.write_collected(os.path.join(out, "premise-increment", "collected"),
                                                {"movie:990000001": {"tags": ["twin-swap"], "by": {}}})
            written = sorted(os.path.relpath(os.path.join(root, name), out)
                             for root, _, names in os.walk(os.path.join(out, "premise-increment")) for name in names)
            sent = [path for path in written if any(self.matches(p, path) for p in self.uploaded())]
            self.assertTrue(any(path.endswith("worklist.jsonl") for path in written), "the fixture wrote the worklist")
            for path in sent:
                with open(os.path.join(out, path), encoding="utf-8") as fh:
                    body = fh.read()
                self.assertNotIn(title, body, path)
                self.assertNotIn(evidence[:20], body, path)
        self.assertEqual(sent, ["premise-increment/weekly/gen/manifest.json"])


if __name__ == "__main__":
    unittest.main()
