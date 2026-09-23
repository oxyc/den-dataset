#!/usr/bin/env python3
"""Prove, from the batch files on disk, that no TMDB prose reached an LLM.

The rule: only titles with `hasWikiPlot == true` may be sent to an AI application. The other
19,542 enriched rows carry TMDB overview prose in the same `overview` field, and sending that
is barred by TMDb §1.C. Every batch builder draws from the wiki-plot corpus, so the rule holds
by construction — but "holds by construction" is what everyone says right up until it doesn't,
and this is the claim that would be most expensive to be wrong about.

So this re-derives the allowed id-set from the **enriched records**, independently of the
corpus file, and checks every title reference in every batch input on disk against it. It also
spot-checks that the plot text actually shipped in a batch is the enriched Wikipedia plot
rather than something that arrived by another path.

## What last-write-wins does NOT cover

Resolving each key once means a title whose `hasWikiPlot` FLIPPED is judged by its latest record. 505 keys
flip today and every one is false -> true, which is the direction that matters: a batch built during the
false era would have carried TMDB prose, and this script now clears those keys. Checked directly — none of
the 505 appears in any batch item carrying prose, so there is no live exposure — but the hole is
structural. If a flipped key HAD been batched before its flip, this reports PASS over a real breach where
the old two-set version reported a violation. The two-set version also reported 505 violations that were
not real, which is why it was replaced; the honest summary is that this trades a guaranteed false alarm
for a narrow, currently-empty blind spot.

The plot-text spot check is not a backstop for it: it only samples directories matching `tags-v2`, which
is 3 of the 25 phase directories.

Exits non-zero on any violation. Run before any LLM phase, and after any change to a builder.
"""
import argparse
import glob
import json
import os
import random
import sys

# The enriched tree to prove compliance over. `--out-dir`, else DEN_OUT_DIR, else out-t02 beside the repo.
# This was one developer's absolute home directory, in the script that is the §1.C compliance proof for a
# PUBLIC repo — so nobody else could run the proof at all.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOT = os.environ.get('DEN_OUT_DIR') or os.path.join(REPO, 'out-t02')

def default_roots(v2):
    """Every `*/in/` directory under v2, discovered rather than listed.

    This was a hand-maintained list, and a hand-maintained list of things to check is a
    list that silently stops covering what you add. Two whole phase trees — the bake-off
    and the sealed-half confirmation, 24 batch files between them — were built, run and
    never gated, while the checker went on reporting PASS over exactly the same 3,403 files
    as before. The count not moving is what gave it away, and only because someone looked.

    A phase is a directory with `in/batch-*.json`, so that is what is searched for. Adding a
    phase now adds it to the gate by construction.
    """
    found = set()
    for path in glob.glob(os.path.join(v2, '**', 'in'), recursive=True):
        if os.path.isdir(path) and glob.glob(os.path.join(path, 'batch-*.json')):
            found.add(path)
    return sorted(found)


def batch_order(name):
    """Enriched batches in NUMERIC order. `sorted()` on the names puts batch-10 before batch-2."""
    stem = name[len('batch-'):-len('.json')]
    return (0, int(stem)) if stem.isdigit() else (1, stem)


def allowed_ids(root):
    """Re-derived from the enriched records, not read back from the corpus file — a corpus
    built with a broken filter would otherwise vouch for itself.

    Each key is resolved ONCE, last write winning, in batch-number order — the rule `finalize` and the
    live readers use (`EnrichedBatches.orderedNames`). This used to fill two SETS while iterating, so a
    title re-enriched into a later batch with a different `hasWikiPlot` landed in both `allowed` and
    `denied`, and the check below (`key in denied or key not in allowed`) then reported it as a §1.C
    violation. 505 keys disagreed that way, which made every failure this script produced evidence about
    batch bookkeeping rather than about what reached a model — and a compliance check that cries wolf is
    worse than none, because it teaches people to ignore it.
    """
    state, plots = {}, {}
    enriched = os.path.join(root, 'enriched')
    for name in sorted(os.listdir(enriched), key=batch_order):
        if not (name.startswith('batch-') and name.endswith('.json')):
            continue
        with open(os.path.join(enriched, name), encoding='utf-8') as fh:
            for rec in json.load(fh):
                key = f"{rec['mediaType']}:{rec['tmdbId']}"
                state[key] = rec.get('hasWikiPlot') is True
                if state[key]:
                    plots[key] = rec.get('overview') or ''
                else:
                    plots.pop(key, None)
    allowed = {k for k, ok in state.items() if ok}
    denied = {k for k, ok in state.items() if not ok}
    return allowed, denied, plots


def references(item, truth=None):
    """Every title key a batch entry sends to a model.

    Three batch shapes, and the third nearly broke this check. A **generation** entry carries
    a keyed anchor plus keyed candidates. A **tagging** entry carries one keyed title. A
    **judging** entry carries no keys at all — deliberately, because a blind judge must not
    see ids — so its titles can only be resolved through the phase's `truth.json`.

    Without that lookup the checker reported 645 violations on the judging batches, every one
    of them spurious: it was finding no key and treating "unnameable" as "not allowed". A
    compliance check that cries wolf is worse than no check, because it teaches people to
    ignore it.
    """
    if isinstance(item.get('anchor'), dict) and 'key' in item['anchor']:
        return [e['key'] for e in [item['anchor']] + list(item.get('candidates') or [])]
    if 'key' in item:
        return [item['key']]
    if 'id' in item:
        if truth is None:
            raise SystemExit(
                'a judging batch was scanned without its truth.json — the ids are not in the '
                'batch by design, so compliance cannot be established from the batch alone')
        row = truth.get(item['id'])
        if row is None:
            raise SystemExit(f"judging case {item['id']!r} is absent from truth.json")
        return [row['anchor'], row['positive'], row['negative']]
    raise SystemExit(f'batch entry has no anchor, key or id: {sorted(item)}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', action='append', default=[])
    ap.add_argument('--out-dir', default=DEFAULT_ROOT,
                    help='the enriched tree to prove over (default: $DEN_OUT_DIR, else out-t02 in the repo)')
    ap.add_argument('--spot-check', type=int, default=30, help='batch files to verify plot text in')
    args = ap.parse_args()
    if not os.path.isdir(os.path.join(args.out_dir, 'enriched')):
        sys.exit(f"{args.out_dir}/enriched does not exist — pass --out-dir or set DEN_OUT_DIR")
    roots = args.root or default_roots(os.path.join(args.out_dir, 'v2'))
    print(f'out-dir: {args.out_dir}', flush=True)
    print(f'phase directories discovered: {len(roots)}', flush=True)

    allowed, denied, plots = allowed_ids(args.out_dir)
    print(f'enriched: {len(allowed)} hasWikiPlot=true, {len(denied)} false', flush=True)

    files = checked = 0
    violations = []
    tag_files = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        # A judging phase keeps its id mapping one level up, beside the pass directories.
        truth = None
        truth_path = os.path.join(os.path.dirname(os.path.dirname(root)), 'truth.json')
        if os.path.exists(truth_path):
            with open(truth_path, encoding='utf-8') as fh:
                truth = json.load(fh)
        for path in sorted(glob.glob(os.path.join(root, 'batch-*.json'))):
            files += 1
            if 'tags-v2' in root:
                tag_files.append(path)
            with open(path, encoding='utf-8') as fh:
                batch = json.load(fh)
            for item in batch:
                for key in references(item, truth):
                    checked += 1
                    if key is None or key in denied or key not in allowed:
                        violations.append((path, key))

    print(f'batch files scanned: {files} · title references checked: {checked:,}')
    print(f'references to a hasWikiPlot=false title: {len(violations)}')

    # The id being allowed is not the same as the text being the wiki plot. Verify the prose.
    mismatches = 0
    spot = 0
    if tag_files:
        rng = random.Random(3)
        for path in rng.sample(tag_files, min(args.spot_check, len(tag_files))):
            with open(path, encoding='utf-8') as fh:
                for item in json.load(fh):
                    src = plots.get(item['key'], '')
                    head = item['plot'].split('\n[…]')[0][:200]
                    spot += 1
                    if not src.startswith(head):
                        mismatches += 1
        print(f'plot-text spot check on {spot} titles: {mismatches} mismatches')

    if violations or mismatches:
        for path, key in violations[:20]:
            print(f'  VIOLATION {key} in {path}', file=sys.stderr)
        sys.exit(1)
    print('PASS — no TMDB-prose title reached any batch file')


if __name__ == '__main__':
    main()
