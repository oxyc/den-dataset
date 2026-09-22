#!/usr/bin/env python3
"""Wikitext to prose, and the article split it rests on.

Every case here is markup that reaches the classifier as text if it is not removed — a template's
parameters, a table's cell contents, a citation's whole bibliography — and prose is what the pass is
charged per title to read.

The heading cases are the ones that failed silently: MediaWiki wraps a heading in markup whenever the page
needs anchors or directionality, so Face/Off's plot section arrives as `<span dir="ltr">Plot</span>` and
The Rifleman's Overview as two `<span class="anchor">` elements followed by the word. Comparing the raw
string dropped those articles with no error anywhere.
"""
import unittest

from . import wikipedia


class Sections(unittest.TestCase):
    def test_the_lead_comes_first_and_has_no_heading(self):
        found = wikipedia.split_sections("Lead prose.\n\n== Plot ==\nWhat happens.")
        self.assertEqual(found[0][0], "")
        self.assertIn("Lead prose.", found[0][2])
        self.assertEqual(found[1][0], "Plot")

    def test_an_article_with_no_headings_is_all_lead(self):
        self.assertEqual(wikipedia.split_sections("Just prose."), [("", 1, "Just prose.")])

    def test_a_section_runs_to_the_next_heading_whatever_its_depth(self):
        found = wikipedia.split_sections("== A ==\none\n=== B ===\ntwo\n== C ==\nthree")
        self.assertEqual([(heading, level) for heading, level, _ in found[1:]],
                         [("A", 2), ("B", 3), ("C", 2)])
        self.assertIn("one", found[1][2])
        self.assertNotIn("two", found[1][2])

    def test_a_heading_is_closed_by_a_run_of_its_own_length(self):
        """Without the backreference a `===` heading is closed by a stray `==` and the rest of the line
        becomes part of the heading."""
        found = wikipedia.split_sections("=== Deep ===\nbody")
        self.assertEqual(found[1][0], "Deep")


class Headings(unittest.TestCase):
    def test_markup_around_a_heading_is_stripped_and_the_case_kept(self):
        self.assertEqual(wikipedia.display_heading('<span dir="ltr">Plot</span>'), "Plot")
        self.assertEqual(
            wikipedia.display_heading('<span class="anchor" id="a"></span><span class="anchor"></span>'
                                      'Overview'), "Overview")

    def test_an_entity_in_a_heading_is_decoded(self):
        self.assertEqual(wikipedia.display_heading("Cast &amp; characters"), "Cast & characters")

    def test_the_recorded_name_keeps_its_case(self):
        """It is read by a person and matched against a later rule change, and "season 1 (2002)" is worse
        at both than "Season 1 (2002)"."""
        self.assertEqual(wikipedia.display_heading("Season 1 (2002)"), "Season 1 (2002)")


class Clean(unittest.TestCase):
    def test_citations_go_whole(self):
        self.assertEqual(wikipedia.clean_wikitext("He wins.<ref>Smith, p. 4</ref> Then leaves."),
                         "He wins. Then leaves.")
        self.assertEqual(wikipedia.clean_wikitext('A<ref name="x" /> B'), "A B")

    def test_a_citation_that_spans_lines_goes_whole(self):
        self.assertEqual(wikipedia.clean_wikitext("A<ref>\nlots\nof\nbibliography\n</ref> B"), "A B")

    def test_templates_unwind_from_the_inside(self):
        """A template's parameters are not prose — an infobox left in reads as a list of credits."""
        self.assertEqual(wikipedia.clean_wikitext("{{Infobox film|name={{lang|fr|Nom}}|year=1999}}Prose."),
                         "Prose.")

    def test_a_table_goes_with_its_contents(self):
        self.assertEqual(wikipedia.clean_wikitext("{| class=wikitable\n|-\n! Year\n| 1999\n|}\nProse."),
                         "Prose.")

    def test_links_keep_their_text_and_media_links_go(self):
        self.assertEqual(wikipedia.clean_wikitext("[[File:poster.jpg|thumb|A poster]]"), "")
        self.assertEqual(wikipedia.clean_wikitext("[[Ridley Scott|Scott]] directs [[Alien]]."),
                         "Scott directs Alien.")

    def test_external_links_keep_their_label_or_go(self):
        self.assertEqual(wikipedia.clean_wikitext("See [https://example.com the review] for more."),
                         "See the review for more.")
        self.assertEqual(wikipedia.clean_wikitext("Text [https://example.com] here."), "Text here.")

    def test_emphasis_and_entities_do_not_survive_as_tokens(self):
        self.assertEqual(wikipedia.clean_wikitext("'''''Alien''''' is a ''film''."), "Alien is a film.")
        self.assertEqual(wikipedia.clean_wikitext("Alien&nbsp;3 &amp; more"), "Alien 3 & more")

    def test_an_already_encoded_ampersand_is_not_decoded_twice(self):
        """`&amp;` is decoded last, so `&amp;nbsp;` becomes the literal text `&nbsp;` rather than a stray
        space."""
        self.assertEqual(wikipedia.clean_wikitext("A &amp;nbsp; B"), "A &nbsp; B")

    def test_whitespace_is_collapsed_rather_than_carried(self):
        self.assertEqual(wikipedia.clean_wikitext("  A   b  \n\n\n  C  "), "A b\nC")

    def test_a_section_that_was_only_markup_is_empty(self):
        """The caller drops empties, which is how an infobox-only section stops becoming a heading with no
        prose under it."""
        self.assertEqual(wikipedia.clean_wikitext("{{Infobox|a=1}}\n[[File:x.jpg]]"), "")


class Prose(unittest.TestCase):
    """`article_prose` over a stubbed response — the assembly, not the fetch."""

    def setUp(self):
        self.original = wikipedia.fetch_parse
        wikipedia.fetch_parse = lambda article, language="en", cache=None: self.body
        self.body = {"parse": {"wikitext": "Lead.\n\n== Plot ==\nHe wins.\n\n== Reception ==\nReviews.",
                               "revid": 42, "title": "Alien (film)"}}

    def tearDown(self):
        wikipedia.fetch_parse = self.original

    def test_every_section_is_kept_with_its_heading_inline(self):
        """The classifier gets the whole article on purpose: the section rules mis-fire, and the LEAD is
        the only thing that says what the article IS — which is how six tmdbIds came to share 46,936
        characters of Wuthering Heights."""
        found = wikipedia.article_prose("Alien")
        self.assertEqual(found["text"], "Lead.\n\n== Plot ==\nHe wins.\n\n== Reception ==\nReviews.")
        self.assertEqual(found["sections"], ["Plot", "Reception"])

    def test_it_records_where_the_fetch_actually_landed(self):
        """`redirects=1` is sent, so the requested title is not good enough: storing the redirect's name
        beside the target's revid would make a later bulk-revid refresh compare against a page that
        effectively never changes, and pin the stale article forever."""
        found = wikipedia.article_prose("Alien")
        self.assertEqual(found["resolvedArticle"], "Alien (film)")
        self.assertEqual(found["revId"], 42)

    def test_an_error_envelope_is_no_article_rather_than_an_empty_one(self):
        """The action API reports a missing page as a 200 carrying `{"error":…}`."""
        self.body = {"error": {"code": "missingtitle"}}
        self.assertIsNone(wikipedia.article_prose("Nothing"))

    def test_an_article_with_no_prose_at_all_is_no_article(self):
        self.body = {"parse": {"wikitext": "{{Infobox|a=1}}", "revid": 1, "title": "Stub"}}
        self.assertIsNone(wikipedia.article_prose("Stub"))


class CacheableBody(unittest.TestCase):
    def test_an_error_envelope_is_never_written_to_the_cache(self):
        """Caching on status alone would pin `missingtitle` for the whole 180-day TTL and make a transient
        outage look like a permanently plotless title."""
        self.assertTrue(wikipedia.is_parse_result({"parse": {"wikitext": "x"}}))
        self.assertFalse(wikipedia.is_parse_result({"error": {"code": "missingtitle"}}))
        self.assertFalse(wikipedia.is_parse_result({"parse": {}, "error": {}}))
        self.assertFalse(wikipedia.is_parse_result([]))


if __name__ == "__main__":
    unittest.main()
