#!/usr/bin/env python3
"""The finalize stage — the bytes it ships and the refusals that stand between a torn store and a release.

The goldens below are not round trips through this module. They are what the Swift `taxonomy-backfill
finalize` wrote for the same fixture (merge base 4169e60), pasted in: the labels artifact is hashed into
`datasetVersion`, so any drift in its bytes is a new dataset version for no reason. The same comparison over
the real `out-repass` index stores — 47,539 titles — was byte-identical on every file, the manifest's two
clock fields and the gzip header's mtime aside (oxyc/den-dataset#27).
"""
import calendar
import hashlib
import json
import os
import tempfile
import time
import unittest

import pipeline

from . import artifacts, finalize
from .contract import Context, StageError, bind

#: The Swift run's clock, so the manifest golden is comparable byte for byte.
BUILT = calendar.timegm(time.strptime("2026-09-22T10:36:23Z", "%Y-%m-%dT%H:%M:%SZ"))

RECORDS = [
    {"tmdbId": 5, "mediaType": "movie", "primaryGenre": "Drama", "source": "llm", "animated": False,
     "subgenres": [{"label": "Legal/Courtroom Drama", "confidence": 1.0}, {"label": "Né", "confidence": 1}],
     "moods": [{"label": "Cozy — \U0001F600", "confidence": 1e-05}], "extra": "dropped"},
    {"tmdbId": 5, "mediaType": "tv", "primaryGenre": "Comedy", "source": "recipe", "animated": True,
     "subgenres": [], "moods": [{"label": "a\"b\\c\td", "confidence": 0.30000000000000004}]},
    {"tmdbId": 7, "mediaType": "movie", "primaryGenre": "Horror", "source": "wikidata", "animated": False,
     "subgenres": [{"label": "Slasher", "confidence": 0.95}], "moods": []},
    # Supersedes the first line: the newest record per key ships, with ITS vector.
    {"tmdbId": 5, "mediaType": "movie", "primaryGenre": "Thriller", "source": "cluster", "animated": False,
     "subgenres": [{"label": "Heist", "confidence": 0.123456789012345678},
                   {"label": "Legal/Courtroom Drama", "confidence": 1.0}, {"label": "Né", "confidence": 1}],
     "moods": [{"label": "Cozy — \U0001F600", "confidence": 1e-05}, {"label": "x", "confidence": 0}]},
]

#: A manifest some other writer left: unowned keys that must survive in ICU order with `%.17g` doubles,
#: and owned ones — the retired sidecar's — that must not.
PREVIOUS = {
    "a10": 1, "a9": 2, "B": 3, "a_b": 4, "a-b": 5, "Zed": {}, "empty": [], "nested": {"k": [1, 2.5, 0.7, True,
    None, "x/y", "é"], "K2": {"z": 1e-05, "Y": 12345678901234567890}},
    "metadataFile": "stale.json", "metadataSha256": "dead", "labelsGzFile": "old.gz", "float2": 2.0,
    "neg": -0.1, "big": 1.5e300, "ctl": "tab\there",
}

GOLDEN_LABELS = (
    '{"count":3,"records":[{"animated":true,"mediaType":"tv","moods":[{"confidence":0.30000000000000004,'
    '"label":"a\\"b\\\\c\\td"}],"primaryGenre":"Comedy","source":"recipe","subgenres":[],"tmdbId":5},'
    '{"animated":false,"mediaType":"movie","moods":[],"primaryGenre":"Horror","source":"wikidata",'
    '"subgenres":[{"confidence":0.95,"label":"Slasher"}],"tmdbId":7},{"animated":false,"mediaType":"movie",'
    '"moods":[{"confidence":1e-05,"label":"Cozy — \U0001F600"},{"confidence":0,"label":"x"}],'
    '"primaryGenre":"Thriller","source":"cluster","subgenres":[{"confidence":0.12345678901234568,'
    '"label":"Heist"},{"confidence":1,"label":"Legal\\/Courtroom Drama"},{"confidence":1,"label":"Né"}],'
    '"tmdbId":5}],"taxonomyVersion":"t02"}')

GOLDEN_MANIFEST = """{
  "a_b" : 4,
  "a-b" : 5,
  "a9" : 2,
  "a10" : 1,
  "B" : 3,
  "big" : 1.5000000000000001e+300,
  "builtAt" : "2026-09-22T10:36:23Z",
  "count" : 3,
  "ctl" : "tab\\there",
  "datasetVersion" : "5ea827864cd5",
  "dims" : 1024,
  "embedderMaxTokens" : 1024,
  "embedderRuntime" : "den-embed\\/5.1.2",
  "embeddingModel" : "bge-m3",
  "embeddingSpace" : "canary-v1:abc",
  "empty" : [

  ],
  "float2" : 2,
  "labelsBytes" : 698,
  "labelsFile" : "labels-t02.json",
  "labelsGzFile" : "labels-t02.json.gz",
  "labelsSha256" : "83b2c9d7346dd23d058c62a90ccfcacd06ab4ec99d93ce254959da90940f7ec0",
  "lastModifiedHttp" : "Tue, 22 Sep 2026 10:36:23 GMT",
  "neg" : -0.10000000000000001,
  "nested" : {
    "k" : [
      1,
      2.5,
      0.69999999999999996,
      true,
      null,
      "x\\/y",
      "é"
    ],
    "K2" : {
      "Y" : 12345678901234567890,
      "z" : 1.0000000000000001e-05
    }
  },
  "quantization" : "int8-symmetric-x127",
  "taxonomyVersion" : "t02",
  "vectorsBytes" : 3112,
  "vectorsFile" : "vectors-bge-m3.bin",
  "vectorsSha256" : "2d827280a25bd55a41322157dd6b48961f7da3844411cf92de3d4e37c3394a44",
  "Zed" : {

  }
}"""

#: The Swift's report for this fixture, less `noPrimary`: that came from `classify-checkpoint.json`, which
#: nothing has written since #48 deleted the vote-pass `assemble`.
GOLDEN_REPORT = """{
  "anime" : 2,
  "report" : {
    "byPrimaryGenre" : {
      "Comedy" : 1,
      "Horror" : 1,
      "Thriller" : 1
    },
    "confidenceHistogram" : {
      "0.0-0.1" : 2,
      "0.1-0.2" : 1,
      "0.3-0.4" : 1,
      "0.9-1.0" : 1,
      "1.0-1.1" : 2
    },
    "fetchFailures" : 1,
    "llmCalls" : 0,
    "processed" : 3,
    "skippedBelowVoteFloor" : 4
  }
}"""

#: `/usr/bin/gzip -k` over the golden labels, with the file's mtime set to this.
GZ_MTIME = 1790073383
GZ_SHA = "a02e3b4a98cb472a2d3f791671b0a315311ea23e535cecb8880480865c488e80"
VECTORS_SHA = "2d827280a25bd55a41322157dd6b48961f7da3844411cf92de3d4e37c3394a44"


def vector(i):
    """1024 values, some past int8 — the stage clamps, it never re-quantises."""
    return [((i * 37 + d * 11) % 300) - 150 for d in range(1024)]


def lay_down(out, records=RECORDS, vectors=None, previous=PREVIOUS, embedder=True, space=True):
    index = os.path.join(out, "index")
    os.makedirs(index, exist_ok=True)
    with open(os.path.join(index, "labels.jsonl"), "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    with open(os.path.join(index, "vectors.jsonl"), "w", encoding="utf-8") as fh:
        for i, rec in enumerate(records):
            v = vectors[i] if vectors else vector(i)
            fh.write(json.dumps({"tmdbId": rec["tmdbId"], "v": v}) + "\n")
    if embedder:
        with open(os.path.join(index, "embedder.json"), "w", encoding="utf-8") as fh:
            json.dump({"model": "bge-m3", "dims": 1024, "runtime": "den-embed/5.1.2", "maxTokens": 1024}, fh)
    if space:
        with open(os.path.join(index, "embedding-space.json"), "w", encoding="utf-8") as fh:
            json.dump({"spaceId": "canary-v1:abc", "canarySet": "canary-v1"}, fh)
    if previous is not None:
        with open(os.path.join(out, "dataset.meta.json"), "w", encoding="utf-8") as fh:
            json.dump(previous, fh)
    with open(os.path.join(out, "enrich-checkpoint.json"), "w", encoding="utf-8") as fh:
        json.dump({"processed": [], "nextBatch": 3, "totals": {"belowFloor": 4, "anime": 2, "failures": 1}},
                  fh)


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
        return fh.read()


class Staged(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.out = self.directory.name

    def tearDown(self):
        self.directory.cleanup()

    def run_stage(self):
        return finalize.run(Context(out_dir=self.out, dataset_version="test"), now=BUILT)

    def refused(self):
        with self.assertRaises(StageError) as refusal:
            self.run_stage()
        return str(refusal.exception)


class Bytes(Staged):
    def test_every_output_is_the_swift_binarys_bytes(self):
        lay_down(self.out)
        self.run_stage()
        self.assertEqual(read(os.path.join(self.out, "labels-t02.json")), GOLDEN_LABELS)
        self.assertEqual(hashlib.sha256(read(os.path.join(self.out, "vectors-bge-m3.bin"), "rb")).hexdigest(),
                         VECTORS_SHA)
        self.assertEqual(read(os.path.join(self.out, "dataset.meta.json")), GOLDEN_MANIFEST)
        self.assertEqual(read(os.path.join(self.out, "report.json")), GOLDEN_REPORT)

    def test_the_gzip_is_the_command_line_tools(self):
        """The header carries the input's name and mtime, which is the only thing two runs disagree on."""
        lay_down(self.out)
        self.run_stage()
        labels = os.path.join(self.out, "labels-t02.json")
        os.utime(labels, (GZ_MTIME, GZ_MTIME))
        finalize.gzip_like_the_cli(labels, labels + ".gz")
        self.assertEqual(hashlib.sha256(read(labels + ".gz", "rb")).hexdigest(), GZ_SHA)


class Report(Staged):
    def counters(self):
        self.run_stage()
        return json.loads(read(os.path.join(self.out, "report.json")))

    def write_checkpoint(self, body):
        with open(os.path.join(self.out, "enrich-checkpoint.json"), "w", encoding="utf-8") as fh:
            json.dump(body, fh)

    def test_counters_come_from_a_file_only_if_it_is_an_enrichment_checkpoint(self):
        """The Swift decoded the checkpoint before reading its totals, so `totals` in a file with no
        `processed` — or with a `processed` that is not a list of keys — are not the enrichment's."""
        lay_down(self.out)
        for body in ({"totals": {"belowFloor": 4, "anime": 2, "failures": 1}},
                     {"processed": "movie:1", "totals": {"anime": 2}},
                     {"processed": [], "totals": {"anime": "2"}}):
            with self.subTest(body=body):
                self.write_checkpoint(body)
                found = self.counters()
                self.assertEqual((found["anime"], found["report"]["skippedBelowVoteFloor"],
                                  found["report"]["fetchFailures"]), (0, 0, 0))

    def test_the_movie_pilots_bare_ids_are_still_a_checkpoint(self):
        lay_down(self.out)
        self.write_checkpoint({"processed": [1, 2], "totals": {"anime": 3}})
        self.assertEqual(self.counters()["anime"], 3)

    def test_no_checkpoint_is_zeros_not_a_refusal(self):
        lay_down(self.out)
        os.remove(os.path.join(self.out, "enrich-checkpoint.json"))
        self.assertEqual(self.counters()["report"]["fetchFailures"], 0)

    def test_the_counters_are_read_from_the_declared_checkpoint(self):
        """An operator pointing `--set enrich_checkpoint=` elsewhere gets that file's counters, not the
        out-dir's."""
        lay_down(self.out)
        elsewhere = os.path.join(self.out, "elsewhere.json")
        with open(elsewhere, "w", encoding="utf-8") as fh:
            json.dump({"processed": [], "totals": {"anime": 7}}, fh)
        finalize.run(Context(out_dir=self.out, dataset_version="test",
                             overrides={"enrich_checkpoint": elsewhere}), now=BUILT)
        self.assertEqual(json.loads(read(os.path.join(self.out, "report.json")))["anime"], 7)


class Store(Staged):
    def test_the_newest_record_per_title_ships_with_its_own_vector(self):
        lay_down(self.out)
        self.run_stage()
        shipped = json.loads(read(os.path.join(self.out, "labels-t02.json")))["records"]
        self.assertEqual([(r["mediaType"], r["tmdbId"], r["primaryGenre"]) for r in shipped],
                         [("tv", 5, "Comedy"), ("movie", 7, "Horror"), ("movie", 5, "Thriller")])
        blob = read(os.path.join(self.out, "vectors-bge-m3.bin"), "rb")
        last_row = blob[-1024:]
        self.assertEqual(list(last_row), [x & 0xFF for x in (max(-128, min(127, v)) for v in vector(3))])

    def test_a_store_torn_between_its_two_files_is_refused(self):
        lay_down(self.out)
        with open(os.path.join(self.out, "index", "vectors.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"tmdbId": 99, "v": vector(9)}) + "\n")
        self.assertIn("4 labels vs 5 vectors", self.refused())

    def test_equal_counts_with_a_shifted_body_is_refused(self):
        """Every title after the shift would ship its neighbour's vector, and the counts would match."""
        lay_down(self.out, vectors=None)
        path = os.path.join(self.out, "index", "vectors.jsonl")
        rows = read(path).splitlines()
        rows[1], rows[2] = rows[2], rows[1]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")
        self.assertIn("misaligned at line 2", self.refused())

    def test_a_record_missing_a_field_is_refused(self):
        broken = [dict(RECORDS[2])]
        del broken[0]["animated"]
        lay_down(self.out, records=broken)
        self.assertIn("not a labels-store record", self.refused())

    def test_vectors_of_two_lengths_are_refused(self):
        lay_down(self.out, records=RECORDS[:2], vectors=[vector(0), vector(1)[:384]])
        self.assertIn("non-uniform length [384, 1024]", self.refused())

    def test_vectors_of_the_wrong_length_for_the_model_are_refused(self):
        lay_down(self.out, records=RECORDS[:1], vectors=[vector(0)[:384]])
        self.assertIn("implies dim 1024", self.refused())


class Identity(Staged):
    def test_an_embedder_that_disagrees_with_the_vectors_is_refused(self):
        lay_down(self.out)
        with open(os.path.join(self.out, "index", "embedder.json"), "w", encoding="utf-8") as fh:
            json.dump({"model": "bge-m3", "dims": 384, "runtime": "x", "maxTokens": 512}, fh)
        self.assertIn("vectors are 1024-dim", self.refused())

    def test_an_unreadable_identity_is_refused_rather_than_skipped(self):
        """Skipped, it drops the identity from the manifest and looks like a store that never had one."""
        for name, body in (("embedder.json", "{not json"), ("embedding-space.json", "{not json"),
                           ("embedder.json", '{"model": "bge-m3", "dims": 1024}'),
                           ("embedding-space.json", '{"canarySet": "canary-v1"}')):
            with self.subTest(name=name, body=body):
                lay_down(self.out)
                with open(os.path.join(self.out, "index", name), "w", encoding="utf-8") as fh:
                    fh.write(body)
                self.assertIn("unreadable", self.refused())

    def test_an_absent_identity_is_an_absent_key_not_an_inherited_one(self):
        """The manifest OWNS these keys: a rewrite that has no space must not keep the last run's."""
        lay_down(self.out, embedder=False, space=False,
                 previous={"embeddingSpace": "canary-v1:stale", "embedderRuntime": "old", "embedderMaxTokens": 512,
                           "storeFile": "s"})
        self.run_stage()
        meta = json.loads(read(os.path.join(self.out, "dataset.meta.json")))
        self.assertNotIn("embeddingSpace", meta)
        self.assertNotIn("embedderRuntime", meta)
        self.assertNotIn("embedderMaxTokens", meta)
        self.assertEqual(meta["storeFile"], "s")


class ShipGuard(unittest.TestCase):
    def test_a_prose_key_is_found_at_any_depth_and_under_any_spelling(self):
        self.assertEqual(finalize.prohibited({"records": [{"tmdbId": 1, "Plot_Summary": "x"}], "overview": 1}),
                         ["Plot_Summary", "overview"])
        self.assertEqual(finalize.prohibited({"title": "Overview", "plots": 1}), [])

    def test_a_key_the_record_does_not_declare_never_reaches_the_artifact(self):
        line = json.dumps(dict(RECORDS[2], overview="TMDB's text"))
        self.assertNotIn("overview", finalize.record(line, "here"))


class Topology(unittest.TestCase):
    def test_it_runs_between_the_embedding_and_the_facts(self):
        self.assertEqual(pipeline.STAGES.index("finalize"), pipeline.STAGES.index("embed") + 1)
        self.assertEqual(pipeline.STAGES.index("facts"), pipeline.STAGES.index("finalize") + 1)

    def test_it_owns_what_it_writes(self):
        for artifact in (artifacts.VECTOR_LABELS, artifacts.VECTORS, artifacts.MANIFEST, artifacts.VECTOR_LABELS_GZ,
                         artifacts.FINALIZE_REPORT):
            self.assertEqual(artifact.producer, "")
            self.assertEqual(pipeline.producers()[artifact.name][0], finalize.PRODUCER)
        self.assertEqual([bind(e).name for e in finalize.OUTPUTS],
                         ["vector_labels", "vectors", "manifest", "vector_labels_gz", "finalize_report"])

    def test_every_file_it_touches_is_declared(self):
        """Run it over a fixture and compare what changed on disk with the declaration. An undeclared read
        or write is a file the registry cannot answer for."""
        with tempfile.TemporaryDirectory() as out:
            lay_down(out)
            before = {os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs}
            finalize.run(Context(out_dir=out, dataset_version="test"), now=BUILT)
            after = {os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs}
        declared_out = {bind(e).artifact.filename for e in finalize.OUTPUTS}
        declared_in = {bind(e).artifact.filename for e in finalize.INPUTS}
        self.assertEqual(after - before, declared_out - before)
        self.assertLessEqual(before - {"dataset.meta.json"}, declared_in)


if __name__ == "__main__":
    unittest.main()
