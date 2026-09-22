"""`build_store.py` against den-spec's committed fixture — the WRITER's half of the format contract.

store-v2 is implemented three times: this writer, `den-core/crates/den-store` and the mmap in den-atlas.
Both readers load `den-spec/vectors/store-v2.*` in their own tests and fail without it. The writer — the
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
        f"{path} not found — this test checks build_store.py against the store-v2 contract and cannot "
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
        committed = spec_or_fail("vectors", "store-v2.store")
        generator = spec_or_fail("tools", "store-fixture.py")
        with tempfile.TemporaryDirectory() as out:
            result = subprocess.run(
                [sys.executable, generator, "--build-store", BUILD_STORE, "--out-dir", out],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, f"the fixture generator failed:\n{result.stderr}")
            rebuilt = os.path.join(out, "store-v2.store")
            self.assertTrue(os.path.isfile(rebuilt), f"generator wrote nothing:\n{result.stdout}")
            self.assertEqual(
                sha256(rebuilt),
                sha256(committed),
                "build_store.py no longer produces the committed fixture. Three causes, and they want "
                "different answers: the LAYOUT changed — in which case this is a new format version "
                "(store-v3.md), not an edit to store-v2; the writer lost its DETERMINISM; or what the "
                "writer CHOOSES TO PUBLISH changed, which is a content change at an unchanged layout "
                "and is fixed by regenerating the fixture in den-spec, not by bumping the version. The "
                "publication gates are the third kind: a fixture facet carrying no `probabilities` "
                "cannot clear them, so den-spec's fixture records need the distributions the real "
                "corpus always carries.",
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
                digests.append(sha256(os.path.join(out, "store-v2.store")))
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

    def facets(self, row, axes):
        """`{axis: (value, confidence)}` for one row of the dense R x 12 facet columns.

        `value` is None where the store makes no claim — an axis the model declined, and now also one
        the publication gates withheld. They are the same sentinel on purpose; see `build_store.py`.
        """
        values = self.ints("facet_v")
        confs = self.ints("facet_c", "B", 1)
        at = row * len(axes)
        return {axis: (self.text(values[at + i]), confs[at + i]) for i, axis in enumerate(axes)}

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

    # movie:1 fills every list section the writer refuses to ship empty; movie:2 has no genres & moods,
    # only the premise pass's copy of some, which the writer does not read.
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
                "basedOnKind": ["book"], "franchise": ["Q111"],
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
              plot_labels=None, premise_labels=None, write_plot_vectors=None, stamp=None,
              build_store=BUILD_STORE):
        """`*_keys` are the blob's OWN key column; `*_labels` the labels artifact's records, which
        default to the same thing. Passing them apart is how the key-set assert is exercised;
        `write_plot_vectors` swaps in a writer of another format. `stamp` asks the writer to record
        what it read into that manifest. `build_store` runs a wrapper around the real writer instead of
        the writer itself — the only way to reach a guard that fires on the writer's own constants."""
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
            [sys.executable, build_store,
             "--corpus", corpus,
             # Q100 carries an alias: `ent_alias_v` is one of the sections the writer refuses to ship empty.
             "--entities", dump("entities.json", dict(
                 {f"Q{q}": {"en": f"Name {q}"} for q in range(100, 111)},
                 Q100={"en": "Name 100", "aliases": ["Nom 100"]})),
             "--facts", dump("facts.json", {"genreMap": {"Q1": {"movie": 18}},
                                            "records": [{"mediaType": t["mediaType"], "tmdbId": t["tmdbId"]}
                                                        for t in titles]}),
             # No `--metadata`: the TMDB sidecar supplied the title, the year and the poster path, and the
             # writer now takes the first two from the corpus's own `facts` and publishes no third.
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


class GenresAndMoodsHaveOneSource(StoreFixture, unittest.TestCase):
    """A title's genres & moods are the corpus `labels` field, joined from `genres-moods.json`, and nothing
    else.

    The premise pass's labels used to fill in where the plot pass had none. They were a copy of the plot
    labels (44,528 of 44,531 identical, oxyc/den-dataset#56), and a second source is a second answer: three
    real titles (movie:51870 *Father and Sons*, movie:121329 *Two Sons of Ringo*, tv:42680 *Sítio do
    Picapau Amarelo*) carried genres & moods only that copy knew of, which the genres & moods stage never
    decided.
    """

    def test_the_premise_pass_labels_are_not_read(self):
        with tempfile.TemporaryDirectory() as out:
            store, stderr = self.build(out)
            self.assertEqual(store.keys(), ["movie:1", "movie:2"])
            primary = store.ints("primary_genre")
            row = store.keys().index("movie:2")
            self.assertIsNone(store.text(primary[row]), "movie:2's only labels are the premise pass's copy")
            self.assertEqual(store.labelled("subgenre", row), [])
            self.assertEqual(store.labelled("mood", row), [])

            plot_row = store.keys().index("movie:1")
            self.assertEqual(store.text(primary[plot_row]), "Drama")
            self.assertEqual(store.labelled("subgenre", plot_row), [("Prison", 70)])
            self.assertEqual(store.labelled("mood", plot_row), [("Bleak", 55)])

            summary = json.loads(stderr[stderr.index("{"):stderr.rindex("}") + 1])
            self.assertEqual(summary["withLabels"], 1)

    def test_a_plot_vector_title_the_corpus_carries_no_genres_and_moods_for_is_fatal(self):
        """The plot labels file is `finalize`'s record of the titles with a vector, each with its genres
        & moods; a corpus row for one of them without any is a join that missed. movie:2's premise copy
        does not stand in for them."""
        titles = self.TITLES + [{"key": "movie:3", "mediaType": "movie", "tmdbId": 3,
                                 "facts": {"titles": {"en": "Gamma"}}, "labels": None,
                                 "premiseLabels": self.TITLES[0]["labels"]}]
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(AssertionError) as caught:
                self.build(out, titles, plot_keys=("movie:1", "movie:3"))
        self.assertIn("labelled titles: 1 in the store, 2 in", str(caught.exception))


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

if __name__ == "__main__":
    unittest.main()


def choice(value, probabilities, confidence=0.9):
    """One typed Choice answer, shaped as the corpus carries it."""
    return {"type": "choice", "choice": value, "confidence": confidence,
            "probabilities": probabilities}


def applicability(validity=1.0, narrative="bounded-fictional-narrative"):
    """An `applicability` block: `validity`'s probability of `correct-screen-work`, and the typed
    content-type answer the archetype gate reads."""
    return {
        "validity": choice("correct-screen-work",
                           {"correct-screen-work": validity, "source-work": 1.0 - validity}),
        "narrative_applicability": choice(narrative, {narrative: 1.0}),
    }


class PublicationGates(unittest.TestCase):
    """`publishable()` against FACETS-V2 § "Publication gates" 2-4, clause by clause.

    The store used to write every non-`does-not-apply` argmax with its confidence and no gate at all, so
    `ending` shipped 8,858 titles whose published value was the literal string `unknown` and `archetype`
    shipped 28,996 including documentaries. The spec has said since the pilot that the pilots "support
    collection, not unconditional argmax publication".
    """

    def setUp(self):
        self.mod = build_store_module()

    def test_a_clean_answer_is_published(self):
        """The gate must not simply empty the axis: a confident, well-separated answer still ships."""
        ok, reason = self.mod.publishable(
            "tone", choice("comic", {"comic": 0.9, "pulpy": 0.1}), 1.0, "bounded-fictional-narrative")
        self.assertTrue(ok, f"a 0.90 answer must publish, refused as {reason}")

    def test_probability_below_070_is_withheld(self):
        """Clause 4: "require probability at least 0.70"."""
        ok, reason = self.mod.publishable(
            "tone", choice("comic", {"comic": 0.6, "pulpy": 0.4}), 1.0, "bounded-fictional-narrative")
        self.assertFalse(ok)
        self.assertEqual(reason, "p<0.70")

    def test_the_probability_is_read_from_the_distribution_not_the_confidence(self):
        """`confidence` is the model's self-report and `probabilities` is its distribution. They are
        separate fields with different values — for movie:100 `ending` reads confidence 0.32 against a
        0.40 probability — and clause 4 is written about the probability."""
        answer = choice("comic", {"comic": 0.6, "pulpy": 0.4}, confidence=0.99)
        ok, _ = self.mod.publishable("tone", answer, 1.0, "bounded-fictional-narrative")
        self.assertFalse(ok, "a 0.99 self-report must not carry a 0.60 probability past the gate")

    def test_a_thin_margin_is_withheld(self):
        """Clause 4: "a top-minus-runner-up margin of at least 0.25".

        Unreachable through the probability clause on a normalised distribution — at p>=0.70 the runner-up
        is at most 0.30 — so it is tested directly. It stays because the spec states it and because a
        distribution that does not sum to 1 (the runner is allowed 0.03 of slack) could still reach it.
        """
        ok, reason = self.mod.publishable(
            "tone", choice("comic", {"comic": 0.8, "pulpy": 0.7}), 1.0, "bounded-fictional-narrative")
        self.assertFalse(ok)
        self.assertEqual(reason, "margin<0.25")

    def test_the_margin_counts_does_not_apply_as_a_rival(self):
        """`does-not-apply` may not be PUBLISHED, but it is still a hypothesis the model weighed.
        Dropping it from the comparison would inflate the margin by whatever mass sat on it."""
        ok, reason = self.mod.publishable(
            "tone", choice("comic", {"comic": 0.8, "does-not-apply": 0.75}), 1.0,
            "bounded-fictional-narrative")
        self.assertFalse(ok)
        self.assertEqual(reason, "margin<0.25")

    def test_validity_below_080_withholds_the_axis(self):
        """Clause 2: "Publish plot facets only when `validity=correct-screen-work` has probability at
        least 0.80" — the article is about some other work, so its plot facets describe that one."""
        ok, reason = self.mod.publishable(
            "tone", choice("comic", {"comic": 0.99, "pulpy": 0.01}), 0.79, "bounded-fictional-narrative")
        self.assertFalse(ok)
        self.assertEqual(reason, "validity<0.80")

    def test_a_non_narrative_programme_publishes_no_facets(self):
        """Clause 3: "Suppress plot facets for talk, variety, game, news, or reality programs without a
        bounded narrative"."""
        for axis in ("tone", "ending", "archetype"):
            ok, reason = self.mod.publishable(
                axis, choice("comic", {"comic": 0.99, "pulpy": 0.01}), 1.0, "non-narrative-program")
            self.assertFalse(ok, f"{axis} must be suppressed for a non-narrative programme")
            self.assertEqual(reason, "non-narrative-program")

    def test_ending_unknown_is_never_published(self):
        """Clause 4: "Exclude `does-not-apply` and `ending=unknown`". `unknown` is the corpus saying a
        work has not ended, which is not an ending a viewer can browse to — and it was the single
        largest `ending` value in the live index at 8,858 titles."""
        ok, reason = self.mod.publishable(
            "ending", choice("unknown", {"unknown": 0.99, "happy": 0.01}), 1.0,
            "bounded-fictional-narrative")
        self.assertFalse(ok)
        self.assertEqual(reason, "ending=unknown")
        # …and `unknown` is excluded on `ending` alone, not as a global stopword.
        ok, _ = self.mod.publishable(
            "tone", choice("unknown", {"unknown": 0.99, "comic": 0.01}), 1.0,
            "bounded-fictional-narrative")
        self.assertTrue(ok, "the exclusion is `ending=unknown`, not every `unknown`")

    def test_does_not_apply_is_never_published(self):
        ok, reason = self.mod.publishable(
            "tone", choice("does-not-apply", {"does-not-apply": 0.99, "comic": 0.01}), 1.0,
            "bounded-fictional-narrative")
        self.assertFalse(ok)
        self.assertEqual(reason, "does-not-apply")

    def test_archetype_needs_a_bounded_narrative(self):
        """Clause 3: "Suppress archetype additionally for documentaries, anthologies, open-ended series,
        and multi-arc works", read from "a separate typed applicability result, not from archetype's own
        no-answer probability".

        The spec's own case: it assigned the documentary `Killer Inside: The Mind of Aaron Hernandez` to
        `downfall` at 0.76 "despite an explicit mandatory documentary exclusion. That is decisive: more
        wording is not a publication guard." 0.76 clears every confidence threshold in the spec, so only
        the content type refuses it.
        """
        downfall = choice("downfall", {"downfall": 0.76, "rise": 0.24})
        for narrative in ("documentary-or-factual", "anthology", "open-or-multi-arc-narrative",
                          "insufficient-evidence", None):
            ok, reason = self.mod.publishable("archetype", downfall, 1.0, narrative)
            self.assertFalse(ok, f"archetype must not publish for {narrative}")
            self.assertIn("not-bounded", reason)
        ok, _ = self.mod.publishable("archetype", downfall, 1.0, "bounded-fictional-narrative")
        self.assertTrue(ok, "a bounded fictional narrative may carry an archetype")

    def test_the_other_axes_survive_a_documentary(self):
        """The archetype suppression is ADDITIONAL: a documentary still has a setting and an era."""
        for axis in ("era", "setting", "tone"):
            ok, _ = self.mod.publishable(
                axis, choice("urban", {"urban": 0.95, "rural": 0.05}), 1.0, "documentary-or-factual")
            self.assertTrue(ok, f"{axis} must survive a documentary")

    def test_an_answer_with_no_distribution_cannot_be_judged(self):
        """Clause 4 is written about a distribution. Publishing an answer that has none would be exactly
        the unconditional argmax publication the gates exist to stop."""
        ok, reason = self.mod.publishable(
            "tone", {"type": "choice", "choice": "comic", "confidence": 0.99}, 1.0,
            "bounded-fictional-narrative")
        self.assertFalse(ok)
        self.assertEqual(reason, "no distribution")

    def test_a_row_with_no_applicability_block_publishes_nothing(self):
        probability, narrative = self.mod.row_applicability({})
        self.assertEqual(probability, 0.0)
        self.assertIsNone(narrative)
        ok, reason = self.mod.publishable(
            "tone", choice("comic", {"comic": 0.99, "pulpy": 0.01}), probability, narrative)
        self.assertFalse(ok)
        self.assertEqual(reason, "validity<0.80")


class GatedFacetsReachTheStore(StoreFixture, unittest.TestCase):
    """The gate in the WRITER: a withheld value must be absent from the built artifact.

    A gate in den-atlas could not implement this one. The store carries an argmax and a single confidence
    byte per axis — the runner-up probability clause 4 needs, and `validity`'s own distribution clause 2
    needs, are not in it and never were. This proves the refusal lands in the bytes.
    """

    def titles(self):
        """Two titles: a documentary whose every facet the model answered confidently, and a film whose
        `tone` it guessed at 0.60."""
        base = dict(self.TITLES[0])
        base["applicability"] = applicability(narrative="documentary-or-factual")
        base["facets"] = {
            "setting": choice("urban", {"urban": 0.95, "rural": 0.05}),
            "archetype": choice("downfall", {"downfall": 0.76, "rise": 0.24}),
            "ending": choice("unknown", {"unknown": 0.9, "happy": 0.1}),
        }
        other = dict(self.TITLES[1])
        other["applicability"] = applicability()
        other["facets"] = {
            "tone": choice("comic", {"comic": 0.6, "pulpy": 0.4}),
            "era": choice("contemporary", {"contemporary": 0.97, "medieval": 0.03}),
        }
        return [base, other]

    def test_the_store_carries_only_what_the_gates_published(self):
        mod = build_store_module()
        with tempfile.TemporaryDirectory() as tmp:
            store, stderr = self.build(tmp, titles=self.titles())
        axes = mod.FACET_AXES
        doc = store.facets(0, axes)
        film = store.facets(1, axes)

        self.assertEqual(doc["setting"][0], "urban", "a confident axis on a documentary still ships")
        self.assertIsNone(doc["archetype"][0], "a documentary must carry no archetype")
        self.assertIsNone(doc["ending"][0], "`ending=unknown` must never be published")
        self.assertEqual(film["era"][0], "contemporary")
        self.assertIsNone(film["tone"][0], "a 0.60 tone must not be published")

        # A withheld cell is the SAME sentinel as an axis the model never answered — the store makes no
        # claim either way, and it has one sentinel per axis. The confidence byte goes with it, so a
        # reader cannot mistake a withheld cell for a published 0.
        self.assertEqual(doc["archetype"], (None, 0))
        self.assertEqual(doc["pacing"], (None, 0), "an axis with no answer reads identically")

        # The withheld values must not be left in the string dictionary either: a vocabulary entry no
        # row uses reads from the outside as a value with zero titles, not as one the gate withheld.
        interned = {store.text(i) for i in range(len(store.str_off) - 1)}
        self.assertNotIn("downfall", interned)
        self.assertIn("urban", interned)

        # And the build must SAY what it withheld, rather than leave it as a coverage surprise.
        self.assertIn("publication gates", stderr)
        self.assertIn("archetype", stderr)

    def test_the_manifest_records_what_was_withheld(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamp = os.path.join(tmp, "dataset.meta.json")
            with open(stamp, "w", encoding="utf-8") as fh:
                json.dump({}, fh)
            self.build(tmp, titles=self.titles(), stamp=stamp)
            with open(stamp, encoding="utf-8") as fh:
                gates = json.load(fh)["facetGates"]
        self.assertEqual(gates["probabilityMin"], 0.70)
        self.assertEqual(gates["validityMin"], 0.80)
        self.assertEqual(gates["axes"]["archetype"]["published"], 0)
        self.assertEqual(gates["axes"]["setting"]["published"], 1)
        self.assertIn("ending=unknown", gates["axes"]["ending"]["withheld"])


#: A wrapper that loads the real writer, edits one of its constants, and runs it. `check_provenance`
#: fires on the writer's own table, so nothing a test can put in the INPUTS reaches it — the table has
#: to be the thing that moves.
MUTATED_WRITER = """import importlib.util, sys
spec = importlib.util.spec_from_file_location("build_store", {writer!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
{mutation}
mod.main()
"""


class EverySectionDeclaresWhereItsBytesCameFrom(StoreFixture, unittest.TestCase):
    """`PROVENANCE` is total over the sections, and checked as a set at assembly.

    The store is a PUBLIC release asset, so publishing it redistributes whatever it holds — and until
    this table, a column carrying vendor-licensed content reached that asset by being added to the
    writer and nothing else. Every other guard in this file checks a section against its SOURCE; none
    of them could see a section that should not exist. See LICENSES.md and oxyc/den#118.
    """

    def mutated(self, out_dir, mutation):
        """A writer with `mutation` applied to its module namespace, as a path to run."""
        path = os.path.join(out_dir, "mutated_writer.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(MUTATED_WRITER.format(writer=BUILD_STORE, mutation=mutation))
        return path

    def test_the_table_names_exactly_the_sections_the_writer_writes(self):
        """The invariant the build asserts, held against a store that was actually built — so the table
        is checked against the bytes rather than against a reading of this file."""
        mod = build_store_module()
        with tempfile.TemporaryDirectory() as out:
            store, _ = self.build(out)
        self.assertEqual(
            set(store.table), set(mod.PROVENANCE),
            "the sections in the built store and the sections PROVENANCE declares have diverged. A "
            "section with no entry is a column nothing reviewed; an entry with no section is a table "
            "describing a store that does not exist.")

    def test_a_section_with_no_declared_source_stops_the_build(self):
        """The case this exists for, end to end: the writer emits a section the table does not know
        about and the store is not written."""
        with tempfile.TemporaryDirectory() as out:
            writer = self.mutated(out, 'mod.PROVENANCE.pop("card_title")')
            with self.assertRaises(AssertionError) as caught:
                self.build(out, build_store=writer)
            self.assertFalse(os.path.exists(os.path.join(out, "test.store")),
                             "the refusal must come before the bytes are written")
        message = str(caught.exception)
        self.assertIn("card_title", message)
        self.assertIn("declare no source", message)

    def test_an_entry_for_a_section_that_is_not_written_stops_the_build(self):
        """The other direction. A stale entry would let a removal look reviewed: the table would still
        describe the column, and the allowlist below would still be granting permission for it."""
        with tempfile.TemporaryDirectory() as out:
            writer = self.mutated(out, 'mod.PROVENANCE["overview"] = "wikipedia"')
            with self.assertRaises(AssertionError) as caught:
                self.build(out, build_store=writer)
        message = str(caught.exception)
        self.assertIn("overview", message)
        self.assertIn("not written", message)

    def test_every_section_declares_one_of_the_named_sources(self):
        """A source outside `SOURCES` is a section that looks declared and says nothing — the check
        below reads `VENDOR_SOURCES` membership, so a typo'd `"tmdb "` would read as harmless."""
        mod = build_store_module()
        self.assertEqual(set(mod.PROVENANCE.values()) - mod.SOURCES, set())

    def test_a_source_the_named_set_does_not_hold_stops_the_build(self):
        with tempfile.TemporaryDirectory() as out:
            writer = self.mutated(out, 'mod.PROVENANCE["card_title"] = "tmdb "')
            with self.assertRaises(AssertionError) as caught:
                self.build(out, build_store=writer)
        self.assertIn("is not one of", str(caught.exception))

    def test_the_only_vendor_sourced_sections_are_the_ones_118_is_removing(self):
        """The allowlist, held against the table. `votes` is all #118 has left to replace; everything
        else is Wikidata, Wikipedia, a model's answer over those, our own bookkeeping, or an identifier.
        When it goes, this set is empty and the store carries identifiers only."""
        mod = build_store_module()
        vendor = {name for name, source in mod.PROVENANCE.items() if source in mod.VENDOR_SOURCES}
        self.assertEqual(vendor, set(), "no section may carry a vendor's content")
        self.assertEqual(mod.VENDOR_ALLOWED, set(), "and nothing is permitted to")
        self.assertEqual(mod.PROVENANCE["card_title"], "wikidata", "the label, not the TMDB title")
        self.assertEqual(mod.PROVENANCE["card_year"], "wikidata", "the release date, not the TMDB year")
        self.assertNotIn("votes", mod.PROVENANCE, "the vote count is joined at read time, not stored")
        self.assertNotIn("imdb", vendor, "an id is a join key, not content")
        self.assertNotIn("keys", vendor)

    def test_a_vendor_sourced_section_outside_the_allowlist_stops_the_build(self):
        with tempfile.TemporaryDirectory() as out:
            writer = self.mutated(out, 'mod.PROVENANCE["primary_genre"] = "tmdb"')
            with self.assertRaises(AssertionError) as caught:
                self.build(out, build_store=writer)
        message = str(caught.exception)
        self.assertIn("primary_genre", message)
        self.assertIn("VENDOR_ALLOWED", message)

    def test_a_section_carrying_imdb_counts_stops_the_build(self):
        """The enrichment reads IMDb's vote counts to admit titles, and IMDb's licence does not allow them
        to be passed on. A column declaring them is refused like a TMDB one."""
        with tempfile.TemporaryDirectory() as out:
            writer = self.mutated(out, 'mod.PROVENANCE["released"] = "imdb"')
            with self.assertRaises(AssertionError) as caught:
                self.build(out, build_store=writer)
            self.assertFalse(os.path.exists(os.path.join(out, "test.store")))
        message = str(caught.exception)
        self.assertIn("released", message)
        self.assertIn("VENDOR_ALLOWED", message)

    def test_an_allowlist_entry_for_a_column_that_is_gone_stops_the_build(self):
        """So removing a vendor column is two deliberate edits — the PROVENANCE entry and the allowlist
        entry — rather than one that leaves a permission behind. The allowlist is empty now, so the
        case is made by putting an entry back for a section that no longer exists."""
        with tempfile.TemporaryDirectory() as out:
            writer = self.mutated(out, 'mod.VENDOR_ALLOWED = {"votes"}')
            with self.assertRaises(AssertionError) as caught:
                self.build(out, build_store=writer)
        message = str(caught.exception)
        self.assertIn("votes", message)
        self.assertIn("allowlist", message)


class ProseCannotEnterTheDictionary(StoreFixture, unittest.TestCase):
    """The backstop on `strings`.

    Every other section is a number, an id, or an offset into this one, so the string dictionary is the
    only way prose can reach the store — and a bound on its longest entry is a bound on the whole
    artifact. It is a BACKSTOP, not the guard: `check_provenance` is what stops a new column, and a
    short vendor string (a ~40-character tagline) would pass this unremarked.
    """

    def test_a_dictionary_entry_longer_than_the_bound_stops_the_build(self):
        """An overview-shaped string arriving through a section that already exists — here `alias_titles`,
        which interns whatever `facts.titles` holds."""
        overview = "A sweeping account of " + "a very long sentence about the picture " * 8
        self.assertGreater(len(overview.encode()), build_store_module().MAX_STRING_BYTES)
        titles = [self.TITLES[0], dict(self.TITLES[1], facts={"titles": {"en": overview}})]
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(AssertionError) as caught:
                self.build(out, titles)
            self.assertFalse(os.path.exists(os.path.join(out, "test.store")))
        message = str(caught.exception)
        self.assertIn("longest entry", message)
        self.assertIn("260", message)

    def test_the_bound_clears_the_longest_real_name_and_refuses_an_overview(self):
        """445,817 strings in the shipped store, the longest 217 bytes — a performer's full name — and
        only 34 over 120. The bound has 43 bytes of headroom above the real corpus and is two orders of
        magnitude below an enriched overview, whose median is 3,321 characters."""
        mod = build_store_module()
        self.assertIsNone(mod.prose_in_the_dictionary(["x" * 217]))
        self.assertIsNone(mod.prose_in_the_dictionary([]))
        self.assertIsNotNone(mod.prose_in_the_dictionary(["x" * 261]))

    def test_the_bound_is_in_bytes_not_characters(self):
        """`str_off` indexes the UTF-8 blob, so the bound has to be measured the way the store stores
        it — 131 accented characters are 262 bytes and must not read as comfortably short."""
        mod = build_store_module()
        self.assertIsNotNone(mod.prose_in_the_dictionary(["é" * 131]))


class ThePosterPathIsNotPublished(StoreFixture, unittest.TestCase):
    """`card_poster` held a TMDB poster path for 47,534 rows of a public release asset.

    It is the one card column with no replacement at all: den-edge's `/metadata/title/query` batches
    posters 100 titles to a request, and den-atlas's Stremio metas already carry a metahub `poster` URL
    beside `posterPath`. `card_title` and `card_year` still ship, from Wikidata now.
    """

    def test_the_store_has_no_poster_section(self):
        mod = build_store_module()
        with tempfile.TemporaryDirectory() as out:
            store, _ = self.build(out)
        self.assertNotIn("card_poster", store.table)
        self.assertNotIn("card_poster", mod.PROVENANCE)
        self.assertNotIn("card_poster", mod.VENDOR_ALLOWED)
        self.assertIn("card_title", store.table, "the other two card columns are not this change's")
        self.assertIn("card_year", store.table)

    def test_no_artwork_reference_reaches_the_dictionary(self):
        """Dropping a section while still interning its strings leaves them in the published bytes,
        reachable by anyone who reads the dictionary — which is redistribution just the same. The
        writer takes no poster input at all now, so this watches the dictionary itself rather than one
        removed argument: any `/…jpg` here would mean a path arrived through some other field."""
        with tempfile.TemporaryDirectory() as out:
            store, _ = self.build(out)
        interned = {store.text(i) for i in range(len(store.str_off) - 1)}
        paths = [s for s in interned if s and s.startswith("/") and s.endswith(".jpg")]
        self.assertEqual(paths, [], "an artwork path is in the store's string table")
        self.assertIn("Alpha", interned, "the title still ships; only the artwork reference is gone")


class EverySeriesATitleIsPartOfShips(StoreFixture, unittest.TestCase):
    """`franchise` is a list (store-v2): every series the facts name, in the facts' order.

    store-v1 held one Q-id per title, the first. 219 titles are in more than one real series, and some only
    meet their siblings through the second — The Batman, Spider-Man: Brand New Day, The Hobbit
    (oxyc/den-atlas#43). The facts stage already orders them most specific first, so the writer must keep
    that order rather than sort it.
    """

    def test_the_list_keeps_the_facts_order_and_every_series(self):
        titles = [
            dict(self.TITLES[0], facts=dict(self.TITLES[0]["facts"], franchise=["Q30", "Q20", "Q30", "L5"])),
            # The older single-valued shape: a bare Q-id is a list of one.
            dict(self.TITLES[1], facts=dict(self.TITLES[1]["facts"], franchise="Q40")),
            {"key": "movie:3", "mediaType": "movie", "tmdbId": 3,
             "facts": {"titles": {"en": "Gamma"}}, "labels": None, "premiseLabels": None},
        ]
        with tempfile.TemporaryDirectory() as out:
            store, _ = self.build(out, titles=titles)
        offsets, values = store.ints("franchise_o"), store.ints("franchise_v")
        got = {key: values[offsets[row]:offsets[row + 1]] for row, key in enumerate(store.keys())}
        self.assertEqual(got, {"movie:1": [30, 20], "movie:2": [40], "movie:3": []},
                         "most specific first, deduplicated, a non-Q-id dropped, none as an empty span")
        self.assertNotIn("franchise", store.table, "the single-valued store-v1 column is gone")
        self.assertEqual(struct.unpack("<I", store.blob[8:12])[0], 2, "a list franchise is format 2")


class OnlyATitleIdReachesTheImdbColumn(unittest.TestCase):
    """IMDb's id space is namespaced by prefix and Wikidata's P345 is not checked against it.

    Two corpus rows carry an id from the wrong namespace — `ev0000003` (an event) and `nm19818812` (a
    person) — and stored verbatim they read as title ids. den-atlas now joins IMDb's public
    `title.ratings` on this column to rank browse rows, so a wrong-namespace id is a row that can never
    match and cannot be told apart from a title IMDb has no rating for.
    """

    def test_a_person_or_event_id_is_not_a_title_id(self):
        mod = build_store_module()
        for wrong in ("nm19818812", "ev0000003", "co0000123", "tt", "ttabc", "", "  ", None, 12345):
            self.assertIsNone(mod.title_imdb_id(wrong), f"{wrong!r} was accepted as a title id")

    def test_a_title_id_survives_however_it_is_wrapped(self):
        mod = build_store_module()
        self.assertEqual(mod.title_imdb_id("tt0000001"), "tt0000001")
        self.assertEqual(mod.title_imdb_id(["tt0111161", "tt0000002"]), "tt0111161",
                         "a list keeps its first entry, as the writer always has")
        self.assertEqual(mod.title_imdb_id(" tt0111161 "), "tt0111161")
        self.assertIsNone(mod.title_imdb_id([]))


class TheCardIsNamedAndDatedFromWikidata(unittest.TestCase):
    """`card_title` and `card_year` came from the TMDB metadata sidecar; they come from `facts` now.

    Measured against the sidecar over the whole corpus before the swap: the title is exact for 88.86%,
    the year agrees for 92.56%, and neither residual is a wrong answer — the titles are the other English
    name a work goes by, and the years differ by one where Wikidata dates the festival premiere.
    """

    def test_the_english_label_is_preferred_then_the_original_then_an_alias(self):
        mod = build_store_module()
        self.assertEqual(mod.display_title({"en": "Heat", "orig": "Heat"}), "Heat")
        self.assertEqual(mod.display_title({"orig": "Alfa", "aliases": ["Alpha One"]}), "Alfa")
        self.assertEqual(mod.display_title({"aliases": ["Alpha One"]}), "Alpha One")
        self.assertIsNone(mod.display_title({}), "no free name is not a name")
        self.assertIsNone(mod.display_title(None))
        self.assertIsNone(mod.display_title({"en": "   "}), "whitespace is not a name")

    def test_a_wikipedia_disambiguator_is_stripped_and_a_real_parenthesis_is_not(self):
        """The vocabulary exists because "strip any trailing (...)" would damage real names — TMDB
        agrees with Wikidata that these four end the way they do."""
        mod = build_store_module()
        for label, want in (
            ("Nausicaä of the Valley of the Wind (film)", "Nausicaä of the Valley of the Wind"),
            ("Gamma (TV series)", "Gamma"),
            ("Batman (serial)", "Batman"),
            ("Die Feuerzangenbowle (1944 film)", "Die Feuerzangenbowle"),
            ("Der Hauptmann (2017)", "Der Hauptmann"),
            ("Live in Texas (Linkin Park album)", "Live in Texas"),
            ("Tatort (Fernsehserie)", "Tatort"),
        ):
            self.assertEqual(mod.display_title({"en": label}), want)
        for real in ("South Park (Not Suitable for Children)", "To Have (Or Not)", "Frontier(s)",
                     "Everything You Always Wanted to Know About Sex* (*But Were Afraid to Ask)"):
            self.assertEqual(mod.display_title({"en": real}), real, "a real name lost its ending")

    def test_the_year_comes_off_the_date_text(self):
        mod = build_store_module()
        self.assertEqual(mod.release_year({"date": "1999-12-31", "precision": "day"}), 1999)
        self.assertEqual(mod.release_year({"date": "2014", "precision": "year"}), 2014,
                         "a year-precision fact still asserts its year")
        self.assertEqual(mod.release_year({"date": "-0044-03-15"}), -44, "BCE stays negative")
        for nothing in (None, {}, {"date": ""}, {"date": "not a date"}, "1999"):
            self.assertIsNone(mod.release_year(nothing))


class TheCardReadsBackFromTheCorpus(StoreFixture, unittest.TestCase):
    """End to end on a real store, because the helpers above can pass while nothing calls them.

    An earlier attempt to read the card off `facts` left `card_year` at its sentinel on all 47,618 rows
    and every structural check passed, so this asserts the VALUES rather than the shape.
    """

    def test_the_name_and_year_come_from_facts_and_nothing_else_supplies_them(self):
        titles = [
            dict(self.TITLES[0], facts=dict(
                self.TITLES[0]["facts"],
                titles={"en": "Alpha (1999 film)", "orig": "Alfa"},
                released={"date": "1999-12-31", "precision": "day"})),
            dict(self.TITLES[1], facts=dict(
                self.TITLES[1]["facts"], started={"date": "2014", "precision": "year"})),
        ]
        with tempfile.TemporaryDirectory() as out:
            store, _ = self.build(out, titles=titles)
        names = [store.text(i) for i in store.ints("card_title")]
        years = store.ints("card_year", "h", 2)
        self.assertEqual(names, ["Alpha", "Beta"], "the disambiguator is off the card")
        self.assertEqual(years, [1999, 2014], "a year-precision `started` still dates the card")
        interned = {store.text(i) for i in range(len(store.str_off) - 1)}
        self.assertIn("Alpha (1999 film)", interned, "search still matches the full label")

    def test_a_row_with_no_free_name_gets_no_card(self):
        """Five real rows have an empty `facts.titles`. They lose their card and drop out of browse and
        search, which is the honest outcome: there is no name we are allowed to publish."""
        nameless = {"key": "movie:3", "mediaType": "movie", "tmdbId": 3,
                    "facts": {"titles": {}}, "labels": None, "premiseLabels": None}
        with tempfile.TemporaryDirectory() as out:
            store, _ = self.build(out, titles=self.TITLES + [nameless])
        self.assertEqual([store.text(i) for i in store.ints("card_title")], ["Alpha", "Beta", None])


class TheAliasDecisionsAreApplied(StoreFixture, unittest.TestCase):
    """`data/alias-decisions.json` takes effect in the store, and the store records how many colliding
    aliases it ships undecided — the number `check-alias-collisions.py --gate` refuses a publish on.

    Run against the committed file, whose `drop` list takes "Alien" off Taxi Driver (movie:103). Wikidata
    still carries that altLabel, so a fresh scrape brings it back; the store is where every path ends.
    """

    DECISIONS = os.path.join(HERE, "..", "..", "data", "alias-decisions.json")

    def titles(self):
        def titled(key, names):
            media, tmdb_id = key.split(":")
            return {"key": key, "mediaType": media, "tmdbId": int(tmdb_id), "facts": {"titles": names},
                    "labels": None, "premiseLabels": None}
        return self.TITLES + [
            titled("movie:103", {"en": "Taxi Driver", "aliases": ["Alien"]}),
            titled("movie:348", {"en": "Alien"}),
            # Another title's name, and the decisions file says nothing about it.
            titled("movie:9", {"en": "Gamma", "aliases": ["Beta"]}),
        ]

    def test_a_dropped_alias_does_not_ship_and_an_undecided_one_is_counted(self):
        with tempfile.TemporaryDirectory() as out:
            meta = os.path.join(out, "dataset.meta.json")
            with open(meta, "w") as fh:
                json.dump({"datasetVersion": "test"}, fh)
            store, _ = self.build(out, titles=self.titles(), stamp=meta)
            with open(meta) as fh:
                record = json.load(fh)["aliasDecisions"]

        offsets, ids = store.ints("alias_titles_o"), store.ints("alias_titles_v")

        def names(key):
            row = store.keys().index(key)
            return [store.text(i) for i in ids[offsets[row]:offsets[row + 1]]]

        self.assertEqual(names("movie:103"), ["Taxi Driver"], "the dropped alias reached alias_titles")
        self.assertEqual(names("movie:9"), ["Gamma", "Beta"], "an undecided alias still ships until decided")
        self.assertEqual(record, {"sha256": sha256(self.DECISIONS), "dropped": 1, "undecided": 1})
