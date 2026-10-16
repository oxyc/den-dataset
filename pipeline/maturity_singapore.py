"""Singapore IMDA film classifications from the official data.gov.sg CSV."""
import csv
import hashlib
import io
import json
import re
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
    # IMDA publishes Total_Running_Time_Actual in minutes (a season package can therefore be several
    # hundred). Treating it as seconds made an ordinary feature film appear one or two minutes long.
    return int(value) if value.isdigit() else None


def _tv_identity(titles, row):
    """Production year and base titles encoded by IMDA in episode-package title fields."""
    found, years = [], set()
    for title in titles:
        match = re.fullmatch(r"(.+?)\s+\(((?:19|20)\d{2})\)", title)
        if match:
            found.append(match.group(1).strip())
            years.add(int(match.group(2)))
        else:
            found.append(title)
    year = next(iter(years)) if len(years) == 1 else None
    credits = [_clean(row[name]) for name in ("Directors", "Producers", "Actors")]
    people = "; ".join(re.sub(r"\s*\((?:creator|director|producer)\)\s*$", "", value,
                              flags=re.I) for value in credits if value)
    return list(dict.fromkeys(found)), year, people


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
        episode = _clean(row["Episode_No"])
        media_types = ("tv",) if episode else ("movie",)
        year, people = None, _clean(row["Directors"])
        if episode:
            titles, year, people = _tv_identity(titles, row)
        yield {"authority": "IMDA", "authorityId": authority_id, "titles": titles,
               "directors": _clean(row["Directors"]), "people": people, "year": year, "raw": rating,
               "age": age(rating), "assessed": row["Decision_Date"].strip(),
               "runtimeMinutes": _runtime(row["Total_Running_Time_Actual"]),
               "matchedBy": "exact-title+year+credited-person",
               # IMDA's Year is the classification year, not a production year. Only episode packages
               # whose official title itself ends in an unambiguous production year can link automatically.
               "mediaTypes": media_types}


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
