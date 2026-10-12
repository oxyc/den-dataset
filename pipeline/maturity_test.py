import datetime
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

from . import maturity, maturity_kavi, maturity_kmrb, maturity_public
from .contract import Context, StageError
from lib import cache, http


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


def kmrb_page(items, page_number=1, total=None, page_size=1000):
    response = ET.Element("response")
    header = ET.SubElement(response, "header")
    ET.SubElement(header, "resultCode").text = "00"
    ET.SubElement(header, "resultMsg").text = "NORMAL SERVICE"
    body = ET.SubElement(response, "body")
    rows = ET.SubElement(ET.SubElement(body, "items"), "rows")
    for values in items:
        item = ET.SubElement(rows, "item")
        defaults = {"wrksNm": "The Film", "oriwkTtl": "", "fbctnYr": "2022",
                    "rtNo": "2026-NFLX-1", "grdNm": "15세이상관람가",
                    "direNm": "Alice Director", "screHr": "100", "PlotCn": "copyrighted plot"}
        defaults.update(values)
        for name, value in defaults.items():
            ET.SubElement(item, name).text = value
    ET.SubElement(body, "pageNo").text = str(page_number)
    ET.SubElement(body, "numOfRows").text = str(page_size)
    ET.SubElement(body, "totalCount").text = str(len(items) if total is None else total)
    return ET.tostring(response, encoding="utf-8", xml_declaration=True)


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

    def test_a_rated_row_without_an_official_authority_id_remains_unknown(self):
        rows = [{"authority": "KMRB", "authorityId": None, "titles": ["The Film"],
                 "directors": "Alice Director", "year": 2022, "raw": "12세이상관람가", "age": 12,
                 "matchedBy": "exact-title+year+director"}]
        records, audit = maturity.link_records(rows, facts(movie(1)))
        self.assertEqual(records, [])
        self.assertEqual(audit["missingIdentityRows"], 1)

    def test_combines_authorities_by_the_highest_verified_age(self):
        norway, _ = maturity.link_records(
            maturity.norway_records(payload(source_row(rating="12 år"))), facts(movie(1)))
        cnc = [{"authority": "CNC", "authorityId": "visa:1", "titles": ["The Film"],
                "directors": "Alice Director", "year": 2022, "raw": "Interdit -16 ans", "age": 16,
                "matchedBy": "exact-title+year+director"}]
        france, _ = maturity.link_records(cnc, facts(movie(1)))
        records, conflicts = maturity.combine((norway, france))
        self.assertEqual(records[0]["minimumAge"], 16)
        self.assertEqual([row["authority"] for row in records[0]["observations"]],
                         ["Medietilsynet", "CNC"])
        self.assertEqual(conflicts, [{"key": "movie:1", "ages": [12, 16]}])

    def test_no_series_can_be_matched_to_the_movie_only_source(self):
        series = {**movie(8), "mediaType": "tv"}
        rows = list(maturity.norway_records(payload(source_row())))
        records, audit = maturity.link_norway(rows, facts(series))
        self.assertEqual(records, [])
        self.assertEqual(audit["unmatchedRows"], 1)


class Stage(unittest.TestCase):
    def test_an_optional_kmrb_snapshot_uses_the_same_exact_public_evidence_path(self):
        norway = payload(source_row(title="Other", director="Bob Director"))
        cnc = b"Titre;R\xc3\xa9alisation;Date;N\xc2\xb0 de visa;D\xc3\xa9cision\n"
        kmrb = maturity_kmrb._zip([kmrb_page([{}])])
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "dataset.meta.json"), "w", encoding="utf-8") as handle:
                json.dump({"datasetVersion": "testver"}, handle)
            with open(os.path.join(out, "facts-testver.json"), "w", encoding="utf-8") as handle:
                json.dump(facts(movie(1)), handle)
            maturity.run(Context(out_dir=out), fetch=lambda: norway,
                         fetch_cnc=lambda: (cnc, "https://static.data.gouv.fr/cnc.csv"),
                         fetch_brazil=lambda: (_ for _ in ()).throw(
                             http.HTTPError(0, "https://dados.mj.gov.br")),
                         fetch_kmrb=lambda: (kmrb, maturity_kmrb.URL))
            with open(os.path.join(out, "maturity-testver.json"), encoding="utf-8") as handle:
                derived = json.load(handle)
            with open(os.path.join(out, "maturity-sources.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            with open(os.path.join(out, "maturity-sources", "kmrb-self-ratings.zip"), "rb") as handle:
                self.assertEqual(handle.read(), kmrb)
        self.assertEqual(derived["records"][0]["minimumAge"], 15)
        self.assertEqual(derived["records"][0]["observations"][0]["authority"], "KMRB")
        self.assertEqual(next(row for row in manifest["sources"] if row["authority"] == "KMRB")
                         ["sha256"], hashlib.sha256(kmrb).hexdigest())

    def test_writes_immutable_snapshot_manifest_and_derived_records(self):
        body = payload(source_row())
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "dataset.meta.json"), "w", encoding="utf-8") as handle:
                json.dump({"datasetVersion": "testver"}, handle)
            with open(os.path.join(out, "facts-testver.json"), "w", encoding="utf-8") as handle:
                json.dump(facts(movie(1)), handle)
            now = datetime.datetime(2026, 10, 12, 12, 30, tzinfo=datetime.timezone.utc)
            cnc = ("Titre;Réalisation;Production;Format;Date;Nationalité;N° de visa;Décision;Localisation\n"
                   "The Film;Alice Director;;;2022;;44;Interdit -16 ans;\n").encode()
            brazil = ("Código;Titulo no Brasil;Titulo original;Ano de produção;Diretores;"
                      "Classificação Atribuída\n9;The Film;;2022;Alice Director;14 anos\n").encode()
            result = maturity.run(Context(out_dir=out), fetch=lambda: body,
                                  fetch_cnc=lambda: (cnc, "https://static.data.gouv.fr/cnc.csv"),
                                  fetch_brazil=lambda: (brazil, "https://dados.mj.gov.br/classind.csv"), now=now)
            with open(os.path.join(out, "maturity-sources.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            with open(os.path.join(out, "maturity-testver.json"), encoding="utf-8") as handle:
                derived = json.load(handle)
            with open(os.path.join(out, "maturity-sources", "medietilsynet-1913-2022.xlsx"), "rb") as handle:
                self.assertEqual(handle.read(), body)
            with open(os.path.join(out, "maturity-sources", "cnc-visas.csv"), "rb") as handle:
                self.assertEqual(handle.read(), cnc)
            with open(os.path.join(out, "maturity-sources", "classind-audiovisual.csv"), "rb") as handle:
                self.assertEqual(handle.read(), brazil)
        self.assertIn("1 titles", result)
        self.assertEqual(manifest["sources"][0]["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(manifest["sources"][0]["retrievedAt"], "2026-10-12T12:30:00+00:00")
        self.assertEqual([row["authority"] for row in manifest["sources"]],
                         ["Medietilsynet", "CNC", "ClassInd"])
        self.assertEqual(derived["records"][0]["minimumAge"], 16)
        self.assertNotIn("copyrighted", json.dumps(derived))

    def test_a_dead_enhancement_source_is_unknown_not_a_partial_fake(self):
        body = payload(source_row())
        cnc = ("Titre;Réalisation;Date;N° de visa;Décision\n"
               "The Film;Alice Director;2022;44;Interdit -16 ans\n").encode()
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "dataset.meta.json"), "w", encoding="utf-8") as handle:
                json.dump({"datasetVersion": "testver"}, handle)
            with open(os.path.join(out, "facts-testver.json"), "w", encoding="utf-8") as handle:
                json.dump(facts(movie(1)), handle)
            def unavailable():
                raise http.HTTPError(0, "https://dados.mj.gov.br/")
            maturity.run(Context(out_dir=out), fetch=lambda: body,
                         fetch_cnc=lambda: (cnc, "https://static.data.gouv.fr/cnc.csv"),
                         fetch_brazil=unavailable)
            with open(os.path.join(out, "maturity-sources.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            with open(os.path.join(out, "maturity-testver.json"), encoding="utf-8") as handle:
                derived = json.load(handle)
            self.assertFalse(os.path.exists(os.path.join(out, "maturity-sources", "classind-audiovisual.csv")))
        self.assertEqual([row["authority"] for row in manifest["sources"]], ["Medietilsynet", "CNC"])
        self.assertEqual(manifest["unavailable"][0]["authority"], "ClassInd")
        self.assertEqual(derived["records"][0]["minimumAge"], 16)

    def test_a_recent_validated_snapshot_survives_a_transient_authority_outage(self):
        body = payload(source_row())
        cnc = ("Titre;Réalisation;Date;N° de visa;Décision\n"
               "The Film;Alice Director;2022;44;Interdit -16 ans\n").encode()
        brazil = ("Código;Titulo no Brasil;Titulo original;Ano de produção;Diretores;"
                  "Classificação Atribuída\n9;The Film;;2022;Alice Director;14 anos\n").encode()
        first = datetime.datetime(2026, 10, 1, 12, tzinfo=datetime.timezone.utc)
        with tempfile.TemporaryDirectory() as root:
            snapshots = cache.ResponseCache("test_maturity", root, 30 * cache.DAY_SECONDS)

            def prepare(out):
                with open(os.path.join(out, "dataset.meta.json"), "w", encoding="utf-8") as handle:
                    json.dump({"datasetVersion": "testver"}, handle)
                with open(os.path.join(out, "facts-testver.json"), "w", encoding="utf-8") as handle:
                    json.dump(facts(movie(1)), handle)

            with tempfile.TemporaryDirectory() as out:
                prepare(out)
                maturity.run(Context(out_dir=out), fetch=lambda: body,
                             fetch_cnc=lambda: (cnc, "https://static.data.gouv.fr/cnc.csv"),
                             fetch_brazil=lambda: (brazil, "https://dados.mj.gov.br/classind.csv"),
                             now=first, snapshot_cache=snapshots)

            def unavailable():
                raise http.HTTPError(0, "https://dados.mj.gov.br/")

            with tempfile.TemporaryDirectory() as out:
                prepare(out)
                maturity.run(Context(out_dir=out), fetch=lambda: body,
                             fetch_cnc=lambda: (cnc, "https://static.data.gouv.fr/cnc.csv"),
                             fetch_brazil=unavailable, now=first + datetime.timedelta(days=1),
                             snapshot_cache=snapshots)
                with open(os.path.join(out, "maturity-sources.json"), encoding="utf-8") as handle:
                    manifest = json.load(handle)
                with open(os.path.join(out, "maturity-sources", "classind-audiovisual.csv"), "rb") as handle:
                    self.assertEqual(handle.read(), brazil)
        source = next(row for row in manifest["sources"] if row["authority"] == "ClassInd")
        self.assertEqual(source["retrievedAt"], first.isoformat())
        self.assertIn("HTTP 0", source["fallbackAfter"])
        self.assertEqual(manifest["unavailable"], [])

    def test_a_corrupt_cached_pair_is_not_used_as_authority_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            snapshots = cache.ResponseCache("test_maturity", root, 30 * cache.DAY_SECONDS)
            snapshots.write(snapshots.key("ClassInd/body"), b"not the recorded body")
            snapshots.write(snapshots.key("ClassInd/metadata"), json.dumps({
                "authority": "ClassInd", "sourceUrl": "https://dados.mj.gov.br/file.csv",
                "retrievedAt": "2026-10-01T00:00:00+00:00", "sha256": "0" * 64}).encode())

            def unavailable():
                raise http.HTTPError(0, "https://dados.mj.gov.br/")

            snapshot, failure = maturity._source_snapshot(
                unavailable, maturity_public.brazil_records, "ClassInd", maturity_public.BRAZIL_PAGE,
                "2026-10-02T00:00:00+00:00", snapshots)
        self.assertIsNone(snapshot)
        self.assertEqual(failure["authority"], "ClassInd")

    def test_a_snapshot_past_the_fallback_window_is_not_used(self):
        brazil = ("Código;Titulo no Brasil;Titulo original;Ano de produção;Diretores;"
                  "Classificação Atribuída\n9;The Film;;2022;Alice Director;14 anos\n").encode()
        with tempfile.TemporaryDirectory() as root:
            writer = cache.ResponseCache("test_maturity", root, 30 * cache.DAY_SECONDS)
            maturity._remember_snapshot(writer, "ClassInd", {
                "payload": brazil, "sourceUrl": "https://dados.mj.gov.br/file.csv",
                "retrievedAt": "2026-01-01T00:00:00+00:00"})
            expired = cache.ResponseCache("test_maturity", root, -1)

            def unavailable():
                raise http.HTTPError(0, "https://dados.mj.gov.br/")

            snapshot, failure = maturity._source_snapshot(
                unavailable, maturity_public.brazil_records, "ClassInd", maturity_public.BRAZIL_PAGE,
                "2026-10-02T00:00:00+00:00", expired)
        self.assertIsNone(snapshot)
        self.assertEqual(failure["authority"], "ClassInd")

    def test_an_operator_kavi_snapshot_joins_the_same_exact_identity_path(self):
        norway = payload(source_row(title="Other", director="Bob Director"))
        cnc = b"Titre;R\xc3\xa9alisation;Date;N\xc2\xb0 de visa;D\xc3\xa9cision\n"
        kavi = (json.dumps({"id": 7, "type": "movie", "name": "The Film", "year": 2022,
                            "directors": ["Alice Director"], "agelimit": 18}) + "\n").encode()
        with tempfile.TemporaryDirectory() as out:
            os.makedirs(os.path.join(out, "maturity-sources"))
            with open(os.path.join(out, "dataset.meta.json"), "w", encoding="utf-8") as handle:
                json.dump({"datasetVersion": "testver"}, handle)
            with open(os.path.join(out, "facts-testver.json"), "w", encoding="utf-8") as handle:
                json.dump(facts(movie(1)), handle)
            with open(os.path.join(out, "maturity-sources", "kavi-movies.jsonl"), "wb") as handle:
                handle.write(kavi)
            maturity.run(Context(out_dir=out), fetch=lambda: norway,
                         fetch_cnc=lambda: (cnc, "https://static.data.gouv.fr/cnc.csv"),
                         fetch_brazil=lambda: (_ for _ in ()).throw(http.HTTPError(0, "https://dados.mj.gov.br")))
            with open(os.path.join(out, "maturity-testver.json"), encoding="utf-8") as handle:
                derived = json.load(handle)
        self.assertEqual(derived["records"][0]["minimumAge"], 18)
        self.assertEqual(derived["records"][0]["observations"][0]["authority"], "KAVI")


class PublicAdapters(unittest.TestCase):
    def test_kmrb_snapshot_keeps_raw_pages_but_emits_only_identity_and_age(self):
        snapshot = maturity_kmrb._zip([kmrb_page([{}])])
        [row] = list(maturity_kmrb.records(snapshot))
        self.assertEqual((row["authorityId"], row["titles"], row["age"], row["year"]),
                         ("kmrb:2026-NFLX-1", ["The Film"], 15, 2022))
        self.assertNotIn("copyrighted plot", json.dumps(row))
        with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
            self.assertIn(b"copyrighted plot", archive.read("page-000001.xml"))

    def test_kmrb_maps_only_stable_numeric_thresholds(self):
        self.assertEqual(maturity_kmrb.age("전체관람가"), 0)
        self.assertEqual(maturity_kmrb.age("12세 이상 관람가"), 12)
        self.assertEqual(maturity_kmrb.age("15세이상관람가"), 15)
        self.assertIsNone(maturity_kmrb.age("청소년관람불가"), "the threshold is now 19, not 18")
        self.assertIsNone(maturity_kmrb.age("제한상영가"))

    def test_kmrb_fetch_pages_to_the_advertised_end_without_leaking_the_key(self):
        asked = []
        bodies = [kmrb_page([{"rtNo": "one"}], 1, 2, 1),
                  kmrb_page([{"rtNo": "two"}], 2, 2, 1)]

        def request(host, path, params, headers):
            asked.append((host, path, params, headers))
            return bodies[len(asked) - 1]

        snapshot, source_url = maturity_kmrb.fetch("secret", request=request)
        self.assertEqual(len(list(maturity_kmrb.records(snapshot))), 2)
        self.assertEqual([row[2]["pageNo"] for row in asked], [1, 2])
        self.assertNotIn("secret", source_url)

    def test_kmrb_refuses_schema_and_snapshot_gaps(self):
        changed = kmrb_page([{}]).replace(b"<direNm>", b"<director>").replace(b"</direNm>", b"</director>")
        with self.assertRaisesRegex(StageError, "schema changed"):
            maturity_kmrb.page(changed)
        with self.assertRaisesRegex(StageError, "advertised 2"):
            list(maturity_kmrb.records(maturity_kmrb._zip([kmrb_page([{}], total=2)])))

    def test_kavi_keeps_movie_identity_and_official_age_only(self):
        body = (json.dumps({"id": 100235, "type": "movie", "name": "BIRDS, THE", "nameFi": "Linnut",
                            "nameSv": "Fåglarna", "year": 1963, "directors": ["Alfred Hitchcock"],
                            "agelimit": 16, "registrationDate": "2013-01-17",
                            "durationInSeconds": 7169, "warnings": ["copyrighted prose"]}) + "\n").encode()
        [row] = list(maturity_kavi.records(body))
        self.assertEqual((row["authorityId"], row["titles"], row["age"], row["runtimeMinutes"]),
                         ("iklu:100235", ["BIRDS, THE", "Linnut", "Fåglarna"], 16, 119))
        self.assertNotIn("copyrighted", json.dumps(row))

    def test_kavi_refuses_schema_drift_and_does_not_invent_an_age(self):
        with self.assertRaisesRegex(StageError, "changed schema"):
            list(maturity_kavi.records(b'{"id":1}\n'))
        body = (json.dumps({"id": 1, "type": "movie", "name": "One", "year": 2020,
                            "directors": ["A"], "agelimit": 13}) + "\n").encode()
        self.assertIsNone(next(maturity_kavi.records(body))["age"])

    def test_cnc_keeps_only_identity_and_numeric_decision_facts(self):
        body = ("Titre;Réalisation;Production;Format;Date;Nationalité;N° de visa;Décision;Localisation du dossier visa\n"
                "The Film;Alice Director;copyrighted prose;Long-métrage;2022;France;44;Interdit -12 ans;secret\n"
                "Another Film;Bob Director;;;2020;;45;Avertissement;\n").encode("utf-8")
        rows = list(maturity_public.cnc_records(body))
        self.assertEqual((rows[0]["authorityId"], rows[0]["age"], rows[0]["year"]),
                         ("visa:44", 12, 2022))
        self.assertIsNone(rows[1]["age"])
        self.assertNotIn("copyrighted", json.dumps(rows))
        self.assertNotIn("secret", json.dumps(rows))

    def test_cnc_maps_only_explicit_restrictions(self):
        self.assertEqual(maturity_public.cnc_age("Tout public"), 0)
        self.assertEqual(maturity_public.cnc_age("Interdit mineurs"), 18)
        self.assertEqual(maturity_public.cnc_age("Interdit -16 ans - Interdit -12 ans"), 16)
        self.assertIsNone(maturity_public.cnc_age("Avertissement"))
        self.assertIsNone(maturity_public.cnc_age("Censure"))

    def test_brazil_maps_official_ladder_and_discards_prose(self):
        body = ("Código;Titulo no Brasil;Titulo original;País de origem;Ano de produção;Produtores;"
                "Diretores;Atores;Classificação Atribuída;Contém\n"
                "9;O Filme;The Film;Brasil;2022;producer;Alice Director;actor;"
                "não recomendado para menores de 14 (catorze) anos;copyrighted reason\n").encode("utf-8")
        [row] = list(maturity_public.brazil_records(body))
        self.assertEqual((row["authorityId"], row["titles"], row["age"]),
                         ("classind:9", ["O Filme", "The Film"], 14))
        self.assertNotIn("copyrighted", json.dumps(row))
        self.assertEqual(maturity_public.brazil_age("Livre"), 0)
        self.assertIsNone(maturity_public.brazil_age("Especialmente recomendado"))

    def test_changed_public_columns_are_refused(self):
        with self.assertRaisesRegex(StageError, "CNC columns changed"):
            list(maturity_public.cnc_records(b"Title,Director\nA,B\n"))


if __name__ == "__main__":
    unittest.main()
