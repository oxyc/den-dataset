#!/usr/bin/env python3
"""`merge_premise_tags.py --into`: appending a later generation run to the committed v2 tags."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merge_premise_tags.py")
GOOD = ["heist-gone-wrong", "undercover-cop", "time-loop", "body-swap", "revenge-quest",
        "trapped-in-one-location", "wrongful-imprisonment", "enemies-to-lovers"]

V2 = {
    "schema": 1,
    "index": "premise-v2",
    "count": 2,
    "derivedFrom": "the v1 run; extended by the v2 run",
    "embeddedBy": "bge-m3 via den-embed",
    "supersedes": "premise-v1",
    "coverageFilled": ["movie:1"],
    "vectorsMissingFor": ["movie:1"],
    "tags": {"movie:1": GOOD, "tv:2": list(reversed(GOOD))},
}


def write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh)


class AppendInto(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name
        self.into = os.path.join(self.root, "premise-tags-v2.json")
        self.embed = os.path.join(self.root, "embed.json")
        write(self.into, V2)

    def tearDown(self):
        self.dir.cleanup()

    def phase(self, name, rows):
        path = os.path.join(self.root, name)
        write(os.path.join(path, "out", "batch-0000.json"), rows)
        return path

    def run_merge(self, *phases, note="extended by the test run", extra=()):
        argv = [sys.executable, SCRIPT, "--into", self.into, "--embed-list", self.embed]
        for p in phases:
            argv += ["--phase", p]
        if note is not None:
            argv += ["--note", note]
        return subprocess.run(argv + list(extra), capture_output=True, text=True)

    def load(self, path=None):
        return json.loads(self.text(path))

    def text(self, path=None):
        with open(path or self.into, encoding="utf-8") as fh:
            return fh.read()

    def test_appends_the_new_titles_and_keeps_the_metadata(self):
        done = self.run_merge(self.phase("a", [{"key": "tv:3", "tags": GOOD}]))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = self.load()
        for field in ("schema", "index", "embeddedBy", "supersedes", "coverageFilled", "vectorsMissingFor"):
            self.assertEqual(out[field], V2[field], field)
        self.assertEqual(out["derivedFrom"], V2["derivedFrom"] + "; extended by the test run",
                         "one clause appended, the v2 clause not repeated")
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["tags"]["movie:1"], V2["tags"]["movie:1"], "an existing record is untouched")
        self.assertEqual(out["tags"]["tv:3"], GOOD)
        self.assertEqual(self.load(self.embed), ["tv:3"], "only the new string is embedded")

    def test_the_repairs_apply_to_the_appended_titles(self):
        tags = GOOD[:6] + ["ménage-à-trois-tension", "romantic-comedy", "character-arc"]
        done = self.run_merge(self.phase("a", [{"key": "movie:4", "tags": tags}]))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.load()["tags"]["movie:4"], GOOD[:6] + ["menage-a-trois-tension"],
                         "accent repaired; genre-only and storytelling-property tags dropped")
        report = json.loads(done.stdout)
        self.assertEqual((report["tagsRepaired"], report["tagsDropped"]), (1, 2))
        self.assertEqual(report["belowFloor"], ["movie:4"], "7 tags left is reported, not padded")

    def test_refuses_a_key_the_file_already_holds(self):
        before = self.text()
        done = self.run_merge(self.phase("a", [{"key": "movie:1", "tags": list(reversed(GOOD))},
                                               {"key": "tv:3", "tags": GOOD}]))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("movie:1", done.stderr)
        self.assertEqual(self.text(), before, "nothing written on a refusal")

    def test_overwrite_replaces_and_embeds_only_a_changed_string(self):
        done = self.run_merge(self.phase("a", [{"key": "movie:1", "tags": list(reversed(GOOD))},
                                               {"key": "tv:2", "tags": list(reversed(GOOD))}]),
                              extra=["--overwrite"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.load()["tags"]["movie:1"], list(reversed(GOOD)))
        self.assertEqual(self.load(self.embed), ["movie:1"], "tv:2's string did not change")

    def test_several_phases_merge_and_a_key_in_two_of_them_is_refused(self):
        a = self.phase("a", [{"key": "tv:3", "tags": GOOD}])
        b = self.phase("b", [{"key": "tv:5", "tags": GOOD}])
        done = self.run_merge(a, b)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.load(self.embed), ["tv:3", "tv:5"])

        write(self.into, V2)
        clash = self.phase("c", [{"key": "tv:3", "tags": list(reversed(GOOD))}])
        done = self.run_merge(a, clash)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("tv:3", done.stderr)

    def test_a_note_is_required(self):
        done = self.run_merge(self.phase("a", [{"key": "tv:3", "tags": GOOD}]), note=None)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("--note", done.stderr)

    def test_the_file_keeps_its_formatting(self):
        """The committed file is `json.dump(indent=1, sort_keys=True)`; the append must write the same so
        the diff is the added keys."""
        with open(self.into, "w", encoding="utf-8") as fh:
            json.dump(V2, fh, indent=1, sort_keys=True)
        before = self.text().splitlines()
        self.assertEqual(self.run_merge(self.phase("a", [{"key": "tv:3", "tags": GOOD}])).returncode, 0)
        gone = set(before) - set(self.text().splitlines())
        self.assertLessEqual(gone, {' "count": 2,', ' "derivedFrom": "the v1 run; extended by the v2 run",'}, gone)


if __name__ == "__main__":
    unittest.main()
