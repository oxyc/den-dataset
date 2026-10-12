import datetime
import json
import os
import tempfile
import unittest

from . import kavi_maturity


class Collector(unittest.TestCase):
    def test_collects_days_resumes_and_deduplicates_ids(self):
        first = datetime.date(2026, 1, 1)
        calls = []
        def fetch(begin, end):
            calls.append((begin, end))
            return [{"id": 1}, {"id": 2}]
        with tempfile.TemporaryDirectory() as directory:
            out = os.path.join(directory, "kavi.jsonl")
            result = kavi_maturity.collect(out, first, first + datetime.timedelta(days=1), 0, fetch)
            again = kavi_maturity.collect(out, first, first + datetime.timedelta(days=1), 0, fetch)
            with open(out, encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
        self.assertEqual((result["days"], result["records"], result["written"]), (2, 2, 2))
        self.assertEqual(again["written"], 0)
        self.assertEqual(calls, [(first, first + datetime.timedelta(days=1))])
        self.assertEqual([row["id"] for row in rows], [1, 2])

    def test_refuses_a_window_at_the_undocumented_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            out = os.path.join(directory, "kavi.jsonl")
            with self.assertRaisesRegex(SystemExit, "refusing a truncated day"):
                kavi_maturity.collect(out, datetime.date(2026, 1, 1), datetime.date(2026, 1, 1), 0,
                                      lambda _begin, _end: [{"id": i} for i in range(100)])
            self.assertFalse(os.path.exists(out + ".state.json"))

    def test_bisects_a_saturated_window_before_writing_it(self):
        first = datetime.date(2026, 1, 1)
        calls = []
        def fetch(begin, end):
            calls.append((begin, end))
            return ([{"id": i} for i in range(100)] if (end - begin).days > 0
                    else [{"id": begin.day}])
        with tempfile.TemporaryDirectory() as directory:
            result = kavi_maturity.collect(os.path.join(directory, "kavi.jsonl"), first,
                                           first + datetime.timedelta(days=2), 0, fetch)
        self.assertEqual(result["days"], 3)
        self.assertEqual(result["records"], 3)
        self.assertEqual(calls[0], (first, first + datetime.timedelta(days=2)))
        self.assertTrue(any((end - begin).days == 0 for begin, end in calls[1:]))


if __name__ == "__main__":
    unittest.main()
