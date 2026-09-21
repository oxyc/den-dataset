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

Run: `python3 scripts/v2/test_consolidate_corpus.py`
"""
import gzip
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

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
        """Shards of one pass are disjoint by construction; an overlap means the same title was
        classified twice and one answer would win arbitrarily."""
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
