#!/usr/bin/env python3
"""`prune-manifest.py` — the rule that decides what `data-latest` still declares."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    """The script, whose filename is not an importable module name."""
    spec = importlib.util.spec_from_file_location("prune_manifest",
                                                  os.path.join(HERE, "prune-manifest.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pm = load()

# The manifest as `data-latest` carried it on 2026-09-20, trimmed to its keys. Every one of these was live.
LIVE = {
    "builtAt": "2026-09-20T11:24:16Z",
    "count": 47539,
    "datasetVersion": "5b1c3213b6a1",
    "dims": 1024,
    "embedderMaxTokens": 1024,
    "embedderRuntime": "den-embed/5.1.2",
    "embeddingModel": "bge-m3",
    "facetsBytes": 577988,
    "facetsFile": "facets.bin",
    "facetsRecords": 38532,
    "facetsSha256": "f6",
    "labelsBytes": 11799722,
    "labelsFile": "labels-t02.json",
    "labelsGzFile": "labels-t02.json.gz",
    "labelsRecords": 47539,
    "labelsSha256": "1d",
    "lastModifiedHttp": "Sun, 20 Sep 2026 11:24:16 GMT",
    "maxBatchId": 199,
    "metadataBytes": 5933843,
    "metadataFile": "metadata-5b1c3213b6a1.json",
    "metadataGzFile": "metadata-5b1c3213b6a1.json.gz",
    "metadataRecords": 47539,
    "metadataSha256": "d2",
    "premiseCount": 44531,
    "premiseDims": 1024,
    "premiseEmbeddingModel": "bge-m3-premise",
    "premiseLabelsBytes": 16352321,
    "premiseLabelsFile": "labels-premise.json",
    "premiseLabelsGzFile": "labels-premise.json.gz",
    "premiseLabelsRecords": 44531,
    "premiseLabelsSha256": "74",
    "premiseVectorsBytes": 45599752,
    "premiseVectorsFile": "vectors-premise.bin",
    "premiseVectorsSha256": "1c",
    "quantization": "int8-symmetric-x127",
    "storeBytes": 135146098,
    "storeFile": "den-5b1c3213b6a1.store",
    "storeRecords": 47618,
    "storeSha256": "ca",
    "taxonomyVersion": "t02",
    "vectorsBytes": 48679944,
    "vectorsFile": "vectors-bge-m3.bin",
    "vectorsSha256": "e1",
}

# What has to survive: the descriptor den-atlas serves, and the one artifact it reads.
#
# `count` is deliberately NOT here. It counted the titles the LABELS artifact carried (47,539), never the
# store's rows (47,618), and atlas served it to the app as though it described the corpus. Its premise twin
# `premiseCount` was retired for describing nothing; keeping the plot one would reinstate the same unowned
# number on the other side. `storeRecords` is the row count, and unlike `count` it is checked.
KEPT = {
    "builtAt", "datasetVersion", "dims", "embedderMaxTokens", "embedderRuntime",
    "embeddingModel", "lastModifiedHttp", "maxBatchId", "quantization", "storeBytes", "storeFile",
    "storeRecords", "storeSha256", "taxonomyVersion",
}


def pruned(meta):
    """`main()` in `--prune` mode over this manifest, returning what it left on disk."""
    with tempfile.TemporaryDirectory() as dir:
        path = os.path.join(dir, "dataset.meta.json")
        with open(path, "w") as fh:
            json.dump(meta, fh)
        old = sys.argv
        sys.argv = ["prune-manifest.py", "--prune", path]
        try:
            pm.main()
        finally:
            sys.argv = old
        with open(path) as fh:
            return json.load(fh)


class PruneManifest(unittest.TestCase):
    def test_the_live_manifest_prunes_to_the_store_and_the_descriptor(self):
        self.assertEqual(set(pruned(LIVE)), KEPT)

    def test_the_stores_own_keys_survive(self):
        after = pruned(LIVE)
        for key in ("storeFile", "storeSha256", "storeBytes", "storeRecords"):
            self.assertEqual(after[key], LIVE[key])

    def test_nothing_describes_a_file_the_manifest_does_not_name(self):
        """The failure the `--consistent` guard catches: a `<x>Sha256` outliving its `<x>File`.

        A prune that dropped `labelsFile` and left `labelsSha256` behind would publish a manifest that
        contradicts itself — and `manifest-counts.py --consistent` would refuse the publish for it.
        """
        after = pruned(LIVE)
        named = {k[: -len("File")] for k in after if k.endswith("File")}
        for key in after:
            for suffix in ("Sha256", "Bytes", "Records"):
                if key.endswith(suffix):
                    self.assertIn(key[: -len(suffix)], named, f"{key} describes a file nothing names")

    def test_a_key_no_producer_has_invented_yet_is_dropped_by_default(self):
        """A keep-list, so the miss is a file that is not published rather than one that is unguarded."""
        after = pruned({**LIVE, "railFacetsFile": "rail-facets-5b1c3213b6a1.json", "railFacetsBytes": 1})
        self.assertEqual(set(after), KEPT)

    def test_a_gz_twin_of_the_store_is_dropped(self):
        """atlas mmaps the store, and a compressed file cannot be mapped."""
        self.assertIn("storeGzFile", pm.retired({**LIVE, "storeGzFile": "den-x.store.gz"}))

    def test_retired_lists_exactly_what_prune_removes(self):
        """The publisher reads `--retired` to decide which dropped keys are deliberate, so the two
        answers must be the same one."""
        self.assertEqual(sorted(pm.retired(LIVE)), sorted(set(LIVE) - set(pruned(LIVE))))

    def test_the_stores_record_of_its_inputs_survives(self):
        """`storeInputs` is `build_store.py`'s record of what it read, and with the store's inputs no
        longer declared it is the ONLY thing holding them to a producer. The suffix rule drops anything
        shaped like a per-blob claim, so a key named `storeInputsFile` — or a rename to something ending
        in Records — would prune it away and reopen oxyc/den#113's gap with no error anywhere."""
        record = [{"arg": "corpus", "path": "out/corpus-5b1c3213b6a1.jsonl.gz",
                   "sha256": "ab", "bytes": 1, "mtime": 2}]
        after = pruned({**LIVE, "storeInputs": record})
        self.assertEqual(after.get("storeInputs"), record)

    def test_an_already_pruned_manifest_is_unchanged(self):
        """Every publish prunes. The second one must be a no-op, not a slow erosion of the descriptor."""
        once = pruned(LIVE)
        self.assertEqual(pruned(once), once)


if __name__ == "__main__":
    unittest.main()
