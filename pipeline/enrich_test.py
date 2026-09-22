#!/usr/bin/env python3
"""One enrichment batch — the rules each of which was bought by a run that went wrong.

The equivalence with the Swift pass it replaced is not asserted here; it was established by replay. With no
Enterprise bearer, a scratch copy of the response cache and outbound network denied, the merge-base
`taxonomy-backfill enrich` and this module enriched the same out-repass worklists, and every title neither
side had to fetch came out as the same record — two whole batches byte-identical under `cmp`. What IS
asserted here is each rule on its own, against a stub TMDB and stubbed Wikipedia/Wikidata, so a rule that
stops holding fails by name rather than as a diff in a 300-title file.

The bytes are pinned separately (`Bytes`), against output captured from the Swift encoder itself: every
reader of a batch — `articles`, `embed`, the provenance backfill, the census — was written against that.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from lib import cache as caching
from lib import http

from . import enrich
from .contract import StageError

#: `JSONEncoder([.prettyPrinted, .sortedKeys])`'s bytes for one row, captured from Swift 6 on macOS 26:
#: ` : `, two-space indent, `\/`, an empty list as `[` + blank line + bracket at the parent's indent, the
#: control characters escaped and DEL, U+2028 and emoji raw, no trailing newline.
SWIFT_ROW = ('[\n  {\n    "a" : [\n\n    ],\n    "b" : [\n      "x\\/y",\n'
             '      "é\\u0001\\u001f\x7f\\t\\n\\r\\b\\f\\"\\\\",\n      "  \U0001F600"\n    ],\n'
             '    "genreIDs" : [\n      1,\n      2\n    ],\n    "genres" : [\n      "Z"\n    ],\n'
             '    "originCountry" : [\n\n    ],\n    "plotArticle" : "P",\n    "plotArticleRedirected" : false,\n'
             '    "plotArticleRole" : "own",\n    "s" : "AC\\/DC",\n    "t" : true\n  }\n]')


class Bytes(unittest.TestCase):
    def test_a_batch_is_the_swift_encoders_bytes(self):
        row = {"a": [], "b": ["x/y", "é\x01\x1f\x7f\t\n\r\b\f\"\\", "  😀"], "originCountry": [],
               "s": "AC/DC", "t": True, "genreIDs": [1, 2], "genres": ["Z"], "plotArticle": "P",
               "plotArticleRedirected": False, "plotArticleRole": "own"}
        self.assertEqual(enrich.swift_json([row]), SWIFT_ROW)

    def test_an_empty_batch_is_the_swift_encoders_too(self):
        self.assertEqual(enrich.swift_json([]), "[\n\n]")

    def test_keys_sort_by_code_point(self):
        """`originCountry` before `originalLanguage`: upper case sorts first, as every batch on disk has it."""
        text = enrich.swift_json({"originalLanguage": "en", "originCountry": []})
        self.assertLess(text.index("originCountry"), text.index("originalLanguage"))

    def test_the_report_is_one_order_every_time(self):
        """The Swift report serialised a `Dictionary`: the same batch printed its keys in a different order
        from one process to the next. Compact, sorted, `/` escaped."""
        self.assertEqual(enrich.compact({"remaining": 0, "batch": "out/enriched/batch-1.json", "count": 3}),
                         '{"batch":"out\\/enriched\\/batch-1.json","count":3,"remaining":0}')


def put(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def detail(tmdb_id, votes=500, overview="x" * 60, **extra):
    body = {"id": tmdb_id, "title": f"Title {tmdb_id}", "vote_count": votes, "overview": overview,
            "original_language": "en", "genres": [], "keywords": {"keywords": []}, "credits": {}}
    body.update(extra)
    return body


def tmdb_record(tmdb_id, **extra):
    """A record as `lib/tmdb.title_record` builds it, plus whatever `extra` says it carries."""
    return dict(enrich.tmdb_api.title_record(detail(tmdb_id), tmdb_id, "movie"), **extra)


class StubTMDB:
    """`get(path, params)` over a dict of bodies; a value that is an exception is raised."""

    def __init__(self, bodies):
        self.bodies = bodies
        self.asked = []

    def get(self, path, params=None):
        self.asked.append(path)
        answer = self.bodies[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


def found(text, resolved="Resolved", revid=7, language="en", sections=("Plot",)):
    return {"text": text, "revId": revid, "resolvedArticle": resolved, "sections": list(sections),
            "language": language}


class Batch(unittest.TestCase):
    """`enrich.run` end to end over a temp out-dir, with the network seams stubbed."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name
        self.cache = caching.ResponseCache("wiki", os.path.join(self.out, "cache"), 3600)
        self.mapping, self.plots, self.plot_calls, self.mapping_calls = {}, {}, [], []
        for target, stub in ((enrich.wikidata, "mapping"), (enrich.plot, "plot")):
            patch = mock.patch.object(target, stub, getattr(self, stub + "_stub"))
            patch.start()
            self.addCleanup(patch.stop)

    def mapping_stub(self, ids, media, languages, cache=None):
        self.mapping_calls.append((media, sorted(ids)))
        return {i: self.mapping[(media, i)] for i in ids if (media, i) in self.mapping}

    def plot_stub(self, article, language="en", cache=None, token=None):
        self.plot_calls.append((article, language))
        answer = self.plots.get((article, language))
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict) and answer.get("source") == enrich.plot.ENTERPRISE:
            enrich.enterprise.gate.sent_this_run += 1   # what one Enterprise request costs the gate
        return answer

    def worklist(self, *entries):
        path = os.path.join(self.out, "worklist.json")
        with open(path, "w") as fh:
            json.dump([{"tmdbId": i, "mediaType": m} for m, i in entries], fh)
        return path

    def run_batch(self, bodies, entries, **kwargs):
        return enrich.run(self.worklist(*entries), self.out, client=StubTMDB(bodies), cache=self.cache,
                          **kwargs)

    def rows(self, batch_id=1):
        with open(enrich.batch_path(self.out, batch_id)) as fh:
            return {f"{r['mediaType']}:{r['tmdbId']}": r for r in json.load(fh)}

    def checkpoint(self):
        with open(enrich.checkpoint_path(self.out)) as fh:
            return json.load(fh)

    # -- ToS ------------------------------------------------------------------------------------------

    def test_tmdb_prose_never_reaches_the_record_with_or_without_a_plot(self):
        self.mapping[("movie", 1)] = {"article": "One"}
        self.plots[("One", "en")] = found("W" * 200)
        self.run_batch({"/movie/1": detail(1, overview="TMDB PROSE " * 5), "/movie/2": detail(2, overview="TMDB PROSE " * 5)},
                       [("movie", 1), ("movie", 2)])
        rows = self.rows()
        self.assertNotIn("TMDB PROSE", json.dumps(rows))
        self.assertEqual(rows["movie:1"]["overview"], "W" * 200)
        self.assertEqual((rows["movie:2"]["overview"], rows["movie:2"]["hasWikiPlot"]), ("", False))
        self.assertEqual(rows["movie:2"]["overviewChars"], 54, "the length survives, trimmed")

    # -- candidate selection --------------------------------------------------------------------------

    def test_the_longest_candidate_wins_and_carries_its_own_role(self):
        """Silo's own article gives 189 characters and the novel 12,415: first-over-the-line kept 189."""
        self.mapping[("tv", 1)] = {"article": "Silo", "sourceArticle": "Wool"}
        self.plots[("Silo", "en")] = found("s" * 189, resolved="Silo")
        self.plots[("Wool", "en")] = found("w" * 900, resolved="Wool (novel)")
        self.run_batch({"/tv/1": detail(1)}, [("tv", 1)])
        row = self.rows()["tv:1"]
        self.assertEqual((row["plotArticleRole"], row["plotArticle"], len(row["overview"])),
                         ("source-work", "Wool (novel)", 900))
        self.assertTrue(row["plotArticleRedirected"], "Wool → Wool (novel) moved")

    def test_an_own_article_that_is_enough_stops_the_search(self):
        """A well-covered adaptation must not pay for a second fetch it cannot use."""
        self.mapping[("movie", 1)] = {"article": "Own", "sourceArticle": "Book",
                                      "articlesByLang": {"de": "Eigen"}}
        self.plots[("Own", "en")] = found("o" * 1000, resolved="Own")
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        self.assertEqual(self.plot_calls, [("Own", "en")])
        self.assertEqual(self.rows()["movie:1"]["plotArticleRole"], "own")
        self.assertIs(self.rows()["movie:1"]["plotArticleRedirected"], False)

    def test_a_source_work_that_is_the_only_candidate_is_still_a_source_work(self):
        """Reading the role off the position would call it `own`: it sits first when there is no English
        article, which is 4% of titles."""
        self.mapping[("movie", 1)] = {"sourceArticle": "Book"}
        self.plots[("Book", "en")] = found("b" * 300, resolved="Book")
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        self.assertEqual(self.rows()["movie:1"]["plotArticleRole"], "source-work")

    def test_the_fallback_reads_the_titles_own_language_first_then_the_rest_in_order(self):
        self.mapping[("movie", 1)] = {"article": "Thin", "articlesByLang": {"it": "Film", "de": "Film",
                                                                             "fr": "Film"}}
        self.plots[("Film", "de")] = found("d" * 300, language="de")
        self.run_batch({"/movie/1": detail(1, original_language="fr")}, [("movie", 1)])
        self.assertEqual(self.plot_calls, [("Thin", "en"), ("Film", "fr"), ("Film", "de"), ("Film", "it")])
        row = self.rows()["movie:1"]
        self.assertEqual((row["plotArticleRole"], row["plotLanguage"]), ("own-other-language", "de"))

    def test_a_title_with_no_english_article_reaches_the_fallback(self):
        """The case the fallback exists for — two thirds of the plotless films have no English article. The
        Swift pass returned `noArticle` before trying it; 215 of 348 such titles in the replay had one."""
        self.mapping.update({("movie", 1): {"articlesByLang": {"de": "Schachnovelle"}},
                             ("movie", 2): {"articlesByLang": {"it": "Senza"}}})
        self.plots[("Schachnovelle", "de")] = found("h" * 300, resolved="Schachnovelle", language="de")
        self.run_batch({"/movie/1": detail(1), "/movie/2": detail(2)}, [("movie", 1), ("movie", 2)])
        rows = self.rows()
        self.assertEqual((rows["movie:1"]["plotArticleRole"], rows["movie:1"]["plotLanguage"]),
                         ("own-other-language", "de"))
        self.assertEqual(rows["movie:2"]["noPlotReason"], "noSection",
                         "an article that exists and has no plot section is not `noArticle`")

    def test_the_fallback_is_skipped_when_english_already_has_enough(self):
        self.mapping[("movie", 1)] = {"article": "Own", "articlesByLang": {"de": "Eigen"}}
        self.plots[("Own", "en")] = found("o" * 999)
        self.plots[("Eigen", "de")] = found("d" * 999, language="de")
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        self.assertIn(("Eigen", "de"), self.plot_calls, "999 is not enough, so the fallback is asked")
        self.assertEqual(self.rows()["movie:1"]["plotArticleRole"], "own", "a tie keeps the earlier")

    def test_an_own_article_keeps_a_tie_with_its_source_work(self):
        """Strictly longer wins, as the Swift pass's `>` had it, so at equal length the EARLIER candidate
        stays. That is the right way round: the own article describes this title, the source work describes
        the book, and nothing about equal length says the book is the better description."""
        self.mapping[("movie", 1)] = {"article": "Own", "sourceArticle": "Book"}
        self.plots[("Own", "en")] = found("o" * 500, resolved="Own")
        self.plots[("Book", "en")] = found("b" * 500, resolved="Book")
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        row = self.rows()["movie:1"]
        self.assertEqual((row["plotArticleRole"], row["plotArticle"]), ("own", "Own"))

    def test_the_longest_other_language_article_wins_not_the_first(self):
        """The fallback reads every sitelink until one is enough, and keeps the longest — the title's own
        language is asked first, not preferred at any length."""
        self.mapping[("movie", 1)] = {"articlesByLang": {"de": "Kurz", "fr": "Long"}}
        self.plots[("Kurz", "de")] = found("d" * 300, resolved="Kurz", language="de")
        self.plots[("Long", "fr")] = found("f" * 600, resolved="Long", language="fr")
        self.run_batch({"/movie/1": detail(1, original_language="de")}, [("movie", 1)])
        self.assertEqual(self.plot_calls, [("Kurz", "de"), ("Long", "fr")])
        row = self.rows()["movie:1"]
        self.assertEqual((row["plotLanguage"], row["plotArticle"], len(row["overview"])), ("fr", "Long", 600))

    def test_another_language_beats_a_thin_english_article_when_it_is_longer(self):
        """A thin English article is what the fallback is FOR: it runs below 1,000 characters, and what it
        finds competes on length with what English gave."""
        self.mapping[("movie", 1)] = {"article": "Thin", "articlesByLang": {"it": "Lungo"}}
        self.plots[("Thin", "en")] = found("e" * 200, resolved="Thin")
        self.plots[("Lungo", "it")] = found("i" * 500, resolved="Lungo", language="it")
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        row = self.rows()["movie:1"]
        self.assertEqual((row["plotArticleRole"], row["plotLanguage"], row["plotArticle"]),
                         ("own-other-language", "it", "Lungo"))

    def test_the_fallback_stops_at_the_first_other_language_article_that_is_enough(self):
        """The same stop as the English loop: once a sitelink gives 1,000 characters, the rest are not
        fetched, even when one of them is longer."""
        self.mapping[("movie", 1)] = {"articlesByLang": {"de": "Genug", "fr": "Plus"}}
        self.plots[("Genug", "de")] = found("d" * 1000, resolved="Genug", language="de")
        self.plots[("Plus", "fr")] = found("f" * 3000, resolved="Plus", language="fr")
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        self.assertEqual(self.plot_calls, [("Genug", "de")])
        self.assertEqual(self.rows()["movie:1"]["plotLanguage"], "de")

    def test_a_thin_article_found_only_in_another_language_is_below_the_floor(self):
        """The class 04129a9 opened: a title whose ONLY article is on another Wikipedia, with a plot section
        too short to ground on. Its section was found, so it is `belowFloor` — a threshold decision — and
        not `noSection`, which sends it to whoever writes heading rules. ~5,778 titles take this path."""
        self.mapping[("movie", 1)] = {"articlesByLang": {"de": "Dünn"}}
        self.plots[("Dünn", "de")] = found("d" * 80, resolved="Dünn", language="de")
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        row = self.rows()["movie:1"]
        self.assertEqual((row["hasWikiPlot"], row["noPlotReason"], row["overview"]), (False, "belowFloor", ""))

    def test_a_plotless_title_starts_from_an_empty_overview_whatever_the_record_carries(self):
        """`overview` holds a Wikipedia plot or nothing. Emptied outright rather than filtered, so a TMDB field
        that some later change carries on the record under another name still cannot reach it."""
        record = tmdb_record(1, overview="TMDB PROSE " * 5, tmdbOverview="TMDB PROSE " * 5)
        verdict, row, _detail = enrich.reground(record, {}, self.cache, None)
        self.assertEqual((verdict, row["overview"], row["hasWikiPlot"]), ("noPlot", "", False))

    def test_the_floor_and_the_four_reasons(self):
        self.mapping.update({("movie", 1): {}, ("movie", 2): {"article": "Bare"},
                             ("movie", 3): {"article": "Short"}, ("movie", 4): {"article": "Gone"},
                             ("movie", 5): {"article": "Floor"}})
        # Literal lengths: the floor is 120 on measurement (Silo's 189-character premise), and a test written
        # against the constant would follow it anywhere.
        self.plots[("Short", "en")] = found("s" * 119)
        self.plots[("Gone", "en")] = http.HTTPError(404, "https://en.wikipedia.org/w/api.php")
        self.plots[("Floor", "en")] = found("f" * 120)
        self.run_batch({f"/movie/{i}": detail(i) for i in range(1, 6)}, [("movie", i) for i in range(1, 6)])
        rows = self.rows()
        self.assertEqual([rows[f"movie:{i}"].get("noPlotReason") for i in range(1, 6)],
                         ["noArticle", "noSection", "belowFloor", "fetchFailed", None])
        self.assertTrue(rows["movie:5"]["hasWikiPlot"])

    def test_the_stored_article_is_the_resolved_one_and_unknown_stays_unknown(self):
        """A redirect's content and revid are the target's; storing the requested name beside them makes a
        revision refresh compare two different pages. The Enterprise path names no page, and absent is
        UNKNOWN — never `false`."""
        self.mapping.update({("movie", 1): {"article": "Jarhead 2"}, ("movie", 2): {"article": "Wire"}})
        self.plots[("Jarhead 2", "en")] = found("j" * 300, resolved="Jarhead (film)")
        self.plots[("Wire", "en")] = found("w" * 300, resolved=None, revid=None)
        self.run_batch({"/movie/1": detail(1), "/movie/2": detail(2)}, [("movie", 1), ("movie", 2)])
        rows = self.rows()
        self.assertEqual((rows["movie:1"]["plotArticle"], rows["movie:1"]["plotArticleRedirected"]),
                         ("Jarhead (film)", True))
        self.assertEqual(rows["movie:2"]["plotArticle"], "Wire")
        self.assertNotIn("plotArticleRedirected", rows["movie:2"])
        self.assertNotIn("plotRevId", rows["movie:2"])

    def test_the_report_counts_the_source_that_served_each_plot(self):
        """Which source was ASKED is not which answered: a throttled bearer falls back title by title."""
        self.mapping.update({("movie", i): {"article": f"A{i}"} for i in (1, 2, 3)})
        self.plots[("A1", "en")] = dict(found("a" * 300, resolved=None, revid=None), source=enrich.plot.ENTERPRISE)
        self.plots[("A2", "en")] = dict(found("b" * 300), source=enrich.plot.ACTION_API)
        with mock.patch.object(enrich.enterprise, "gate", enrich.enterprise.Gate()):
            report = self.run_batch({f"/movie/{i}": detail(i) for i in (1, 2, 3)},
                                    [("movie", i) for i in (1, 2, 3)])
            self.assertEqual((report["plotsFromEnterprise"], report["plotsFromActionApi"], report["wikiPlot"],
                              report["enterpriseRequests"]), (1, 1, 2, 1))
            self.assertNotIn("source", json.dumps(self.rows()), "which source served is reported, not recorded")
            self.mapping[("movie", 4)] = {"article": "A4"}
            self.plots[("A4", "en")] = dict(found("d" * 300, resolved=None, revid=None),
                                            source=enrich.plot.ENTERPRISE)
            report = self.run_batch({"/movie/4": detail(4)}, [("movie", 4)])
        self.assertEqual(report["enterpriseRequests"], 1, "this batch's requests, not the process's")

    def test_a_malformed_reserve_refuses_a_run_that_holds_a_bearer(self):
        """Read inside a grounding worker, the error would be swallowed as one more failed fast path."""
        with mock.patch.object(enrich.enterprise, "gate", enrich.enterprise.Gate()), \
                mock.patch.dict(os.environ, {"DEN_ENTERPRISE_RESERVE": "lots"}):
            with self.assertRaises(ValueError):
                self.run_batch({"/movie/1": detail(1)}, [("movie", 1)], token="bearer")
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))

    # -- the checkpoint -------------------------------------------------------------------------------

    def test_transient_and_below_floor_ids_stay_pending_and_the_rest_are_checkpointed(self):
        self.mapping[("movie", 4)] = {"article": "Blip"}
        self.plots[("Blip", "en")] = http.HTTPError(503, "x")
        report = self.run_batch({"/movie/1": http.HTTPError(429, "x"), "/movie/2": detail(2, votes=10),
                                 "/movie/3": http.HTTPError(404, "x"), "/movie/4": detail(4),
                                 "/movie/5": detail(5, overview="stub")},
                                [("movie", i) for i in range(1, 6)])
        self.assertEqual(sorted(self.checkpoint()["processed"]), ["movie:3", "movie:5"])
        self.assertEqual((report["deferred"], report["belowFloor"], report["failures"], report["noOverview"],
                          report["remaining"], report["count"]), (2, 1, 1, 1, 3, 0))

    def test_no_answer_at_all_is_transient_everywhere(self):
        """A dropped connection, a TLS failure, a refused socket: none says anything about the title. The
        Swift pass wrote some of these as a permanently plotless `fetchFailed`."""
        self.mapping[("movie", 2)] = {"article": "Two"}
        self.plots[("Two", "en")] = http.HTTPError(0, "https://en.wikipedia.org/w/api.php")
        report = self.run_batch({"/movie/1": http.HTTPError(0, "x"), "/movie/2": detail(2)},
                                [("movie", 1), ("movie", 2)])
        self.assertEqual((report["deferred"], report["failures"], report["count"]), (2, 0, 0))
        self.assertEqual(self.checkpoint()["processed"], [])

    def test_the_next_batch_is_numbered_from_the_directory_when_the_checkpoint_is_missing(self):
        """`out-t02` had 153 batches and no checkpoint; a delta numbered from 1 and overwrote two."""
        os.makedirs(os.path.join(self.out, "enriched"))
        for name in ("batch-9.json", "batch-153.json", "batch-17.json", "notes.txt"):
            put(os.path.join(self.out, "enriched", name), "[]")
        report = self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        self.assertEqual(report["batchId"], 154)
        self.assertEqual(self.checkpoint()["nextBatch"], 155)

    def test_an_existing_batch_is_never_overwritten(self):
        os.makedirs(os.path.join(self.out, "enriched"))
        with open(enrich.checkpoint_path(self.out), "w") as fh:
            json.dump({"processed": [], "nextBatch": 1}, fh)
        with mock.patch.object(enrich, "batches", return_value=[]):
            put(enrich.batch_path(self.out, 1), "[]")
            with self.assertRaises(StageError):
                self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        with open(enrich.batch_path(self.out, 1)) as fh:
            self.assertEqual(fh.read(), "[]")

    def test_an_unreadable_checkpoint_is_a_refusal_not_a_reset(self):
        put(enrich.checkpoint_path(self.out), '{"processed": [')
        with self.assertRaises(StageError):
            self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])

    def test_a_legacy_checkpoint_of_bare_ids_means_movies(self):
        with open(enrich.checkpoint_path(self.out), "w") as fh:
            json.dump({"processed": [1]}, fh)
        report = self.run_batch({"/tv/1": detail(1)}, [("movie", 1), ("tv", 1)])
        self.assertEqual(report["count"], 1)
        self.assertIn("tv:1", self.rows())

    def test_anime_is_kept_unless_asked(self):
        """Excluding it by default silently cost the corpus 1,498 titles, the Ghibli catalogue among them."""
        body = detail(1, original_language="ja", genres=[{"id": 16, "name": "Animation"}])
        self.assertEqual(self.run_batch({"/movie/1": body}, [("movie", 1)])["count"], 1)
        os.remove(enrich.checkpoint_path(self.out))
        report = self.run_batch({"/movie/1": body}, [("movie", 1)], exclude_anime=True)
        self.assertEqual((report["anime"], report["count"]), (1, 0))

    def test_both_media_in_one_worklist_are_mapped_apart(self):
        """Series 91545 looked up as a MOVIE grounded Young Wallander on "Sunday Drive (film)". The refusal
        of a mixed worklist is gone; the keying by pair is what makes that safe."""
        self.mapping.update({("tv", 95): {"article": "Buffy"}, ("movie", 95): {"article": "Armageddon"}})
        self.plots.update({("Buffy", "en"): found("b" * 300, resolved="Buffy"),
                           ("Armageddon", "en"): found("a" * 300, resolved="Armageddon")})
        self.run_batch({"/tv/95": detail(95), "/movie/95": detail(95)}, [("tv", 95), ("movie", 95)])
        rows = self.rows()
        self.assertEqual((rows["tv:95"]["plotArticle"], rows["movie:95"]["plotArticle"]), ("Buffy", "Armageddon"))

    def test_remaining_counts_a_series_and_a_film_that_share_an_id_apart(self):
        """`remaining` is how the drain decides it is finished. Counted by bare id, a processed movie 95
        would mark series 95 done and the drain would stop with it never enriched."""
        self.mapping.update({("movie", 95): {"article": "Armageddon"}, ("tv", 95): {"article": "Buffy"}})
        bodies = {"/movie/95": detail(95), "/tv/95": detail(95), "/tv/7": detail(7)}
        report = self.run_batch(bodies, [("movie", 95), ("tv", 95), ("tv", 7)], limit=1)
        self.assertEqual(report["remaining"], 2, "tv:95 and tv:7 are still pending")
        report = self.run_batch(bodies, [("movie", 95), ("tv", 95), ("tv", 7)], limit=2)
        self.assertEqual(report["remaining"], 0)

    def test_a_series_only_worklist_reports_zero_once_its_batch_is_written(self):
        report = self.run_batch({"/tv/1": detail(1), "/tv/2": detail(2)}, [("tv", 1), ("tv", 2)])
        self.assertEqual((report["count"], report["remaining"]), (2, 0))

    def test_a_key_is_recovered_only_when_the_same_media_holds_it(self):
        """Series 95 written after movie 95 is a new title, not a re-covered one — and series 7 written twice
        is, whichever media the earlier batch was read as."""
        os.makedirs(os.path.join(self.out, "enriched"))
        put(enrich.batch_path(self.out, 1), json.dumps([{"tmdbId": 95, "mediaType": "movie"},
                                                         {"tmdbId": 7, "mediaType": "tv"}]))
        again = enrich.recovered(self.out, 2, [{"tmdbId": 95, "mediaType": "tv"},
                                               {"tmdbId": 7, "mediaType": "tv"}])
        self.assertEqual(again, ["tv:7 (batch 1)"])

    def test_each_media_is_asked_about_its_own_ids_only(self):
        """The query TEXT is the mapping's cache key. Asking about the whole batch under each media returns
        the same facts — WDQS answers only the ids that are that media — but hashes to a different key, so
        every mapping the Swift pass cached for a single-media batch would be fetched again."""
        self.run_batch({"/tv/7": detail(7), "/movie/95": detail(95), "/movie/3": detail(3)},
                       [("tv", 7), ("movie", 95), ("movie", 3)])
        self.assertEqual(self.mapping_calls, [("movie", [3, 95]), ("tv", [7])])

    def test_the_limit_is_how_many_ids_one_batch_takes(self):
        client = StubTMDB({f"/movie/{i}": detail(i) for i in range(1, 6)})
        report = enrich.run(self.worklist(*[("movie", i) for i in range(1, 6)]), self.out, limit=2,
                            client=client, cache=self.cache)
        self.assertEqual(sorted(client.asked), ["/movie/1", "/movie/2"], "the first two, in worklist order")
        self.assertEqual((report["count"], report["remaining"]), (2, 3))

    def test_a_failed_mapping_aborts_the_batch_and_writes_nothing(self):
        with mock.patch.object(enrich.wikidata, "mapping", side_effect=http.HTTPError(0, "x")):
            with self.assertRaises(enrich.Aborted):
                self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
        self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

    def test_a_drained_worklist_needs_no_client(self):
        with open(enrich.checkpoint_path(self.out), "w") as fh:
            json.dump({"processed": ["movie:1"], "nextBatch": 2}, fh)
        report = enrich.run(self.worklist(("movie", 1)), self.out, client=None, cache=self.cache)
        self.assertEqual(report, {"remaining": 0, "count": 0})


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CommandLine(unittest.TestCase):
    """The two places a person or a timer types this module's command line, held to its parser."""

    def invocations(self):
        found = []
        for path in ("scripts/delta-run.sh", "docs/OPERATE.md"):
            with open(os.path.join(REPO, path), encoding="utf-8") as fh:
                text = fh.read().replace("\\\n", " ")
            found += [(path, line.split("python3 -m pipeline.enrich", 1)[1])
                      for line in text.splitlines()
                      if "python3 -m pipeline.enrich " in line and not line.lstrip().startswith("#")]
        return found

    def test_every_documented_invocation_parses(self):
        """The daily delta runs this unattended; a flag the parser does not know is a dead timer."""
        found = self.invocations()
        self.assertEqual(sorted({path for path, _ in found}), ["docs/OPERATE.md", "scripts/delta-run.sh"])
        for path, rest in found:
            words = rest.split("|")[0].split("#")[0].replace(")", " ").split()
            flags = {word for word in words if word.startswith("--")}
            args = []
            for flag in flags:
                args += [flag] if flag == "--exclude-anime" else [flag, "1"]
            enrich.parser().parse_args(args)  # an unknown flag exits here

    def test_an_unknown_flag_is_refused(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            enrich.parser().parse_args(["--worklist", "w", "--out-dir", "o", "--plot-cap", "3"])


if __name__ == "__main__":
    unittest.main()
