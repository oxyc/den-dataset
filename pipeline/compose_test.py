#!/usr/bin/env python3
"""The embedding document — held to what the Swift composer wrote.

The goldens are the merge-base binary's own output (`embed-corpus --dump-docs`, 4169e60), not round trips
through this module. Over the real corpus the two composers agreed on all 47,539 documents; the corpus
never exercises the plot cap's character counting, though — 15,189 plots are over the cap and not one
reads differently counted by code point — so the grapheme cases below were written to reach it, and each
is a document the Swift composed.
"""
import hashlib
import json
import os
import tempfile
import unittest

from . import compose


def sentences(unit, n):
    return " ".join(f"{unit * (k % 7 + 3)} word{k}." for k in range(n))


#: id -> (plot, the Swift document's length, the first 16 hex of its sha256). Built exactly as the probe
#: the binary composed them from was.
GRAPHEMES = {
    1: (sentences("é", 400), 4885, "ef2e05f2c3cf3588"),                   # combining acute
    2: (sentences("\U0001F468‍\U0001F469‍\U0001F467", 400), 8926, "c170c7b389719b28"),  # ZWJ family
    3: ("\r\n".join(sentences("ab", 20) for _ in range(40)), 3548, "0acb495b54b71954"),           # CRLF
    4: (sentences("क्ष", 400), 6232, "c618fd0831d4c939"),         # Devanagari conjunct
    5: (sentences("\U0001F1EB\U0001F1EE", 400), 4885, "1b9e8a2a816f26bf"),       # flag pairs
    6: (sentences("각", 400), 6232, "1f8d4172c4baceb9"),         # Hangul L V T
    7: (sentences("กำ", 400), 4885, "d1456e2dbe814481"),               # Thai SARA AM
    8: (sentences("\U0001F44D\U0001F3FD", 400), 4885, "0c996940575037e0"),       # skin-tone modifier
    9: (sentences("a️", 400), 4885, "ef6893fbce304370"),                    # variation selector
    10: (sentences("नि", 400), 4885, "b337547bc5c25fa8"),              # spacing vowel sign
    11: ("   " + sentences("x", 500) + " ", 3538, "3a80d4d0c75293ca"),  # U+001C is not trimmed
}


class Document(unittest.TestCase):
    def test_the_lean_shape(self):
        self.assertEqual(compose.lean(["Vince Gilligan"], ["crime drama"], ["Neo-Noir", "Slow-burn"],
                                      "\n  A chemistry teacher turns to crime.  \n"),
                         "Created by Vince Gilligan. Genres: crime drama. Themes: Neo-Noir, Slow-burn. "
                         "Plot: A chemistry teacher turns to crime.")

    def test_the_plot_is_trimmed_by_foundations_rules_not_pythons(self):
        """Each pair is what the Swift composer wrote for that plot. `str.strip` would take the U+001C and
        U+001F and leave the zero-width spaces; Foundation does the opposite."""
        for plot, doc in ((" x\n", "Plot: x"), (" y ", "Plot: y"), ("\tz ", "Plot: z"),
                          ("​w​", "Plot: w"), ("　v", "Plot: v")):
            self.assertEqual(compose.lean([], [], [], plot), doc)

    def test_an_empty_clause_is_left_out_and_the_plot_clause_never_is(self):
        """A title with no plot still reads as a document, not a fragment."""
        self.assertEqual(compose.lean([], [], [], ""), "Plot:")

    def test_the_cap_snaps_back_to_a_sentence(self):
        plot = "One two. Three four. Five six seven."
        self.assertEqual(compose.capped_plot(plot, 22), "One two. Three four.")
        self.assertEqual(compose.capped_plot("no stop anywhere in here", 7), "no stop")
        self.assertEqual(compose.capped_plot(plot, len(plot)), plot)


class Characters(unittest.TestCase):
    def test_the_cap_counts_characters_as_the_swift_composer_did(self):
        for tmdb_id, (plot, length, digest) in GRAPHEMES.items():
            with self.subTest(case=tmdb_id):
                doc = compose.lean(["A B"], ["drama"], ["Heist"], compose.capped_plot(plot, 3500))
                self.assertEqual((len(doc), hashlib.sha256(doc.encode()).hexdigest()[:16]), (length, digest))


class Batches(unittest.TestCase):
    def test_the_newest_batch_wins_a_title_two_batches_disagree_about(self):
        """One Piece is plotless in batch-175 and has its plot in batch-176. Oldest-first, first-wins, it
        embeds with no plot — and `sorted()` puts batch-99 after batch-176."""
        with tempfile.TemporaryDirectory() as out:
            for number, has_plot in ((99, False), (176, True)):
                with open(os.path.join(out, f"batch-{number}.json"), "w", encoding="utf-8") as fh:
                    json.dump([{"tmdbId": 37854, "mediaType": "tv", "overview": "A pirate sets sail.",
                                "hasWikiPlot": has_plot}], fh)
            label = {"subgenres": [], "moods": []}
            tally = {}
            docs = list(compose.documents(out, {"tv:37854": label, "tv:1": label}, {}, set(), 3500, tally))
            self.assertEqual([doc for _, _, doc in docs], ["Plot: A pirate sets sail."])
            self.assertEqual(tally, {})

    def test_an_overview_that_is_not_a_wikipedia_plot_is_never_composed(self):
        """`overview` holds TMDB's text wherever the enrichment found no Wikipedia plot, and TMDB's terms bar
        it from the corpus. Only a row that says `hasWikiPlot` may put it in the document."""
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "batch-1.json"), "w", encoding="utf-8") as fh:
                json.dump([{"tmdbId": 1, "mediaType": "movie", "overview": "TMDB's synopsis.", "hasWikiPlot": False},
                           {"tmdbId": 2, "mediaType": "movie", "overview": "TMDB's synopsis."}], fh)
            label = {"subgenres": [], "moods": []}
            docs = list(compose.documents(out, {"movie:1": label, "movie:2": label}, {}, set(), 3500, {}))
            self.assertEqual([doc for _, _, doc in docs], ["Plot:", "Plot:"])


class Translations(unittest.TestCase):
    """A plot not in English is embedded as the translation made from exactly that plot (#89)."""

    PLOT = "  Ein Anwalt feiert seine Verlobung. Dann geschieht ein Mord.\n"
    ENGLISH = "A lawyer celebrates his engagement. Then a murder happens."

    def compose(self, translations):
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "batch-1.json"), "w", encoding="utf-8") as fh:
                json.dump([{"tmdbId": 1, "mediaType": "movie", "overview": self.PLOT, "hasWikiPlot": True,
                            "plotLanguage": "de", "createdBy": ["A B"]}], fh)
            label = {"subgenres": [{"label": "Legal"}], "moods": []}
            [(_, _, doc)] = compose.documents(out, {"movie:1": label}, {"movie:1": {"genres": ["drama"]}},
                                              set(), 3500, {}, translations)
            return doc

    def source_sha(self):
        return compose.plot_sha(compose.source_plot({"overview": self.PLOT, "hasWikiPlot": True}, 3500))

    def test_the_source_hash_is_of_the_text_after_plot(self):
        doc = self.compose(None)
        self.assertEqual(self.source_sha(), compose.plot_sha(doc[doc.index("Plot: ") + len("Plot: "):]))

    def test_a_matching_translation_is_embedded(self):
        doc = self.compose({("movie:1", self.source_sha()): self.ENGLISH})
        self.assertEqual(doc, "Created by A B. Genres: drama. Themes: Legal. Plot: " + self.ENGLISH)

    def test_a_translation_of_another_plot_is_ignored(self):
        stale = compose.plot_sha("Ein Anwalt feiert seine Verlobung.")
        self.assertEqual(self.compose({("movie:1", stale): self.ENGLISH}), self.compose(None))

    def test_the_prefix_is_byte_identical(self):
        original = self.compose(None)
        translated = self.compose({("movie:1", self.source_sha()): self.ENGLISH})
        cut = original.index("Plot: ") + len("Plot: ")
        self.assertEqual(translated[:cut].encode(), original[:cut].encode())
        self.assertNotEqual(translated, original)

    def test_the_translation_is_capped_like_any_plot(self):
        long = "A sentence. " * 400
        doc = self.compose({("movie:1", self.source_sha()): long})
        self.assertEqual(doc[doc.index("Plot: ") + len("Plot: "):], compose.capped_plot(long, 3500).strip())

    def test_the_cache_reads_last_line_wins_and_skips_a_torn_tail(self):
        rows = [{"key": "movie:1", "source_sha256": "a", "english": "old"},
                {"key": "movie:1", "source_sha256": "a", "english": "new"}]
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, "t.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("".join(json.dumps(r) + "\n" for r in rows) + '{"key": "movie:2", "sour')
            self.assertEqual(compose.read_translations(path), {("movie:1", "a"): "new"})
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"key": "movie:2"\n' + json.dumps(rows[0]) + "\n")
            with self.assertRaisesRegex(Exception, "is not a translation row"):
                compose.read_translations(path)


if __name__ == "__main__":
    unittest.main()
