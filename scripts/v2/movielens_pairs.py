#!/usr/bin/env python3
"""Build the co-rating ruler: (seed -> titles the same people also liked) pairs for RecoEval.

This is the "would they enjoy it" ruler. It is behavioural, not semantic: it says nothing
about whether two films share a premise, only that the people who liked one liked the
other. That is exactly why v2 is required not to REGRESS here while it wins on premise
discrimination — a premise index that improves theme matching by wrecking taste agreement
is not an improvement.

Method: implicit-feedback item-item association over "liked" events (rating >= 4.0), with
a support shrinkage so a pair seen 5 times cannot outrank a pair seen 5,000 times, and a
minimum co-occurrence floor.

**Scoring is nPMI, not cosine, and that choice is load-bearing.** Cosine's sqrt(n_i n_j)
denominator does not correct popularity nearly hard enough on this data: under cosine the
neighbours of *Amélie* came back as Fight Club / Memento / The Matrix — the MovieLens
canon, not films anyone would call similar. A ruler whose ground truth is "the popular
canon" cannot tell two rankers apart, because returning popular titles scores well against
every seed. nPMI divides out both marginals in log space and collapses that. `canonShare`
in the emitted meta is the guard: it reports what fraction of all `relevant` items are
drawn from the 100 most-liked titles, so the bias is a measured number, not a hope.

Sourcing/licence: MovieLens ml-32m (GroupLens, University of Minnesota). Research use;
non-commercial without permission; redistribution of transformations must carry the same
licence conditions. Cite Harper & Konstan 2015, https://doi.org/10.1145/2827872.
The output stays in den-dataset as EVAL data — it is never bundled into the app artifact.

Streams ratings.csv (877 MB) once; nothing here loads it whole.
"""
import argparse
import csv
import json
import os
import sys

import numpy as np
from scipy.sparse import csr_matrix

ROOT = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'
ML = os.path.join(ROOT, 'movielens', 'ml-32m')
JOIN = os.path.join(ROOT, 'movielens', 'join.json')
OUT_DIR = os.path.join(ROOT, 'eval')

LIKE_THRESHOLD = 4.0
MIN_USER_PROFILE = 5        # a user who liked <5 usable films carries no co-occurrence signal
MAX_USER_PROFILE = 1000     # and one who liked >1000 links everything to everything
MIN_ITEM_LIKES = 50         # a seed needs enough raters for its neighbours to mean anything
MIN_COOC = 10               # a pair seen fewer times than this is noise
SHRINK = 20.0               # cooc/(cooc+SHRINK): support discount, standard for item-item CF
TOP_K = 20                  # size of the held-out `relevant` set per seed


def load_usable():
    with open(JOIN, encoding='utf-8') as fh:
        join = json.load(fh)
    return {int(k): v for k, v in join['movieIdToTmdb'].items()}, join['summary']


def stream_likes(usable_movie_ids):
    """Yield (userId, movieId) for every liked rating on a usable movie."""
    path = os.path.join(ML, 'ratings.csv')
    users, items = [], []
    ubuf, ibuf = [], []
    with open(path, encoding='utf-8', newline='') as fh:
        header = fh.readline().strip()
        if header != 'userId,movieId,rating,timestamp':
            sys.exit(f'ratings.csv header changed: {header!r}')
        for line in fh:
            u, m, r, _ = line.split(',', 3)
            if float(r) < LIKE_THRESHOLD:
                continue
            mi = int(m)
            if mi not in usable_movie_ids:
                continue
            ubuf.append(int(u))
            ibuf.append(mi)
            if len(ubuf) >= 2_000_000:
                users.append(np.array(ubuf, dtype=np.int32))
                items.append(np.array(ibuf, dtype=np.int32))
                ubuf, ibuf = [], []
    if ubuf:
        users.append(np.array(ubuf, dtype=np.int32))
        items.append(np.array(ibuf, dtype=np.int32))
    return np.concatenate(users), np.concatenate(items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seeds', type=int, default=6000)
    ap.add_argument('--seed-rng', type=int, default=20260904)
    ap.add_argument('--score', choices=['npmi', 'cosine'], default='npmi')
    ap.add_argument('--out', default='reco-cases.json')
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    usable, join_summary = load_usable()
    print(f'usable MovieLens movieIds: {len(usable)}', flush=True)

    raw_users, raw_items = stream_likes(usable)
    print(f'liked events on usable movies: {len(raw_users):,}', flush=True)

    # Compact ids.
    uniq_users, u_idx = np.unique(raw_users, return_inverse=True)
    uniq_items, i_idx = np.unique(raw_items, return_inverse=True)
    del raw_users, raw_items
    n_users, n_items = len(uniq_users), len(uniq_items)
    print(f'users {n_users:,} · items {n_items:,}', flush=True)

    # Drop users whose profile is too small to co-occur or so large it co-occurs with everything.
    profile = np.bincount(u_idx, minlength=n_users)
    keep_user = (profile >= MIN_USER_PROFILE) & (profile <= MAX_USER_PROFILE)
    mask = keep_user[u_idx]
    u_idx, i_idx = u_idx[mask], i_idx[mask]
    print(f'after user filter: {mask.sum():,} events from {keep_user.sum():,} users', flush=True)

    A = csr_matrix((np.ones(len(u_idx), dtype=np.float32), (u_idx, i_idx)),
                   shape=(n_users, n_items))
    A.sum_duplicates()
    A.data[:] = 1.0
    likes = np.asarray(A.sum(axis=0)).ravel()

    tmdb_of = np.array([usable[int(m)] for m in uniq_items], dtype=np.int64)

    eligible = np.flatnonzero(likes >= MIN_ITEM_LIKES)
    print(f'items with >= {MIN_ITEM_LIKES} likes: {len(eligible):,}', flush=True)

    # Stratify seeds across popularity deciles so the ruler is not all blockbusters.
    rng = np.random.default_rng(args.seed_rng)
    order = eligible[np.argsort(likes[eligible])]
    buckets = np.array_split(order, 10)
    per_bucket = args.seeds // 10
    chosen = []
    for b in buckets:
        take = min(per_bucket, len(b))
        chosen.append(rng.choice(b, size=take, replace=False))
    seeds = np.unique(np.concatenate(chosen))
    print(f'seeds: {len(seeds):,}', flush=True)

    At = A.T.tocsr()
    n_active_users = float(keep_user.sum())
    inv_sqrt = 1.0 / np.sqrt(np.maximum(likes, 1.0))
    p_item = np.maximum(likes, 1.0) / n_active_users
    log_p_item = np.log(p_item)

    # The 100 most-liked titles: the "canon" whose over-representation is the failure mode.
    canon = set(np.argsort(-likes)[:100].tolist())

    cases = []
    canon_hits = 0
    canon_total = 0
    CHUNK = 250
    for start in range(0, len(seeds), CHUNK):
        block = seeds[start:start + CHUNK]
        cooc = np.asarray((At[block] @ A).todense(), dtype=np.float32)
        for row, s in enumerate(block):
            c = cooc[row]
            c[s] = 0.0
            support = c / (c + SHRINK)
            if args.score == 'cosine':
                score = c * inv_sqrt[s] * inv_sqrt * support
            else:
                # nPMI = log(p_ij / (p_i p_j)) / -log(p_ij), in [-1, 1]; then support-shrunk.
                with np.errstate(divide='ignore', invalid='ignore'):
                    p_ij = c / n_active_users
                    log_p_ij = np.log(np.maximum(p_ij, 1e-12))
                    pmi = log_p_ij - log_p_item[s] - log_p_item
                    score = np.where(p_ij > 0, pmi / -log_p_ij, 0.0).astype(np.float32) * support
            score[c < MIN_COOC] = 0.0
            top = np.argpartition(-score, min(TOP_K, len(score) - 1))[:TOP_K]
            top = top[score[top] > 0]
            top = top[np.argsort(-score[top])]
            if len(top) < 5:
                continue
            canon_hits += sum(1 for j in top if int(j) in canon)
            canon_total += len(top)
            cases.append({
                'seed': f'movie:{int(tmdb_of[s])}',
                'seedLikes': int(likes[s]),
                'relevant': [f'movie:{int(tmdb_of[j])}' for j in top],
                'scores': [round(float(score[j]), 6) for j in top],
            })
        del cooc
        print(f'  seeds {start + len(block)}/{len(seeds)} · cases {len(cases)}', flush=True)

    popularity = {f'movie:{int(tmdb_of[j])}': int(likes[j]) for j in range(n_items)}

    meta = {
        'source': 'MovieLens ml-32m (GroupLens, University of Minnesota)',
        'citation': 'Harper & Konstan 2015, ACM TiiS 5(4):19, https://doi.org/10.1145/2827872',
        'licence': 'Research use; non-commercial without permission; transformations redistributable '
                   'only under the same conditions. Eval-only — never bundled into the app artifact.',
        'method': f'item-item {args.score} over likes (rating >= {LIKE_THRESHOLD}) with '
                  f'cooc/(cooc+{SHRINK}) shrinkage, min cooc {MIN_COOC}, top {TOP_K}',
        'canonShare': round(canon_hits / max(canon_total, 1), 4),
        'canonShareNote': 'fraction of all `relevant` items drawn from the 100 most-liked titles; '
                          'a high value means the ruler is measuring popularity, not taste agreement',
        'coverage': 'MOVIES ONLY — ml-32m carries no TV ids, so the ~10% of the index that is '
                    'series is not measured by this ruler at all.',
        'join': join_summary,
        'params': {
            'score': args.score,
            'likeThreshold': LIKE_THRESHOLD, 'minUserProfile': MIN_USER_PROFILE,
            'maxUserProfile': MAX_USER_PROFILE, 'minItemLikes': MIN_ITEM_LIKES,
            'minCooc': MIN_COOC, 'shrink': SHRINK, 'topK': TOP_K, 'rngSeed': args.seed_rng,
        },
        'cases': len(cases),
    }
    with open(os.path.join(OUT_DIR, args.out), 'w', encoding='utf-8') as fh:
        json.dump({'meta': meta, 'cases': cases}, fh)
    with open(os.path.join(OUT_DIR, 'reco-popularity.json'), 'w', encoding='utf-8') as fh:
        json.dump(popularity, fh)
    print(json.dumps(meta, indent=2))


if __name__ == '__main__':
    main()
