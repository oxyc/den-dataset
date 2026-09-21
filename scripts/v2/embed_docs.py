#!/usr/bin/env python3
"""Embed composed documents on whichever den-embed serves live queries, and write the index store.

  scripts/v2/embed_docs.py --docs docs.jsonl --out-dir out --url http://den-embed:8080 \
      --canary data/embed-canary.json

Input is `embed-corpus --dump-docs` output: one `{"key": "movie:11", "doc": "…"}` per line. Output is the
same pair of append-only stores `embed-corpus` writes — `labels.jsonl` is NOT written here (the caller
already has it); this writes `vectors.jsonl` keyed by the same `key`, plus `keys.json` in row order.

## Why this exists rather than pointing embed-corpus at the box

arm64 and x86_64 den-embed return different int8 vectors for identical input — 525 of 1024 dims, measured
on the same pinned model sha256 and the same strings (oxyc/den-dataset#21). So the embed has to happen on
the box. The plots and the composition logic live on the laptop. Rather than ship 148 MB of enriched
batches and a Swift toolchain to the box, or publish a deliberately-internal service to reach it from the
laptop, the composed documents travel and this embeds them in place.

## Batch size

den-embed's per-request budget is `sum(min(actual_tokens, max_tokens)) <= 8192`. At `MAX_TOKENS=1024` a
corpus document can count its full 1024, so 8 documents is the ceiling and anything above it 413s. The
default here is 7, matching what `embed-corpus --chunk` needs for the same reason. A 413 is fatal, not
retried: it means the batch size is wrong, and retrying the same request just fails again more slowly.

## Workers

One request at a time leaves den-embed at ~313% of the box's 6 cores — the client is the bottleneck, not
the model. `--workers` sends that many batches concurrently. Rows are written as each batch returns, so
`vectors.jsonl` is no longer in document order; nothing downstream depends on that, because `keys.json` is
built from the file's own row order below and every row carries its `key`.

## Resume

Append-only, and an existing `vectors.jsonl` is read first so a killed run continues where it stopped. A
long embed WILL be interrupted; re-reading 38k lines costs a second and re-embedding them costs an hour.

That resume is also why the canary below runs on EVERY invocation and not only the first: the box rebuilds
den-embed weekly, so a run resumed after a redeploy would append to the same file from a service nobody
re-checked. `--canary` must be mounted alongside this script when it runs in a container (see
`docs/OPERATE.md`), because the whole point is that it travels with the code that writes the vectors.
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import embed_canary  # noqa: E402


def embed(url, texts, retries=4):
    payload = json.dumps({"texts": texts}).encode("utf-8")
    endpoint = url.rstrip("/") + "/embed/batch"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(endpoint, data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.load(resp)["vectors"]
        except urllib.error.HTTPError as e:
            if e.code == 413:
                sys.exit(f"{endpoint}: 413 on a batch of {len(texts)} — lower --batch (the request budget "
                         f"is sum(min(tokens, max_tokens)) <= 8192)")
            if attempt == retries - 1:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)


ap = argparse.ArgumentParser()
ap.add_argument("--docs", required=True, help="embed-corpus --dump-docs output")
ap.add_argument("--out-dir", required=True)
ap.add_argument("--url", required=True)
ap.add_argument("--batch", type=int, default=7)
ap.add_argument("--workers", type=int, default=1)
ap.add_argument("--canary", default=None,
                help="the known-answer file for the embedding space (default: data/embed-canary.json "
                     "beside this checkout; pass it explicitly when running from a container)")
args = ap.parse_args()

os.makedirs(args.out_dir, exist_ok=True)
vec_path = os.path.join(args.out_dir, "vectors.jsonl")

done = set()
if os.path.exists(vec_path):
    with open(vec_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                done.add(json.loads(line)["key"])
            except Exception:
                pass  # a half-written final line from a kill; it is re-embedded below

rows = []
with open(args.docs, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r["key"] not in done:
            rows.append(r)

health = json.load(urllib.request.urlopen(args.url.rstrip("/") + "/health", timeout=30))
print(json.dumps({"docs": len(rows), "alreadyDone": len(done), "embedder": health}), file=sys.stderr)
if health.get("max_tokens", 0) < 1024:
    sys.exit(f"refusing: den-embed reports max_tokens={health.get('max_tokens')}, which truncates a corpus "
             f"document. See den-dataset/docs/OPERATE.md 'The alignment rule'.")

# Before the append handle is opened, so a service that has moved produces no rows rather than rows in a
# second space. A partially-written store loads, ranks, and is wrong; an absent one is merely absent.
# The verified space is recorded beside the vectors so `import_box_vectors.py` can carry it into the index
# dir and `finalize` can stamp it into dataset.meta.json.
space = embed_canary.gate(args.url, canary=args.canary,
                          record=os.path.join(args.out_dir, "embedding-space.json"))
print(f"  embedding space: {space}", file=sys.stderr)

t0 = time.time()
written = 0
lock = threading.Lock()
blocks = [rows[i:i + args.batch] for i in range(0, len(rows), args.batch)]

with open(vec_path, "a", encoding="utf-8") as out:
    def run(block):
        global written
        vectors = embed(args.url, [r["doc"] for r in block])
        if len(vectors) != len(block):
            sys.exit(f"den-embed returned {len(vectors)} vectors for {len(block)} docs")
        # One lock around the write keeps lines whole; rows land in completion order, not document order.
        with lock:
            for r, v in zip(block, vectors):
                out.write(json.dumps({"key": r["key"], "v": v}) + "\n")
                written += 1
            out.flush()
            if written % (args.batch * 40) < args.batch or written == len(rows):
                rate = written / max(time.time() - t0, 1e-6)
                print(f"  {written}/{len(rows)}  {rate:.1f}/s  "
                      f"eta {(len(rows)-written)/max(rate,1e-6)/60:.1f}m", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for _ in pool.map(run, blocks):
            pass

keys = []
with open(vec_path, encoding="utf-8") as fh:
    for line in fh:
        keys.append(json.loads(line)["key"])
json.dump(keys, open(os.path.join(args.out_dir, "keys.json"), "w"), indent=0)

print(json.dumps({"embedded": written, "totalRows": len(keys), "out": vec_path,
                  "elapsedMin": round((time.time() - t0) / 60, 1)}, indent=2))
