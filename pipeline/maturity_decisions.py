"""Reviewed official-authority identifier links for maturity records exact metadata cannot resolve."""
import hashlib
import json
import os
import re

from .contract import REPO, StageError


PATH = os.path.join(REPO, "data", "maturity-authority-decisions.json")
AUTHORITIES = frozenset(("Medietilsynet", "CNC", "ClassInd", "KMRB", "KAVI", "BAMID", "IMDA"))
FIELDS = frozenset(("authority", "authorityId", "key"))
KEY = re.compile(r"movie:[1-9]\d*\Z")


def load(path=PATH):
    """Return the strictly validated ``(authority, id) -> corpus key`` review ledger and its digest."""
    try:
        with open(path, "rb") as handle:
            payload = handle.read()
        blob = json.loads(payload)
    except (OSError, UnicodeDecodeError, ValueError) as broken:
        raise StageError(f"maturity: authority decisions are unreadable: {broken}") from None
    if (not isinstance(blob, dict) or set(blob) != {"schema", "decisions"}
            or type(blob.get("schema")) is not int or blob["schema"] != 1
            or not isinstance(blob.get("decisions"), list)):
        raise StageError("maturity: authority decisions must have schema 1 and a decisions list")
    decisions = {}
    for number, row in enumerate(blob["decisions"], 1):
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise StageError(f"maturity: authority decision {number} has unexpected fields")
        authority, authority_id, key = row["authority"], row["authorityId"], row["key"]
        if authority not in AUTHORITIES:
            raise StageError(f"maturity: authority decision {number} names an unknown authority")
        if (not isinstance(authority_id, str) or not authority_id
                or authority_id != authority_id.strip()):
            raise StageError(f"maturity: authority decision {number} has an invalid authorityId")
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise StageError(f"maturity: authority decision {number} has an invalid movie key")
        identity = (authority, authority_id)
        if identity in decisions:
            raise StageError(f"maturity: duplicate authority decision for {authority} {authority_id}")
        decisions[identity] = key
    return decisions, hashlib.sha256(payload).hexdigest()


def validate_targets(decisions, facts_blob):
    targets = {f"movie:{row['tmdbId']}" for row in facts_blob.get("records") or ()
               if row.get("mediaType") == "movie" and isinstance(row.get("tmdbId"), int)
               and not isinstance(row.get("tmdbId"), bool) and row["tmdbId"] > 0}
    missing = sorted({key for key in decisions.values() if key not in targets})
    if missing:
        raise StageError(f"maturity: authority decisions target absent corpus keys: {', '.join(missing)}")


def validate_source(decisions, authority, rows):
    """When an authority is available, refuse reviewed IDs that disappeared from its current snapshot."""
    expected = {authority_id for (name, authority_id) in decisions if name == authority}
    if not expected:
        return
    present = {row.get("authorityId") for row in rows}
    missing = sorted(expected - present)
    if missing:
        raise StageError(f"maturity: {authority} authority decisions reference absent IDs: "
                         f"{', '.join(missing)}")
