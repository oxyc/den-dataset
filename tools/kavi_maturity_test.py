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
        def fetch(day):
            calls.append(day)
            return [{"id": 1, "day": day.isoformat()}] if day == first else [{"id": 1}, {"id": 2}]
        with tempfile.TemporaryDirectory() as directory:
            out = os.path.join(directory, "kavi.jsonl")
            result = kavi_maturity.collect(out, first, first + datetime.timedelta(days=1), 0, fetch)
            again = kavi_maturity.collect(out, first, first + datetime.timedelta(days=1), 0, fetch)
            with open(out, encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
        self.assertEqual((result["days"], result["records"], result["written"]), (2, 2, 2))
        self.assertEqual(again["written"], 0)
        self.assertEqual(calls, [first, first + datetime.timedelta(days=1)])
        self.assertEqual([row["id"] for row in rows], [1, 2])

    def test_refuses_a_window_at_the_undocumented_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            out = os.path.join(directory, "kavi.jsonl")
            with self.assertRaisesRegex(SystemExit, "refusing a truncated day"):
                kavi_maturity.collect(out, datetime.date(2026, 1, 1), datetime.date(2026, 1, 1), 0,
                                      lambda _day: [{"id": i} for i in range(100)])
            self.assertFalse(os.path.exists(out + ".state.json"))


if __name__ == "__main__":
    unittest.main()
