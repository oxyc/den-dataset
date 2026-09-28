#!/usr/bin/env python3
"""The two fixed-point encoders' refusals, and the section writer's, which nothing else checks.

`hundredths` and `score_hundredths` each refuse a value carrying more than two decimals rather than
rounding it. That refusal is the whole reason the pair exists: the twentieths encoder it replaced had
no such check and silently rounded 94,498 of 190,116 score values, which shipped. Deleting either
refusal leaves a store that is well formed, passes every structural guard, and is wrong — so the loss
is invisible unless something asserts the refusal itself.

The range refusals are held here too. A confidence of 1.4 or a score of 9.0 is a producer bug, and
without the check it becomes a plausible-looking small integer rather than a stopped run.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store.format import SCORE_NONE, Sections, hundredths, score_hundredths  # noqa: E402


class Hundredths(unittest.TestCase):
    """A probability as u8 hundredths."""

    def test_two_decimals_encode_exactly(self):
        self.assertEqual(hundredths(0.85, "confidence", "movie/1"), 85)
        self.assertEqual(hundredths(0.0, "confidence", "movie/1"), 0)
        self.assertEqual(hundredths(1.0, "confidence", "movie/1"), 100)

    def test_none_is_zero(self):
        self.assertEqual(hundredths(None, "confidence", "movie/1"), 0)

    def test_a_third_decimal_is_refused_rather_than_rounded(self):
        # 0.855 has an exact-looking answer (86) and that answer is a silent precision loss.
        with self.assertRaises(SystemExit) as caught:
            hundredths(0.855, "facet confidence", "movie/603")
        self.assertIn("more than two decimals", str(caught.exception))

    def test_the_refusal_is_not_a_float_artefact(self):
        # 0.07 is not representable in binary either; only genuine third decimals may be refused, or
        # the guard would stop every ordinary run and be deleted for it.
        for value in (0.01, 0.03, 0.07, 0.29, 0.83, 0.99):
            self.assertEqual(hundredths(value, "confidence", "k"), round(value * 100))

    def test_outside_zero_to_one_is_refused(self):
        for value in (-0.01, 1.01, float("nan")):
            with self.assertRaises(SystemExit) as caught:
                hundredths(value, "confidence", "movie/603")
            self.assertIn("not a probability", str(caught.exception))


class ScoreHundredths(unittest.TestCase):
    """A 0..4 axis score as u16 hundredths."""

    def test_two_decimals_encode_exactly(self):
        self.assertEqual(score_hundredths(0.0, "score craft", "tv/1399"), 0)
        self.assertEqual(score_hundredths(2.56, "score craft", "tv/1399"), 256)
        self.assertEqual(score_hundredths(4.0, "score craft", "tv/1399"), 400)

    def test_none_is_the_absent_sentinel_not_a_zero_score(self):
        self.assertEqual(score_hundredths(None, "score craft", "tv/1399"), SCORE_NONE)
        self.assertNotEqual(SCORE_NONE, 0)

    def test_a_third_decimal_is_refused_rather_than_rounded(self):
        with self.assertRaises(SystemExit) as caught:
            score_hundredths(3.725, "score craft", "tv/1399")
        self.assertIn("more than two decimals", str(caught.exception))

    def test_the_refusal_is_not_a_float_artefact(self):
        for value in (0.07, 1.23, 2.56, 3.99, 4.0):
            self.assertEqual(score_hundredths(value, "score craft", "k"), round(value * 100))

    def test_outside_zero_to_four_is_refused(self):
        for value in (-0.01, 4.01, float("nan")):
            with self.assertRaises(SystemExit) as caught:
                score_hundredths(value, "score craft", "tv/1399")
            self.assertIn("outside the 0..4 score range", str(caught.exception))


class SectionRefusals(unittest.TestCase):
    """Every length check `Sections` makes before a block reaches the file.

    Each one is the per-section count assert: a column one row short, or an offsets array that does not span
    the rows, still writes a well-formed file that a reader maps without complaint — every row after the
    tear reads its neighbour's value. A refusal nothing has seen fail is not known to work, so each is
    asserted here rather than trusted from the store round-trip, which only ever feeds it correct columns.
    """

    def refused(self, call, *needles):
        with self.assertRaises(SystemExit) as caught:
            call()
        for needle in needles:
            self.assertIn(needle, str(caught.exception))

    def test_a_column_of_the_wrong_length_is_refused(self):
        sec = Sections(3)
        self.refused(lambda: sec.put("card_year", "h", [1, 2], 2, expect=3), "card_year", "2 values, expected 3")
        sec.put("card_year", "h", [1, 2, 3], 2, expect=3)
        self.assertEqual(sec.order, ["card_year"])

    def test_a_raw_block_of_the_wrong_length_is_refused(self):
        sec = Sections(2)
        self.refused(lambda: sec.put_raw("facet_c", b"\x01\x02\x03", 1, expect=2), "facet_c", "expected 2 x 1")
        sec.put_raw("facet_c", b"\x01\x02", 1, expect=2)
        self.assertEqual(sec.order, ["facet_c"])

    def test_an_empty_raw_block_is_refused_unless_zero_was_expected(self):
        sec = Sections(0)
        self.refused(lambda: sec.put_raw("genres", b"", 1), "genres is empty")
        sec.put_raw("vec_plot", b"", 1, expect=0)
        self.assertEqual(sec.order, ["vec_plot"])

    def test_a_list_whose_offsets_do_not_span_the_rows_is_refused(self):
        sec = Sections(3)
        self.refused(lambda: sec.put_list("cast", "I", 4, [[1], [2]]), "cast", "3 offsets, expected 4")
        self.refused(lambda: sec.put_list("ent_alias", "I", 4, [[1]], expect_rows=2), "2 offsets, expected 3")
        sec.put_list("cast", "I", 4, [[1], [], [2, 3]])
        self.assertEqual(sec.order, ["cast_v", "cast_o"])

    def test_a_list_with_no_values_is_refused_unless_it_may_be_empty(self):
        sec = Sections(2)
        self.refused(lambda: sec.put_list("genres", "I", 4, [[], []]), "genres_v holds no values for 2 rows")
        sec.put_list("versions", "I", 4, [[], []], allow_empty=True)
        self.assertEqual(sec.order, ["versions_v", "versions_o"])

    def test_a_labelled_list_whose_offsets_do_not_span_the_rows_is_refused(self):
        sec = Sections(3)
        self.refused(lambda: sec.put_labelled_list("mood", [[(1, 50)], [(2, 60)]]), "mood", "3 offsets, expected 4")

    def test_a_labelled_list_with_no_values_is_refused(self):
        sec = Sections(2)
        self.refused(lambda: sec.put_labelled_list("subgenre", [[], []]), "subgenre_v holds no values for 2 rows")
        sec.put_labelled_list("subgenre", [[(1, 50)], []])
        self.assertEqual(sec.order, ["subgenre_v", "subgenre_c", "subgenre_o"])


if __name__ == "__main__":
    unittest.main()
