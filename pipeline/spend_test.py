import datetime
import json
import os
import tempfile
import unittest

from .contract import StageError
from .spend import Ledger, month_to_date


class PublishedReports(unittest.TestCase):
    def test_month_fixture_is_summed_and_other_month_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            for name, date, amount in (("a.json", "2026-09-01", .12), ("b.json", "2026-09-29", .34),
                                       ("old.json", "2026-08-31", 9.0)):
                with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
                    json.dump({"date": date, "spend": {"totalUSD": amount}}, handle)
            self.assertEqual(month_to_date(root, datetime.date(2026, 9, 30)), {"usd": .46, "reports": 2})

    def test_malformed_published_report_is_not_counted_as_zero(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "bad.json"), "w", encoding="utf-8") as handle:
                handle.write("{}")
            with self.assertRaisesRegex(StageError, "no valid date"):
                month_to_date(root, datetime.date(2026, 9, 30))


class Caps(unittest.TestCase):
    def test_step_cap_refuses_before_reservation(self):
        ledger = Ledger({"usd": 0, "reports": 0}, 10)
        with self.assertRaisesRegex(StageError, "daily cap"):
            ledger.reserve("premiseTags", 1.01, 1.0)
        self.assertEqual(ledger.steps, {})

    def test_monthly_cap_includes_published_reports_and_other_steps(self):
        ledger = Ledger({"usd": .80, "reports": 3}, 1.0)
        ledger.reserve("typesafe", .10, 1.0)
        with self.assertRaisesRegex(StageError, "monthly cap"):
            ledger.reserve("premiseTags", .11, 1.0)


if __name__ == "__main__":
    unittest.main()
