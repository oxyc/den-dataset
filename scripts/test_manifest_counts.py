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
            if os.environ.get("DEN_SPEC_OPTIONAL"):
                self.skipTest("den-spec absent and DEN_SPEC_OPTIONAL is set")
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
            published = json.load(open(meta))
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


if __name__ == "__main__":
    unittest.main()
