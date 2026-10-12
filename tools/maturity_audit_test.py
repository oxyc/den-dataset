import json
import os
import tempfile
import unittest

from . import maturity_audit


def observation(authority, age, ident, **extra):
    return {"authority": authority, "authorityId": ident, "raw": str(age), "age": age,
            "matchedBy": "exact-title+year+director", **extra}


def record(ident, observations, media="movie"):
    return {"mediaType": media, "tmdbId": ident,
            "minimumAge": max(row["age"] for row in observations), "observations": observations}


class Audit(unittest.TestCase):
    def write(self, directory, rows):
        path = os.path.join(directory, "maturity.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "datasetVersion": "v1", "records": rows}, handle)
        return path

    def test_includes_every_conflict_and_samples_each_authority_deterministically(self):
        rows = [
            record(1, [observation("CNC", 12, "visa:1"), observation("KAVI", 16, "iklu:1")]),
            record(2, [observation("CNC", 12, "visa:2")]),
            record(3, [observation("CNC", 16, "visa:3")]),
            record(4, [observation("KAVI", 7, "iklu:4")]),
            record(5, [observation("Medietilsynet", 9, "xlsx:5")], media="tv"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, rows)
            first = maturity_audit.build(path, per_authority=1, seed="fixed")
            second = maturity_audit.build(path, per_authority=1, seed="fixed")
        self.assertEqual(first, second)
        self.assertEqual([row["tmdbId"] for row in first["conflicts"]], [1])
        self.assertEqual(first["summary"]["sampledForAuthority"],
                         {"CNC": 1, "KAVI": 1, "Medietilsynet": 1})
        self.assertNotIn(1, [row["tmdbId"] for row in first["samples"]])
        self.assertEqual(first["sourceSha256"], second["sourceSha256"])

    def test_copies_only_factual_audit_fields_and_drops_unexpected_prose(self):
        rows = [record(1, [observation("CNC", 12, "visa:1", reason="do not copy",
                                               synopsis="do not copy")])]
        with tempfile.TemporaryDirectory() as directory:
            report = maturity_audit.build(self.write(directory, rows), per_authority=1)
        body = json.dumps(report)
        self.assertNotIn("do not copy", body)
        self.assertIn("visa:1", body)

    def test_refuses_duplicate_malformed_and_non_conservative_records(self):
        valid = record(1, [observation("CNC", 12, "visa:1")])
        cases = [
            [valid, valid],
            ["not a record"],
            [{**valid, "minimumAge": 0}],
            [{**valid, "observations": [{**valid["observations"][0], "age": True}]}],
            [{**valid, "tmdbId": True}],
            [{**valid, "tmdbId": 0}],
            [{**valid, "observations": ["not an observation"]}],
        ]
        with tempfile.TemporaryDirectory() as directory:
            for rows in cases:
                with self.subTest(rows=rows), self.assertRaises(ValueError):
                    maturity_audit.build(self.write(directory, rows))


if __name__ == "__main__":
    unittest.main()
