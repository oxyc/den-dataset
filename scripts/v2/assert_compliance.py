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
