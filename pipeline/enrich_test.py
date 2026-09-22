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
        self.mapping, self.plots, self.plot_calls = {}, {}, []
        for target, stub in ((enrich.wikidata, "mapping"), (enrich.plot, "plot")):
            patch = mock.patch.object(target, stub, getattr(self, stub + "_stub"))
            patch.start()
            self.addCleanup(patch.stop)

    def mapping_stub(self, ids, media, languages, cache=None):
        return {i: self.mapping[(media, i)] for i in ids if (media, i) in self.mapping}

    def plot_stub(self, article, language="en", cache=None, token=None):
        self.plot_calls.append((article, language))
        answer = self.plots.get((article, language))
        if isinstance(answer, Exception):
            raise answer
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
        self.plots[("Own", "en")] = found("o" * enrich.OWN_ARTICLE_SUFFICIENT, resolved="Own")
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

    def test_the_floor_and_the_four_reasons(self):
        self.mapping.update({("movie", 1): {}, ("movie", 2): {"article": "Bare"},
                             ("movie", 3): {"article": "Short"}, ("movie", 4): {"article": "Gone"},
                             ("movie", 5): {"article": "Floor"}})
        self.plots[("Short", "en")] = found("s" * (enrich.WIKI_PLOT_FLOOR - 1))
        self.plots[("Gone", "en")] = http.HTTPError(404, "https://en.wikipedia.org/w/api.php")
        self.plots[("Floor", "en")] = found("f" * enrich.WIKI_PLOT_FLOOR)
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
