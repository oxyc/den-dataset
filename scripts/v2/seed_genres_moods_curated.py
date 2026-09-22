#!/usr/bin/env python3
"""Seed `data/genres-moods-curated.json` from `labels-t02.json`, with the September phase's subgenres &
moods replaced by ones derived from the classify pass.

  scripts/v2/seed_genres_moods_curated.py \
      --labels out-repass/labels-t02.json --phase out-repass/classify \
      --combined out-repass/combined-v1-r2.jsonl \
      --combined out-repass/combined-v1-r2-token-fallback.jsonl \
      --combined out-repass/combined-v1-r2-token-fallback-2.jsonl \
      --out out-repass/labels-t02.json --curated data/genres-moods-curated.json

Genres & moods (per title: a primary genre, up to three subgenres/themes and up to three moods, each with
a confidence) have no labeller that can rebuild them: the July answers came from Claude Code subagents,
not from code. So the trusted copy is a committed source file, and this script is the one-off that made
it. `--out` is the out-dir's `labels-t02.json`, updated with the same swap so the next build uses it until
the stages read the curated file.

## Why the swap (oxyc/den-dataset#56)

The 9,010 titles `merge_classify_labels.py` added in September were labelled by Claude subagents, and on
the golden set their subgenres and moods score well below the July records (subgenre 0.674 vs 0.778 micro
F1, mood 0.486 vs 0.669). On those titles the classify pass's answers score better, and swapping them in
lifts the whole golden set on both families. Their primary genre is kept: there the September phase
holds up and classify's Choice does not.

## Which titles

Exactly the keys in the phase's answer batches, `<phase>/out/batch-*.json`: the rows
`merge_classify_labels.py` folded in. That is the phase's own record of what it labelled, not a heuristic
such as "not in the pre-classify copy". A phase key missing from the labels is refused; the merge put
every one of them there, so a missing one means the wrong labels file or the wrong phase. Every other
record predates September and is the July relabel's.

## The rule

From each September title's classify row: every subgenre/theme Noul and every mood Noul at or above 0.8,
strongest first (ties by label), at most three per field, with the Noul as the confidence. Subgenres and
themes share one field and one cap, as they do in the labels. A field classify leaves empty stays empty:
keeping the subagents' labels there instead scored lower on the golden set in both families (subgenre
0.7713/0.7801 vs 0.7727/0.7816 micro/macro, mood 0.6448/0.5718 vs 0.6505/0.5770).

## Which classify row, when a title was classified again

The corpus join's rule (`consolidate_corpus.latest`): a title answered in several shards takes the row of
the run that started latest, by the `runStartedAt` in each shard's manifest, and a title withdrawn in
`--withdrawn` loses every row from a run that started before its withdrawal. So a September title
re-classified on a corrected article derives from the new answers, and one that lost its plot has no
classify row and is refused below.

## Re-deriving a few titles: `--keys`

  scripts/v2/seed_genres_moods_curated.py --keys reclassified.txt \
      --labels out-repass/labels-t02.json --phase out-repass/classify \
      --combined out-repass/combined-v1-r2.jsonl … --combined out-reground/combined-v1-r2.jsonl \
      --out out-repass/labels-t02.json --curated data/genres-moods-curated.json

rewrites the subgenres and moods of the listed titles only, in both files, and leaves every other title's
bytes alone — including the curated file's entries that `./den genres-moods merge` wrote, which a full
re-seed from the labels would overwrite. Every listed title must be a September title whose curated
entry is still `classify-swap`: the rule only ever applied to those, and a title the enrichment has since
relabelled is not this script's to take back.

## Refused rather than guessed

- a September title (or listed title) with no classify row;
- a key twice in one shard, or in two shards whose runs are not ordered by their manifests (two answers,
  no way to pick);
- a classify row whose taxonomy answers are not exactly the taxonomy's Nouls, each a number: a row asked
  under another taxonomy would map probabilities onto the wrong labels.

Both files are written to a temporary file beside the target and renamed over it, so a failed run leaves
the old one whole.
"""
import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import consolidate_corpus as cc  # noqa: E402  — the corpus join's supersede rule and tombstones
from combined_questions import taxonomy_questions  # noqa: E402

THRESHOLD, CAP = 0.8, 3
JULY, SEPTEMBER, SWAP = "july-relabel", "september-subagents", "classify-swap"
SOURCES = {
    JULY: "The July relabel: Claude Code subagents (Opus on the most-voted titles, Sonnet on the rest), "
          "one pass, assembled at 0.55 subgenre / 0.50 theme / 0.55 mood, top 3.",
    SEPTEMBER: "The September labelling phase (scripts/v2/merge_classify_labels.py): Claude subagents, 0.5 "
               "floor, top 3. Only its primary genres and `animated` are kept.",
    SWAP: f"Derived from the classify pass (combined-v1-r2): every subgenre/theme and mood Noul >= "
          f"{THRESHOLD}, strongest first, top {CAP} per field, the Noul as the confidence (#56).",
}


def key_of(row):
    return f"{row['mediaType']}:{row['tmdbId']}"


def september_keys(phase):
    """The keys the September phase answered for, from its answer batches."""
    out_dir = os.path.join(phase, "out")
    names = sorted(n for n in os.listdir(out_dir) if n.startswith("batch-") and n.endswith(".json"))
    if not names:
        sys.exit(f"{out_dir}: no batch-*.json, so not a labelling phase's output")
    keys = set()
    for name in names:
        with open(os.path.join(out_dir, name), encoding="utf-8") as fh:
            keys |= {row["key"] for row in json.load(fh)}
    return keys


def classify_answers(paths, wanted, withdrawals=None):
    """`(key → answers for every wanted key that has a row, the join's shard report)`, under the corpus
    join's supersede rule and tombstones."""
    rows, report, _ = cc.latest(paths, "combined", withdrawals,
                                keep=lambda r: (r.get("answers") or {}) if key_of(r) in wanted else None)
    return {k: rows[k] for k in wanted if k in rows}, report


def derive(answers, mapping):
    """Subgenres and moods from one title's classify answers, under the rule above."""
    asked = {q for q in answers if q.startswith("tax__")}
    if asked != set(mapping):
        raise ValueError(f"taxonomy answers do not match the taxonomy (missing "
                         f"{sorted(set(mapping) - asked)[:3]}, extra {sorted(asked - set(mapping))[:3]})")
    ranked = []
    for q, meta in mapping.items():
        p = (answers[q] or {}).get("noul")
        if not isinstance(p, (int, float)) or isinstance(p, bool):
            raise ValueError(f"{q}: noul is {p!r}, not a number")
        ranked.append((-p, meta["label"], "moods" if meta["family"] == "moods" else "subgenres", p))
    out = {"subgenres": [], "moods": []}
    for _, label, field, p in sorted(ranked):
        if p >= THRESHOLD and len(out[field]) < CAP:
            out[field].append({"confidence": p, "label": label})
    return out


def rederive(blob, keys, answers, mapping):
    """Rewrite the subgenres and moods of `keys`' records in `blob` in place; return the records by key
    and the counts."""
    records = blob["records"]
    by_key = {key_of(r): r for r in records}
    if len(by_key) != len(records):
        sys.exit("refusing: the labels file holds a key twice")
    absent = sorted(keys - set(by_key))
    if absent:
        sys.exit(f"refusing: {len(absent)} September titles are not in the labels, e.g. {absent[:4]}")
    missing = sorted(keys - set(answers))
    if missing:
        sys.exit(f"refusing: {len(missing)} September titles have no classify row, e.g. {missing[:4]}")
    counts = {"changed": 0, "unchanged": 0,
              "noSubgenresAtThreshold": 0, "noMoodsAtThreshold": 0, "neitherAtThreshold": 0}
    bad = []
    for key in sorted(keys):
        try:
            new = derive(answers[key], mapping)
        except ValueError as e:
            bad.append(f"{key}: {e}")
            continue
        rec = by_key[key]
        changed = (rec["subgenres"], rec["moods"]) != (new["subgenres"], new["moods"])
        rec["subgenres"], rec["moods"] = new["subgenres"], new["moods"]
        counts["changed" if changed else "unchanged"] += 1
        counts["noSubgenresAtThreshold"] += not new["subgenres"]
        counts["noMoodsAtThreshold"] += not new["moods"]
        counts["neitherAtThreshold"] += not new["subgenres"] and not new["moods"]
    if bad:
        sys.exit(f"refusing: {len(bad)} classify rows are not usable, e.g. {bad[:3]}")
    return by_key, counts


def swap(blob, sept, answers, mapping):
    """Rewrite `blob`'s September records in place and return the curated entries and the counts."""
    records = blob["records"]
    _, rederived = rederive(blob, sept, answers, mapping)
    counts = {"july": len(records) - len(sept), "september": len(sept), **rederived}
    curated = {}
    for rec in records:
        key = key_of(rec)
        curated[key] = {"animated": rec["animated"], "moods": rec["moods"], "primaryGenre": rec["primaryGenre"],
                        "primaryGenreSource": SEPTEMBER if key in sept else JULY,
                        "source": SWAP if key in sept else JULY, "subgenres": rec["subgenres"]}
    return curated, counts


def dump_labels(blob):
    """The labels file's own encoding (Swift's JSONEncoder: compact, sorted keys, `/` escaped), so the
    records this does not touch come out byte-identical."""
    return json.dumps(blob, separators=(",", ":"), sort_keys=True, ensure_ascii=False).replace("/", "\\/")


def dump_curated(curated, taxonomy_version):
    """One title per line, so a change to a title is a one-line diff."""
    head = {"_": "Genres & moods per title, keyed mediaType:tmdbId. `source` says where a title's subgenres "
                 "and moods came from, `primaryGenreSource` where its primary genre and `animated` came from. "
                 "Seeded by scripts/v2/seed_genres_moods_curated.py (oxyc/den-dataset#56).",
            "count": len(curated), "sources": SOURCES, "taxonomyVersion": taxonomy_version}
    return encode_curated(head, curated)


def encode_curated(head, titles):
    """The curated file's bytes for a head and its titles, in the titles' own order."""
    lines = [json.dumps(head, ensure_ascii=False, sort_keys=True)[:-1] + ',"titles":{']
    items = list(titles.items())
    for i, (key, entry) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f"{json.dumps(key)}:{json.dumps(entry, ensure_ascii=False, sort_keys=True)}{comma}")
    lines.append("}}")
    return "\n".join(lines) + "\n"


def write_atomically(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True, help="labels-t02.json to read")
    ap.add_argument("--phase", required=True, help="the September labelling phase (its out/batch-*.json)")
    ap.add_argument("--combined", required=True, action="append",
                    help="a classify shard; repeat for each (incl. the token-fallback shards)")
    ap.add_argument("--out", required=True, help="where to write the updated labels; may be --labels itself")
    ap.add_argument("--curated", required=True,
                    help="where to write the curated genres & moods; with --keys, read and updated in place")
    ap.add_argument("--withdrawn", help="the corpus join's tombstones; a withdrawn title has no classify row")
    ap.add_argument("--keys", help="re-derive only these titles, one mediaType:tmdbId per line")
    args = ap.parse_args(argv)

    _, mapping, _ = taxonomy_questions()
    with open(args.labels, encoding="utf-8") as fh:
        blob = json.load(fh)
    before = len(blob["records"])
    sept = september_keys(args.phase)
    withdrawals = cc.read_withdrawals(args.withdrawn)
    if args.keys:
        return rederive_keys(args, blob, sept, withdrawals, mapping)
    answers, report = classify_answers(args.combined, sept, withdrawals)
    curated, counts = swap(blob, sept, answers, mapping)
    if len(blob["records"]) != before or len(curated) != before or blob.get("count") not in (None, before):
        sys.exit("refusing: the record count changed")
    write_atomically(args.curated, dump_curated(curated, blob.get("taxonomyVersion")))
    write_atomically(args.out, dump_labels(blob))
    print(json.dumps({**counts, "records": before, "out": args.out, "curated": args.curated,
                      "combined": report}, indent=2))
    return 0


def rederive_keys(args, blob, sept, withdrawals, mapping):
    """`--keys`: the listed titles' subgenres and moods, re-derived in both files; nothing else moves."""
    keys = set(cc.read_keys(args.keys))
    outside = sorted(keys - sept)
    if outside:
        sys.exit(f"refusing: {len(outside)} listed titles are not September titles, so classify never set "
                 f"their subgenres and moods, e.g. {outside[:4]}")
    with open(args.curated, encoding="utf-8") as fh:
        head = json.load(fh)
    titles = head.pop("titles")
    relabelled = sorted(k for k in keys if (titles.get(k) or {}).get("source") != SWAP)
    if relabelled:
        sys.exit(f"refusing: {len(relabelled)} listed titles are not {SWAP} in {args.curated}, e.g. "
                 f"{relabelled[:4]}")
    answers, report = classify_answers(args.combined, keys, withdrawals)
    by_key, counts = rederive(blob, keys, answers, mapping)
    for key in keys:
        titles[key]["subgenres"], titles[key]["moods"] = by_key[key]["subgenres"], by_key[key]["moods"]
    write_atomically(args.curated, encode_curated(head, titles))
    write_atomically(args.out, dump_labels(blob))
    print(json.dumps({**counts, "keys": len(keys), "out": args.out, "curated": args.curated,
                      "combined": report}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
