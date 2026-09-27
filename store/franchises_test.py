import json
import os
import tempfile
import unittest

from .format import Sections, Strings, U32_NONE
from .franchises import Franchises


def document():
    return {
        "schema": 2,
        "datasetVersion": "test",
        "franchises": {
            "Qbeck": {
                "id": "Qbeck", "name": "Beck", "confidence": 0.85, "source": "jev-franchise-v1",
                "eras": [
                    {"id": "Qbeck:era:old", "name": "Older films", "order": 0,
                     "members": ["movie:2"]},
                    {"id": "Qbeck:era:tv", "name": "TV run", "order": 1,
                     "members": ["tv:3"]},
                ],
                "members": [
                    {"key": "movie:2", "eraId": "Qbeck:era:old", "order": 0},
                    {"key": "tv:3", "eraId": "Qbeck:era:tv", "order": 1},
                ],
            }
        },
        "titles": {
            "movie:2": {"primary": "Qbeck"},
            "tv:3": {"primary": "Qbeck", "umbrella": {"id": "Qcrime", "name": "Nordic crime"}},
        },
    }


class CuratedFranchises(unittest.TestCase):
    def read(self, blob=None, keys=("movie:1", "movie:2", "tv:3")):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "franchises.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(document() if blob is None else blob, fh)
        return Franchises(path, list(keys), "test")

    def test_mixed_members_primary_umbrella_eras_and_order_reach_the_sections(self):
        got = self.read()
        strings = Strings()
        got.intern(strings)
        ordered = strings.freeze()
        sec = Sections(3)
        got.put(sec, strings)
        text = lambda ident: None if ident == U32_NONE else ordered[ident]
        self.assertEqual(got.primary, [U32_NONE, 0, 0])
        self.assertEqual([text(i) for i in _ints(sec, "fr_id")], ["Qbeck"])
        self.assertEqual([text(i) for i in _ints(sec, "fr_umb_id")], [None, None, "Qcrime"])
        self.assertEqual(_ints(sec, "fr_conf", "B"), [85])
        self.assertEqual(_ints(sec, "fr_mem_row"), [1, 2], "movie and TV are corpus rows, in release order")
        self.assertEqual(_ints(sec, "fr_mem_era"), [0, 1])
        self.assertEqual(_ints(sec, "fr_mem_order"), [0, 1])

    def test_absent_input_writes_no_sections(self):
        got = Franchises(None, ["movie:1"], "test")
        strings, sec = Strings(), Sections(1)
        strings.freeze()
        got.put(sec, strings)
        self.assertEqual(sec.order, [])

    def test_member_and_title_indexes_must_agree(self):
        blob = document()
        del blob["titles"]["tv:3"]
        with self.assertRaisesRegex(SystemExit, "titles index differs"):
            self.read(blob)

    def test_order_and_confidence_are_wire_exact(self):
        blob = document()
        blob["franchises"]["Qbeck"]["members"][1]["order"] = 3
        with self.assertRaisesRegex(SystemExit, "order must be 1"):
            self.read(blob)
        blob = document()
        blob["franchises"]["Qbeck"]["confidence"] = 0.855
        with self.assertRaisesRegex(SystemExit, "more than two decimals"):
            self.read(blob)


def _ints(sec, name, fmt="I"):
    import struct
    width = struct.calcsize(fmt)
    block = sec.blocks[name]
    return list(struct.unpack(f"<{len(block) // width}{fmt}", block))


if __name__ == "__main__":
    unittest.main()
