#!/usr/bin/env python3
"""One enrichment batch — the rules each of which was bought by a run that went wrong.

The equivalence with the Swift pass it replaced is not asserted here; it was established by replay. With no
Enterprise bearer, a scratch copy of the response cache and outbound network denied, the merge-base
`taxonomy-backfill enrich` and this module enriched the same out-repass worklists, and every title neither
side had to fetch came out as the same record — two whole batches byte-identical under `cmp`. What IS
asserted here is each rule on its own, against stubbed Wikipedia/Wikidata and with every other upstream
refused, so a rule that stops holding fails by name rather than as a diff in a 300-title file.

The bytes are pinned separately (`Bytes`), against output captured from the Swift encoder itself: every
reader of a batch — `articles`, `embed`, the provenance backfill, the census — was written against that.
"""
import datetime
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


class Killed(BaseException):
    """A process death: nothing below `run` may catch it."""


#: The TMDB count a test's worklist row states unless it says otherwise: above every floor, so a title is
#: admitted and what is under test is what happens next.
VOTES = 500


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
        # How many Wikipedias have an article on each (media, tmdbId), and what each lookup asked, on which day.
        self.wikis, self.wiki_calls = {}, []
        # Which Wikidata items state each (media, tmdbId), what each item states, and the items each lookup
        # was told to leave out.
        # The first question a batch asks about a title, so what a batch took on is read off these calls.
        self.claimants, self.evidence, self.excluded, self.claimant_calls = {}, {}, [], []
        # Wikidata P495 per (media, tmdbId), which picks the tier where the worklist states none.
        self.origins, self.origin_calls = {}, []
        for target, stub in ((enrich.wikidata, "mapping"), (enrich.plot, "plot"), (enrich.wikidata, "wikipedias"),
                             (enrich.wikidata, "languages"), (enrich.wikidata, "kinds"),
                             (enrich.wikidata, "sources"), (enrich.wikidata, "origins"),
                             (enrich.wikidata, "claimants"), (enrich.wikidata, "item_evidence")):
            patch = mock.patch.object(target, stub, getattr(self, stub + "_stub"))
            patch.start()
            self.addCleanup(patch.stop)
        # The committed decisions name real ids (movie:3 among them), which the stubs here say nothing claims;
        # read here, they would refuse every batch that holds one of those ids as stale.
        patch = mock.patch.object(enrich.wikidata, "load_decisions", lambda path=None: {})
        patch.start()
        self.addCleanup(patch.stop)
        # The shipped catalogue names real ids (movie:2 among them); a test names the ones it means.
        self.catalogue, self.real_shipped = set(), enrich.shipped
        patch = mock.patch.object(enrich, "shipped", lambda path=None: frozenset(self.catalogue))
        patch.start()
        self.addCleanup(patch.stop)
        # Every upstream is behind a stub above. A request that reaches the wire — TMDB above all — fails.
        patch = mock.patch.object(http, "request", side_effect=AssertionError("an unstubbed upstream was asked"))
        patch.start()
        self.addCleanup(patch.stop)

    def origins_stub(self, ids, media, cache=None, excluded=None):
        self.origin_calls.append((media, sorted(ids)))
        return {i: self.origins[(media, i)] for i in ids if (media, i) in self.origins}

    def claimants_stub(self, ids, media, cache=None):
        self.claimant_calls.append((media, sorted(ids)))
        return {i: self.claimants[(media, i)] for i in ids if (media, i) in self.claimants}

    def item_evidence_stub(self, qids, media, cache=None):
        return {q: self.evidence[q] for q in qids if q in self.evidence}

    def wikipedias_stub(self, ids, media, day, cache=None, excluded=None):
        self.wiki_calls.append((media, sorted(ids), day))
        self.excluded.append(("wikipedias", media, excluded))
        return {i: self.wikis[(media, i)] for i in ids if (media, i) in self.wikis}

    def mapping_stub(self, ids, media, languages, cache=None, excluded=None):
        self.mapping_calls.append((media, sorted(ids)))
        self.excluded.append(("mapping", media, excluded))
        return {i: self.mapping[(media, i)] for i in ids if (media, i) in self.mapping}

    def languages_stub(self, ids, media, cache=None, excluded=None):
        self.language_calls.append((media, sorted(ids)))
        self.excluded.append(("languages", media, excluded))
        return {i: self.languages[(media, i)] for i in ids if (media, i) in self.languages}

    def kinds_stub(self, ids, media, cache=None, excluded=None):
        self.kind_calls.append((media, sorted(ids)))
        self.excluded.append(("kinds", media, excluded))
        return {i: self.kinds[(media, i)] for i in ids if (media, i) in self.kinds}

    def sources_stub(self, ids, media, cache=None, excluded=None):
        self.source_calls.append((media, sorted(ids)))
        self.excluded.append(("sources", media, excluded))
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

    def worklist(self, *entries, votes=None, tiers=None, planned=()):
        """A worklist whose rows state `VOTES` TMDB votes — the shape `/discover` and the delta write —
        unless `votes` names another count per `(media, id)`; None there is an export row, which states
        none. `tiers` names a row's `regional` answer; a row it does not name states none. A row `planned`
        names says `"admitted": true`, as a plan built from an earlier out-dir writes it."""
        path = os.path.join(self.out, "worklist.json")
        rows = []
        for media, tmdb_id in entries:
            row = {"tmdbId": tmdb_id, "mediaType": media}
            count = (votes or {}).get((media, tmdb_id), VOTES)
            if count is not None:
                row["voteCount"] = count
            if tiers and (media, tmdb_id) in tiers:
                row["regional"] = tiers[(media, tmdb_id)]
            if (media, tmdb_id) in planned:
                row["admitted"] = True
            rows.append(row)
        with open(path, "w") as fh:
            json.dump(rows, fh)
        return path

    def run_batch(self, entries, votes=None, tiers=None, planned=(), **kwargs):
        return enrich.run(self.worklist(*entries, votes=votes, tiers=tiers, planned=planned), self.out,
                          cache=self.cache, **kwargs)

    def rows(self, batch_id=1):
        with open(enrich.batch_path(self.out, batch_id)) as fh:
            return {f"{r['mediaType']}:{r['tmdbId']}": r for r in json.load(fh)}

    def checkpoint(self):
        with open(enrich.checkpoint_path(self.out)) as fh:
            return json.load(fh)

    # -- ToS ------------------------------------------------------------------------------------------

    def test_the_overview_is_a_wikipedia_plot_or_nothing(self):
        self.mapping[("movie", 1)] = {"article": "One"}
        self.plots[("One", "en")] = found("W" * 200)
        self.run_batch([("movie", 1), ("movie", 2)])
        rows = self.rows()
        self.assertEqual(rows["movie:1"]["overview"], "W" * 200)
        self.assertEqual((rows["movie:2"]["overview"], rows["movie:2"]["hasWikiPlot"]), ("", False))
        self.assertEqual([name for name in rows["movie:2"] if "overview" in name.lower()], ["overview"])

    def test_tmdb_is_asked_nothing_and_no_batch_row_holds_a_tmdb_field(self):
        """oxyc/den-dataset#53. A title is its worklist row; `setUp` fails any request that reaches the wire.
        Neither the count nor the tier the gate read is written, and `animated` is Wikidata's."""
        self.mapping[("movie", 1)] = {"article": "One"}
        self.plots[("One", "en")] = found("W" * 200)
        self.kinds[("movie", 1)] = ["animated film", "film"]
        report = self.run_batch([("movie", 1)], tiers={("movie", 1): True})
        self.assertEqual((report["admittedByTmdb"], report["votesFromWorklist"]), (1, 1))
        row = self.rows()["movie:1"]
        tmdb_sourced = {"title", "year", "genres", "keywords", "keywordIDs", "director", "topCast",
                        "originalLanguage", "voteCount", "genreIDs", "originCountry", "regional"}
        self.assertEqual(tmdb_sourced & set(row), set())
        self.assertIs(row["animated"], True)
        self.assertEqual(enrich.NOT_WRITTEN, frozenset(tmdb_sourced))

    def test_animated_is_wikidatas_genres_and_types(self):
        """TMDB's genre 16 said it before. A live-action film with animated sequences is not animated, and a
        title Wikidata names no genre or type for is unknown — absent, never `false`."""
        self.kinds.update({("movie", 1): ["adult animated television series"], ("movie", 2): ["anime film"],
                           ("movie", 3): ["live-action/animated", "fantasy film"], ("movie", 4): ["drama film"]})
        self.run_batch([("movie", i) for i in range(1, 6)])
        rows = self.rows()
        self.assertEqual([rows[f"movie:{i}"].get("animated") for i in range(1, 6)], [True, True, False, False, None])
        self.assertNotIn("animated", rows["movie:5"])
        self.assertEqual(self.kind_calls, [("movie", [1, 2, 3, 4, 5])], "one query for the batch, per media")

    def test_creators_are_wikidatas_or_none(self):
        """`createdBy` is composed into the embedding document. With no Wikidata P170 it is EMPTY."""
        self.mapping[("tv", 1)] = {"article": "One", "creators": ["Wikidata Creator"]}
        self.mapping[("tv", 2)] = {"article": "Two"}
        self.plots[("One", "en")] = found("W" * 200)
        self.run_batch([("tv", 1), ("tv", 2)])
        rows = self.rows()
        self.assertEqual((rows["tv:1"]["createdBy"], rows["tv:2"]["createdBy"]), (["Wikidata Creator"], []))

    # -- candidate selection --------------------------------------------------------------------------

    def test_an_own_premise_beats_a_longer_source_work(self):
        """Gen V's own Premise is 668 characters; its P144 work is The Boys, whose 3,164 won on length and
        described the parent series instead. A plot of its own that clears the floor is never out-read."""
        self.mapping[("tv", 1)] = {"article": "Gen V", "sourceArticle": "The Boys (TV series)"}
        self.plots[("Gen V", "en")] = found("g" * 668, resolved="Gen V")
        self.plots[("The Boys (TV series)", "en")] = found("b" * 3164, resolved="The Boys (TV series)")
        self.run_batch([("tv", 1)])
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
        self.run_batch([("tv", 1)])
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
        self.run_batch([("tv", 1), ("tv", 2)])
        rows = self.rows()
        self.assertEqual([(rows[k]["plotArticleRole"], rows[k]["plotArticle"]) for k in ("tv:1", "tv:2")],
                         [("source-work", "Book")] * 2)

    def test_a_source_work_that_redirects_is_not_read_as_one(self):
        """A P144 sitelink that lands elsewhere is not the work either: `La noia` landed on the article about
        its author."""
        self.mapping[("movie", 1)] = {"sourceArticle": "La noia"}
        self.plots[("La noia", "en")] = found("a" * 900, resolved="Alberto Moravia")
        self.run_batch([("movie", 1)])
        row = self.rows()["movie:1"]
        self.assertEqual((row["hasWikiPlot"], row["noPlotReason"]), (False, "noArticle"))

    def test_an_own_article_that_is_enough_stops_the_search(self):
        """A well-covered adaptation must not pay for a second fetch it cannot use."""
        self.mapping[("movie", 1)] = {"article": "Own", "sourceArticle": "Book",
                                      "articlesByLang": {"de": "Eigen"}}
        self.plots[("Own", "en")] = found("o" * 1000, resolved="Own")
        self.run_batch([("movie", 1)])
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
        self.run_batch([("tv", 1), ("tv", 2)])
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
        self.run_batch([("movie", 1), ("movie", 2)])
        rows = self.rows()
        self.assertEqual([(rows[k]["plotArticleRole"], rows[k]["plotArticle"]) for k in ("movie:1", "movie:2")],
                         [("source-work", "The Wonderful Wizard of Oz"), ("source-work", "Novel")])
        self.assertNotIn(("The Wizard of Oz (1939 film)", "en"), self.plot_calls)

    def test_a_source_work_the_lookup_says_nothing_about_is_read_as_before(self):
        """Unknown is not a screen work: a pick Wikidata's answer does not name is read."""
        self.mapping[("movie", 1)] = {"sourceArticle": "Book"}
        self.sources[("movie", 1)] = {"Something Else": True}
        self.plots[("Book", "en")] = found("b" * 900)
        self.run_batch([("movie", 1)])
        self.assertEqual(self.rows()["movie:1"]["plotArticle"], "Book")

    def test_the_source_lookup_is_one_query_per_media_for_the_titles_with_a_source_work(self):
        self.mapping.update({("movie", 1): {"sourceArticle": "A"}, ("movie", 2): {"article": "B"},
                             ("movie", 3): {"sourceArticle": "C"}, ("tv", 4): {"sourceArticle": "D"}})
        self.run_batch([("movie", 1), ("movie", 2), ("movie", 3), ("tv", 4)])
        self.assertEqual(self.source_calls, [("movie", [1, 3]), ("tv", [4])])

    def test_a_failed_source_lookup_aborts_the_batch_and_writes_nothing(self):
        """Swallowed, every remake in the batch would be grounded on its original again."""
        self.mapping[("movie", 1)] = {"sourceArticle": "Original"}
        with mock.patch.object(enrich.wikidata, "sources", side_effect=http.HTTPError(0, "x")):
            with self.assertRaises(enrich.Aborted):
                self.run_batch([("movie", 1)])
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
        self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

    def test_a_source_work_that_is_the_only_candidate_is_still_a_source_work(self):
        """Reading the role off the position would call it `own`: it sits first when there is no English
        article, which is 4% of titles."""
        self.mapping[("movie", 1)] = {"sourceArticle": "Book"}
        self.plots[("Book", "en")] = found("b" * 300, resolved="Book")
        self.run_batch([("movie", 1)])
        self.assertEqual(self.rows()["movie:1"]["plotArticleRole"], "source-work")

    def test_the_fallback_reads_the_titles_own_language_first_then_the_rest_in_order(self):
        self.mapping[("movie", 1)] = {"article": "Thin", "articlesByLang": {"it": "Film", "de": "Film",
                                                                             "fr": "Film"}}
        self.languages[("movie", 1)] = ["fr"]
        self.plots[("Film", "de")] = found("d" * 300, language="de")
        self.run_batch([("movie", 1)])
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
        self.run_batch([("movie", 1), ("movie", 2), ("tv", 3)])
        self.assertEqual(self.plot_calls[:5],
                         [("Thin", "en"), ("F", "fr"), ("F", "sv"), ("F", "de"), ("F", "it")])
        self.assertEqual(self.language_calls, [("movie", [1, 2]), ("tv", [3])],
                         "one query for the batch, per media — never one per title")

    def test_a_title_wikidata_states_no_language_for_reads_its_sitelinks_in_code_order(self):
        """1,164 of the 12,611 corpus titles grounded this way have no P364. Nothing is preferred rather
        than TMDB's code being preferred."""
        self.mapping[("movie", 1)] = {"article": "Thin", "articlesByLang": {"it": "F", "de": "F"}}
        self.run_batch([("movie", 1)])
        self.assertEqual(self.plot_calls, [("Thin", "en"), ("F", "de"), ("F", "it")])

    def test_a_failed_language_lookup_aborts_the_batch_and_writes_nothing(self):
        """It asks the same service as the mapping. Swallowed, it would silently reorder every fallback in
        the batch to code order and record nothing about it."""
        with mock.patch.object(enrich.wikidata, "languages", side_effect=http.HTTPError(0, "x")):
            with self.assertRaises(enrich.Aborted):
                self.run_batch([("movie", 1)])
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
        self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

    def test_a_title_with_no_english_article_reaches_the_fallback(self):
        """The case the fallback exists for — two thirds of the plotless films have no English article. The
        Swift pass returned `noArticle` before trying it; 215 of 348 such titles in the replay had one."""
        self.mapping.update({("movie", 1): {"articlesByLang": {"de": "Schachnovelle"}},
                             ("movie", 2): {"articlesByLang": {"it": "Senza"}}})
        self.plots[("Schachnovelle", "de")] = found("h" * 300, resolved="Schachnovelle", language="de")
        self.run_batch([("movie", 1), ("movie", 2)])
        rows = self.rows()
        self.assertEqual((rows["movie:1"]["plotArticleRole"], rows["movie:1"]["plotLanguage"]),
                         ("own-other-language", "de"))
        self.assertEqual(rows["movie:2"]["noPlotReason"], "noSection",
                         "an article that exists and has no plot section is not `noArticle`")

    def test_the_fallback_is_skipped_when_english_already_has_enough(self):
        self.mapping[("movie", 1)] = {"article": "Own", "articlesByLang": {"de": "Eigen"}}
        self.plots[("Own", "en")] = found("o" * 999)
        self.plots[("Eigen", "de")] = found("d" * 999, language="de")
        self.run_batch([("movie", 1)])
        self.assertIn(("Eigen", "de"), self.plot_calls, "999 is not enough, so the fallback is asked")
        self.assertEqual(self.rows()["movie:1"]["plotArticleRole"], "own", "a tie keeps the earlier")

    def test_the_longest_other_language_article_wins_not_the_first(self):
        """The fallback reads every sitelink until one is enough, and keeps the longest — the title's own
        language is asked first, not preferred at any length."""
        self.mapping[("movie", 1)] = {"articlesByLang": {"de": "Kurz", "fr": "Long"}}
        self.languages[("movie", 1)] = ["de"]
        self.plots[("Kurz", "de")] = found("d" * 300, resolved="Kurz", language="de")
        self.plots[("Long", "fr")] = found("f" * 600, resolved="Long", language="fr")
        self.run_batch([("movie", 1)])
        self.assertEqual(self.plot_calls, [("Kurz", "de"), ("Long", "fr")])
        row = self.rows()["movie:1"]
        self.assertEqual((row["plotLanguage"], row["plotArticle"], len(row["overview"])), ("fr", "Long", 600))

    def test_another_language_beats_a_thin_english_article_when_it_is_longer(self):
        """A thin English article is what the fallback is FOR: it runs below 1,000 characters, and what it
        finds competes on length with what English gave."""
        self.mapping[("movie", 1)] = {"article": "Thin", "articlesByLang": {"it": "Lungo"}}
        self.plots[("Thin", "en")] = found("e" * 200, resolved="Thin")
        self.plots[("Lungo", "it")] = found("i" * 500, resolved="Lungo", language="it")
        self.run_batch([("movie", 1)])
        row = self.rows()["movie:1"]
        self.assertEqual((row["plotArticleRole"], row["plotLanguage"], row["plotArticle"]),
                         ("own-other-language", "it", "Lungo"))

    def test_the_fallback_stops_at_the_first_other_language_article_that_is_enough(self):
        """The same stop as the English loop: once a sitelink gives 1,000 characters, the rest are not
        fetched, even when one of them is longer."""
        self.mapping[("movie", 1)] = {"articlesByLang": {"de": "Genug", "fr": "Plus"}}
        self.plots[("Genug", "de")] = found("d" * 1000, resolved="Genug", language="de")
        self.plots[("Plus", "fr")] = found("f" * 3000, resolved="Plus", language="fr")
        self.run_batch([("movie", 1)])
        self.assertEqual(self.plot_calls, [("Genug", "de")])
        self.assertEqual(self.rows()["movie:1"]["plotLanguage"], "de")

    def test_a_thin_article_found_only_in_another_language_is_below_the_floor(self):
        """The class 04129a9 opened: a title whose ONLY article is on another Wikipedia, with a plot section
        too short to ground on. Its section was found, so it is `belowFloor` — a threshold decision — and
        not `noSection`, which sends it to whoever writes heading rules. ~5,778 titles take this path."""
        self.mapping[("movie", 1)] = {"articlesByLang": {"de": "Dünn"}}
        self.plots[("Dünn", "de")] = found("d" * 80, resolved="Dünn", language="de")
        self.run_batch([("movie", 1)])
        row = self.rows()["movie:1"]
        self.assertEqual((row["hasWikiPlot"], row["noPlotReason"], row["overview"]), (False, "belowFloor", ""))

    def test_a_plotless_title_starts_from_an_empty_overview_whatever_the_record_carries(self):
        """`overview` holds a Wikipedia plot or nothing. Emptied outright rather than filtered, so whatever a
        record copied forward from an older batch carries cannot reach it."""
        record = {"tmdbId": 1, "mediaType": "movie", "overview": "STALE PROSE " * 5}
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
        self.run_batch([("movie", i) for i in range(1, 6)])
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
        self.run_batch([("movie", i) for i in range(1, 5)])
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
        self.run_batch([("movie", 1), ("movie", 2)])
        rows = self.rows()
        self.assertEqual((rows["movie:1"]["hasWikiPlot"], rows["movie:1"]["noPlotReason"]), (False, "noArticle"))
        self.assertEqual((rows["movie:2"]["plotArticleRole"], rows["movie:2"]["plotArticle"],
                          rows["movie:2"]["plotArticleRedirected"]), ("own-other-language", "Jarhead 3", False))

    def test_an_unseen_redirect_stays_unknown(self):
        """The Enterprise path names no page, so it cannot be refused as a redirect, and absent is UNKNOWN —
        never `false`."""
        self.mapping[("movie", 2)] = {"article": "Wire"}
        self.plots[("Wire", "en")] = found("w" * 300, resolved=None, revid=None)
        self.run_batch([("movie", 2)])
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
            report = self.run_batch([("movie", i) for i in (1, 2, 3)])
            self.assertEqual((report["plotsFromEnterprise"], report["plotsFromActionApi"], report["wikiPlot"],
                              report["enterpriseRequests"]), (1, 1, 2, 1))
            self.assertNotIn("source", json.dumps(self.rows()), "which source served is reported, not recorded")
            self.mapping[("movie", 4)] = {"article": "A4"}
            self.plots[("A4", "en")] = dict(found("d" * 300, resolved=None, revid=None),
                                            source=enrich.plot.ENTERPRISE)
            report = self.run_batch([("movie", 4)])
        self.assertEqual(report["enterpriseRequests"], 1, "this batch's requests, not the process's")

    def test_a_malformed_reserve_refuses_a_run_that_holds_a_bearer(self):
        """Read inside a grounding worker, the error would be swallowed as one more failed fast path."""
        with mock.patch.object(enrich.enterprise, "gate", enrich.enterprise.Gate()), \
                mock.patch.dict(os.environ, {"DEN_ENTERPRISE_RESERVE": "lots"}):
            with self.assertRaises(ValueError):
                self.run_batch([("movie", 1)], token="bearer")
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))

    # -- admission: TMDB's count OR the Wikipedia count ------------------------------------------------

    def admission(self, *titles):
        """One batch of `(tmdbId, TMDB votes, regional, Wikipedias or None)` movies, as a `/discover`
        worklist states the first two — None for a title Wikidata has no item for. Returns the report and
        the keys the batch wrote."""
        for tmdb_id, _votes, _regional, wikis in titles:
            if wikis is not None:
                self.wikis[("movie", tmdb_id)] = wikis
        report = self.run_batch([("movie", t[0]) for t in titles], votes={("movie", t[0]): t[1] for t in titles},
                                tiers={("movie", t[0]): t[2] for t in titles})
        return report, set(self.rows()) if os.path.exists(enrich.batch_path(self.out, 1)) else set()

    def test_a_title_only_its_wikipedias_admit_is_admitted(self):
        """`Chang` (1927): 25 TMDB votes, an article on 22 Wikipedias. TMDB's floor alone never let it in."""
        report, written = self.admission((1, 25, False, 22))
        self.assertEqual(written, {"movie:1"})
        self.assertEqual((report["admittedByWikipedias"], report["admittedByTmdb"], report["belowFloor"]),
                         (1, 0, 0))
        self.assertEqual(self.checkpoint()["processed"], ["movie:1"])

    def test_a_title_tmdb_admits_is_admitted_without_asking_wikidata(self):
        """The union keeps everything TMDB's floor admits, however few Wikipedias cover it."""
        report, written = self.admission((1, 3650, False, 0), (2, 60, False, None))
        self.assertEqual(written, {"movie:1", "movie:2"})
        self.assertEqual((report["admittedByTmdb"], report["admittedByWikipedias"]), (2, 0))
        self.assertEqual(self.wiki_calls, [], "nothing was left short, so nothing is asked")

    def test_a_title_neither_admits_is_refused_and_judged_for_the_day(self):
        """Below every floor is a verdict about today: not processed, which would make it permanent, but
        recorded with what it was judged on, so this drain stops asking and a later day asks again."""
        report, written = self.admission((1, 49, False, 4))
        self.assertEqual(written, set())
        self.assertEqual((report["belowFloor"], report["count"], report["remaining"]), (1, 0, 0))
        checkpoint = self.checkpoint()
        self.assertEqual(checkpoint["processed"], [])
        self.assertEqual(sorted(checkpoint["judgedBelow"]), ["movie:1"])
        self.assertEqual(checkpoint["judgedBelow"]["movie:1"],
                         {"on": checkpoint["judgedBelow"]["movie:1"]["on"], "votes": 49, "floors": [50, 15, 10, 3]})

    def test_each_tier_has_its_own_floors(self):
        """A regional title clears at 15 TMDB votes or 3 Wikipedias; any other needs 50 or 10."""
        report, written = self.admission(
            (1, 20, True, 0),           # regional, TMDB 20 >= 15
            (2, 20, False, None),       # worldwide, TMDB 20 < 50, no Wikidata item
            (3, 5, True, 3),            # regional, 3 Wikipedias >= 3
            (4, 5, False, 9),           # worldwide, 9 < 10
            (5, 5, False, 10))          # worldwide, 10 >= 10
        self.assertEqual(written, {"movie:1", "movie:3", "movie:5"})
        self.assertEqual(report["belowFloor"], 2)
        self.assertEqual(self.origin_calls, [], "every row stated its tier, so Wikidata is not asked for one")

    def test_the_worklists_tier_decides_over_wikidatas_origin(self):
        """`regional` is what `/discover` said. A co-production TMDB files under GB and Wikidata under US
        (`Family Tree`) stays regional, and P495 is not asked."""
        self.origins[("movie", 1)] = ["US"]
        _report, written = self.admission((1, 20, True, None))
        self.assertEqual(written, {"movie:1"})
        self.assertEqual(self.origin_calls, [])

    def test_a_row_with_no_tier_is_judged_in_the_tier_wikidatas_p495_names(self):
        """An export row, or a hand-made list: `/discover` stated nothing, so Wikidata's country of origin
        picks the floors. Asked once per media, and only about the titles whose count does not clear every
        tier's TMDB floor anyway."""
        self.origins.update({("movie", 1): ["FR"], ("movie", 2): ["US"], ("movie", 3): ["US"]})
        self.wikis.update({("movie", 1): 3, ("movie", 2): 3, ("movie", 4): 3})
        report = self.run_batch([("movie", n) for n in (1, 2, 3, 4)],
                                votes={("movie", 1): None, ("movie", 2): None, ("movie", 3): 500,
                                       ("movie", 4): 20})
        self.assertEqual(set(self.rows()), {"movie:1", "movie:3"})
        self.assertEqual(self.origin_calls, [("movie", [1, 2, 4])])
        self.assertEqual((report["admittedByTmdb"], report["admittedByWikipedias"], report["belowFloor"]), (1, 1, 2))
        self.assertEqual([key for key, row in self.rows().items() if "originCountry" in row], [],
                         "read to pick the floors, never written")

    def test_a_failed_origin_lookup_aborts_the_batch_and_writes_nothing(self):
        with mock.patch.object(enrich.wikidata, "origins", side_effect=http.HTTPError(0, "x")):
            with self.assertRaises(enrich.Aborted):
                self.run_batch([("movie", 1)], votes={("movie", 1): None})
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
        self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

    def test_the_floors_a_run_names_are_the_ones_it_judges_by(self):
        self.wikis[("movie", 1)] = 2
        report = self.run_batch([("movie", 1)], votes={("movie", 1): 30}, tiers={("movie", 1): False},
                                floors=enrich.floor_rules.given(wikipedias=2))
        self.assertEqual(report["admittedByWikipedias"], 1)

    def test_the_gate_judges_by_the_count_on_the_worklist_row(self):
        """`/discover` stated it when the universe was built, and that is the query the floor selected on."""
        report = self.run_batch([("movie", 1), ("movie", 2)], votes={("movie", 1): 500, ("movie", 2): 5})
        self.assertEqual(set(self.rows()), {"movie:1"})
        self.assertEqual((report["admittedByTmdb"], report["belowFloor"]), (1, 1))
        self.assertEqual(report["votesFromWorklist"], 2)

    def test_a_row_that_states_no_count_is_judged_on_its_wikipedias_alone(self):
        """An export universe is built from the daily dump, which states popularity and not votes. Nothing
        else is asked for one: TMDB's detail call supplied it once, and is gone (oxyc/den-dataset#53)."""
        self.wikis.update({("movie", 1): 12, ("movie", 2): 9})
        report = self.run_batch([("movie", 1), ("movie", 2)], votes={("movie", 1): None, ("movie", 2): None})
        self.assertEqual(set(self.rows()), {"movie:1"})
        self.assertEqual((report["votesFromWorklist"], report["admittedByWikipedias"], report["belowFloor"]),
                         (0, 1, 1))
        self.assertEqual(self.checkpoint()["judgedBelow"]["movie:2"]["votes"], None)

    def test_a_shipped_title_on_a_list_of_ids_keeps_its_admission_and_a_new_one_does_not(self):
        """A re-fetch plan or `pipeline/build_worklist.py` list names titles an earlier build admitted, and
        carries no TMDB count. Judged on its Wikipedia count alone a shipped title with few articles was
        refused — 61 of 200 in a replay — unless someone remembered the floor flags. A new title with the
        same count is judged as any other, and a shipped title whose row states a count is judged on it."""
        self.catalogue.update({"movie:1", "movie:3"})
        self.wikis.update({("movie", 1): 1, ("movie", 2): 1, ("movie", 3): 1})
        report = self.run_batch([("movie", 1), ("movie", 2), ("movie", 3)],
                                votes={("movie", 1): None, ("movie", 2): None, ("movie", 3): 10},
                                tiers={("movie", 3): False})
        self.assertEqual(set(self.rows()), {"movie:1"})
        self.assertEqual((report["admittedAsShipped"], report["belowFloor"]), (1, 2))
        self.assertEqual(self.wiki_calls[0][:2], ("movie", [2, 3]), "a shipped title is not asked about")
        self.assertEqual(self.origin_calls, [("movie", [2])])

    def test_a_plan_row_that_says_it_was_admitted_keeps_its_admission(self):
        """A title enriched once and never shipped — plotless — is not in the catalogue. The plan that
        re-fetches it knows it was admitted, and says so on the row; the same row without it is judged
        again as new. A plan row with a count is judged on the count."""
        self.wikis.update({("movie", 1): 1, ("movie", 2): 1, ("movie", 3): 1})
        report = self.run_batch([("movie", 1), ("movie", 2), ("movie", 3)],
                                votes={("movie", 1): None, ("movie", 2): None, ("movie", 3): 10},
                                tiers={("movie", 3): False}, planned={("movie", 1), ("movie", 3)})
        self.assertEqual(set(self.rows()), {"movie:1"})
        self.assertEqual((report["admittedByPlan"], report["admittedAsShipped"], report["belowFloor"]), (1, 0, 2))
        self.assertNotIn("admitted", self.rows()["movie:1"], "the plan's word is read, not written")

    def test_only_a_literal_true_says_admitted(self):
        """A hand-edited plan's `"admitted": "no"` is not a yes."""
        path = os.path.join(self.out, "plan.json")
        put(path, json.dumps([{"tmdbId": 1, "mediaType": "movie", "admitted": True},
                              {"tmdbId": 2, "mediaType": "movie", "admitted": "no"},
                              {"tmdbId": 3, "mediaType": "movie"}]))
        self.assertEqual(enrich.read_worklist(path)[3], frozenset({"movie:1"}))

    def test_an_unreadable_catalogue_is_a_refusal_not_an_empty_one(self):
        """Read as empty, it would judge every shipped title on a list of ids again, silently."""
        with tempfile.TemporaryDirectory() as directory:
            broken = os.path.join(directory, "catalogue.json")
            put(broken, '{"titles": ')
            with self.assertRaises(StageError):
                self.real_shipped(broken)
        self.assertIn("movie:2", self.real_shipped(enrich.CATALOGUE), "the committed catalogue is the shipped one")

    def test_the_gate_is_decided_before_the_expensive_work(self):
        """A refused title is never mapped to its articles and no plot is fetched for it: extra candidates
        cost one count lookup per media, not a plot fetch and a classification — and only the titles TMDB
        left short are asked about, on the day the batch runs."""
        self.mapping[("movie", 1)] = {"article": "Kept"}
        self.mapping[("movie", 2)] = {"article": "Refused"}
        self.mapping[("movie", 3)] = {"article": "Popular"}
        self.plots[("Kept", "en")] = found("k" * 300)
        self.plots[("Popular", "en")] = found("p" * 300)
        self.admission((1, 25, False, 22), (2, 10, False, 1), (3, 900, False, 40))
        self.assertEqual(self.mapping_calls, [("movie", [1, 3])])
        self.assertEqual(sorted(self.plot_calls), [("Kept", "en"), ("Popular", "en")])
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        self.assertEqual(self.wiki_calls, [("movie", [1, 2], today)], "one lookup for the short titles, per media")

    def test_a_failed_wikipedia_count_aborts_the_batch_and_writes_nothing(self):
        with mock.patch.object(enrich.wikidata, "wikipedias", side_effect=http.HTTPError(0, "x")):
            with self.assertRaises(enrich.Aborted):
                self.run_batch([("movie", 1)], votes={("movie", 1): 10})
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
        self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

    # -- below-floor verdicts: one run drains, a later day re-judges -------------------------------------

    DAY, NEXT_DAY = "2026-09-22", "2026-09-23"

    def below(self, votes=10, wikis=1, today=DAY, **kwargs):
        """One batch over movie 1: a worklist row stating `votes` TMDB votes, and articles on `wikis`
        Wikipedias."""
        self.wikis[("movie", 1)] = wikis
        self.mapping[("movie", 1)] = {"article": "One"}
        self.plots[("One", "en")] = found("o" * 300)
        before = len(self.claimant_calls)
        report = enrich.run(self.worklist(("movie", 1), votes={("movie", 1): votes}, tiers={("movie", 1): False}),
                            self.out, cache=self.cache, today=today, **kwargs)
        return report, [f"{media}:{i}" for media, ids in self.claimant_calls[before:] for i in ids]

    def test_a_run_asks_about_a_below_floor_title_once(self):
        """Left unrecorded, every batch of a drain asked it again, `remaining` never reached 0, and a
        universe holding one below-floor title could never drain — an export universe is mostly those."""
        report, asked = self.below()
        self.assertEqual((report["belowFloor"], report["remaining"]), (1, 0))
        self.assertEqual(asked, ["movie:1"])
        report, asked = self.below()
        self.assertEqual((report, asked), ({"remaining": 0, "count": 0}, []), "judged today already")

    def test_a_later_day_judges_it_again_and_admits_it_once_it_clears(self):
        """The verdict is about the day's counts, and by the next day a title can have gained the
        Wikipedia articles that carry it over the floor."""
        self.below()
        report, asked = self.below(wikis=12, today=self.NEXT_DAY)
        self.assertEqual(asked, ["movie:1"])
        self.assertEqual((report["admittedByWikipedias"], report["belowFloor"], report["batchId"]), (1, 0, 1))
        self.assertEqual(set(self.rows()), {"movie:1"})
        self.assertEqual((self.checkpoint()["processed"], self.checkpoint()["judgedBelow"]), (["movie:1"], {}))

    def test_a_later_day_that_still_refuses_it_records_that_day(self):
        self.below()
        self.below(today=self.NEXT_DAY)
        self.assertEqual(self.checkpoint()["judgedBelow"]["movie:1"]["on"], self.NEXT_DAY)

    def test_a_changed_worklist_count_is_judged_again_the_same_day(self):
        """A delta rebuilt later the same day can state a higher count; the verdict was about the old one."""
        self.below(votes=10)
        report, asked = self.below(votes=60)
        self.assertEqual((asked, report["admittedByTmdb"]), (["movie:1"], 1))

    def test_a_lowered_floor_is_judged_again_the_same_day(self):
        """`--vote-floor 0` is how an operator takes in the low-vote tail; a verdict made at 50 says nothing
        about 5."""
        self.below(votes=10)
        report, asked = self.below(votes=10, floors=enrich.floor_rules.given(tmdb=5))
        self.assertEqual((asked, report["admittedByTmdb"]), (["movie:1"], 1))

    def test_a_verdict_made_under_the_imdb_floors_is_judged_again(self):
        """A checkpoint written while the gate's other half was IMDb's holds verdicts under floors
        `[50, 15, 2000, 500]` and an `imdb` flag. Those floors are not today's, so the title is asked again."""
        put(enrich.checkpoint_path(self.out), json.dumps({"processed": [], "judgedBelow": {
            "movie:1": {"on": self.DAY, "votes": 10, "floors": [50, 15, 2000, 500], "imdb": True}}}))
        report, asked = self.below(wikis=12)
        self.assertEqual((asked, report["admittedByWikipedias"]), (["movie:1"], 1))

    def test_a_batch_that_admits_nothing_writes_no_batch_and_keeps_its_number(self):
        """Each refused attempt wrote a `batch-N.json` holding `[]` and moved the numbering on."""
        report, _asked = self.below()
        self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))
        self.assertNotIn("batchId", report)
        self.assertEqual(self.checkpoint()["nextBatch"], 1)
        report, _asked = self.below(wikis=12, today=self.NEXT_DAY)
        self.assertEqual(report["batchId"], 1)
        self.assertEqual(self.checkpoint()["nextBatch"], 2)

    def test_a_malformed_verdict_map_is_a_refusal_not_a_reset(self):
        put(enrich.checkpoint_path(self.out), json.dumps({"processed": [], "judgedBelow": ["movie:1"]}))
        with self.assertRaises(StageError):
            self.below()

    # -- the checkpoint -------------------------------------------------------------------------------

    def test_transient_ids_stay_pending_below_floor_ids_are_judged_and_the_rest_are_checkpointed(self):
        self.mapping.update({("movie", 1): {"article": "Blip"}, ("movie", 4): {"article": "Blip"}})
        self.plots[("Blip", "en")] = http.HTTPError(503, "x")
        report = self.run_batch([("movie", i) for i in range(1, 5)], votes={("movie", 2): 10})
        self.assertEqual(sorted(self.checkpoint()["processed"]), ["movie:3"])
        self.assertEqual(sorted(self.checkpoint()["judgedBelow"]), ["movie:2"])
        self.assertEqual((report["deferred"], report["belowFloor"], report["remaining"], report["count"]),
                         (2, 1, 2, 1))

    def test_no_answer_at_all_is_transient(self):
        """A dropped connection, a TLS failure, a refused socket: none says anything about the title. The
        Swift pass wrote some of these as a permanently plotless `fetchFailed`."""
        self.mapping[("movie", 2)] = {"article": "Two"}
        self.plots[("Two", "en")] = http.HTTPError(0, "https://en.wikipedia.org/w/api.php")
        report = self.run_batch([("movie", 2)])
        self.assertEqual((report["deferred"], report["count"]), (1, 0))
        self.assertEqual(self.checkpoint()["processed"], [])

    def test_the_next_batch_is_numbered_from_the_directory_when_the_checkpoint_is_missing(self):
        """`out-t02` had 153 batches and no checkpoint; a delta numbered from 1 and overwrote two."""
        os.makedirs(os.path.join(self.out, "enriched"))
        for name in ("batch-9.json", "batch-153.json", "batch-17.json", "notes.txt"):
            put(os.path.join(self.out, "enriched", name), "[]")
        report = self.run_batch([("movie", 1)])
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
        entries = [("movie", i) for i in range(1, 5)]
        self.run_batch(entries, limit=2)
        with self.killed_at_the_checkpoint(), self.assertRaises(Killed):
            self.run_batch(entries, limit=2)
        self.assertEqual(sorted(self.keys_by_batch()), ["batch-1.json", "batch-2.json"])
        report = self.run_batch(entries, limit=2)
        self.assertEqual(report, {"remaining": 0, "count": 0})
        self.assertEqual(self.keys_by_batch(), {"batch-1.json": ["movie:1", "movie:2"],
                                                "batch-2.json": ["movie:3", "movie:4"]})

    def test_the_first_batch_is_covered_too(self):
        """With no checkpoint yet there is nothing to measure batch-1 against, so its number is reserved in a
        checkpoint before it is written."""
        batch = enrich.batch_path(self.out, 1)
        with self.killed_at_the_checkpoint(when=lambda: os.path.exists(batch)), self.assertRaises(Killed):
            self.run_batch([("movie", 1), ("movie", 2)])
        self.assertEqual(self.checkpoint()["nextBatch"], 1, "reserved, not advanced")
        self.assertEqual(self.run_batch([("movie", 1), ("movie", 2)]), {"remaining": 0, "count": 0})
        self.assertEqual(list(self.keys_by_batch()), ["batch-1.json"])

    def test_a_checkpoint_killed_mid_write_leaves_the_previous_one_whole(self):
        """Written in place, a kill mid-write leaves a truncated checkpoint, which the next run refuses — the
        whole drain stops. Through a temp file and a rename it leaves the previous one."""
        self.run_batch([("movie", i) for i in range(1, 5)], limit=2)
        with open(enrich.checkpoint_path(self.out), "rb") as fh:
            before = fh.read()
        real = os.replace

        def dying(source, destination):
            if destination == enrich.checkpoint_path(self.out):
                raise Killed()
            real(source, destination)
        with mock.patch.object(enrich.caching.os, "replace", dying), self.assertRaises(Killed):
            self.run_batch([("movie", i) for i in range(1, 5)], limit=2)
        with open(enrich.checkpoint_path(self.out), "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_an_existing_batch_is_never_overwritten(self):
        os.makedirs(os.path.join(self.out, "enriched"))
        with open(enrich.checkpoint_path(self.out), "w") as fh:
            json.dump({"processed": [], "nextBatch": 1}, fh)
        with mock.patch.object(enrich, "batches", return_value=[]):
            put(enrich.batch_path(self.out, 1), "[]")
            with self.assertRaises(StageError):
                self.run_batch([("movie", 1)])
        with open(enrich.batch_path(self.out, 1)) as fh:
            self.assertEqual(fh.read(), "[]")

    def test_an_unreadable_checkpoint_is_a_refusal_not_a_reset(self):
        put(enrich.checkpoint_path(self.out), '{"processed": [')
        with self.assertRaises(StageError):
            self.run_batch([("movie", 1)])

    def test_a_legacy_checkpoint_of_bare_ids_means_movies(self):
        with open(enrich.checkpoint_path(self.out), "w") as fh:
            json.dump({"processed": [1]}, fh)
        report = self.run_batch([("movie", 1), ("tv", 1)])
        self.assertEqual(report["count"], 1)
        self.assertIn("tv:1", self.rows())

    def test_anime_is_kept_unless_asked(self):
        """Excluding it by default silently cost the corpus 1,498 titles, the Ghibli catalogue among them."""
        self.kinds[("movie", 1)] = ["anime television series"]
        self.assertEqual(self.run_batch([("movie", 1)])["count"], 1)
        os.remove(enrich.checkpoint_path(self.out))
        report = self.run_batch([("movie", 1)], exclude_anime=True, limit=5)
        self.assertEqual((report["anime"], report["count"]), (1, 0))

    def test_anime_is_wikidatas_genres_and_types(self):
        """Wikidata says it in a P136 genre or a P31 type, which catches the co-productions TMDB's language
        test missed — `Ulysses 31` is French-Japanese, `Dogtanian` Spanish-Japanese. Animation is not anime."""
        self.kinds.update({("movie", 2): ["adventure anime and manga", "film"],
                           ("movie", 3): ["animated film"]})
        report = self.run_batch([("movie", i) for i in (1, 2, 3)], exclude_anime=True)
        self.assertEqual(sorted(self.rows()), ["movie:1", "movie:3"])
        self.assertEqual(report["anime"], 1)
        self.assertEqual(self.kind_calls, [("movie", [1, 2, 3])],
                         "one query for the batch, per media — never one per title")

    def test_a_title_the_gate_refused_is_never_asked_about(self):
        """The same rule as the mapping: a refused title costs an id lookup, not a second query about what
        it is."""
        self.kinds[("movie", 2)] = ["anime film"]
        self.run_batch([("movie", 1), ("movie", 2)], votes={("movie", 2): 1}, exclude_anime=True)
        self.assertEqual(self.kind_calls, [("movie", [1])])

    def test_anime_influenced_animation_is_not_anime(self):
        """Western animation drawn in the style. `lib/wikidata_facts.genre_map` sets `live-action/animated`
        aside from its animation rule for the same reason."""
        self.kinds[("movie", 1)] = ["anime-influenced animation"]
        report = self.run_batch([("movie", 1)], exclude_anime=True)
        self.assertEqual((report["anime"], sorted(self.rows())), (0, ["movie:1"]))

    def test_a_failed_genre_lookup_aborts_the_batch_and_writes_nothing(self):
        """Swallowed, every row would be written with no `animated` flag, and a run asked to drop anime
        would keep it."""
        for exclude in (False, True):
            with mock.patch.object(enrich.wikidata, "kinds", side_effect=http.HTTPError(0, "x")):
                with self.assertRaises(enrich.Aborted):
                    self.run_batch([("movie", 1)], exclude_anime=exclude)
            self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
            self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

    def test_both_media_in_one_worklist_are_mapped_apart(self):
        """Series 91545 looked up as a MOVIE grounded Young Wallander on "Sunday Drive (film)". The refusal
        of a mixed worklist is gone; the keying by pair is what makes that safe."""
        self.mapping.update({("tv", 95): {"article": "Buffy"}, ("movie", 95): {"article": "Armageddon"}})
        self.plots.update({("Buffy", "en"): found("b" * 300, resolved="Buffy"),
                           ("Armageddon", "en"): found("a" * 300, resolved="Armageddon")})
        self.run_batch([("tv", 95), ("movie", 95)])
        rows = self.rows()
        self.assertEqual((rows["tv:95"]["plotArticle"], rows["movie:95"]["plotArticle"]), ("Buffy", "Armageddon"))

    # -- one Wikidata item per title ---------------------------------------------------------------------

    def test_a_tmdb_id_two_items_claim_is_answered_by_the_one_the_rules_single_out(self):
        """Every query was keyed by the TMDB id, so series 2559 shipped Bonn's name beside Boon's IMDb id.
        Bonn also states its own id 215780, so Boon is the item that states this one alone; every lookup
        leaves Bonn out, and the row records the choice."""
        self.claimants[("tv", 2559)] = ["Q132860965", "Q116226000"]
        self.evidence.update({"Q116226000": {"claims": [2559, 215780], "articles": []},
                              "Q132860965": {"claims": [2559], "articles": ["Boon (TV series)"]}})
        self.mapping[("tv", 2559)] = {"article": "Boon (TV series)"}
        self.plots[("Boon (TV series)", "en")] = found("B" * 300)
        self.wikis[("tv", 2559)] = 12
        self.run_batch([("tv", 2559)], votes={("tv", 2559): None})
        row = self.rows()["tv:2559"]
        self.assertEqual(row["wikidataItem"], "Q132860965")
        self.assertEqual(row["wikidataCandidates"], ["Q116226000", "Q132860965"])
        told = {name: excluded for name, media, excluded in self.excluded}
        for name in ("wikipedias", "mapping", "languages", "kinds"):
            self.assertEqual(told[name], {2559: ["Q116226000"]}, name)

    def test_a_title_nothing_singles_one_item_out_for_asks_neither(self):
        """Two items, both claiming only this id and neither with an English article: the row keeps both
        names and no item, and every lookup leaves both out rather than merging them."""
        self.claimants[("movie", 5)] = ["Q2", "Q1"]
        self.evidence.update({"Q1": {"claims": [5], "articles": []}, "Q2": {"claims": [5], "articles": []}})
        self.run_batch([("movie", 5)])
        row = self.rows()["movie:5"]
        self.assertNotIn("wikidataItem", row)
        self.assertEqual(row["wikidataCandidates"], ["Q1", "Q2"])
        self.assertEqual(dict((n, e) for n, m, e in self.excluded)["mapping"], {5: ["Q1", "Q2"]})

    def test_an_uncontested_title_records_nothing_and_its_queries_are_unchanged(self):
        self.claimants[("movie", 1)] = ["Q1"]
        self.mapping[("movie", 1)] = {"article": "One"}
        self.run_batch([("movie", 1)])
        row = self.rows()["movie:1"]
        self.assertNotIn("wikidataItem", row)
        self.assertNotIn("wikidataCandidates", row)
        self.assertEqual([excluded for _, _, excluded in self.excluded if excluded], [])

    def test_remaining_counts_a_series_and_a_film_that_share_an_id_apart(self):
        """`remaining` is how the drain decides it is finished. Counted by bare id, a processed movie 95
        would mark series 95 done and the drain would stop with it never enriched."""
        self.mapping.update({("movie", 95): {"article": "Armageddon"}, ("tv", 95): {"article": "Buffy"}})
        report = self.run_batch([("movie", 95), ("tv", 95), ("tv", 7)], limit=1)
        self.assertEqual(report["remaining"], 2, "tv:95 and tv:7 are still pending")
        report = self.run_batch([("movie", 95), ("tv", 95), ("tv", 7)], limit=2)
        self.assertEqual(report["remaining"], 0)

    def test_a_series_only_worklist_reports_zero_once_its_batch_is_written(self):
        report = self.run_batch([("tv", 1), ("tv", 2)])
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
        self.run_batch([("tv", 7), ("movie", 95), ("movie", 3)])
        self.assertEqual(self.mapping_calls, [("movie", [3, 95]), ("tv", [7])])

    def test_the_limit_is_how_many_ids_one_batch_takes(self):
        report = self.run_batch([("movie", i) for i in range(1, 6)], limit=2)
        self.assertEqual(self.claimant_calls, [("movie", [1, 2])], "the first two, in worklist order")
        self.assertEqual((report["count"], report["remaining"]), (2, 3))

    def test_a_failed_mapping_aborts_the_batch_and_writes_nothing(self):
        with mock.patch.object(enrich.wikidata, "mapping", side_effect=http.HTTPError(0, "x")):
            with self.assertRaises(enrich.Aborted):
                self.run_batch([("movie", 1)])
        self.assertFalse(os.path.exists(enrich.checkpoint_path(self.out)))
        self.assertFalse(os.path.exists(os.path.join(self.out, "enriched")))

    def test_a_drained_worklist_asks_nothing(self):
        with open(enrich.checkpoint_path(self.out), "w") as fh:
            json.dump({"processed": ["movie:1"], "nextBatch": 2}, fh)
        report = enrich.run(self.worklist(("movie", 1)), self.out, cache=self.cache)
        self.assertEqual(report, {"remaining": 0, "count": 0})
        self.assertEqual(self.claimant_calls, [])


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CommandLine(unittest.TestCase):
    """Where a person types this module's command line — `docs/OPERATE.md` — held to its parser."""

    def invocations(self):
        found = []
        for path in ("docs/OPERATE.md",):
            with open(os.path.join(REPO, path), encoding="utf-8") as fh:
                text = fh.read().replace("\\\n", " ")
            found += [(path, line.split("python3 -m pipeline.enrich", 1)[1])
                      for line in text.splitlines()
                      if "python3 -m pipeline.enrich " in line and not line.lstrip().startswith("#")]
        return found

    def test_every_documented_invocation_parses(self):
        """A documented flag the parser does not know is a command that fails for whoever types it."""
        found = self.invocations()
        self.assertEqual(sorted({path for path, _ in found}), ["docs/OPERATE.md"])
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
                         "--wikipedia-floor", "7", "--regional-wikipedia-floor", "4"])
            self.assertEqual(ran.call_args.args[2], enrich.floor_rules.Floors(40, 10, 7, 4))
            enrich.main(["--worklist", "w", "--out-dir", "o"])
            self.assertEqual(ran.call_args.args[2], enrich.floor_rules.DEFAULT)

    def test_an_unknown_flag_is_refused(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            enrich.parser().parse_args(["--worklist", "w", "--out-dir", "o", "--plot-cap", "3"])


if __name__ == "__main__":
    unittest.main()
