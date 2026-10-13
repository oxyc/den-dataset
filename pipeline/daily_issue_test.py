"""The daily run's failure reporting decides, from what a run left behind, whether to open, update or close an issue.

Nothing here reaches GitHub: `plan` is pure, and the hand-off script runs against a stub `gh` on PATH.
"""
import json
import os
import stat
import subprocess
import tempfile
import unittest

from . import daily_issue

RUN = "https://github.com/oxyc/den-dataset/actions/runs/1"
HERE = os.path.dirname(os.path.abspath(__file__))
FAILED = daily_issue.outcome(False, False, {"verdict": "embed: the canary refused"}, {})
OPEN = {"number": 7, "body": daily_issue.issue_body(3, "2026-10-08", RUN, "x", "")}


class Outcome(unittest.TestCase):
    def test_ready(self):
        self.assertEqual(daily_issue.outcome(True, False, {"verdict": "ok"}, {})["kind"], "ready")

    def test_failed_carries_the_report_verdict(self):
        self.assertEqual(FAILED["kind"], "failed")
        self.assertEqual(FAILED["verdict"], "embed: the canary refused")

    def test_no_report_and_cancelled_are_still_failures(self):
        self.assertIn("before it wrote a report", daily_issue.outcome(False, False, {}, {})["verdict"])
        out = daily_issue.outcome(False, True, {}, {})
        self.assertEqual(out["kind"], "failed")
        self.assertIn("timed out", out["verdict"])

    def test_handoff_state_wins_over_a_cancelled_run(self):
        out = daily_issue.outcome(False, True, {}, {"state": "handed", "why": "w", "cpu": "AMD X"})
        self.assertEqual((out["kind"], out["cpu"]), ("handed", "AMD X"))

    def test_exhausted_says_nobody_did_the_work(self):
        out = daily_issue.outcome(False, False, {}, {"state": "exhausted", "why": "AMD X is blocked"})
        self.assertEqual(out["kind"], "exhausted")
        self.assertIn("no run did the day's work", out["verdict"])

    def test_verdict_is_scrubbed(self):
        v = daily_issue.scrub("fetch failed https://x.test/a?api_key=SECRET1 and token=abc123\n`more`")
        self.assertNotIn("SECRET1", v)
        self.assertNotIn("abc123", v)
        self.assertNotIn("\n", v)
        self.assertNotIn("`", v)
        self.assertLessEqual(len(daily_issue.scrub("x" * 5000)), daily_issue.MAX_VERDICT)


class Plan(unittest.TestCase):
    def plan(self, out, issue=None, cpu_titles=()):
        return daily_issue.plan(out, issue, set(cpu_titles), "2026-10-11", RUN)

    def test_first_failure_opens_the_issue_with_count_one(self):
        (a,) = self.plan(FAILED)
        self.assertEqual((a["do"], a["label"]), ("create", daily_issue.LABEL))
        self.assertIn("Consecutive failed runs: 1", a["body"])
        self.assertIn("`daily: NOT ready — embed: the canary refused`", a["body"])
        self.assertIn(RUN, a["body"])

    def test_repeat_failure_raises_the_count_and_comments(self):
        edit, comment = self.plan(FAILED, OPEN)
        self.assertEqual((edit["do"], edit["number"], comment["do"], comment["number"]), ("edit", 7, "comment", 7))
        self.assertIn("Consecutive failed runs: 4", edit["body"])
        self.assertIn("consecutive failed runs: 4", comment["body"])
        self.assertIn(RUN, comment["body"])

    def test_ready_closes_an_open_issue_with_the_run_link(self):
        (a,) = self.plan(daily_issue.outcome(True, False, {}, {}), OPEN)
        self.assertEqual((a["do"], a["number"]), ("close", 7))
        self.assertIn(RUN, a["body"])

    def test_ready_with_no_issue_does_nothing(self):
        self.assertEqual(self.plan(daily_issue.outcome(True, False, {}, {})), [])

    def test_handed_off_without_a_cpu_does_nothing_even_with_an_issue_open(self):
        self.assertEqual(self.plan(daily_issue.outcome(False, True, {}, {"state": "handed", "why": "w"}), OPEN), [])

    def test_exhausted_counts_as_a_failure(self):
        out = daily_issue.outcome(False, False, {}, {"state": "exhausted", "why": "AMD X blocked"})
        (a,) = self.plan(out)
        self.assertEqual(a["do"], "create")
        self.assertIn("Consecutive failed runs: 1", a["body"])
        self.assertEqual(len(self.plan(out, OPEN)), 2)

    def test_unlisted_cpu_gets_its_own_issue_while_handed_off(self):
        out = daily_issue.outcome(False, True, {}, {"state": "handed", "why": "w", "cpu": "AMD EPYC 9Z99"})
        (a,) = self.plan(out, OPEN)
        self.assertEqual((a["do"], a["label"]), ("create", daily_issue.CPU_LABEL))
        self.assertIn("\nAMD EPYC 9Z99\n", a["body"])
        self.assertIn("data/embed-cpu-blocklist.txt", a["body"])

    def test_unlisted_cpu_issue_is_not_repeated(self):
        out = daily_issue.outcome(False, True, {}, {"state": "handed", "why": "w", "cpu": "AMD EPYC 9Z99"})
        self.assertEqual(self.plan(out, None, ["Add AMD EPYC 9Z99 to the embed CPU blocklist"]), [])

    def test_exhausted_on_an_unlisted_cpu_reports_both(self):
        out = daily_issue.outcome(False, False, {}, {"state": "exhausted", "why": "w", "cpu": "AMD EPYC 9Z99"})
        self.assertEqual([a.get("label") for a in self.plan(out)], [daily_issue.CPU_LABEL, daily_issue.LABEL])


class DataReview(unittest.TestCase):
    def plan(self, found, issue=None):
        return daily_issue.plan_review(found, issue, "2026-10-13", "https://example/run/1")

    def test_contested_titles_and_stale_merges_open_one_issue(self):
        [a] = self.plan((["tv:6618"], ["Q81565646"]))
        self.assertEqual((a["do"], a["label"]), ("create", daily_issue.REVIEW_LABEL))
        self.assertIn("tv:6618", a["body"])
        self.assertIn("Q81565646", a["body"])

    def test_an_open_issue_is_rewritten_and_closed_when_clear(self):
        self.assertEqual(self.plan((["tv:1"], []), {"number": 4})[0]["do"], "edit")
        self.assertEqual(self.plan(([], []), {"number": 4})[0]["do"], "close")
        self.assertEqual(self.plan(([], [])), [])
        self.assertEqual(self.plan(None, {"number": 4}), [])

    def test_reads_both_lists_off_the_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dataset.meta.json")
            with open(path, "w") as fh:
                json.dump({"wikidataItems": {"ambiguous": ["tv:2"]}, "awardMerges": {"stale": []}}, fh)
            self.assertEqual(daily_issue.read_review_items(path), (["tv:2"], []))
            self.assertIsNone(daily_issue.read_review_items(os.path.join(d, "missing.json")))


class AliasReview(unittest.TestCase):
    ROW = {"mediaType": "movie", "tmdbId": 9, "title": "Gamma", "alias": "Be|ta",
           "claimedBy": [{"mediaType": "movie", "tmdbId": 2}]}

    def plan(self, rows, issue=None):
        return daily_issue.plan_aliases(rows, issue, "2026-10-13", "https://example/run/1")

    def test_held_aliases_open_the_issue_with_a_row_each(self):
        [a] = self.plan([self.ROW])
        self.assertEqual((a["do"], a["label"]), ("create", daily_issue.ALIAS_LABEL))
        self.assertIn("| movie:9 Gamma | Be\\|ta | movie:2 |", a["body"])

    def test_an_open_issue_is_rewritten_not_duplicated(self):
        [a] = self.plan([self.ROW], {"number": 7, "body": ""})
        self.assertEqual((a["do"], a["number"]), ("edit", 7))

    def test_none_held_closes_it_and_no_store_says_nothing(self):
        self.assertEqual([a["do"] for a in self.plan([], {"number": 7, "body": ""})], ["close"])
        self.assertEqual(self.plan([]), [])
        self.assertEqual(self.plan(None, {"number": 7, "body": ""}), [])


class HandOffScript(unittest.TestCase):
    """pipeline/hand-off-day.sh against a stub `gh` that records its calls."""

    def run_script(self, attempt, why="AMD X embeds outside", cpu=None):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.mkdir(bin_dir)
            gh = os.path.join(bin_dir, "gh")
            with open(gh, "w") as f:
                f.write(f'#!/bin/sh\necho "$@" >> "{tmp}/calls"\n')
            os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
            # `sleep 300` after a cancel would hang the test; the stub sleep returns at once.
            sleep = os.path.join(bin_dir, "sleep")
            with open(sleep, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(sleep, os.stat(sleep).st_mode | stat.S_IEXEC)
            env = {"PATH": bin_dir + os.pathsep + os.environ["PATH"], "RUNNER_TEMP": tmp, "ATTEMPT": str(attempt),
                   "SPEND": "false", "WEEKS": "8", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "9"}
            args = ["bash", os.path.join(HERE, "hand-off-day.sh"), why] + ([cpu] if cpu else [])
            code = subprocess.run(args, env=env, capture_output=True, text=True).returncode
            try:
                with open(os.path.join(tmp, "calls")) as f:
                    calls = f.read().splitlines()
            except OSError:
                calls = []
            return code, calls, daily_issue.read_handoff(os.path.join(tmp, "day-handoff"))

    def test_a_handed_off_run_starts_the_next_attempt_and_cancels_itself(self):
        code, calls, state = self.run_script(2, cpu="AMD EPYC 9Z99")
        self.assertEqual(code, 1)
        self.assertEqual(state, {"state": "handed", "why": "AMD X embeds outside", "cpu": "AMD EPYC 9Z99"})
        self.assertIn("host_attempt=3", calls[0])
        self.assertTrue(calls[1].startswith("run cancel 9"))

    def test_the_last_attempt_fails_loudly_instead_of_cancelling(self):
        code, calls, state = self.run_script(6)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(state["state"], "exhausted")


if __name__ == "__main__":
    unittest.main()
