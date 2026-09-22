"""`run_delta.py` buys from a paid provider, so it must not without `--spend`, and what it buys must be
answers about the article, not the title.

Every test here runs with the provider client replaced, so a regression fails an assertion rather than
spending money.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_delta  # noqa: E402
from delta_questions import delta_questions  # noqa: E402
from pipeline import article_sections, combined_questions  # noqa: E402
from pipeline import run_combined as rc  # noqa: E402
from pipeline.run_combined_test import FakeClient, answer_for  # noqa: E402

TEXT = "Lead paragraph.\n\n== Plot ==\n" + "A story happens. " * 40 + "\n\n== Reception ==\nIt was reviewed.\n"
# Too long to send whole under LONG_MAX, so the corpus pass sent it as role-selected sections.
LONG_TEXT = "Lead.\n\n== Plot ==\n" + "P" * 2000 + "\n== Reception ==\n" + "R" * 2000
LONG_MAX = 3000
MANIFEST = {"runId": "r", "configSha256": "f" * 64,
            "config": {"requestedModel": combined_questions.PINNED_MODEL}}


def article(tmdb_id, text=TEXT):
    headings = [section["heading"] for section in article_sections.parse_sections(text)[1:]]
    return {"mediaType": "movie", "tmdbId": tmdb_id, "title": f"Example {tmdb_id}", "year": 2001,
            "article": f"Example {tmdb_id} (film)", "language": "en", "revId": 99,
            "sections": headings, "plotSections": ["Plot"], "text": text}


def corpus_row(rec, max_state_chars=rc.DEFAULT_MAX_STATE_CHARS):
    """The corpus pass's row for `rec`, and the client that recorded what it sent."""
    client = FakeClient()
    global_qs, _, _ = combined_questions.global_questions()
    return rc.classify(rec, client, global_qs, MANIFEST, max_state_chars), client


class StubTypeSafe:
    """The paid client's surface as `paid_run` uses it, recording every request instead of sending it."""
    requests = []
    RATE_PER_INPUT_TOKEN = rc.TypeSafe.RATE_PER_INPUT_TOKEN

    def __init__(self, model):
        self.model = model
        self.calls = self.input_tokens = self.spend = 0

    def ask_with_metadata(self, state, questions):
        self.__class__.requests.append((state, questions))
        self.calls += 1
        return ({key: answer_for(question) for key, question in questions.items()},
                {"model": self.model, "usage": {"input_tokens": 10, "output_tokens": 5}})

    def summary(self):
        return ""


class DeltaCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.articles = os.path.join(self.dir, "articles.jsonl")
        self.out = os.path.join(self.dir, "delta.jsonl")
        self.combined = os.path.join(self.dir, "combined.jsonl")
        self.write_articles([article(i) for i in (1, 2, 3)])
        self.write_combined([corpus_row(article(i))[0] for i in (1, 2, 3)])
        StubTypeSafe.requests = []

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write_articles(self, records):
        with open(self.articles, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def write_combined(self, rows):
        with open(self.combined, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def _run(self, *extra, paid=True, combined=True):
        """main() with the provider stubbed; with `paid=False` the paid loop is replaced too."""
        err, out = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            if paid:
                stack.enter_context(mock.patch.object(rc, "TypeSafe", StubTypeSafe))
                loop = None
            else:
                stack.enter_context(mock.patch.object(
                    rc.TypeSafe, "__init__", side_effect=AssertionError("constructed a paid client")))
                loop = stack.enter_context(mock.patch.object(rc, "paid_run", return_value=0))
            stack.enter_context(contextlib.redirect_stderr(err))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = run_delta.main(["--articles", self.articles, "--out", self.out,
                                   *(["--combined", self.combined] if combined else []), *extra])
        return code, loop, out.getvalue(), err.getvalue()

    def rows(self):
        with open(self.out, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]


class SpendGate(DeltaCase):
    def test_without_spend_it_refuses_and_says_what_it_would_spend(self):
        code, paid, _, err = self._run(paid=False)
        self.assertEqual(code, 2)
        paid.assert_not_called()
        self.assertIn("--spend", err)
        self.assertIn("3 titles", err)
        self.assertRegex(err, r"\$\d")

    def test_the_refusal_touches_nothing_on_disk(self):
        self._run(paid=False)
        self.assertEqual(sorted(os.listdir(self.dir)), ["articles.jsonl", "combined.jsonl"],
                         "no lock, no rows, no manifest")

    def test_with_spend_it_runs_the_paid_pass(self):
        code, paid, _, _ = self._run("--spend", paid=False)
        self.assertEqual(code, 0)
        paid.assert_called_once()

    def test_the_plan_prices_the_article_and_no_section_questions(self):
        code, paid, out, _ = self._run("--plan", paid=False)
        self.assertEqual(code, 0)
        paid.assert_not_called()
        plan = json.loads(out)
        self.assertEqual((plan["titles"], plan["calls"], plan["sectionDecisions"]), (3, 3, 0))
        rec = article(1)
        state = article_sections.state_for(rec, article_sections.parse_sections(TEXT, rec["plotSections"]))
        per_title = article_sections.encoded_chars(state) + len(rc.canonical(delta_questions()))
        self.assertEqual(plan["roughInputTokens"], round(3 * per_title / 4))


class DeltaSendsTheArticle(DeltaCase):
    def test_every_request_carries_the_whole_article_and_asks_only_the_delta(self):
        code, _, _, _ = self._run("--spend")
        self.assertEqual(code, 0)
        self.assertEqual(len(StubTypeSafe.requests), 3)
        rec = article(1)
        sections = article_sections.parse_sections(TEXT, rec["plotSections"])
        for state, questions in StubTypeSafe.requests:
            self.assertEqual(state["article"]["sections"],
                             article_sections.state_for(rec, sections)["article"]["sections"])
            self.assertEqual(questions, delta_questions())
        for row in self.rows():
            self.assertEqual(row["globalStateSectionIds"], [s["id"] for s in sections])
            self.assertEqual(row["calls"][0]["sectionIds"], [s["id"] for s in sections])
            self.assertGreater(row["calls"][0]["stateChars"], len(TEXT))
            self.assertEqual(row["omittedFromGlobalState"], [])
            self.assertEqual(set(row["answers"]), set(delta_questions()))
            self.assertTrue(all("role" not in section for section in row["sections"]))

    def test_the_combined_pass_is_left_whole(self):
        """The delta no longer changes shared state: the corpus pass still asks its section questions."""
        self._run("--spend")
        row, client = corpus_row(article(1))
        global_qs, _, _ = combined_questions.global_questions()
        self.assertEqual(len(client.calls[0][1]), len(global_qs) + 3)
        self.assertTrue(all("role" in section for section in row["sections"]))


class LongArticles(DeltaCase):
    """An article the corpus run could not send whole is sent as the sections that run sent.

    The corpus row here was made under a lower ceiling than the delta runs at — the capacity-fallback shards'
    case — so recomputing the state under the delta's own ceiling would send the whole article instead.
    """

    def setUp(self):
        super().setUp()
        self.long = article(9, LONG_TEXT)
        self.write_articles([article(1), self.long])
        self.long_row, self.corpus_client = corpus_row(self.long, LONG_MAX)
        self.assertTrue(self.long_row["oversized"])
        self.assertLess(len(self.long_row["globalStateSectionIds"]), len(self.long_row["sections"]))
        self.write_combined([corpus_row(article(1))[0], self.long_row])

    def test_it_sends_the_state_the_corpus_run_sent(self):
        code, _, _, _ = self._run("--spend")
        self.assertEqual(code, 0)
        corpus_global_state = self.corpus_client.calls[-1][0]
        delta_long_state = [state for state, _ in StubTypeSafe.requests
                            if state["requestedTarget"]["tmdbId"] == 9]
        self.assertEqual(delta_long_state, [corpus_global_state])
        row = next(row for row in self.rows() if row["tmdbId"] == 9)
        self.assertEqual(row["globalStateSectionIds"], self.long_row["globalStateSectionIds"])
        self.assertEqual(row["omittedFromGlobalState"], self.long_row["omittedFromGlobalState"])
        self.assertEqual(row["calls"][0]["phase"], "global-after-section-audit")
        self.assertEqual(len(row["calls"]), 1)

    def test_the_plan_prices_the_state_the_corpus_run_sent(self):
        code, _, out, _ = self._run("--plan")
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual((plan["calls"], plan["oversizedTitles"], plan["sectionDecisions"]), (2, 1, 0))
        sent = [s for s in article_sections.parse_sections(LONG_TEXT, self.long["plotSections"])
                if s["id"] in self.long_row["globalStateSectionIds"]]
        whole = article_sections.state_for(article(1), article_sections.parse_sections(TEXT, ["Plot"]))
        chars = (article_sections.encoded_chars(article_sections.state_for(self.long, sent))
                 + article_sections.encoded_chars(whole) + 2 * len(rc.canonical(delta_questions())))
        self.assertEqual(plan["roughInputTokens"], round(chars / 4))

    def test_a_title_without_a_corpus_row_is_refused_before_spending(self):
        self.write_combined([self.long_row])
        with self.assertRaisesRegex(SystemExit, "movie:1 has no state.*--combined"):
            self._run("--spend")
        self.assertEqual(StubTypeSafe.requests, [])

    def test_the_corpus_rows_are_required(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self._run("--plan", combined=False)

    def test_a_row_from_a_different_article_is_refused(self):
        self.long_row["articleSha256"] = "0" * 64
        self.write_combined([corpus_row(article(1))[0], self.long_row])
        with self.assertRaisesRegex(SystemExit, "different article"):
            self._run("--plan")

    def test_a_title_in_two_shards_is_refused(self):
        second = os.path.join(self.dir, "combined-2.jsonl")
        with open(second, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.long_row) + "\n")
        with self.assertRaisesRegex(SystemExit, "more than one"):
            self._run("--plan", "--combined", second)


class NoTitleOnlyState(unittest.TestCase):
    def test_a_state_without_article_sections_is_never_sent(self):
        client = FakeClient()
        state = article_sections.state_for(article(1), [])
        with self.assertRaisesRegex(ValueError, "no article section"):
            rc.call(client, state, delta_questions(), "global", [])
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
