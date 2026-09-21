"""`manifest-counts.py` — the record-count stamp and the shrink guard.

The guard's whole job is to notice a blob that stays declared and loses rows. It had no test, and it also
could not see the one artifact that now carries every title: `count()` returned None for anything not
ending `.json`, so a store with every row missing would have stamped nothing, compared nothing, and
published.
"""
import importlib.util
import json
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    """The script, whose filename is not an importable module name."""
    spec = importlib.util.spec_from_file_location("manifest_counts", os.path.join(HERE, "manifest-counts.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mc = load()


def write_store(path, rows, magic=b"DENSTOR1"):
    """A store-v1 header and nothing else — `count` reads 32 bytes and never the sections."""
    head = bytearray(64)
    head[0:8] = magic
    struct.pack_into("<I", head, 8, 1)  # format_version
    struct.pack_into("<I", head, 12, 0x01020304)  # endianness
    struct.pack_into("<I", head, 24, 0)  # section_count
    struct.pack_into("<I", head, 28, rows)
    with open(path, "wb") as f:
        f.write(head)


class StoreCounting(unittest.TestCase):
    def test_counts_a_store_from_its_header(self):
        with tempfile.TemporaryDirectory() as dir:
            write_store(os.path.join(dir, "den-abc.store"), 47618)
            self.assertEqual(mc.count({"storeFile": "den-abc.store"}, "storeFile", dir), 47618)

    def test_counts_the_den_spec_fixture(self):
        """The committed fixture, so this agrees with the Rust readers rather than with itself."""
        spec = os.environ.get("DEN_SPEC_DIR", os.path.join(HERE, "..", "..", "den-spec"))
        fixture = os.path.join(spec, "vectors", "store-v1.store")
        if not os.path.isfile(fixture):
            if os.environ.get("DEN_SPEC_OPTIONAL") == "1":
                self.skipTest("den-spec absent and DEN_SPEC_OPTIONAL=1")
            self.fail(f"{fixture} not found — set DEN_SPEC_DIR, or DEN_SPEC_OPTIONAL=1 to skip on purpose")
        self.assertEqual(mc.store_rows(fixture), 3)

    def test_refuses_a_file_that_is_not_a_store(self):
        """A wrong magic reads as uncountable, not as zero rows — zero would trip the shrink guard and
        blame the publish for a file this script simply cannot read."""
        with tempfile.TemporaryDirectory() as dir:
            path = os.path.join(dir, "den-abc.store")
            write_store(path, 47618, magic=b"NOTASTOR")
            self.assertIsNone(mc.store_rows(path))
            with open(os.path.join(dir, "short.store"), "wb") as f:
                f.write(b"DENSTOR1")
            self.assertIsNone(mc.store_rows(os.path.join(dir, "short.store")))

    def test_store_is_counted(self):
        self.assertIn("storeFile", mc.COUNTED)


def write_json(path, doc):
    with open(path, "w") as f:
        json.dump(doc, f)


def write_facets(path, n):
    """A DFI2 facets blob header — magic plus a u32 count, as `build-facets-bin.py` writes it."""
    with open(path, "wb") as f:
        f.write(b"DFI2" + struct.pack("<I", n))


def write_records(path, n):
    """A blob in the `{"records": [...]}` shape `count()` reads, with `n` of them."""
    write_json(path, {"records": [{"tmdbId": i} for i in range(n)]})


def run(argv):
    """`main()` with these arguments, returning what it printed."""
    import contextlib
    import io

    out = io.StringIO()
    old = sys.argv
    sys.argv = ["manifest-counts.py", *argv]
    try:
        with contextlib.redirect_stdout(out):
            mc.main()
    finally:
        sys.argv = old
    return out.getvalue()


class StampAndCompare(unittest.TestCase):
    """`main()` itself, through both modes — the guard as `publish-dataset.sh` invokes it."""

    def test_stamp_writes_store_records_and_compare_reports_a_shrink(self):
        with tempfile.TemporaryDirectory() as dir:
            write_store(os.path.join(dir, "den-abc.store"), 47618)
            meta = os.path.join(dir, "meta.json")
            write_json(meta, {"storeFile": "den-abc.store"})

            run(["--stamp", meta, dir])
            with open(meta) as fh:
                published = json.load(fh)
            self.assertEqual(published["storeRecords"], 47618, "the stamp comes from the header, not the meta")

            # The next generation, 7,618 titles short — a join that missed, which is the failure this
            # whole script exists for and the one it could not see for a binary blob.
            write_store(os.path.join(dir, "den-def.store"), 40000)
            next_meta = os.path.join(dir, "next.json")
            write_json(next_meta, {"storeFile": "den-def.store"})
            report = run(["--compare", meta, next_meta, dir])
            self.assertIn("storeFile", report)
            self.assertIn("47618 -> 40000", report)

    def test_compare_is_silent_when_the_store_grows(self):
        with tempfile.TemporaryDirectory() as dir:
            write_store(os.path.join(dir, "den-abc.store"), 47618)
            meta = os.path.join(dir, "meta.json")
            write_json(meta, {"storeFile": "den-abc.store"})
            run(["--stamp", meta, dir])

            write_store(os.path.join(dir, "den-def.store"), 47700)
            next_meta = os.path.join(dir, "next.json")
            write_json(next_meta, {"storeFile": "den-def.store"})
            self.assertEqual(run(["--compare", meta, next_meta, dir]), "")


class AdvertisedCounts(unittest.TestCase):
    """`premiseCount` is what the served descriptor tells the app. Nothing in the pipeline wrote it —
    `DatasetMeta` does not model it, so it was merged forward from whenever the premise index was first
    published. Found live at 38,532 while the labels file and the vector blob both held 44,531."""

    def test_premise_count_is_derived_from_the_labels_file(self):
        with tempfile.TemporaryDirectory() as dir:
            write_records(os.path.join(dir, "labels-premise.json"), 44531)
            meta = os.path.join(dir, "meta.json")
            # The stale value, exactly as it was live.
            write_json(meta, {"premiseLabelsFile": "labels-premise.json", "premiseCount": 38532})
            run(["--stamp", meta, dir])
            with open(meta) as fh:
                stamped = json.load(fh)
            self.assertEqual(stamped["premiseCount"], 44531, "the advertised count must follow the file")
            self.assertEqual(stamped["premiseLabelsRecords"], 44531)

    def test_no_premise_labels_leaves_the_key_alone(self):
        """A dataset shipping no premise index must not have the key invented or zeroed for it."""
        with tempfile.TemporaryDirectory() as dir:
            meta = os.path.join(dir, "meta.json")
            write_json(meta, {"labelsFile": "absent.json"})
            run(["--stamp", meta, dir])
            with open(meta) as fh:
                self.assertNotIn("premiseCount", json.load(fh))


class Consistency(unittest.TestCase):
    """Is the manifest internally TRUE right now — a different question from whether a blob moved."""

    LIVE = {  # the manifest exactly as it was published, before the fix
        "labelsFile": "labels-t02.json", "count": 47539, "labelsRecords": 47539,
        "vectorsFile": "vectors-bge-m3.bin", "dims": 1024, "vectorsBytes": 48679944,
        "premiseLabelsFile": "labels-premise.json",
        "premiseCount": 38532, "premiseLabelsRecords": 44531,
        "premiseVectorsFile": "vectors-premise.bin",
        "premiseDims": 1024, "premiseVectorsBytes": 45599752,
    }
    COUNTS = {"labelsFile": 47539, "premiseLabelsFile": 44531}

    def test_it_catches_the_real_one(self):
        found = mc.inconsistencies(self.LIVE, self.COUNTS)
        self.assertEqual(len(found), 2, f"both directions must fire: {found}")
        self.assertTrue(any("premiseCount" in f and "38532" in f for f in found))
        self.assertTrue(any("premise vectors" in f for f in found))

    def test_a_consistent_manifest_is_silent(self):
        fixed = dict(self.LIVE, premiseCount=44531)
        self.assertEqual(mc.inconsistencies(fixed, self.COUNTS), [])

    def test_the_vector_check_uses_the_real_key_names(self):
        """`f"{prefix}dims"` gives `premisedims`, which no manifest has — so the premise half of this
        check silently did nothing when it was first written. Drop premiseCount's own agreement and the
        vector arithmetic must still catch it on its own."""
        only_vectors = {k: v for k, v in self.LIVE.items() if k != "premiseLabelsRecords"}
        found = mc.inconsistencies(only_vectors, {"labelsFile": 47539})
        self.assertTrue(any("premise vectors" in f for f in found), found)

    def test_the_plot_index_is_consistent_in_the_real_manifest(self):
        """The control. 48,679,944 = 8 + 47,539 x 1024, so the plot half must NOT fire — a check that
        flags a healthy artifact gets switched off."""
        self.assertFalse(any("plot vectors" in f for f in mc.inconsistencies(self.LIVE, self.COUNTS)))

    def test_metadata_left_behind_by_a_dropped_key_is_caught(self):
        """Dropping a key leaves its metadata. The Step-3 publish removed `railFacetsFile` and its gz
        twin and left `railFacetsSha256`/`railFacetsBytes` describing a file no longer named or shipped —
        nothing reads them and nothing cleans them, so they would be merged forward forever."""
        found = mc.inconsistencies(
            {"labelsFile": "l.json", "labelsBytes": 10,
             "railFacetsSha256": "abc", "railFacetsBytes": 49897524}, {})
        self.assertEqual(len(found), 2, found)
        self.assertTrue(all("railFacets" in f for f in found))

    def test_a_complete_key_set_is_not_flagged(self):
        """The control: every `*Sha256`/`*Bytes`/`*Records` whose `*File` is present must pass, or this
        fires on every healthy manifest and gets switched off."""
        self.assertEqual(mc.inconsistencies(
            {"labelsFile": "l.json", "labelsSha256": "a", "labelsBytes": 1, "labelsRecords": 1,
             "storeFile": "s.store", "storeRecords": 2, "maxBatchId": 173}, {}), [])

    def test_a_missing_number_is_not_a_mismatch(self):
        self.assertEqual(mc.inconsistencies({"count": 10}, {}), [])


class Coverage(unittest.TestCase):
    """A blob's records as a share of the labels — the question an absolute count cannot answer."""

    def test_a_blob_nobody_rebuilt_is_caught_although_it_lost_nothing(self):
        """The failure that has actually happened twice here. The corpus grows; one blob is carried
        forward untouched. Its own count never drops, so `now < stored` is false for it — and it covers
        less of the dataset with every publish."""
        with tempfile.TemporaryDirectory() as dir:
            write_records(os.path.join(dir, "labels-a.json"), 40000)
            write_records(os.path.join(dir, "facets-a.json"), 40000)
            meta = os.path.join(dir, "meta.json")
            write_json(meta, {"labelsFile": "labels-a.json", "plotFacetsFile": "facets-a.json"})
            run(["--stamp", meta, dir])

            # Labels grew to 44,000; the facets blob is the SAME FILE, never rebuilt.
            write_records(os.path.join(dir, "labels-b.json"), 44000)
            next_meta = os.path.join(dir, "next.json")
            write_json(next_meta, {"labelsFile": "labels-b.json", "plotFacetsFile": "facets-a.json"})
            report = run(["--compare", meta, next_meta, dir])

            self.assertNotIn("records lost", report, "nothing shrank — that is the whole point")
            self.assertIn("plotFacetsFile", report)
            self.assertIn("100.0% -> 90.9%", report)

    def test_coverage_tolerates_ordinary_churn(self):
        """A guard that fires on normal movement gets switched off. Both grow, the blob a shade
        slower: 100% -> 99.3% coverage, which keeps 99.3% of its share and must pass."""
        with tempfile.TemporaryDirectory() as dir:
            write_records(os.path.join(dir, "labels-a.json"), 40000)
            write_records(os.path.join(dir, "facets-a.json"), 40000)
            meta = os.path.join(dir, "meta.json")
            write_json(meta, {"labelsFile": "labels-a.json", "plotFacetsFile": "facets-a.json"})
            run(["--stamp", meta, dir])

            write_records(os.path.join(dir, "labels-b.json"), 40400)
            write_records(os.path.join(dir, "facets-b.json"), 40100)  # grew, but a shade slower
            next_meta = os.path.join(dir, "next.json")
            write_json(next_meta, {"labelsFile": "labels-b.json", "plotFacetsFile": "facets-b.json"})
            self.assertEqual(run(["--compare", meta, next_meta, dir]), "")

    def test_the_threshold_is_2_041_percent_growth(self):
        """Pin the number the docstring quotes, because it is what an operator plans a publish around and
        it is derived (`1/(1+g) < 0.98`), not chosen. Just under must pass; just over must fire."""
        for growth, expect_fire in ((0.0200, False), (0.0210, True)):
            with tempfile.TemporaryDirectory() as dir:
                base = 47539
                write_records(os.path.join(dir, "labels-a.json"), base)
                write_records(os.path.join(dir, "facets-a.json"), base)
                meta = os.path.join(dir, "meta.json")
                write_json(meta, {"labelsFile": "labels-a.json", "plotFacetsFile": "facets-a.json"})
                run(["--stamp", meta, dir])

                grown = int(base * (1 + growth))
                write_records(os.path.join(dir, "labels-b.json"), grown)
                next_meta = os.path.join(dir, "next.json")
                write_json(next_meta, {"labelsFile": "labels-b.json", "plotFacetsFile": "facets-a.json"})
                report = run(["--compare", meta, next_meta, dir])
                fired = "plotFacetsFile" in report
                self.assertEqual(fired, expect_fire, f"growth {growth:.2%} -> {report!r}")

    def test_a_key_with_no_published_baseline_is_not_scored(self):
        """Coverage cannot be satisfied by renaming: a key the published manifest never stamped is
        skipped entirely rather than compared against a number it did not earn."""
        with tempfile.TemporaryDirectory() as dir:
            write_records(os.path.join(dir, "labels-a.json"), 40000)
            write_records(os.path.join(dir, "facets-a.json"), 40000)
            meta = os.path.join(dir, "meta.json")
            write_json(meta, {"labelsFile": "labels-a.json", "plotFacetsFile": "facets-a.json"})
            run(["--stamp", meta, dir])
            with open(meta) as fh:
                self.assertIn("plotFacetsRecords", json.load(fh))

            # The same data moved under a different key: no baseline, so no comparison at all.
            write_records(os.path.join(dir, "facets-b.json"), 1000)
            next_meta = os.path.join(dir, "next.json")
            write_json(next_meta, {"labelsFile": "labels-a.json", "metadataFile": "facets-b.json"})
            report = run(["--compare", meta, next_meta, dir])
            self.assertNotIn("metadataFile", report, "a new key has nothing to be compared against")

    def test_a_low_coverage_blob_is_held_to_the_same_standard(self):
        """Why the threshold is a RATIO and not points of the whole corpus.

        A blob that is never rebuilt loses `c*g/(1+g)` POINTS when the corpus grows by `g`, so the points
        it loses scale with its own coverage. plot-facets really sits at 13.85%: across the real
        38,532 -> 44,531 repass an untouched blob drops 1.87 points, which a 2-point rule waves through
        while the blob covers a seventh of what it did. As a fraction of its own share it is the same
        86.5% whatever `c` is."""
        with tempfile.TemporaryDirectory() as dir:
            write_records(os.path.join(dir, "labels-a.json"), 38532)
            write_records(os.path.join(dir, "facets-a.json"), 5336)  # 13.85%
            meta = os.path.join(dir, "meta.json")
            write_json(meta, {"labelsFile": "labels-a.json", "plotFacetsFile": "facets-a.json"})
            run(["--stamp", meta, dir])

            write_records(os.path.join(dir, "labels-b.json"), 44531)
            next_meta = os.path.join(dir, "next.json")
            write_json(next_meta, {"labelsFile": "labels-b.json", "plotFacetsFile": "facets-a.json"})
            report = run(["--compare", meta, next_meta, dir])
            self.assertIn("plotFacetsFile", report, "1.87 points, but it kept only 86.5% of its share")
            self.assertIn("kept 86.5%", report)

    def test_the_store_may_cover_more_than_the_labels(self):
        """The store is the union of facts and the pass, so it holds MORE rows than the labels and reads
        above 100%. The guard watches for a fall, not for a ceiling."""
        with tempfile.TemporaryDirectory() as dir:
            write_records(os.path.join(dir, "labels-a.json"), 47539)
            write_store(os.path.join(dir, "den-abc.store"), 47618)
            meta = os.path.join(dir, "meta.json")
            write_json(meta, {"labelsFile": "labels-a.json", "storeFile": "den-abc.store"})
            run(["--stamp", meta, dir])

            write_records(os.path.join(dir, "labels-b.json"), 47600)
            write_store(os.path.join(dir, "den-def.store"), 47700)
            next_meta = os.path.join(dir, "next.json")
            write_json(next_meta, {"labelsFile": "labels-b.json", "storeFile": "den-def.store"})
            self.assertEqual(run(["--compare", meta, next_meta, dir]), "")

    def test_facets_bin_is_counted_from_its_own_header(self):
        """The blob the guard was NAMED for. It is binary, so a json-only reader skipped it entirely and
        could never have produced a line about the 999 titles it fell behind."""
        with tempfile.TemporaryDirectory() as dir:
            write_facets(os.path.join(dir, "facets.bin"), 38532)
            self.assertEqual(mc.count({"facetsFile": "facets.bin"}, "facetsFile", dir), 38532)
            self.assertIn("facetsFile", mc.COUNTED)

    def test_the_real_999_title_case_is_caught(self):
        """facets.bin at parity, then the corpus gains 999 titles and it is carried forward untouched."""
        with tempfile.TemporaryDirectory() as dir:
            write_records(os.path.join(dir, "labels-a.json"), 37533)
            write_facets(os.path.join(dir, "facets.bin"), 37533)
            meta = os.path.join(dir, "meta.json")
            write_json(meta, {"labelsFile": "labels-a.json", "facetsFile": "facets.bin"})
            run(["--stamp", meta, dir])

            write_records(os.path.join(dir, "labels-b.json"), 38532)
            next_meta = os.path.join(dir, "next.json")
            write_json(next_meta, {"labelsFile": "labels-b.json", "facetsFile": "facets.bin"})
            report = run(["--compare", meta, next_meta, dir])
            self.assertNotIn("records lost", report, "it lost nothing — that is why counts missed it")
            self.assertIn("facetsFile", report)

    def test_an_unreadable_store_is_reported_not_skipped(self):
        """Uncountable used to mean unguarded. A truncated store published with no record check at all,
        and `storeSha256` cannot help: --stamp-meta computes it from the bytes just written, so a
        wrongly-written store is self-consistently wrong."""
        with tempfile.TemporaryDirectory() as dir:
            write_records(os.path.join(dir, "labels-a.json"), 40000)
            write_store(os.path.join(dir, "den-abc.store"), 47618)
            meta = os.path.join(dir, "meta.json")
            write_json(meta, {"labelsFile": "labels-a.json", "storeFile": "den-abc.store"})
            run(["--stamp", meta, dir])

            with open(os.path.join(dir, "den-bad.store"), "wb") as fh:
                fh.write(b"DENSTOR1")  # magic, then nothing
            next_meta = os.path.join(dir, "next.json")
            write_json(next_meta, {"labelsFile": "labels-a.json", "storeFile": "den-bad.store"})
            report = run(["--compare", meta, next_meta, dir])
            self.assertIn("storeFile", report)
            self.assertIn("must be countable", report)


if __name__ == "__main__":
    unittest.main()
