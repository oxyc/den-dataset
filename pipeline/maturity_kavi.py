"""KAVI IKLU movie rows collected without accepting the API's silent 100-result truncation."""
import hashlib
import json

from .contract import StageError

AUTHORITY = "KAVI"
API = "https://luokittelu.kavi.fi/agelimit/"
GUIDE = "https://kavi.fi/wp-content/uploads/2020/03/aPI.pdf"


def records(payload):
    for number, line in enumerate(payload.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as broken:
            raise StageError(f"maturity: KAVI snapshot line {number} is not JSON: {broken}") from None
        required = ("id", "type", "name", "year", "directors", "agelimit")
        if not isinstance(row, dict) or any(name not in row for name in required):
            raise StageError(f"maturity: KAVI snapshot line {number} changed schema")
        if row["type"] != "movie":
            continue
        age = row["agelimit"]
        if not isinstance(age, int) or isinstance(age, bool) or age not in {0, 7, 12, 16, 18}:
            age = None
        directors = row["directors"] if isinstance(row["directors"], list) else []
        titles = [row.get(name) for name in ("name", "nameFi", "nameSv")
                  if isinstance(row.get(name), str) and row[name].strip()]
        yield {"authority": AUTHORITY, "authorityId": f"iklu:{row['id']}", "titles": titles,
               "directors": "; ".join(name for name in directors if isinstance(name, str)),
               "year": row["year"] if isinstance(row["year"], int) else None,
               # The corpus keeps `raw` as the authority's own text (consolidate_corpus.maturity_rows).
               "raw": None if row["agelimit"] is None else str(row["agelimit"]), "age": age,
               "assessed": row.get("registrationDate"),
               "runtimeMinutes": (row.get("durationInSeconds") // 60
                                  if isinstance(row.get("durationInSeconds"), int) else None),
               "matchedBy": "exact-title+year+director"}


def manifest_entry(snapshot, scope):
    payload = snapshot["payload"]
    return {"authority": AUTHORITY, "dataset": "IKLU movie classifications",
            "sourceUrl": snapshot["sourceUrl"], "landingPage": GUIDE, "reuseBasis": GUIDE,
            "retrievedAt": snapshot["retrievedAt"], "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload), "scope": scope}
