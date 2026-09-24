#!/usr/bin/env python3
"""The bundle a publish puts beside the dataset, and the out-dir `seed` lays out from it (`pipeline/published.py`).

`den_daily_test.py` holds a seeded day to building what the out-dir that kept everything builds. These are the
rules around it, on a hand-made generation: what a bundle refuses to be made without, what a seed refuses to
start from, and the records it writes for the stages to resume from.
"""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pipeline import artifacts, enrich, published  # noqa: E402
from pipeline.contract import StageError  # noqa: E402
from store import vector_blob  # noqa: E402

VERSION = "0123456789ab"
SOURCE = {"hasWikiPlot": True, "plotArticle": "The Ledger", "plotLanguage": "en", "plotRevId": 7,
          "plotSha256": "ab" * 32, "wikidataItem": "Q1"}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def generation(out):
    """An out-dir as a run leaves it: two titles, one grounded and embedded, and a manifest."""
    rows = [{"key": "movie:1", "tmdbId": 1, "mediaType": "movie", "source": SOURCE},
            {"key": "tv:2", "tmdbId": 2, "mediaType": "tv"}]
    os.makedirs(out, exist_ok=True)
    with gzip.open(os.path.join(out, f"corpus-{VERSION}.jsonl.gz"), "wt", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    for path, _asset, required in published.BUNDLE:
        target = os.path.join(out, path.format(version=VERSION))
        if required and not os.path.exists(target):
            write(target, "{}")
    write(os.path.join(out, artifacts.VECTOR_LABELS.filename),
          json.dumps({"records": [{"tmdbId": 1, "mediaType": "movie", "genres": ["drama"]}]}))
    vector_blob.write(os.path.join(out, artifacts.VECTORS.filename), ["movie:1"], bytes([1, 255, 128]), 3)
    meta = {"datasetVersion": VERSION, "maxBatchId": 4}
    write(os.path.join(out, artifacts.MANIFEST.filename), json.dumps(meta))
    return meta


class Published(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="den-published-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "out")
        self.meta = generation(self.out)
        self.fresh = os.path.join(self.tmp, "fresh")
        self.record = published.bundle(self.out, os.path.join(self.fresh, "published"))
        shutil.copy(os.path.join(self.out, artifacts.MANIFEST.filename),
                    os.path.join(self.fresh, artifacts.PUBLISHED_META.filename))

    def read(self, *path):
        with open(os.path.join(self.fresh, *path), encoding="utf-8") as fh:
            return fh.read()

    def test_a_bundle_is_not_made_without_a_file_it_requires(self):
        os.unlink(os.path.join(self.out, "doc-facts.json"))
        with self.assertRaisesRegex(StageError, "doc-facts.json"):
            published.bundle(self.out, os.path.join(self.tmp, "again"))

    def test_the_bundle_records_its_generation_and_every_file(self):
        self.assertEqual(self.record["datasetVersion"], VERSION)
        self.assertNotIn("vectors-premise.bin", self.record["files"], "optional, and not there")
        for asset, digest in self.record["files"].items():
            self.assertEqual(published.sha256(os.path.join(self.fresh, "published", asset)), digest)

    def test_a_seed_lays_the_generation_out_under_the_names_the_stages_read(self):
        said = published.seed(self.fresh)
        self.assertEqual(said, {"datasetVersion": VERSION, "baseline": 1, "vectors": 1, "batch": 4})
        self.assertTrue(os.path.exists(os.path.join(self.fresh, f"facts-{VERSION}.json")))
        self.assertTrue(os.path.exists(os.path.join(self.fresh, artifacts.PUBLISHED_GENRES_MOODS.filename)))
        self.assertEqual(json.loads(self.read(artifacts.MANIFEST.filename)), self.meta)

    def test_the_seeded_batch_is_each_title_with_a_source_and_no_plot(self):
        published.seed(self.fresh)
        batch = json.loads(self.read(enrich.batch_path("", 4)))
        self.assertEqual(batch, [{"tmdbId": 1, "mediaType": "movie", **SOURCE}])
        state = json.loads(self.read(os.path.basename(enrich.checkpoint_path(self.fresh))))
        self.assertEqual((state["nextBatch"], state["processed"]), (5, [enrich.key("movie", 1)]))

    def test_the_embed_stores_are_the_blob_in_its_order(self):
        published.seed(self.fresh)
        labels = [json.loads(line) for line in self.read(artifacts.EMBED_LABELS.filename).splitlines()]
        vectors = [json.loads(line) for line in self.read(artifacts.EMBED_VECTORS.filename).splitlines()]
        self.assertEqual(labels, [{"tmdbId": 1, "mediaType": "movie", "genres": ["drama"]}])
        self.assertEqual(vectors, [{"tmdbId": 1, "v": [1, -1, -128]}])

    def test_a_seed_refuses_a_bundle_of_another_generation(self):
        write(os.path.join(self.fresh, artifacts.PUBLISHED_META.filename),
              json.dumps({**self.meta, "datasetVersion": "ba9876543210"}))
        with self.assertRaisesRegex(StageError, "download the corpus-ba9876543210 release"):
            published.seed(self.fresh)

    def test_a_seed_refuses_a_file_the_bundle_does_not_record(self):
        write(os.path.join(self.fresh, "published", "doc-facts.json"), '{"edited": true}')
        with self.assertRaisesRegex(StageError, "doc-facts.json is not the file the bundle records"):
            published.seed(self.fresh)

    def test_a_seed_refuses_an_out_dir_that_already_has_batches(self):
        write(enrich.batch_path(self.fresh, 1), "[]")
        with self.assertRaisesRegex(StageError, "already holds enriched batches"):
            published.seed(self.fresh)

    def test_a_seed_refuses_a_manifest_without_a_baseline(self):
        write(os.path.join(self.fresh, artifacts.PUBLISHED_META.filename),
              json.dumps({"datasetVersion": VERSION}))
        with self.assertRaisesRegex(StageError, "no maxBatchId"):
            published.seed(self.fresh)


if __name__ == "__main__":
    unittest.main()
