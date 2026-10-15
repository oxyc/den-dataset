import datetime
import hashlib
import json
import os
import tarfile
import tempfile
import unittest

from . import artifacts, maturity, maturity_audit
from .contract import Context, StageError
from .maturity_test import facts, movie, payload, source_row


VERSION = "auditversion"


def write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle)


class MaturityAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = self.tmp.name
        self.facts = facts(movie(1))
        write(os.path.join(self.out, artifacts.MANIFEST.filename), {"datasetVersion": VERSION})
        write(os.path.join(self.out, artifacts.FACTS.filename.format(version=VERSION)), self.facts)
        self.snapshot = payload(source_row())
        maturity.run(Context(out_dir=self.out),
                     fetchers={"Medietilsynet": lambda: self.snapshot},
                     now=datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc))
        self.archive = os.path.join(self.out, maturity_audit.ASSET)

    def test_archive_rebuilds_the_exact_observations_from_retained_inputs(self):
        record = maturity_audit.build(self.out, self.archive, VERSION)
        facts_payload = json.dumps(self.facts).encode()
        self.assertEqual(maturity_audit.verify(self.archive, facts_payload, VERSION), record)
        with tarfile.open(self.archive, "r:gz") as archive:
            names = set(archive.getnames())
        self.assertEqual(names, {"audit.json", "maturity.json", "maturity-sources.json",
                                 "maturity-authority-decisions.json",
                                 "sources/medietilsynet-1913-2022.xlsx"})

    def test_archive_is_deterministic_and_one_release_asset(self):
        maturity_audit.build(self.out, self.archive, VERSION)
        with open(self.archive, "rb") as handle:
            first = hashlib.sha256(handle.read()).hexdigest()
        second_path = os.path.join(self.out, "second.tar.gz")
        maturity_audit.build(self.out, second_path, VERSION)
        with open(second_path, "rb") as handle:
            second = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(first, second)

    def test_changed_snapshot_is_refused_before_publication(self):
        with open(os.path.join(self.out, artifacts.MATURITY_NORWAY.filename), "ab") as handle:
            handle.write(b"changed")
        with self.assertRaisesRegex(StageError, "snapshot does not match maturity-sources.json"):
            maturity_audit.build(self.out, self.archive, VERSION)

    def test_changed_derived_observation_is_refused_by_rebuild(self):
        derived_path = os.path.join(self.out, artifacts.MATURITY.filename.format(version=VERSION))
        with open(derived_path, encoding="utf-8") as handle:
            derived = json.load(handle)
        derived["records"][0]["minimumAge"] = 18
        write(derived_path, derived)
        with self.assertRaisesRegex(StageError, "do not reproduce maturity.json observations"):
            maturity_audit.build(self.out, self.archive, VERSION)


if __name__ == "__main__":
    unittest.main()
