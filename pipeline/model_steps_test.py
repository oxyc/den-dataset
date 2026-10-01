#!/usr/bin/env python3
"""`pipeline/model_steps.py` (#187 item 3): when a weekly step is due, which titles wait for it, and its Batch
jobs — submitted on its day, collected on a later one, an expired job finished online inside the caps and
across runs — offline, with the providers stubbed."""
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
WEDNESDAY = TUESDAY + datetime.timedelta(days=1)


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
    """A provider whose Batch job answers what it was sent once `state` says so; online answers too. It
    lists its jobs by label, as Gemini and OpenAI do."""

    def __init__(self, answer):
        self.answer, self.state, self.submitted, self.online_calls = answer, "running", {}, []
        self.made, self.lose_response = {}, False

    def submit(self, cfg, requests, label, source):
        self.submitted.update(requests)
        self.made[label] = {"name": f"batches/{label}"}
        if self.lose_response:
            raise providers.Unavailable("connection reset by org-secret", None)
        return self.made[label]

    def find(self, cfg, label):
        return self.made.get(label)

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


def day_on(now, out, ledger, environ, spend=True):
    args = argparse.Namespace(out_dir=out, mode="export", since=None, revisit_weeks=None, spend=spend,
                              fan_picks_max_spend_usd=1.0, premise_max_spend_usd=1.0, paid_state=ledger,
                              spend_premise=True)
    return daily.Day(args, environ, now)


class FanPicksWeekly(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.ledger = os.path.join(self.root, "paid-state.json.gz")
        self.provider = Batch(lambda request: '{"k":true,"p":[["Seven Samurai",1954,"f"]]}')
        for patch in (mock.patch.dict(providers.PROVIDERS, {"gemini": self.provider}),
                      contextlib.redirect_stderr(io.StringIO())):
            self.enterContext(patch)

    def run_day(self, now, waiting=("movie:1",), anchors=None, cap=1.0, spend=True, monthly=10.0, unnamed=()):
        out = os.path.join(self.root, now.date().isoformat())
        os.makedirs(out)
        corpus, articles, franchises, existing = fan_tests.Daily().fixture(out, anchors or {"movie:2": []})
        with gzip.open(corpus, "at", encoding="utf-8") as fh:
            for key in unnamed:
                fh.write(json.dumps({"key": key, "facts": {"released": {"date": "2024"}}}) + "\n")
        paths = {artifacts.CORPUS: corpus, artifacts.ARTICLES: articles, artifacts.FRANCHISES: franchises,
                 artifacts.FAN_PICKS: existing}
        ctx = types.SimpleNamespace(path=lambda artifact: paths.get(artifact, os.path.join(out, "x")))
        day = day_on(now, out, self.ledger, {"GEMINI_API_KEY": "g"}, spend=spend)
        day.args.fan_picks_max_spend_usd = cap
        day.ledger.monthly_cap = monthly
        day.waiting = {"fan_picks": list(waiting)}
        with mock.patch.object(model_steps.fan_picks, "load_reask", return_value={}):
            day.reasks = model_steps.reasks_due(day)
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

    def test_a_corpus_title_with_no_name_is_not_asked(self):
        # The 2026-10-01 daily: 134 corpus titles have no Wikidata name, so no card to ask about, and the first
        # of them stopped the store stage.
        monday, _ = self.run_day(MONDAY, waiting=("movie:1", "movie:1000127"), unnamed=("movie:1000127",))
        self.assertEqual(monday.models["fan_picks"]["submitted"]["titles"], 1)
        self.assertEqual(len(self.provider.submitted), 1)

    def test_a_submitted_job_is_committed_spend_until_it_is_collected(self):
        monday, _ = self.run_day(MONDAY)
        projected = monday.paid.data["batches"][0]["projectedUSD"]
        self.assertGreater(projected, 0)
        self.assertEqual(monday.ledger.report()["pendingBatchUSD"], projected)
        self.assertEqual(monday.ledger.report()["totalUSD"], 0.0, "nothing measured yet")
        self.provider.state = "done"
        tuesday, _ = self.run_day(TUESDAY)
        self.assertEqual(daily.paid.Ledger(self.ledger).pending_usd(), 0.0)
        self.assertEqual(tuesday.ledger.report()["pendingBatchUSD"], 0.0)
        self.assertGreater(tuesday.ledger.report()["totalUSD"], 0.0, "measured the day it is collected")

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
        self.run_day(MONDAY - datetime.timedelta(days=120), waiting=(), anchors=anchors)
        self.assertEqual(daily.paid.Ledger(self.ledger).data["steps"]["fan_picks"]["reaskPlan"]["movie:1"],
                         model_steps.fan_picks.reask_dates("2026-06-01", False, None),
                         "the schedule is fixed when the answer is first seen")
        monday, before = self.run_day(MONDAY, waiting=(), anchors=anchors)
        self.assertEqual((before["movie:1"], monday.models["fan_picks"]["reasks"]["due"]), ([], 1))
        self.assertEqual(monday.paid.data["steps"]["fan_picks"]["reasked"], {"movie:1": ["2026-08-31"]})
        self.provider.state = "done"
        tuesday, after = self.run_day(TUESDAY, waiting=(), anchors=anchors)
        self.assertEqual(after["movie:1"], ["movie:2"])
        self.assertEqual(tuesday.models["fan_picks"]["reasks"], {"due": 0, "answered": 1})
        self.assertEqual(len(self.provider.submitted), 1, "asked once for that date")
        self.provider.state = "running"
        _, still = self.run_day(WEDNESDAY, waiting=(), anchors=anchors)
        self.assertEqual(still["movie:1"], ["movie:2"], "the re-ask answer is applied until a publish prunes it")

    def test_a_new_title_is_scheduled_from_its_release_date_in_todays_corpus(self):
        rows = {"movie:1": {"facts": {"released": {"date": "2026-09-20"}}}}
        day = day_on(MONDAY, self.root, self.ledger, {})
        day.paid.answers("fan_picks")["movie:1"] = {"answer": {"known": True, "at": "2026-10-05T03:23:00"}}
        model_steps.schedule_reasks(day, rows)
        self.assertEqual(day.paid.data["steps"]["fan_picks"]["reaskPlan"]["movie:1"],
                         model_steps.fan_picks.reask_dates("2026-10-05", True, "2026-09-20"))
        self.assertTrue(day.paid.data["steps"]["fan_picks"]["reaskPlan"]["movie:1"],
                        "known, but released two weeks before it was asked: asked again")

    def test_an_outage_while_collecting_keeps_the_job_and_reports_a_code_not_the_providers_text(self):
        self.run_day(MONDAY)
        self.provider.state = "done"

        def down(*_):
            raise providers.Unavailable("HTTP 503 b'busy for org-1234'", 503)
        self.provider.poll = down
        tuesday, anchors = self.run_day(TUESDAY)
        [pending] = tuesday.models["fan_picks"]["pending"]
        self.assertEqual(pending["error"], "HTTP 503")
        self.assertEqual(len(tuesday.paid.data["batches"]), 1)
        self.assertNotIn("movie:1", anchors)

    def test_a_backlog_past_the_cap_waits(self):
        each = model_steps.fan_picks.PILOT_COST * model_steps.fan_picks.MARGIN * llm.BATCH_FACTOR
        monday, _ = self.run_day(MONDAY, waiting=("movie:1", "movie:8", "movie:9"), cap=each * 1.5)
        self.assertEqual((monday.models["fan_picks"]["submitted"]["titles"], monday.models["fan_picks"]["deferred"]),
                         (1, 2))

    def test_a_month_with_no_room_defers_the_submit_and_keeps_the_step_due(self):
        """What is committed counts: the month's other spend leaves no room, so nothing is submitted, the day
        does not fail, and the step is due again on the next run."""
        monday, _ = self.run_day(MONDAY, waiting=("movie:1", "movie:8"), monthly=0.000001)
        report = monday.models["fan_picks"]
        self.assertEqual((report["submitted"], report["deferred"]), (None, 2))
        self.assertNotIn("lastSubmit", monday.paid.data["steps"]["fan_picks"])
        self.assertEqual(self.provider.submitted, {})

    def test_an_expired_job_is_finished_online(self):
        self.run_day(MONDAY)
        self.provider.state = "expired"
        tuesday, anchors = self.run_day(TUESDAY)
        self.assertEqual(anchors["movie:1"], ["movie:2"])
        self.assertEqual(len(self.provider.online_calls), 1)
        self.assertEqual(len(tuesday.models["fan_picks"]["expired"]), 1)
        self.assertEqual(tuesday.paid.data["batches"], [])

    def test_an_expired_job_is_finished_across_days_inside_the_cap_and_never_bought_twice(self):
        """The reviewer's probe, end to end: Tuesday's allowance holds one online call of three, so Tuesday
        finishes one and keeps the job; Wednesday finishes the other two, asking none already kept."""
        self.run_day(MONDAY, waiting=("movie:1", "movie:8", "movie:9"))
        self.provider.state = "expired"
        projected = daily.paid.Ledger(self.ledger).data["batches"][0]["projectedUSD"]
        call = llm.cost(model_steps.fan_picks.CFG, USAGE, "online")
        with mock.patch.object(llm, "ceiling", return_value=0.01):
            hold = 0.01 * providers.GENERATION_SENDS
            tuesday, anchors = self.run_day(TUESDAY, waiting=("movie:1", "movie:8", "movie:9"),
                                            cap=projected + hold + call / 2)
            self.assertEqual(len(self.provider.online_calls), 1)
            [pending] = tuesday.models["fan_picks"]["pending"]
            self.assertEqual(pending["unfinished"], 2)
            self.assertLessEqual(tuesday.ledger.current("fanPicks"), projected + hold + call / 2)
            self.assertEqual(len([key for key in ("movie:1", "movie:8", "movie:9") if key in anchors]), 1)
            wednesday, anchors = self.run_day(WEDNESDAY, waiting=("movie:1", "movie:8", "movie:9"))
        self.assertEqual(len(self.provider.online_calls), 3, "each title asked online once")
        self.assertEqual(wednesday.paid.data["batches"], [])
        self.assertTrue(all(anchors[key] == ["movie:2"] for key in ("movie:1", "movie:8", "movie:9")))

    def test_a_run_that_may_not_spend_still_collects_and_asks_nothing_online(self):
        self.run_day(MONDAY)
        self.provider.state = "expired"
        tuesday, anchors = self.run_day(TUESDAY, spend=False)
        self.assertEqual(self.provider.online_calls, [])
        self.assertEqual(tuesday.models["fan_picks"]["pending"][0]["unfinished"], 1)
        self.assertNotIn("movie:1", anchors)
        self.provider.state = "done"
        _, anchors = self.run_day(WEDNESDAY, spend=False)
        self.assertEqual(anchors["movie:1"], ["movie:2"], "a finished job is read without spend")

    def test_a_job_owed_titles_past_the_give_up_age_is_let_go(self):
        self.run_day(MONDAY)
        self.provider.state = "expired"
        later = MONDAY + datetime.timedelta(days=model_steps.GIVE_UP_DAYS + 1)
        day, _ = self.run_day(later, spend=False)
        self.assertEqual(day.paid.data["batches"], [])

    def test_a_submit_whose_response_was_lost_is_found_by_its_label_not_bought_again(self):
        self.provider.lose_response = True
        monday, _ = self.run_day(MONDAY)
        self.assertEqual(monday.models["fan_picks"]["submitError"], "unavailable")
        self.assertEqual(len(monday.paid.data["intents"]), 1)
        self.assertNotIn("lastSubmit", monday.paid.data["steps"]["fan_picks"])
        self.assertGreater(monday.ledger.report()["pendingBatchUSD"], 0, "an unconfirmed submit is committed")
        self.provider.lose_response = False
        self.provider.state = "done"
        tuesday, anchors = self.run_day(TUESDAY)
        self.assertEqual(len(self.provider.made), 1, "adopted by its label, not submitted again")
        self.assertEqual(tuesday.paid.data["intents"], [])
        self.assertEqual(anchors["movie:1"], ["movie:2"])

    def test_a_lost_submit_the_provider_never_made_gives_its_titles_back(self):
        self.provider.lose_response = True
        self.run_day(MONDAY)
        self.provider.made.clear()
        self.provider.lose_response = False
        tuesday, _ = self.run_day(TUESDAY)
        self.assertEqual(tuesday.paid.data["intents"], [])
        self.assertEqual(tuesday.models["fan_picks"]["submitted"]["titles"], 1, "asked again: never made")


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
        self.case = premise_tests.PremiseGeneration()
        self.case.enterContext = self.enterContext
        self.phase = self.case.phase()

    def run_day(self, now, skipped=None, phase=None):
        out = os.path.join(self.root, now.date().isoformat())
        os.makedirs(os.path.join(out, "changes"))
        with open(os.path.join(out, "changes", "plan.json"), "w") as fh:
            json.dump({"baseline": {"datasetVersion": "live"}, "added": [], "changed": {}}, fh)
        day = day_on(now, out, self.ledger, {"OPENAI_API_KEY": "o", "DEN_EMBED_URL": "http://embed.invalid"})
        day.waiting = {"premise_tags": ["movie:7", "movie:8", "movie:9"]}
        merged = []
        phase = phase or self.phase
        rows = premise_daily.rows_of(phase)
        manifest = {"titles": len(rows), "estimatedInputTokens": 100, "estimatedOutputTokens": 100,
                    "skippedKeys": skipped if skipped is not None else
                    {"movie:9": {"reason": "noArticleText", "articleSha256": "s9"}}}
        with mock.patch.object(premise_daily, "ensure_tags", return_value=os.path.join(self.root, "tags.json")), \
                mock.patch.object(premise_daily, "prepare", return_value=(os.path.dirname(phase), manifest)), \
                mock.patch.object(premise_daily, "rows_of", return_value=rows), \
                mock.patch.object(premise_daily, "merge", lambda ctx, phase, result, when: merged.append(
                    (premise_daily._json(os.path.join(phase, "out", "batch-0000.json")), result))):
            model_steps.premise_step(day)
        return day, merged

    def test_monday_submits_tuesday_collects_and_merges_with_who_answered(self):
        monday, merged = self.run_day(MONDAY)
        self.assertEqual((merged, monday.models["premise_tags"]["submitted"]["titles"]), ([], 2))
        self.assertEqual(monday.paid.data["steps"]["premise_tags"]["settled"], {},
                         "movie:9 has no article today, so it is not settled")
        self.provider.state = "done"
        tuesday, merged = self.run_day(TUESDAY)
        [(rows, result)] = merged
        self.assertEqual(sorted(row["key"] for row in rows), ["movie:7", "movie:8"])
        self.assertEqual(rows[0]["by"]["mode"], "batch")
        self.assertEqual((result["titles"], tuesday.premise["titles"]), (2, 2), "the finalize stage embeds them")
        self.assertEqual(set(tuesday.paid.answers("premise_tags")), {"movie:7", "movie:8"})

    def test_a_verdict_on_the_classification_settles_a_title_and_a_reclassification_reopens_it(self):
        monday, _ = self.run_day(MONDAY, skipped={"movie:9": {"reason": "nonNarrative", "articleSha256": "s9"}})
        self.assertEqual(monday.paid.data["steps"]["premise_tags"]["settled"], {"movie:9": "s9"})
        day = day_on(TUESDAY, os.path.join(self.root, "x"), self.ledger, {})
        self.assertEqual(day.paid.data["steps"]["premise_tags"]["settled"], {"movie:9": "s9"}, "kept in the ledger")

    def test_a_title_whose_article_changed_since_it_was_classified_is_never_settled(self):
        monday, _ = self.run_day(MONDAY, skipped={"movie:9": {"reason": "articleChanged", "articleSha256": "s9"}})
        self.assertEqual(monday.paid.data["steps"]["premise_tags"]["settled"], {})

    def test_a_title_whose_article_changed_between_submit_and_collect_is_requeued_with_its_answer_kept(self):
        self.run_day(MONDAY)
        self.provider.state = "done"
        # Tuesday's worklist no longer has movie:8: its article changed, so it waits to be classified again.
        tuesday, merged = self.run_day(TUESDAY, phase=self.case.phase(keys=("movie:7",)),
                                       skipped={"movie:8": {"reason": "articleChanged", "articleSha256": "s8"}})
        [(rows, _)] = merged
        self.assertEqual([row["key"] for row in rows], ["movie:7"], "only the title its evidence still fits")
        kept = tuesday.paid.answers("premise_tags")
        self.assertEqual(kept["movie:8"]["tags"], premise_tests.TAGS, "what was paid for is kept")
        self.assertEqual(tuesday.paid.data["steps"]["premise_tags"]["settled"], {}, "and the title is not settled")
        self.assertEqual(tuesday.paid.data["batches"], [], "nothing is owed: it is not finished on other evidence")
        self.assertEqual(self.provider.online_calls, [])

    def test_a_title_no_model_would_tag_is_recorded_and_not_asked_again_until_its_retry(self):
        self.provider.answer = lambda request: json.dumps({"rows": []})
        self.run_day(MONDAY)
        self.provider.state = "done"
        self.run_day(TUESDAY)
        kept = daily.paid.Ledger(self.ledger).answers("premise_tags")
        self.assertEqual({kept[key]["untaggable"] for key in ("movie:7", "movie:8")}, {"short"})
        next_week = MONDAY + datetime.timedelta(days=7)
        day, _ = self.run_day(next_week)
        self.assertEqual((day.models["premise_tags"]["notRetriedYet"], day.models["premise_tags"]["submitted"]),
                         (2, None))


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
                "rows": '{"mediaType": "movie", "tmdbId": 4}\n{"mediaType": "tv", "tmdbId": 6}\n'
                        '{"mediaType": "tv", "tmdbId": 7, "articleSha256": "a"}\n', "manifest": "{}"}
            day.paid.data["steps"]["premise_tags"] = {"settled": {"tv:7": "a"}}
            day.paid.data["steps"]["fan_picks"] = {"reaskPlan": {"movie:1": ["2026-08-31"]}}
            with mock.patch.object(model_steps.fan_picks, "load_reask", return_value={}):
                waiting = model_steps.write_waiting(day)
            with open(os.path.join(out, "changes", "waiting.txt")) as fh:
                listed = fh.read().split()
            with open(os.path.join(out, "changes", "reclassify.txt")) as fh:
                self.assertEqual(fh.read().split(), ["tv:6"], "classify looks again at a waiting title's article")
            # Classified again on a new article: the verdict on the old one no longer holds.
            day.paid.data["shards"]["combined-v1-r2-def.jsonl"] = {
                "rows": '{"mediaType": "tv", "tmdbId": 7, "articleSha256": "b"}\n',
                "manifest": '{"runStartedAt": "2026-10-05T00:00:00Z"}'}
            with mock.patch.object(model_steps.fan_picks, "load_reask", return_value={}):
                again = model_steps.write_waiting(day)
        self.assertEqual(waiting, {"fan_picks": ["movie:2", "movie:5"], "premise_tags": ["tv:6"]})
        self.assertEqual(day.paid.data["steps"]["premise_tags"]["waiting"], ["tv:6", "tv:7"])
        self.assertEqual(again["premise_tags"], ["tv:6", "tv:7"])
        self.assertEqual(day.reasks, {"movie:1": ["2026-08-31"]})
        self.assertEqual(listed, ["movie:1", "movie:2", "movie:5", "tv:6"], "a re-ask needs its lead fetched too")


if __name__ == "__main__":
    unittest.main()
