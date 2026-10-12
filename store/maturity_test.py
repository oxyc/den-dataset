import unittest
from unittest import mock

from .format import AGE_NONE, Sections
from .maturity import Maturity


class MaturityColumn(unittest.TestCase):
    def test_numeric_age_and_unknown_have_distinct_bytes(self):
        column = Maturity()
        column.add("movie:1", {"maturity": {"minimumAge": 0}})
        column.add("movie:2", {"maturity": {"minimumAge": 13}})
        column.add("movie:3", {"maturity": None})
        sections = Sections(3)
        column.put(sections, 3)
        self.assertEqual(sections.blocks["minimum_age"], bytes([0, 13, AGE_NONE]))
        self.assertEqual(column.known, 2)

    def test_out_of_range_non_integer_and_boolean_are_refused(self):
        for value in (-1, 19, 12.5, True, "12"):
            with self.subTest(value=value), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                Maturity().add("movie:1", {"maturity": {"minimumAge": value}})


if __name__ == "__main__":
    unittest.main()
