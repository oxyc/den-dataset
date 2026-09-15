#!/usr/bin/env python3
"""Two invariants between the enriched corpus and the shipped labels, reported rather than enforced.

A title with a Wikipedia plot should end up with labels and therefore a vector. When it does not, it has no
More Like This and cannot be reached by any thematic query — and nothing notices. That is how Spirited Away,
One Piece, Bleach, Pokémon, Re:ZERO and Off Campus came to be in the shipped dataset with a plot and no
labels; it surfaced only because someone asked why The Wire had no similar titles.

This is the same shape as the failures `check-producers.py` exists for: an artifact drifting from the thing
it derives from, with nothing positioned to notice.

## Why it WARNS instead of failing

Both invariants are violated today — 6 and 0 under the newest-wins reading, 3 and 1 under the lexical one the
readers used before. A gate that refuses the next publish on a pre-existing violation gets switched off, so
this reports and returns 0. Turn it into a gate once the counts are zero.

## Why `--enriched-dir` is explicit, and `--max-batch-id` exists

There is no record of which enriched batches a published dataset was built from: `out-t02-cc0b/enriched` is a
SYMLINK to `../out-t02/enriched`, so a publish reads a live directory that keeps growing. Enriching after
embedding is the NORMAL order — the embed pass ran at 18:28 and batches 174-176 were written at 22:26-01:22 —
so comparing today's enriched tree against an older labels blob reports every one of those as a violation.
`--max-batch-id` bounds the comparison to the batches a publish actually saw; `manifest-counts.py --stamp`
records it as `maxBatchId`.

    scripts/check-plot-invariants.py --enriched-dir out-t02/enriched --labels out-publish/labels-t02.json
    scripts/check-plot-invariants.py ... --max-batch-id 177 --facts out-publish/facts-<ver>.json
"""
import argparse
import json
import os
import sys


def batch_files(enriched_dir, max_batch_id=None):
    """Batch files in BATCH-NUMBER order, oldest first — the order `EnrichedBatches.orderedNames` defines.

    Not `sorted()`: that is lexicographic, so `batch-99.json` comes after `batch-177.json` and the winner
    among duplicate keys depends on how many digits an id has.
    """
    out = []
    for name in os.listdir(enriched_dir):
        if not (name.startswith("batch-") and name.endswith(".json")):
            continue
        try:
            n = int(name[len("batch-"):-len(".json")])
        except ValueError:
            continue
        if max_batch_id is not None and n > max_batch_id:
            continue
        out.append((n, name))
    return [name for _, name in sorted(out)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enriched-dir", required=True)
    ap.add_argument("--labels", required=True, help="the shipped labels blob (labels-<tax>.json)")
    ap.add_argument("--facts", help="scope to keys in this facts file, as the shipped dataset does")
    ap.add_argument("--max-batch-id", type=int, help="ignore batches above this (manifest `maxBatchId`)")
    ap.add_argument("--fail", action="store_true", help="exit 1 on a violation, once the counts are zero")
    args = ap.parse_args()

    # Last occurrence wins, matching `finalize`'s own de-dup.
    has_plot = {}
    for name in batch_files(args.enriched_dir, args.max_batch_id):
        with open(os.path.join(args.enriched_dir, name), encoding="utf-8") as fh:
            for r in json.load(fh):
                has_plot[f"{r['mediaType']}:{r['tmdbId']}"] = bool(r.get("hasWikiPlot"))

    with open(args.labels, encoding="utf-8") as fh:
        labelled = {f"{r.get('mediaType', 'movie')}:{r['tmdbId']}" for r in json.load(fh)["records"]}

    scope = None
    if args.facts:
        with open(args.facts, encoding="utf-8") as fh:
            scope = {f"{r['mediaType']}:{r['tmdbId']}" for r in json.load(fh)["records"]}

    def inscope(key):
        return scope is None or key in scope

    plot_no_labels = sorted(k for k, v in has_plot.items() if v and k not in labelled and inscope(k))
    labels_no_plot = sorted(k for k in labelled if has_plot.get(k) is False and inscope(k))

    print(f"enriched keys {len(has_plot)} (batches <= {args.max_batch_id or 'all'})  labelled {len(labelled)}")
    print(f"  hasWikiPlot and NOT labelled : {len(plot_no_labels)}")
    for k in plot_no_labels[:20]:
        print(f"      {k}")
    print(f"  labelled and NOT hasWikiPlot : {len(labels_no_plot)}")
    for k in labels_no_plot[:20]:
        print(f"      {k}")

    if plot_no_labels or labels_no_plot:
        print("\nwarning: a title with a plot and no labels has no vector, so no More Like This and no "
              "thematic reach.", file=sys.stderr)
        return 1 if args.fail else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
