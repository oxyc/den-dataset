"""The replay's claims, each against a cache built to make one of them fail if it were wrong."""
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "backfill_plot_provenance",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill-plot-provenance.py"))
backfill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill)


def sitelink(article, language="en"):
    return f"https://{language}.wikipedia.org/wiki/{article.replace(' ', '_')}"


class Cache:
    """A response cache written the way `ResponseCache` files it, so the reader is exercised for real."""

    def __init__(self, directory):
        self.directory = directory

    def sparql(self, name, rows):
        """A body under a name no batch re-derives — a query from another corpus or an older text."""
        body = {"results": {"bindings": rows}}
        self._write(hashlib.sha256(name.encode()).hexdigest(), body)

    def answered(self, media, ids, rows):
        """A body filed under the enrich pass's own mapping query for `ids` asked as `media`."""
        self._write(backfill.mapping_digest(ids, media), {"results": {"bindings": rows}})

    def parse(self, article, language, landed_on):
        query = dict(backfill.PARSE_QUERY, page=article)
        safe = "&".join(f"{key}={value}" for key, value in sorted(query.items()))
        digest = hashlib.sha256(
            f"{backfill.NAMESPACE}\x01{language}.wikipedia.org/w/api.php?{safe}".encode()).hexdigest()
        self._write(digest, {"parse": {"title": landed_on}})

    def _write(self, digest, body):
        directory = os.path.join(self.directory, digest[:2])
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"{digest}.json"), "w", encoding="utf-8") as handle:
            json.dump(body, handle)


def binding(tmdb_id, article=None, source=None, any_article=None, any_language=None):
    row = {"tmdb": {"value": str(tmdb_id)}}
    if article:
        row["article"] = {"value": sitelink(article)}
    if source:
        row["sourceArticle"] = {"value": sitelink(source)}
    if any_article:
        row["anyArticle"] = {"value": sitelink(any_article, any_language)}
        row["anySite"] = {"value": f"https://{any_language}.wikipedia.org/"}
    return row


def title(tmdb_id, article, language="en", grounded=True, **extra):
    return dict({"tmdbId": tmdb_id, "mediaType": "movie", "hasWikiPlot": grounded,
                 "plotArticle": article, "plotLanguage": language}, **extra)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_dir = os.path.join(self.tmp.name, "wiki")
        os.makedirs(self.cache_dir)
        self.cache = Cache(self.cache_dir)
        self.enriched = os.path.join(self.tmp.name, "enriched")
        os.makedirs(self.enriched)

    def run_backfill(self, rows, out_dir=None):
        with open(os.path.join(self.enriched, "batch-0001.json"), "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        out_dir = out_dir or os.path.join(self.tmp.name, "out")
        counts = backfill.backfill(self.enriched, out_dir, self.cache_dir)
        with open(os.path.join(out_dir, "batch-0001.json"), encoding="utf-8") as handle:
            return counts, json.load(handle)

    def test_the_titles_own_article_is_recorded_as_its_own(self):
        self.cache.sparql("q", [binding(1, article="Heat (1995 film)")])
        _, rows = self.run_backfill([title(1, "Heat (1995 film)")])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.OWN)
        self.assertIs(rows[0]["plotArticleRedirected"], False)

    def test_a_source_work_is_not_recorded_as_the_titles_own(self):
        """The whole point: 1% of source-work groundings describe the right screen work."""
        self.cache.sparql("q", [binding(2, article="Silo (TV series)", source="Wool (novel)")])
        _, rows = self.run_backfill([title(2, "Wool (novel)")])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.SOURCE_WORK)

    def test_a_source_work_wins_even_when_it_is_the_only_candidate(self):
        """4% of titles have no English article, so the source work sits where the own article would.

        Reading the role off the candidate's POSITION would call this one `own`.
        """
        self.cache.sparql("q", [binding(3, source="Notes from Underground")])
        _, rows = self.run_backfill([title(3, "Notes from Underground")])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.SOURCE_WORK)

    def test_another_wikipedias_copy_of_the_titles_own_article_is_still_its_own(self):
        self.cache.sparql("q", [binding(4, article="Iago", any_article="Iago (film)", any_language="it")])
        _, rows = self.run_backfill([title(4, "Iago (film)", language="it")])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.OWN_OTHER_LANGUAGE)

    def test_a_redirect_into_another_work_is_recorded_as_a_redirect(self):
        """`Jarhead 2` asks for its own page and lands in `Jarhead (film)`. The role still reads `own`,
        which is why the redirect flag is a second field rather than a fourth role."""
        self.cache.sparql("q", [binding(5, article="Jarhead 2: Field of Fire")])
        self.cache.parse("Jarhead 2: Field of Fire", "en", "Jarhead (film)")
        _, rows = self.run_backfill([title(5, "Jarhead (film)")])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.OWN)
        self.assertIs(rows[0]["plotArticleRedirected"], True)

    def test_an_exact_match_beats_a_redirect_that_lands_on_the_same_page(self):
        """A candidate that IS the recorded article was reached without moving; one that merely lands on
        it is the weaker claim, and taking it would report a redirect that never happened."""
        self.cache.sparql("q", [binding(6, article="Dune (2021 film)", source="Dune (novel)")])
        self.cache.parse("Dune (novel)", "en", "Dune (2021 film)")
        _, rows = self.run_backfill([title(6, "Dune (2021 film)")])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.OWN)
        self.assertIs(rows[0]["plotArticleRedirected"], False)

    def test_an_exact_match_wins_over_an_earlier_candidate_that_only_redirects_onto_it(self):
        """The case that separates "exact beats redirect" from "the first candidate that matches wins":
        an adaptation whose own page redirects into the novel's, which is also the source work. The text
        IS the novel's page, so the role is `source-work` — taking the earlier candidate would record it
        as the title's own article, reached by a redirect."""
        self.cache.sparql("q", [binding(14, article="The Shining (film)", source="The Shining (novel)")])
        self.cache.parse("The Shining (film)", "en", "The Shining (novel)")
        _, rows = self.run_backfill([title(14, "The Shining (novel)")])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.SOURCE_WORK)
        self.assertIs(rows[0]["plotArticleRedirected"], False)

    def test_a_row_the_cache_cannot_explain_keeps_both_fields_absent(self):
        """UNKNOWN, never `own`. A guess here reports a title as correctly grounded on a failed replay."""
        self.cache.sparql("q", [binding(7, article="Some Other Film")])
        counts, rows = self.run_backfill([title(7, "An Article Nothing Names")])
        self.assertNotIn("plotArticleRole", rows[0])
        self.assertNotIn("plotArticleRedirected", rows[0])
        self.assertEqual(counts["unrecoverable"], 1)

    def test_a_role_the_enrich_pass_recorded_is_never_overwritten(self):
        """The pass watched the decision; this replays it. Where they disagree, the witness wins."""
        self.cache.sparql("q", [binding(8, article="Heat (1995 film)")])
        counts, rows = self.run_backfill(
            [title(8, "Heat (1995 film)", plotArticleRole=backfill.SOURCE_WORK,
                   plotArticleRedirected=True)])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.SOURCE_WORK)
        self.assertEqual(counts["already recorded"], 1)

    def test_a_title_with_no_plot_is_left_alone(self):
        self.cache.sparql("q", [binding(9, article="Nothing Here")])
        _, rows = self.run_backfill([title(9, None, grounded=False)])
        self.assertNotIn("plotArticleRole", rows[0])

    def test_every_body_that_mentions_an_id_is_tried_and_the_recorded_article_decides(self):
        """Neither body's query can be re-derived from the batch, so neither says which media it answered;
        the corpus holds only a film with this id, so the match is used and counted as unconfirmed."""
        self.cache.sparql("first", [binding(10, article="A Film")])
        self.cache.sparql("second", [binding(10, article="Another Film")])
        counts, rows = self.run_backfill([title(10, "Another Film")])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.OWN)
        self.assertEqual(counts["own (media unconfirmed)"], 1)

    def test_a_body_the_batches_attribute_to_a_media_is_confirmed(self):
        self.cache.answered("movie", [15], [binding(15, article="Heat (1995 film)")])
        counts, rows = self.run_backfill([title(15, "Heat (1995 film)")])
        self.assertEqual((rows[0]["plotArticleRole"], counts["own"]), (backfill.OWN, 1))

    def test_a_body_that_answered_the_other_media_never_decides_a_row(self):
        """Movie 95 is Armageddon and series 95 is Buffy. Keyed by bare id, the series' body explained a film
        row that recorded Buffy's article — the Young Wallander mistake, confirmed as `own`."""
        rows = [title(95, "Buffy the Vampire Slayer"),
                title(95, "Buffy the Vampire Slayer", mediaType="tv")]
        self.cache.answered("tv", [95], [binding(95, article="Buffy the Vampire Slayer")])
        self.cache.answered("movie", [95], [binding(95, article="Armageddon (1998 film)")])
        counts, rows = self.run_backfill(rows)
        self.assertNotIn("plotArticleRole", rows[0], "the film's own body names Armageddon")
        self.assertEqual(rows[1]["plotArticleRole"], backfill.OWN)
        self.assertEqual((counts["unrecoverable"], counts["own"]), (1, 1))

    def test_a_mixed_batch_asked_as_one_media_is_attributed_to_that_media(self):
        """Before a mixed batch was refused, the whole batch was asked as its first title's media: series
        91545, Young Wallander, came back as the film with that id and was grounded on "Sunday Drive (film)".
        That body is the MOVIE query's, so it cannot confirm the series row that recorded its article."""
        self.cache.answered("movie", [1, 91545], [binding(1, article="Heat (1995 film)"),
                                                  binding(91545, article="Sunday Drive (film)")])
        counts, rows = self.run_backfill([title(1, "Heat (1995 film)"),
                                          title(91545, "Sunday Drive (film)", mediaType="tv")])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.OWN)
        self.assertNotIn("plotArticleRole", rows[1])
        self.assertEqual((counts["own"], counts["unrecoverable"]), (1, 1))

    def test_an_unattributed_body_is_ambiguous_when_the_corpus_holds_both_titles(self):
        """Its query cannot be re-derived, so it may be the series' answer as easily as the film's."""
        self.cache.sparql("older query text", [binding(95, article="Buffy the Vampire Slayer")])
        counts, rows = self.run_backfill([title(95, "Buffy the Vampire Slayer"),
                                          title(95, "Something Else", mediaType="tv")])
        self.assertNotIn("plotArticleRole", rows[0])
        self.assertEqual(counts["ambiguous media"], 1)

    def test_an_unattributed_match_the_other_titles_own_body_rules_out_is_used(self):
        """The series' attributed body says what series 95's candidates are, and the film's article is not
        one of them — so the unattributed match cannot be the series answering."""
        self.cache.sparql("older query text", [binding(95, article="Armageddon (1998 film)")])
        self.cache.answered("tv", [95], [binding(95, article="Buffy the Vampire Slayer")])
        counts, rows = self.run_backfill([title(95, "Armageddon (1998 film)"),
                                          title(95, "Buffy the Vampire Slayer", mediaType="tv")])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.OWN)
        self.assertEqual((counts["own (media unconfirmed)"], counts["own"]), (1, 1))

    def test_an_unattributed_body_is_ambiguous_when_a_body_answered_the_id_as_the_other_media(self):
        self.cache.sparql("older query text", [binding(95, article="Buffy the Vampire Slayer")])
        self.cache.answered("tv", [95, 96], [binding(95, article="Buffy the Vampire Slayer")])
        counts, rows = self.run_backfill([title(95, "Buffy the Vampire Slayer"), title(96, None, grounded=False)])
        self.assertNotIn("plotArticleRole", rows[0])
        self.assertEqual(counts["ambiguous media"], 1)

    def test_rewriting_in_place_keeps_every_row(self):
        self.cache.sparql("q", [binding(11, article="Heat (1995 film)")])
        _, rows = self.run_backfill(
            [title(11, "Heat (1995 film)"), title(12, None, grounded=False)], out_dir=self.enriched)
        self.assertEqual([r["tmdbId"] for r in rows], [11, 12])
        self.assertEqual(rows[0]["plotArticleRole"], backfill.OWN)

    def test_a_truncated_cache_body_is_unknown_rather_than_an_answer(self):
        self.cache.sparql("q", [binding(13, article="Asked For")])
        query = dict(backfill.PARSE_QUERY, page="Asked For")
        safe = "&".join(f"{key}={value}" for key, value in sorted(query.items()))
        digest = hashlib.sha256(
            f"{backfill.NAMESPACE}\x01en.wikipedia.org/w/api.php?{safe}".encode()).hexdigest()
        directory = os.path.join(self.cache_dir, digest[:2])
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"{digest}.json"), "w", encoding="utf-8") as handle:
            handle.write('{"parse": {"title": "Land')
        counts, rows = self.run_backfill([title(13, "Landed Somewhere")])
        self.assertNotIn("plotArticleRole", rows[0])
        self.assertEqual(counts["unrecoverable"], 1)

    def test_a_missing_cache_refuses_rather_than_reporting_a_clean_run(self):
        """An empty replay rewrites every batch unchanged and counts nothing — which reads as a finished
        job, not as a tool that had nothing to work from."""
        code = backfill.main(["--enriched-dir", self.enriched, "--out-dir", self.enriched,
                              "--cache-dir", os.path.join(self.tmp.name, "absent")])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
