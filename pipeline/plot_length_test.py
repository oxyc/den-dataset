#!/usr/bin/env python3
"""The plot-length fit and the exact projection bytes consumers reproduce."""
import base64
import hashlib
import math
import json
import os
import tempfile
import unittest

from . import artifacts, finalize, plot_length
from .contract import Context, StageError


def rows(vectors):
    return {key: ({}, vector, None) for key, vector in vectors.items()}


class Fit(unittest.TestCase):
    def test_two_lengths_fit_the_axis_between_their_unit_vectors(self):
        keys, direction = plot_length.fit(
            rows({"movie:1": [127, 0, 0], "movie:2": [0, 127, 0]}),
            {"movie:1": 1, "movie:2": 4})
        self.assertEqual(keys, ["movie:1", "movie:2"])
        self.assertAlmostEqual(direction[0], -math.sqrt(0.5), places=14)
        self.assertAlmostEqual(direction[1], math.sqrt(0.5), places=14)
        self.assertEqual(direction[2], 0.0)

    def test_fit_is_key_order_deterministic(self):
        vectors = {"movie:3": [80, 20], "movie:1": [127, 0], "movie:2": [20, 80]}
        lengths = {"movie:2": 20, "movie:3": 80, "movie:1": 5}
        self.assertEqual(plot_length.fit(rows(vectors), lengths),
                         plot_length.fit(rows(dict(reversed(list(vectors.items())))),
                                         dict(reversed(list(lengths.items())))))


class Projection(unittest.TestCase):
    def test_projection_renormalisation_and_quantisation_match_atlas(self):
        direction = [-math.sqrt(0.5), math.sqrt(0.5), 0.0]
        # Removing the (-1,+1) axis leaves (+1,+1), whose int8 unit spelling is (90,90).
        self.assertEqual(finalize.project_row([127, 0, 0], direction), [90, 90, 0])

    def test_a_row_on_the_direction_becomes_zero(self):
        self.assertEqual(finalize.project_row([127, 0, 0], [1.0, 0.0, 0.0]), [0, 0, 0])

    def test_source_negative_128_is_read_as_int8_and_output_is_symmetric_x127(self):
        got = finalize.project_row([-128, 1, 2], [0.0, 0.0, 1.0])
        self.assertEqual(got[2], 0)
        self.assertTrue(all(-127 <= value <= 127 for value in got))

    def test_rounding_is_halves_away_from_zero_like_rust(self):
        self.assertEqual([finalize._round_away(x) for x in (-1.5, -0.5, 0.5, 1.5)], [-2, -1, 1, 2])


class Manifest(unittest.TestCase):
    def test_direction_and_signed_object_digests_cover_the_exact_bytes(self):
        raw = plot_length.direction_bytes([1.0, 0.0])
        artifact = {
            "schema": 1, "algorithm": plot_length.ALGORITHM, "dims": 2,
            "inputEmbeddingSpace": "space", "directionEncoding": plot_length.ENCODING,
            "directionBase64": base64.b64encode(raw).decode(),
            "directionSha256": hashlib.sha256(raw).hexdigest(), "fitMethod": plot_length.FIT_METHOD,
        }
        public = plot_length.public_record(artifact, "ab" * 32)
        digest = hashlib.sha256(plot_length.canonical(public)).hexdigest()
        self.assertEqual(len(plot_length.decode_direction(artifact)), 2)
        changed = dict(public, directionBase64=base64.b64encode(raw[:-1] + b"x").decode())
        self.assertNotEqual(digest, hashlib.sha256(plot_length.canonical(changed)).hexdigest())


class Stage(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name
        os.makedirs(os.path.join(self.out, "index"))
        os.makedirs(os.path.join(self.out, "enriched"))
        labels = []
        titles = {}
        batch = []
        for ident, overview in ((1, "One."), (2, "A substantially longer plot than one.")):
            record = {"tmdbId": ident, "mediaType": "movie", "primaryGenre": "Drama", "source": "recipe",
                      "animated": False, "subgenres": [], "moods": []}
            labels.append(record)
            titles[f"movie:{ident}"] = {key: record[key] for key in
                                         ("primaryGenre", "animated", "subgenres", "moods")}
            batch.append({"tmdbId": ident, "mediaType": "movie", "hasWikiPlot": True,
                          "plotLanguage": "en", "overview": overview})
        self.write("genres-moods.json", {"taxonomyVersion": "t02", "titles": titles})
        self.write("doc-facts.json", {})
        self.write("enriched/batch-1.json", batch)
        self.write("index/labels.jsonl", "".join(json.dumps(row) + "\n" for row in labels), raw=True)
        vectors = ({"tmdbId": 1, "v": [127, 0]}, {"tmdbId": 2, "v": [0, 127]})
        self.write("index/vectors.jsonl", "".join(json.dumps(row) + "\n" for row in vectors), raw=True)
        self.write("index/composition.json", {"docShape": "lean", "dropDirector": True, "plotCap": 3500})
        self.write("index/embedding-space.json", {"spaceId": "fixture-space"})

    def write(self, name, value, raw=False):
        path = os.path.join(self.out, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value if raw else json.dumps(value))

    def test_fit_artifact_keeps_observations_provenance_and_reuses_identically(self):
        path = plot_length.run(Context(out_dir=self.out))
        with open(path, "rb") as fh:
            before = fh.read()
        record = json.loads(before)
        self.assertEqual(record["fit"]["observations"], 2)
        self.assertEqual(record["observations"],
                          [{"key": "movie:1", "plotChars": 4},
                          {"key": "movie:2", "plotChars": 37}])
        self.assertEqual(record["inputEmbeddingSpace"], "fixture-space")
        self.assertIn("vectorsStoreSha256", record["inputs"])
        plot_length.run(Context(out_dir=self.out))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_a_recorded_document_mismatch_refuses_the_fit(self):
        path = os.path.join(self.out, artifacts.EMBED_VECTORS.filename)
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        rows[0]["docSha256"] = "0" * 64
        self.write(artifacts.EMBED_VECTORS.filename,
                   "".join(json.dumps(row) + "\n" for row in rows), raw=True)
        with self.assertRaisesRegex(StageError, "another document"):
            plot_length.run(Context(out_dir=self.out))

    def test_a_pre_transform_stateless_seed_refuses_at_the_named_migration_boundary(self):
        self.write("published/dataset.meta.json",
                   {"datasetVersion": "f0506bd528d0", "maxBatchId": 202})
        self.write("enriched/batch-202.json", [{"tmdbId": 1, "mediaType": "movie", "hasWikiPlot": True,
                                                 "plotSha256": "1" * 64}])
        with self.assertRaisesRegex(StageError, "pre-transform-baseline.*f0506bd528d0.*combined full rebuild"):
            plot_length.run(Context(out_dir=self.out))

    def test_an_old_manifest_does_not_block_a_full_rebuild_that_has_plot_prose(self):
        self.write("published/dataset.meta.json",
                   {"datasetVersion": "f0506bd528d0", "maxBatchId": 1})
        self.assertEqual(plot_length.run(Context(out_dir=self.out)),
                         os.path.join(self.out, artifacts.PLOT_LENGTH_TRANSFORM.filename))


if __name__ == "__main__":
    unittest.main()
