#!/usr/bin/env python3
"""The JSON bytes the Swift producer wrote, for the artifacts whose bytes are hashed or compared.

`labels-t02.json` is hashed into `datasetVersion`, so a second spelling of it is a new dataset version for
a reason that is not a change in any label. The Swift used two encoders with different rules, and each
artifact keeps the one it was written with:

  * **`JSONEncoder` with `.sortedKeys`** — `compact` and `pretty`. Keys in code-point order, `/` escaped,
    UTF-8 left raw, and a float in its shortest round-trip form with no `.0` on an integral value: a
    confidence of 1.0 is written `1`. Python's `json` writes `1.0`, so every label record carrying one
    would change bytes.
  * **`JSONSerialization` with `.sortedKeys`** — `manifest`, for `dataset.meta.json`, which went through
    it to keep the keys it does not model. Two differences, both measured against the binary rather than
    read off documentation: keys are ordered by ICU collation (numeric runs by value, case-insensitive —
    `sharedPlotArticles` sorts BEFORE `sharedPlotArticleTitles`, `a9` before `a10`, `_` before `-`), and a
    non-integral double is written `%.17g`, so the 0.7 another writer stamped comes back as
    `0.69999999999999996`. Every reader parses that to the same double; it is reproduced because it is
    what the file holds, not because anything needs the digits.

The collation is ICU's root order for printable ASCII, which is every key a manifest has carried. A key
outside ASCII sorts after all of it by code point — an approximation, and the only one here.
"""
import json
import re

#: ICU root collation order for printable ASCII (plus the whitespace that precedes it), with letters
#: folded to one case. Punctuation sorts before digits and digits before letters, which is why `a_b` comes
#: before `a-b` and both before `a9`.
_ICU_ORDER = "\t\n\x0b\x0c\r _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$0123456789abcdefghijklmnopqrstuvwxyz"
_RANK = {c: i for i, c in enumerate(_ICU_ORDER)}
_DIGITS = re.compile(r"(\d+)")


def _collation_key(key):
    """`JSONSerialization`'s key order: primary weights with digit runs compared as numbers and case
    ignored, then the literal string to break a tie the way `.forcedOrdering` does."""
    weights = []
    for part in _DIGITS.split(key):
        if not part:
            continue
        if part.isdigit():
            weights.append((_RANK["0"], int(part)))
            continue
        for char in part.lower():
            weights.append((_RANK.get(char, len(_RANK) + ord(char)), 0))
    return (weights, key)


def _string(value):
    return json.dumps(value, ensure_ascii=False).replace("/", r"\/")


def _swift_float(value):
    """`JSONEncoder`: shortest round-trip, and an integral value without `.0`."""
    if value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def _ns_float(value):
    """`JSONSerialization`: seventeen significant digits, which drops a trailing `.0` too."""
    return "%.17g" % value


def _scalar(value, float_format):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return float_format(value)
    if isinstance(value, str):
        return _string(value)
    raise TypeError(f"{type(value).__name__} is not a JSON value")


def _compact(value):
    if isinstance(value, dict):
        return "{" + ",".join(f"{_string(k)}:{_compact(value[k])}" for k in sorted(value)) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_compact(v) for v in value) + "]"
    return _scalar(value, _swift_float)


def _pretty(value, depth, order, float_format):
    """Two-space indent, `" : "`, and an empty container as its brackets around a blank line — which is
    how both Swift encoders print one, measured."""
    pad, inner = "  " * depth, "  " * (depth + 1)
    if isinstance(value, dict):
        if not value:
            return "{\n\n" + pad + "}"
        rows = [f"{inner}{_string(k)} : {_pretty(value[k], depth + 1, order, float_format)}"
                for k in sorted(value, key=order)]
        return "{\n" + ",\n".join(rows) + "\n" + pad + "}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[\n\n" + pad + "]"
        rows = [inner + _pretty(v, depth + 1, order, float_format) for v in value]
        return "[\n" + ",\n".join(rows) + "\n" + pad + "]"
    return _scalar(value, float_format)


def compact(value):
    """`JSONEncoder` with `.sortedKeys`. No trailing newline."""
    return _compact(value)


def pretty(value):
    """`JSONEncoder` with `[.prettyPrinted, .sortedKeys]`. No trailing newline."""
    return _pretty(value, 0, None, _swift_float)


def manifest(value):
    """`JSONSerialization` with `[.prettyPrinted, .sortedKeys]`. No trailing newline."""
    return _pretty(value, 0, _collation_key, _ns_float)


def merge_manifest(new, existing, owned):
    """`new` over the manifest already on disk, keeping the keys some other writer put there.

    A key `new` OWNS always wins — including by being absent, which is how it says "there is no sidecar".
    `owned` has to be passed in because absence alone cannot tell the two cases apart: an owned optional
    left out looked exactly like an unmodelled key, and inherited the previous run's value — a manifest
    naming a sidecar built for an older version and swearing to its old sha, which both consumers
    hard-verify. An existing file that does not parse contributes nothing, as it did in the Swift.
    """
    merged = dict(new)
    if existing:
        try:
            old = json.loads(existing)
        except ValueError:
            old = None
        if isinstance(old, dict):
            for key, value in old.items():
                if key not in merged and key not in owned:
                    merged[key] = value
    return merged
