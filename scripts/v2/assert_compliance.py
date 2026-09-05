#!/usr/bin/env python3
"""Prove, from the batch files on disk, that no TMDB prose reached an LLM.

The rule: only titles with `hasWikiPlot == true` may be sent to an AI application. The other
19,255 enriched rows carry TMDB overview prose in the same `overview` field, and sending that
is barred by TMDb §1.C. Every batch builder draws from the wiki-plot corpus, so the rule holds
by construction — but "holds by construction" is what everyone says right up until it doesn't,
and this is the claim that would be most expensive to be wrong about.

So this re-derives the allowed id-set from the **enriched records**, independently of the
corpus file, and checks every title reference in every batch input on disk against it. It also
spot-checks that the plot text actually shipped in a batch is the enriched Wikipedia plot
rather than something that arrived by another path.

Exits non-zero on any violation. Run before any LLM phase, and after any change to a builder.
"""
import argparse
import glob
import json
import os
import random
import sys

ROOT = '/Users/cindy/Projects/Personal/den-dataset/out-t02'
V2 = os.path.join(ROOT, 'v2')

DEFAULT_ROOTS = [
    os.path.join(V2, 'ruler', 'gen', 'in'),
    os.path.join(V2, 'ruler', 'judge', 'pass1', 'in'),
    os.path.join(V2, 'tags-v2', 'pass1', 'in'),
    os.path.join(V2, 'tags-v2', 'pass2', 'in'),
    os.path.join(V2, 'tags-v2', 'pass3', 'in'),
]


def allowed_ids():
    """Re-derived from the enriched records, not read back from the corpus file — a corpus
    built with a broken filter would otherwise vouch for itself."""
    allowed, denied, plots = set(), set(), {}
    enriched = os.path.join(ROOT, 'enriched')
    for name in sorted(os.listdir(enriched)):
        if not (name.startswith('batch-') and name.endswith('.json')):
            continue
        with open(os.path.join(enriched, name), encoding='utf-8') as fh:
            for rec in json.load(fh):
                key = f"{rec['mediaType']}:{rec['tmdbId']}"
                if rec.get('hasWikiPlot') is True:
                    allowed.add(key)
                    plots[key] = rec.get('overview') or ''
                else:
                    denied.add(key)
    return allowed, denied, plots


def references(item):
    """Every title a batch entry sends to a model. A generation entry carries an anchor plus
    its candidates; a tagging or judging entry carries one title."""
    if 'anchor' in item and isinstance(item['anchor'], dict) and 'key' in item['anchor']:
        return [item['anchor']] + list(item.get('candidates') or [])
    return [item]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', action='append', default=[])
    ap.add_argument('--spot-check', type=int, default=30, help='batch files to verify plot text in')
    args = ap.parse_args()
    roots = args.root or DEFAULT_ROOTS

    allowed, denied, plots = allowed_ids()
    print(f'enriched: {len(allowed)} hasWikiPlot=true, {len(denied)} false', flush=True)

    files = checked = 0
    violations = []
    tag_files = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for path in sorted(glob.glob(os.path.join(root, 'batch-*.json'))):
            files += 1
            if 'tags-v2' in root:
                tag_files.append(path)
            with open(path, encoding='utf-8') as fh:
                batch = json.load(fh)
            for item in batch:
                for entry in references(item):
                    key = entry.get('key')
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
