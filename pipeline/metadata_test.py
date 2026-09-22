#!/usr/bin/env python3
"""The poster sidecar — four failures that all shipped, and all of them silent.

Every one of these ends the same way: a file that still hashes correctly, so both consumers accept it and
never re-sync.

  * **a partial result written over the good one.** Each fetch was discarded on failure, so an expired key
    yielded an EMPTY sidecar with its sha stamped into the manifest;
  * **a probe that wrote.** `--limit` truncated the record list BEFORE coverage was computed, so coverage
    was always ~100% and the floor could never fire — and the N rows landed on the shipped 37.5k-row file;
  * **an unstable order.** The sha is folded into the app's syncKey, and `tmdbId` alone is not a total
    order: 940 ids in the corpus are both a movie and a series;
  * **the manifest rewritten through a closed model.** Twelve keys it does not declare went missing, and
    den-atlas lost premise search and facets with no error on either side.

Fetching is stubbed. Equivalence with the Swift `metadata` was measured separately
(oxyc/den-dataset#27) over 5,000 shipped titles replayed from `.cache/tmdb`: the two sidecars were
byte-identical at 629,629 bytes and both stamped the same sha into the manifest.
"""
import hashlib
import json
import os
import tempfile
import unittest

import pipeline

from . import artifacts, metadata
from .contract import Context, StageError, bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "testver"

#: A manifest shaped like the one `finalize` writes, plus a key no model in this repo declares.
META = {"datasetVersion": VERSION, "taxonomyVersion": "t02", "storeFile": f"den-{VERSION}.store",
        "facetsFile": "facets.bin", "premiseCount": 42}


class FakeTMDB:
    """Answers a poster row per title, or raises for the ones named as failures."""

    def __init__(self, failing=(), posterless=()):
        self.failing = set(failing)
        self.posterless = set(posterless)
        self.asked = []

    def poster_meta(self, media, tmdb_id):
        self.asked.append((media, tmdb_id))
        if (media, tmdb_id) in self.failing:
            raise ValueError("HTTP 401")
        row = {"tmdbId": tmdb_id, "mediaType": media, "title": f"Title {tmdb_id}", "year": 1999}
        if (media, tmdb_id) not in self.posterless:
            row["posterPath"] = f"/{tmdb_id}.jpg"
        return row


class Staged(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.out = self.directory.name
        self.meta_path = os.path.join(self.out, "dataset.meta.json")
        self.write_meta(META)

    def tearDown(self):
        self.directory.cleanup()

    def write_meta(self, meta):
        with open(self.meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)

    def labels(self, pairs):
        records = [{"tmdbId": tmdb_id, "mediaType": media, "primaryGenre": "Drama",
                    "subgenres": [], "moods": [], "source": "llm"} for media, tmdb_id in pairs]
        with open(os.path.join(self.out, "labels-t02.json"), "w", encoding="utf-8") as fh:
            json.dump({"taxonomyVersion": "t02", "count": len(records), "records": records}, fh)

    def context(self, **kwargs):
        return Context(out_dir=self.out, dataset_version=VERSION, **kwargs)

    def sidecar(self):
        with open(os.path.join(self.out, f"metadata-{VERSION}.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def manifest(self):
        with open(self.meta_path, encoding="utf-8") as fh:
            return json.load(fh)


class Coverage(Staged):
    def test_a_partial_result_is_refused_and_leaves_what_is_there(self):
        """An expired key or a rate-limit storm is TMDB failing, not titles without posters. Writing the
        thin file and stamping its sha re-synced every device onto it."""
        self.labels([("movie", n) for n in range(1, 11)])
        client = FakeTMDB(failing=[("movie", n) for n in range(1, 6)])
        with self.assertRaises(StageError) as refused:
            metadata.run(self.context(), client)
        self.assertIn("TMDB failing", str(refused.exception))
        self.assertFalse(os.path.exists(os.path.join(self.out, f"metadata-{VERSION}.json")))
        self.assertNotIn("metadataFile", self.manifest())

    def test_the_reason_each_miss_failed_is_written_down(self):
        """The floor asserting "TMDB is failing" without having looked at a single error is what made it
        undiagnosable."""
        self.labels([("movie", n) for n in range(1, 11)])
        with self.assertRaises(StageError):
            metadata.run(self.context(), FakeTMDB(failing=[("movie", n) for n in range(1, 6)]))
        with open(os.path.join(self.out, "enrich-log.txt"), encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("metadata-miss movie:1 (HTTP 401)", log)

    def test_a_handful_of_titles_without_a_poster_is_not_a_failure(self):
        """Real coverage is ~99%: a title without a poster still returns a row."""
        self.labels([("movie", n) for n in range(1, 101)])
        metadata.run(self.context(), FakeTMDB(failing=[("movie", 1)]))
        self.assertEqual(len(self.sidecar()), 99)

    def test_a_missing_poster_leaves_the_field_out_rather_than_writing_null(self):
        """How the 47,539 rows already on disk are shaped. A second spelling of "no poster" moves the sha
        of a file whose content did not change."""
        self.labels([("movie", 1)])
        metadata.run(self.context(), FakeTMDB(posterless=[("movie", 1)]))
        self.assertNotIn("posterPath", self.sidecar()[0])


class Order(Staged):
    def test_the_row_order_is_total(self):
        """`tmdbId` alone is not: 940 ids in the corpus are both a movie and a series, and the sha is
        folded into the app's syncKey — so a tie broken by whichever request finished first costs every
        device a ~4.6 MB re-download of a file that did not change."""
        pairs = [("tv", 95), ("movie", 12), ("movie", 95), ("tv", 12)]
        self.labels(pairs)
        metadata.run(self.context(), FakeTMDB())
        self.assertEqual([(row["tmdbId"], row["mediaType"]) for row in self.sidecar()],
                         [(12, "movie"), (12, "tv"), (95, "movie"), (95, "tv")])

    def test_two_runs_over_one_corpus_write_one_file(self):
        self.labels([("movie", n) for n in range(1, 40)])
        metadata.run(self.context(), FakeTMDB())
        with open(os.path.join(self.out, f"metadata-{VERSION}.json"), "rb") as fh:
            first = fh.read()
        metadata.run(self.context(), FakeTMDB())
        with open(os.path.join(self.out, f"metadata-{VERSION}.json"), "rb") as fh:
            self.assertEqual(fh.read(), first)
        self.assertEqual(self.manifest()["metadataSha256"], hashlib.sha256(first).hexdigest())


class Probe(Staged):
    def test_a_probe_writes_nothing_and_touches_no_manifest(self):
        """It used to truncate the records before coverage was computed, so the floor could never fire —
        and the N rows were written over the shipped 37.5k-row sidecar with their sha stamped in."""
        self.labels([("movie", n) for n in range(1, 101)])
        client = FakeTMDB()
        metadata.run(self.context(limit=5), client)
        self.assertEqual(len(client.asked), 5)
        self.assertFalse(os.path.exists(os.path.join(self.out, f"metadata-{VERSION}.json")))
        self.assertNotIn("metadataFile", self.manifest())

    def test_a_probe_still_answers_to_the_coverage_floor(self):
        """Which is the point of it: the cheap credential smoke-test has to be able to say the credential
        is wrong."""
        self.labels([("movie", n) for n in range(1, 101)])
        with self.assertRaises(StageError):
            metadata.run(self.context(limit=10), FakeTMDB(failing=[("movie", n) for n in range(1, 6)]))


class Manifest(Staged):
    def test_it_declares_the_sidecar_it_wrote(self):
        self.labels([("movie", 1)])
        metadata.run(self.context(), FakeTMDB())
        meta = self.manifest()
        path = os.path.join(self.out, f"metadata-{VERSION}.json")
        self.assertEqual(meta["metadataFile"], f"metadata-{VERSION}.json")
        self.assertEqual(meta["metadataBytes"], os.path.getsize(path))

    def test_a_key_no_model_here_declares_survives_the_patch(self):
        """The shipped manifest carries twelve of them — the facet blob's, the premise index's — written
        by a tool that is not in this repo. Rewriting it through a closed struct erased every one, the
        publisher still uploaded the blobs because they match its glob, and den-atlas lost premise search
        and facets with no error on either side."""
        self.labels([("movie", 1)])
        metadata.run(self.context(), FakeTMDB())
        meta = self.manifest()
        self.assertEqual(meta["facetsFile"], "facets.bin")
        self.assertEqual(meta["premiseCount"], 42)
        self.assertEqual(meta["storeFile"], f"den-{VERSION}.store")

    def test_a_run_building_a_different_version_than_the_manifest_names_is_refused(self):
        """The sidecar's filename carries the version. Written under the other name, the manifest goes on
        pointing at the PREVIOUS sidecar — which still hashes correctly, so nothing re-syncs and every
        title this run added renders with no poster."""
        self.labels([("movie", 1)])
        self.write_meta(dict(META, datasetVersion="other"))
        with self.assertRaises(StageError) as refused:
            metadata.run(self.context(), FakeTMDB())
        self.assertIn("finalize", str(refused.exception))

    def test_a_missing_manifest_is_refused_with_what_writes_it(self):
        self.labels([("movie", 1)])
        os.remove(self.meta_path)
        with self.assertRaises(StageError) as refused:
            metadata.run(self.context(), FakeTMDB())
        self.assertIn("taxonomy-backfill finalize", str(refused.exception))

    def test_a_labels_file_with_no_records_is_refused(self):
        self.labels([])
        with self.assertRaises(StageError) as refused:
            metadata.run(self.context(), FakeTMDB())
        self.assertIn("no records", str(refused.exception))


class Topology(unittest.TestCase):
    def test_the_sidecar_is_owned_by_the_stage_that_writes_it(self):
        self.assertEqual(artifacts.METADATA.producer, "")
        self.assertEqual(pipeline.producers()["metadata"],
                         (metadata.PRODUCER, metadata.HOW, True))
        self.assertTrue(os.path.isfile(os.path.join(REPO, metadata.PRODUCER)))

    def test_the_producer_guard_reads_it_off_the_stage_rather_than_a_hand_written_entry(self):
        """It was spelled out in `check-producers.py` while the Swift owned it. A registry nothing
        executes is a copy, and that dict's copies have drifted twice in one day."""
        with open(os.path.join(REPO, "scripts", "check-producers.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn('"metadataFile": (', source)
        self.assertEqual(artifacts.METADATA.manifest_key, "metadataFile")

    def test_it_runs_after_the_version_it_names_exists_and_before_the_publish(self):
        order = list(pipeline.STAGES)
        self.assertLess(order.index("store"), order.index("metadata"))
        self.assertLess(order.index("metadata"), order.index("publish"))

    def test_the_labels_keep_one_name_and_are_read_under_their_own(self):
        bound = {bind(e).name: bind(e) for e in metadata.INPUTS}["vector_labels"]
        self.assertEqual(bound.flag(), "--labels")
        self.assertEqual(bound.artifact, artifacts.VECTOR_LABELS)


if __name__ == "__main__":
    unittest.main()
