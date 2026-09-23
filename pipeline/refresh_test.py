#!/usr/bin/env python3
"""The refresh — which titles count as changed, what a re-fetch writes, and what it hands downstream.

Wikipedia is stubbed at three seams: the bulk revision answer (`ask`), the Wikidata candidates, and
`lib/plot.plot`. The cache is a real one in a temp dir, because the backfill's whole claim is about which
body on disk a stored text came from.
"""
import datetime
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from lib import cache as caching, http

from . import artifacts, enrich, fetch, refresh
from .contract import Context, StageError

NOW = datetime.datetime(2026, 9, 22, 12, 0, tzinfo=datetime.timezone.utc)


def grounded(tmdb_id, text, revision=100, article=None, language="en", media="movie", **extra):
    record = {"tmdbId": tmdb_id, "mediaType": media, "genreIDs": [18], "originCountry": ["US"],
              "originalLanguage": "en", "voteCount": 900, "hasWikiPlot": True, "overview": text,
              "plotArticle": article or f"Film {tmdb_id}", "plotLanguage": language,
              "plotArticleRole": "own", "plotArticleRedirected": False, "plotSections": ["Plot"],
              "createdBy": [], "runtimeMinutes": 100}
    if revision is not None:
        record["plotRevId"] = revision
    return dict(record, **extra)


def found(text, article, revision, language="en"):
    return {"text": text, "revId": revision, "resolvedArticle": article, "sections": ["Plot"],
            "language": language, "source": "action-api"}


PLOT = "A crew wakes to a distress call and brings something back aboard. " * 3


class Refresh(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = directory.name
        self.cache = caching.ResponseCache("wiki", os.path.join(self.out, ".cache"), 3600)
        self.current, self.asked = {}, []
        self.plots, self.plot_calls = {}, []
        self.candidate_calls = []
        for target, name, stub in ((enrich.plot, "plot", self.plot_stub),
                                   (enrich, "candidates", self.candidates_stub)):
            patch = mock.patch.object(target, name, stub)
            patch.start()
            self.addCleanup(patch.stop)
        for stream in ("sys.stdout", "sys.stderr"):
            patch = mock.patch(stream, io.StringIO())
            patch.start()
            self.addCleanup(patch.stop)

    # -- the seams ----------------------------------------------------------------------------------------

    def ask(self, titles, language="en"):
        self.asked.append((language, list(titles)))
        return {title: self.current.get((language, title)) for title in titles}

    def candidates_stub(self, titles, cache, excluded):
        self.candidate_calls.append(([t["tmdbId"] for t in titles], excluded))
        return {enrich.key(t["mediaType"], t["tmdbId"]): {"article": f"Film {t['tmdbId']}"} for t in titles}

    def plot_stub(self, article, language="en", cache=None, token=None):
        self.plot_calls.append((article, language, cache, token))
        answer = self.plots.get(article)
        if isinstance(answer, Exception):
            raise answer
        return answer

    # -- helpers -----------------------------------------------------------------------------------------

    def batch(self, number, rows):
        os.makedirs(os.path.join(self.out, "enriched"), exist_ok=True)
        with open(enrich.batch_path(self.out, number), "w", encoding="utf-8") as fh:
            fh.write(enrich.swift_json(rows))

    def checkpoint(self, state):
        with open(enrich.checkpoint_path(self.out), "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def cached(self, article, text_wikitext, revision, language="en"):
        self.cache.write(self.cache.key(f"{language}.wikipedia.org/w/api.php",
                                        dict(refresh.wikipedia.PARSE_QUERY, page=article)),
                         json.dumps({"parse": {"title": article, "revid": revision,
                                               "wikitext": text_wikitext}}).encode())

    def refreshed(self, **kwargs):
        ctx = Context(out_dir=self.out, **kwargs)
        return refresh.run(ctx, cache=self.cache, ask=self.ask, now=NOW)

    def listing(self):
        return sorted(os.listdir(os.path.join(self.out, "enriched")))

    def latest(self):
        return refresh.latest(os.path.join(self.out, "enriched"))

    def lists(self):
        run_dir = os.path.join(self.out, "refresh", "20260922T120000Z")
        out = {}
        for name in ("changed", "plotless"):
            with open(os.path.join(run_dir, f"{name}.txt"), encoding="utf-8") as fh:
                out[name] = fh.read().split()
        return out


class Survey(Refresh):
    def test_only_a_moved_gone_or_unknown_revision_is_fetched(self):
        self.batch(1, [grounded(1, PLOT, 100), grounded(2, PLOT, 100), grounded(3, PLOT, 100),
                       grounded(4, PLOT, None),
                       {"tmdbId": 5, "mediaType": "movie", "hasWikiPlot": False, "overview": ""}])
        self.current = {("en", "Film 1"): 100, ("en", "Film 2"): 101, ("en", "Film 3"): None,
                        ("en", "Film 4"): 555}
        surveyed, _requests = refresh.survey(self.latest(), self.cache, self.ask)
        self.assertEqual({key: verdict for key, (verdict, _r, _b) in surveyed.items()},
                         {"movie:1": refresh.UNCHANGED, "movie:2": refresh.MOVED, "movie:3": refresh.GONE,
                          "movie:4": refresh.UNKNOWN},
                         "unknown counts as changed, and a plotless title has no article to ask about")

    def test_the_newest_batch_is_the_record_asked_about(self):
        self.batch(1, [grounded(1, PLOT, 90)])
        self.batch(2, [grounded(1, PLOT, 100)])
        self.current = {("en", "Film 1"): 100}
        surveyed, _requests = refresh.survey(self.latest(), self.cache, self.ask)
        self.assertEqual(surveyed["movie:1"][0], refresh.UNCHANGED)

    def test_fifty_articles_a_request_per_wikipedia(self):
        self.batch(1, [grounded(n, PLOT, 1) for n in range(1, 52)]
                   + [grounded(100 + n, PLOT, 1, language="de") for n in range(3)])
        _surveyed, requests = refresh.survey(self.latest(), self.cache, self.ask)
        self.assertEqual(requests, 3)
        self.assertEqual([(language, len(titles)) for language, titles in self.asked], [("de", 3), ("en", 51)])

    def test_a_revision_is_backfilled_only_from_a_cached_body_whose_plot_is_exactly_the_record(self):
        """The Enterprise path recorded no revision. The body on disk ties a text to one only when it yields
        that text exactly: the Enterprise and wikitext cleanings differ, so a near match proves nothing."""
        self.cached("Film 1", f"Lead.\n== Plot ==\n{PLOT}", 77)
        self.cached("Film 2", f"Lead.\n== Plot ==\n{PLOT} And more.", 78)
        self.batch(1, [grounded(1, PLOT.strip(), None), grounded(2, PLOT.strip(), None)])
        self.current = {("en", "Film 1"): 77, ("en", "Film 2"): 78}
        surveyed, _requests = refresh.survey(self.latest(), self.cache, self.ask)
        self.assertEqual(surveyed["movie:1"][0::2], (refresh.UNCHANGED, 77))
        self.assertEqual(surveyed["movie:2"][0::2], (refresh.UNKNOWN, None))

    def test_a_backfilled_revision_that_moved_since_is_fetched(self):
        self.cached("Film 1", f"Lead.\n== Plot ==\n{PLOT}", 77)
        self.batch(1, [grounded(1, PLOT.strip(), None)])
        self.current = {("en", "Film 1"): 80}
        surveyed, _requests = refresh.survey(self.latest(), self.cache, self.ask)
        self.assertEqual(surveyed["movie:1"][0::2], (refresh.MOVED, None))


class Plan(Refresh):
    def test_a_plan_counts_and_fetches_and_writes_nothing(self):
        self.batch(1, [grounded(1, PLOT, 100), grounded(2, PLOT, 100), grounded(3, PLOT, None)])
        self.current = {("en", "Film 1"): 100, ("en", "Film 2"): 200, ("en", "Film 3"): 5}
        before = self.listing()
        report = self.refreshed(plan=True)
        self.assertEqual((report["unchanged"], report["moved"], report["unknown"], report["toFetch"]),
                         (1, 1, 1, 2))
        self.assertEqual((self.plot_calls, self.candidate_calls), ([], []))
        self.assertEqual(self.listing(), before)
        self.assertFalse(os.path.exists(os.path.join(self.out, "refresh")))


class Run(Refresh):
    def setUp(self):
        super().setUp()
        self.checkpoint({"processed": ["movie:1", "movie:2", "movie:3", "movie:4", "movie:5"], "nextBatch": 2,
                         "totals": {}, "judgedBelow": {}})
        self.cached("Film 5", f"Lead.\n== Plot ==\n{PLOT}", 55)
        self.batch(1, [grounded(1, PLOT, 100, title="TMDB title", keywords=["legacy"]),
                       grounded(2, PLOT, 100), grounded(3, PLOT, 100), grounded(4, PLOT, 100),
                       grounded(5, PLOT.strip(), None, title="TMDB title", topCast=["Legacy Cast"]),
                       grounded(6, PLOT, 100, wikidataItem="Q1", wikidataCandidates=["Q1", "Q2"])])
        self.current = {("en", "Film 1"): 101, ("en", "Film 2"): 102, ("en", "Film 3"): 103,
                        ("en", "Film 4"): 104, ("en", "Film 5"): 55, ("en", "Film 6"): 106}
        self.plots = {"Film 1": found("Rewritten: " + PLOT, "Film 1", 101),
                      "Film 2": found(PLOT, "Film 2", 102),
                      "Film 3": None,
                      "Film 4": http.HTTPError(503, "x"),
                      "Film 6": found(PLOT, "Film 6", 106)}

    def test_what_changed_is_rewritten_in_a_new_batch_and_listed(self):
        report = self.refreshed()
        self.assertEqual(self.listing(), ["batch-1.json", "batch-2.json", "batch-3.json"])
        rows = self.latest()
        self.assertEqual((rows["movie:1"]["overview"], rows["movie:1"]["plotRevId"]), ("Rewritten: " + PLOT, 101))
        self.assertEqual(self.lists(), {"changed": ["movie:1"], "plotless": ["movie:3"]})
        self.assertEqual((report["changed"], report["revised"], report["plotless"], report["deferred"],
                          report["recorded"]), (1, 2, 1, 1, 1))

    def test_an_edit_that_left_the_plot_alone_takes_the_new_revision_and_is_in_no_list(self):
        self.refreshed()
        self.assertEqual(self.latest()["movie:2"]["plotRevId"], 102)
        self.assertNotIn("movie:2", sum(self.lists().values(), []))

    def test_a_title_that_lost_its_plot_carries_nothing_from_the_old_grounding(self):
        self.refreshed()
        row = self.latest()["movie:3"]
        self.assertFalse(row["hasWikiPlot"])
        for name in ("plotArticle", "plotRevId", "plotLanguage", "plotArticleRole"):
            self.assertNotIn(name, row)

    def test_a_failed_fetch_keeps_the_old_record_and_is_asked_again_next_time(self):
        self.refreshed()
        self.assertEqual(self.latest()["movie:4"]["plotRevId"], 100)
        surveyed, _requests = refresh.survey(self.latest(), self.cache, self.ask)
        self.assertEqual(surveyed["movie:4"][0], refresh.MOVED)

    def test_a_backfilled_revision_is_recorded_so_the_next_refresh_knows_it(self):
        self.refreshed()
        self.assertEqual(self.latest()["movie:5"]["plotRevId"], 55)

    def test_a_backfilled_record_sheds_the_tmdb_fields_no_reader_wants(self):
        """Only its revision is new, but it is written into a new batch, and an old row's TMDB title, cast,
        vote count and language are not copied forward into it (oxyc/den-dataset#53)."""
        self.refreshed()
        row = self.latest()["movie:5"]
        self.assertEqual({"title", "topCast", "voteCount", "originalLanguage"} & set(row), set())
        self.assertEqual((row["overview"], row["genreIDs"], row["originCountry"]), (PLOT.strip(), [18], ["US"]))

    def test_a_second_refresh_finds_nothing_left_but_the_failure(self):
        self.refreshed()
        surveyed, _requests = refresh.survey(self.latest(), self.cache, self.ask)
        self.assertEqual(refresh.counts(surveyed, 0)["toFetch"], 1)

    def test_every_wikipedia_read_goes_to_the_network_and_its_answer_replaces_the_cached_body(self):
        """The cache would answer with the revision the survey just found stale."""
        self.refreshed()
        caches = {cache for _a, _l, cache, _t in self.plot_calls}
        self.assertEqual(len(caches), 1)
        fresh = caches.pop()
        self.assertIsInstance(fresh, refresh.Fresh)
        key = self.cache.key("en.wikipedia.org/w/api.php", dict(refresh.wikipedia.PARSE_QUERY, page="Film 5"))
        self.assertIsNone(fresh.read(key))
        fresh.write("k" * 64, b"{}")
        self.assertEqual(self.cache.read("k" * 64), b"{}")
        self.assertEqual({token for _a, _l, _c, token in self.plot_calls}, {None},
                         "the action API, which records a revision; Enterprise names none")

    def test_a_refreshed_record_keeps_its_tmdb_half_and_item_and_drops_legacy_fields(self):
        self.refreshed()
        row = self.latest()["movie:1"]
        self.assertEqual((row["genreIDs"], row["originCountry"]), ([18], ["US"]))
        self.assertEqual({"title", "keywords", "voteCount", "originalLanguage"} & set(row), set())
        self.assertEqual(self.latest()["movie:6"]["wikidataItem"], "Q1")
        self.assertIn(({"movie": {6: ["Q2"]}}), [excluded for _ids, excluded in self.candidate_calls],
                      "the item set aside at enrichment stays set aside")

    def test_the_new_batches_follow_every_batch_and_the_checkpoint_moves_past_them(self):
        self.batch(7, [grounded(99, PLOT, 1)])
        self.current[("en", "Film 99")] = 1
        self.refreshed()
        self.assertEqual(self.listing()[-2:], ["batch-8.json", "batch-9.json"])
        with open(enrich.checkpoint_path(self.out), encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual(state["nextBatch"], 10)
        self.assertEqual(len(state["processed"]), 5, "nothing else in the checkpoint moves")

    def test_a_wikidata_failure_stops_the_refresh_before_its_chunk_is_written(self):
        with mock.patch.object(enrich, "candidates", side_effect=http.HTTPError(0, "wdqs")):
            with self.assertRaises(StageError):
                self.refreshed()
        self.assertEqual(self.listing(), ["batch-1.json"])

    def test_an_unanswered_revision_check_writes_nothing(self):
        def down(titles, language="en"):
            raise http.HTTPError(0, "en.wikipedia.org")
        with self.assertRaises(StageError):
            refresh.run(Context(out_dir=self.out), cache=self.cache, ask=down, now=NOW)
        self.assertEqual(self.listing(), ["batch-1.json"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "refresh")))


class Stage(unittest.TestCase):
    """`fetch --refresh`: the drain, then the refresh; `--plan` runs neither the drain nor a fetch."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = directory.name
        self.drained = []
        for name, stub in (("drain", lambda ctx, media: self.drained.append(media) or 1),
                           ("check_outputs", lambda ctx: None)):
            patch = mock.patch.object(fetch, name, stub)
            patch.start()
            self.addCleanup(patch.stop)

    def test_refresh_runs_after_the_drain(self):
        report = {"changed": 2, "plotless": 1, "run": "out/refresh/x"}
        with mock.patch.object(refresh, "run", return_value=report) as refreshed:
            made = fetch.run(Context(out_dir=self.out, refresh=True))
        self.assertEqual(self.drained, ["movie", "tv"])
        self.assertTrue(refreshed.called)
        self.assertIn("refreshed 2 changed, 1 lost their plot", made)

    def test_a_plan_runs_no_drain(self):
        with mock.patch.object(refresh, "run", return_value={"toFetch": 3, "grounded": 9}):
            made = fetch.run(Context(out_dir=self.out, refresh=True, plan=True))
        self.assertEqual(self.drained, [])
        self.assertIn("3 of 9", made)

    def test_without_the_flag_there_is_no_refresh(self):
        with mock.patch.object(refresh, "run", side_effect=AssertionError("refreshed unasked")):
            fetch.run(Context(out_dir=self.out))
        self.assertEqual(self.drained, ["movie", "tv"])

    def test_the_refresh_lists_are_the_fetch_stages_output(self):
        self.assertIn(artifacts.REFRESH, fetch.OUTPUTS)


if __name__ == "__main__":
    unittest.main()
