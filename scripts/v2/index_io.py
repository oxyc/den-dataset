#!/usr/bin/env python3
"""Load the shipped int8 vector blobs and answer top-k queries against them.

Blob format (as `finalize` writes it): little-endian [int32 count][int32 dim] then
count x dim int8 rows, quantized int8-symmetric-x127 from L2-normalized floats.

Row order is NOT in the blob — it comes from the sidecar that was written beside it. The
plot index is ordered by `labels-t02.json` records; the premise index by
`premise-tags-wip/premise-ids.json`. Getting that pairing wrong produces an index that
loads cleanly and returns nonsense, which is why both are asserted against the header count.
"""
import json
import os

import numpy as np

ROOT = '/Users/cindy/Projects/Personal/den-dataset/out-t02'


def read_blob(path):
    with open(path, 'rb') as fh:
        header = np.frombuffer(fh.read(8), dtype='<i4')
        count, dim = int(header[0]), int(header[1])
        raw = np.frombuffer(fh.read(), dtype=np.int8)
    if raw.size != count * dim:
        raise ValueError(f'{path}: header says {count}x{dim} = {count * dim} bytes, got {raw.size}')
    return raw.reshape(count, dim), count, dim


class Index:
    def __init__(self, keys, vectors, name):
        if len(keys) != vectors.shape[0]:
            raise ValueError(f'{name}: {len(keys)} keys but {vectors.shape[0]} vectors')
        self.name = name
        self.keys = list(keys)
        self.pos = {k: i for i, k in enumerate(self.keys)}
        # Re-normalize in float32: int8 rounding leaves rows slightly off unit length, and
        # an unnormalized dot product would quietly rank longer rows higher.
        v = vectors.astype(np.float32)
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.vectors = v / norms

    def topk(self, key, k, restrict_mask=None):
        i = self.pos.get(key)
        if i is None:
            return []
        sims = self.vectors @ self.vectors[i]
        sims[i] = -2.0
        if restrict_mask is not None:
            sims = np.where(restrict_mask, sims, -2.0)
        top = np.argpartition(-sims, k)[:k]
        top = top[np.argsort(-sims[top])]
        return [(self.keys[j], float(sims[j])) for j in top]

    def topk_many(self, keys, k, restrict_mask=None, block=256):
        """Batched: one matmul per block of seeds rather than one per seed."""
        out = {}
        idx = [(key, self.pos[key]) for key in keys if key in self.pos]
        for start in range(0, len(idx), block):
            chunk = idx[start:start + block]
            rows = np.array([i for _, i in chunk])
            sims = self.vectors[rows] @ self.vectors.T
            for r, (key, i) in enumerate(chunk):
                s = sims[r]
                s[i] = -2.0
                if restrict_mask is not None:
                    s = np.where(restrict_mask, s, -2.0)
                top = np.argpartition(-s, k)[:k]
                top = top[np.argsort(-s[top])]
                out[key] = [(self.keys[j], float(s[j])) for j in top]
            del sims
        return out


def load_plot_index():
    with open(os.path.join(ROOT, 'labels-t02.json'), encoding='utf-8') as fh:
        records = json.load(fh)['records']
    keys = [f"{r.get('mediaType', 'movie')}:{r['tmdbId']}" for r in records]
    vectors, count, _ = read_blob(os.path.join(ROOT, 'vectors-bge-m3.bin'))
    if count != len(keys):
        raise ValueError(f'plot blob has {count} rows, labels-t02 has {len(keys)} records')
    return Index(keys, vectors, 'plot')


def load_premise_v1_index():
    """v1's sidecar is a list of BARE tmdb ids. That is only unambiguous because the 940
    movie/TV id collisions were already dropped upstream — verified: zero colliding bare ids
    among the 37,533 shipped. The media type is recovered from labels-premise.json, which
    does carry it, rather than assuming 'movie'."""
    with open(os.path.join(ROOT, 'labels-premise.json'), encoding='utf-8') as fh:
        records = json.load(fh)['records']
    media_of = {r['tmdbId']: r['mediaType'] for r in records}
    if len(media_of) != len(records):
        raise ValueError('labels-premise.json has colliding bare tmdb ids — cannot key v1 by id alone')
    with open(os.path.join(ROOT, 'premise-tags-wip', 'premise-ids.json'), encoding='utf-8') as fh:
        bare = json.load(fh)
    keys = [f"{media_of[i]}:{i}" for i in bare]
    vectors, count, _ = read_blob(os.path.join(ROOT, 'vectors-premise.bin'))
    if count != len(keys):
        raise ValueError(f'premise blob has {count} rows, premise-ids has {len(keys)}')
    return Index(keys, vectors, 'premise-v1')
