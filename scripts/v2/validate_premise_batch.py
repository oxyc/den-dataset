#!/usr/bin/env python3
"""Check a premise-generation batch against its input, and reject the whole batch on corruption.

  scripts/v2/validate_premise_batch.py --phase out-premise-v2/gen [--batch 0000] [--strict-language]

A generating model can return a correct row COUNT while inventing ids, duplicating one key and dropping
another — `README.md` records exactly that: ids fabricated in 3 of 12 batches and a key duplicated in a 4th,
"reporting success every time". So a count is not evidence, and the only check worth running compares the
answer's key set against the input's.

Rejection is per batch, not per row. A batch that invented one id is a batch whose other rows were produced
by the same confused pass, and keeping the survivors silently mixes verified and unverified work into an
append-only artifact.

## The language check

`data/premise-tags-v1.SPEC.md` requires English tags whatever the plot's language, because this index is one
shared vocabulary — a tag nobody else can emit matches nothing. That rule is new, and 79% of the current
worklist is non-English (de 20%, it 18%, fr 16%), so it is the rule most likely to be ignored and the one
whose failure is invisible in a row count.

Detection is deliberately a heuristic and deliberately loud: non-ASCII letters are a certainty, and beyond
that we flag tags carrying diacritics or characteristic non-English function words. It cannot catch a
plausible-looking German compound rendered in ASCII, so `--strict-language` exists to fail on any hit rather
than warn, and a human should read a sample of non-English titles' output regardless.
"""
import argparse
import json
import os
import re
import sys
import unicodedata

TAG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MIN_TAGS, MAX_TAGS = 8, 12
# Function words that would only appear inside a tag left in its source language.
FOREIGN_HINT = re.compile(
    r"(?:^|-)(?:der|die|das|und|mit|eine?[nrsm]?|von|zum|zur|im|auf|für"
    r"|le|la|les|une?|des|du|dans|pour|avec|sur"
    r"|el|los|las|una?|del|por|para|con"
    r"|il|lo|gli|una|dei|nel|per|col"
    r"|de|het|een|van|voor)(?:-|$)")


def has_non_ascii(tag):
    return any(ord(c) > 127 for c in tag)


def has_diacritic(tag):
    return any(unicodedata.combining(c) for c in unicodedata.normalize("NFD", tag))


def check(batch_in, batch_out, strict_language):
    """Returns a list of problems. Empty means the batch is usable."""
    bad = []
    if not isinstance(batch_out, list):
        return ["output is not a JSON array"]

    want = [r["key"] for r in batch_in]
    got = [r.get("key") for r in batch_out if isinstance(r, dict)]
    if len(got) != len(batch_out):
        bad.append("some rows are not objects")
    if len(set(got)) != len(got):
        dupes = sorted({k for k in got if got.count(k) > 1})
        bad.append(f"duplicated keys: {dupes}")
    invented = sorted(set(got) - set(want))
    missing = sorted(set(want) - set(got))
    if invented:
        bad.append(f"invented keys not in the input: {invented}")
    if missing:
        bad.append(f"keys dropped from the input: {missing}")

    for row in batch_out:
        if not isinstance(row, dict):
            continue
        key, tags = row.get("key"), row.get("tags")
        if not isinstance(tags, list) or not tags:
            bad.append(f"{key}: no tags")
            continue
        if not MIN_TAGS <= len(tags) <= MAX_TAGS:
            bad.append(f"{key}: {len(tags)} tags, spec says {MIN_TAGS}-{MAX_TAGS}")
        for tag in tags:
            if not isinstance(tag, str) or not tag.strip():
                bad.append(f"{key}: empty tag")
            elif not TAG.match(tag):
                bad.append(f"{key}: {tag!r} is not lowercase-kebab-case")
            elif has_non_ascii(tag) or has_diacritic(tag):
                bad.append(f"{key}: {tag!r} is not English (non-ASCII)")
            elif FOREIGN_HINT.search(tag):
                problem = f"{key}: {tag!r} looks like it was left in the source language"
                bad.append(problem) if strict_language else print(f"  warn {problem}", file=sys.stderr)
    return bad


ap = argparse.ArgumentParser()
ap.add_argument("--phase", required=True, help="the gen/ directory holding in/ and out/")
ap.add_argument("--batch", help="one batch number, e.g. 0000 (default: every batch with an output)")
ap.add_argument("--strict-language", action="store_true",
                help="fail on a suspected source-language tag rather than warning")
args = ap.parse_args()

in_dir, out_dir = os.path.join(args.phase, "in"), os.path.join(args.phase, "out")
names = ([f"batch-{args.batch}.json"] if args.batch
         else sorted(n for n in os.listdir(out_dir) if n.startswith("batch-")))

rejected, ok, rows = [], 0, 0
for name in names:
    out_path = os.path.join(out_dir, name)
    if not os.path.exists(out_path):
        rejected.append((name, ["no output written"]))
        continue
    try:
        batch_out = json.load(open(out_path, encoding="utf-8"))
    except Exception as exc:
        rejected.append((name, [f"unparseable: {exc}"]))
        continue
    batch_in = json.load(open(os.path.join(in_dir, name), encoding="utf-8"))
    problems = check(batch_in, batch_out, args.strict_language)
    if problems:
        rejected.append((name, problems))
    else:
        ok += 1
        rows += len(batch_out)

for name, problems in rejected:
    print(f"REJECT {name}")
    for p in problems[:6]:
        print(f"    {p}")
    if len(problems) > 6:
        print(f"    … and {len(problems) - 6} more")

print(json.dumps({"checked": len(names), "accepted": ok, "rejected": len(rejected), "rowsAccepted": rows}))
sys.exit(1 if rejected else 0)
