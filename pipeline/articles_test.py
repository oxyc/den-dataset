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
import tempfile
import unittest

import pipeline

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
        self.original = articles.wikipedia.article_prose
        articles.wikipedia.article_prose = self.stub

    def tearDown(self):
        articles.wikipedia.article_prose = self.original
        self.directory.cleanup()

    def stub(self, article, language="en", cache=None):
        self.fetched.append((article, language))
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

    def test_an_article_that_could_not_be_read_is_left_for_a_later_run(self):
        """An article briefly unreachable and one that does not exist look the same from here, and a row
        with no prose in it is a title the classify pass would pay to read and learn nothing from."""
        self.batch(1, [record(1, article="Missing Thing"), record(2)])
        articles.run(self.context())
        self.assertEqual([row["tmdbId"] for row in self.dumped()], [2])


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

    def test_the_count_it_records_is_the_one_the_auditor_recomputes(self):
        """`audit_combined.py` refuses a row whose `articleChars` is not `len(text)`. A dumper counting in
        a different unit puts the auditor and the file it audits into permanent disagreement."""
        self.batch(1, [record(7)])
        articles.run(self.context())
        row = self.dumped()[0]
        self.assertEqual(row["chars"], len(row["text"]))

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
