#!/usr/bin/env python3
"""Batch bookkeeping shared by every v2 LLM phase.

Each phase is a directory with `in/batch-NNNN.json`, a `manifest.json` naming the id-set,
and `out/batch-NNNN.json` written by one subagent each. The rules here exist because the
DT-H de-risk run lost writes to name collisions and then passed a coverage check that had
been done with a glob:

- a batch's output path is fixed and derived from its input path — never chosen by the worker;
- coverage is checked against the **manifest id-set**, never against `ls out/`, so a batch
  that wrote the wrong ids fails instead of counting;
- JSON is parsed strictly and a malformed or short file is QUARANTINED and reported, never
  skipped silently;
- re-running is idempotent: `--list-missing` emits exactly the batches still owed, so a
  resumed run repeats no work and drops none.

Usage:
  llm_phase.py --phase <dir> --list-missing [--limit N]
  llm_phase.py --phase <dir> --status
  llm_phase.py --phase <dir> --verify        # strict parse + coverage, exit 1 on any gap
"""
import argparse
import json
import os
import sys


def batch_path(phase, kind, index):
    return os.path.join(phase, kind, f'batch-{index:04d}.json')


def load_manifest(phase):
    path = os.path.join(phase, 'manifest.json')
    if not os.path.exists(path):
        sys.exit(f'no manifest at {path} — a phase without an id-set cannot be verified')
    with open(path, encoding='utf-8') as fh:
        manifest = json.load(fh)
    # A phase may be run over a prefix of what was built — the mining is cheap and was sized
    # generously, the LLM pass is not. `scope.json` records how far the run is meant to go so
    # coverage is judged against the batches actually in scope, not against everything on disk.
    scope_path = os.path.join(phase, 'scope.json')
    if os.path.exists(scope_path):
        with open(scope_path, encoding='utf-8') as fh:
            scope = json.load(fh)
        if scope['batches'] > manifest['batches']:
            sys.exit(f"scope.json asks for {scope['batches']} batches but only "
                     f"{manifest['batches']} were built")
        manifest['batches'] = scope['batches']
        manifest['scopeReason'] = scope.get('reason')
    return manifest


def expected_ids(phase, index):
    """The ids batch `index` was given. Coverage is judged against these, not against
    whatever the output happens to contain."""
    with open(batch_path(phase, 'in', index), encoding='utf-8') as fh:
        batch = json.load(fh)
    out = []
    for item in batch:
        if 'anchor' in item:
            out.append(item['anchor']['key'])
        elif 'triplet' in item:
            out.append(item['triplet']['id'])
        else:
            out.append(item['key'])
    return out


def read_output(phase, index, strict=True):
    """Parse one output file. Returns (rows, error). A parse failure is an error, never a
    silently-empty result."""
    path = batch_path(phase, 'out', index)
    if not os.path.exists(path):
        return None, 'missing'
    try:
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
    except OSError as exc:
        return None, f'unreadable: {exc}'
    if not text.strip():
        return None, 'empty file'
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f'invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}'
    if not isinstance(data, list):
        return None, f'top level is {type(data).__name__}, expected a JSON array'
    return data, None


def audit(phase):
    manifest = load_manifest(phase)
    n = manifest['batches']
    ok, problems = [], {}
    covered = set()
    for i in range(n):
        want = expected_ids(phase, i)
        rows, err = read_output(phase, i)
        if err:
            problems[i] = err
            continue
        got = []
        for r in rows:
            key = r.get('key') or r.get('anchor') or r.get('id')
            if key is None:
                problems[i] = 'a row has no key/anchor/id field'
                break
            got.append(key)
        else:
            unexpected = set(got) - set(want)
            if unexpected:
                problems[i] = f'{len(unexpected)} ids not in this batch, e.g. {sorted(unexpected)[:3]}'
                continue
            covered.update(got)
            ok.append(i)
    all_ids = set()
    for i in range(n):
        all_ids.update(expected_ids(phase, i))
    return {
        'batches': n,
        'complete': len(ok),
        'problems': problems,
        'idsExpected': len(all_ids),
        'idsCovered': len(covered),
        'idsMissing': sorted(all_ids - covered),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--phase', required=True)
    ap.add_argument('--list-missing', action='store_true')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--verify', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    manifest = load_manifest(args.phase)
    n = manifest['batches']

    if args.list_missing:
        missing = []
        for i in range(n):
            rows, err = read_output(args.phase, i)
            if err is not None:
                missing.append(i)
                continue
            want = set(expected_ids(args.phase, i))
            got = {r.get('key') or r.get('anchor') or r.get('id') for r in rows}
            # A batch that answered for fewer than 80% of its ids is treated as owed: a
            # partial answer is usually a truncated write, not a considered "none".
            if len(got & want) < 0.8 * len(want):
                missing.append(i)
        if args.limit:
            missing = missing[:args.limit]
        print('\n'.join(str(i) for i in missing))
        return

    report = audit(args.phase)
    if args.status or args.verify:
        summary = {k: v for k, v in report.items() if k not in ('problems', 'idsMissing')}
        summary['problemBatches'] = len(report['problems'])
        summary['idsMissingCount'] = len(report['idsMissing'])
        print(json.dumps(summary, indent=2))
        if report['problems']:
            print('\nproblems:', file=sys.stderr)
            for i, why in sorted(report['problems'].items())[:40]:
                print(f'  batch-{i:04d}: {why}', file=sys.stderr)
    if args.verify and (report['problems'] or report['idsMissing']):
        sys.exit(1)


if __name__ == '__main__':
    main()
