import json
import os
import tempfile
import unittest

from pipeline import maturity_italy, maturity_italy_historical, maturity_taiwan
from pipeline.maturity_test import italy_historical_payload, italy_snapshot
from . import bamid_review, maturity_review


def source(rows):
    header = ",".join(maturity_taiwan.HEADERS) + "\n"
    return maturity_taiwan._zip([("113.csv", (header + "".join(rows)).encode())])


class Review(unittest.TestCase):
    def write(self, directory, name, payload, binary=False):
        path = os.path.join(directory, name)
        with open(path, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8"})) as handle:
            handle.write(payload if binary else json.dumps(payload))
        return path

    def test_selects_only_unique_exact_title_matches_and_excludes_decisions(self):
        facts = {"records": [
            {"mediaType": "movie", "tmdbId": 1, "titles": {"en": "Anyone but You", "aliases": []},
             "released": {"date": "2023-12-22"}},
            {"mediaType": "movie", "tmdbId": 2, "titles": {"en": "Shared", "aliases": []}},
            {"mediaType": "movie", "tmdbId": 3, "titles": {"en": "Other", "aliases": ["Shared"]}},
            {"mediaType": "tv", "tmdbId": 4, "titles": {"en": "TV only", "aliases": []}},
        ]}
        snapshot = source([
            "113,cert-1,輔12,愛愛愛上你,ANYONE BUT YOU,美國,英語,Studio,103分鐘\n",
            "113,cert-2,普,共享,Shared,台灣,國語,Studio,90分鐘\n",
            "113,cert-3,護,沒有,Missing,台灣,國語,Studio,90分鐘\n",
            "113,cert-4,限,電視,TV only,台灣,國語,Studio,90分鐘\n",
            "113,cert-5,未定,Anyone but You,,台灣,國語,Studio,90分鐘\n",
            "113,cert-6,普,Anyone but You,,台灣,國語,Studio,90分鐘\n",
        ])
        rows = list(maturity_taiwan.records(snapshot))
        decided = next(row for row in rows if "cert-6" in row["authorityId"])
        with tempfile.TemporaryDirectory() as directory:
            report = bamid_review.build(
                self.write(directory, "facts.json", facts),
                self.write(directory, "bamid.zip", snapshot, binary=True),
                decisions={("BAMID", decided["authorityId"]): "movie:1"})
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(report["candidates"][0]["decision"]["key"], "movie:1")
        self.assertEqual(report["candidates"][0]["exactTitles"], ["anyone but you"])
        self.assertEqual(report["summary"], {
            "sourceRows": 6, "ratedRows": 5, "unknownRatingRows": 1, "missingIdentityRows": 0,
            "alreadyDecidedRows": 1,
            "uniqueExactTitleRows": 1, "ambiguousExactTitleRows": 1,
            "noExactTitleRows": 2, "queuedRows": 1})

    def test_limit_is_deterministic_and_report_is_factual_only(self):
        facts = {"records": [
            {"mediaType": "movie", "tmdbId": number,
             "titles": {"en": f"Film {number}", "aliases": []}}
            for number in range(1, 5)]}
        snapshot = source([
            f"113,cert-{number},普,片名 {number},Film {number},台灣,國語,Studio,90分鐘\n"
            for number in range(1, 5)])
        with tempfile.TemporaryDirectory() as directory:
            facts_path = self.write(directory, "facts.json", facts)
            snapshot_path = self.write(directory, "bamid.zip", snapshot, binary=True)
            first = bamid_review.build(facts_path, snapshot_path, limit=2, seed="fixed", decisions={})
            second = bamid_review.build(facts_path, snapshot_path, limit=2, seed="fixed", decisions={})
        self.assertEqual(first, second)
        self.assertEqual(len(first["candidates"]), 2)
        body = json.dumps(first)
        self.assertNotIn("synopsis", body)
        self.assertNotIn("overview", body)

    def test_refuses_bad_limits_and_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            facts_path = self.write(directory, "facts.json", {"records": "wrong"})
            snapshot_path = self.write(directory, "bamid.zip", source([]), binary=True)
            with self.assertRaises(ValueError):
                bamid_review.build(facts_path, snapshot_path, decisions={})
            valid_path = self.write(directory, "valid.json", {"records": []})
            with self.assertRaisesRegex(ValueError, "limit"):
                bamid_review.build(valid_path, snapshot_path, limit=0, decisions={})

    def test_generic_queue_uses_the_registered_modern_italy_provider(self):
        facts = {"records": [
            {"mediaType": "movie", "tmdbId": 7,
             "titles": {"en": "Reviewed Film", "aliases": []},
             "released": {"date": "2025-01-01"}},
        ]}
        snapshot = italy_snapshot([{
            "NUMERO_CLASSIFICAZIONE": "C12345678901234", "TITOLO": "Reviewed Film",
            "TITOLO_ORIGINALE": "", "REGIA": "Director", "MINUTAGGIO": "95",
            "CLASSIFICAZIONE": "Opera vietata ai minori di anni 14",
            "DATA_CLASSIFICAZIONE": "2025-02-03",
        }])
        with tempfile.TemporaryDirectory() as directory:
            report = maturity_review.build(
                self.write(directory, "facts.json", facts),
                self.write(directory, "italy.zip", snapshot, binary=True),
                maturity_italy.AUTHORITY, decisions={})
        candidate = next(row for row in report["candidates"]
                         if row["source"]["authorityId"] == "C12345678901234")
        self.assertEqual(candidate["decision"], {
            "authority": maturity_italy.AUTHORITY,
            "authorityId": "C12345678901234", "key": "movie:7"})
        self.assertEqual(candidate["source"]["directors"], "Director")

    def test_generic_queue_uses_the_registered_historical_italy_provider(self):
        facts = {"records": [
            {"mediaType": "movie", "tmdbId": 8,
             "titles": {"en": "Historical Film", "aliases": []}},
        ]}
        snapshot = italy_historical_payload([{
            "PROTOCOLLO": "9999", "TITOLO": "Historical Film",
            "DATA": "03-02-1970", "DIVIETO": "18",
        }])
        with tempfile.TemporaryDirectory() as directory:
            report = maturity_review.build(
                self.write(directory, "facts.json", facts),
                self.write(directory, "historical.csv", snapshot, binary=True),
                maturity_italy_historical.AUTHORITY, decisions={})
        candidate = next(row for row in report["candidates"]
                         if row["source"]["authorityId"] == "nulla-osta:9999")
        self.assertEqual(candidate["decision"]["key"], "movie:8")
        self.assertEqual(candidate["source"]["age"], 18)

    def test_generic_queue_refuses_an_unknown_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            facts_path = self.write(directory, "facts.json", {"records": []})
            snapshot_path = self.write(directory, "source", b"", binary=True)
            with self.assertRaisesRegex(ValueError, "unknown maturity authority"):
                maturity_review.build(facts_path, snapshot_path, "Not-A-Provider", decisions={})


if __name__ == "__main__":
    unittest.main()
