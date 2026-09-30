"""The publish guard against silently losing optional store sections."""
import json
import os
import tempfile
import unittest

from pipeline import check_store_sections as guard
from store import format as store_format


def write_store(path, names):
    sec = store_format.Sections(1)
    for name in names:
        sec.put_raw(name, b"x", 1, expect=1)
    store_format.write(path, sec, 1, "fixture")


class StoreSections(unittest.TestCase):
    def test_a_candidate_may_add_but_not_drop_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            old = os.path.join(directory, "old.store")
            new = os.path.join(directory, "new.store")
            meta = os.path.join(directory, "meta.json")
            write_store(old, ["keys", "fan_picks_v", "fan_picks_o", "fan_picks_a"])
            write_store(new, ["keys"])
            with open(meta, "w", encoding="utf-8") as handle:
                json.dump({}, handle)
            self.assertEqual(guard.main([meta, new, "--published-store", old]), 1)
            write_store(new, ["keys", "fan_picks_v", "fan_picks_o", "fan_picks_a", "new_section"])
            self.assertEqual(guard.main([meta, new, "--published-store", old]), 0)

    def test_a_stamped_manifest_needs_no_downloaded_store(self):
        with tempfile.TemporaryDirectory() as directory:
            new = os.path.join(directory, "new.store")
            meta = os.path.join(directory, "meta.json")
            write_store(new, ["keys"])
            with open(meta, "w", encoding="utf-8") as handle:
                json.dump({"storeSections": ["keys", "fan_picks_a"]}, handle)
            self.assertEqual(guard.main([meta, new]), 1)


if __name__ == "__main__":
    unittest.main()
