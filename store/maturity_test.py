import unittest
from unittest import mock

from .format import AGE_NONE, U32_NONE, Sections, Strings
from .maturity import Maturity


class MaturityColumn(unittest.TestCase):
    def test_numeric_age_and_unknown_have_distinct_bytes(self):
        column = Maturity()
        rows = [
            ("movie:1", {"maturity": {"minimumAge": 0, "observations": [
                {"authority": "one", "authorityId": "1", "raw": "G", "age": 0}]}}),
            ("movie:2", {"maturity": {"minimumAge": 13, "observations": [
                {"authority": "two", "authorityId": "2", "raw": "13+", "age": 13,
                 "assessed": "2026-01-02", "runtimeMinutes": 90, "format": "cinema"}]}}),
            ("movie:3", {"maturity": None}),
        ]
        strings = Strings()
        for key, row in rows:
            column.intern(strings, key, row)
        strings.freeze()
        for key, row in rows:
            column.add(strings, key, row)
        sections = Sections(3)
        column.put(sections, 3)
        self.assertEqual(sections.blocks["minimum_age"], bytes([0, 13, AGE_NONE]))
        self.assertEqual(sections.blocks["mat_age"], bytes([0, 13]))
        self.assertEqual(list(sections.blocks["mat_runtime"]), [0, 0, 90, 0])
        self.assertNotEqual(sections.blocks["mat_format"][-4:], U32_NONE.to_bytes(4, "little"))
        self.assertEqual(column.known, 2)
        self.assertEqual(column.evidence, 2)

    def test_out_of_range_non_integer_and_boolean_are_refused(self):
        for value in (-1, 19, 12.5, True, "12"):
            with self.subTest(value=value), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                Maturity().intern(Strings(), "movie:1", {"maturity": {"minimumAge": value,
                    "observations": [{"authority": "one", "authorityId": "1", "raw": "x", "age": value}]}})

    def test_a_reported_rating_can_have_no_normalized_age(self):
        row = {"maturity": {"minimumAge": None, "observations": [
            {"authority": "Medietilsynet", "authorityId": "n:1",
             "raw": "Tillatt for alle", "age": None}]}}
        strings, column = Strings(), Maturity()
        column.intern(strings, "movie:1", row)
        strings.freeze()
        column.add(strings, "movie:1", row)
        sections = Sections(1)
        column.put(sections, 1)
        self.assertEqual(sections.blocks["minimum_age"], bytes([AGE_NONE]))
        self.assertEqual(sections.blocks["mat_age"], bytes([AGE_NONE]))


if __name__ == "__main__":
    unittest.main()
