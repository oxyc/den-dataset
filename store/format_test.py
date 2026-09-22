#!/usr/bin/env python3
"""The two fixed-point encoders' refusals, which nothing else checks.

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

from store.format import SCORE_NONE, hundredths, score_hundredths  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
