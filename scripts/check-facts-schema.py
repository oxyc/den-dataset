#!/usr/bin/env python3
"""Check a facts file against the shapes atlas actually deserialises into.

A producer can exist, be committed, be run by the right command — and still emit a shape its consumer cannot
read. That is not the failure `check-producers.py` guards against, and it happened: the entity map shipped

    "aliases": "Adrian Anthony Lester"

where atlas types `RawEntity.aliases` as `Vec<String>`. Serde rejected it at the FIRST entity, byte 3127 of a
27 MB file, and atlas does not partially load a facts file — it dropped the whole thing and ran
`facts_unusable`, losing people search, imdbId, countries, /recommend and the entire basedOn feature. One
character, in one field, of one of 144,820 entities.

Nothing caught it before publish. The blob had a producer, the record counts were right, the shas matched,
the gzip was valid, and every existing guard passed. What no guard did was read it the way atlas does.

## Kept honest against the consumer

Every rule below cites the Rust type it mirrors (den-atlas/src/facts.rs). When atlas changes a field's type,
this file is where the change is felt — and a failure here is cheap, where a failure in production is not.

    scripts/check-facts-schema.py <facts.json> [...]
"""
import json
import sys

# field -> the JSON shape atlas requires. Mirrors `RawRecord` and `RawEntity` in den-atlas/src/facts.rs.
#
# "list"        -> Option<Vec<String>>      a scalar here is the bug this script exists for
# "one_or_many" -> Option<OneOrMany>        scalar OR list, both accepted (imdbId, franchise)
# "str"         -> Option<String>
# "obj"         -> a nested object
RECORD_SHAPES = {
    "mediaType": "str", "tmdbId": "int", "hasVector": "bool",
    "imdbId": "one_or_many", "franchise": "one_or_many",
    "genres": "list", "countries": "list", "productionCountries": "list", "languages": "list",
    "directors": "list", "creators": "list", "cast": "list", "broadcaster": "list",
    "basedOn": "list", "basedOnKind": "list",
    "released": "obj", "started": "obj", "titles": "obj",
}
# `RawEntity`: en/tmdbPersonId are strings, aliases is a LIST.
ENTITY_SHAPES = {"en": "str", "tmdbPersonId": "str", "aliases": "list"}


def wrong(value, shape):
    """The reason `value` does not fit `shape`, or None when it does."""
    if value is None:
        return None
    if shape == "list":
        if isinstance(value, list):
            return None if all(isinstance(v, str) for v in value) else "a list of non-strings"
        return f"{type(value).__name__}, expected a list"
    if shape == "one_or_many":
        if isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            return None
        return f"{type(value).__name__}, expected a string or a list of them"
    if shape == "str":
        return None if isinstance(value, str) else f"{type(value).__name__}, expected a string"
    if shape == "int":
        return None if isinstance(value, int) and not isinstance(value, bool) else "expected an integer"
    if shape == "bool":
        return None if isinstance(value, bool) else "expected a boolean"
    if shape == "obj":
        return None if isinstance(value, dict) else f"{type(value).__name__}, expected an object"
    return None


def check(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    problems = []

    def note(where, field, value, reason):
        # Show the offending value, truncated: "aliases: str, expected a list" is far less use than seeing
        # the string sitting there.
        problems.append(f"{where}.{field}: {reason} — {json.dumps(value, ensure_ascii=False)[:60]}")

    for record in doc.get("records", []):
        where = f"records[{record.get('mediaType')}:{record.get('tmdbId')}]"
        for field, shape in RECORD_SHAPES.items():
            reason = wrong(record.get(field), shape)
            if reason:
                note(where, field, record[field], reason)
        # An unknown field is not an error — atlas ignores what it does not deserialise — so nothing is said
        # about the properties the slim file drops.
        if len(problems) > 20:
            break

    for qid, entity in doc.get("entities", {}).items():
        if not isinstance(entity, dict):
            problems.append(f"entities.{qid}: {type(entity).__name__}, expected an object")
            continue
        for field, shape in ENTITY_SHAPES.items():
            reason = wrong(entity.get(field), shape)
            if reason:
                note(f"entities.{qid}", field, entity[field], reason)
        if len(problems) > 20:
            break

    return problems


def main():
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    failed = False
    for path in paths:
        problems = check(path)
        if problems:
            failed = True
            print(f"error: {path} does not match the shapes atlas reads:", file=sys.stderr)
            for problem in problems[:20]:
                print(f"       - {problem}", file=sys.stderr)
            if len(problems) > 20:
                print(f"       … and more", file=sys.stderr)
        else:
            print(f"ok: {path}")
    if failed:
        print("       atlas does not partially load a facts file — one bad field discards all of it.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
