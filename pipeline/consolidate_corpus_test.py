"""`consolidate_corpus.py` — the join that builds the dataset's source of truth.

It had no test, which is the same shape of problem it exists to solve: every failure this file guards
against actually happened, silently, and was found by someone reading output rather than by anything
failing. Each test below is one of them.

  - 89 titles carry facts and no pass row. Iterating the pass instead of the union dropped all 89 — the
    records nothing else covers (no labels, no vectors, no facets), so a coverage check against
    `labelsRecords` read 99.98% and passed.
  - `labels` was null on all 47,529 rows because `by_key` fell through to the wrapper dict, which is a
    dict, so every lookup missed and nothing complained.
  - Eleven titles were absent from a derived blob for a day because a producer read one shard of three.

Run: `python3 -m unittest pipeline/consolidate_corpus_test.py`
"""
import contextlib
import gzip
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "consolidate_corpus.py")


def load():
    spec = importlib.util.spec_from_file_location("consolidate_corpus", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cc = load()


def write(path, rows):
    """One JSON object per line."""
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def combined(tmdb_id, media="movie", answers=None):
    return {"mediaType": media, "tmdbId": tmdb_id, "answers": answers or {"tone": {"choice": "bleak"}}}


def facts_file(path, keys, entities=None):
    blob = {
        "records": [{"mediaType": k.split(":")[0], "tmdbId": int(k.split(":")[1]), "countries": ["US"]}
                    for k in keys],
        "entities": entities or {"Q1": {"en": "Ada Director"}},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(blob, fh)


def labels_file(path, keys, wrapper="records"):
    rows = [{"mediaType": k.split(":")[0], "tmdbId": int(k.split(":")[1]), "primaryGenre": "Crime"}
            for k in keys]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({wrapper: rows} if wrapper else rows, fh)


def genres_moods_file(path, keys):
    """`genres-moods.json` as the genres & moods stage writes it: titles keyed `mediaType:tmdbId`."""
    entry = {"animated": False, "moods": [], "primaryGenre": "Crime", "primaryGenreSource": "jev-v3",
             "source": "jev-v3", "subgenres": []}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"count": len(keys), "taxonomyVersion": "t02", "titles": {k: entry for k in keys}}, fh)


def run(dir, combined_paths, delta_paths, facts, labels, out, extra=()):
    """The script as the pipeline runs it, returning (exit code, stderr)."""
    argv = [sys.executable, SCRIPT]
    for path in combined_paths:
        argv += ["--combined", path]
    for path in delta_paths:
        argv += ["--delta", path]
    argv += ["--facts", facts, "--labels", labels, "--out", out, *extra]
    done = subprocess.run(argv, capture_output=True, text=True, cwd=dir)
    return done.returncode, done.stderr


def read(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class Spine(unittest.TestCase):
    def test_a_title_with_facts_and_no_pass_row_still_reaches_the_corpus(self):
        """THE 89-title bug. The spine is the union of facts and the pass, not the pass alone.

        These are the records nothing else covers, so their loss is invisible to every count that
        divides by `labelsRecords` — and `fit.rs` reads them to judge library titles outside the index.
        """
        with tempfile.TemporaryDirectory() as dir:
            c = os.path.join(dir, "combined.jsonl")
            d = os.path.join(dir, "delta.jsonl")
            f = os.path.join(dir, "facts.json")
            l = os.path.join(dir, "labels.json")
            out = os.path.join(dir, "corpus.jsonl")
            write(c, [combined(1)])
            write(d, [])
            facts_file(f, ["movie:1", "movie:2"])   # movie:2 has facts and no pass row
            labels_file(l, ["movie:1", "movie:2"])
            code, err = run(dir, [c], [d], f, l, out)
            self.assertEqual(code, 0, err)
            rows = {r["key"]: r for r in read(out)}
            self.assertEqual(sorted(rows), ["movie:1", "movie:2"])
            self.assertEqual(rows["movie:2"]["facts"]["countries"], ["US"])
            self.assertEqual(rows["movie:2"]["tmdbId"], 2, "the id is recovered from the key")

    def test_a_facts_only_title_is_counted_as_such(self):
        with tempfile.TemporaryDirectory() as dir:
            paths = {n: os.path.join(dir, n) for n in
                     ("combined.jsonl", "delta.jsonl", "facts.json", "labels.json", "corpus.jsonl")}
            write(paths["combined.jsonl"], [combined(1)])
            write(paths["delta.jsonl"], [])
            facts_file(paths["facts.json"], ["movie:1", "movie:2", "tv:9"])
            labels_file(paths["labels.json"], ["movie:1", "movie:2", "tv:9"])
            argv = [sys.executable, SCRIPT, "--combined", paths["combined.jsonl"],
                    "--delta", paths["delta.jsonl"], "--facts", paths["facts.json"],
                    "--labels", paths["labels.json"], "--out", paths["corpus.jsonl"]]
            done = subprocess.run(argv, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)["factsOnly"], 2)


class Joins(unittest.TestCase):
    def test_a_wrapper_with_no_known_rows_key_fails_instead_of_returning_itself(self):
        """THE `labels: null` bug, at its source.

        `by_key` used to fall through to the wrapper dict. A dict is dict-like, so every lookup missed
        and every row was written with `labels: null` — for all 47,529 titles, with no error.
        """
        with tempfile.TemporaryDirectory() as dir:
            path = os.path.join(dir, "labels.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"taxonomyVersion": "t02", "rows": [{"tmdbId": 1, "mediaType": "movie"}]}, fh)
            with self.assertRaises(SystemExit) as raised:
                cc.by_key(path, "labels")
            self.assertIn("no recognisable rows", str(raised.exception))

    def test_the_genres_moods_file_is_joined_under_its_titles_and_nothing_else_labels_a_title(self):
        """`genres-moods.json` nests its titles under `titles`; read as an unknown wrapper it would be
        refused, and read as the rows it would miss every key. The premise labels are not a second
        source any more, so no row carries them."""
        with tempfile.TemporaryDirectory() as dir:
            c, d = os.path.join(dir, "c.jsonl"), os.path.join(dir, "d.jsonl")
            f, l = os.path.join(dir, "f.json"), os.path.join(dir, "genres-moods.json")
            out = os.path.join(dir, "corpus.jsonl")
            write(c, [combined(1)])
            write(d, [])
            facts_file(f, ["movie:1", "movie:2"])
            genres_moods_file(l, ["movie:1"])
            code, err = run(dir, [c], [d], f, l, out)
            self.assertEqual(code, 0, err)
            rows = {r["key"]: r for r in read(out)}
            self.assertEqual(rows["movie:1"]["labels"]["primaryGenre"], "Crime")
            self.assertEqual(rows["movie:1"]["labels"]["source"], "jev-v3")
            self.assertIsNone(rows["movie:2"]["labels"])
            self.assertNotIn("premiseLabels", rows["movie:1"])
            self.assertNotIn("--premise-labels", cc.build_parser().format_help())

    def test_a_bare_mapping_of_key_to_row_is_accepted(self):
        with tempfile.TemporaryDirectory() as dir:
            path = os.path.join(dir, "labels.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"movie:1": {"primaryGenre": "Crime"}, "tv:9": {"primaryGenre": "Drama"}}, fh)
            self.assertEqual(sorted(cc.by_key(path, "labels")), ["movie:1", "tv:9"])

    def test_a_labels_join_that_mostly_misses_is_fatal(self):
        """A join producing almost nothing is a bug in the join, not a corpus that lacks the data.

        The artifact names ten titles and the corpus holds ten; nine of its keys match nothing, so nine
        records never arrive. Measured against the ARTIFACT, which is the only thing that knows how many
        there were meant to be."""
        with tempfile.TemporaryDirectory() as dir:
            c, d = os.path.join(dir, "c.jsonl"), os.path.join(dir, "d.jsonl")
            f, l = os.path.join(dir, "f.json"), os.path.join(dir, "l.json")
            out = os.path.join(dir, "corpus.jsonl")
            write(c, [combined(i) for i in range(1, 11)])
            write(d, [])
            facts_file(f, [f"movie:{i}" for i in range(1, 11)])
            # Ten records, one of which matches a corpus key — a join that missed, not a small artifact.
            labels_file(l, ["movie:1"] + [f"movie:{i}" for i in range(900, 909)])
            code, err = run(dir, [c], [d], f, l, out)
            self.assertEqual(code, 1)
            self.assertIn("did not reach the corpus", err)

    def test_a_join_losing_records_while_clearing_the_old_half_the_corpus_floor_is_fatal(self):
        """The case `hits < written * 0.5` could not see.

        Six of the artifact's eleven records reach a ten-row corpus. Six clears half of ten, so the old
        floor was satisfied while five records were lost in silence. In production the premise pass covers
        44,531 of 47,618 rows and could shed twenty thousand records the same way."""
        with tempfile.TemporaryDirectory() as dir:
            c, d = os.path.join(dir, "c.jsonl"), os.path.join(dir, "d.jsonl")
            f, l = os.path.join(dir, "f.json"), os.path.join(dir, "l.json")
            out = os.path.join(dir, "corpus.jsonl")
            write(c, [combined(i) for i in range(1, 11)])
            write(d, [])
            facts_file(f, [f"movie:{i}" for i in range(1, 11)])
            labels_file(l, [f"movie:{i}" for i in range(1, 7)] + [f"movie:{i}" for i in range(900, 905)])
            code, err = run(dir, [c], [d], f, l, out)
            self.assertEqual(code, 1, f"expected a refusal, got {code}: {err}")
            self.assertIn("5 of 11 records", err)


class Shards(unittest.TestCase):
    def test_every_shard_is_read(self):
        """Eleven titles went missing for a day because a producer read one shard of three."""
        with tempfile.TemporaryDirectory() as dir:
            a, b = os.path.join(dir, "a.jsonl"), os.path.join(dir, "b.jsonl")
            d = os.path.join(dir, "d.jsonl")
            f, l = os.path.join(dir, "f.json"), os.path.join(dir, "l.json")
            out = os.path.join(dir, "corpus.jsonl")
            write(a, [combined(1)])
            write(b, [combined(2)])
            write(d, [])
            facts_file(f, ["movie:1", "movie:2"])
            labels_file(l, ["movie:1", "movie:2"])
            code, err = run(dir, [a, b], [d], f, l, out)
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted(r["key"] for r in read(out)), ["movie:1", "movie:2"])

    def test_the_same_title_in_two_shards_is_refused(self):
        """An overlap means the same title was classified twice. With no manifest recording when either
        run started, nothing says which answer is newer, and one would win arbitrarily."""
        with tempfile.TemporaryDirectory() as dir:
            a, b = os.path.join(dir, "a.jsonl"), os.path.join(dir, "b.jsonl")
            d = os.path.join(dir, "d.jsonl")
            f, l = os.path.join(dir, "f.json"), os.path.join(dir, "l.json")
            out = os.path.join(dir, "corpus.jsonl")
            write(a, [combined(1)])
            write(b, [combined(1)])
            write(d, [])
            facts_file(f, ["movie:1"])
            labels_file(l, ["movie:1"])
            code, err = run(dir, [a, b], [d], f, l, out)
            self.assertEqual(code, 1)
            self.assertIn("duplicate key", err)

    def test_a_short_run_is_refused_when_the_count_is_declared(self):
        with tempfile.TemporaryDirectory() as dir:
            c, d = os.path.join(dir, "c.jsonl"), os.path.join(dir, "d.jsonl")
            f, l = os.path.join(dir, "f.json"), os.path.join(dir, "l.json")
            out = os.path.join(dir, "corpus.jsonl")
            write(c, [combined(1)])
            write(d, [])
            facts_file(f, ["movie:1"])
            labels_file(l, ["movie:1"])
            code, err = run(dir, [c], [d], f, l, out, extra=["--expect", "2"])
            self.assertEqual(code, 1)
            self.assertIn("a shard is missing", err)


def started(shard, when):
    """The sidecar a pass writes beside a shard, reduced to the stamp the supersede rule reads."""
    with open(shard + ".manifest.json", "w", encoding="utf-8") as fh:
        json.dump({"runId": os.path.basename(shard), "runStartedAt": when}, fh)


EARLY, LATE = "2026-09-19T16:13:40+00:00", "2026-09-23T09:00:00+00:00"


class Supersede(unittest.TestCase):
    """A title classified again on a corrected article, into a new shard beside the old one (#64)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.p ={n: os.path.join(self.dir, n) for n in ("f.json", "l.json", "corpus.jsonl")}
        # Named so that path order and run order disagree: the NEWER run sorts first by name.
        self.old, self.new = os.path.join(self.dir, "z-old.jsonl"), os.path.join(self.dir, "a-new.jsonl")
        self.old_d, self.new_d = os.path.join(self.dir, "z-old-d.jsonl"), os.path.join(self.dir, "a-new-d.jsonl")
        write(self.old, [combined(1, answers={"tone": {"choice": "bleak"}}) | {"articleSha256": "wrong"},
                         combined(2) | {"articleSha256": "two"}])
        write(self.new, [combined(1, answers={"tone": {"choice": "hopeful"}}) | {"articleSha256": "right"}])
        write(self.old_d, [{"mediaType": "movie", "tmdbId": 1, "articleSha256": "wrong",
                            "answers": {"critique__craft": {"p": 0.1}}}])
        write(self.new_d, [{"mediaType": "movie", "tmdbId": 1, "articleSha256": "right",
                            "answers": {"critique__craft": {"p": 0.9}}}])
        for shard, when in ((self.old, EARLY), (self.new, LATE), (self.old_d, EARLY), (self.new_d, LATE)):
            started(shard, when)
        facts_file(self.p["f.json"], ["movie:1", "movie:2"])
        labels_file(self.p["l.json"], ["movie:1", "movie:2"])

    def join(self, combined_paths, delta_paths, extra=(), out=None):
        argv = [sys.executable, SCRIPT]
        for path in combined_paths:
            argv += ["--combined", path]
        for path in delta_paths:
            argv += ["--delta", path]
        argv += ["--facts", self.p["f.json"], "--labels", self.p["l.json"],
                 "--out", out or self.p["corpus.jsonl"], *extra]
        return subprocess.run(argv, capture_output=True, text=True)

    def test_the_run_that_started_later_wins_whatever_order_the_shards_are_passed_in(self):
        first = os.path.join(self.dir, "first.jsonl")
        done = self.join([self.old, self.new], [self.old_d, self.new_d], out=first)
        self.assertEqual(done.returncode, 0, done.stderr)
        rows = {r["key"]: r for r in read(first)}
        self.assertEqual(rows["movie:1"]["facets"]["tone"]["choice"], "hopeful")
        self.assertEqual(rows["movie:1"]["critique"]["craft"], {"p": 0.9})
        self.assertEqual(rows["movie:2"]["facets"]["tone"]["choice"], "bleak", "the old shard's other title")
        second = os.path.join(self.dir, "second.jsonl")
        done = self.join([self.new, self.old], [self.new_d, self.old_d], out=second)
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(first, "rb") as a, open(second, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_the_report_says_how_many_keys_each_shard_superseded(self):
        done = self.join([self.new, self.old], [self.new_d, self.old_d])
        self.assertEqual(done.returncode, 0, done.stderr)
        shards = json.loads(done.stdout)["combined"]["shards"]
        self.assertEqual([(s["shard"], s["runStartedAt"], s["rows"], s["supersedes"], s["superseded"])
                          for s in shards],
                         [("z-old.jsonl", EARLY, 2, 0, 1), ("a-new.jsonl", LATE, 1, 1, 0)],
                         "oldest run first, by the manifest, not by name")
        delta = json.loads(done.stdout)["delta"]["shards"]
        self.assertEqual([(s["shard"], s["supersedes"]) for s in delta], [("z-old-d.jsonl", 0), ("a-new-d.jsonl", 1)])

    def test_a_key_twice_within_one_shard_is_still_refused(self):
        write(self.new, [combined(1) | {"articleSha256": "right"}, combined(1) | {"articleSha256": "right"}])
        done = self.join([self.old, self.new], [self.old_d, self.new_d])
        self.assertEqual(done.returncode, 1)
        self.assertIn("duplicate key within combined shard", done.stderr)

    def test_two_runs_that_started_at_the_same_instant_are_refused(self):
        started(self.new, EARLY)
        done = self.join([self.old, self.new], [self.old_d, self.new_d])
        self.assertEqual(done.returncode, 1)
        self.assertIn("neither is later", done.stderr)

    def test_an_overlap_with_a_shard_that_records_no_start_is_refused(self):
        os.remove(self.new + ".manifest.json")
        done = self.join([self.old, self.new], [self.old_d, self.new_d])
        self.assertEqual(done.returncode, 1)
        self.assertIn("records none", done.stderr)

    def test_a_reclassified_title_whose_critique_was_not_rerun_is_refused(self):
        """Half a fold-in: facets from the corrected article beside a critique of the wrong one."""
        done = self.join([self.old, self.new], [self.old_d])
        self.assertEqual(done.returncode, 1)
        self.assertIn("read a different article", done.stderr)
        self.assertIn("movie:1", done.stderr)


class Withdrawn(unittest.TestCase):
    """A title a re-fetch left with no plot: its rows stop shipping, the title does not."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.c, self.d = os.path.join(self.dir, "c.jsonl"), os.path.join(self.dir, "d.jsonl")
        self.f, self.l = os.path.join(self.dir, "f.json"), os.path.join(self.dir, "l.json")
        self.out = os.path.join(self.dir, "corpus.jsonl")
        self.tombstones = os.path.join(self.dir, "withdrawn.jsonl")
        self.keys = os.path.join(self.dir, "A-to-plotless.txt")
        write(self.c, [combined(1, answers={"tax__survival": {"noul": 0.9}}), combined(2)])
        write(self.d, [{"mediaType": "movie", "tmdbId": 1, "answers": {"critique__craft": {"p": 0.7}}}])
        started(self.c, EARLY)
        started(self.d, EARLY)
        facts_file(self.f, ["movie:1", "movie:2"])
        labels_file(self.l, ["movie:1", "movie:2"])
        with open(self.keys, "w", encoding="utf-8") as fh:
            fh.write("movie:1\n")

    def withdraw(self, when="2026-09-22T20:00:00+00:00"):
        with contextlib.redirect_stdout(io.StringIO()):
            return cc.withdraw(["--keys", self.keys, "--reason", "#64: the sitelink is a redirect",
                                "--out", self.tombstones], now=datetime.fromisoformat(when))

    def join(self, *shards):
        return run(self.dir, [self.c, *shards], [self.d], self.f, self.l, self.out,
                   extra=["--withdrawn", self.tombstones, "--expect", "2"])

    def test_a_withdrawn_titles_rows_stop_shipping_and_the_title_stays(self):
        self.withdraw()
        code, err = self.join()
        self.assertEqual(code, 0, err)
        rows = {r["key"]: r for r in read(self.out)}
        self.assertEqual(sorted(rows), ["movie:1", "movie:2"], "withdrawn is not deleted")
        self.assertEqual(rows["movie:1"]["nouls"], {})
        self.assertEqual(rows["movie:1"]["critique"], {})
        self.assertEqual(rows["movie:1"]["facts"]["countries"], ["US"])
        self.assertEqual(rows["movie:1"]["labels"]["primaryGenre"], "Crime")
        self.assertEqual(rows["movie:2"]["facets"]["tone"]["choice"], "bleak", "only the listed title")

    def test_the_report_counts_the_withdrawals(self):
        self.withdraw()
        done = subprocess.run([sys.executable, SCRIPT, "--combined", self.c, "--delta", self.d,
                               "--facts", self.f, "--labels", self.l, "--out", self.out,
                               "--withdrawn", self.tombstones], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertEqual(report["tombstones"], 1)
        self.assertEqual((report["combined"]["withdrawn"], report["delta"]["withdrawn"]), (1, 1))
        self.assertEqual(report["combined"]["shards"][0]["withdrawn"], 1)
        self.assertEqual((report["withPass"], report["factsOnly"]), (1, 0))

    def test_a_run_that_started_after_the_withdrawal_stands(self):
        """The title regained a plot and was answered again: the tombstone does not have to be edited."""
        self.withdraw()
        again, again_d = os.path.join(self.dir, "c2.jsonl"), os.path.join(self.dir, "d2.jsonl")
        write(again, [combined(1, answers={"tone": {"choice": "hopeful"}})])
        write(again_d, [{"mediaType": "movie", "tmdbId": 1, "answers": {"critique__craft": {"p": 0.2}}}])
        started(again, LATE)
        started(again_d, LATE)
        done = subprocess.run([sys.executable, SCRIPT, "--combined", self.c, "--combined", again,
                               "--delta", self.d, "--delta", again_d, "--facts", self.f, "--labels", self.l,
                               "--out", self.out, "--withdrawn", self.tombstones],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        rows = {r["key"]: r for r in read(self.out)}
        self.assertEqual(rows["movie:1"]["facets"]["tone"]["choice"], "hopeful")
        self.assertEqual(json.loads(done.stdout)["combined"]["answeredAfterWithdrawal"], 1)

    def test_a_withdrawal_against_a_shard_with_no_recorded_start_is_refused(self):
        self.withdraw()
        os.remove(self.c + ".manifest.json")
        code, err = self.join()
        self.assertEqual(code, 1)
        self.assertIn("before or after the withdrawal", err)

    def test_the_tombstone_records_why_when_and_from_which_list(self):
        self.withdraw()
        with open(self.tombstones, encoding="utf-8") as fh:
            [row] = [json.loads(line) for line in fh]
        self.assertEqual((row["mediaType"], row["tmdbId"]), ("movie", 1))
        self.assertEqual(row["reason"], "#64: the sitelink is a redirect")
        self.assertEqual(row["withdrawnAt"], "2026-09-22T20:00:00+00:00")
        self.assertEqual(row["keysFile"], "A-to-plotless.txt")
        self.assertEqual(len(row["keysSha256"]), 64)

    def test_a_title_is_not_withdrawn_twice_at_the_same_instant(self):
        self.withdraw()
        with self.assertRaises(SystemExit) as refused:
            self.withdraw()
        self.assertIn("not withdrawn later", str(refused.exception))
        with open(self.tombstones, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 1, "a refusal appends nothing")

    def test_a_second_later_withdrawal_takes_rows_written_after_the_first(self):
        self.withdraw()
        again, again_d = os.path.join(self.dir, "c2.jsonl"), os.path.join(self.dir, "d2.jsonl")
        write(again, [combined(1, answers={"tone": {"choice": "hopeful"}})])
        write(again_d, [{"mediaType": "movie", "tmdbId": 1,
                         "answers": {"critique__craft": {"p": 0.2}}}])
        started(again, LATE)
        started(again_d, LATE)
        self.withdraw(when="2026-09-24T20:00:00+00:00")
        done = subprocess.run([sys.executable, SCRIPT, "--combined", self.c, "--combined", again,
                               "--delta", self.d, "--delta", again_d, "--facts", self.f,
                               "--labels", self.l, "--out", self.out, "--withdrawn", self.tombstones],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        rows = {r["key"]: r for r in read(self.out)}
        self.assertEqual(rows["movie:1"]["facets"], {})
        self.assertEqual(rows["movie:1"]["critique"], {})
        with open(self.tombstones, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 2, "both withdrawal events stay auditable")

    def test_a_tombstone_with_no_reason_is_refused(self):
        with open(self.tombstones, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"mediaType": "movie", "tmdbId": 1, "withdrawnAt": LATE}) + "\n")
        code, err = self.join()
        self.assertEqual(code, 1)
        self.assertIn("no reason", err)


class Prose(unittest.TestCase):
    def test_source_prose_is_refused_rather_than_published(self):
        """The repo's one hard rule: derived judgements are ours to publish, the prose they were read
        from is not (TMDb §1.C, and the Wikipedia text). There is deliberately no flag to override it."""
        with tempfile.TemporaryDirectory() as dir:
            c, d = os.path.join(dir, "c.jsonl"), os.path.join(dir, "d.jsonl")
            f, l = os.path.join(dir, "f.json"), os.path.join(dir, "l.json")
            out = os.path.join(dir, "corpus.jsonl")
            write(c, [combined(1, answers={"tone__evidence": {"text": "a paragraph of the article"}})])
            write(d, [])
            facts_file(f, ["movie:1"])
            labels_file(l, ["movie:1"])
            code, err = run(dir, [c], [d], f, l, out)
            self.assertEqual(code, 1)
            self.assertIn("refusing to write source prose", err)

    def test_every_prose_key_is_caught_by_substring_not_exact_name(self):
        """`tone__evidence` is not `evidence`; matching exactly would let every prefixed answer through."""
        for name in cc.PROSE:
            self.assertTrue(any(p in f"axis__{name}".lower() for p in cc.PROSE), name)


class Output(unittest.TestCase):
    def test_gzip_output_is_byte_identical_across_runs(self):
        """`mtime=0`, so republishing an unchanged corpus does not re-upload a 44 MB asset that only
        differs by its timestamp."""
        with tempfile.TemporaryDirectory() as dir:
            c, d = os.path.join(dir, "c.jsonl"), os.path.join(dir, "d.jsonl")
            f, l = os.path.join(dir, "f.json"), os.path.join(dir, "l.json")
            write(c, [combined(1)])
            write(d, [])
            facts_file(f, ["movie:1"])
            labels_file(l, ["movie:1"])
            first, second = os.path.join(dir, "a.jsonl.gz"), os.path.join(dir, "b.jsonl.gz")
            for out in (first, second):
                code, err = run(dir, [c], [d], f, l, out)
                self.assertEqual(code, 0, err)
            with open(first, "rb") as a, open(second, "rb") as b:
                self.assertEqual(a.read(), b.read())

    def test_the_entities_sidecar_is_written_beside_the_corpus(self):
        """The Q-ids in every row name entities that live here, once, rather than repeated 47,529 times."""
        with tempfile.TemporaryDirectory() as dir:
            c, d = os.path.join(dir, "c.jsonl"), os.path.join(dir, "d.jsonl")
            f, l = os.path.join(dir, "f.json"), os.path.join(dir, "l.json")
            out = os.path.join(dir, "corpus-abc.jsonl.gz")
            write(c, [combined(1)])
            write(d, [])
            facts_file(f, ["movie:1"], entities={"Q42": {"en": "Ada Director"}})
            labels_file(l, ["movie:1"])
            code, err = run(dir, [c], [d], f, l, out)
            self.assertEqual(code, 0, err)
            sidecar = os.path.join(dir, "corpus-abc-entities.json.gz")
            self.assertTrue(os.path.exists(sidecar), "the corpus is unreadable without its entity names")
            with gzip.open(sidecar, "rt", encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["Q42"]["en"], "Ada Director")

    def test_a_row_carries_the_facts_labels_and_model_answers_for_its_title(self):
        with tempfile.TemporaryDirectory() as dir:
            c, d = os.path.join(dir, "c.jsonl"), os.path.join(dir, "d.jsonl")
            f, l = os.path.join(dir, "f.json"), os.path.join(dir, "l.json")
            out = os.path.join(dir, "corpus.jsonl")
            write(c, [combined(1, answers={
                "tone": {"choice": "bleak", "confidence": 0.9},
                "score__intensity": {"value": 3},
                "tax__survival": {"p": 0.8},
            })])
            write(d, [{"mediaType": "movie", "tmdbId": 1,
                       "answers": {"critique__craft": {"p": 0.7},
                                   "made_for_children": {"choice": "no"}}}])
            facts_file(f, ["movie:1"])
            labels_file(l, ["movie:1"])
            code, err = run(dir, [c], [d], f, l, out)
            self.assertEqual(code, 0, err)
            row = read(out)[0]
            self.assertEqual(row["facets"]["tone"]["choice"], "bleak")
            self.assertEqual(row["scores"]["intensity"], {"value": 3})
            self.assertEqual(row["nouls"]["survival"], {"p": 0.8})
            self.assertEqual(row["critique"]["craft"], {"p": 0.7})
            self.assertEqual(row["audience"]["made_for_children"]["choice"], "no")
            self.assertEqual(row["labels"]["primaryGenre"], "Crime")
            # `mediaType`/`tmdbId` are the row's own keys, not repeated inside `facts`.
            self.assertNotIn("mediaType", row["facts"])


if __name__ == "__main__":
    unittest.main()
