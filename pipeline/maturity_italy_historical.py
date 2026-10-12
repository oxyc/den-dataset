"""Italian DGCA historical film clearances from the complete Nulla Osta CSV export."""
import csv
import datetime
import hashlib
import io
import json
import re
import urllib.parse

from lib import http
from .contract import StageError

AUTHORITY = "DGCA-Italy-NullaOsta"
HOST = "cinema.cultura.gov.it"
PAGE_PATH = "/database-nulla-osta-film/"
PAGE = f"https://{HOST}{PAGE_PATH}"
API_PATH = "/wp-json/mic-nullaosta/v1/datatable"
API = f"https://{HOST}{API_PATH}"
PARAMS = {"draw": "1", "start": "0", "length": "10", "esporta": "1"}
DOWNLOAD_PREFIX = "/tempcsv/DGCA-Export_DB_NULLA_OSTA-"
LEGAL = "https://cinema.cultura.gov.it/note-legali/"
LICENSE = "https://creativecommons.org/licenses/by/3.0/"
MIN_ROWS = 15000
MIN_GROUPS = 15000
HEADERS = ("PROTOCOLLO", "TITOLO", "ORIGINALE", "REGIA", "PRODUZIONE", "RICHIEDENTE",
           "METRI", "DATA", "NAZIONALITA", "EDIZIONE", "DIVIETO", "RAGAZZI", "")
RATINGS = {"Per tutti": 0, "tutti": 0, "14": 14, "18": 18}


def age(value):
    return RATINGS.get(str(value or "").strip())


def _clean(value):
    value = " ".join(str(value or "").strip().split())
    return "" if value == "-" else value


def _date(value):
    try:
        return datetime.datetime.strptime(_clean(value), "%d-%m-%Y").date().isoformat()
    except ValueError:
        return None


def records(payload):
    try:
        lines = payload.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        raise StageError("maturity: DGCA Nulla Osta CSV is not UTF-8") from None
    try:
        start = next(index for index, line in enumerate(lines) if line.startswith("PROTOCOLLO;"))
    except StopIteration:
        raise StageError("maturity: DGCA Nulla Osta CSV has no header") from None
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])), delimiter=";")
    if tuple(reader.fieldnames or ()) != HEADERS:
        raise StageError(f"maturity: DGCA Nulla Osta columns changed: {reader.fieldnames!r}")
    rows = list(reader)
    if len(rows) < MIN_ROWS:
        raise StageError(f"maturity: DGCA Nulla Osta export shrank to {len(rows)} rows")
    grouped = {}
    for row in rows:
        protocol = _clean(row["PROTOCOLLO"])
        if not re.fullmatch(r"\d{4,7}", protocol):
            raise StageError("maturity: DGCA Nulla Osta export has an invalid protocol number")
        entry = grouped.setdefault(protocol, {"titles": [], "ratings": set(), "dates": set()})
        for key in ("TITOLO", "ORIGINALE"):
            title = _clean(row[key])
            if title and title not in entry["titles"]:
                entry["titles"].append(title)
        entry["ratings"].add(_clean(row["DIVIETO"]))
        if assessed := _date(row["DATA"]):
            entry["dates"].add(assessed)
    if len(grouped) < MIN_GROUPS:
        raise StageError(f"maturity: DGCA Nulla Osta export shrank to {len(grouped)} protocols")
    for protocol, entry in sorted(grouped.items()):
        ratings = entry["ratings"]
        mapped = [age(rating) for rating in ratings]
        # An unrecognised/conditional/refused decision is never weakened by another duplicate row.
        minimum = max(mapped) if mapped and all(value is not None for value in mapped) else None
        yield {"authority": AUTHORITY, "authorityId": f"nulla-osta:{protocol}",
               "titles": entry["titles"], "directors": "", "year": None,
               "raw": "|".join(sorted(ratings)), "age": minimum,
               "assessed": max(entry["dates"]) if entry["dates"] else None,
               "runtimeMinutes": None, "matchedBy": "exact-title+year+director"}


def fetch(request=None):
    request = request or http.request
    metadata = request(HOST, API_PATH, PARAMS, headers={"Accept": "application/json"})
    try:
        root = json.loads(metadata.decode("utf-8"))
        url = root["filename"]
        total = root["recordsTotal"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise StageError("maturity: DGCA Nulla Osta export response changed") from None
    if not isinstance(total, int) or total < MIN_ROWS:
        raise StageError(f"maturity: DGCA Nulla Osta API advertised only {total!r} rows")
    parsed = urllib.parse.urlsplit(url if isinstance(url, str) else "")
    if (parsed.scheme != "https" or parsed.hostname != HOST
            or not parsed.path.startswith(DOWNLOAD_PREFIX) or not parsed.path.endswith(".csv")):
        raise StageError("maturity: DGCA Nulla Osta advertised an unexpected export URL")
    payload = request(HOST, parsed.path, headers={"Accept": "*/*"})
    found = list(records(payload))
    # The export includes three preamble lines before the advertised data rows.
    if len(payload.decode("utf-8-sig").splitlines()) - 3 < total:
        raise StageError("maturity: DGCA Nulla Osta export is shorter than its advertised total")
    if not found:
        raise StageError("maturity: DGCA Nulla Osta export contains no usable protocols")
    return payload, API


def manifest_entry(snapshot, scope):
    payload = snapshot["payload"]
    return {"authority": AUTHORITY, "dataset": "Database Nulla Osta film",
            "sourceUrl": snapshot["sourceUrl"], "landingPage": PAGE,
            "reuseBasis": LEGAL, "licence": LICENSE, "retrievedAt": snapshot["retrievedAt"],
            "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "scope": scope,
            **({"fallbackAfter": snapshot["fallbackAfter"]}
               if snapshot.get("fallbackAfter") else {})}
