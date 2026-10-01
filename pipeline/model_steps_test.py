#!/usr/bin/env python3
"""`pipeline/model_steps.py` (#187 item 3): when a weekly step is due, which titles wait for it, and its Batch
jobs — submitted on its day, collected on a later one, an expired job finished online — offline, with the
providers stubbed."""
import argparse
import contextlib
import datetime
import gzip
import io
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from lib import llm, llm_providers as providers

from . import artifacts, daily, model_steps, premise_daily
from . import premise_daily_test as premise_tests
from tools import fan_picks_test as fan_tests

MONDAY = datetime.datetime(2026, 10, 5, 3, 23, tzinfo=datetime.timezone.utc)
TUESDAY = MONDAY + datetime.timedelta(days=1)


class Due(unittest.TestCase):
    weekly = {"cadence": "weekly", "day": "mon"}

    def test_a_daily_step_is_always_due(self):
        self.assertTrue(model_steps.due({"cadence": "daily"}, TUESDAY.date(), "2026-10-06"))

    def test_a_weekly_step_is_due_once_its_day_has_come_since_its_last_submit(self):
        due = model_steps.due
        self.assertTrue(due(self.weekly, MONDAY.date(), None))
        self.assertFalse(due(self.weekly, MONDAY.date(), "2026-10-05"))
        self.assertFalse(due(self.weekly, TUESDAY.date(), "2026-10-05"))
        self.assertTrue(due(self.weekly, TUESDAY.date(), "2026-09-28"), "a failed Monday is caught up on Tuesday")
        self.assertTrue(due(self.weekly, MONDAY.date() + datetime.timedelta(days=7), "2026-10-05"))


class Batch:
    """A provider whose Batch job answers what it was sent once `state` says so; online answers too."""

    def __init__(self, answer):
        self.answer, self.state, self.submitted, self.online_calls = answer, "running", {}, []

    def submit(self, cfg, requests, label, source):
        self.submitted.update(requests)
        return {"name": f"batches/{label}"}

    def poll(self, cfg, job, customs):
        if self.state != "done":
            return self.state, {}
        return "done", {custom: providers.answer(self.answer(self.submitted[custom]), usage=USAGE)
                        for custom in customs}

    def online(self, cfg, request):
        self.online_calls.append(request)
        return providers.answer(self.answer(request), usage=USAGE)

    @staticmethod
    def body(cfg, request):
        return providers.Gemini().body(cfg, request)


USAGE = {"inputTokens": 400, "cachedTokens": 0, "outputTokens": 300, "reasoningTokens": 0}


def day_on(now, out, ledger, environ):
    args = argparse.Namespace(out_dir=out, mode="export", since=None, revisit_weeks=None, spend=True,
                              fan_picks_max_spend_usd=1.0, premise_max_spend_usd=1.0, paid_state=ledger,
                              spend_premise=True)
    day = daily.Day(args, environ, now)
    return day


class FanPicksWeekly(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.ledger = os.path.join(self.root, "paid-state.json.gz")
        self.provider = Batch(lambda request: '{"k":true,"p":[["Seven Samurai",1954,"f"]]}')
        for patch in (mock.patch.dict(providers.PROVIDERS, {"gemini": self.provider}),
                      contextlib.redirect_stderr(io.StringIO())):
            self.enterContext(patch)

    def run_day(self, now, waiting=("movie:1",), anchors=None):
        out = os.path.join(self.root, now.date().isoformat())
        os.makedirs(out)
        corpus, articles, franchises, existing = fan_tests.Daily().fixture(out, anchors or {"movie:2": []})
        paths = {artifacts.CORPUS: corpus, artifacts.ARTICLES: articles, artifacts.FRANCHISES: franchises,
                 artifacts.FAN_PICKS: existing}
        ctx = types.SimpleNamespace(path=lambda artifact: paths.get(artifact, os.path.join(out, "x")))
        day = day_on(now, out, self.ledger, {"GEMINI_API_KEY": "g"})
        day.waiting = {"fan_picks": list(waiting)}
        with mock.patch.object(model_steps.fan_picks, "load_reask", return_value={}):
            day.reasks = model_steps.reasks_due(day, ctx)
        with mock.patch.object(model_steps, "finalize_ctx", return_value=ctx), \
                mock.patch.object(model_steps.fan_picks, "sequel_keys", return_value={}):
            model_steps.fan_picks_step(day)
        with open(existing, encoding="utf-8") as fh:
            return day, json.load(fh)["anchors"]

    def test_monday_submits_what_waits_and_a_later_run_collects_it(self):
        monday, anchors = self.run_day(MONDAY)
        self.assertNotIn("movie:1", anchors, "submitted, not yet answered: published without fan picks")
        self.assertEqual(monday.models["fan_picks"]["submitted"]["titles"], 1)
        self.assertEqual(len(monday.paid.data["batches"]), 1)
        self.provider.state = "done"
        tuesday, anchors = self.run_day(TUESDAY)
        self.assertEqual(anchors["movie:1"], ["movie:2"])
        report = tuesday.models["fan_picks"]
        self.assertEqual((report["due"], report["collected"], report["submitted"]), (False, 1, None))
        self.assertAlmostEqual(report["costUSD"], llm.cost(model_steps.fan_picks.CFG, USAGE, "batch"), places=6)
        self.assertEqual(tuesday.paid.data["batches"], [])
        self.assertEqual(self.provider.online_calls, [])

    def test_a_job_still_running_is_reported_with_its_age_and_not_resubmitted(self):
        self.run_day(MONDAY)
        tuesday, anchors = self.run_day(TUESDAY)
        [pending] = tuesday.models["fan_picks"]["pending"]
        self.assertEqual((pending["titles"], pending["ageHours"]), (1, 24.0))
        self.assertEqual(len(self.provider.submitted), 1, "never submitted twice")
        self.assertNotIn("movie:1", anchors)

    def test_a_title_due_a_reask_is_asked_again_and_a_known_answer_replaces_its_picks(self):
        """#187 item 5: asked on 2026-06-01 and not known, so asked again from 2026-08-31 — once."""
        ledger = daily.paid.Ledger(self.ledger)
        ledger.answers("fan_picks")["movie:1"] = {"accepted": True, "requestSha256": "old", "error": None,
                                                 "answer": {"known": False, "picks": [], "at": "2026-06-01"}}
        ledger.save()
        anchors = {"movie:1": [], "movie:2": []}
        monday, before = self.run_day(MONDAY, waiting=(), anchors=anchors)
        self.assertEqual((before["movie:1"], monday.models["fan_picks"]["reasks"]["due"]), ([], 1))
        self.assertEqual(monday.paid.data["steps"]["fan_picks"]["reasked"], {"movie:1": ["2026-08-31"]})
        self.provider.state = "done"
        tuesday, after = self.run_day(TUESDAY, waiting=(), anchors=anchors)
        self.assertEqual(after["movie:1"], ["movie:2"])
        self.assertEqual(tuesday.models["fan_picks"]["reasks"], {"due": 0, "collected": 1})
        self.assertEqual(len(self.provider.submitted), 1, "asked once for that date")

    def test_an_expired_job_is_finished_online(self):
        self.run_day(MONDAY)
        self.provider.state = "expired"
        tuesday, anchors = self.run_day(TUESDAY)
        self.assertEqual(anchors["movie:1"], ["movie:2"])
        self.assertEqual(len(self.provider.online_calls), 1)
        self.assertEqual(tuesday.models["fan_picks"]["expired"], ["fan-picks-2026-10-05"])


class PremiseWeekly(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.ledger = os.path.join(self.root, "paid-state.json.gz")
        tags = premise_tests.TAGS
        self.provider = Batch(lambda request: json.dumps({"rows": [
            {"key": row["key"], "tags": tags} for row in json.loads(request["prompt"].split("\n", 1)[1])]}))
        self.provider.KEY = providers.OpenAI.KEY
        self.enterContext(mock.patch.dict(providers.PROVIDERS, {"openai": self.provider}))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        case = premise_tests.PremiseGeneration()
        case.enterContext = self.enterContext
        self.phase = case.phase()

    def run_day(self, now, fetched=(7, 8)):
        out = os.path.join(self.root, now.date().isoformat())
        os.makedirs(os.path.join(out, "changes"))
        with open(os.path.join(out, "changes", "plan.json"), "w") as fh:
            json.dump({"baseline": {"datasetVersion": "live"}, "added": [], "changed": {}}, fh)
        with open(os.path.join(out, artifacts.ARTICLES.filename), "w") as fh:
            fh.writelines(json.dumps({"mediaType": "movie", "tmdbId": n, "text": "x"}) + "\n" for n in fetched)
        day = day_on(now, out, self.ledger, {"OPENAI_API_KEY": "o", "DEN_EMBED_URL": "http://embed.invalid"})
        day.waiting = {"premise_tags": ["movie:7", "movie:8", "movie:9"]}
        merged = []
        work = os.path.dirname(self.phase)
        with mock.patch.object(premise_daily, "ensure_tags", return_value=os.path.join(self.root, "tags.json")), \
                mock.patch.object(premise_daily, "prepare", return_value=(work, {"estimatedInputTokens": 100,
                                                                                 "estimatedOutputTokens": 100})), \
                mock.patch.object(premise_daily, "rows_of", return_value=premise_daily.rows_of(self.phase)), \
                mock.patch.object(premise_daily, "merge", lambda ctx, phase, result, when: merged.append(
                    (premise_daily._json(os.path.join(phase, "out", "batch-0000.json")), result))):
            model_steps.premise_step(day)
        return day, merged

    def test_monday_submits_tuesday_collects_and_merges_with_who_answered(self):
        monday, merged = self.run_day(MONDAY)
        self.assertEqual((merged, monday.models["premise_tags"]["submitted"]["titles"]), ([], 2))
        self.assertEqual(monday.paid.data["steps"]["premise_tags"]["settled"], [],
                         "movie:9 has no article today, so it is not settled")
        self.provider.state = "done"
        tuesday, merged = self.run_day(TUESDAY)
        [(rows, result)] = merged
        self.assertEqual(sorted(row["key"] for row in rows), ["movie:7", "movie:8"])
        self.assertEqual(rows[0]["by"]["mode"], "batch")
        self.assertEqual((result["titles"], tuesday.premise["titles"]), (2, 2), "the finalize stage embeds them")
        self.assertEqual(set(tuesday.paid.answers("premise_tags")), {"movie:7", "movie:8"})

    def test_a_title_the_worklist_turns_away_with_its_article_in_hand_is_settled(self):
        """movie:9's article was fetched and the worklist still did not take it (not narrative, no premise
        section…): it stops waiting, so its article is not fetched again every day."""
        monday, _ = self.run_day(MONDAY, fetched=(7, 8, 9))
        self.assertEqual(monday.paid.data["steps"]["premise_tags"]["settled"], ["movie:9"])
        day = day_on(TUESDAY, os.path.join(self.root, "x"), self.ledger, {})
        self.assertEqual(day.paid.data["steps"]["premise_tags"]["settled"], ["movie:9"], "kept in the ledger")


class Waiting(unittest.TestCase):
    def test_the_waiting_titles_are_written_for_the_articles_stage(self):
        with tempfile.TemporaryDirectory() as out:
            os.makedirs(os.path.join(out, "published"))
            os.makedirs(os.path.join(out, "changes"))
            with open(os.path.join(out, "changes", "plan.json"), "w") as fh:
                json.dump({"baseline": {"datasetVersion": "live"}, "added": ["movie:5"], "changed": {},
                           "withdrawn": {"movie:3": "gone"}}, fh)
            for name in ("new", "keys", "withdrawn", "items"):
                open(os.path.join(out, "changes", f"{name}.txt"), "w").close()
            with gzip.open(os.path.join(out, artifacts.PUBLISHED_CORPUS.filename), "wt") as fh:
                fh.writelines(json.dumps({"key": k}) + "\n" for k in ("movie:1", "movie:2", "movie:3"))
            with open(os.path.join(out, artifacts.FAN_PICKS.filename), "w") as fh:
                json.dump({"anchors": {"movie:1": []}}, fh)
            with open(os.path.join(out, artifacts.PREMISE_TAGS.filename), "w") as fh:
                json.dump({"tags": {"movie:4": ["a"]}}, fh)
            day = day_on(MONDAY, out, os.path.join(out, "ledger.json.gz"),
                         {"GEMINI_API_KEY": "g", "OPENAI_API_KEY": "o", "DEN_EMBED_URL": "http://embed.invalid"})
            day.paid.data["shards"]["combined-v1-r2-abc.jsonl"] = {
                "rows": '{"mediaType": "movie", "tmdbId": 4}\n{"mediaType": "tv", "tmdbId": 6}\n', "manifest": "{}"}
            with mock.patch.object(model_steps.fan_picks, "load_reask",
                                   return_value={"movie:1": {"askedAt": "2026-06-01", "known": False,
                                                             "released": None}}):
                waiting = model_steps.write_waiting(day)
            with open(os.path.join(out, "changes", "waiting.txt")) as fh:
                listed = fh.read().split()
        self.assertEqual(waiting, {"fan_picks": ["movie:2", "movie:5"], "premise_tags": ["tv:6"]})
        self.assertEqual(day.reasks, {"movie:1": ["2026-08-31"]})
        self.assertEqual(listed, ["movie:1", "movie:2", "movie:5", "tv:6"], "a re-ask needs its lead fetched too")


if __name__ == "__main__":
    unittest.main()
