#!/usr/bin/env python3
"""The paid-answers ledger (`pipeline/paid.py`, #187 item 1): a run killed after its paid calls returned and
before anything was published, then run again in a fresh out-dir with only the ledger, makes no paid call
for those titles — classify (with its critique), fan picks and premise tags alike."""
import gzip
import json
import os
import subprocess
import sys
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
#: The real `subprocess.run`, for the premise worklist script while classify's passes run in process.
RUN = subprocess.run


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

    def test_a_kept_shard_carries_no_tmdb_title_or_year_and_is_kept_again_when_resumed_in_place(self):
        """The release is public: a row's `title` and `year` can be TMDB's. A shard laid back and then added to
        in place has the ledger's name and new rows, so it is compared by content, not by name."""
        with tempfile.TemporaryDirectory() as root:
            out, path = os.path.join(root, "out"), os.path.join(root, "paid-state.json.gz")
            os.makedirs(out)
            ledger = paid.Ledger(path)
            ledger.data["shards"]["combined-v1-r2-abc.jsonl"] = {
                "rows": '{"mediaType":"movie","tmdbId":1}\n', "manifest": "{}"}
            ledger.restore(Context(out_dir=out))
            with open(os.path.join(out, "combined-v1-r2-abc.jsonl"), "a") as fh:
                fh.write('{"mediaType": "movie", "tmdbId": 2, "title": "Heat", "year": 1995, "answers": {}}\n')
            ledger.keep(Context(out_dir=out))
            ledger.save()
            with gzip.open(path, "rb") as fh:
                body = fh.read()
        rows = ledger.data["shards"]["combined-v1-r2-abc.jsonl"]["rows"].splitlines()
        self.assertEqual([json.loads(row) for row in rows],
                         [{"mediaType": "movie", "tmdbId": 1}, {"mediaType": "movie", "tmdbId": 2, "answers": {}}])
        self.assertNotIn(b"Heat", body)
        self.assertNotIn(b"1995", body)

    def test_a_shard_already_in_the_out_dir_that_is_not_the_ledgers_is_never_taken_in(self):
        with tempfile.TemporaryDirectory() as root:
            out = os.path.join(root, "out")
            os.makedirs(out)
            for suffix, body in (("", '{"row": 1}\n'), (".manifest.json", "{}")):
                with open(os.path.join(out, "combined-v1-r2-operator.jsonl" + suffix), "w") as fh:
                    fh.write(body)
            ledger = paid.Ledger(os.path.join(root, "paid-state.json.gz"))
            ledger.restore(Context(out_dir=out))
            with open(os.path.join(out, "combined-v1-r2-operator.jsonl"), "a") as fh:
                fh.write('{"row": 2}\n')
            ledger.keep(Context(out_dir=out))
        self.assertEqual(ledger.data["shards"], {})

    def test_a_publish_prunes_what_it_carries_and_keeps_what_still_waits(self):
        ledger = paid.Ledger(None)
        ledger.data["shards"] = {
            "combined-v1-r2-a.jsonl": {"rows": '{"mediaType":"movie","tmdbId":1}\n', "manifest": "{}"},
            "combined-v1-r2-b.jsonl": {"rows": '{"mediaType":"movie","tmdbId":2}\n', "manifest": "{}"},
            "combined-v1-r2-c.jsonl": {"rows": '{"mediaType":"movie","tmdbId":3}\n', "manifest": "{}"},
            "genres-moods-answers-x.jsonl": {"rows": '{"key":"movie:1"}\n', "manifest": "{}"}}
        ledger.data["files"] = {"franchise-decisions.json": "{}"}
        ledger.answers("fan_picks")["movie:5"] = {"accepted": True}
        ledger.answers("premise_tags").update({
            "movie:1": {"ask": "a", "tags": TAGS}, "movie:4": {"ask": "b", "tags": TAGS},
            "movie:6": {"ask": "c", "untaggable": "refused"}})
        ledger.data["steps"] = {"premise_tags": {"waiting": ["movie:1"]},
                                "fan_picks": {"reaskAnswers": {"movie:5": {}}, "reaskPlan": {"movie:5": []}}}
        ledger.data["batches"] = [{"chunks": {"c00000": ["movie:2"]}, "projectedUSD": 0.25}]
        ledger.data["intents"] = [{"chunks": {"c00000": ["movie:9"]}, "projectedUSD": 0.5}]
        dropped = ledger.prune()
        self.assertEqual(sorted(ledger.data["shards"]), ["combined-v1-r2-a.jsonl", "combined-v1-r2-b.jsonl"],
                         "a waiting title's and a pending job's classify rows: the bundle has no sections")
        self.assertEqual((ledger.data["files"], ledger.answers("fan_picks")), ({}, {}))
        self.assertEqual(sorted(ledger.answers("premise_tags")), ["movie:1", "movie:6"])
        self.assertEqual(ledger.data["steps"]["fan_picks"], {"reaskPlan": {"movie:5": []}})
        self.assertEqual((dropped["shards"], dropped["premise_tags"]), (2, 1))
        self.assertEqual(ledger.pending_usd(), 0.75, "an unconfirmed submit is committed too")

    def test_the_prune_command_rewrites_the_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "paid-state.json.gz")
            ledger = paid.Ledger(path)
            ledger.data["files"] = {"franchise-decisions.json": "{}"}
            ledger.save()
            with mock.patch("sys.stdout"):
                self.assertEqual(paid.main(["prune", path]), 0)
            self.assertEqual(paid.Ledger(path).data["files"], {})

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

    def out_dir(self, root, name, ids=(1, 2, 3)):
        out = os.path.join(root, name)
        os.makedirs(os.path.join(out, artifacts.ENRICHED.filename))
        with open(os.path.join(out, artifacts.ARTICLES.filename), "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(article(i)) + "\n" for i in ids)
        change_set(out, [f"movie:{i}" for i in ids[1:]])
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

    def test_a_title_waiting_for_premise_tags_is_classified_again_when_its_article_changed(self):
        """Classified on day one, its article edited before the weekly premise ask: the worklist would turn it
        away as `articleChanged` for good. Listed in `changes/reclassify.txt`, classify asks it again on the
        new article — and only it — and the worklist then takes it."""
        ids = (990000001, 990000002, 990000003)  # titles no committed premise-tags file holds
        with tempfile.TemporaryDirectory() as root, mock.patch.object(classify.subprocess, "run", in_process):
            ledger_path = os.path.join(root, "paid-state.json.gz")
            first = self.out_dir(root, "first", ids)
            ledger = paid.Ledger(ledger_path)
            ledger.restore(first)
            classify.run(first)
            ledger.keep(first)
            ledger.save()
            StubTypeSafe.requests = []
            second = self.out_dir(root, "second", ids)
            change_set(second.out_dir, [], new=[])
            with open(os.path.join(second.out_dir, "changes", "reclassify.txt"), "w") as fh:
                fh.write("movie:990000002\nmovie:990000003\n")
            moved = dict(article(ids[2]), text=article(ids[2])["text"].replace("A story happens.", "A story unfolds."))
            with open(second.path(artifacts.ARTICLES), "w", encoding="utf-8") as fh:
                fh.writelines(json.dumps(record) + "\n" for record in (article(ids[0]), article(ids[1]), moved))
            paid.Ledger(ledger_path).restore(second)
            before = self.worklist(second, "before", ids)
            self.assertEqual(classify.reclassified(second), {"movie:990000003"}, "the other article did not change")
            classify.run(second)
            self.assertEqual([state["requestedTarget"]["tmdbId"] for state, _ in StubTypeSafe.requests], [ids[2]])
            self.assertEqual(classify.reclassified(second), set(), "classified on today's article")
            after = self.worklist(second, "after", ids)
        self.assertEqual(before.get("movie:990000003", {}).get("reason"), "articleChanged")
        self.assertEqual(after, {}, "both titles are in the worklist, so the next weekly submit asks them")

    @staticmethod
    def worklist(ctx, name, ids):
        """The premise worklist's turned-away titles, built from `ctx`'s shards."""
        plan = os.path.join(ctx.out_dir, f"premise-plan-{name}.json")
        with open(plan, "w") as fh:
            json.dump({"baseline": {"datasetVersion": "live"}, "added": [f"movie:{i}" for i in ids[1:]],
                       "changed": {}}, fh)
        tags = os.path.join(ctx.out_dir, "no-tags.json")
        with open(tags, "w") as fh:
            json.dump({"tags": {}}, fh)
        out = os.path.join(ctx.out_dir, f"premise-{name}")
        command = [sys.executable, os.path.join(premise_daily.REPO, "pipeline", "build_premise_worklist.py"),
                   "--articles", ctx.path(artifacts.ARTICLES), "--changes", plan, "--token-ceiling", "100000",
                   "--existing-tags", tags, "--out-dir", out]
        for path in ctx.paths(artifacts.COMBINED):
            command += ["--combined", path]
        RUN(command, check=True, capture_output=True)
        with open(os.path.join(out, "gen", "manifest.json"), encoding="utf-8") as fh:
            return json.load(fh)["skippedKeys"]

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
