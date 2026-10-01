"""The incremental premise worklist spends only on newly admitted/regained titles."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "pipeline", "build_premise_worklist.py")


class IncrementalPremiseWorklistTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = self.temp.name
        self.articles = os.path.join(self.directory, "articles.jsonl")
        self.combined = os.path.join(self.directory, "combined.jsonl")
        self.plan = os.path.join(self.directory, "plan.json")

        rows = [
            ("tv", 990000004, "Regained premise"),
            ("movie", 990000003, "Merely reworded"),
            ("movie", 990000002, "Second new title"),
            ("tv", 990000005, "Only gained a plot"),
            ("movie", 990000001, "First new title"),
        ]
        with open(self.articles, "w", encoding="utf-8") as articles, \
                open(self.combined, "w", encoding="utf-8") as combined:
            for media, ident, text in rows:
                articles.write(json.dumps({"mediaType": media, "tmdbId": ident, "text": text}) + "\n")
                combined.write(json.dumps({
                    "mediaType": media, "tmdbId": ident, "title": str(ident), "year": 2026,
                    "language": "en", "article": str(ident),
                    "answers": {
                        "validity": {"choice": "correct-screen-work"},
                        "narrative_applicability": {"choice": "narrative"},
                    },
                    "sections": [{"heading": "Plot", "start": 0, "end": len(text),
                                  "role": {"value": "story-premise"}}],
                }) + "\n")
        with open(self.plan, "w", encoding="utf-8") as handle:
            json.dump({
                "baseline": {"datasetVersion": "old", "maxBatchId": 1},
                "added": ["movie:990000002", "movie:990000001"],
                "changed": {
                    "movie:990000003": ["plot"],
                    "tv:990000004": ["regained"],
                    "tv:990000005": ["gainedPlot"],
                },
            }, handle)

    def run_script(self, ceiling="10000", out="out", extra=()):
        return subprocess.run([
            sys.executable, SCRIPT, "--combined", self.combined, "--articles", self.articles,
            "--changes", self.plan, "--token-ceiling", ceiling,
            "--out-dir", os.path.join(self.directory, out),
            *extra,
        ], cwd=ROOT, text=True, capture_output=True)

    def test_only_added_and_regained_are_batched_with_audited_evidence(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = os.path.join(self.directory, "out")
        with open(os.path.join(out, "worklist.jsonl"), encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle]
        self.assertEqual([(row["mediaType"], row["tmdbId"]) for row in rows], [
            ("movie", 990000001), ("movie", 990000002), ("tv", 990000004),
        ])
        for row in rows:
            self.assertEqual(row["sourceDigestSha256"],
                             hashlib.sha256(row["evidence"].encode()).hexdigest())
        with open(os.path.join(out, "gen", "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["selection"], "added-or-regained-only")
        self.assertEqual(manifest["eligibleKeys"], 3)
        self.assertEqual(manifest["eligibleMissingFromCombined"], [])
        self.assertFalse(manifest["generationAuthorized"])
        self.assertEqual(manifest["tokenCeiling"], 10000)
        self.assertEqual(len(manifest["sourceInputs"]["combinedSha256"]), 1)

    def test_a_full_generation_cannot_masquerade_as_an_increment(self):
        with open(self.plan, "w", encoding="utf-8") as handle:
            json.dump({"baseline": None, "added": [], "changed": {}}, handle)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no published baseline", result.stderr)

    def test_declared_token_ceiling_is_a_gate(self):
        result = self.run_script(ceiling="1", out="too-large")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exceeds --token-ceiling 1", result.stderr)

    def test_the_live_bundles_tag_copy_prevents_a_second_purchase(self):
        tags = os.path.join(self.directory, "premise-tags-v2.json")
        with open(tags, "w", encoding="utf-8") as handle:
            json.dump({"tags": {"movie:990000001": ["already-tagged"]}}, handle)
        result = self.run_script(out="with-live-tags", extra=("--existing-tags", tags))
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.directory, "with-live-tags", "gen", "manifest.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["ids"], ["movie:990000002", "tv:990000004"])

    def test_a_title_classified_again_is_read_from_the_newer_run(self):
        """A kept shard from an earlier run beside today's, both holding a title whose article changed:
        the increment reads the run that started later, as the corpus join does, instead of refusing."""
        newer = os.path.join(self.directory, "combined-newer.jsonl")
        with open(self.combined, encoding="utf-8") as handle:
            row = json.loads(handle.readline())
        row["answers"]["validity"]["choice"] = "wrong-work"
        with open(newer, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        for path, started in ((self.combined, "2026-09-30T00:00:00+00:00"), (newer, "2026-10-01T00:00:00+00:00")):
            with open(path + ".manifest.json", "w", encoding="utf-8") as handle:
                json.dump({"runStartedAt": started}, handle)
        result = self.run_script(out="two-shards", extra=("--combined", newer))
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.directory, "two-shards", "gen", "manifest.json"), encoding="utf-8") as handle:
            self.assertNotIn("tv:990000004", json.load(handle)["ids"], "the newer row's validity counts")


if __name__ == "__main__":
    unittest.main()
