"""Singapore IMDA film classifications from the official data.gov.sg CSV."""
import csv
import hashlib
import io
import json
import urllib.parse

from lib import http
from .contract import StageError

DATASET = "d_ee0231c767e05c37f61e01923df2e461"
API_HOST = "api-open.data.gov.sg"
API_PATH = f"/v1/public/api/datasets/{DATASET}/poll-download"
API = f"https://{API_HOST}{API_PATH}"
PAGE = f"https://data.gov.sg/datasets/{DATASET}/view"
LICENSE = "https://developers.data.gov.sg/open-data-licence"
GUIDE = "https://iris.imda.gov.sg/guide/film-and-video-classification-guide"
DOWNLOAD_HOST = "s3.ap-southeast-1.amazonaws.com"
DOWNLOAD_PREFIX = f"/table-downloads-ingest.data.gov.sg/{DATASET}/"
MIN_ROWS = 4000
HEADERS = ("Year", "Title", "Romanised_Title", "Also_Known_As", "Actors", "Producers", "Directors",
           "Language_Description", "Format", "Rating", "Decision", "Consumer_Advice",
           "Total_Running_Time_Actual", "General_Info", "More_Info", "Decision_Date", "Episode_No",
           "Episode_Title", "Classified_By")


def age(value):
    return {"G - General": 0, "NC16 - No Children Below 16": 16,
            "M18 - Mature 18": 18}.get(str(value or "").strip())


def _clean(value):
    value = str(value or "").strip()
    return "" if value in ("-", "'-", "NIL", "Nil", "nil") else value


def _runtime(value):
    value = str(value or "").strip()
    return int(value) // 60 if value.isdigit() else None


def records(payload):
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    except UnicodeDecodeError:
        raise StageError("maturity: IMDA CSV is not UTF-8") from None
    if tuple(reader.fieldnames or ()) != HEADERS:
        raise StageError(f"maturity: IMDA columns changed: {reader.fieldnames!r}")
    rows = list(reader)
    if len(rows) < MIN_ROWS:
        raise StageError(f"maturity: IMDA CSV shrank to {len(rows)} rows")
    seen = set()
    for row in rows:
        if row["Classified_By"].strip() != "IMDA":
            continue
        titles = list(dict.fromkeys(value for key in ("Title", "Romanised_Title", "Also_Known_As")
                                    if (value := _clean(row[key]))))
        identity = "\x1f".join(_clean(row[key]) for key in
                                ("Year", "Title", "Romanised_Title", "Also_Known_As", "Directors",
                                 "Format", "Rating", "Decision", "Total_Running_Time_Actual",
                                 "Decision_Date", "Episode_No", "Episode_Title"))
        authority_id = "imda:" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        if authority_id in seen:
            continue
        seen.add(authority_id)
        rating = row["Rating"].strip()
        yield {"authority": "IMDA", "authorityId": authority_id, "titles": titles,
               "directors": _clean(row["Directors"]), "year": None, "raw": rating,
               "age": age(rating), "assessed": row["Decision_Date"].strip(),
               "runtimeMinutes": _runtime(row["Total_Running_Time_Actual"]),
               "matchedBy": "exact-title+year+director"}


def fetch(request=None):
    request = request or http.request
    metadata = request(API_HOST, API_PATH, headers={"Accept": "application/json"})
    try:
        root = json.loads(metadata.decode("utf-8"))
        url = root["data"]["url"] if root.get("code") == 0 else None
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError):
        raise StageError("maturity: IMDA download response changed") from None
    parsed = urllib.parse.urlsplit(url or "")
    if (parsed.scheme != "https" or parsed.hostname != DOWNLOAD_HOST
            or not parsed.path.startswith(DOWNLOAD_PREFIX) or not parsed.path.endswith(".csv")):
        raise StageError("maturity: IMDA advertised an unexpected download URL")
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    payload = request(parsed.netloc, path, headers={"Accept": "text/csv"})
    list(records(payload))
    return payload, API


def manifest_entry(snapshot, scope):
    payload = snapshot["payload"]
    return {"authority": "IMDA", "dataset": "Film Classification Database",
            "sourceUrl": snapshot["sourceUrl"], "landingPage": PAGE, "reuseBasis": LICENSE,
            "retrievedAt": snapshot["retrievedAt"], "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload), "scope": scope,
            **({"fallbackAfter": snapshot["fallbackAfter"]} if snapshot.get("fallbackAfter") else {})}
