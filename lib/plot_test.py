#!/usr/bin/env python3
"""Which of an article's sections describe the WORK, and which source a plot is read from.

Every section case is one the classifier got wrong while it was being written, measured on a real article:
The Wire (a plot spread over five `Season N` headings under `Cast and characters`), Harry & Meghan (reviews
under `Volume I`), Misfits (`Series 1`). Across 33 sampled plotless series the first-plot-heading rule
grounded 0 of them; this one grounds 21.

The source cases are the ones that change what a record CAN say: the Enterprise path names no page, so it
must report no revision and no resolved article rather than echo the requested title back as if no
redirect had happened.
"""
import io
import json
import tempfile
import unittest
from unittest import mock

from . import cache as caching
from . import enterprise
from . import http
from . import plot
from . import wikidata


class Kinds(unittest.TestCase):
    def test_a_story_leaf_outranks_an_organisational_parent(self):
        """The Wire nests its seasons under `Cast and characters`; excluding a leaf for its parent threw
        away the whole plot."""
        self.assertEqual(plot.section_kind("Season 1 (2002)", "Cast and characters"), plot.STORY)
        self.assertEqual(plot.section_kind("Plot", "Production"), plot.STORY)

    def test_a_hard_excluded_parent_beats_even_a_story_leaf(self):
        """Harry & Meghan: four `Volume` headings under `Critical response` and `Veracity of claims`, 18,141
        characters of reviews and fact-checking that the leaf-wins rule let in as plot."""
        self.assertEqual(plot.section_kind("Volume I", "Critical response"), plot.EXCLUDED)
        self.assertEqual(plot.section_kind("Volume II", "Veracity of claims"), plot.EXCLUDED)
        self.assertEqual(plot.section_kind("Season 1", "Reception"), plot.EXCLUDED)
        self.assertEqual(plot.section_kind("Volume I: Fantine", "Plot"), plot.STORY)

    def test_a_theme_parent_is_inherited(self):
        self.assertEqual(plot.section_kind("Institutional dysfunction", "Themes"), plot.THEME)
        self.assertEqual(plot.section_kind("Surveillance", "Themes"), plot.THEME)

    def test_a_plot_parent_is_inherited_by_a_structural_child(self):
        """"Act II" names nothing; without inheritance every film whose plot is in acts loses its back half."""
        self.assertEqual(plot.section_kind("Act II", "Plot"), plot.STORY)

    def test_an_unrecognised_leaf_under_an_unrecognised_parent_stays_out(self):
        """`Realism` under `Style` is 2,618 characters about the writers' research."""
        self.assertEqual(plot.section_kind("Realism", "Style"), plot.EXCLUDED)
        self.assertEqual(plot.section_kind("Critical response", "Reception"), plot.EXCLUDED)
        self.assertEqual(plot.section_kind("Realism"), plot.EXCLUDED)

    def test_the_exclusion_is_a_prefix_without_a_word_boundary(self):
        """As the pass that grounded the corpus wrote it: `cast` also takes "Castle". Pinned so a change here
        is a decision about the corpus, not a tidy-up."""
        self.assertEqual(plot.section_kind("Castle"), plot.EXCLUDED)
        self.assertEqual(plot.section_kind("Format and rules"), plot.THEME)


class Serial(unittest.TestCase):
    def test_a_serial_word_alone_is_not_an_instalment(self):
        """"Episode structure" is production prose under The Wire's `Production`."""
        self.assertFalse(plot.is_serial("episode structure"))
        self.assertFalse(plot.is_serial("seasonal marketing"))
        self.assertTrue(plot.is_serial("season 1 (2002)"))
        self.assertTrue(plot.is_serial("part one"))
        self.assertTrue(plot.is_serial("episodes"))
        self.assertTrue(plot.is_serial("the episodes"), "The Storyteller's anthology heading")

    def test_series_is_not_depluralised_into_nothing(self):
        """"serie" matched nothing and dropped every British series article — Misfits' 16,679 characters."""
        self.assertTrue(plot.is_serial("series 1 (2009)"))
        self.assertTrue(plot.is_serial("seasons"))


class Rank(unittest.TestCase):
    def test_a_qualified_plot_heading_sits_between_plot_and_summary(self):
        """Appending it after every exact name put "Plot and background" below "Summary"."""
        self.assertLess(plot.plot_rank("Plot"), plot.plot_rank("Plot segments"))
        self.assertLess(plot.plot_rank("Plot segments"), plot.plot_rank("Synopsis"))
        self.assertLess(plot.plot_rank("Summary"), plot.plot_rank("Segments"))
        self.assertIsNone(plot.plot_rank("Cast"))

    def test_markup_and_padding_do_not_hide_a_plot_heading(self):
        """Face/Off's plot section arrives as `<span dir="ltr">Plot</span>`."""
        self.assertEqual(plot.plot_rank('<span dir="ltr">Plot</span>'), 0)
        self.assertEqual(plot.plot_rank(" Plot "), 0)


WIRE = ("Lead prose.\n\n== Production ==\n" + "Casting details. " * 20 +
        "\n\n== Cast and characters ==\n=== Season 1 (2002) ===\n" + "McNulty meets the judge. " * 20 +
        "\n\n== Themes ==\n=== Institutional dysfunction ===\n" + "The institutions fail. " * 10 +
        "\n\n== Reception ==\n" + "Widely acclaimed. " * 20)


class Describing(unittest.TestCase):
    def test_the_story_is_kept_and_the_making_dropped(self):
        text, sections = plot.describing_prose(WIRE)
        self.assertEqual(sections, ["Season 1 (2002)", "Institutional dysfunction"])
        self.assertIn("McNulty", text)
        self.assertNotIn("Casting", text)
        self.assertNotIn("acclaimed", text)
        self.assertNotIn("Lead prose", text, "the lead is the classifier's, not the embedder's")

    def test_story_comes_before_theme_whatever_the_article_order(self):
        """So a caller trimming to a budget drops the weaker evidence, not the end of the plot."""
        text, sections = plot.describing_prose("== Themes ==\nAbout grief.\n\n== Plot ==\nShe leaves.")
        self.assertEqual(sections, ["Plot", "Themes"])
        self.assertEqual(text, "She leaves.\n\nAbout grief.")


class Fetching(unittest.TestCase):
    """The two sources, against a real cache directory with `lib/http` as the seam."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = caching.ResponseCache("wiki", self.directory.name, 3600)
        self.requests = []
        self.enterprise = None
        for patch in (mock.patch.object(http, "request", self.answer),
                      mock.patch.object(enterprise, "gate", enterprise.Gate()),
                      mock.patch("sys.stderr", io.StringIO())):
            patch.start()
            self.addCleanup(patch.stop)

    def answer(self, host, path, params=None, **kwargs):
        if host == enterprise.AUTH_HOST:
            return json.dumps({"ondemand_requests_count": 0, "ondemand_limit": 50000}).encode()
        self.requests.append(host)
        if host == plot.ENTERPRISE_HOST:
            if isinstance(self.enterprise, Exception):
                raise self.enterprise
            return json.dumps(self.enterprise).encode()
        return json.dumps({"parse": {"title": "The Wire (TV series)", "revid": 77, "wikitext": WIRE}}).encode()

    def test_the_action_api_records_the_page_it_landed_on_and_its_revision(self):
        found = plot.plot("The Wire", cache=self.cache)
        self.assertEqual((found["resolvedArticle"], found["revId"], found["language"]),
                         ("The Wire (TV series)", 77, "en"))

    def test_the_enterprise_path_names_no_page_and_no_revision(self):
        """Structured contents return sections and no title. Echoing the requested title back would read as
        "asked for it and got it" on the one path that cannot see a redirect."""
        self.enterprise = [{"sections": [{"name": "Plot", "has_parts": [
            {"type": "paragraph", "value": "First."}, {"type": "paragraph", "value": "Second."}]}]}]
        found = plot.plot("The Wire", cache=self.cache, token="bearer")
        self.assertEqual(found["text"], "First.\nSecond.")
        self.assertIsNone(found["resolvedArticle"])
        self.assertIsNone(found["revId"])
        self.assertEqual(self.requests, [plot.ENTERPRISE_HOST], "the action API was not needed")

    def test_the_enterprise_path_is_never_cached(self):
        self.enterprise = [{"sections": [{"name": "Plot", "value": "A hero saves the day."}]}]
        plot.plot("The Wire", cache=self.cache, token="bearer")
        plot.plot("The Wire", cache=self.cache, token="bearer")
        self.assertEqual(self.requests, [plot.ENTERPRISE_HOST] * 2)

    def test_without_a_bearer_the_action_api_answers_and_is_cached(self):
        plot.plot("The Wire", cache=self.cache)
        plot.plot("The Wire", cache=self.cache)
        self.assertEqual(self.requests, ["en.wikipedia.org"])

    def test_an_enterprise_failure_or_an_empty_answer_falls_back_to_the_action_api(self):
        """Best-effort: the free API has the same coverage, so a failed fast path costs speed, not a plot."""
        for failure in (http.HTTPError(401, "x"), [{"sections": [{"name": "Reception", "value": "Good."}]}],
                        {"error": "not a list"}):
            self.requests.clear()
            self.enterprise = failure
            with mock.patch.object(enterprise, "gate", enterprise.Gate()):
                found = plot.plot("The Wire", cache=self.cache, token="bearer")
            self.assertEqual((found["revId"], found["source"]), (77, plot.ACTION_API), failure)
            self.assertEqual(self.requests[0], plot.ENTERPRISE_HOST)

    def test_the_enterprise_tree_uses_the_same_classifier_with_nesting_as_the_parent(self):
        payload = [{"sections": [
            {"name": "Production", "has_parts": [{"type": "paragraph", "value": "Filmed in Baltimore."}]},
            {"name": "Cast and characters", "has_parts": [
                {"type": "section", "name": "Season 1 (2002)", "has_parts": [
                    {"type": "paragraph", "value": "McNulty meets the judge."}]}]},
            {"name": "Themes", "has_parts": [
                {"type": "section", "name": "Institutional dysfunction", "has_parts": [
                    {"type": "paragraph", "value": "The institutions fail."}]}]},
            {"name": "Reception", "has_parts": [{"type": "paragraph", "value": "Widely acclaimed."}]}]}]
        text, sections = plot.enterprise_prose(json.dumps(payload).encode())
        self.assertEqual(sections, ["Season 1 (2002)", "Institutional dysfunction"])
        self.assertNotIn("Filmed", text)
        self.assertNotIn("acclaimed", text)


class OtherLanguages(unittest.TestCase):
    def test_a_language_with_no_heading_list_is_not_guessed_at(self):
        """Welsh appears more often than Italian in the sample — the signature of bot stubs, not coverage."""
        self.assertNotIn("cy", plot.HEADINGS_BY_LANGUAGE)
        with mock.patch.object(http, "request", side_effect=AssertionError("no request for a guess")):
            self.assertIsNone(plot.plot("Anything", "cy"))

    def test_the_local_heading_is_what_is_read(self):
        body = {"parse": {"title": "Schachnovelle (2021)", "revid": 5,
                          "wikitext": "Lead.\n== Handlung ==\nDr. Bartok plays.\n== Kritik ==\nGood."}}
        with mock.patch.object(http, "request", return_value=json.dumps(body).encode()) as sent:
            found = plot.plot("Schachnovelle (2021)", "de")
        self.assertEqual((found["text"], found["sections"], found["language"]),
                         ("Dr. Bartok plays.", ["Handlung"], "de"))
        self.assertEqual(sent.call_args[0][0], "de.wikipedia.org", "the host is the language's")

    def test_a_local_heading_is_matched_as_a_whole_word_prefix(self):
        """"Handlung und Hintergrund" is the plot with more after it, and is read; "Handlungsort" shares the
        letters and not the word, and is not."""
        body = {"parse": {"title": "Film", "revid": 5, "wikitext": (
            "Lead.\n== Handlung und Hintergrund ==\nSie flieht.\n== Handlungsort ==\nIn Wien gedreht.")}}
        with mock.patch.object(http, "request", return_value=json.dumps(body).encode()):
            found = plot.plot("Film", "de")
        self.assertEqual((found["text"], found["sections"]), ("Sie flieht.", ["Handlung und Hintergrund"]))

    def test_the_mapping_asks_for_sitelinks_on_exactly_those_wikis(self):
        """An unrestricted sitelink query returns a row per language and multiplies the result set; English
        is the primary path, never a fallback."""
        query = wikidata.mapping_query([1], "movie", plot.HEADINGS_BY_LANGUAGE)
        for code in plot.HEADINGS_BY_LANGUAGE:
            self.assertIn(f"<https://{code}.wikipedia.org/>", query)
        self.assertNotIn("cy.wikipedia.org", query)
        self.assertEqual(query.count("<https://en.wikipedia.org/>"), 2, "the own and source articles only")


if __name__ == "__main__":
    unittest.main()
