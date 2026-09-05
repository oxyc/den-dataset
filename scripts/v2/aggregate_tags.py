#!/usr/bin/env python3
"""Aggregate the three v2 tagging passes into one frozen tag set per title.

Three independent passes over identical inputs, aggregated by vote. DT-C uses n=3 because
single-pass output is noisy; DT-H's tags were single-pass, so this is the actual upgrade
over v1 rather than "more tags".

Voting on raw strings would barely work. v1's vocabulary is 246,307 distinct tags over
37,314 titles with **89.9% used exactly once** — an open vocabulary where three passes
rarely produce the identical string for the same idea. So votes are counted over a
normalized form and near-duplicates are merged within a title by token overlap
(`time-loop` / `repeating-time-loop` / `looping-day` collapse; `heist-gone-wrong` and
`heist-planning` do not).

`agreement` per title is the mean pairwise Jaccard over the three merged tag sets. Titles
below `--escalate-below` are listed for an adjudication pass rather than shipped on a
coin-flip.

Validation is strict and loud: a tag longer than five words is a licence problem, not a
style problem — a sentence-length tag is an abridgement of a Wikipedia plot under CC BY-SA —
so it is dropped and counted, never silently kept.
"""
import argparse
import collections
import itertools
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_phase import load_manifest, read_output, expected_ids  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'
TAG_RE = re.compile(r'^[a-z0-9]+(-[a-z0-9]+){1,4}$')
KINDS = {'premise', 'trope', 'subject', 'tone-setting'}

# Tokens that carry no discriminating meaning when deciding whether two tags are the same
# idea. Kept small on purpose: an aggressive list merges genuinely different premises.
FILLER = {'a', 'an', 'the', 'of', 'to', 'in', 'for', 'with', 'his', 'her', 'their', 'its',
          'and', 'by', 'on', 'at', 'from', 'into', 'own'}


def tokens(tag):
    return frozenset(t for t in tag.split('-') if t not in FILLER) or frozenset(tag.split('-'))


def same_idea(a, b):
    """Token-overlap test, tuned to merge restatements without merging siblings."""
    ta, tb = tokens(a), tokens(b)
    if ta == tb:
        return True
    inter = len(ta & tb)
    if inter == 0:
        return False
    return inter / min(len(ta), len(tb)) >= 0.75 and abs(len(ta) - len(tb)) <= 2


def canonical(cluster):
    """The cluster's name: most-voted string, then shortest, then alphabetical — so the
    canonical form is stable across runs rather than depending on iteration order."""
    return sorted(cluster.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0][0]


def load_pass(phase_dir):
    manifest = load_manifest(phase_dir)
    rows = {}
    problems = {}
    stats = collections.Counter()
    for i in range(manifest['batches']):
        data, err = read_output(phase_dir, i)
        if err:
            if err != 'missing':
                problems[i] = err
            continue
        want = set(expected_ids(phase_dir, i))
        for r in data:
            key = r.get('key')
            if key not in want:
                problems[i] = f'row key {key!r} not in this batch'
                break
            clean = []
            seen = set()
            for t in r.get('tags') or []:
                tag = (t.get('tag') or '').strip().lower()
                if not TAG_RE.match(tag):
                    stats['rejected_shape'] += 1
                    continue
                if len(tag.split('-')) > 5:
                    stats['rejected_too_long'] += 1
                    continue
                if tag in seen:
                    stats['rejected_duplicate'] += 1
                    continue
                kind = t.get('kind')
                if kind not in KINDS:
                    stats['rejected_kind'] += 1
                    continue
                sal = t.get('salience')
                if not isinstance(sal, int) or not 1 <= sal <= 5:
                    stats['rejected_salience'] += 1
                    continue
                seen.add(tag)
                clean.append({'tag': tag, 'salience': sal, 'kind': kind, 'rank': len(clean)})
            # An empty tag list is a FAILURE, not a result. A title in a batch has a
            # non-empty Wikipedia plot by construction, so there is always something to tag;
            # zero tags means the worker truncated and stubbed the remainder. Measured on the
            # Haiku bake-off arm at 40 titles/batch: 23 of 120 titles came back with `"tags":
            # []` while carrying 2,600-4,100 characters of plot. Counted separately and
            # excluded, so a truncating model cannot look merely terse.
            if not clean:
                stats['empty_tag_lists'] += 1
                continue
            stats['titles'] += 1
            stats['tags_kept'] += len(clean)
            rows[key] = clean
    return rows, problems, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=os.path.join(V2, 'tags-v2'))
    ap.add_argument('--passes', type=int, default=3)
    ap.add_argument('--min-votes', type=int, default=2)
    ap.add_argument('--escalate-below', type=float, default=0.34)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    passes, all_problems, all_stats = [], {}, collections.Counter()
    for p in range(1, args.passes + 1):
        rows, problems, stats = load_pass(os.path.join(args.dir, f'pass{p}'))
        passes.append(rows)
        all_problems[f'pass{p}'] = problems
        for k, v in stats.items():
            all_stats[f'pass{p}.{k}'] = v
        print(f'pass{p}: {len(rows)} titles, {len(problems)} problem batches', file=sys.stderr)

    complete = set(passes[0])
    for rows in passes[1:]:
        complete &= set(rows)
    partial = set().union(*(set(r) for r in passes)) - complete
    print(f'titles with all {args.passes} passes: {len(complete)} · partial: {len(partial)}',
          file=sys.stderr)

    out = {}
    escalate = []
    agreements = []
    for key in sorted(complete):
        variants = [passes[i][key] for i in range(args.passes)]

        # Cluster every tag string across the three passes into ideas.
        clusters = []          # list of {string: votes-within-cluster}
        meta = []              # parallel: per-cluster salience/kind/rank samples
        for pi, tags in enumerate(variants):
            for t in tags:
                for ci, cluster in enumerate(clusters):
                    if any(same_idea(t['tag'], s) for s in cluster):
                        cluster[t['tag']] = cluster.get(t['tag'], 0) + 1
                        meta[ci]['passes'].add(pi)
                        meta[ci]['salience'].append(t['salience'])
                        meta[ci]['kind'].append(t['kind'])
                        meta[ci]['rank'].append(t['rank'])
                        break
                else:
                    clusters.append({t['tag']: 1})
                    meta.append({'passes': {pi}, 'salience': [t['salience']],
                                 'kind': [t['kind']], 'rank': [t['rank']]})

        merged = []
        for cluster, m in zip(clusters, meta):
            votes = len(m['passes'])
            if votes < args.min_votes:
                continue
            merged.append({
                'tag': canonical(cluster),
                'votes': votes,
                'salience': round(sum(m['salience']) / len(m['salience']), 2),
                'kind': collections.Counter(m['kind']).most_common(1)[0][0],
                'rank': round(sum(m['rank']) / len(m['rank']), 2),
                'variants': sorted(cluster) if len(cluster) > 1 else None,
            })
        # Rank order is the trustworthy signal (DT-H); votes break ties above it, mean rank
        # orders within a vote tier, salience is the weak tertiary.
        merged.sort(key=lambda t: (-t['votes'], t['rank'], -t['salience']))

        # Agreement: mean pairwise Jaccard over the three passes' idea-sets.
        idea_sets = []
        for pi in range(args.passes):
            ideas = set()
            for cluster, m in zip(clusters, meta):
                if pi in m['passes']:
                    ideas.add(canonical(cluster))
            idea_sets.append(ideas)
        jac = []
        for x, y in itertools.combinations(idea_sets, 2):
            union = x | y
            jac.append(len(x & y) / len(union) if union else 0.0)
        agreement = round(sum(jac) / len(jac), 4) if jac else 0.0
        agreements.append(agreement)

        out[key] = {'tags': merged, 'agreement': agreement,
                    'perPassCounts': [len(v) for v in variants]}
        if agreement < args.escalate_below:
            escalate.append(key)

    dest = args.out or os.path.join(args.dir, 'tags-v2-aggregated.json')
    with open(dest, 'w', encoding='utf-8') as fh:
        json.dump({'titles': len(out), 'records': out}, fh)
    with open(os.path.join(args.dir, 'escalate.json'), 'w', encoding='utf-8') as fh:
        json.dump({'threshold': args.escalate_below, 'count': len(escalate), 'keys': escalate}, fh)

    agreements.sort()
    summary = {
        'titlesAggregated': len(out),
        'titlesPartial': len(partial),
        'meanTagsPerTitle': round(sum(len(v['tags']) for v in out.values()) / max(len(out), 1), 2),
        'agreement': {
            'mean': round(sum(agreements) / max(len(agreements), 1), 4),
            'p10': agreements[len(agreements) // 10] if agreements else None,
            'p50': agreements[len(agreements) // 2] if agreements else None,
            'p90': agreements[9 * len(agreements) // 10] if agreements else None,
        },
        'escalate': len(escalate),
        'escalateThreshold': args.escalate_below,
        'validation': dict(all_stats),
        'problemBatches': {p: len(v) for p, v in all_problems.items()},
    }
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
