#!/usr/bin/env python3
"""Compose tag strings under a cutoff and embed them into an int8 blob.

This is the free knob. The expensive LLM pass emitted a superset; tag-count, salience floor
and kind filter are all swept here, and a sweep costs one embed run, never another LLM run.

**Both v1 and v2 must be embedded by the same service to be comparable.** The shipped v1
premise blob was built by the Python den-embed on ONNX Runtime 1.22; the live service is the
Rust rewrite on 1.28, where byte parity ended — a mean of 457 of 1024 dims move and top-10
ordering survives for none of den-embed's own 30 test queries. Comparing a v2 blob from
today's service against the shipped v1 blob would measure the embedder, not the tags. So
`--source v1` re-embeds v1's frozen tag strings through whatever service this run uses, and
that is the v1 arm any v1-vs-v2 claim must cite.

Output is the same format `finalize` writes: [int32 count][int32 dim] + count x dim int8,
plus a JSON key list in row order. Written to NEW paths — nothing here overwrites v1.
"""
import argparse
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'
ROOT = '/Users/cindy/Projects/Personal/den-dataset/out-t02'


def compose(tags):
    """One document per title: the selected tags, most-defining first, space separated.

    Kept identical for v1 and v2 so a comparison is about the tags and not the formatting.
    Hyphens are left intact — bge-m3 sub-word tokenizes them, and joining with spaces
    instead measurably blurs a compound premise into its parts.
    """
    return ' '.join(tags)


def select_v2(record, top_n, min_salience, kinds):
    out = []
    for t in record['tags']:
        if t['salience'] < min_salience:
            continue
        if kinds and t['kind'] not in kinds:
            continue
        out.append(t['tag'])
        if top_n and len(out) >= top_n:
            break
    return out


def embed_all(texts, url, chunk, retries=4):
    vectors = []
    endpoint = url.rstrip('/') + '/embed/batch'
    for start in range(0, len(texts), chunk):
        block = texts[start:start + chunk]
        payload = json.dumps({'texts': block}).encode('utf-8')
        for attempt in range(retries):
            try:
                req = urllib.request.Request(endpoint, data=payload,
                                             headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.load(resp)
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == retries - 1:
                    raise SystemExit(f'embed failed at offset {start}: {exc}')
                time.sleep(2 ** attempt)
        got = body['vectors']
        if len(got) != len(block):
            raise SystemExit(f'service returned {len(got)} vectors for {len(block)} texts')
        vectors.extend(got)
        if start % (chunk * 20) == 0:
            print(f'  embedded {start + len(block)}/{len(texts)}', flush=True)
    return vectors


def write_blob(path, vectors, dim):
    with open(path, 'wb') as fh:
        fh.write(struct.pack('<ii', len(vectors), dim))
        for v in vectors:
            if len(v) != dim:
                raise SystemExit(f'vector of length {len(v)}, expected {dim}')
            fh.write(bytes((x & 0xFF) for x in v))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', choices=['v1', 'v2'], required=True)
    ap.add_argument('--aggregated', default=os.path.join(V2, 'tags-v2', 'tags-v2-aggregated.json'))
    ap.add_argument('--top-n', type=int, default=0, help='0 = no cap')
    ap.add_argument('--min-salience', type=int, default=1)
    ap.add_argument('--kinds', default='', help='comma-separated subset of premise,trope,subject,tone-setting')
    ap.add_argument('--url', default=os.environ.get('DEN_EMBED_URL', 'http://127.0.0.1:8791'))
    ap.add_argument('--chunk', type=int, default=32)
    ap.add_argument('--label', required=True, help='names the output files')
    ap.add_argument('--out-dir', default=os.path.join(V2, 'vectors'))
    args = ap.parse_args()

    kinds = {k.strip() for k in args.kinds.split(',') if k.strip()}
    os.makedirs(args.out_dir, exist_ok=True)

    if args.source == 'v1':
        with open(os.path.join(ROOT, 'labels-premise.json'), encoding='utf-8') as fh:
            media_of = {r['tmdbId']: r['mediaType'] for r in json.load(fh)['records']}
        with open(os.path.join(ROOT, 'premise-tags-wip', 'tags-raw.json'), encoding='utf-8') as fh:
            raw = json.load(fh)
        items = []
        for r in raw:
            tags = r['tags'][:args.top_n] if args.top_n else r['tags']
            items.append((f"{media_of[r['tmdbId']]}:{r['tmdbId']}", compose(tags)))
    else:
        with open(args.aggregated, encoding='utf-8') as fh:
            records = json.load(fh)['records']
        items = []
        for key in sorted(records):
            tags = select_v2(records[key], args.top_n, args.min_salience, kinds)
            if not tags:
                continue
            items.append((key, compose(tags)))

    keys = [k for k, _ in items]
    texts = [t for _, t in items]
    if len(set(keys)) != len(keys):
        raise SystemExit('duplicate keys in the embed set')
    print(f'{args.label}: {len(texts)} documents, mean {sum(len(t) for t in texts) / len(texts):.0f} chars',
          flush=True)

    vectors = embed_all(texts, args.url, args.chunk)
    dim = len(vectors[0])
    blob = os.path.join(args.out_dir, f'vectors-{args.label}.bin')
    write_blob(blob, vectors, dim)
    with open(os.path.join(args.out_dir, f'keys-{args.label}.json'), 'w', encoding='utf-8') as fh:
        json.dump(keys, fh)
    meta = {'label': args.label, 'source': args.source, 'count': len(keys), 'dims': dim,
            'topN': args.top_n, 'minSalience': args.min_salience,
            'kinds': sorted(kinds) or None, 'service': args.url,
            'meanChars': round(sum(len(t) for t in texts) / len(texts), 1)}
    with open(os.path.join(args.out_dir, f'meta-{args.label}.json'), 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, indent=2)
    print(json.dumps(meta, indent=2))


if __name__ == '__main__':
    main()
