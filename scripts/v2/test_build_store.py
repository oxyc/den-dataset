"""`build_store.py` against den-spec's committed fixture — the WRITER's half of the format contract.

store-v1 is implemented three times: this writer, `den-core/crates/den-store` and the mmap in den-atlas.
Both readers load `den-spec/vectors/store-v1.*` in their own tests and fail without it. The writer — the
side that decides what the bytes actually are — had no test at all, so a layout change here would have
been caught only by the readers failing afterwards, in other repos, on someone else's branch.

The check is a rebuild: run the real generator, and require the result to be byte-identical to the
committed fixture. Byte-identical is the right bar rather than a field-by-field comparison, because the
writer promises deterministic output (dictionaries sorted before ids are assigned, rows sorted by key) and
that promise is what makes a content hash meaningful. Anything that perturbs a single byte — a changed
section order, an unsorted dictionary, a float rounded differently — fails here.
"""
import gzip
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_STORE = os.path.join(HERE, "build_store.py")
DIMS = 1024


def spec_dir():
    """den-spec, as a sibling checkout or via DEN_SPEC_DIR."""
    return os.environ.get("DEN_SPEC_DIR", os.path.join(HERE, "..", "..", "..", "den-spec"))


def spec_or_fail(*parts):
    """A path inside den-spec, or a FAILURE.

    Skipping when the contract is absent is the false pass this kind of test is most prone to: the run
    reports OK and has verified nothing. DEN_SPEC_OPTIONAL=1 is the deliberate escape, matching
    den-core's `fixture_or_fail!` and den-atlas's `spec_fixture`.
    """
    path = os.path.join(spec_dir(), *parts)
    if os.path.isfile(path):
        return path
    # `== "1"`, not truthiness: DEN_SPEC_OPTIONAL=0, set to turn skipping OFF, would otherwise turn it on.
    if os.environ.get("DEN_SPEC_OPTIONAL") == "1":
        raise unittest.SkipTest("den-spec absent and DEN_SPEC_OPTIONAL=1")
    raise AssertionError(
        f"{path} not found — this test checks build_store.py against the store-v1 contract and cannot "
        "do so without it. Check out den-spec beside this repo, set DEN_SPEC_DIR, or set "
        "DEN_SPEC_OPTIONAL=1 to skip deliberately."
    )


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class FixtureRoundTrip(unittest.TestCase):
    def test_the_writer_reproduces_the_committed_fixture(self):
        committed = spec_or_fail("vectors", "store-v1.store")
        generator = spec_or_fail("tools", "store-fixture.py")
        with tempfile.TemporaryDirectory() as out:
            result = subprocess.run(
                [sys.executable, generator, "--build-store", BUILD_STORE, "--out-dir", out],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, f"the fixture generator failed:\n{result.stderr}")
            rebuilt = os.path.join(out, "store-v1.store")
            self.assertTrue(os.path.isfile(rebuilt), f"generator wrote nothing:\n{result.stdout}")
            self.assertEqual(
                sha256(rebuilt),
                sha256(committed),
                "build_store.py no longer produces the committed fixture. Either the layout changed — in "
                "which case this is a NEW format version (store-v2.md), not an edit to store-v1 — or the "
                "writer lost its determinism.",
            )

    def test_a_rebuild_is_byte_identical_to_itself(self):
        """Determinism across processes, which is what lets a content hash mean anything. Python's hash
        randomisation differs per process, so two runs disagreeing would mean a dictionary order leaked
        into the bytes."""
        generator = spec_or_fail("tools", "store-fixture.py")
        digests = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as out:
                result = subprocess.run(
                    [sys.executable, generator, "--build-store", BUILD_STORE, "--out-dir", out],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                digests.append(sha256(os.path.join(out, "store-v1.store")))
        self.assertEqual(digests[0], digests[1], "two runs of the writer disagree byte-for-byte")


class ReadBack:
    """The bits of a store this file needs to assert on — the section table, and a row's labels.

    Deliberately not a general reader: den-core and den-atlas are the readers, and a third one here would
    be a third place the layout can drift. It parses the table and four sections, nothing else.
    """

    def __init__(self, path):
        with open(path, "rb") as fh:
            self.blob = fh.read()
        count, self.rows = struct.unpack("<II", self.blob[24:32])
        self.table = {}
        for i in range(count):
            at = 64 + i * 32
            name, off, length, _ = struct.unpack("<16sQII", self.blob[at:at + 32])
            self.table[name.rstrip(b"\0").decode()] = (off, length)
        self.str_off = self.ints("str_off")
        off, length = self.table["strings"]
        self.str_blob = self.blob[off:off + length]

    def ints(self, name, fmt="I", width=4):
        off, length = self.table[name]
        return list(struct.unpack(f"<{length // width}{fmt}", self.blob[off:off + length]))

    def text(self, ident):
        if ident == 0xFFFFFFFF:
            return None
        return self.str_blob[self.str_off[ident]:self.str_off[ident + 1]].decode("utf-8")

    def keys(self):
        return [f"{'movie' if k >> 32 == 0 else 'tv'}:{k & 0xFFFFFFFF}" for k in self.ints("keys", "Q", 8)]

    def labelled(self, name, row):
        """`[(label, confidence)]` for one row of a labelled list section."""
        offsets = self.ints(f"{name}_o")
        ids = self.ints(f"{name}_v")
        confs = self.ints(f"{name}_c", "B", 1)
        return [(self.text(ids[i]), confs[i]) for i in range(offsets[row], offsets[row + 1])]


class PremiseOnlyTitlesKeepTheirLabels(unittest.TestCase):
    """A title labelled by the PREMISE pass and not the plot pass must carry its labels.

    The store's label sections were written from the corpus `labels` field alone — the plot pass's output —
    while `premiseLabels` sat beside it, read only to increment a counter that was never asserted on. Three
    real titles are labelled from their premise and never from a plot (movie:51870 *Father and Sons*,
    movie:121329 *Two Sons of Ringo*, tv:42680 *Sítio do Picapau Amarelo*), so the store answered no
    primary genre, no subgenres and no moods for them where the legacy blobs answered all three — and they
    are in the premise index, which is exactly where a reader asks.

    This builds the case directly rather than through den-spec's fixture: that fixture is the byte-for-byte
    format contract three implementations are held to, so adding a title to it would be a format change.
    """

    # movie:1 fills every list section the writer refuses to ship empty; movie:2 is the case under test.
    TITLES = [
        {
            "key": "movie:1", "mediaType": "movie", "tmdbId": 1,
            "facts": {
                "titles": {"en": "Alpha", "orig": "Alfa", "aliases": ["Alpha One"]},
                "genres": ["Q1"], "countries": ["US"], "languages": ["EN"],
                "directors": ["Q100"], "cast": ["Q101"], "broadcaster": ["Q102"],
                "composers": ["Q103"], "cinematographers": ["Q104"], "distributors": ["Q105"],
                "productionCompanies": ["Q106"], "narrativeLocations": ["Q107"],
                "mainSubjects": ["Q108"], "instanceOf": ["Q109"], "basedOn": ["Q110"],
                "basedOnKind": ["book"],
            },
            "labels": {"primaryGenre": "Drama", "animated": False,
                       "subgenres": [{"label": "Prison", "confidence": 0.7}],
                       "moods": [{"label": "Bleak", "confidence": 0.55}]},
            "premiseLabels": None,
            "nouls": {"theme__epic": {"noul": 0.9}},
        },
        {
            "key": "movie:2", "mediaType": "movie", "tmdbId": 2,
            "facts": {"titles": {"en": "Beta"}},
            "labels": None,
            "premiseLabels": {"primaryGenre": "Western", "animated": False,
                              "subgenres": [{"label": "Road Movie", "confidence": 0.5}],
                              "moods": [{"label": "Campy", "confidence": 0.6}]},
        },
    ]

    def build(self, out_dir, titles=None):
        titles = self.TITLES if titles is None else titles

        def dump(name, value):
            path = os.path.join(out_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(value, fh, sort_keys=True)
            return path

        def rows_file(keys):
            return {"records": [{"mediaType": k.split(":")[0], "tmdbId": int(k.split(":")[1])}
                                for k in keys]}

        def vectors(path, count, fill):
            with open(path, "wb") as fh:
                fh.write(struct.pack("<II", count, DIMS))
                for i in range(count):
                    fh.write(bytes((fill + i + j) % 256 for j in range(DIMS)))
            return path

        corpus = os.path.join(out_dir, "corpus.jsonl.gz")
        with open(corpus, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as fh:
                for row in titles:
                    fh.write((json.dumps(row, sort_keys=True) + "\n").encode())

        store = os.path.join(out_dir, "test.store")
        result = subprocess.run(
            [sys.executable, BUILD_STORE,
             "--corpus", corpus,
             # Q100 carries an alias: `ent_alias_v` is one of the sections the writer refuses to ship empty.
             "--entities", dump("entities.json", dict(
                 {f"Q{q}": {"en": f"Name {q}"} for q in range(100, 111)},
                 Q100={"en": "Name 100", "aliases": ["Nom 100"]})),
             "--facts", dump("facts.json", {"genreMap": {"Q1": {"movie": 18}},
                                            "records": [{"mediaType": t["mediaType"], "tmdbId": t["tmdbId"]}
                                                        for t in titles]}),
             "--metadata", dump("metadata.json", {"records": [
                 {"mediaType": "movie", "tmdbId": 1, "title": "Alpha", "year": 1999},
                 {"mediaType": "movie", "tmdbId": 2, "title": "Beta", "year": 2001}]}),
             # The plot pass labelled movie:1 only; the premise pass labelled movie:2 only.
             "--vectors", vectors(os.path.join(out_dir, "plot.bin"), 1, 7),
             "--vector-labels", dump("plot-labels.json", rows_file(["movie:1"])),
             "--premise-vectors", vectors(os.path.join(out_dir, "premise.bin"), 1, 200),
             "--premise-labels", dump("premise-labels.json", rows_file(["movie:2"])),
             "--dataset-version", "test", "--out", store],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"build_store failed:\n{result.stderr}")
        return ReadBack(store), result.stderr

    def test_the_premise_pass_labels_reach_the_store(self):
        with tempfile.TemporaryDirectory() as out:
            store, stderr = self.build(out)
            self.assertEqual(store.keys(), ["movie:1", "movie:2"])
            row = store.keys().index("movie:2")

            primary = store.ints("primary_genre")
            self.assertEqual(
                store.text(primary[row]), "Western",
                "movie:2 was labelled by the premise pass alone and the store holds no primary genre "
                "for it — the label sections are being written from the plot pass only.")
            self.assertEqual(store.labelled("subgenre", row), [("Road Movie", 50)])
            self.assertEqual(store.labelled("mood", row), [("Campy", 60)])

            # And the plot pass's title is untouched: the union adds, it does not replace.
            plot_row = store.keys().index("movie:1")
            self.assertEqual(store.text(primary[plot_row]), "Drama")
            self.assertEqual(store.labelled("subgenre", plot_row), [("Prison", 70)])
            self.assertEqual(store.labelled("mood", plot_row), [("Bleak", 55)])

            summary = json.loads(stderr[stderr.index("{"):stderr.rindex("}") + 1])
            self.assertEqual(summary["withLabels"], 2, "both titles are labelled, by one pass or the other")
            self.assertEqual(summary["withPlotLabels"], 1)
            self.assertEqual(summary["withPremiseLabels"], 1)

    def test_a_label_artifact_the_store_does_not_cover_is_fatal(self):
        """The assert that makes the union's own miss loud. A title the premise pass labelled but the
        corpus never carried is the same silent drop in the other direction, and a count that matched only
        the plot artifact would not see it."""
        # The premise artifact still names movie:2; the corpus row for it no longer carries the labels.
        stripped = [dict(t, premiseLabels=None) if t["key"] == "movie:2" else t for t in self.TITLES]
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(AssertionError) as caught:
                self.build(out, stripped)
        self.assertIn("labelled titles", str(caught.exception))

    def test_the_two_passes_disagreeing_is_fatal_rather_than_silently_resolved(self):
        """`title_labels` takes the plot record WHOLE where both passes answered, which is safe only while
        they agree — and they do today, for all 44,528 such titles. If a repass ever makes them diverge,
        which labelling ships is a decision, and the `or` would make it by preferring whichever field came
        first. The build refuses and names the titles instead of choosing quietly.

        movie:1 is plot-labelled `Drama` in the fixture; give it a premise record calling it `Horror`."""
        clashing = [
            dict(t, premiseLabels={"primaryGenre": "Horror", "animated": False,
                                   "subgenres": [{"label": "Prison", "confidence": 0.7}],
                                   "moods": [{"label": "Bleak", "confidence": 0.55}]})
            if t["key"] == "movie:1" else t
            for t in self.TITLES
        ]
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(AssertionError) as caught:
                self.build(out, clashing)
        message = str(caught.exception)
        self.assertIn("disagree", message)
        self.assertIn("primaryGenre", message)
        self.assertIn("movie:1", message)


class StampsTheManifest(unittest.TestCase):
    """`--stamp-meta`. Without it the store is written and nothing names it: `publish-dataset.sh`
    announces an unowned blob, `den-atlas` never sees a `storeFile`, and the rail falls back."""

    def test_the_store_declares_itself(self):
        import hashlib
        import json

        generator = spec_or_fail("tools", "store-fixture.py")
        with tempfile.TemporaryDirectory() as out:
            meta = os.path.join(out, "dataset.meta.json")
            with open(meta, "w") as fh:
                json.dump({"datasetVersion": "fixture", "labelsFile": "labels.json"}, fh)
            result = subprocess.run(
                [sys.executable, generator, "--build-store", BUILD_STORE, "--out-dir", out,
                 "--", "--stamp-meta", meta],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            with open(meta) as fh:
                stamped = json.load(fh)
            with open(os.path.join(out, stamped["storeFile"]), "rb") as fh:
                blob = fh.read()
            self.assertEqual(stamped["storeBytes"], len(blob))
            self.assertEqual(stamped["storeSha256"], hashlib.sha256(blob).hexdigest())
            self.assertEqual(
                stamped["labelsFile"], "labels.json", "stamping must not drop the keys already there"
            )
            self.assertNotIn(
                "storeGzFile", stamped, "the store is mmap'd; a compressed twin cannot be mapped"
            )


if __name__ == "__main__":
    unittest.main()
