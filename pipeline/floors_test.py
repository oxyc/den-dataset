#!/usr/bin/env python3
"""The admission floors: which tier a title is judged in, and what a run may override."""
import unittest

from . import floors


class Tiers(unittest.TestCase):
    def test_a_regional_origin_is_judged_by_the_regional_floors(self):
        self.assertEqual(floors.DEFAULT.of({"originCountry": ["FI"]}), (15, 3))
        self.assertEqual(floors.DEFAULT.of({"originCountry": ["US", "SE"]}), (15, 3),
                         "a co-production with one regional origin is in that tier")

    def test_any_other_origin_is_judged_worldwide(self):
        for origin in (["US"], ["JP"], ["TR"], [], None):
            with self.subTest(origin=origin):
                self.assertEqual(floors.DEFAULT.of({"originCountry": origin}), (50, 5))
        self.assertEqual(floors.DEFAULT.of({}), (50, 5))

    def test_japan_is_not_regional(self):
        """Its low-vote tail is mostly anime; its popular titles clear the worldwide floor."""
        self.assertNotIn("JP", floors.REGIONAL_ORIGINS)

    def test_a_regional_title_is_still_in_the_worldwide_tier(self):
        """Raising the regional floors above the worldwide ones must not make a regional title harder to
        admit than any other: it belongs to both tiers and is judged by the lower floor of each."""
        raised = floors.Floors(tmdb=50, regional_tmdb=80, wikipedias=5, regional_wikipedias=9)
        self.assertEqual(raised.of({"originCountry": ["FR"]}), (50, 5))

    def test_the_measured_defaults(self):
        """Measured over the corpus: the median Wikipedia count at each TMDB floor (see the module docstring)."""
        self.assertEqual(floors.DEFAULT, floors.Floors(tmdb=50, regional_tmdb=15, wikipedias=5,
                                                       regional_wikipedias=3))
        self.assertEqual(floors.DEFAULT.lowest_tmdb, 15)


class Given(unittest.TestCase):
    def test_a_named_floor_replaces_its_default_and_only_it(self):
        self.assertEqual(floors.given(wikipedias=8), floors.Floors(50, 15, 8, 3))
        self.assertEqual(floors.given(), floors.DEFAULT)

    def test_zero_is_a_floor_not_an_absence(self):
        """`--vote-floor 0` is how an operator admits the low-vote tail; read as unset it would be ignored."""
        self.assertEqual(floors.given(tmdb=0).tmdb, 0)
        self.assertEqual(floors.given(tmdb=0).of({"originCountry": ["FR"]})[0], 0)


if __name__ == "__main__":
    unittest.main()
