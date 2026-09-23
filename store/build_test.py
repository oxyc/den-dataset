#!/usr/bin/env python3
"""The section table's ORDER, which no other check can see.

`check_provenance` compares the written sections against `PROVENANCE` as a SET, and it has to: it is
asking whether every column declares a source, not where it sits. But a section's position in the table
is its position in the file, so the order the groups are asked to write in is part of the format —
`applic_v` and `applic_c` land between `facts_has_vec` and `runtime`, which no reading of `facets.py`
or `facts.py` alone would predict.

That order used to be self-evident from one function emitting every section in sequence. It is now
spread across nine modules, so this holds it against `PROVENANCE`, whose comment has always claimed to
list the sections "in the order the writer emits them" — a claim nothing checked.

den-spec's committed fixture pins the same thing byte-for-byte, and more strongly. It pins it from
another repo, though, and skips when that repo is absent.
"""
import importlib.util
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE = os.path.join(REPO, "pipeline")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


writer = load("build_store", os.path.join(PIPELINE, "build_store.py"))
# The two-title corpus `build_store_test.py` already defines, reused rather than rebuilt: it is shaped
# to fill every list section the writer refuses to ship empty, and a second definition of "a valid
# corpus" is the drift this store's guards exist to prevent.
fixture = load("build_store_test", os.path.join(PIPELINE, "build_store_test.py"))


class SectionOrder(fixture.StoreFixture, unittest.TestCase):
    def test_the_sections_are_written_in_the_order_the_table_declares(self):
        with tempfile.TemporaryDirectory() as out:
            store, _ = self.build(out)
        self.assertEqual(
            list(store.table), list(writer.PROVENANCE),
            "the writer's section order and PROVENANCE's have diverged. The table is the file layout, "
            "so this is a format change: either a group is emitting its sections in the wrong place, "
            "or the declaration needs to be reordered to match and den-spec's fixture regenerated.")


if __name__ == "__main__":
    unittest.main()
