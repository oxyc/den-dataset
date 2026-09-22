"""`run_delta.py` buys from a paid provider, so it must not without `--spend`.

Every test here runs with the provider client and the paid loop replaced, so a regression in the gate fails
an assertion rather than spending money.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import article_sections  # noqa: E402
import run_combined as rc  # noqa: E402
import run_delta  # noqa: E402

TEXT = "Lead paragraph.\n\n== Plot ==\n" + "A story happens. " * 40 + "\n\n== Reception ==\nIt was reviewed.\n"


def article(tmdb_id):
    headings = [section["heading"] for section in article_sections.parse_sections(TEXT)[1:]]
    return {"mediaType": "movie", "tmdbId": tmdb_id, "title": f"Example {tmdb_id}", "year": 2001,
            "article": f"Example {tmdb_id} (film)", "language": "en", "revId": 99,
            "sections": headings, "plotSections": ["Plot"], "text": TEXT}


class SpendGate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.articles = os.path.join(self.dir, "articles.jsonl")
        self.out = os.path.join(self.dir, "delta.jsonl")
        with open(self.articles, "w", encoding="utf-8") as fh:
            for i in (1, 2, 3):
                fh.write(json.dumps(article(i)) + "\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def _run(self, *extra):
        """main() with the provider unreachable: a TypeSafe client or a paid loop would be the regression."""
        err, out = io.StringIO(), io.StringIO()
        # `run_delta` patches `sections_for_record` on the shared module; restore it for the other tests.
        with mock.patch.object(rc, "sections_for_record", rc.sections_for_record), \
                mock.patch.object(rc.TypeSafe, "__init__", side_effect=AssertionError("constructed a paid client")), \
                mock.patch.object(rc, "paid_run", return_value=0) as paid, \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = run_delta.main(["--articles", self.articles, "--out", self.out, *extra])
        return code, paid, out.getvalue(), err.getvalue()

    def test_without_spend_it_refuses_and_says_what_it_would_spend(self):
        code, paid, _, err = self._run()
        self.assertEqual(code, 2)
        paid.assert_not_called()
        self.assertIn("--spend", err)
        self.assertIn("3 titles", err)
        self.assertRegex(err, r"\$\d")

    def test_the_refusal_touches_nothing_on_disk(self):
        self._run()
        self.assertEqual(sorted(os.listdir(self.dir)), ["articles.jsonl"], "no lock, no rows, no manifest")

    def test_with_spend_it_runs_the_paid_pass(self):
        code, paid, _, _ = self._run("--spend")
        self.assertEqual(code, 0)
        paid.assert_called_once()

    def test_the_plan_prices_no_section_questions(self):
        """The delta asks no per-section questions, so its plan must not price any."""
        code, paid, out, _ = self._run("--plan")
        self.assertEqual(code, 0)
        paid.assert_not_called()
        plan = json.loads(out)
        self.assertEqual((plan["titles"], plan["calls"], plan["sectionDecisions"]), (3, 3, 0))


if __name__ == "__main__":
    unittest.main()
