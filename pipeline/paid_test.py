#!/usr/bin/env python3
"""The paid-answers ledger (`pipeline/paid.py`, #187 item 1): a run killed after its paid calls returned and
before anything was published, then run again in a fresh out-dir with only the ledger, makes no paid call
for those titles — classify (with its critique), fan picks and premise tags alike."""
import gzip
import json
import os
import tempfile
import unittest
from unittest import mock

from . import artifacts, classify, paid, premise_daily
from . import premise_daily_test as premise_tests
from .contract import Context
from .critique_test import change_set, in_process
from .run_delta_test import StubTypeSafe, article
from tools import fan_picks
from tools import fan_picks_test as fan_tests

TAGS = premise_tests.TAGS


class Ledger(unittest.TestCase):
    def test_it_round_trips_and_keeps_only_what_this_run_bought(self):
        with tempfile.TemporaryDirectory() as root:
            out, path = os.path.join(root, "out"), os.path.join(root, "paid-state.json.gz")
            os.makedirs(out)
            for name in ("combined-v1-r2.jsonl", "combined-v1-r2-old.jsonl"):
                for suffix, body in (("", '{"row": 1}\n'), (".manifest.json", "{}")):
                    with open(os.path.join(out, name + suffix), "w") as fh:
                        fh.write(body)
            ledger = paid.Ledger(path)
            ledger.restore(Context(out_dir=out))
            for suffix, body in (("", '{"row": 2}\n'), (".manifest.json", '{"runId": "r"}')):
                with open(os.path.join(out, "combined-v1-r2-abc.jsonl" + suffix), "w") as fh:
                    fh.write(body)
            ledger.answers("fan_picks")["movie:1"] = {"accepted": True}
            ledger.keep(Context(out_dir=out))
            ledger.save()
            again = paid.Ledger(path)
            with gzip.open(path, "rb") as fh:
                self.assertNotIn(b"\n ", fh.read(), "one compact line")
        self.assertEqual(list(again.data["shards"]), ["combined-v1-r2-abc.jsonl"])
        self.assertEqual(again.data["shards"]["combined-v1-r2-abc.jsonl"],
                         {"rows": '{"row": 2}\n', "manifest": '{"runId": "r"}'})
        self.assertEqual(again.answers("fan_picks"), {"movie:1": {"accepted": True}})

    def test_without_a_file_or_a_restore_it_keeps_nothing(self):
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "combined-v1-r2.jsonl"), "w") as fh:
                fh.write("{}\n")
            unfiled = paid.Ledger(None)
            unfiled.restore(Context(out_dir=out))
            unfiled.keep(Context(out_dir=out))
            unrestored = paid.Ledger(os.path.join(out, "ledger.json.gz"))
            unrestored.keep(Context(out_dir=out))
        self.assertEqual((unfiled.data["shards"], unrestored.data["shards"]), ({}, {}))

    def test_franchise_decisions_are_restored_as_the_union_of_both(self):
        doc = {"schema": "s", "questionsSha256": "q", "model": "m",
               "decisions": {"movie:1": {"usage": {"inputTokens": 5, "outputTokens": 1}}},
               "usage": {}}
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, artifacts.FRANCHISE_DECISIONS.filename)
            with open(path, "w") as fh:
                json.dump({**doc, "decisions": {"movie:2": {"usage": {"inputTokens": 7, "outputTokens": 1}}}}, fh)
            ledger = paid.Ledger(None)
            ledger.data["files"][artifacts.FRANCHISE_DECISIONS.filename] = json.dumps(doc)
            ledger.restore(Context(out_dir=out))
            with open(path) as fh:
                merged = json.load(fh)
        self.assertEqual(sorted(merged["decisions"]), ["movie:1", "movie:2"])
        self.assertEqual(merged["usage"]["calls"], 2)
        self.assertEqual(merged["usage"]["inputTokens"], 12)


class KilledAndRunAgain(unittest.TestCase):
    """A run that bought and was killed before the store, and the next one, which starts from nothing but
    the ledger."""

    def out_dir(self, root, name):
        out = os.path.join(root, name)
        os.makedirs(os.path.join(out, artifacts.ENRICHED.filename))
        with open(os.path.join(out, artifacts.ARTICLES.filename), "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(article(i)) + "\n" for i in (1, 2, 3))
        change_set(out, ["movie:2", "movie:3"])
        return Context(out_dir=out, spend=True)

    def test_classify_and_critique_are_not_bought_again(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(classify.subprocess, "run", in_process):
            ledger_path = os.path.join(root, "paid-state.json.gz")
            StubTypeSafe.requests = []
            first = self.out_dir(root, "first")
            ledger = paid.Ledger(ledger_path)
            ledger.restore(first)
            classify.run(first)
            self.assertEqual(len(StubTypeSafe.requests), 2, "two titles bought")
            ledger.keep(first)
            ledger.save()
            # Killed here: nothing published, the out-dir gone.
            StubTypeSafe.requests = []
            second = self.out_dir(root, "second")
            self.assertEqual(paid.Ledger(ledger_path).restore(second), 1)
            self.assertIsNone(classify.changed_articles(second))
            self.assertIn("nothing to classify", classify.run(second))
            self.assertEqual(StubTypeSafe.requests, [])
            self.assertIsNotNone(classify.changed_articles(second, skip_answered=False),
                                 "the titles still count as having articles for the stages after")

    def test_a_changed_article_is_bought_again_and_only_it(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(classify.subprocess, "run", in_process):
            ledger_path = os.path.join(root, "paid-state.json.gz")
            first = self.out_dir(root, "first")
            ledger = paid.Ledger(ledger_path)
            ledger.restore(first)
            classify.run(first)
            ledger.keep(first)
            ledger.save()
            StubTypeSafe.requests = []
            second = self.out_dir(root, "second")
            moved = dict(article(3), text=article(3)["text"].replace("A story happens.", "A story unfolds."))
            with open(second.path(artifacts.ARTICLES), "w", encoding="utf-8") as fh:
                fh.writelines(json.dumps(record) + "\n" for record in (article(1), article(2), moved))
            paid.Ledger(ledger_path).restore(second)
            classify.run(second)
        self.assertEqual([state["requestedTarget"]["tmdbId"] for state, _ in StubTypeSafe.requests], [3])

    def test_fan_picks_are_not_bought_again(self):
        calls, kept = [], {}

        def generate(title, key, ask=0):
            calls.append(key)
            return {"key": key, "picks": [], "costUSD": 0.003}, None, True
        with tempfile.TemporaryDirectory() as root:
            first = os.path.join(root, "first")
            os.makedirs(first)
            corpus, articles, franchises, existing = fan_tests.Daily().fixture(first, {"movie:2": []})
            os.remove(franchises)
            with self.assertRaises(FileNotFoundError):  # killed after the answers, before the merge
                fan_picks.daily_update(corpus, articles, franchises, existing, existing, ["movie:1"], workers=1,
                                       generate=generate, follows={}, kept=kept)
            second = os.path.join(root, "second")
            os.makedirs(second)
            corpus, articles, franchises, existing = fan_tests.Daily().fixture(second, {"movie:2": []})
            result = fan_picks.daily_update(corpus, articles, franchises, existing, existing, ["movie:1"],
                                            workers=1, generate=generate, follows={}, kept=kept)
        self.assertEqual(calls, ["movie:1"])
        self.assertEqual((result["resumed"], result["generated"]), (1, 0))

    def test_premise_tags_are_not_bought_again(self):
        kept = {}
        case = premise_tests.PremiseGeneration()
        case.enterContext = self.enterContext
        first, second = case.phase(), case.phase()
        wire = premise_tests.Wire({"movie:7": TAGS, "movie:8": TAGS})
        with mock.patch("urllib.request.urlopen", wire), \
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "o", "ANTHROPIC_API_KEY": "a"}):
            premise_daily.generate(first, 1.0, kept=kept)
            self.assertEqual(len(wire.sent), 1)
            result = premise_daily.generate(second, 1.0, kept=kept)
        self.assertEqual(len(wire.sent), 1, "nothing asked the second time")
        self.assertEqual((result["resumed"], result["generated"]), (2, 0))
        with open(os.path.join(second, "out", "batch-0000.json"), encoding="utf-8") as fh:
            self.assertEqual({row["key"] for row in json.load(fh)}, {"movie:7", "movie:8"})


if __name__ == "__main__":
    unittest.main()
