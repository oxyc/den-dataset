#!/usr/bin/env python3
"""The critique stage, and the classify stage's change-set mode it pairs with.

Both passes buy, so the provider is `run_delta_test`'s stub and each pass's `main` runs in this process on
the command line the stage built — the stage's subprocess is replaced by exactly that. What is under test is
what the stages hand the passes: which dump, which shards, which flag; and that what they buy together is a
pair the corpus join accepts over the shards it supersedes.
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import pipeline

from . import artifacts, classify, consolidate_corpus, critique, run_delta
from . import run_combined as rc
from .contract import Context, StageError, bind
from .run_delta_test import StubTypeSafe, article


def change_set(out, keys, baseline=True):
    directory = os.path.join(out, "changes")
    os.makedirs(directory, exist_ok=True)
    for name in ("keys", "withdrawn", "items"):
        with open(os.path.join(directory, f"{name}.txt"), "w", encoding="utf-8") as fh:
            fh.writelines(f"{key}\n" for key in (keys if name == "keys" else ()))
    with open(os.path.join(directory, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump({"baseline": {"datasetVersion": "live", "maxBatchId": 1} if baseline else None,
                   "revisit": None}, fh)


def in_process(command, **_kwargs):
    """`subprocess.run` for a pass: its `main` on the stage's arguments, with the provider stubbed."""
    main = {classify.SCRIPT: rc.main, critique.SCRIPT: run_delta.main}[command[1]]
    with mock.patch.object(rc, "TypeSafe", StubTypeSafe), contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        try:
            code = main(command[2:]) or 0
        except SystemExit as stopped:
            code = stopped.code if isinstance(stopped.code, int) else 1
    return subprocess.CompletedProcess(command, code)


class ChangeSet(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name
        self.lines = [json.dumps(article(i)) + "\n" for i in (1, 2, 3)]
        with open(os.path.join(self.out, artifacts.ARTICLES.filename), "w", encoding="utf-8") as fh:
            fh.writelines(self.lines)
        os.makedirs(os.path.join(self.out, artifacts.ENRICHED.filename))
        StubTypeSafe.requests = []
        for module in (classify, critique):
            patch = mock.patch.object(module.subprocess, "run", in_process)
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, **kwargs):
        return Context(out_dir=self.out, **dict({"spend": True}, **kwargs))

    def rows(self, path):
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_the_passes_buy_the_change_sets_titles_into_shards_of_their_own(self):
        change_set(self.out, ["movie:2", "movie:9"])
        dump, digest = classify.changed_articles(self.ctx())
        with open(dump, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.lines[1], "the shared dump's bytes, for the listed title with one")
        shard = classify.run(self.ctx())
        self.assertEqual(os.path.basename(shard), f"combined-v1-r2-{digest}.jsonl")
        delta = critique.run(self.ctx())
        self.assertEqual(os.path.basename(delta), f"delta-v2-{digest}.jsonl")
        for path in (shard, delta):
            self.assertEqual([(r["mediaType"], r["tmdbId"]) for r in self.rows(path)], [("movie", 2)])
            self.assertTrue(os.path.exists(path + ".manifest.json"))
        # The corpus join's globs find both, and their manifests are the ones the audit reads.
        self.assertIn(shard, self.ctx().paths(artifacts.COMBINED))
        self.assertIn(delta, self.ctx().paths(artifacts.DELTA))

    def test_a_second_start_resumes_and_buys_nothing(self):
        change_set(self.out, ["movie:2"])
        classify.run(self.ctx())
        critique.run(self.ctx())
        StubTypeSafe.requests = []
        classify.run(self.ctx())
        critique.run(self.ctx())
        self.assertEqual(StubTypeSafe.requests, [])

    def test_the_pair_supersedes_the_older_shards_in_the_join(self):
        """Bought first over the whole dump, then over a change set whose article moved: the join takes the
        change set's pair for that title and refuses nothing — each kept critique read its classify's article."""
        classify.run(self.ctx())
        critique.run(self.ctx())
        moved = dict(article(2), text=article(2)["text"].replace("A story happens.", "A story unfolds."))
        self.lines[1] = json.dumps(moved) + "\n"
        with open(os.path.join(self.out, artifacts.ARTICLES.filename), "w", encoding="utf-8") as fh:
            fh.writelines(self.lines)
        change_set(self.out, ["movie:2"])
        shard, delta = classify.run(self.ctx()), critique.run(self.ctx())
        combined = self.ctx().paths(artifacts.COMBINED)
        kept, _report, _ = consolidate_corpus.latest(combined, "combined", {}, keep=consolidate_corpus.pass_row)
        kept_delta, _report, _ = consolidate_corpus.latest(self.ctx().paths(artifacts.DELTA), "delta", {},
                                                           keep=consolidate_corpus.pass_row)
        by_new = {f"{r['mediaType']}:{r['tmdbId']}": r["articleSha256"] for r in self.rows(shard)}
        self.assertEqual(kept["movie:2"]["articleSha256"], by_new["movie:2"])
        self.assertEqual(kept_delta["movie:2"]["articleSha256"], by_new["movie:2"])
        self.assertEqual({r["tmdbId"] for r in self.rows(delta)}, {2})

    def test_without_spend_the_pass_keeps_its_own_refusal(self):
        change_set(self.out, ["movie:2"])
        classify.run(self.ctx())
        StubTypeSafe.requests = []
        with self.assertRaisesRegex(StageError, "not given --spend"):
            critique.run(self.ctx(spend=False))
        self.assertEqual(StubTypeSafe.requests, [])

    def test_a_change_set_with_no_article_buys_nothing(self):
        change_set(self.out, ["movie:9"])
        self.assertIn("nothing to classify", classify.run(self.ctx()))
        self.assertIn("nothing to critique", critique.run(self.ctx()))
        self.assertEqual(StubTypeSafe.requests, [])
        self.assertEqual(self.ctx().paths(artifacts.COMBINED), ())

    def test_a_first_generation_classifies_the_whole_dump(self):
        change_set(self.out, ["movie:2"], baseline=False)
        self.assertEqual(os.path.basename(classify.run(self.ctx())), "combined-v1-r2.jsonl")
        self.assertEqual(len(self.rows(os.path.join(self.out, "combined-v1-r2.jsonl"))), 3)

    def test_the_critique_needs_what_the_classify_stage_bought(self):
        change_set(self.out, ["movie:2"])
        with self.assertRaisesRegex(StageError, "Run ./den stage classify --spend first"):
            critique.run(self.ctx())
        self.assertIn("not classified yet", critique.run(self.ctx(plan=True)))


class CommandLine(unittest.TestCase):
    def test_a_whole_run_hands_over_every_classify_shard_and_buys(self):
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, artifacts.ARTICLES.filename), "w", encoding="utf-8") as fh:
                fh.write(json.dumps(article(1)) + "\n")
            os.makedirs(os.path.join(out, "enriched"))
            for name in ("combined-v1-r2.jsonl", "combined-v1-r2-quarantine.jsonl"):
                open(os.path.join(out, name), "w").close()
            command = critique.argv(Context(out_dir=out, spend=True))
            self.assertEqual(command[2:], [
                "--articles", os.path.join(out, "articles.jsonl"), "--enriched-dir", os.path.join(out, "enriched"),
                "--combined", os.path.join(out, "combined-v1-r2-quarantine.jsonl"),
                "--combined", os.path.join(out, "combined-v1-r2.jsonl"),
                "--out", os.path.join(out, "delta-v2.jsonl"), "--spend"])
            self.assertEqual(critique.argv(Context(out_dir=out, plan=True))[-1], "--plan")
            self.assertNotIn("--spend", critique.argv(Context(out_dir=out)), "only a run given --spend buys")


class Declaration(unittest.TestCase):
    def test_it_runs_straight_after_the_classify_stage_and_buys(self):
        order = list(pipeline.STAGES)
        self.assertEqual(order.index("critique"), order.index("classify") + 1)
        self.assertTrue(critique.SPENDS)
        self.assertEqual(pipeline.producers()["delta"], (critique.PRODUCER, critique.HOW, True))
        self.assertIn(artifacts.CHANGED_ARTICLES, [bind(e).artifact for e in classify.OUTPUTS])
        self.assertIn(artifacts.CHANGED_ARTICLES, [bind(e).artifact for e in critique.INPUTS])

    def test_the_delta_shard_names_match_the_joins_glob_and_not_their_manifests(self):
        ctx = Context(out_dir="/o")
        self.assertEqual(classify.named(ctx, artifacts.DELTA, "abc"), "/o/delta-v2-abc.jsonl")
        self.assertEqual(classify.named(ctx, artifacts.CHANGED_ARTICLES, "abc"), "/o/classify/articles-abc.jsonl")


if __name__ == "__main__":
    unittest.main()
