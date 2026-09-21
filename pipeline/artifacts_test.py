#!/usr/bin/env python3
"""The artifact catalogue — one entry per file, and nothing in it that nothing reaches."""
import unittest

import pipeline

from . import artifacts


class Catalogue(unittest.TestCase):
    def test_every_entry_is_named_by_a_stage(self):
        """The attrition rule, applied to declarations rather than to files.

        An entry no stage reads or writes is a producer registration for an artifact this pipeline does
        not handle — `check-producers.py` would go on asking after it, and the answer would go on being
        true and meaningless. That is `facets.bin` again: an artifact with an owner on paper, nothing
        rebuilding it, and 999 titles of drift before anyone looked.
        """
        named = {a.name for a in pipeline.declared()}
        orphans = sorted(a.name for a in artifacts.CATALOGUE if a.name not in named)
        self.assertEqual(orphans, [], f"in the catalogue and reached by no stage: {orphans}. Delete the "
                                      f"entry, or declare it in the stage that reads or writes it.")

    def test_every_stage_declaration_comes_from_the_catalogue(self):
        """The other direction: a stage inventing an Artifact inline puts a second declaration of the
        same file one import away from the first."""
        stray = sorted(a.name for a in pipeline.declared() if a not in artifacts.CATALOGUE)
        self.assertEqual(stray, [], f"declared by a stage and not in the catalogue: {stray}")

    def test_names_and_filenames_are_unique(self):
        names = [a.name for a in artifacts.CATALOGUE]
        filenames = [a.filename for a in artifacts.CATALOGUE]
        self.assertCountEqual(names, set(names))
        self.assertCountEqual(filenames, set(filenames))

    def test_a_manifest_key_is_claimed_once(self):
        """Two artifacts claiming one manifest key would make the derived producer registry depend on
        catalogue order."""
        keys = [a.manifest_key for a in artifacts.CATALOGUE if a.manifest_key]
        self.assertCountEqual(keys, set(keys))


if __name__ == "__main__":
    unittest.main()
