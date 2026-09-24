#!/usr/bin/env python3
"""The daily job, twice, over the fixture corpus — offline, and through every publish gate.

`den_run_test.py` runs the pipeline once into an empty out-dir. The daily job (oxyc/den-dataset#27) is the
same pipeline run again over the out-dir the live dataset was built from, doing only what moved. This runs
it as the job does, on two days:

  * **day one** — `den run` into an empty out-dir, then `den stage publish --plan`: every gate the publisher
    runs, nothing signed or uploaded. The manifest it leaves is "published": copied to `published/`, where
    the next day's change set and gates read the live dataset from.
  * **day two** — one film's plot is rewritten upstream. `den run --refresh` re-reads it, the change set
    names it, and the stages after redo it and nothing else; the gates run again, now against the live
    manifest, so the record-count and store-identity guards compare two generations.
  * **day two, from the release** — the same day again, in an out-dir that holds only the bundle day one's
    publish put beside the dataset (`pipeline/published.py`): no plots, no shards, no answers. It has to
    build the same corpus and the same store, since that is how the job runs — it keeps nothing between runs.

Everything `DenRun` asserts about the out-dir holds after day two as well — the subclass inherits its
cases and runs them against the second day's state.

**The one gate the fixture cannot pass is the quality gate**, and the gate is not changed to let it. It
scores the labels against `data/eval/golden-large.json` and refuses unless 70% of those 2,568 real titles
are in the corpus (`pipeline/eval_taxonomy.py`, `MIN_COVERAGE`); an invented corpus has none of them. So
the check is required to get through every gate before it and to be refused by that one, for that reason.
Any other refusal fails this test.
"""
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

import den_run_test as fixture
import pipeline

from pipeline import artifacts, consolidate_corpus, published
from pipeline.enrich import batch_path as enrich_batch

EDITED = "movie:900001"
LEDGER = "The Lighthouse Ledger"
#: What the quality gate says about a corpus the golden set does not overlap, and what check mode says
#: once every gate has passed.
UNSCORABLE = "eval: the golden set and the labels do not overlap at all"
READY = "ready to publish"


class DenDaily(fixture.DenRun):
    """Two days of the job. `DenRun`'s cases run against the out-dir day two leaves."""

    @classmethod
    def drive(cls, den):
        common = ("--out-dir", cls.out, "--stamp-meta", cls.meta)
        cls.run_code, cls.run_said = cls.den(den, "run", *common, "--mode", "export")
        cls.began = re.findall(r"^==> (\w+)$", cls.run_said, re.M)
        cls.day_one = cls.snapshot()
        cls.check_one = cls.check()
        cls.go_live()
        cls.seeded = cls.seed_from_release()

        page = cls.upstreams.pages["en"][LEDGER]
        page["revid"] += 1
        page["wikitext"] = page["wikitext"].replace("rows to the mainland", "sails to the mainland")
        cls.day_two_code, cls.day_two_said = cls.den(den, "daily", "--out-dir", cls.out, "--mode", "export")
        cls.day_two = cls.snapshot()
        cls.check_two = cls.check()
        cls.seeded_code, cls.seeded_said = cls.den(den, "daily", "--out-dir", cls.seeded, "--mode", "export")
        cls.facts_refusal = cls.den(den, "stage", "facts", *common, "--dataset-version", fixture.GIVEN_VERSION)

    @classmethod
    def check(cls):
        """`den stage publish --plan`, from outside the process: the publisher is a subprocess whose output
        is the gates' verdicts, and nothing it does goes near the network."""
        done = subprocess.run([sys.executable, os.path.join(fixture.HERE, "den"), "stage", "publish",
                               "--out-dir", cls.out, "--plan"], capture_output=True, text=True, cwd=fixture.HERE)
        return done.returncode, done.stdout + done.stderr

    @classmethod
    def snapshot(cls):
        with open(cls.meta, encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(os.path.join(cls.out, artifacts.EMBED_LABELS.filename), encoding="utf-8") as fh:
            embedded = [json.loads(line) for line in fh if line.strip()]
        return {"version": meta["datasetVersion"], "storeSha256": meta.get("storeSha256"),
                "embedded": [f"{r['mediaType']}:{r['tmdbId']}" for r in embedded]}

    @classmethod
    def go_live(cls):
        """What a publish of day one leaves on `data-latest`: the manifest as the check left it, pruned and
        stamped with `maxBatchId`. Copied rather than uploaded, which is all the next day reads of it."""
        live = os.path.join(cls.out, artifacts.PUBLISHED_META.filename)
        os.makedirs(os.path.dirname(live), exist_ok=True)
        shutil.copy(cls.meta, live)

    @classmethod
    def seed_from_release(cls):
        """An empty out-dir holding only what a machine that keeps nothing starts from: the bundle day one's
        publish put on its `corpus-<ver>` release, the live manifest, and the day's TMDB export worklists."""
        seeded = os.path.join(cls.tmp, "seeded")
        published.bundle(cls.out, os.path.join(seeded, "published"))
        shutil.copy(cls.meta, os.path.join(seeded, artifacts.PUBLISHED_META.filename))
        for name in ("movie_ids.json", "tv_series_ids.json"):
            shutil.copy(os.path.join(cls.out, name), seeded)
        return seeded

    @classmethod
    def corpus_of(cls, out_dir):
        with open(os.path.join(out_dir, artifacts.MANIFEST.filename), encoding="utf-8") as fh:
            version = json.load(fh)["datasetVersion"]
        with gzip.open(os.path.join(out_dir, f"corpus-{version}.jsonl.gz"), "rt", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_the_manifest_describes_the_labels_and_the_vectors_it_ships_beside(self):
        """`den daily` ends with the check, which prunes the manifest as a publish does, so what `finalize`
        declared about the labels and the blob is gone from it. What a publish keeps still has to hold."""
        meta = fixture.read_json(self.meta)
        _count, dims, blob_keys, _blob, _base = fixture.vector_blob.read(self.path(artifacts.VECTORS))
        self.assertEqual(set(blob_keys), self.labels())
        self.assertEqual(meta["dims"], dims)
        self.assertEqual(meta["embeddingSpace"], self.canary["spaceId"])
        self.assertEqual(meta["embedderMaxTokens"], fixture.EmbedStandIn.HEALTH["max_tokens"])
        self.assertNotIn("labelsFile", meta, "pruned, as a publish prunes it")
        self.assertIsInstance(meta["maxBatchId"], int, "stamped, for the next day's change set")

    def gates(self, check):
        code, said = check
        self.assertIn("award merge gate: 7 merges applied, none stale", said)
        self.assertIn("plot-vector gate: all", said)
        self.assertIn("wikidata item gate: every contested title has its item chosen", said)
        self.assertIn("alias gate: the store applied", said)
        self.assertIn(UNSCORABLE, said, f"refused by a gate before the quality gate:\n{said[-3000:]}")
        self.assertEqual(code, 1)
        self.assertNotIn(READY, said)

    # ---- the gates -----------------------------------------------------------------------------------

    def test_day_one_passes_every_gate_the_fixture_can(self):
        self.gates(self.check_one)
        self.assertIn("left to the publish", self.check_one[1], "no live manifest on day one")

    def test_day_two_is_gated_against_the_live_dataset(self):
        self.gates(self.check_two)
        self.assertNotIn("left to the publish", self.check_two[1])

    # ---- what day two did ---------------------------------------------------------------------------

    def plan(self):
        with open(os.path.join(self.out, "changes", "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def report(self):
        with open(os.path.join(self.out, "daily-report.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_day_two_is_one_command_that_runs_every_stage_it_may(self):
        """`den daily`: every stage but the two that buy, the check at the end — refused, on the fixture, by
        the quality gate alone (`test_day_two_is_gated_against_the_live_dataset`)."""
        ran = self.report()["ran"]
        self.assertEqual(ran, [name for name in pipeline.STAGES if name not in ("classify", "critique", "publish")],
                         self.day_two_said[-3000:])
        self.assertEqual(self.day_two_code, 1)
        self.assertFalse(self.report()["ready"])
        self.assertIn("publish", self.report()["verdict"])

    def test_the_report_names_what_moved_and_what_was_skipped(self):
        report = self.report()
        self.assertEqual(report["baseline"]["datasetVersion"], self.day_one["version"])
        self.assertEqual((report["added"], report["changed"]), ([], {EDITED: ["plot"]}))
        self.assertEqual(report["datasetVersion"], self.day_two["version"])
        self.assertEqual([s["stage"] for s in report["skipped"]], ["classify", "critique", "genres_moods (ask)"])
        self.assertEqual(report["spend"], {"inputTokens": 0, "usd": 0.0})
        with open(os.path.join(self.out, "daily-report.md"), encoding="utf-8") as fh:
            summary = fh.read()
        self.assertIn(f"{EDITED} (plot)", summary)
        self.assertIn("Not ready", summary)

    def test_the_delta_ids_are_written_by_the_documented_rule(self):
        """The live facts' titles and the ids already listed, less what the new labels carry."""
        with open(os.path.join(self.out, artifacts.DELTA_IDS.filename), encoding="utf-8") as fh:
            ids = set(fh.read().split())
        self.assertFalse(ids & self.labels())
        self.assertIn("tv:900006", ids, "the premise-only title with no plot vector")

    def test_the_change_set_is_the_edit(self):
        plan = self.plan()
        self.assertEqual(plan["baseline"]["datasetVersion"], self.day_one["version"])
        self.assertEqual((plan["added"], plan["changed"], plan["withdrawn"]), ([], {EDITED: ["plot"]}, {}))

    def test_only_the_changed_title_is_embedded_again(self):
        again = self.day_two["embedded"][len(self.day_one["embedded"]):]
        self.assertEqual(self.day_two["embedded"][:len(self.day_one["embedded"])], self.day_one["embedded"])
        self.assertEqual(again, [EDITED])

    # ---- the same day, from the release ------------------------------------------------------------------

    def test_a_day_seeded_from_the_release_builds_what_the_kept_out_dir_builds(self):
        """The job keeps nothing between runs (#27): it starts each day from the bundle the last publish put
        beside the live dataset. That day has to be the day an out-dir that kept everything has — the same
        corpus, the same generation, the same store — without the classify and critique shards, the answers
        or a single plot."""
        self.assertIn("seeded", self.seeded_said, self.seeded_said[-3000:])
        self.assertIsNotNone(self.day_two["storeSha256"])
        self.assertEqual(self.corpus_of(self.seeded), self.corpus_of(self.out))
        with open(os.path.join(self.seeded, artifacts.MANIFEST.filename), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual((meta["datasetVersion"], meta.get("storeSha256")),
                         (self.day_two["version"], self.day_two["storeSha256"]))
        self.assertEqual(self.seeded_code, self.day_two_code)

    def test_the_seed_carries_no_plot(self):
        """The bundle is published beside the dataset, so what it lays out holds a plot's digest, never its
        text: the edited title's plot is in the seeded out-dir only because day two fetched it again."""
        with open(enrich_batch(self.seeded, self.day_one_batch()), encoding="utf-8") as fh:
            seeded = json.load(fh)
        self.assertTrue(seeded)
        allowed = {"tmdbId", "mediaType", "plotSha256", *consolidate_corpus.SOURCE}
        for record in seeded:
            self.assertLessEqual(set(record), allowed, record)

    def day_one_batch(self):
        with open(os.path.join(self.seeded, artifacts.PUBLISHED_META.filename), encoding="utf-8") as fh:
            return json.load(fh)["maxBatchId"]

    def test_a_new_generation_is_built(self):
        self.assertNotEqual(self.day_two["version"], self.day_one["version"])
        self.assertNotEqual(self.day_two["storeSha256"], self.day_one["storeSha256"])


if __name__ == "__main__":
    unittest.main()
