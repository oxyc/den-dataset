#!/usr/bin/env python3
"""The publisher's gate on `data/award-ceremony-merges.json`.

Wikidata files some awarding bodies' prizes half under the organisation and half under its "Awards" group,
so the store build merges each listed pair into one ceremony (`store/awards.py`). An entry whose `from` no
title names any more is stale: the facts moved on, and whatever split it fixed may now be split another way.
The build records `awardMerges` in the manifest — the list's sha256, how many merges applied, which are
stale — and this refuses a store built from another list, or with a stale entry.

    pipeline/check_award_merges.py --gate out/dataset.meta.json
"""
import argparse
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MERGES = os.path.join(REPO, "data", "award-ceremony-merges.json")


def gate(meta_path, merges_path=MERGES):
    """0 passes, 1 refuses."""
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    merges = os.path.relpath(merges_path)
    rebuild = f"pipeline/build_store.py … --stamp-meta {meta_path}"
    record = meta.get("awardMerges")
    if not (isinstance(record, dict) and isinstance(record.get("stale"), list)):
        print(f"error: the manifest records no awardMerges, so nothing says which award ceremony merges the "
              f"store applied. Rebuild the store with {rebuild}.", file=sys.stderr)
        return 1
    with open(merges_path, "rb") as fh:
        committed = hashlib.sha256(fh.read()).hexdigest()
    if record.get("sha256") != committed:
        print(f"error: the store applied award ceremony merges other than the committed {merges}. Rebuild "
              f"the store with {rebuild}.", file=sys.stderr)
        return 1
    if record["stale"]:
        print(f"error: {len(record['stale'])} merge(s) in {merges} name a ceremony no title does any more: "
              f"{', '.join(record['stale'])}\n"
              f"       The facts no longer file any award under it, so the split it fixed has moved. Look at\n"
              f"       the body on Wikidata, then delete the entry or point it at the item its prizes file\n"
              f"       under now, and rebuild the store with {rebuild}", file=sys.stderr)
        return 1
    print(f"award merge gate: {record.get('applied')} merges applied, none stale")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", metavar="META", required=True,
                    help="refuse this manifest's store while an award ceremony merge is stale")
    ap.add_argument("--merges", default=MERGES)
    args = ap.parse_args()
    return gate(args.gate, args.merges)


if __name__ == "__main__":
    sys.exit(main())
