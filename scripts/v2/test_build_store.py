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
import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vector_blob  # noqa: E402

BUILD_STORE = os.path.join(HERE, "build_store.py")
MIGRATE = os.path.join(HERE, "migrate_vector_blob.py")
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


def build_store_module():
    """`build_store.py` as a module — its filename is not an importable module name."""
    spec = importlib.util.spec_from_file_location("build_store", BUILD_STORE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


class StoreFixture:
    """A two-title corpus and the inputs to build a store from it.

    Built directly rather than through den-spec's fixture: that fixture is the byte-for-byte format
    contract three implementations are held to, so adding a title to it would be a format change. The
    cases below it are about what the writer DOES — which pass's labels reach the store, what it records
    about its inputs — not about the layout.
    """

    # movie:1 fills every list section the writer refuses to ship empty; movie:2 is labelled by the
    # premise pass alone.
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

    def build(self, out_dir, titles=None, plot_keys=("movie:1",), premise_keys=("movie:2",),
              plot_labels=None, premise_labels=None, write_plot_vectors=None, stamp=None):
        """`*_keys` are the blob's OWN key column; `*_labels` the labels artifact's records, which
        default to the same thing. Passing them apart is how the key-set assert is exercised;
        `write_plot_vectors` swaps in a writer of another format. `stamp` asks the writer to record
        what it read into that manifest."""
        titles = self.TITLES if titles is None else titles
        plot_keys, premise_keys = list(plot_keys), list(premise_keys)
        plot_labels = plot_keys if plot_labels is None else list(plot_labels)
        premise_labels = premise_keys if premise_labels is None else list(premise_labels)

        def dump(name, value):
            path = os.path.join(out_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(value, fh, sort_keys=True)
            return path

        def rows_file(keys):
            return {"records": [{"mediaType": k.split(":")[0], "tmdbId": int(k.split(":")[1])}
                                for k in keys]}

        def vectors(path, keys, fill):
            rows = bytearray()
            for i in range(len(keys)):
                rows.extend(bytes((fill + i + j) % 256 for j in range(DIMS)))
            vector_blob.write(path, keys, bytes(rows), DIMS)
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
             "--vectors", (write_plot_vectors or vectors)(os.path.join(out_dir, "plot.bin"), plot_keys, 7),
             "--vector-labels", dump("plot-labels.json", rows_file(plot_labels)),
             "--premise-vectors", vectors(os.path.join(out_dir, "premise.bin"), premise_keys, 200),
             "--premise-labels", dump("premise-labels.json", rows_file(premise_labels)),
             "--dataset-version", "test", "--out", store,
             *(["--stamp-meta", stamp] if stamp else [])],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"build_store failed:\n{result.stderr}")
        return ReadBack(store), result.stderr


class PremiseOnlyTitlesKeepTheirLabels(StoreFixture, unittest.TestCase):
    """A title labelled by the PREMISE pass and not the plot pass must carry its labels.

    The store's label sections were written from the corpus `labels` field alone — the plot pass's output —
    while `premiseLabels` sat beside it, read only to increment a counter that was never asserted on. Three
    real titles are labelled from their premise and never from a plot (movie:51870 *Father and Sons*,
    movie:121329 *Two Sons of Ringo*, tv:42680 *Sítio do Picapau Amarelo*), so the store answered no
    primary genre, no subgenres and no moods for them where the legacy blobs answered all three — and they
    are in the premise index, which is exactly where a reader asks.
    """

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


class TheBlobNamesItsOwnRows(StoreFixture, unittest.TestCase):
    """The vectors are joined BY KEY, and the key sets are asserted.

    v1 was `[i32 count][i32 dim][rows…]`: which title row *n* belonged to was recorded only in the order
    a separate `labels-*.json` happened to list its records. Two files with the same count and different
    orders produced a store that built cleanly and gave every title someone else's vector. That is why the
    labels artifacts had to keep being BUILT after they stopped being PUBLISHED — purely as an order
    oracle, and an oracle cannot fail, so it caught nothing.

    """

    # A deeper corpus, so "the order changed" is a thing that can be expressed at all. Two titles with
    # one vector each cannot express it; three can.
    THREE = [
        dict(StoreFixture.TITLES[0]),
        dict(StoreFixture.TITLES[1]),
        {"key": "movie:3", "mediaType": "movie", "tmdbId": 3,
         "facts": {"titles": {"en": "Gamma"}},
         "labels": {"primaryGenre": "Comedy", "animated": False,
                    "subgenres": [{"label": "Satire", "confidence": 0.8}],
                    "moods": [{"label": "Wry", "confidence": 0.4}]}},
    ]

    def test_a_reordered_labels_file_no_longer_moves_the_vectors(self):
        """THE bug. The labels artifact lists the same two titles in the other order; under the positional
        join every vector swapped titles, silently. Under the key join the store is unchanged."""
        plot = ["movie:1", "movie:3"]
        with tempfile.TemporaryDirectory() as out:
            store, _ = self.build(out, self.THREE, plot_keys=plot, plot_labels=plot)
            in_order = self.vector_rows(store)
        with tempfile.TemporaryDirectory() as out:
            # Same blob, same key column — only the labels file's record order is reversed.
            store, _ = self.build(out, self.THREE, plot_keys=plot, plot_labels=list(reversed(plot)))
            reversed_labels = self.vector_rows(store)
        self.assertEqual(
            in_order, reversed_labels,
            "reversing the labels artifact changed which title holds which vector — the store is still "
            "joining positionally, and a regenerated labels file silently reshuffles the whole index")
        self.assertNotEqual(in_order["movie:1"], in_order["movie:3"],
                            "the fixture must give the two titles different vectors, or this proves nothing")

    def test_a_blob_whose_keys_are_not_the_labels_key_set_is_fatal(self):
        """The check that replaces the assumption.

        Deliberately built so every count still agrees: the plot pass labelled movie:1 and movie:3, the
        artifact declares both, and the blob holds two rows — but its second row belongs to movie:2. Under
        the positional join that is two rows against two records and the build went straight through,
        handing movie:3 movie:2's vector. Only the key sets can see it.
        """
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(AssertionError) as caught:
                self.build(out, self.THREE,
                           plot_keys=["movie:1", "movie:2"], plot_labels=["movie:1", "movie:3"])
        message = str(caught.exception)
        self.assertIn("movie:3", message, "the refusal must name the title that has no vector")
        self.assertIn("movie:2", message, "and the vector that names a title the artifact does not")

    def test_a_v1_blob_is_refused_by_name(self):
        """A blob from before the bump joins by nothing. Refusing it has to say so — a length error would
        send someone looking for a corrupt file."""
        def v1(path, keys, fill):
            with open(path, "wb") as fh:
                fh.write(struct.pack("<II", len(keys), DIMS))
                for i in range(len(keys)):
                    fh.write(bytes((fill + i + j) % 256 for j in range(DIMS)))
            return path

        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(AssertionError) as caught:
                self.build(out, write_plot_vectors=v1)
        message = str(caught.exception)
        self.assertIn("DENVEC02", message)
        self.assertIn("migrate_vector_blob.py", message)

    def vector_rows(self, store):
        """`key -> its 1024 bytes` out of the built store."""
        off, length = store.table["vec_plot"]
        blob = store.blob[off:off + length]
        return {k: blob[i * DIMS:(i + 1) * DIMS] for i, k in enumerate(store.keys())}


class TheMigrationKeepsTheVectors(unittest.TestCase):
    """`migrate_vector_blob.py` REWRITES a v1 blob; it never re-embeds one.

    den-embed's output differs by build host — an arm64 laptop and the x86_64 box disagree on a mean of
    457 of 1024 dims for the same text — so "regenerate it with keys" would replace a measured index with
    a different one that looks identical from the outside. The rows must come through byte for byte.
    """

    KEYS = ["tv:10", "movie:1", "movie:2"]

    def v1(self, path):
        rows = bytearray()
        for i in range(len(self.KEYS)):
            rows.extend(bytes((i * 37 + j) % 256 for j in range(DIMS)))
        with open(path, "wb") as fh:
            fh.write(struct.pack("<II", len(self.KEYS), DIMS))
            fh.write(rows)
        return bytes(rows)

    def labels(self, path, keys):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"records": [{"mediaType": k.split(":")[0], "tmdbId": int(k.split(":")[1])}
                                   for k in keys]}, fh)
        return path

    def run_migration(self, out, blob, labels, dest):
        return subprocess.run([sys.executable, MIGRATE, "--blob", blob, "--labels", labels,
                               "--out", dest], capture_output=True, text=True, cwd=out)

    def test_the_vectors_are_byte_identical_and_the_keys_are_the_labels_order(self):
        with tempfile.TemporaryDirectory() as out:
            blob = os.path.join(out, "vectors.bin")
            rows = self.v1(blob)
            labels = self.labels(os.path.join(out, "labels.json"), self.KEYS)
            dest = os.path.join(out, "vectors-v2.bin")
            result = self.run_migration(out, blob, labels, dest)
            self.assertEqual(result.returncode, 0, result.stderr)

            count, dims, keys, migrated, base = vector_blob.read(dest)
            self.assertEqual(keys, self.KEYS, "the key column is the labels file's record order — the "
                                              "order the positional join used, and the last time it is read")
            self.assertEqual(count, len(self.KEYS))
            self.assertEqual(dims, DIMS)
            self.assertEqual(migrated[base:], rows, "the vectors changed — this must rewrite, not re-embed")
            self.assertEqual(len(migrated), len(rows) + 16 + 8 * len(self.KEYS))
            # The original is untouched: the migration writes a new file, it does not convert in place.
            with open(blob, "rb") as fh:
                self.assertEqual(fh.read()[8:], rows)

            report = json.loads(result.stdout)
            self.assertTrue(report["vectorsByteIdentical"])
            self.assertEqual(report["vectorsSha256Before"], report["vectorsSha256After"])

    def test_a_labels_file_of_another_generation_is_refused(self):
        """The one thing the migration cannot check for itself is whether this labels file is the one the
        blob was aligned to. A differing COUNT is the part it can see, and it refuses rather than pads."""
        with tempfile.TemporaryDirectory() as out:
            blob = os.path.join(out, "vectors.bin")
            self.v1(blob)
            labels = self.labels(os.path.join(out, "labels.json"), self.KEYS[:2])
            result = self.run_migration(out, blob, labels, os.path.join(out, "v2.bin"))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing to guess", result.stderr)

    def test_migrating_an_already_migrated_blob_is_refused(self):
        with tempfile.TemporaryDirectory() as out:
            blob = os.path.join(out, "vectors.bin")
            self.v1(blob)
            labels = self.labels(os.path.join(out, "labels.json"), self.KEYS)
            dest = os.path.join(out, "v2.bin")
            self.assertEqual(self.run_migration(out, blob, labels, dest).returncode, 0)
            again = self.run_migration(out, dest, labels, os.path.join(out, "v3.bin"))
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("already", again.stderr)


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


class RecordsWhatItRead(StoreFixture, unittest.TestCase):
    """`--stamp-meta` records WHAT THE BUILD READ, as `storeInputs`.

    oxyc/den#113's remaining gap. `data-latest` carries one blob, so the manifest names one blob, so
    `check-producers.py` — which walks the manifest's keys — checks one blob. The labels, vectors,
    metadata, facts and corpus the store is built FROM are still produced and no longer declared, and a
    store built from a stale one of them published with every guard green. This record is the only thing
    that can see them, so it has to name every input and hash it truthfully.
    """

    def test_the_stamped_manifest_records_what_the_build_read(self):
        with tempfile.TemporaryDirectory() as out:
            meta = os.path.join(out, "dataset.meta.json")
            with open(meta, "w") as fh:
                json.dump({"datasetVersion": "test"}, fh)
            self.build(out, stamp=meta)
            with open(meta) as fh:
                record = json.load(fh)["storeInputs"]

            self.assertEqual(
                sorted(e["arg"] for e in record),
                sorted(a for a in build_store_module().INPUT_ARGS if a != "enriched"),
                "every input this build was given must be recorded (it is given all but --enriched)")
            for entry in record:
                self.assertEqual(entry["sha256"], sha256(entry["path"]),
                                 f"{entry['arg']} was recorded as bytes it does not hold")
                self.assertEqual(entry["bytes"], os.path.getsize(entry["path"]))
                self.assertEqual(entry["mtime"], int(os.path.getmtime(entry["path"])))

    def test_every_input_the_parser_accepts_is_one_the_record_names(self):
        """The record is only worth anything if it is COMPLETE. A `--foo` added to the parser and not to
        INPUT_ARGS is an input the store reads, nothing records, and no producer owns — which is the gap
        this record was written to close, reopened one argument at a time."""
        bs = build_store_module()
        parsed = {a.dest for a in bs.build_parser()._actions} - {"help"}
        # The three that are not inputs: where it writes, what it calls the generation, what it stamps.
        self.assertEqual(parsed - {"out", "dataset_version", "stamp_meta"}, set(bs.INPUT_ARGS))

    def test_a_directory_input_is_digested_in_batch_number_order(self):
        """The enriched batches have no single file, so the digest is over their listing — and in
        BATCH-NUMBER order, the same rule `read_votes` uses to decide which batch wins. Lexicographic
        order puts `batch-10` before `batch-2`, so a lexicographic digest would depend on how many
        digits a batch id happens to have."""
        bs = build_store_module()
        with tempfile.TemporaryDirectory() as dir:
            enriched = os.path.join(dir, "enriched")
            os.makedirs(enriched)
            bodies = {"batch-2.json": '[{"a":1}]', "batch-10.json": '[{"b":2}]'}
            for name, body in bodies.items():
                with open(os.path.join(enriched, name), "w") as fh:
                    fh.write(body)

            digest, size, _ = bs.input_digest(enriched)
            self.assertEqual(size, sum(len(b) for b in bodies.values()))

            listing = hashlib.sha256()
            for name in ("batch-2.json", "batch-10.json"):
                listing.update(
                    f"{name} {hashlib.sha256(bodies[name].encode()).hexdigest()}\n".encode())
            self.assertEqual(digest, listing.hexdigest())

            # And it MOVES when a batch is rewritten — a directory whose digest ignored its contents
            # would be a record that cannot notice the thing it exists to notice.
            with open(os.path.join(enriched, "batch-2.json"), "w") as fh:
                fh.write('[{"a":99}]')
            self.assertNotEqual(bs.input_digest(enriched)[0], digest)


if __name__ == "__main__":
    unittest.main()
