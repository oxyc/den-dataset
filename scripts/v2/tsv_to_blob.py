#!/usr/bin/env python3
"""Convert the box-side embed output (`key\\tcsv` per line) into the shipped blob format.

The embed runs on the homelab box because the published den-embed image cannot run on an
Apple Silicon Mac (no AVX2 under emulation — see scripts/v2/README.md), and it writes a
resumable TSV rather than a binary so a killed run can append.

Output is exactly what `finalize` writes: little-endian [int32 count][int32 dim] then
count x dim int8 rows, plus a JSON key list in row order. Written to NEW paths under
`v2/vectors/` — nothing here touches the shipped v1 blobs.

Rows are ordered by the key file if one is given, so two arms can be built in the same order
and compared row-for-row; otherwise by first appearance, which is the embed order.
"""
import argparse
import json
import os
import struct
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tsv', required=True)
    ap.add_argument('--label', required=True)
    ap.add_argument('--order', default=None, help='optional JSON list of keys fixing row order')
    ap.add_argument('--out-dir',
                    default='/Users/cindy/Projects/Personal/den-dataset/out-t02/v2/vectors')
    args = ap.parse_args()

    vectors = {}
    dim = None
    dupes = 0
    with open(args.tsv, encoding='utf-8') as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.rstrip('\n')
            if not line:
                continue
            try:
                key, csv = line.split('\t', 1)
            except ValueError:
                sys.exit(f'{args.tsv}:{lineno}: no tab separator')
            try:
                vec = [int(x) for x in csv.split(',')]
            except ValueError as exc:
                sys.exit(f'{args.tsv}:{lineno}: non-integer component ({exc})')
            if dim is None:
                dim = len(vec)
            elif len(vec) != dim:
                sys.exit(f'{args.tsv}:{lineno}: {len(vec)} dims, expected {dim}')
            if any(x < -128 or x > 127 for x in vec):
                sys.exit(f'{args.tsv}:{lineno}: component outside int8 range')
            if key in vectors:
                # A resumed run can legitimately re-embed a chunk it had already written.
                # Identical repeats are fine; a differing repeat means two different texts
                # were written under one key, which would silently corrupt the index.
                if vectors[key] != vec:
                    sys.exit(f'{args.tsv}:{lineno}: key {key} repeats with a DIFFERENT vector')
                dupes += 1
                continue
            vectors[key] = vec

    if args.order:
        with open(args.order, encoding='utf-8') as fh:
            order = json.load(fh)
        missing = [k for k in order if k not in vectors]
        if missing:
            sys.exit(f'{len(missing)} keys in the order file were never embedded, '
                     f'e.g. {missing[:3]}')
        keys = order
    else:
        keys = list(vectors)

    os.makedirs(args.out_dir, exist_ok=True)
    blob = os.path.join(args.out_dir, f'vectors-{args.label}.bin')
    with open(blob, 'wb') as fh:
        fh.write(struct.pack('<ii', len(keys), dim))
        for k in keys:
            fh.write(bytes(x & 0xFF for x in vectors[k]))
    with open(os.path.join(args.out_dir, f'keys-{args.label}.json'), 'w', encoding='utf-8') as fh:
        json.dump(keys, fh)

    zero = sum(1 for k in keys if not any(vectors[k]))
    print(json.dumps({'label': args.label, 'rows': len(keys), 'dims': dim,
                      'duplicateLinesSkipped': dupes, 'allZeroRows': zero,
                      'blob': blob, 'bytes': os.path.getsize(blob)}, indent=2))
    if zero:
        print(f'!! {zero} all-zero vectors — den-embed returns zeros for empty text', file=sys.stderr)


if __name__ == '__main__':
    main()
