#!/usr/bin/env python3
"""Build `facts-slim-<version>.json` — the facts file atlas actually loads.

Nothing in this repo produced this blob until now. It was generated once, elsewhere, and carried forward by
every publish since, which is how it came to hold exactly the 13 fields atlas parsed *at that moment* — and
why `basedOn` could be added to the full file, published, and still be invisible to `/recommend`. The full
file grew; the slim one could not.

That is the same failure the README already names: an artifact whose producer is not committed gets described
wrongly, and drifts silently from the thing it is derived from.

## What "slim" means

The full facts file carries all 24 scraped properties for every title, most of which atlas never reads. The
slim file keeps the fields its parser declares (`den-atlas/src/facts.rs`, `struct RawRecord`) and drops the
rest, along with the `entities` map entries nothing references. Atlas prefers it purely for load time and
memory.

**KEEP is therefore a mirror of atlas's parser, not a judgement.** When atlas learns a field, it goes here in
the same change — otherwise the field is published and unreachable, which is the bug this script exists to
stop happening twice.

    scripts/build-facts-slim.py <facts-full.json> <facts-slim.json>
"""
import json
import sys

# Mirrors `RawRecord` in den-atlas/src/facts.rs. Each entry is a field atlas deserialises; adding one here
# without adding it there wastes bytes, adding it there without adding it here silently does nothing.
KEEP = (
    "mediaType", "tmdbId", "hasVector",
    "imdbId", "released", "started",
    "genres", "countries", "productionCountries", "languages",
    "directors", "creators", "cast", "franchise", "broadcaster",
    # Authorship is the heaviest weight in the rail's scorer and these two were scraped, then dropped here,
    # so atlas never saw them: screenwriters 67.2% coverage, productionCompanies 44.6%. David Simon is
    # credited on We Own This City and The Plot Against America only via `screenwriters`; `creators` links
    # him to The Deuce alone.
    "screenwriters", "productionCompanies",
    "titles",
    # What a title was adapted from: the Q-ids link adaptations of one source to each other, the kinds are
    # what "based on a book" needs. 7,279 records carry the first, 7,271 the second.
    "basedOn", "basedOnKind",
    # Minutes (P2047) — 85% overall, 91% of films. Answers "something short tonight", which nothing could.
    # A SERIES' value is per episode, so the two media types are not comparable and atlas judges only films.
    "runtimeMinutes",
)


def main():
    source_path, dest_path = sys.argv[1], sys.argv[2]
    with open(source_path, encoding="utf-8") as fh:
        full = json.load(fh)

    records = [{k: r[k] for k in KEEP if k in r} for r in full["records"]]

    # Entities are Q-id → name. Keep only the ones a kept field still references: the full map is ~145k
    # entries and most of them name properties the slim records no longer carry.
    referenced = set()
    for record in records:
        for key, value in record.items():
            if isinstance(value, list):
                referenced.update(v for v in value if isinstance(v, str) and v.startswith("Q"))
            elif isinstance(value, str) and value.startswith("Q"):
                referenced.add(value)
    entities = {q: v for q, v in full.get("entities", {}).items() if q in referenced}

    slim = {
        "schema": full["schema"],
        "datasetVersion": full["datasetVersion"],
        "genreMap": full.get("genreMap", {}),
        "entities": entities,
        "records": records,
    }

    # A slim file with fewer records than the full one is a bug, not a saving: atlas would lose those titles
    # entirely, and the loss is invisible from every other artifact.
    if len(records) != len(full["records"]):
        sys.exit(f"record count changed: {len(full['records'])} -> {len(records)}")

    with open(dest_path, "w", encoding="utf-8") as fh:
        json.dump(slim, fh)

    dropped = sorted({k for r in full["records"][:2000] for k in r} - set(KEEP))
    print(json.dumps({
        "records": len(records),
        "entities": f"{len(entities)} of {len(full.get('entities', {}))}",
        "droppedFields": dropped,
        "path": dest_path,
    }))


if __name__ == "__main__":
    main()
