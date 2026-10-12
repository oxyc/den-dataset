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
import json
import os
import re
import unicodedata

from lib import cache, http
from . import artifacts, finalize, maturity_decisions, maturity_providers
from .contract import StageError, UpstreamError

NAME = "maturity"
PRODUCER = "pipeline/maturity.py"
HOW = "./den stage maturity --out-dir <dir>"
PUBLISHES = False
SPENDS = False
INPUTS = (artifacts.MANIFEST, artifacts.FACTS, artifacts.MATURITY_KAVI)
OUTPUTS = (artifacts.MATURITY_NORWAY, artifacts.MATURITY_CNC, artifacts.MATURITY_BRAZIL,
           artifacts.MATURITY_KMRB, artifacts.MATURITY_TAIWAN, artifacts.MATURITY_SINGAPORE,
           artifacts.MATURITY_QUEBEC, artifacts.MATURITY_ITALY,
           artifacts.MATURITY_SOURCES, artifacts.MATURITY)

SOURCE_CACHE_NAMESPACE = "maturity_source"
SOURCE_CACHE_DAYS = 30


def normalized(value):
    """Exact matching normalization: Unicode/case/space only, never punctuation deletion or similarity."""
    if not isinstance(value, str):
        return None
    text = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return text or None


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


def run(ctx, fetchers=None, now=None, snapshot_cache=None):
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
    # An explicit mapping is a hermetic provider set. Production uses every configured registry entry.
    injected = fetchers is not None
    fetchers = dict(fetchers or {})
    remote_authorities = {provider.authority for provider in maturity_providers.REMOTE}
    unknown = sorted(set(fetchers) - remote_authorities)
    if unknown:
        raise StageError(f"maturity: unknown provider fetchers: {unknown!r}")
    if injected:
        missing = [provider.authority for provider in maturity_providers.REMOTE
                   if provider.required and provider.authority not in fetchers]
        if missing:
            raise StageError(f"maturity: required provider fetchers missing: {missing!r}")
    if snapshot_cache is None and not injected:
        snapshot_cache = cache.configured(SOURCE_CACHE_NAMESPACE, SOURCE_CACHE_DAYS)

    snapshots, failures = {}, []
    for provider in maturity_providers.REMOTE:
        if injected and provider.authority not in fetchers:
            continue
        if not injected and not provider.configured():
            continue
        try:
            snapshot, failure = _source_snapshot(
                fetchers.get(provider.authority, provider.fetch), provider.records, provider.authority,
                provider.source_url, stamp, snapshot_cache, required=provider.required)
        except http.HTTPError as refusal:
            raise UpstreamError(
                f"maturity: the {provider.authority} source could not be downloaded ({refusal})") from None
        if snapshot is not None:
            snapshots[provider.authority] = snapshot
        if failure is not None:
            failures.append(failure)

    for provider in maturity_providers.PROVIDERS:
        if provider.acquisition != "local":
            continue
        path = ctx.path(provider.artifact)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as handle:
            payload = handle.read()
        snapshots[provider.authority] = {
            "payload": payload, "sourceUrl": provider.source_url,
            "retrievedAt": datetime.datetime.fromtimestamp(
                os.stat(path).st_mtime, datetime.timezone.utc).isoformat(),
            "rows": list(provider.records(payload)),
        }

    for provider in maturity_providers.REMOTE:
        snapshot = snapshots.get(provider.authority)
        if snapshot is None:
            continue
        raw_path = ctx.path(provider.artifact)
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        cache.write_atomically(raw_path, snapshot["payload"])

    scope = "factual classification and identity fields only; prose fields excluded"
    sources = [provider.manifest_entry(snapshots[provider.authority], scope)
               for provider in maturity_providers.PROVIDERS if provider.authority in snapshots]
    _write_json(ctx.path(artifacts.MATURITY_SOURCES),
                {"schema": 1, "sources": sources, "unavailable": failures})
    authority_rows = {provider.authority: snapshots[provider.authority]["rows"]
                      for provider in maturity_providers.PROVIDERS
                      if provider.authority in snapshots}
    for authority, rows in authority_rows.items():
        maturity_decisions.validate_source(decisions, authority, rows)
    linked, audits = [], {}
    for authority in (provider.authority for provider in maturity_providers.PROVIDERS):
        authority_linked, audits[authority] = link_records(authority_rows.get(authority, ()), facts, decisions)
        linked.append(authority_linked)
    records, conflicts = combine(linked)
    out = {"schema": 1, "datasetVersion": version, "records": records,
           "derivation": {"rule": "highest verified age; exact identity or reviewed official ID only",
                           "authorityDecisionsSha256": decisions_sha,
                           "sourceSha256": {row["authority"]: row["sha256"] for row in sources},
                           "audit": {**audits, "unavailable": failures,
                                     "crossAuthorityConflicts": conflicts}}}
    _write_json(ctx.path(artifacts.MATURITY), out)
    return (f"{ctx.path(artifacts.MATURITY)} — {len(records):,} titles; "
            f"{sum(audit['ambiguousRows'] for audit in audits.values()):,} ambiguous, "
            f"{len(conflicts):,} cross-authority conflicts")
