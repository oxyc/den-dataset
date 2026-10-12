"""Québec MCC feature-film classifications from the official Données Québec CSV."""
import csv
import hashlib
import io
import json
import re
import urllib.parse

from lib import http
from .contract import StageError

AUTHORITY = "MCC-Québec"
HOST = "www.donneesquebec.ca"
DATASET = "classement-des-long-metrages-diffuses-sur-le-territoire-quebecois"
PACKAGE = "39308c05-abee-4197-9081-eae1d3ed9bfc"
RESOURCE = "17f8a69b-acac-468a-ae85-6aa4d6d29929"
API_PATH = "/recherche/api/3/action/package_show"
API = f"https://{HOST}{API_PATH}?id={DATASET}"
PAGE = f"https://{HOST}/recherche/dataset/{DATASET}"
LICENSE = "https://www.donneesquebec.ca/licence/#cc-0"
GUIDE = "https://repertoire.cinema.mcc.gouv.qc.ca/criteres-de-classement/"
DOWNLOAD_PREFIX = f"/recherche/dataset/{PACKAGE}/resource/{RESOURCE}/download/"
MIN_ROWS = 29000
HEADERS = ("NO_FILM", "TITRE", "TITRE_FRANCAIS", "TITRE_ANGLAIS", "REALISATEUR1",
           "REALISATEUR2", "REALISATEUR3", "NATION1", "NATION2", "NATION3", "ANNEE",
           "CLASSEMENT", "INDICATION_1", "INDICATION_2", "DATE_DECISION", "NO_FILM_1", "URL")


def age(value):
    return {"G": 0, "13": 13, "16": 16, "18": 18}.get(str(value or "").strip())


def _title_variants(value):
    value = str(value or "").strip()
    if not value:
        return []
    match = re.fullmatch(r"(.+),\s+(THE|A|AN|LE|LA|LES|L')", value, re.I)
    if not match:
        return [value]
    stem, article = match.groups()
    natural = article + ("" if article.upper() == "L'" else " ") + stem
    return [value, natural]


def _director(value):
    value = str(value or "").strip()
    match = re.fullmatch(r"([^,]+),\s*(.+)", value)
    return f"{match.group(2)} {match.group(1)}" if match else value


def records(payload):
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")), delimiter=";")
    except UnicodeDecodeError:
        raise StageError("maturity: Québec CSV is not UTF-8") from None
    if tuple(reader.fieldnames or ()) != HEADERS:
        raise StageError(f"maturity: Québec columns changed: {reader.fieldnames!r}")
    rows = list(reader)
    if len(rows) < MIN_ROWS:
        raise StageError(f"maturity: Québec CSV shrank to {len(rows)} rows")
    seen = set()
    for row in rows:
        ident = row["NO_FILM"].strip()
        if (not ident or ident != row["NO_FILM_1"].strip()
                or not row["URL"].rstrip("/").endswith("/" + ident)):
            raise StageError("maturity: Québec film identity columns disagree")
        authority_id = f"quebec:{ident}"
        if authority_id in seen:
            raise StageError(f"maturity: Québec repeated {authority_id}")
        seen.add(authority_id)
        titles = []
        for key in ("TITRE", "TITRE_FRANCAIS", "TITRE_ANGLAIS"):
            titles.extend(_title_variants(row[key]))
        titles = list(dict.fromkeys(titles))
        directors = [_director(row[key]) for key in ("REALISATEUR1", "REALISATEUR2", "REALISATEUR3")]
        rating = row["CLASSEMENT"].strip()
        year = row["ANNEE"].strip()
        yield {"authority": AUTHORITY, "authorityId": authority_id, "titles": titles,
               "directors": "; ".join(value for value in directors if value),
               "year": int(year) if year.isdigit() else None, "raw": rating, "age": age(rating),
               "assessed": row["DATE_DECISION"].strip(), "runtimeMinutes": None,
               "matchedBy": "exact-title+year+director"}


def fetch(request=None):
    request = request or http.request
    metadata = request(HOST, API_PATH, {"id": DATASET}, headers={"Accept": "application/json"})
    try:
        root = json.loads(metadata.decode("utf-8"))
        package = root["result"] if root.get("success") is True else None
        resources = {entry.get("id"): entry for entry in package.get("resources") or ()}
        url = (resources.get(RESOURCE) or {}).get("url")
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError):
        raise StageError("maturity: Québec catalogue response changed") from None
    parsed = urllib.parse.urlsplit(url or "")
    if (parsed.scheme != "https" or parsed.hostname != HOST
            or not parsed.path.startswith(DOWNLOAD_PREFIX) or not parsed.path.endswith(".csv")):
        raise StageError("maturity: Québec catalogue advertised an unexpected CSV URL")
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    payload = request(parsed.netloc, path, headers={"Accept": "text/csv"})
    list(records(payload))
    return payload, API


def manifest_entry(snapshot, scope):
    payload = snapshot["payload"]
    return {"authority": AUTHORITY, "dataset": "Feature films distributed in Québec",
            "sourceUrl": snapshot["sourceUrl"], "landingPage": PAGE, "reuseBasis": LICENSE,
            "classificationGuide": GUIDE, "retrievedAt": snapshot["retrievedAt"],
            "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "scope": scope,
            **({"fallbackAfter": snapshot["fallbackAfter"]} if snapshot.get("fallbackAfter") else {})}
