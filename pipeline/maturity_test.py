import datetime
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

from . import maturity
from .contract import Context, StageError


def _column(index):
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def workbook(rows):
    """The two XLSX members the adapter reads, with omitted cells testing reference preservation."""
    strings, ids = [], {}
    for row in rows:
        for value in row:
            if value is not None and str(value) not in ids:
                ids[str(value)] = len(strings)
                strings.append(str(value))
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared = ET.Element("sst", xmlns=ns, count=str(len(strings)), uniqueCount=str(len(strings)))
    for value in strings:
        item = ET.SubElement(shared, "si")
        ET.SubElement(item, "t").text = value
    sheet = ET.Element("worksheet", xmlns=ns)
    data = ET.SubElement(sheet, "sheetData")
    for row_number, values in enumerate(rows, 1):
        row = ET.SubElement(data, "row", r=str(row_number))
        for column, value in enumerate(values, 1):
            if value is None:
                continue
            cell = ET.SubElement(row, "c", r=f"{_column(column)}{row_number}", t="s")
            ET.SubElement(cell, "v").text = str(ids[str(value)])
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", ET.tostring(shared, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/worksheets/sheet1.xml", ET.tostring(sheet, encoding="utf-8", xml_declaration=True))
    return body.getvalue()


def source_row(title="The Film", original=None, rating="12 år", director="Alice Director", year="2022",
               reason="copyrighted reason", synopsis="copyrighted synopsis", runtime="100"):
    return [title, original, "44800", rating, reason, "Distributor", director, year, "USA", runtime,
            synopsis, "other prose"]


def payload(*rows):
    return workbook([list(maturity.HEADERS), *rows])


def facts(*records):
    return {"records": list(records), "entities": {
        "Qalice": {"en": "Alice Director"}, "Qbob": {"en": "Bob Director"},
    }}


def movie(ident, title="The Film", year=2022, director="Qalice"):
    return {"mediaType": "movie", "tmdbId": ident, "titles": {"en": title},
            "released": {"date": str(year), "precision": "year"}, "directors": [director]}


class NorwayAdapter(unittest.TestCase):
    def test_reads_blank_cells_and_keeps_only_factual_fields(self):
        [record] = list(maturity.norway_records(payload(source_row(original=None))))
        self.assertEqual(record["titles"], ["The Film"])
        self.assertEqual((record["age"], record["year"], record["runtimeMinutes"]), (12, 2022, 100))
        self.assertNotIn("copyrighted reason", json.dumps(record))
        self.assertNotIn("copyrighted synopsis", json.dumps(record))

    def test_maps_all_ages_and_numeric_history_but_not_banned_or_missing(self):
        self.assertEqual(maturity.norway_age("Tillatt for alle"), 0)
        for age in (5, 6, 7, 9, 10, 11, 12, 15, 16, 18):
            self.assertEqual(maturity.norway_age(f"{age} år"), age)
        self.assertIsNone(maturity.norway_age("Forbudt"))
        self.assertIsNone(maturity.norway_age(None))

    def test_changed_columns_are_refused_instead_of_silently_shifted(self):
        body = workbook([["Title", *maturity.HEADERS[1:]], source_row()])
        with self.assertRaisesRegex(StageError, "columns changed"):
            list(maturity.norway_records(body))


class Linker(unittest.TestCase):
    def test_exact_title_year_director_match_uses_highest_verified_age(self):
        rows = list(maturity.norway_records(payload(
            source_row(rating="12 år"), source_row(rating="15 år"),
            source_row(title="Another Film", rating="Tillatt for alle", director="Bob Director"),
            source_row(title="The Film", rating="Forbudt"))))
        records, audit = maturity.link_norway(rows, facts(movie(1), movie(2, "Another Film", director="Qbob")))
        self.assertEqual([(row["tmdbId"], row["minimumAge"]) for row in records], [(1, 15), (2, 0)])
        self.assertEqual(audit["matchedRows"], 3)
        self.assertEqual(audit["matchedTitles"], 2)
        self.assertEqual(audit["conflictingTitles"], 1)
        self.assertEqual(audit["unknownRatingRows"], 1)
        self.assertEqual(records[0]["observations"][0]["matchedBy"], "exact-title+year+director")

    def test_wrong_director_missing_year_and_multiple_candidates_remain_unknown(self):
        rows = list(maturity.norway_records(payload(
            source_row(director="Someone Else"), source_row(year=None), source_row())))
        records, audit = maturity.link_norway(rows, facts(movie(1), movie(3)))
        self.assertEqual(records, [])
        self.assertEqual(audit["unmatchedRows"], 1)
        self.assertEqual(audit["missingIdentityRows"], 1)
        self.assertEqual(audit["ambiguousRows"], 1)
        self.assertEqual(audit["ambiguous"][0]["candidates"], ["movie:1", "movie:3"])

    def test_no_series_can_be_matched_to_the_movie_only_source(self):
        series = {**movie(8), "mediaType": "tv"}
        rows = list(maturity.norway_records(payload(source_row())))
        records, audit = maturity.link_norway(rows, facts(series))
        self.assertEqual(records, [])
        self.assertEqual(audit["unmatchedRows"], 1)


class Stage(unittest.TestCase):
    def test_writes_immutable_snapshot_manifest_and_derived_records(self):
        body = payload(source_row())
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "dataset.meta.json"), "w", encoding="utf-8") as handle:
                json.dump({"datasetVersion": "testver"}, handle)
            with open(os.path.join(out, "facts-testver.json"), "w", encoding="utf-8") as handle:
                json.dump(facts(movie(1)), handle)
            now = datetime.datetime(2026, 10, 12, 12, 30, tzinfo=datetime.timezone.utc)
            result = maturity.run(Context(out_dir=out), fetch=lambda: body, now=now)
            with open(os.path.join(out, "maturity-sources.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            with open(os.path.join(out, "maturity-testver.json"), encoding="utf-8") as handle:
                derived = json.load(handle)
            with open(os.path.join(out, "maturity-sources", "medietilsynet-1913-2022.xlsx"), "rb") as handle:
                self.assertEqual(handle.read(), body)
        self.assertIn("1 titles", result)
        self.assertEqual(manifest["sources"][0]["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(manifest["sources"][0]["retrievedAt"], "2026-10-12T12:30:00+00:00")
        self.assertEqual(derived["records"][0]["minimumAge"], 12)
        self.assertNotIn("copyrighted", json.dumps(derived))


if __name__ == "__main__":
    unittest.main()
