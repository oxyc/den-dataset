#!/usr/bin/env python3
"""Aggregate Wikidata and private runtime-TMDB certification coverage (oxyc/den-dataset#226).

The output is deliberately incapable of carrying a title key or a certification value: title-level source
answers live only in memory, and ``aggregate`` constructs the report from counters.  In particular, do not
add a raw/debug/checkpoint output to this tool.  TMDB's release-date/content-rating answers and anything
derived for one title may not enter a published dataset or a file that leaves the measurement box.
"""
import argparse
import collections
import concurrent.futures
import datetime
import gzip
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import cache, tmdb, wikidata  # noqa: E402


COUNTRY_PROPERTY = {"US": ("P1657", "P2363"), "GB": ("P2629",), "DE": ("P1981",)}
# Only labels with a defensible numeric boundary are mapped. Advisory labels such as US/GB ``PG`` do not
# state an age and deliberately remain unknown: guessing a low age is worse than losing coverage. Values
# describe the age at which the title is eligible without a parent; accompaniment is profile/session
# policy, not part of this title fact.
AGES = {
    "US": {"G": 0, "TV-Y": 0, "TV-G": 0, "TV-Y7": 7, "TV-Y7-FV": 7,
           "PG-13": 13, "TV-14": 14, "R": 17, "NC-17": 18, "TV-MA": 17},
    "GB": {"U": 0, "12": 12, "12A": 12, "15": 15, "18": 18, "R18": 18},
    "DE": {"0": 0, "FSK 0": 0, "6": 6, "FSK 6": 6, "12": 12, "FSK 12": 12,
           "16": 16, "FSK 16": 16, "18": 18, "FSK 18": 18},
}
AGE_CEILINGS = (0, 6, 7, 9, 10, 12, 13, 14, 15, 16, 17, 18)


def _open(path, mode="rt"):
    return gzip.open(path, mode, encoding="utf-8") if path.endswith(".gz") else open(path, mode, encoding="utf-8")


def load_titles(labels_path, facts_path, exports):
    """The corpus spine plus publishable year and ephemeral popularity; never a certification fact."""
    with _open(labels_path) as fh:
        records = json.load(fh).get("records")
    if not isinstance(records, list) or not records:
        raise ValueError(f"{labels_path}: expected a non-empty records list")
    years = {}
    with _open(facts_path) as fh:
        facts = json.load(fh).get("records")
    if not isinstance(facts, list):
        raise ValueError(f"{facts_path}: expected a records list")
    for row in facts:
        key = f"{row.get('mediaType')}:{row.get('tmdbId')}"
        value = row.get("started") or row.get("released")
        match = re.match(r"^[+-]?(\d{1,4})-", str((value or {}).get("date", "")))
        years[key] = int(match.group(1)) if match else None
    popularity = {}
    for media, path in exports.items():
        if not path:
            continue
        with _open(path) as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    popularity[f"{media}:{int(row['id'])}"] = float(row.get("popularity") or 0)
                except (ValueError, KeyError, TypeError):
                    raise ValueError(f"{path}: malformed TMDB export row") from None
    titles = []
    for row in records:
        media, ident = row.get("mediaType"), row.get("tmdbId")
        if media not in ("movie", "tv") or not isinstance(ident, int):
            raise ValueError(f"{labels_path}: malformed label record")
        key = f"{media}:{ident}"
        titles.append({"media": media, "id": ident, "year": years.get(key), "popularity": popularity.get(key)})
    ranked = sorted((r for r in titles if r["popularity"] is not None),
                    key=lambda r: (-r["popularity"], r["media"], r["id"]))
    for index, row in enumerate(ranked):
        row["percentile"] = index / len(ranked)
    return titles


def _parse_sparql(payload):
    try:
        rows = json.loads(payload.decode("utf-8"))["results"]["bindings"]
    except (ValueError, KeyError, TypeError):
        raise wikidata.WikidataError(f"certification query was not a SPARQL result") from None
    out = collections.defaultdict(lambda: collections.defaultdict(set))
    for row in rows:
        try:
            out[int(row["tmdb"]["value"])][row["country"]["value"]].add(row["value"]["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def wikidata_query(ids, media, excluded=None):
    values = " ".join(f'"{int(value)}"' for value in sorted(set(ids)))
    clauses = []
    for country, properties in COUNTRY_PROPERTY.items():
        path = "|".join(f"wdt:{prop}" for prop in properties)
        clauses.append(f'{{ ?film ({path}) ?rating . BIND("{country}" AS ?country) }}')
    union = " UNION\n  ".join(clauses)
    return (f"SELECT DISTINCT ?tmdb ?country ?value WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{wikidata.ID_PROPERTY[media]} ?tmdb .\n"
            f"{wikidata.exclusion(ids, excluded)}"
            f"  {union}\n"
            f"  OPTIONAL {{ ?rating rdfs:label ?english . FILTER(LANG(?english) = \"en\") }}\n"
            f"  BIND(COALESCE(?english, STR(?rating)) AS ?value)\n"
            f"}} ORDER BY ?tmdb ?country ?value")


def fetch_wikidata(titles, ask=wikidata.sparql):
    """Return title-level values in memory. Only ``aggregate`` may serialize their consequences."""
    found = collections.defaultdict(lambda: collections.defaultdict(set))
    wd_cache = wikidata.cache_for()
    for media in ("movie", "tv"):
        ids = [row["id"] for row in titles if row["media"] == media]
        for start in range(0, len(ids), wikidata.TMDB_CHUNK):
            batch = ids[start:start + wikidata.TMDB_CHUNK]
            resolution = wikidata.resolve(batch, media, wd_cache)
            payload = ask(wikidata_query(batch, media, wikidata.set_aside(resolution)), fallback=True)
            for ident, countries in _parse_sparql(payload).items():
                for country, values in countries.items():
                    found[(media, ident)][country].update(values)
    return found


def tmdb_values(media, body):
    """Normalize the two TMDB endpoint shapes to ``country -> raw values`` in memory."""
    out = collections.defaultdict(set)
    if not isinstance(body.get("results"), list):
        raise tmdb.TMDBError(f"/{media} certification response carries no `results` list")
    if media == "movie":
        for release in body["results"]:
            country = release.get("iso_3166_1")
            for row in release.get("release_dates") or []:
                if country and row.get("certification"):
                    out[country].add(row["certification"])
    else:
        for row in body["results"]:
            if row.get("iso_3166_1") and row.get("rating"):
                out[row["iso_3166_1"]].add(row["rating"])
    return out


def fetch_tmdb(titles, client=None, workers=8, pause_ms=0):
    """Fetch runtime-only certification values. There is intentionally no title-level checkpoint/output."""
    client = client or tmdb.TMDB()

    def one(row):
        if pause_ms:
            time.sleep(pause_ms / 1000)
        endpoint = "release_dates" if row["media"] == "movie" else "content_ratings"
        return (row["media"], row["id"]), tmdb_values(row["media"], client.get(
            f"/{row['media']}/{row['id']}/{endpoint}"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, titles))


def minimum_age(country, value):
    text = re.sub(r"\s+", " ", str(value).strip().upper().replace("–", "-").replace("—", "-"))
    text = re.sub(r"^(?:RATED|BBFC)\s+", "", text)
    return AGES.get(country, {}).get(text)


def _groups(title, as_of):
    yield "all"
    yield f"media:{title['media']}"
    percentile = title.get("percentile")
    popularity = ("missing" if percentile is None else "top10" if percentile < .10 else
                  "next15" if percentile < .25 else "middle25" if percentile < .50 else "bottom50")
    yield f"popularity:{popularity}"
    year = title.get("year")
    recency = ("unknown" if year is None else "future" if year > as_of.year else
               "last5" if year >= as_of.year - 4 else "last10" if year >= as_of.year - 9 else "older")
    yield f"recency:{recency}"


def _observed(values, region):
    countries = {country: set(raw) for country, raw in (values or {}).items() if raw}
    # A certification from another market is not evidence about this viewer. Presence in the requested
    # region wins even when none of its values map; only absence permits the US fallback. That makes an
    # unknown regional value fail closed instead of being silently replaced by a more permissive US one.
    effective_country = region if countries.get(region) else "US" if countries.get("US") else None
    raw = countries.get(effective_country, ()) if effective_country else ()
    effective = {mapped for value in raw if (mapped := minimum_age(effective_country, value)) is not None}
    maximum = max(effective) if effective else None
    return {"any": bool(countries), "region": bool(countries.get(region)),
            "usFallback": effective_country == "US" and region != "US", "ages": effective,
            "onlyUnknown": bool(raw) and not effective, "minimumAge": maximum}


def aggregate(titles, wikidata_values, tmdb_runtime_values, region="US", as_of=None, generated_at=None):
    as_of = as_of or datetime.date.today()
    generated_at = generated_at or datetime.datetime.now(datetime.timezone.utc)
    counters = {source: collections.defaultdict(collections.Counter) for source in ("wikidata", "tmdbRuntime")}
    overlap = collections.Counter()
    for title in titles:
        key = (title["media"], title["id"])
        observations = {"wikidata": _observed(wikidata_values.get(key), region),
                        "tmdbRuntime": _observed(tmdb_runtime_values.get(key), region)}
        for source, observed in observations.items():
            for group in _groups(title, as_of):
                count = counters[source][group]
                count["titles"] += 1
                count["anyCertification"] += observed["any"]
                count["viewerRegion"] += observed["region"]
                count["usFallback"] += observed["usFallback"]
                count["onlyUnknownOrUnmapped"] += observed["onlyUnknown"]
                for age in observed["ages"]:
                    count[f"age:{age}"] += 1
                for ceiling in AGE_CEILINGS:
                    count[f"eligibleAt:{ceiling}"] += (observed["minimumAge"] is not None and
                                                        observed["minimumAge"] <= ceiling)
        wd, runtime = observations["wikidata"]["any"], observations["tmdbRuntime"]["any"]
        overlap["both"] += wd and runtime
        overlap["wikidataOnly"] += wd and not runtime
        overlap["tmdbRuntimeOnly"] += runtime and not wd
        overlap["neither"] += not wd and not runtime

    def complete(count):
        return {name: int(count.get(name, 0)) for name in
                ("titles", "anyCertification", "viewerRegion", "usFallback", "onlyUnknownOrUnmapped",
                 *(f"age:{age}" for age in AGE_CEILINGS),
                 *(f"eligibleAt:{age}" for age in AGE_CEILINGS))}

    return {
        "schemaVersion": 2,
        "generatedAt": generated_at.astimezone(datetime.timezone.utc).isoformat(),
        "scope": {"titles": len(titles), "viewerRegion": region, "asOf": as_of.isoformat()},
        "definitions": {
            "licensingBoundary": "TMDB title-level certifications and derived values remain in memory; this report contains aggregate counts only.",
            "viewerRegion": "title has any raw certification stated for the requested country",
            "usFallback": "requested region has no certification and the title has a raw US certification",
            "ageCounts": "a title is counted once at every exact numeric age mapped from the selected viewer-region values, or US values only when the region is absent",
            "eligibleAt": "the highest mapped age selected from viewer-region values, or US values only when the region is absent, is at most the named ceiling; unknown/unmapped is excluded",
            "popularity": "percentile among corpus titles present in the supplied TMDB daily exports",
            "recency": "Wikidata release/start year relative to asOf",
        },
        "collection": {
            "wikidata": {"license": "CC0 publishable facts", "propertiesByCountry": COUNTRY_PROPERTY,
                           "method": "batched SPARQL joined through the corpus TMDB identifiers"},
            "tmdbRuntime": {"licenseBoundary": "private runtime signal; aggregate counts are the only output",
                             "method": "movie release_dates and TV content_ratings detail endpoints; no title-level cache or checkpoint"},
        },
        "sources": {source: {group: complete(value) for group, value in sorted(groups.items())}
                    for source, groups in counters.items()},
        "overlap": {name: int(overlap.get(name, 0)) for name in
                    ("both", "wikidataOnly", "tmdbRuntimeOnly", "neither")},
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", required=True, help="labels-t02.json: the exact corpus spine")
    parser.add_argument("--facts", required=True, help="facts-<version>.json: publishable Wikidata dates")
    parser.add_argument("--movie-export", help="TMDB daily movie export (.json or .json.gz), for popularity")
    parser.add_argument("--tv-export", help="TMDB daily TV export (.json or .json.gz), for popularity")
    parser.add_argument("--region", default="US", choices=sorted(COUNTRY_PROPERTY))
    parser.add_argument("--as-of", type=datetime.date.fromisoformat, default=datetime.date.today())
    parser.add_argument("--generated-at", type=datetime.datetime.fromisoformat,
                        help="report timestamp (ISO-8601); supply it to make the timestamp deterministic")
    parser.add_argument("--limit", type=int, help="safe smoke-test prefix; omit for the whole corpus")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--pause-ms", type=int, default=0)
    parser.add_argument("--out", help="aggregate JSON report; default stdout")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.workers < 1 or args.pause_ms < 0 or (args.limit is not None and args.limit < 1):
        raise SystemExit("--workers and --limit must be positive; --pause-ms must not be negative")
    if args.generated_at is not None and args.generated_at.tzinfo is None:
        raise SystemExit("--generated-at must include a UTC offset")
    titles = load_titles(args.labels, args.facts, {"movie": args.movie_export, "tv": args.tv_export})
    if args.limit:
        titles = titles[:args.limit]
    report = aggregate(titles, fetch_wikidata(titles),
                       fetch_tmdb(titles, workers=args.workers, pause_ms=args.pause_ms), args.region, args.as_of,
                       args.generated_at)
    body = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        cache.write_atomically(args.out, body.encode("utf-8"))
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
