#!/usr/bin/env python3
"""MATURITY — conservative numeric ages from redistributable classification authorities.

Raw public-authority snapshots are kept byte-for-byte beside a manifest; only factual identity and
classification fields enter the derived JSON. Synopsis and classification-reason prose are never copied.
A source row is accepted only when exact normalized title, production year, and an exact director name
identify one corpus movie. Everything else stays unknown; there is intentionally no fuzzy or
model-assisted path.
"""
import dataclasses
import datetime
import hashlib
import io
import json
import os
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

from lib import cache, http
from . import artifacts, finalize, maturity_public
from .contract import StageError

NAME = "maturity"
PRODUCER = "pipeline/maturity.py"
HOW = "./den stage maturity --out-dir <dir>"
PUBLISHES = False
SPENDS = False
INPUTS = (artifacts.MANIFEST, artifacts.FACTS)
OUTPUTS = (artifacts.MATURITY_NORWAY, artifacts.MATURITY_CNC, artifacts.MATURITY_BRAZIL,
           artifacts.MATURITY_SOURCES, artifacts.MATURITY)

NORWAY_HOST = "www.medietilsynet.no"
NORWAY_PATH = "/contentassets/afe645f8781745e484a8f51d5e50a4ad/filmdatabasen.xlsx"
NORWAY_URL = f"https://{NORWAY_HOST}{NORWAY_PATH}"
NORWAY_PAGE = "https://www.medietilsynet.no/filmdatabasen/"
NORWAY_LICENSE = "https://lovdata.no/lov/2006-05-19-16/%C2%A77"
XML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
HEADERS = ("Tittel", "Originaltittel", "Vurdert dato", "Aldersgrense", "Begrunnelse", "Distributør",
           "Regissør", "Prod.år", "Prod.land", "Lengde", "Handling", "Annen info")


def normalized(value):
    """Exact matching normalization: Unicode/case/space only, never punctuation deletion or similarity."""
    if not isinstance(value, str):
        return None
    text = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return text or None


def _column(reference):
    match = re.match(r"[A-Z]+", reference or "")
    if not match:
        raise ValueError(f"invalid XLSX cell reference {reference!r}")
    number = 0
    for letter in match.group():
        number = number * 26 + ord(letter) - 64
    return number - 1


def xlsx_rows(payload):
    """Yield workbook rows using only the stdlib; blank cells retain their declared column."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        shared = []
        with archive.open("xl/sharedStrings.xml") as source:
            for _event, element in ET.iterparse(source, events=("end",)):
                if element.tag == XML + "si":
                    shared.append("".join(part.text or "" for part in element.iter(XML + "t")))
                    element.clear()
        with archive.open("xl/worksheets/sheet1.xml") as source:
            for _event, element in ET.iterparse(source, events=("end",)):
                if element.tag != XML + "row":
                    continue
                cells = {}
                for cell in element.findall(XML + "c"):
                    value = cell.find(XML + "v")
                    raw = "" if value is None else value.text or ""
                    cells[_column(cell.get("r"))] = shared[int(raw)] if cell.get("t") == "s" and raw else raw
                yield [cells.get(index) for index in range(max(cells, default=-1) + 1)]
                element.clear()
    except (KeyError, ValueError, zipfile.BadZipFile, ET.ParseError) as broken:
        raise StageError(f"maturity: Medietilsynet workbook is not a readable XLSX: {broken}") from None


def norway_age(raw):
    text = normalized(raw)
    if text == "tillatt for alle":
        return 0
    match = re.fullmatch(r"(\d{1,2}) år", text or "")
    if not match:
        return None
    age = int(match.group(1))
    return age if 0 <= age <= 18 else None


def _integer(value):
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def norway_records(payload):
    rows = iter(xlsx_rows(payload))
    header = next(rows, None)
    if header != list(HEADERS):
        raise StageError(f"maturity: Medietilsynet columns changed: {header!r}")
    for number, row in enumerate(rows, 2):
        row += [None] * (len(HEADERS) - len(row))
        title, original, assessed, rating, _reason, _distributor, director, year, country, runtime = row[:10]
        identity = "\x1f".join(str(value or "") for value in
                              (title, original, assessed, rating, director, year, runtime))
        yield {
            "authority": "Medietilsynet",
            "authorityId": "xlsx:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
            "row": number,
            "titles": [value for value in (title, original) if normalized(value)],
            "assessed": assessed,
            "raw": rating,
            "age": norway_age(rating),
            "directors": director,
            "year": _integer(year),
            "country": country,
            "runtimeMinutes": _integer(runtime),
            "matchedBy": "exact-title+year+director",
        }


def _year(fact):
    value = fact.get("released") or fact.get("started") or {}
    date = value.get("date") if isinstance(value, dict) else value
    match = re.match(r"^[+-]?(\d{1,4})", str(date or ""))
    return int(match.group(1)) if match else None


def _aliases(fact):
    titles = fact.get("titles") or {}
    values = [titles.get("en"), titles.get("orig"), *(titles.get("aliases") or [])]
    return {name for value in values if (name := normalized(value))}


def _entity_names(qids, entities):
    out = set()
    for qid in qids or ():
        entity = entities.get(qid) or {}
        for value in (entity.get("en"), *(entity.get("aliases") or [])):
            if name := normalized(value):
                out.add(name)
    return out


def _director_parts(value):
    """Full source credit plus exact individual names separated by explicit multi-credit punctuation."""
    whole = normalized(value)
    if not whole:
        return set()
    parts = {whole}
    for part in re.split(r"\s*(?:/|;|,|\bog\b|\band\b|&)\s*", whole):
        if part:
            parts.add(part)
    return parts


def link_records(source_records, facts_blob):
    entities = facts_blob.get("entities") or {}
    targets, index = {}, {}
    for fact in facts_blob.get("records") or ():
        if fact.get("mediaType") != "movie" or not isinstance(fact.get("tmdbId"), int):
            continue
        key = f"movie:{fact['tmdbId']}"
        target = {"key": key, "year": _year(fact), "directors": _entity_names(fact.get("directors"), entities)}
        targets[key] = target
        for title in _aliases(fact):
            index.setdefault((title, target["year"]), set()).add(key)

    observations, ambiguous = {}, []
    counts = {"sourceRows": 0, "ratedRows": 0, "matchedRows": 0, "unmatchedRows": 0,
              "ambiguousRows": 0, "missingIdentityRows": 0, "unknownRatingRows": 0}
    for source in source_records:
        counts["sourceRows"] += 1
        if source["age"] is None:
            counts["unknownRatingRows"] += 1
            continue
        counts["ratedRows"] += 1
        title_keys = {(title, source["year"]) for raw in source["titles"] if (title := normalized(raw))}
        candidates = set().union(*(index.get(pair, set()) for pair in title_keys)) if title_keys else set()
        directors = _director_parts(source["directors"])
        if source["year"] is None or not title_keys or not directors:
            counts["missingIdentityRows"] += 1
            continue
        candidates = {key for key in candidates if targets[key]["directors"] & directors}
        if len(candidates) != 1:
            counts["ambiguousRows" if candidates else "unmatchedRows"] += 1
            if candidates:
                ambiguous.append({"authorityId": source["authorityId"], "candidates": sorted(candidates)})
            continue
        key = next(iter(candidates))
        counts["matchedRows"] += 1
        observation = {name: source[name] for name in
                       ("authorityId", "raw", "age", "assessed", "runtimeMinutes") if source.get(name) is not None}
        observation.update({"authority": source["authority"], "matchedBy": source["matchedBy"]})
        observations.setdefault(key, []).append(observation)

    records, conflicts = [], []
    for key, found in sorted(observations.items()):
        ages = sorted({entry["age"] for entry in found})
        if len(ages) > 1:
            conflicts.append({"key": key, "ages": ages})
        media, ident = key.split(":")
        records.append({"mediaType": media, "tmdbId": int(ident), "minimumAge": max(ages),
                        "observations": sorted(found, key=lambda row: (row["age"], row["authorityId"]))})
    return records, {**counts, "matchedTitles": len(records), "conflictingTitles": len(conflicts),
                     "ambiguous": ambiguous, "conflicts": conflicts}


def link_norway(source_records, facts_blob):
    """Compatibility name for the original adapter and its focused tests."""
    return link_records(source_records, facts_blob)


def combine(linked):
    """One conservative title row across authorities: the highest verified numeric observation wins."""
    observations = {}
    for records in linked:
        for record in records:
            key = f"{record['mediaType']}:{record['tmdbId']}"
            observations.setdefault(key, []).extend(record["observations"])
    out, conflicts = [], []
    for key, found in sorted(observations.items()):
        ages = sorted({row["age"] for row in found})
        if len(ages) > 1:
            conflicts.append({"key": key, "ages": ages})
        media, ident = key.split(":")
        out.append({"mediaType": media, "tmdbId": int(ident), "minimumAge": max(ages),
                    "observations": sorted(found, key=lambda row: (row["age"], row["authority"],
                                                                    row["authorityId"]))})
    return out, conflicts


def fetch_norway():
    return http.request(NORWAY_HOST, NORWAY_PATH,
                        headers={"Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})


def _write_json(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    cache.write_atomically(path, (json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode())


def _enhancement(fetcher, authority):
    """A dead optional authority leaves its titles unknown; a body/schema error still refuses the stage."""
    try:
        return fetcher(), None
    except http.HTTPError as refusal:
        return None, {"authority": authority, "error": str(refusal)}


def run(ctx, fetch=fetch_norway, fetch_cnc=None, fetch_brazil=None, now=None):
    try:
        version = finalize.manifest_version(ctx)
    except StageError as refusal:
        raise StageError(f"maturity: {refusal}") from None
    ctx = dataclasses.replace(ctx, dataset_version=version)
    with open(ctx.require(artifacts.FACTS), encoding="utf-8") as handle:
        facts = json.load(handle)
    norway = fetch()
    cnc_result, cnc_failure = _enhancement(fetch_cnc or maturity_public.fetch_cnc, "CNC")
    brazil_result, brazil_failure = _enhancement(fetch_brazil or maturity_public.fetch_brazil, "ClassInd")
    cnc, cnc_url = cnc_result or (None, None)
    brazil, brazil_url = brazil_result or (None, None)
    snapshots = [(artifacts.MATURITY_NORWAY, norway)]
    snapshots += [(artifacts.MATURITY_CNC, cnc)] if cnc is not None else []
    snapshots += [(artifacts.MATURITY_BRAZIL, brazil)] if brazil is not None else []
    for artifact, payload in snapshots:
        raw_path = ctx.path(artifact)
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        cache.write_atomically(raw_path, payload)
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(datetime.timezone.utc).isoformat()
    scope = "factual classification and identity fields only; prose fields excluded"
    sources = [
        {"authority": "Medietilsynet", "dataset": "Filmdatabasen 1913–2022", "sourceUrl": NORWAY_URL,
         "landingPage": NORWAY_PAGE, "reuseBasis": NORWAY_LICENSE, "retrievedAt": stamp,
         "sha256": hashlib.sha256(norway).hexdigest(), "bytes": len(norway), "scope": scope},
    ]
    if cnc is not None:
        sources.append({"authority": "CNC", "dataset": "Visas d'exploitation 1945–2025", "sourceUrl": cnc_url,
         "landingPage": maturity_public.CNC_PAGE, "reuseBasis": maturity_public.CNC_LICENSE,
         "retrievedAt": stamp, "sha256": maturity_public.digest(cnc), "bytes": len(cnc), "scope": scope})
    if brazil is not None:
        sources.append({"authority": "ClassInd", "dataset": "Obras Audiovisuais", "sourceUrl": brazil_url,
         "landingPage": maturity_public.BRAZIL_PAGE, "reuseBasis": maturity_public.BRAZIL_LICENSE,
         "retrievedAt": stamp, "sha256": maturity_public.digest(brazil), "bytes": len(brazil), "scope": scope})
    failures = [row for row in (cnc_failure, brazil_failure) if row]
    _write_json(ctx.path(artifacts.MATURITY_SOURCES),
                {"schema": 1, "sources": sources, "unavailable": failures})
    norway_linked, norway_audit = link_records(norway_records(norway), facts)
    cnc_linked, cnc_audit = link_records(maturity_public.cnc_records(cnc) if cnc is not None else (), facts)
    brazil_linked, brazil_audit = link_records(
        maturity_public.brazil_records(brazil) if brazil is not None else (), facts)
    records, conflicts = combine((norway_linked, cnc_linked, brazil_linked))
    out = {"schema": 1, "datasetVersion": version, "records": records,
           "derivation": {"rule": "highest verified age across authorities; exact identity only",
                          "sourceSha256": {row["authority"]: row["sha256"] for row in sources},
                          "audit": {"Medietilsynet": norway_audit, "CNC": cnc_audit,
                                    "ClassInd": brazil_audit, "unavailable": failures,
                                    "crossAuthorityConflicts": conflicts}}}
    _write_json(ctx.path(artifacts.MATURITY), out)
    return (f"{ctx.path(artifacts.MATURITY)} — {len(records):,} titles; "
            f"{sum(a['ambiguousRows'] for a in (norway_audit, cnc_audit, brazil_audit)):,} ambiguous, "
            f"{len(conflicts):,} cross-authority conflicts")
