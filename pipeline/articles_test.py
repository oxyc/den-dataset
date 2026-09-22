#!/usr/bin/env python3
"""The article dump — what it selects, what it refuses, and that its bytes do not move.

Fetching is stubbed: what is under test is which titles are chosen and in what order, which is where every
failure this stage has had actually lived.

  * **newest batch wins.** 1,855 of 59,218 keys appear in more than one batch and 505 of them disagree
    about `hasWikiPlot`. Read oldest-first, first-wins, a stale no-plot record beats the later one that
    found the article and the title is never dumped;
  * **batch NUMBER order, not name order.** `sorted()` puts `batch-99` after `batch-177`, which silently
    inverts the tie-break above;
  * **a title with no Wikipedia plot is not dumped.** That is the ToS rule — its prose would be TMDB's;
  * **the file is byte-stable.** The classify pass hashes it into its manifest and costs $20.47.

Equivalence with the Swift `dump-articles` was measured separately (oxyc/den-dataset#27) by replaying
3,000 real articles out of `.cache/wiki`: the same 3,000 keys and every field value equal, except `chars`
on 7 rows — see `row()`. It cannot run in CI, which has neither the toolchain nor the cache.
"""
import json
import os
import sys
import tempfile
import unittest

import pipeline
from lib import http

from . import articles, artifacts, fetch
from .contract import Context, StageError, bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "testver"


def record(tmdb_id, media="movie", plot=True, article=None, **extra):
    row = {"tmdbId": tmdb_id, "mediaType": media, "title": f"Title {tmdb_id}", "year": 1999,
           "hasWikiPlot": plot,
           "plotArticle": article if article is not None else (f"Article {tmdb_id}" if plot else None),
           "plotLanguage": "en", "plotRevId": 100 + tmdb_id, "plotSections": ["Plot"]}
    row.update(extra)
    return row


def prose(name, language="en"):
    """What a stubbed fetch returns for an article."""
    return {"text": f"Lead about {name}.\n\n== Plot ==\nWhat happens in {name}.",
            "revId": 900, "resolvedArticle": name, "sections": ["Plot"], "language": language}


class Staged(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.out = self.directory.name
        self.enriched = os.path.join(self.out, "enriched")
        os.makedirs(self.enriched)
        self.fetched = []
        self.answers = {}
        self.original = articles.wikipedia.article_prose
        articles.wikipedia.article_prose = self.stub
        # Wikidata's name for each title: `(media, id) -> {"title", "year"}`, a default for any other id, or
        # an exception to raise.
        self.named, self.named_calls, self.naming = {}, [], None
        self.original_targets = articles.wikidata.targets
        articles.wikidata.targets = self.targets_stub

    def tearDown(self):
        articles.wikipedia.article_prose = self.original
        articles.wikidata.targets = self.original_targets
        self.directory.cleanup()

    def targets_stub(self, ids, media, cache=None):
        self.named_calls.append((media, list(ids)))
        if self.naming is not None:
            raise self.naming
        return {i: self.named.get((media, i), {"title": f"Wikidata {i}", "year": 2001}) for i in ids
                if self.named.get((media, i), True) is not None}

    def stub(self, article, language="en", cache=None):
        self.fetched.append((article, language))
        if article in self.answers:
            return self.answers[article]
        if article.startswith("Down"):
            raise http.HTTPError(503, f"https://{language}.wikipedia.org/w/api.php")
        return None if article.startswith("Missing") else prose(article, language)

    def batch(self, number, records):
        with open(os.path.join(self.enriched, f"batch-{number}.json"), "w", encoding="utf-8") as fh:
            json.dump(records, fh)

    def context(self, **kwargs):
        return Context(out_dir=self.out, dataset_version=VERSION, **kwargs)

    def dumped(self):
        path = os.path.join(self.out, "articles.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class Selection(Staged):
    def test_a_title_with_no_wikipedia_plot_is_not_dumped(self):
        """Not a coverage decision: a title without one has no article recorded to fetch, and its prose
        would be TMDB's, which may not reach an LLM."""
        self.batch(1, [record(1), record(2, plot=False)])
        articles.run(self.context())
        self.assertEqual([row["tmdbId"] for row in self.dumped()], [1])

    def test_a_title_whose_article_name_is_empty_is_not_dumped(self):
        self.batch(1, [record(1, article=""), record(2)])
        articles.run(self.context())
        self.assertEqual([row["tmdbId"] for row in self.dumped()], [2])

    def test_the_newest_batch_decides_whether_a_title_has_a_plot(self):
        """505 keys disagree about `hasWikiPlot` across batches. Oldest-first, first-wins, the stale
        no-plot record wins and One Piece is never dumped at all."""
        self.batch(175, [record(37854, plot=False)])
        self.batch(176, [record(37854, plot=True, article="One Piece")])
        articles.run(self.context())
        self.assertEqual([row["article"] for row in self.dumped()], ["One Piece"])

    def test_batches_are_ordered_by_number_rather_than_by_name(self):
        """`sorted()` is lexicographic: `batch-99` lands after `batch-177`, which inverts the tie-break
        above for every key those two batches share."""
        self.batch(99, [record(1, article="Old")])
        self.batch(177, [record(1, article="New")])
        self.assertEqual(articles.ordered_batches(self.enriched),
                         ["batch-99.json", "batch-177.json"])
        articles.run(self.context())
        self.assertEqual([row["article"] for row in self.dumped()], ["New"])

    def test_a_movie_and_a_series_sharing_an_id_are_two_titles(self):
        """TMDB's id spaces overlap — movie 95 is Armageddon, series 95 is Buffy — and this file is joined
        back on the qualified key."""
        self.batch(1, [record(95, media="movie"), record(95, media="tv")])
        articles.run(self.context())
        self.assertEqual(sorted(row["mediaType"] for row in self.dumped()), ["movie", "tv"])

    def test_a_limit_stops_after_that_many(self):
        self.batch(1, [record(n) for n in range(1, 11)])
        articles.run(self.context(limit=3))
        self.assertEqual(len(self.dumped()), 3)

    def test_a_limit_of_zero_fetches_nothing(self):
        """Zero means zero, as it does to `enrich` and `embed-corpus` — not "no limit", which turns a
        dry-run-sized request into the whole corpus."""
        self.batch(1, [record(n) for n in range(1, 11)])
        articles.run(self.context(limit=0))
        self.assertEqual((self.fetched, self.dumped()), ([], []))


class Resume(Staged):
    def test_it_re_reads_its_own_output_and_does_not_refetch(self):
        """A kill costs at most the titles in flight. 445 MB of articles is hours of polite fetching, so
        the alternative is not "slower", it is a pass nobody dares re-run."""
        self.batch(1, [record(1), record(2)])
        articles.run(self.context(limit=1))
        self.fetched.clear()
        articles.run(self.context())
        self.assertEqual([name for name, _lang in self.fetched], ["Article 2"])
        self.assertEqual([row["tmdbId"] for row in self.dumped()], [1, 2])

    def test_an_article_that_is_not_there_is_not_written(self):
        """A row with no prose in it is a title the classify pass would pay to read and learn nothing
        from."""
        self.batch(1, [record(1, article="Missing Thing"), record(2)])
        articles.run(self.context())
        self.assertEqual([row["tmdbId"] for row in self.dumped()], [2])

    def test_an_article_that_could_not_be_reached_is_left_for_a_later_run(self):
        self.batch(1, [record(1, article="Down Thing"), record(2), record(3)])
        articles.run(self.context())
        self.assertEqual([row["tmdbId"] for row in self.dumped()], [2, 3])
        self.fetched.clear()
        self.answers["Down Thing"] = prose("Down Thing")
        articles.run(self.context())
        self.assertEqual([name for name, _lang in self.fetched], ["Down Thing"])


class Outage(Staged):
    """Wikipedia down is not a corpus with no articles in it. Reported as "no article" it exits 0 over an
    empty file, one step before the pass that pays per title."""

    def test_a_run_where_every_fetch_failed_in_transport_is_refused(self):
        self.batch(1, [record(n, article=f"Down {n}") for n in range(5)])
        with self.assertRaises(StageError) as refused:
            articles.run(self.context())
        self.assertIn("5 of 5 fetches failed in transport", str(refused.exception))
        self.assertIn("HTTP 503", str(refused.exception))

    def test_a_run_where_most_fetches_failed_is_refused_and_keeps_what_arrived(self):
        self.batch(1, [record(1), record(2, article="Down 2"), record(3, article="Down 3")])
        with self.assertRaises(StageError):
            articles.run(self.context())
        self.assertEqual([row["tmdbId"] for row in self.dumped()], [1])

    def test_articles_that_are_genuinely_not_there_are_not_an_outage(self):
        self.batch(1, [record(1, article="Missing 1"), record(2, article="Missing 2"), record(3)])
        articles.run(self.context())
        self.assertEqual([row["tmdbId"] for row in self.dumped()], [3])


class Output(Staged):
    def test_the_row_carries_what_the_classify_pass_joins_and_audits_on(self):
        self.batch(1, [record(7, article="Solaris (1972 film)")])
        articles.run(self.context())
        row = self.dumped()[0]
        self.assertEqual(row["mediaType"], "movie")
        self.assertEqual(row["tmdbId"], 7)
        self.assertEqual(row["article"], "Solaris (1972 film)")
        self.assertEqual(row["resolvedArticle"], "Solaris (1972 film)")
        self.assertEqual(row["revId"], 900)
        # The revision the PLOT was extracted at, beside the one this article was read at — together they
        # say whether the article moved since the title was grounded.
        self.assertEqual(row["extractorArticleRevId"], 107)
        self.assertEqual(row["chars"], len(row["text"]))

    def test_the_target_is_wikidatas_never_the_enriched_records(self):
        """`title` and `year` are what the classify pass judges the article against, and the enriched
        record's are TMDB's — which is how TMDB Content came to sit on every row of this file."""
        self.batch(1, [record(7, article="Solaris (1972 film)", title="TMDB Title", year=1971)])
        self.named[("movie", 7)] = {"title": "Solaris", "year": 1972}
        articles.run(self.context())
        row = self.dumped()[0]
        self.assertEqual((row["title"], row["year"], row["targetSource"]), ("Solaris", 1972, "wikidata"))
        self.assertNotIn("TMDB Title", json.dumps(row))
        self.assertEqual(self.named_calls, [("movie", [7])])

    def test_the_classify_state_names_the_wikidata_target(self):
        """`run_combined.py` builds `requestedTarget` from the row as it stands, and fills an ABSENT `year`
        from the enriched batches — TMDB's. A row this stage writes reaches the state as Wikidata's."""
        sys.path.insert(0, os.path.join(REPO, "scripts", "v2"))
        import article_sections
        import run_combined
        self.batch(1, [record(7, title="TMDB Title", year=1971), record(8, title="TMDB Other", year=1980)])
        self.named[("movie", 7)] = {"title": "Solaris", "year": 1972}
        self.named[("movie", 8)] = None   # Wikidata answers nothing for this one
        articles.run(self.context())
        records, _keys = run_combined.load_articles(os.path.join(self.out, "articles.jsonl"))
        run_combined.attach_enriched_evidence(records, self.enriched)
        targets = {rec["tmdbId"]: article_sections.target(rec) for rec in records}
        self.assertEqual((targets[7]["title"], targets[7]["year"]), ("Solaris", 1972))
        self.assertEqual((targets[8]["title"], targets[8]["year"]), ("", None),
                         "unknown stays unknown rather than falling back to TMDB's")

    def test_a_target_wikidata_does_not_know_is_null_and_never_omitted(self):
        self.batch(1, [record(7)])
        self.named[("movie", 7)] = None
        articles.run(self.context())
        with open(os.path.join(self.out, "articles.jsonl"), encoding="utf-8") as fh:
            raw = fh.readline()
        self.assertIn('"title":null,"year":null,"targetSource":"wikidata"', raw)

    def test_each_media_is_named_apart_in_fixed_batches(self):
        """Movie 95 and series 95 are different works, and batch membership is part of the cache key."""
        self.batch(1, [record(n, media=m) for n in range(articles.TARGET_BATCH + 1, 0, -1)
                       for m in ("tv", "movie")])
        articles.run(self.context())
        self.assertEqual([(media, len(ids), ids[0]) for media, ids in self.named_calls],
                         [("movie", articles.TARGET_BATCH, 1), ("movie", 1, articles.TARGET_BATCH + 1),
                          ("tv", articles.TARGET_BATCH, 1), ("tv", 1, articles.TARGET_BATCH + 1)])

    def test_a_failed_lookup_writes_nothing(self):
        """A row with no target would have the classify pass pay to judge an article against no name."""
        self.batch(1, [record(1), record(2)])
        self.naming = articles.wikidata.WikidataError("maintenance page")
        with self.assertRaises(StageError) as refused:
            articles.run(self.context())
        self.assertIn("Nothing was written", str(refused.exception))
        self.assertEqual((self.fetched, self.dumped()), ([], []))

    def test_the_count_it_records_is_the_one_the_auditor_recomputes(self):
        """`audit_combined.py` refuses a row whose `articleChars` is not `len(text)`. A dumper counting in
        a different unit puts the auditor and the file it audits into permanent disagreement.

        The text carries a combining mark on purpose: on ASCII every unit agrees, and the Swift dumper's
        grapheme count disagreed on exactly such rows — 7 in 3,000."""
        self.batch(1, [record(7)])
        self.answers["Article 7"] = dict(prose("Article 7"), text="Café noir")
        articles.run(self.context())
        row = self.dumped()[0]
        self.assertEqual(row["text"], "Café noir")
        self.assertEqual(row["chars"], 10, "code points: 'e' and its combining acute are two")

    def test_an_unrecorded_plot_revision_is_null_and_never_omitted(self):
        """The readers take `rec.get("extractorArticleRevId", rec.get("revId"))`. Omitted, the key falls
        back to the revision this dump READ, and a title whose plot revision nobody recorded would claim it
        was extracted at that same revision — `sectionAuditSameRevision` true for a fact that is unknown.
        `null` keeps it unknown."""
        self.batch(1, [record(7, plotRevId=None)])
        articles.run(self.context())
        with open(os.path.join(self.out, "articles.jsonl"), encoding="utf-8") as fh:
            raw = fh.readline()
        self.assertIn('"extractorArticleRevId":null', raw)
        self.assertIsNone(json.loads(raw)["extractorArticleRevId"])

    def test_a_row_is_the_bytes_the_file_already_holds(self):
        """Written against a golden line, not against `line()`: `articles.jsonl` is 445 MB the Swift pass
        wrote, the classify pass hashes it, and this stage APPENDS to it. `/` escaped and UTF-8 unescaped
        is the Swift encoder's spelling; a row in a second spelling is the same JSON and a moved hash."""
        self.batch(1, [{"tmdbId": 7, "mediaType": "movie", "hasWikiPlot": True,
                        "plotArticle": "AC/DC: Let There Be Rock", "plotLanguage": "en",
                        "plotSections": ["Plot"]}])
        self.answers["AC/DC: Let There Be Rock"] = {
            "text": "Café AC/DC.", "revId": 900, "resolvedArticle": "AC/DC: Let There Be Rock",
            "sections": ["Plot"], "language": "en"}
        self.named[("movie", 7)] = {"title": "AC/DC: Let There Be Rock", "year": 1980}
        articles.run(self.context())
        golden = ('{"mediaType":"movie","tmdbId":7,"title":"AC\\/DC: Let There Be Rock","year":1980,'
                  '"targetSource":"wikidata","article":"AC\\/DC: Let There Be Rock","language":"en",'
                  '"resolvedArticle":"AC\\/DC: Let There Be Rock","revId":900,"extractorArticleRevId":null,'
                  '"sections":["Plot"],"plotSections":["Plot"],"chars":12,"text":"Café AC\\/DC."}\n')
        with open(os.path.join(self.out, "articles.jsonl"), "rb") as fh:
            self.assertEqual(fh.read(), golden.encode("utf-8"))

    def test_two_runs_over_one_set_of_batches_write_one_file(self):
        """The classify pass hashes this file into its manifest and costs $20.47. A row order or a key
        order that moves makes a re-dump look like a different input."""
        self.batch(1, [record(n) for n in range(1, 25)])
        articles.run(self.context())
        with open(os.path.join(self.out, "articles.jsonl"), "rb") as fh:
            first = fh.read()
        os.remove(os.path.join(self.out, "articles.jsonl"))
        articles.run(self.context())
        with open(os.path.join(self.out, "articles.jsonl"), "rb") as fh:
            self.assertEqual(fh.read(), first)

    def test_a_row_is_one_line_however_long_the_article_is(self):
        self.batch(1, [record(1)])
        articles.run(self.context())
        with open(os.path.join(self.out, "articles.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)


class Refusal(Staged):
    def test_an_enriched_tree_with_no_grounded_title_is_refused(self):
        """An empty dump is what the classify pass reads as "nothing to classify" — it would report a
        finished pass over no titles at all."""
        self.batch(1, [record(1, plot=False)])
        with self.assertRaises(StageError) as refused:
            articles.run(self.context())
        self.assertIn(fetch.HOW, str(refused.exception))

    def test_a_missing_enrichment_is_refused_with_what_builds_it(self):
        os.rmdir(self.enriched)
        with self.assertRaises(StageError) as refused:
            articles.run(self.context())
        self.assertIn(fetch.HOW, str(refused.exception))


class Topology(unittest.TestCase):
    def test_the_dump_is_owned_by_the_stage_that_writes_it(self):
        self.assertEqual(artifacts.ARTICLES.producer, "")
        self.assertEqual(pipeline.producers()["articles"],
                         (articles.PRODUCER, articles.HOW, True))
        self.assertTrue(os.path.isfile(os.path.join(REPO, articles.PRODUCER)))

    def test_it_runs_before_the_pass_that_reads_it(self):
        """The classify pass reads this file and nothing else here does, so the dump is immediately
        before it — and a stage that read it earlier would read the previous generation."""
        order = list(pipeline.STAGES)
        self.assertLess(order.index("articles"), order.index("classify"))
        reader = pipeline.stage("classify")
        self.assertIn("articles", [bind(e).name for e in reader.INPUTS])

    def test_the_enrichment_is_owned_by_the_stage_that_drains_it(self):
        """The seam this input used to sit on is CLOSED: the batches were `scripts/enrich-run.sh`'s and
        are the fetch stage's now, so the registry names that stage's rule — which is what an operator
        handed a missing `out/enriched` is sent to run.

        Asserted rather than deleted. An artifact quietly going back to naming a script would mean a stage
        stopped running what it claims to, and the derived registry is the only thing that would notice."""
        self.assertEqual(artifacts.ENRICHED.producer, "")
        producer, how, _ = pipeline.producers()["enriched"]
        self.assertEqual((producer, how), (fetch.PRODUCER, fetch.HOW))


if __name__ == "__main__":
    unittest.main()
