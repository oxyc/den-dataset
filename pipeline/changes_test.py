#!/usr/bin/env python3
"""The change set (`pipeline/changes.py`): what moved since the live dataset, read off the enriched batches.

Two halves. The rules on batches written here, one title per case. Then the stage over the fixture corpus in
`pipeline/fixture-corpus/`, offline, with the same stand-in upstreams `den_run_test.py` runs the whole
pipeline against: the fetch stage enriches, a live manifest is stamped, an article is edited upstream and
`fetch --refresh` re-reads it, and the plan has to name exactly what that did.
"""
import contextlib
import datetime
import io
import itertools
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import pipeline  # noqa: E402
from lib import http  # noqa: E402
from pipeline import artifacts, changes, enrich, refresh  # noqa: E402
from pipeline.contract import Context, StageError  # noqa: E402

import den_run_test as fixture  # noqa: E402  — the offline upstreams, not its test cases

DAY = datetime.datetime(2026, 9, 24, 3, 0, tzinfo=datetime.timezone.utc)


def row(tmdb_id, media="movie", plot="A keeper finds a ledger.", article="The Ledger", language="en",
        revision=1, item="Q1", **extra):
    grounded = plot is not None
    out = {"tmdbId": tmdb_id, "mediaType": media, "hasWikiPlot": grounded, "overview": plot or "",
           "wikidataItem": item}
    if grounded:
        out.update(plotArticle=article, plotLanguage=language, plotRevId=revision)
    out.update(extra)
    return {k: v for k, v in out.items() if v is not None}


class Batches:
    """An out-dir whose enriched batches are written by the enrichment's own encoder, numbered as it numbers."""

    def __init__(self, test):
        self.out = tempfile.mkdtemp(prefix="changes-")
        test.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        os.makedirs(os.path.join(self.out, "enriched"))
        self.number = 0

    def add(self, *rows):
        self.number += 1
        with open(enrich.batch_path(self.out, self.number), "w", encoding="utf-8") as fh:
            fh.write(enrich.swift_json(list(rows)))
        return self.number

    def publish(self, through, version="live"):
        os.makedirs(os.path.join(self.out, "published"), exist_ok=True)
        with open(os.path.join(self.out, artifacts.PUBLISHED_META.filename), "w", encoding="utf-8") as fh:
            json.dump({"datasetVersion": version, "maxBatchId": through}, fh)

    def plan(self, **context):
        with contextlib.redirect_stdout(io.StringIO()):
            changes.run(Context(out_dir=self.out, **context), now=DAY)
        return self.read("plan.json")

    def read(self, name):
        with open(os.path.join(self.out, "changes", name), encoding="utf-8") as fh:
            return json.load(fh) if name.endswith(".json") else fh.read().split()


class Rules(unittest.TestCase):
    def test_with_no_live_manifest_every_title_is_new(self):
        out = Batches(self)
        out.add(row(2), row(1, media="tv"), row(1))
        plan = out.plan()
        self.assertIsNone(plan["baseline"])
        self.assertEqual(plan["added"], ["movie:1", "movie:2", "tv:1"], "movies first, then by number")
        self.assertEqual(out.read("keys.txt"), ["movie:1", "movie:2", "tv:1"])

    def test_a_title_in_a_batch_after_the_live_one_is_added(self):
        out = Batches(self)
        out.publish(out.add(row(1)))
        out.add(row(2))
        plan = out.plan()
        self.assertEqual(plan["baseline"], {"datasetVersion": "live", "maxBatchId": 1})
        self.assertEqual((plan["added"], plan["changed"], plan["counts"]["unchanged"]), (["movie:2"], {}, 1))

    def test_each_kind_of_change_is_named(self):
        out = Batches(self)
        out.publish(out.add(row(1), row(2), row(3), row(4, plot=None), row(5), row(6), row(7)))
        out.add(row(1, plot="The keeper burns the ledger.", revision=2),           # the text
                row(2, article="The Ledger (film)", revision=2),                   # another article, same text
                row(3, language="fr", article="Le Registre"),                      # another language
                row(4),                                                            # a plot where none was
                row(5, item="Q2"),                                                 # another Wikidata item
                row(6, revision=9),                                                # an edit outside the plot
                row(7, plot=None))                                                 # the plot is gone
        plan = out.plan()
        self.assertEqual(plan["changed"], {"movie:1": ["plot"], "movie:2": ["article"],
                                           "movie:3": ["article"], "movie:4": ["gainedPlot"],
                                           "movie:5": ["item"]})
        self.assertEqual(plan["withdrawn"], {"movie:7": "lostPlot"})
        self.assertEqual((plan["counts"]["revised"], plan["counts"]["unchanged"]), (1, 0))
        self.assertEqual(out.read("keys.txt"), [f"movie:{n}" for n in (1, 2, 3, 4, 5)])
        self.assertEqual(out.read("withdrawn.txt"), ["movie:7"])

    def test_an_unknown_revision_or_item_is_not_a_change(self):
        """The Enterprise path records no revision, and older batches no item. An unknown proves nothing."""
        out = Batches(self)
        out.publish(out.add(row(1, revision=None, item=None), row(2, revision=3)))
        out.add(row(1, revision=7, item="Q1"), row(2, revision=None))
        plan = out.plan()
        self.assertEqual((plan["changed"], plan["counts"]["revised"], plan["counts"]["unchanged"]), ({}, 0, 2))

    def test_the_newest_record_wins_on_both_sides_of_the_boundary(self):
        out = Batches(self)
        out.add(row(1, plot="first"))
        out.publish(out.add(row(1, plot="second")))
        out.add(row(1, plot="third"))
        out.add(row(1, plot="second"))
        self.assertEqual(out.plan()["changed"], {}, "a title edited and edited back is what was published")

    def test_a_plotless_title_never_has_its_text_read(self):
        """An old batch's `overview` is TMDB's prose when `hasWikiPlot` is false; no digest is taken of it."""
        prose = row(1, plot=None)
        prose["overview"] = "TMDB's own words about the film."
        self.assertIsNone(changes.fingerprint(prose)["text"])
        out = Batches(self)
        out.publish(out.add(prose))
        again = dict(prose, overview="Different words from TMDB.")
        out.add(again)
        self.assertEqual(out.plan()["changed"], {})

    def test_the_plan_names_keys_and_carries_no_text(self):
        out = Batches(self)
        out.publish(out.add(row(1, plot="A secret plot sentence.")))
        out.add(row(1, plot="Another secret plot sentence."))
        out.plan()
        with open(os.path.join(out.out, "changes", "plan.json"), encoding="utf-8") as fh:
            self.assertNotIn("secret plot sentence", fh.read())

    def test_a_manifest_that_names_no_batch_is_refused(self):
        out = Batches(self)
        out.add(row(1))
        os.makedirs(os.path.join(out.out, "published"))
        with open(os.path.join(out.out, artifacts.PUBLISHED_META.filename), "w", encoding="utf-8") as fh:
            json.dump({"datasetVersion": "live"}, fh)
        with self.assertRaisesRegex(StageError, "records no maxBatchId"):
            out.plan()

    def test_a_manifest_from_another_out_dir_is_refused(self):
        out = Batches(self)
        out.add(row(1))
        out.publish(9)
        with self.assertRaisesRegex(StageError, "not the out-dir that built it"):
            out.plan()

    def test_an_empty_enrichment_is_refused(self):
        out = Batches(self)
        with self.assertRaisesRegex(StageError, "holds no batch"):
            out.plan()


class WeeklySlice(unittest.TestCase):
    KEYS = [f"movie:{n}" for n in range(1, 400)] + [f"tv:{n}" for n in range(1, 400)]

    def test_every_title_is_in_exactly_one_week_of_a_cycle(self):
        weeks = 4
        seen = [key for w in range(weeks) for key in self.KEYS if changes.in_slice(key, weeks, w)]
        self.assertEqual(sorted(seen), sorted(self.KEYS))
        sizes = [sum(changes.in_slice(k, weeks, w) for k in self.KEYS) for w in range(weeks)]
        self.assertTrue(all(len(self.KEYS) / weeks * 0.7 < size < len(self.KEYS) / weeks * 1.3 for size in sizes),
                        sizes)

    def test_the_week_rolls_over_on_monday_and_not_at_new_year(self):
        monday = datetime.date(2026, 9, 21)
        self.assertEqual(changes.week(monday), changes.week(monday + datetime.timedelta(days=6)))
        self.assertEqual(changes.week(monday) + 1, changes.week(monday + datetime.timedelta(days=7)))
        self.assertEqual(changes.week(datetime.date(2026, 1, 1)) + 1,
                         changes.week(datetime.date(2026, 1, 8)))

    def test_the_slice_leaves_out_what_is_already_listed(self):
        out = Batches(self)
        out.publish(out.add(*(row(n) for n in range(1, 41))))
        out.add(row(1, plot="rewritten"))
        slices = []
        for days in (0, 7):
            with contextlib.redirect_stdout(io.StringIO()):
                changes.run(Context(out_dir=out.out, revisit_weeks=2), now=DAY + datetime.timedelta(days=days))
            plan = out.read("plan.json")
            self.assertEqual(plan["revisit"]["weeks"], 2)
            slices.append(set(out.read("revisit.txt")))
        self.assertFalse(slices[0] & slices[1])
        self.assertEqual(slices[0] | slices[1], {f"movie:{n}" for n in range(2, 41)})
        out.plan()
        self.assertFalse(os.path.exists(os.path.join(out.out, "changes", "revisit.txt")),
                         "a run without a slice leaves no slice from the week before")

    def test_a_cycle_shorter_than_a_week_is_refused(self):
        out = Batches(self)
        out.add(row(1))
        with self.assertRaisesRegex(StageError, "not a cycle length"):
            out.plan(revisit_weeks=-1)


class Declaration(unittest.TestCase):
    def test_it_reads_the_batches_the_fetch_stage_writes_and_runs_before_anything_reads_them(self):
        order = list(pipeline.STAGES)
        self.assertEqual(order.index("changes"), order.index("fetch") + 1)
        self.assertEqual(pipeline.producers()["enriched"][0], pipeline.stage("fetch").PRODUCER)
        self.assertEqual(pipeline.producers()["changes"], (changes.PRODUCER, changes.HOW, False))

    def test_the_live_manifest_is_optional_and_not_the_out_dirs_own(self):
        self.assertFalse(artifacts.PUBLISHED_META.required)
        self.assertNotEqual(artifacts.PUBLISHED_META.filename, artifacts.MANIFEST.filename,
                            "finalize rewrites the out-dir's manifest during the run it is the baseline of")


class OnTheFixtureCorpus(unittest.TestCase):
    """The stage after real fetches, over the fixture's upstreams, offline."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="changes-fixture-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)
        for name in ("movie_ids.json", "tv_series_ids.json"):
            shutil.copy(os.path.join(fixture.SEEDS, "unproduced", name), self.out)
        self.upstreams = fixture.Upstreams(http.request)
        env = {k: v for k, v in os.environ.items() if not k.startswith("WIKIMEDIA_ENTERPRISE")}
        env["DEN_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        runs = itertools.count(1)
        for patch in (mock.patch.dict(os.environ, env, clear=True),
                      mock.patch.object(http, "request", self.upstreams.request),
                      mock.patch.object(socket.socket, "connect", fixture.offline_connect(socket.socket.connect)),
                      # A refresh names its directory by the second it started; two in one test are two runs.
                      mock.patch.object(refresh, "stamp", lambda _now=None: f"run-{next(runs)}")):
            patch.start()
            self.addCleanup(patch.stop)
        self.den_module = fixture.load_den()

    def den(self, *argv):
        said = io.StringIO()
        with contextlib.redirect_stderr(said), contextlib.redirect_stdout(said):
            code = self.den_module.main([*argv, "--out-dir", self.out])
        self.assertEqual(code, 0, said.getvalue()[-3000:])
        return said.getvalue()

    def plan(self):
        self.den("stage", "changes")
        with open(os.path.join(self.out, "changes", "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def publish_through_now(self):
        highest = enrich.batches(os.path.join(self.out, "enriched"))[-1][0]
        os.makedirs(os.path.join(self.out, "published"), exist_ok=True)
        with open(os.path.join(self.out, artifacts.PUBLISHED_META.filename), "w", encoding="utf-8") as fh:
            json.dump({"datasetVersion": "fixture-live", "maxBatchId": highest}, fh)

    def edit(self, title, wikitext=None, language="en"):
        page = self.upstreams.pages[language][title]
        page["revid"] += 1
        if wikitext is not None:
            page["wikitext"] = wikitext

    def test_a_day_of_changes_on_the_fixture(self):
        self.den("stage", "worklist", "--mode", "export")
        self.den("stage", "fetch", "--media", "movie")
        first = self.plan()
        self.assertIsNone(first["baseline"])
        self.assertEqual(first["added"], ["movie:900001", "movie:900002", "movie:900003"],
                         "the title below every floor is not a title")

        # The movies go live. The next day the series are drained for the first time, one article's plot is
        # rewritten, another article is edited outside its plot, and a third loses its plot section.
        self.publish_through_now()
        self.assertEqual(self.plan()["counts"]["unchanged"], 3, "nothing moved since the live dataset")
        ledger = self.upstreams.pages["en"]["The Lighthouse Ledger"]["wikitext"]
        self.edit("The Lighthouse Ledger", ledger.replace("rows to the mainland", "sails to the mainland"))
        self.edit("Le Jardin d'hiver", language="fr")
        self.den("stage", "fetch", "--refresh")
        plan = self.plan()
        self.assertEqual(plan["baseline"]["datasetVersion"], "fixture-live")
        self.assertEqual(plan["added"], ["tv:900001", "tv:900005"])
        self.assertEqual(plan["changed"], {"movie:900001": ["plot"]})
        self.assertEqual((plan["withdrawn"], plan["counts"]["revised"]), ({}, 1))
        with open(os.path.join(self.out, "changes", "keys.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().split(), ["movie:900001", "tv:900001", "tv:900005"])

        # Published again; then the film's article loses its plot section altogether.
        self.publish_through_now()
        self.edit("The Lighthouse Ledger", ledger.split("== Plot ==")[0] + "== Production ==\nSix weeks.\n")
        self.den("stage", "fetch", "--refresh")
        plan = self.plan()
        self.assertEqual((plan["added"], plan["changed"]), ([], {}))
        self.assertEqual(plan["withdrawn"], {"movie:900001": "lostPlot"})


if __name__ == "__main__":
    unittest.main()
