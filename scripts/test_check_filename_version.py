"""`check-filename-version.py` — refuse a blob from a dead `datasetVersion`.

The failure it exists for is silent by construction: the file is there, its sha matches, its record count
has not moved (because nothing rebuilt it) and its producer is registered. Only the NAME says anything.
"""
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location(
        "check_filename_version", os.path.join(HERE, "check-filename-version.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cfv = load()
CURRENT = "5b1c3213b6a1"
DEAD = "c85c707b0b18"


class Stale(unittest.TestCase):
    def test_the_real_case_that_was_live(self):
        """Exactly what the published manifest held when this was written: everything at the current
        generation except plot-facets, frozen a generation back and still being served."""
        meta = {
            "datasetVersion": CURRENT,
            "factsFile": f"facts-{CURRENT}.json",
            "storeFile": f"den-{CURRENT}.store",
            "plotFacetsFile": f"plot-facets-{DEAD}.json",
            "plotFacetsGzFile": f"plot-facets-{DEAD}.json.gz",
        }
        found = cfv.stale(meta)
        self.assertEqual([k for k, _, _ in found], ["plotFacetsFile", "plotFacetsGzFile"])
        self.assertTrue(all(v == DEAD for _, _, v in found))

    def test_a_clean_manifest_passes(self):
        meta = {
            "datasetVersion": CURRENT,
            "factsFile": f"facts-{CURRENT}.json",
            "factsGzFile": f"facts-{CURRENT}.json.gz",
            "storeFile": f"den-{CURRENT}.store",
        }
        self.assertEqual(cfv.stale(meta), [])

    def test_the_unversioned_blobs_are_exempt_and_the_exemption_is_not_a_blanket(self):
        """Names carrying no version are unversioned by design — the taxonomy and model names, not a
        generation. The exemption must come from the NAME having no version, not from a key list, or a
        stale `plot-facets-<dead>.json` would be waved through by the same rule."""
        meta = {
            "datasetVersion": CURRENT,
            "labelsFile": "labels-t02.json",
            "labelsGzFile": "labels-t02.json.gz",
            "premiseLabelsFile": "labels-premise.json",
            "vectorsFile": "vectors-bge-m3.bin",
            "premiseVectorsFile": "vectors-premise.bin",
            "facetsFile": "facets.bin",
        }
        self.assertEqual(cfv.stale(meta), [])
        # Same keys, now carrying a dead version: they must NOT be exempt.
        meta["labelsFile"] = f"labels-{DEAD}.json"
        self.assertEqual([k for k, _, _ in cfv.stale(meta)], ["labelsFile"])

    def test_a_hex_run_that_is_not_a_version_is_not_matched(self):
        """The pattern is `-<12 hex>` before a suffix, the shape the producer emits. A looser search for
        any 12-hex run would fire on a filename that merely contains one."""
        meta = {"datasetVersion": CURRENT, "factsFile": "facts-abcdefabcdef12-extra.json"}
        self.assertEqual(cfv.stale(meta), [])

    def test_a_manifest_with_no_version_is_refused(self):
        self.assertEqual(cfv.stale({"factsFile": "facts-x.json"})[0][0], "datasetVersion")

    def test_main_exits_nonzero_on_a_stale_blob(self):
        with tempfile.TemporaryDirectory() as dir:
            path = os.path.join(dir, "meta.json")
            with open(path, "w") as fh:
                json.dump({"datasetVersion": CURRENT, "plotFacetsFile": f"plot-facets-{DEAD}.json"}, fh)
            import sys
            old = sys.argv
            sys.argv = ["check-filename-version.py", path]
            try:
                self.assertEqual(cfv.main(), 1)
            finally:
                sys.argv = old


if __name__ == "__main__":
    unittest.main()
