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


class Killed(BaseException):
    """A process death: nothing below `run` may catch it."""


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


#: `found`'s default: the fetch landed on the page it asked for. Filled in by `plot_stub`, which knows it.
ASKED = object()


def found(text, resolved=ASKED, revid=7, language="en", sections=("Plot",)):
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
        # Wikidata P364 per (media, tmdbId): what orders the other-language plot fallback. And its P136
        # genres and P31 types, which is what the opt-in anime exclusion reads.
        self.languages, self.language_calls = {}, []
        self.kinds, self.kind_calls = {}, []
        # Per (media, tmdbId): the English article of each P144 work, and whether it is a film or a series.
        self.sources, self.source_calls = {}, []
        # IMDb: the id Wikidata names per (media, tmdbId), the dump's counts per id, and what loading the
        # dump does — a `Ratings`, or an exception to raise.
        self.imdb_ids, self.imdb_votes, self.imdb_id_calls = {}, {}, []
        self.dump = None
        for target, stub in ((enrich.wikidata, "mapping"), (enrich.plot, "plot"), (enrich.wikidata, "imdb_ids"),
                             (enrich.wikidata, "languages"), (enrich.wikidata, "kinds"),
                             (enrich.wikidata, "sources"), (enrich.imdb, "ratings")):
            patch = mock.patch.object(target, stub, getattr(self, stub + "_stub"))
            patch.start()
            self.addCleanup(patch.stop)

    def imdb_ids_stub(self, ids, media, cache=None):
        self.imdb_id_calls.append((media, sorted(ids)))
        return {i: self.imdb_ids[(media, i)] for i in ids if (media, i) in self.imdb_ids}

    def ratings_stub(self, minimum=0, env=None, request=None):
        if isinstance(self.dump, Exception):
            raise self.dump
        return self.dump or enrich.imdb.Ratings(
            {tt: n for tt, n in self.imdb_votes.items() if n >= minimum}, "unchanged")

    def mapping_stub(self, ids, media, languages, cache=None):
        self.mapping_calls.append((media, sorted(ids)))
        return {i: self.mapping[(media, i)] for i in ids if (media, i) in self.mapping}

    def languages_stub(self, ids, media, cache=None):
        self.language_calls.append((media, sorted(ids)))
        return {i: self.languages[(media, i)] for i in ids if (media, i) in self.languages}

    def kinds_stub(self, ids, media, cache=None):
        self.kind_calls.append((media, sorted(ids)))
        return {i: self.kinds[(media, i)] for i in ids if (media, i) in self.kinds}

    def sources_stub(self, ids, media, cache=None):
        self.source_calls.append((media, sorted(ids)))
        return {i: self.sources[(media, i)] for i in ids if (media, i) in self.sources}

    def plot_stub(self, article, language="en", cache=None, token=None):
        self.plot_calls.append((article, language))
        answer = self.plots.get((article, language))
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict) and answer.get("source") == enrich.plot.ENTERPRISE:
            enrich.enterprise.gate.sent_this_run += 1   # what one Enterprise request costs the gate
        if isinstance(answer, dict) and answer["resolvedArticle"] is ASKED:
            answer = dict(answer, resolvedArticle=article)
        return answer

    def worklist(self, *entries, votes=None):
        """An export-shaped worklist — two keys per row — unless `votes` names a count per `(media, id)`,
        which is the shape `/discover` and the delta write."""
        path = os.path.join(self.out, "worklist.json")
        rows = []
        for media, tmdb_id in entries:
            row = {"tmdbId": tmdb_id, "mediaType": media}
            if votes and (media, tmdb_id) in votes:
                row["voteCount"] = votes[(media, tmdb_id)]
            rows.append(row)
        with open(path, "w") as fh:
            json.dump(rows, fh)
        return path

    def run_batch(self, bodies, entries, votes=None, **kwargs):
        return enrich.run(self.worklist(*entries, votes=votes), self.out, client=StubTMDB(bodies),
                          cache=self.cache, **kwargs)

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
        self.assertEqual([name for name in rows["movie:2"] if "overview" in name.lower()], ["overview"],
                         "not even the TMDB overview's LENGTH is carried any more")

    def test_a_batch_row_carries_only_the_tmdb_fields_a_reader_needs(self):
        """oxyc/den-dataset#53. `genreIDs` stays for `./den genres-moods`' `animated` flag; the title, year,
        genre names, keywords, director and cast were written for readers that are gone."""
        self.mapping[("movie", 1)] = {"article": "One"}
        self.plots[("One", "en")] = found("W" * 200)
        body = detail(1, release_date="1994-09-23", genres=[{"id": 16, "name": "Animation"}],
                      keywords={"keywords": [{"id": 378, "name": "prison"}]},
                      credits={"cast": [{"name": "Tim Robbins", "order": 0}],
                               "crew": [{"name": "Frank Darabont", "job": "Director"}]})
        self.run_batch({"/movie/1": body}, [("movie", 1)])
        row = self.rows()["movie:1"]
        self.assertEqual({"title", "year", "genres", "keywords", "keywordIDs", "director", "topCast"} & set(row),
                         set())
        self.assertEqual(row["genreIDs"], [16])

    def test_creators_are_wikidatas_or_none_never_tmdbs(self):
        """`createdBy` is composed into the embedding document. With no Wikidata P170 it is EMPTY — TMDB's
        `created_by` (or a crew "Creator" credit) is not a fallback."""
        self.mapping[("tv", 1)] = {"article": "One", "creators": ["Wikidata Creator"]}
        self.mapping[("tv", 2)] = {"article": "Two"}
        self.plots[("One", "en")] = found("W" * 200)
        tmdb_creators = {"created_by": [{"name": "TMDB Creator"}],
                         "credits": {"crew": [{"name": "TMDB Crew", "job": "Creator"}]}}
        self.run_batch({"/tv/1": detail(1, **tmdb_creators), "/tv/2": detail(2, **tmdb_creators)},
                       [("tv", 1), ("tv", 2)])
        rows = self.rows()
        self.assertEqual((rows["tv:1"]["createdBy"], rows["tv:2"]["createdBy"]), (["Wikidata Creator"], []))
        self.assertNotIn("TMDB C", json.dumps(rows))

    # -- candidate selection --------------------------------------------------------------------------

    def test_an_own_premise_beats_a_longer_source_work(self):
        """Gen V's own Premise is 668 characters; its P144 work is The Boys, whose 3,164 won on length and
        described the parent series instead. A plot of its own that clears the floor is never out-read."""
        self.mapping[("tv", 1)] = {"article": "Gen V", "sourceArticle": "The Boys (TV series)"}
        self.plots[("Gen V", "en")] = found("g" * 668, resolved="Gen V")
        self.plots[("The Boys (TV series)", "en")] = found("b" * 3164, resolved="The Boys (TV series)")
        self.run_batch({"/tv/1": detail(1)}, [("tv", 1)])
        row = self.rows()["tv:1"]
        self.assertEqual((row["plotArticleRole"], row["plotArticle"], len(row["overview"])),
                         ("own", "Gen V", 668))
        self.assertNotIn(("The Boys (TV series)", "en"), self.plot_calls, "the source work is not even read")

    def test_an_own_other_language_plot_beats_the_source_work_too(self):
        """The source work is the last resort after EVERY own article, not after English: a title whose
        English page has no plot section is still described by its German one before by the book."""
        self.mapping[("tv", 1)] = {"article": "Series", "sourceArticle": "Novel",
                                   "articlesByLang": {"de": "Serie"}}
        self.plots[("Serie", "de")] = found("d" * 300, resolved="Serie", language="de")
        self.plots[("Novel", "en")] = found("n" * 6000, resolved="Novel")
        self.run_batch({"/tv/1": detail(1)}, [("tv", 1)])
        row = self.rows()["tv:1"]
        self.assertEqual((row["plotArticleRole"], row["plotLanguage"]), ("own-other-language", "de"))
        self.assertNotIn(("Novel", "en"), self.plot_calls)

    def test_the_source_work_grounds_a_title_with_no_plot_of_its_own(self):
        """What the fallback is for: an adaptation whose article is cast and episode tables. Below the floor
        counts as none — 80 characters of its own lose to the novel."""
        self.mapping.update({("tv", 1): {"article": "Bare", "sourceArticle": "Book"},
                             ("tv", 2): {"article": "Stub", "sourceArticle": "Book"}})
        self.plots[("Stub", "en")] = found("s" * 80, resolved="Stub")
        self.plots[("Book", "en")] = found("b" * 900, resolved="Book")
        self.run_batch({"/tv/1": detail(1), "/tv/2": detail(2)}, [("tv", 1), ("tv", 2)])
        rows = self.rows()
        self.assertEqual([(rows[k]["plotArticleRole"], rows[k]["plotArticle"]) for k in ("tv:1", "tv:2")],
                         [("source-work", "Book")] * 2)

    def test_a_source_work_that_redirects_is_not_read_as_one(self):
        """A P144 sitelink that lands elsewhere is not the work either: `La noia` landed on the article about
        its author."""
        self.mapping[("movie", 1)] = {"sourceArticle": "La noia"}
        self.plots[("La noia", "en")] = found("a" * 900, resolved="Alberto Moravia")
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        row = self.rows()["movie:1"]
        self.assertEqual((row["hasWikiPlot"], row["noPlotReason"]), (False, "noArticle"))

    def test_an_own_article_that_is_enough_stops_the_search(self):
        """A well-covered adaptation must not pay for a second fetch it cannot use."""
        self.mapping[("movie", 1)] = {"article": "Own", "sourceArticle": "Book",
                                      "articlesByLang": {"de": "Eigen"}}
        self.plots[("Own", "en")] = found("o" * 1000, resolved="Own")
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        self.assertEqual(self.plot_calls, [("Own", "en")])
        self.assertEqual(self.rows()["movie:1"]["plotArticleRole"], "own")
        self.assertIs(self.rows()["movie:1"]["plotArticleRedirected"], False)

    def test_a_source_work_that_is_a_film_or_a_series_is_never_read(self):
        """`La oficina` is based on The Office: its plot, cast and tone are another production's. Nothing of
        its own clears the floor, so it is plotless and says why — and the article is not even fetched."""
        self.mapping.update({("tv", 1): {"article": "La oficina", "sourceArticle": "The Office"},
                             ("tv", 2): {"sourceArticle": "The Office"}})
        self.sources.update({("tv", 1): {"The Office": True}, ("tv", 2): {"The Office": True}})
        self.plots[("La oficina", "en")] = found("o" * 80, resolved="La oficina")
        self.plots[("The Office", "en")] = found("t" * 3000, resolved="The Office")
        self.run_batch({"/tv/1": detail(1), "/tv/2": detail(2)}, [("tv", 1), ("tv", 2)])
        rows = self.rows()
        self.assertEqual([(rows[k]["hasWikiPlot"], rows[k]["noPlotReason"], rows[k]["overview"])
                          for k in ("tv:1", "tv:2")], [(False, "sourceIsScreenWork", "")] * 2,
                         "below the floor on its own page or with none at all, the reason is the refusal")
        self.assertEqual(self.plot_calls, [("La oficina", "en")], "only its own page is fetched — no source, and no empty name in its place")

    def test_a_novel_is_still_read_and_is_preferred_over_a_screen_work_the_title_also_names(self):
        """P144 may name both the book and an earlier film of it. The book tells the story; the film is
        another production of it."""
        self.mapping.update({("movie", 1): {"sourceArticle": "The Wizard of Oz (1939 film)"},
                             ("movie", 2): {"sourceArticle": "Novel"}})
        self.sources.update({("movie", 1): {"The Wizard of Oz (1939 film)": True, "The Wonderful Wizard of Oz": False},
                             ("movie", 2): {"Novel": False}})
        self.plots[("The Wonderful Wizard of Oz", "en")] = found("w" * 900)
        self.plots[("Novel", "en")] = found("n" * 900)
        self.run_batch({"/movie/1": detail(1), "/movie/2": detail(2)}, [("movie", 1), ("movie", 2)])
        rows = self.rows()
        self.assertEqual([(rows[k]["plotArticleRole"], rows[k]["plotArticle"]) for k in ("movie:1", "movie:2")],
                         [("source-work", "The Wonderful Wizard of Oz"), ("source-work", "Novel")])
        self.assertNotIn(("The Wizard of Oz (1939 film)", "en"), self.plot_calls)

    def test_a_source_work_the_lookup_says_nothing_about_is_read_as_before(self):
        """Unknown is not a screen work: a pick Wikidata's answer does not name is read."""
        self.mapping[("movie", 1)] = {"sourceArticle": "Book"}
        self.sources[("movie", 1)] = {"Something Else": True}
        self.plots[("Book", "en")] = found("b" * 900)
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        self.assertEqual(self.rows()["movie:1"]["plotArticle"], "Book")

    def test_the_source_lookup_is_one_query_per_media_for_the_titles_with_a_source_work(self):
        self.mapping.update({("movie", 1): {"sourceArticle": "A"}, ("movie", 2): {"article": "B"},
                             ("movie", 3): {"sourceArticle": "C"}, ("tv", 4): {"sourceArticle": "D"}})
        self.run_batch({"/movie/1": detail(1), "/movie/2": detail(2), "/movie/3": detail(3), "/tv/4": detail(4)},
                       [("movie", 1), ("movie", 2), ("movie", 3), ("tv", 4)])
        self.assertEqual(self.source_calls, [("movie", [1, 3]), ("tv", [4])])

    def test_a_failed_source_lookup_aborts_the_batch_and_writes_nothing(self):
        """Swallowed, every remake in the batch would be grounded on its original again."""
        self.mapping[("movie", 1)] = {"sourceArticle": "Original"}
        with mock.patch.object(enrich.wikidata, "sources", side_effect=http.HTTPError(0, "x")):
            with self.assertRaises(enrich.Aborted):
                self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
        self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

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
        self.languages[("movie", 1)] = ["fr"]
        self.plots[("Film", "de")] = found("d" * 300, language="de")
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        self.assertEqual(self.plot_calls, [("Thin", "en"), ("Film", "fr"), ("Film", "de"), ("Film", "it")])
        row = self.rows()["movie:1"]
        self.assertEqual((row["plotArticleRole"], row["plotLanguage"]), ("own-other-language", "de"))

    def test_the_order_is_wikidatas_languages_not_tmdbs(self):
        """P364, and every code it states. TMDB names one `original_language` and a co-production has
        several, so the list is the better ordering as well as the CC0 one."""
        self.mapping[("movie", 1)] = {"article": "Thin",
                                      "articlesByLang": {"it": "F", "de": "F", "fr": "F", "sv": "F"}}
        self.mapping[("movie", 2)] = {"article": "Other"}
        self.mapping[("tv", 3)] = {"article": "Series"}
        self.languages[("movie", 1)] = ["fr", "sv"]
        self.run_batch({"/movie/1": detail(1, original_language="it"), "/movie/2": detail(2),
                        "/tv/3": detail(3)}, [("movie", 1), ("movie", 2), ("tv", 3)])
        self.assertEqual(self.plot_calls[:5],
                         [("Thin", "en"), ("F", "fr"), ("F", "sv"), ("F", "de"), ("F", "it")])
        self.assertEqual(self.language_calls, [("movie", [1, 2]), ("tv", [3])],
                         "one query for the batch, per media — never one per title")

    def test_a_title_wikidata_states_no_language_for_reads_its_sitelinks_in_code_order(self):
        """1,164 of the 12,611 corpus titles grounded this way have no P364. Nothing is preferred rather
        than TMDB's code being preferred."""
        self.mapping[("movie", 1)] = {"article": "Thin", "articlesByLang": {"it": "F", "de": "F"}}
        self.run_batch({"/movie/1": detail(1, original_language="it")}, [("movie", 1)])
        self.assertEqual(self.plot_calls, [("Thin", "en"), ("F", "de"), ("F", "it")])

    def test_a_failed_language_lookup_aborts_the_batch_and_writes_nothing(self):
        """It asks the same service as the mapping. Swallowed, it would silently reorder every fallback in
        the batch to code order and record nothing about it."""
        with mock.patch.object(enrich.wikidata, "languages", side_effect=http.HTTPError(0, "x")):
            with self.assertRaises(enrich.Aborted):
                self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
        self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

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

    def test_the_longest_other_language_article_wins_not_the_first(self):
        """The fallback reads every sitelink until one is enough, and keeps the longest — the title's own
        language is asked first, not preferred at any length."""
        self.mapping[("movie", 1)] = {"articlesByLang": {"de": "Kurz", "fr": "Long"}}
        self.languages[("movie", 1)] = ["de"]
        self.plots[("Kurz", "de")] = found("d" * 300, resolved="Kurz", language="de")
        self.plots[("Long", "fr")] = found("f" * 600, resolved="Long", language="fr")
        self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])
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

    def test_a_page_the_wiki_does_not_have_is_no_article_not_a_failed_fetch(self):
        """`missingtitle`: the sitelink names a page that is gone. A retry gets the same answer and no heading
        rule reaches a page that does not exist, so it is `noArticle` — whose re-run is a fresh Wikidata
        mapping — unless another candidate existed, which then decides the reason as always."""
        gone = enrich.plot.NoPage("en.wikipedia.org has no page 'Gone' (missingtitle)")
        self.mapping.update({("movie", 1): {"article": "Gone"},
                             ("movie", 2): {"article": "Gone", "articlesByLang": {"de": "Leer"}},
                             ("movie", 3): {"article": "Gone", "articlesByLang": {"de": "Dünn"}},
                             ("movie", 4): {"article": "Gone", "articlesByLang": {"de": "Weg"}}})
        self.plots.update({("Gone", "en"): gone, ("Weg", "de"): gone,
                           ("Dünn", "de"): found("d" * 50, language="de")})
        self.run_batch({f"/movie/{i}": detail(i) for i in range(1, 5)}, [("movie", i) for i in range(1, 5)])
        rows = self.rows()
        self.assertEqual([rows[f"movie:{i}"]["noPlotReason"] for i in range(1, 5)],
                         ["noArticle", "noSection", "belowFloor", "noArticle"])
        self.assertEqual(sorted(self.checkpoint()["processed"]), [f"movie:{i}" for i in range(1, 5)],
                         "an answer, so checkpointed like any other")

    def test_a_sitelink_that_redirects_into_another_page_is_no_article(self):
        """`Jarhead 2: Field of Fire`'s sitelink is a redirect into `Jarhead (film)`: grounding on it described
        the first film. It counts as no article on that wiki, so the other-language fallback still runs."""
        self.mapping.update({("movie", 1): {"article": "Jarhead 2"},
                             ("movie", 2): {"article": "Jarhead 3", "articlesByLang": {"de": "Jarhead 3"}}})
        self.plots[("Jarhead 2", "en")] = found("j" * 300, resolved="Jarhead (film)")
        self.plots[("Jarhead 3", "en")] = found("j" * 300, resolved="Jarhead (film)")
        self.plots[("Jarhead 3", "de")] = found("d" * 300, resolved="Jarhead 3", language="de")
        self.run_batch({"/movie/1": detail(1), "/movie/2": detail(2)}, [("movie", 1), ("movie", 2)])
        rows = self.rows()
        self.assertEqual((rows["movie:1"]["hasWikiPlot"], rows["movie:1"]["noPlotReason"]), (False, "noArticle"))
        self.assertEqual((rows["movie:2"]["plotArticleRole"], rows["movie:2"]["plotArticle"],
                          rows["movie:2"]["plotArticleRedirected"]), ("own-other-language", "Jarhead 3", False))

    def test_an_unseen_redirect_stays_unknown(self):
        """The Enterprise path names no page, so it cannot be refused as a redirect, and absent is UNKNOWN —
        never `false`."""
        self.mapping[("movie", 2)] = {"article": "Wire"}
        self.plots[("Wire", "en")] = found("w" * 300, resolved=None, revid=None)
        self.run_batch({"/movie/2": detail(2)}, [("movie", 2)])
        row = self.rows()["movie:2"]
        self.assertEqual(row["plotArticle"], "Wire")
        self.assertNotIn("plotArticleRedirected", row)
        self.assertNotIn("plotRevId", row)

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

    # -- admission: TMDB's count OR IMDb's ------------------------------------------------------------

    def admission(self, *titles):
        """One batch of `(tmdbId, TMDB votes, origin, IMDb id or None, IMDb votes)` movies. Returns the
        report and the keys the batch wrote."""
        bodies = {}
        for tmdb_id, votes, origin, imdb_id, imdb_votes in titles:
            bodies[f"/movie/{tmdb_id}"] = detail(tmdb_id, votes=votes, origin_country=origin)
            if imdb_id:
                self.imdb_ids[("movie", tmdb_id)] = imdb_id
                self.imdb_votes[imdb_id] = imdb_votes
        report = self.run_batch(bodies, [("movie", t[0]) for t in titles])
        return report, set(self.rows()) if os.path.exists(enrich.batch_path(self.out, 1)) else set()

    def test_a_title_only_imdb_admits_is_admitted(self):
        """`Elkürtük`: 44 TMDB votes, 40,939 on IMDb. TMDB's floor alone never let it in."""
        report, written = self.admission((1, 44, ["TR"], "tt1", 40939))
        self.assertEqual(written, {"movie:1"})
        self.assertEqual((report["admittedByImdb"], report["admittedByTmdb"], report["belowFloor"]), (1, 0, 0))
        self.assertEqual(self.checkpoint()["processed"], ["movie:1"])

    def test_a_title_only_tmdb_admits_is_admitted(self):
        """`El Señor de los Cielos`: IMDb undercounts it. The union keeps everything TMDB's floor admits."""
        report, written = self.admission((1, 3650, ["US"], "tt1", 1648), (2, 60, ["US"], None, 0))
        self.assertEqual(written, {"movie:1", "movie:2"})
        self.assertEqual((report["admittedByTmdb"], report["admittedByImdb"], report["admittedByBoth"]), (2, 0, 0))

    def test_a_title_both_admit_is_counted_as_both(self):
        report, _written = self.admission((1, 500, ["US"], "tt1", 5000))
        self.assertEqual((report["admittedByBoth"], report["admittedByTmdb"], report["admittedByImdb"]), (1, 0, 0))

    def test_a_title_neither_admits_is_refused_and_stays_pending(self):
        """Below every floor is a verdict about today, so it is not checkpointed — as before the union."""
        report, written = self.admission((1, 49, ["US"], "tt1", 1999))
        self.assertEqual(written, set())
        self.assertEqual((report["belowFloor"], report["count"], report["remaining"]), (1, 0, 1))
        self.assertEqual(self.checkpoint()["processed"], [])

    def test_each_tier_has_its_own_floors(self):
        """A regional origin clears at 15 TMDB or 500 IMDb; any other origin needs 50 or 2,000."""
        report, written = self.admission(
            (1, 20, ["FR"], None, 0),          # regional, TMDB 20 ≥ 15
            (2, 20, ["US"], None, 0),          # worldwide, TMDB 20 < 50, no IMDb id
            (3, 5, ["BR"], "tt3", 600),        # regional, IMDb 600 ≥ 500
            (4, 5, ["US"], "tt4", 600),        # worldwide, IMDb 600 < 2,000
            (5, 5, ["US"], "tt5", 2500))       # worldwide, IMDb 2,500 ≥ 2,000
        self.assertEqual(written, {"movie:1", "movie:3", "movie:5"})
        self.assertEqual(report["belowFloor"], 2)

    def test_the_floors_a_run_names_are_the_ones_it_judges_by(self):
        bodies = {"/movie/1": detail(1, votes=30, origin_country=["US"])}
        self.imdb_ids[("movie", 1)] = "tt1"
        self.imdb_votes["tt1"] = 900
        report = self.run_batch(bodies, [("movie", 1)], floors=enrich.floor_rules.given(imdb=800))
        self.assertEqual(report["admittedByImdb"], 1)

    def test_the_gate_judges_by_the_count_on_the_worklist_row(self):
        """`/discover` stated it when the universe was built, and that is the query the floor selected on.
        Reading it back off the detail call asks TMDB the same number a second time, per title."""
        bodies = {"/movie/1": detail(1, votes=5), "/movie/2": detail(2, votes=500)}
        report = self.run_batch(bodies, [("movie", 1), ("movie", 2)],
                                votes={("movie", 1): 500, ("movie", 2): 5})
        self.assertEqual(set(self.rows()), {"movie:1"}, "the worklist's count decides, not the record's")
        self.assertEqual((report["admittedByTmdb"], report["belowFloor"]), (1, 1))
        self.assertEqual(report["votesFromWorklist"], 2)

    def test_a_row_that_states_no_count_falls_back_to_the_detail_call(self):
        """An export universe is built from the daily dump, which states popularity and not votes."""
        report = self.run_batch({"/movie/1": detail(1, votes=500)}, [("movie", 1)])
        self.assertEqual(set(self.rows()), {"movie:1"})
        self.assertEqual(report["votesFromWorklist"], 0)

    def test_a_title_no_tmdb_count_is_stated_for_is_still_admitted_on_imdbs(self):
        """The union's IMDb half exists for the titles TMDB undercounts, so a title neither the worklist
        nor the record names a count for is judged on IMDb's alone — not compared against a floor."""
        record = dict(tmdb_record(1), voteCount=None, originCountry=["US"])
        ratings = enrich.imdb.Ratings({"tt1": 5000}, "unchanged")
        self.imdb_ids[("movie", 1)] = "tt1"
        admitted, _short, from_worklist = enrich.admit([record], enrich.floor_rules.DEFAULT, ratings,
                                                       self.cache, {})
        self.assertEqual((admitted, from_worklist), ({"movie:1": enrich.IMDB}, 0))

    def test_a_title_with_no_imdb_id_is_judged_on_tmdb_alone_and_counted(self):
        report, written = self.admission((1, 30, ["US"], None, 0), (2, 80, ["US"], None, 0))
        self.assertEqual(written, {"movie:2"})
        self.assertEqual(report["shortOfImdbId"], 1, "only a title TMDB left short needed the id")

    def test_the_gate_is_decided_before_the_expensive_work(self):
        """A refused title is never mapped to its articles and no plot is fetched for it: extra candidates
        cost an id lookup, not a plot fetch and a classification."""
        self.mapping[("movie", 1)] = {"article": "Kept"}
        self.mapping[("movie", 2)] = {"article": "Refused"}
        self.plots[("Kept", "en")] = found("k" * 300)
        self.admission((1, 44, ["TR"], "tt1", 40939), (2, 10, ["US"], "tt2", 10))
        self.assertEqual(self.mapping_calls, [("movie", [1])])
        self.assertEqual(self.plot_calls, [("Kept", "en")])
        self.assertEqual(self.imdb_id_calls, [("movie", [1, 2])], "one lookup for the batch, per media")

    def test_a_dump_that_cannot_be_had_admits_on_tmdb_alone_and_says_so(self):
        """Not a refusal: the union's TMDB half is the floor the pipeline ran on before, so nothing it admits
        is lost, and a title only IMDb admits is below-floor — never checkpointed — until a dump arrives."""
        self.dump = enrich.imdb.Unavailable("HTTP 503 for https://datasets.imdbws.com/title.ratings.tsv.gz")
        with mock.patch("sys.stderr") as stderr:
            report, written = self.admission((1, 500, ["US"], "tt1", 5000), (2, 44, ["TR"], "tt2", 40939))
        self.assertEqual(written, {"movie:1"})
        self.assertEqual((report["admittedByTmdb"], report["admittedByBoth"], report["belowFloor"]), (1, 0, 1))
        self.assertTrue(report["imdbGate"].startswith("off: "), report["imdbGate"])
        self.assertIn("HTTP 503", report["imdbGate"])
        self.assertEqual(self.imdb_id_calls, [], "no id is worth looking up with nothing to judge it by")
        self.assertIn("IMDb half", "".join(str(c) for c in stderr.write.call_args_list))
        with open(os.path.join(self.out, "enrich-log.txt")) as fh:
            self.assertIn("imdb-gate off: HTTP 503", fh.read())
        self.assertEqual(self.checkpoint()["processed"], ["movie:1"], "the IMDb-only title stays pending")

    def test_the_report_names_the_dumps_freshness(self):
        report, _written = self.admission((1, 500, ["US"], None, 0))
        self.assertEqual(report["imdbGate"], "unchanged")

    def test_a_failed_imdb_id_lookup_aborts_the_batch_and_writes_nothing(self):
        with mock.patch.object(enrich.wikidata, "imdb_ids", side_effect=http.HTTPError(0, "x")):
            with self.assertRaises(enrich.Aborted):
                self.run_batch({"/movie/1": detail(1, votes=10)}, [("movie", 1)])
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
        self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

    def test_no_imdb_count_is_written_anywhere(self):
        """IMDb's licence is non-transferable: its counts decide admission and go nowhere. Not the batch,
        which the corpus and the store are built from; not the log or the report either."""
        report, _written = self.admission((1, 44, ["TR"], "tt1", 987654), (2, 500, ["US"], "tt2", 876543))
        with open(enrich.batch_path(self.out, 1), encoding="utf-8") as fh:
            batch = fh.read()
        with open(os.path.join(self.out, "enrich-log.txt"), "a+", encoding="utf-8") as fh:
            fh.seek(0)
            logged = fh.read()
        for text in (batch, logged, json.dumps(report)):
            self.assertNotIn("987654", text)
            self.assertNotIn("876543", text)

    # -- the checkpoint -------------------------------------------------------------------------------

    def test_transient_and_below_floor_ids_stay_pending_and_the_rest_are_checkpointed(self):
        self.mapping[("movie", 4)] = {"article": "Blip"}
        self.plots[("Blip", "en")] = http.HTTPError(503, "x")
        report = self.run_batch({"/movie/1": http.HTTPError(429, "x"), "/movie/2": detail(2, votes=10),
                                 "/movie/3": http.HTTPError(404, "x"), "/movie/4": detail(4),
                                 "/movie/5": detail(5)},
                                [("movie", i) for i in range(1, 6)])
        self.assertEqual(sorted(self.checkpoint()["processed"]), ["movie:3", "movie:5"])
        self.assertEqual((report["deferred"], report["belowFloor"], report["failures"],
                          report["remaining"], report["count"]), (2, 1, 1, 3, 1))

    def test_a_refused_tmdb_key_stops_the_batch_and_checkpoints_nothing(self):
        """A revoked key answers 401 for every id. Read as per-title failures, one batch checkpointed 150 of
        150 with `remaining` falling, and the drain would have marked the whole universe dead."""
        for status in (401, 403):
            with self.subTest(status=status):
                bodies = {f"/movie/{i}": detail(i) for i in range(1, 40)}
                bodies["/movie/7"] = http.HTTPError(status, "https://api.themoviedb.org/3/movie/7")
                with self.assertRaises(StageError) as refused:
                    self.run_batch(bodies, [("movie", i) for i in range(1, 40)])
                self.assertIn("TMDB_API_KEY", str(refused.exception))
                self.assertIn(f"HTTP {status}", str(refused.exception))
                self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
                self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

    def test_every_id_refused_is_one_refusal_not_a_batch_of_failures(self):
        class Revoked:
            def get(self, path, params=None):
                raise http.HTTPError(401, "https://api.themoviedb.org/3" + path)
        with self.assertRaises(StageError):
            enrich.run(self.worklist(*[("movie", i) for i in range(1, 501)]), self.out, client=Revoked(),
                       cache=self.cache)
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))

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

    def killed_at_the_checkpoint(self, when=lambda: True):
        """`write_atomically` that dies at the checkpoint while `when()` holds — a SIGKILL between the batch
        write and the checkpoint write, as close as a test can put one."""
        real = enrich.caching.write_atomically

        def dying(path, body):
            if path == enrich.checkpoint_path(self.out) and when():
                raise Killed()
            real(path, body)
        return mock.patch.object(enrich.caching, "write_atomically", dying)

    def keys_by_batch(self):
        found = {}
        for number, name in enrich.batches(os.path.join(self.out, "enriched")):
            found[name] = sorted(self.rows(number))
        return found

    def test_a_death_between_the_batch_and_the_checkpoint_writes_no_key_twice(self):
        """Killed there, the checkpoint still named the batch as next, and the resumed run wrote the same 300
        keys again into the batch after it."""
        bodies = {f"/movie/{i}": detail(i) for i in range(1, 5)}
        entries = [("movie", i) for i in range(1, 5)]
        self.run_batch(bodies, entries, limit=2)
        with self.killed_at_the_checkpoint(), self.assertRaises(Killed):
            self.run_batch(bodies, entries, limit=2)
        self.assertEqual(sorted(self.keys_by_batch()), ["batch-1.json", "batch-2.json"])
        report = self.run_batch(bodies, entries, limit=2)
        self.assertEqual(report, {"remaining": 0, "count": 0})
        self.assertEqual(self.keys_by_batch(), {"batch-1.json": ["movie:1", "movie:2"],
                                                "batch-2.json": ["movie:3", "movie:4"]})

    def test_the_first_batch_is_covered_too(self):
        """With no checkpoint yet there is nothing to measure batch-1 against, so its number is reserved in a
        checkpoint before it is written."""
        bodies = {f"/movie/{i}": detail(i) for i in range(1, 3)}
        batch = enrich.batch_path(self.out, 1)
        with self.killed_at_the_checkpoint(when=lambda: os.path.exists(batch)), self.assertRaises(Killed):
            self.run_batch(bodies, [("movie", 1), ("movie", 2)])
        self.assertEqual(self.checkpoint()["nextBatch"], 1, "reserved, not advanced")
        self.assertEqual(self.run_batch(bodies, [("movie", 1), ("movie", 2)]), {"remaining": 0, "count": 0})
        self.assertEqual(list(self.keys_by_batch()), ["batch-1.json"])

    def test_a_checkpoint_killed_mid_write_leaves_the_previous_one_whole(self):
        """Written in place, a kill mid-write leaves a truncated checkpoint, which the next run refuses — the
        whole drain stops. Through a temp file and a rename it leaves the previous one."""
        bodies = {f"/movie/{i}": detail(i) for i in range(1, 5)}
        self.run_batch(bodies, [("movie", i) for i in range(1, 5)], limit=2)
        with open(enrich.checkpoint_path(self.out), "rb") as fh:
            before = fh.read()
        real = os.replace

        def dying(source, destination):
            if destination == enrich.checkpoint_path(self.out):
                raise Killed()
            real(source, destination)
        with mock.patch.object(enrich.caching.os, "replace", dying), self.assertRaises(Killed):
            self.run_batch(bodies, [("movie", i) for i in range(1, 5)], limit=2)
        with open(enrich.checkpoint_path(self.out), "rb") as fh:
            self.assertEqual(fh.read(), before)

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
        self.kinds[("movie", 1)] = ["anime television series"]
        self.assertEqual(self.run_batch({"/movie/1": detail(1)}, [("movie", 1)])["count"], 1)
        self.assertEqual(self.kind_calls, [], "nothing is asked when the flag is not passed")
        os.remove(enrich.checkpoint_path(self.out))
        report = self.run_batch({"/movie/1": detail(1)}, [("movie", 1)], exclude_anime=True)
        self.assertEqual((report["anime"], report["count"]), (1, 0))
        self.assertEqual(self.kind_calls, [("movie", [1])], "one query for the batch, per media")

    def test_anime_is_wikidatas_genres_and_types_not_tmdbs_keyword(self):
        """TMDB tagged it with keyword 210024 or read Animation plus `original_language: ja`. Wikidata says
        it in a P136 genre or a P31 type, which catches the co-productions TMDB's language test misses —
        `Ulysses 31` is French-Japanese, `Dogtanian` Spanish-Japanese."""
        self.kinds.update({("movie", 2): ["adventure anime and manga", "film"],
                           ("movie", 3): ["animated film"]})
        bodies = {"/movie/1": detail(1, original_language="ja", genres=[{"id": 16, "name": "Animation"}]),
                  "/movie/2": detail(2, original_language="fr"), "/movie/3": detail(3)}
        report = self.run_batch(bodies, [("movie", i) for i in (1, 2, 3)], exclude_anime=True)
        self.assertEqual(sorted(self.rows()), ["movie:1", "movie:3"], "TMDB's tags decide nothing")
        self.assertEqual(report["anime"], 1)
        self.assertEqual(self.kind_calls, [("movie", [1, 2, 3])],
                         "one query for the batch, per media — never one per title")

    def test_a_title_the_gate_refused_is_never_asked_about(self):
        """The same rule as the mapping: a refused title costs an id lookup, not a second query about what
        it is."""
        self.kinds[("movie", 2)] = ["anime film"]
        self.run_batch({"/movie/1": detail(1, votes=500), "/movie/2": detail(2, votes=1)},
                       [("movie", 1), ("movie", 2)], exclude_anime=True)
        self.assertEqual(self.kind_calls, [("movie", [1])])

    def test_anime_influenced_animation_is_not_anime(self):
        """Western animation drawn in the style. `lib/wikidata_facts.genre_map` sets `live-action/animated`
        aside from its animation rule for the same reason."""
        self.kinds[("movie", 1)] = ["anime-influenced animation"]
        report = self.run_batch({"/movie/1": detail(1)}, [("movie", 1)], exclude_anime=True)
        self.assertEqual((report["anime"], sorted(self.rows())), (0, ["movie:1"]))

    def test_a_failed_genre_lookup_aborts_the_batch_and_writes_nothing(self):
        """Swallowed, the run would keep every anime in a batch a person asked to have it dropped from."""
        with mock.patch.object(enrich.wikidata, "kinds", side_effect=http.HTTPError(0, "x")):
            with self.assertRaises(enrich.Aborted):
                self.run_batch({"/movie/1": detail(1)}, [("movie", 1)], exclude_anime=True)
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
        self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

    def test_a_title_with_a_tiny_tmdb_overview_is_enriched_like_any_other(self):
        """The stub check read a length `title_record` carried across the TMDB boundary for it alone.
        `overview` holds a Wikipedia plot or nothing, so the length said nothing about what the title would
        be grounded on — and over the whole repass it refused 3 titles out of 59,209."""
        self.mapping[("movie", 1)] = {"article": "One"}
        self.plots[("One", "en")] = found("W" * 200)
        report = self.run_batch({"/movie/1": detail(1, overview="."), "/movie/2": detail(2, overview="")},
                                [("movie", 1), ("movie", 2)])
        self.assertEqual(sorted(self.rows()), ["movie:1", "movie:2"])
        self.assertEqual(self.rows()["movie:1"]["overview"], "W" * 200)
        self.assertNotIn("noOverview", report)

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

    def test_the_floors_on_the_command_line_are_the_ones_the_batch_runs_at(self):
        with mock.patch.object(enrich, "run", return_value={"remaining": 0}) as ran, mock.patch("sys.stdout"):
            enrich.main(["--worklist", "w", "--out-dir", "o", "--vote-floor", "40", "--regional-vote-floor", "10",
                         "--imdb-floor", "1500", "--regional-imdb-floor", "300"])
            self.assertEqual(ran.call_args.args[2], enrich.floor_rules.Floors(40, 10, 1500, 300))
            enrich.main(["--worklist", "w", "--out-dir", "o"])
            self.assertEqual(ran.call_args.args[2], enrich.floor_rules.DEFAULT)

    def test_an_unknown_flag_is_refused(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            enrich.parser().parse_args(["--worklist", "w", "--out-dir", "o", "--plot-cap", "3"])


if __name__ == "__main__":
    unittest.main()
