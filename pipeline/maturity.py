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
from . import artifacts, finalize, maturity_decisions, maturity_kavi, maturity_kmrb, maturity_public
from .contract import StageError, UpstreamError

NAME = "maturity"
PRODUCER = "pipeline/maturity.py"
HOW = "./den stage maturity --out-dir <dir>"
PUBLISHES = False
SPENDS = False
INPUTS = (artifacts.MANIFEST, artifacts.FACTS, artifacts.MATURITY_KAVI)
OUTPUTS = (artifacts.MATURITY_NORWAY, artifacts.MATURITY_CNC, artifacts.MATURITY_BRAZIL,
           artifacts.MATURITY_KMRB,
           artifacts.MATURITY_SOURCES, artifacts.MATURITY)

NORWAY_HOST = "www.medietilsynet.no"
NORWAY_PATH = "/contentassets/afe645f8781745e484a8f51d5e50a4ad/filmdatabasen.xlsx"
NORWAY_URL = f"https://{NORWAY_HOST}{NORWAY_PATH}"
NORWAY_PAGE = "https://www.medietilsynet.no/filmdatabasen/"
NORWAY_LICENSE = "https://lovdata.no/lov/2006-05-19-16/%C2%A77"
XML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
HEADERS = ("Tittel", "Originaltittel", "Vurdert dato", "Aldersgrense", "Begrunnelse", "Distributør",
           "Regissør", "Prod.år", "Prod.land", "Lengde", "Handling", "Annen info")
SOURCE_CACHE_NAMESPACE = "maturity_source"
SOURCE_CACHE_DAYS = 30


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


def link_records(source_records, facts_blob, decisions=None):
    decisions = decisions or {}
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
    counts = {"sourceRows": 0, "ratedRows": 0, "matchedRows": 0, "decisionMatchedRows": 0,
              "unmatchedRows": 0,
              "ambiguousRows": 0, "missingIdentityRows": 0, "unknownRatingRows": 0}
    for source in source_records:
        counts["sourceRows"] += 1
        if source["age"] is None:
            counts["unknownRatingRows"] += 1
            continue
        counts["ratedRows"] += 1
        if not source.get("authorityId"):
            counts["missingIdentityRows"] += 1
            continue
        title_keys = {(title, source["year"]) for raw in source["titles"] if (title := normalized(raw))}
        candidates = set().union(*(index.get(pair, set()) for pair in title_keys)) if title_keys else set()
        directors = _director_parts(source["directors"])
        exact_candidates = ({key for key in candidates if targets[key]["directors"] & directors}
                            if source["year"] is not None and title_keys and directors else set())
        decision = decisions.get((source["authority"], source["authorityId"]))
        if decision is not None:
            if decision not in targets:
                raise StageError(f"maturity: reviewed {source['authority']} {source['authorityId']} "
                                 f"targets absent corpus key {decision}")
            if exact_candidates and decision not in exact_candidates:
                raise StageError(f"maturity: reviewed {source['authority']} {source['authorityId']} "
                                 f"targets {decision}, but exact identity now excludes it "
                                 f"({', '.join(sorted(exact_candidates))})")
            key = decision
            matched_by = "authority-id-decision"
            counts["decisionMatchedRows"] += 1
        else:
            if source["year"] is None or not title_keys or not directors:
                counts["missingIdentityRows"] += 1
                continue
            candidates = exact_candidates
            if len(candidates) != 1:
                counts["ambiguousRows" if candidates else "unmatchedRows"] += 1
                if candidates:
                    ambiguous.append({"authorityId": source["authorityId"], "candidates": sorted(candidates)})
                continue
            key = next(iter(candidates))
            matched_by = source["matchedBy"]
        counts["matchedRows"] += 1
        observation = {name: source[name] for name in
                       ("authorityId", "raw", "age", "assessed", "runtimeMinutes") if source.get(name) is not None}
        observation.update({"authority": source["authority"], "matchedBy": matched_by})
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


def _cached_snapshot(store, authority):
    if store is None:
        return None
    body = store.read(store.key(f"{authority}/body"))
    metadata = store.read(store.key(f"{authority}/metadata"))
    if body is None or metadata is None:
        return None
    try:
        metadata = json.loads(metadata.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if (not isinstance(metadata, dict) or metadata.get("authority") != authority
            or metadata.get("sha256") != hashlib.sha256(body).hexdigest()
            or not isinstance(metadata.get("sourceUrl"), str)
            or not metadata["sourceUrl"].startswith("https://")
            or not isinstance(metadata.get("retrievedAt"), str)):
        return None
    return {"payload": body, "sourceUrl": metadata["sourceUrl"],
            "retrievedAt": metadata["retrievedAt"]}


def _remember_snapshot(store, authority, snapshot):
    if store is None:
        return
    body = snapshot["payload"]
    metadata = {"authority": authority, "sourceUrl": snapshot["sourceUrl"],
                "retrievedAt": snapshot["retrievedAt"], "sha256": hashlib.sha256(body).hexdigest()}
    # Metadata last: an interruption can leave an unreferenced new body, never metadata that vouches for
    # bytes it did not hash. `_cached_snapshot` checks the pair again before use.
    store.write(store.key(f"{authority}/body"), body)
    store.write(store.key(f"{authority}/metadata"), json.dumps(metadata, sort_keys=True).encode())


def _source_snapshot(fetcher, parser, authority, default_url, stamp, store=None, required=False):
    """Fetch and validate one source, falling back only to a recent validated last-known-good body."""
    try:
        fetched = fetcher()
    except http.HTTPError as refusal:
        snapshot = _cached_snapshot(store, authority)
        if snapshot is None:
            if required:
                raise
            return None, {"authority": authority, "error": str(refusal)}
        # Re-validate cached bytes under today's parser. A source schema we no longer understand must not
        # become valid merely because its body came from yesterday.
        snapshot["rows"] = list(parser(snapshot["payload"]))
        snapshot["fallbackAfter"] = str(refusal)
        return snapshot, None
    payload, source_url = fetched if isinstance(fetched, tuple) else (fetched, default_url)
    snapshot = {"payload": payload, "sourceUrl": source_url, "retrievedAt": stamp,
                "rows": list(parser(payload))}
    _remember_snapshot(store, authority, snapshot)
    return snapshot, None


def run(ctx, fetch=fetch_norway, fetch_cnc=None, fetch_brazil=None, fetch_kmrb=None,
        now=None, snapshot_cache=None):
    try:
        version = finalize.manifest_version(ctx)
    except StageError as refusal:
        raise StageError(f"maturity: {refusal}") from None
    ctx = dataclasses.replace(ctx, dataset_version=version)
    with open(ctx.require(artifacts.FACTS), encoding="utf-8") as handle:
        facts = json.load(handle)
    decisions, decisions_sha = maturity_decisions.load()
    maturity_decisions.validate_targets(decisions, facts)
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(datetime.timezone.utc).isoformat()
    # Tests inject their fetchers and are hermetic by default. The real stage uses the persisted Actions
    # response-cache directory added in #233; callers can inject a temporary store to exercise fallback.
    if (snapshot_cache is None and fetch is fetch_norway and fetch_cnc is None
            and fetch_brazil is None and fetch_kmrb is None):
        snapshot_cache = cache.configured(SOURCE_CACHE_NAMESPACE, SOURCE_CACHE_DAYS)
    try:
        norway_snapshot, _ = _source_snapshot(fetch, norway_records, "Medietilsynet", NORWAY_URL, stamp,
                                              snapshot_cache, required=True)
    except http.HTTPError as refusal:
        raise UpstreamError(f"maturity: the Medietilsynet workbook could not be downloaded ({refusal})") from None
    cnc_snapshot, cnc_failure = _source_snapshot(
        fetch_cnc or maturity_public.fetch_cnc, maturity_public.cnc_records, "CNC",
        maturity_public.CNC_PAGE, stamp, snapshot_cache)
    brazil_snapshot, brazil_failure = _source_snapshot(
        fetch_brazil or maturity_public.fetch_brazil, maturity_public.brazil_records, "ClassInd",
        maturity_public.BRAZIL_PAGE, stamp, snapshot_cache)
    kmrb_snapshot, kmrb_failure = maturity_kmrb.optional_snapshot(
        _source_snapshot, stamp, snapshot_cache, fetch_kmrb)
    norway = norway_snapshot["payload"]
    cnc = cnc_snapshot["payload"] if cnc_snapshot else None
    brazil = brazil_snapshot["payload"] if brazil_snapshot else None
    kmrb = kmrb_snapshot["payload"] if kmrb_snapshot else None
    kavi_path = ctx.path(artifacts.MATURITY_KAVI)
    kavi = None
    if os.path.exists(kavi_path):
        with open(kavi_path, "rb") as handle:
            kavi = handle.read()
    kavi_rows = list(maturity_kavi.records(kavi)) if kavi is not None else []
    snapshots = [(artifacts.MATURITY_NORWAY, norway)]
    snapshots += [(artifacts.MATURITY_CNC, cnc)] if cnc is not None else []
    snapshots += [(artifacts.MATURITY_BRAZIL, brazil)] if brazil is not None else []
    snapshots += [(artifacts.MATURITY_KMRB, kmrb)] if kmrb is not None else []
    for artifact, payload in snapshots:
        raw_path = ctx.path(artifact)
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        cache.write_atomically(raw_path, payload)
    scope = "factual classification and identity fields only; prose fields excluded"
    sources = [
        {"authority": "Medietilsynet", "dataset": "Filmdatabasen 1913–2022", "sourceUrl": NORWAY_URL,
         "landingPage": NORWAY_PAGE, "reuseBasis": NORWAY_LICENSE,
         "retrievedAt": norway_snapshot["retrievedAt"],
         "sha256": hashlib.sha256(norway).hexdigest(), "bytes": len(norway), "scope": scope},
    ]
    if norway_snapshot.get("fallbackAfter"):
        sources[0]["fallbackAfter"] = norway_snapshot["fallbackAfter"]
    if cnc is not None:
        sources.append({"authority": "CNC", "dataset": "Visas d'exploitation 1945–2025",
         "sourceUrl": cnc_snapshot["sourceUrl"],
         "landingPage": maturity_public.CNC_PAGE, "reuseBasis": maturity_public.CNC_LICENSE,
         "retrievedAt": cnc_snapshot["retrievedAt"], "sha256": maturity_public.digest(cnc),
         "bytes": len(cnc), "scope": scope, **({"fallbackAfter": cnc_snapshot["fallbackAfter"]}
                                                if cnc_snapshot.get("fallbackAfter") else {})})
    if brazil is not None:
        sources.append({"authority": "ClassInd", "dataset": "Obras Audiovisuais",
         "sourceUrl": brazil_snapshot["sourceUrl"],
         "landingPage": maturity_public.BRAZIL_PAGE, "reuseBasis": maturity_public.BRAZIL_LICENSE,
         "retrievedAt": brazil_snapshot["retrievedAt"], "sha256": maturity_public.digest(brazil),
         "bytes": len(brazil), "scope": scope, **({"fallbackAfter": brazil_snapshot["fallbackAfter"]}
                                                   if brazil_snapshot.get("fallbackAfter") else {})})
    if kmrb is not None:
        sources.append(maturity_kmrb.manifest_entry(kmrb_snapshot, scope))
    if kavi is not None:
        retrieved = datetime.datetime.fromtimestamp(os.stat(kavi_path).st_mtime, datetime.timezone.utc).isoformat()
        sources.append({"authority": "KAVI", "dataset": "IKLU movie classifications",
                        "sourceUrl": maturity_kavi.API, "landingPage": maturity_kavi.GUIDE,
                        "reuseBasis": maturity_kavi.GUIDE, "retrievedAt": retrieved,
                        "sha256": hashlib.sha256(kavi).hexdigest(), "bytes": len(kavi), "scope": scope})
    failures = [row for row in (cnc_failure, brazil_failure, kmrb_failure) if row]
    _write_json(ctx.path(artifacts.MATURITY_SOURCES),
                {"schema": 1, "sources": sources, "unavailable": failures})
    authority_rows = {
        "Medietilsynet": norway_snapshot["rows"],
        "CNC": cnc_snapshot["rows"] if cnc_snapshot else None,
        "ClassInd": brazil_snapshot["rows"] if brazil_snapshot else None,
        "KMRB": kmrb_snapshot["rows"] if kmrb_snapshot else None,
        "KAVI": kavi_rows if kavi is not None else None,
    }
    for authority, rows in authority_rows.items():
        if rows is not None:
            maturity_decisions.validate_source(decisions, authority, rows)
    norway_linked, norway_audit = link_records(authority_rows["Medietilsynet"], facts, decisions)
    cnc_linked, cnc_audit = link_records(authority_rows["CNC"] or (), facts, decisions)
    brazil_linked, brazil_audit = link_records(authority_rows["ClassInd"] or (), facts, decisions)
    kmrb_linked, kmrb_audit = link_records(authority_rows["KMRB"] or (), facts, decisions)
    kavi_linked, kavi_audit = link_records(authority_rows["KAVI"] or (), facts, decisions)
    records, conflicts = combine((norway_linked, cnc_linked, brazil_linked, kmrb_linked, kavi_linked))
    out = {"schema": 1, "datasetVersion": version, "records": records,
           "derivation": {"rule": "highest verified age; exact identity or reviewed official ID only",
                          "authorityDecisionsSha256": decisions_sha,
                          "sourceSha256": {row["authority"]: row["sha256"] for row in sources},
                          "audit": {"Medietilsynet": norway_audit, "CNC": cnc_audit,
                                    "ClassInd": brazil_audit, "KMRB": kmrb_audit, "KAVI": kavi_audit,
                                    "unavailable": failures,
                                    "crossAuthorityConflicts": conflicts}}}
    _write_json(ctx.path(artifacts.MATURITY), out)
    return (f"{ctx.path(artifacts.MATURITY)} — {len(records):,} titles; "
            f"{sum(a['ambiguousRows'] for a in (norway_audit, cnc_audit, brazil_audit, kmrb_audit, kavi_audit)):,} ambiguous, "
            f"{len(conflicts):,} cross-authority conflicts")
