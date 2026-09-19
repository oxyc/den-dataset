#!/usr/bin/env python3
"""Check a classification batch against its input and the controlled vocabulary.

  scripts/v2/validate_classify_batch.py --phase out-repass/classify [--batch 0000]

Same split as `validate_premise_batch.py`, for the same reason: a generating pass returns a correct row
COUNT while inventing keys, duplicating one and dropping another, and reports success every time. A count
is not evidence; the key set is.

  fatal   — the batch is unusable: invented/dropped/duplicated keys, a non-object row, a `primary_genre`
            missing or outside the vocabulary. These mean the pass was not doing the task.
  quality — one field to drop: an off-vocabulary subgenre or mood, a confidence out of range, a duplicate
            label within a work, more than 3 of something. The rest of the row is still good.

`animated` is checked but never fatal: it is a boolean a pass can reasonably get wrong, and a wrong flag
costs one title's `animated` filter, not its labels.
"""
import argparse
import json
import os
import sys

MAX_SUB, MAX_MOOD = 3, 3


def check(batch_in, batch_out, vocab):
    fatal, quality = [], []
    if not isinstance(batch_out, list):
        return ["output is not a JSON array"], []

    want = [r["key"] for r in batch_in]
    got = [r.get("key") for r in batch_out if isinstance(r, dict)]
    if len(got) != len(batch_out):
        fatal.append("some rows are not objects")
    if len(set(got)) != len(got):
        fatal.append(f"duplicated keys: {sorted({k for k in got if got.count(k) > 1})}")
    invented = sorted(set(got) - set(want))
    missing = sorted(set(want) - set(got))
    if invented:
        fatal.append(f"invented keys not in the input: {invented}")
    if missing:
        fatal.append(f"keys dropped from the input: {missing}")

    pg_ok = set(vocab["primary_genre"])
    sg_ok = set(vocab["subgenres"])
    md_ok = set(vocab["moods"])

    for row in batch_out:
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        pg = row.get("primary_genre")
        if not pg:
            fatal.append(f"{key}: no primary_genre")
        elif pg not in pg_ok:
            fatal.append(f"{key}: primary_genre {pg!r} is not in the vocabulary")

        for field, allowed, cap in (("subgenres", sg_ok, MAX_SUB), ("moods", md_ok, MAX_MOOD)):
            items = row.get(field) or []
            if not isinstance(items, list):
                fatal.append(f"{key}: {field} is not a list")
                continue
            if len(items) > cap:
                quality.append(f"{key}: {len(items)} {field}, cap is {cap} — drop the weakest")
            seen = set()
            for it in items:
                if not isinstance(it, dict) or "label" not in it:
                    quality.append(f"{key}: malformed {field} entry {it!r} — drop it")
                    continue
                lab = it["label"]
                if lab in seen:
                    quality.append(f"{key}: {field} repeats {lab!r} — drop the duplicate")
                seen.add(lab)
                if lab not in allowed:
                    quality.append(f"{key}: {field} {lab!r} is not in the vocabulary — drop it")
                c = it.get("confidence")
                if not isinstance(c, (int, float)) or not 0 <= c <= 1:
                    quality.append(f"{key}: {field} {lab!r} confidence {c!r} out of range — drop it")
                elif c < 0.5:
                    quality.append(f"{key}: {field} {lab!r} confidence {c} below the 0.5 floor — drop it")

        if not isinstance(row.get("animated"), bool):
            quality.append(f"{key}: animated is {row.get('animated')!r}, not a boolean")

    return fatal, quality


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True)
    ap.add_argument("--batch")
    args = ap.parse_args()

    vocab = json.load(open(os.path.join(args.phase, "vocab.json"), encoding="utf-8"))
    in_dir, out_dir = os.path.join(args.phase, "in"), os.path.join(args.phase, "out")
    names = ([f"batch-{args.batch}.json"] if args.batch
             else sorted(n for n in os.listdir(out_dir) if n.startswith("batch-")))

    rejected, quality, ok, rows = [], [], 0, 0
    for name in names:
        out_path = os.path.join(out_dir, name)
        if not os.path.exists(out_path):
            continue
        try:
            bi = json.load(open(os.path.join(in_dir, name), encoding="utf-8"))
            bo = json.load(open(out_path, encoding="utf-8"))
        except json.JSONDecodeError as e:
            rejected.append((name, [f"unreadable: {e}"]))
            continue
        f, q = check(bi, bo, vocab)
        if q:
            quality.append((name, q))
        if f:
            rejected.append((name, f))
        else:
            ok += 1
            rows += len(bo)

    for name, notes in quality:
        print(f"notes {name}  ({len(notes)} field(s) to drop; the batch is still usable)")
        for n in notes[:4]:
            print(f"    {n}")
        if len(notes) > 4:
            print(f"    … and {len(notes) - 4} more")
    for name, problems in rejected:
        print(f"REJECT {name}")
        for p in problems[:4]:
            print(f"    {p}")

    print(json.dumps({"batches": len(names), "accepted": ok, "rejected": len(rejected),
                      "rowsAccepted": rows, "batchesWithFieldNotes": len(quality),
                      "fieldsToDrop": sum(len(n) for _, n in quality)}))
    sys.exit(1 if rejected else 0)


if __name__ == "__main__":
    main()
