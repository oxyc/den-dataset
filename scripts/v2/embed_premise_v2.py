#!/usr/bin/env python3
"""Build the premise-v2 vector blob, reusing every vector the live embedder would reproduce byte-for-byte.

  scripts/v2/embed_premise_v2.py --tags data/premise-tags-v2.json --out-dir out-premise-v2/vectors \
      --url http://10.89.0.198:8080

## Why this is not "embed the strings that changed"

int8 dot products are only meaningful between vectors from the SAME den-embed generation, and this repo has
three premise blobs built by two different ones:

  * `out-t02/vectors-premise.bin` (2026-07-05) — measured against the live service on 6 titles spanning the
    blob: 48-55% of dims differ, cosine 0.977-0.982. A different embedder. Unusable as a base.
  * `out-t02/v2/vectors/vectors-premise-v1-realigned.bin` (2026-09-05) — v1's strings re-embedded on the
    newer service. Measured on the same 6 titles: 0 dims differ, cosine 1.000000. This is the base.
  * `out-t02/v2/vectors/vectors-coverage-fill.bin` — cosine 0.90-0.96 against live. Also unusable; its 219
    titles are re-embedded here.

So "reuse" means reuse from the REALIGNED blob only, and only where the composed string is unchanged. The
manifest could not have told us any of this: the July blob predates the `embedderRuntime` / `vectorEpoch`
fields that exist to catch exactly this, which is why the check below is a measurement, not a metadata read.

## The guard

Before reusing anything, this re-embeds a sample of the base blob's own rows and refuses if they do not come
back identical. That is the whole safety property: if the box's den-embed is redeployed (it rebuilds weekly)
and its output moves, the reuse silently becomes a two-embedder index — the failure this file exists to
prevent. Better to refuse and re-embed everything than to publish a blob that loads cleanly and ranks wrong.
"""
import argparse
import json
import os
import random
import struct
import sys
import time
import urllib.error
import urllib.request


def compose(tags):
    """Identical to merge_premise_tags.compose — the document is the tags, space separated."""
    return " ".join(tags)


def read_blob(path):
    with open(path, "rb") as fh:
        count, dim = struct.unpack("<ii", fh.read(8))
        raw = fh.read()
    if len(raw) != count * dim:
        sys.exit(f"{path}: header says {count}x{dim}, got {len(raw)} bytes")
    return count, dim, raw


def embed(url, texts, retries=4):
    """POST one batch. den-embed caps a request's total tokens, so callers must keep batches small."""
    payload = json.dumps({"texts": texts}).encode("utf-8")
    endpoint = url.rstrip("/") + "/embed/batch"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(endpoint, data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.load(resp)["vectors"]
        except urllib.error.HTTPError as e:
            if e.code == 413:
                sys.exit(f"{endpoint}: 413 on a batch of {len(texts)} — lower --batch")
            if attempt == retries - 1:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)


ap = argparse.ArgumentParser()
ap.add_argument("--tags", default="data/premise-tags-v2.json")
ap.add_argument("--base-vectors", default="out-t02/v2/vectors/vectors-premise-v1-realigned.bin")
ap.add_argument("--base-keys", default="out-t02/v2/vectors/keys-premise-v1-realigned.json")
ap.add_argument("--v1-tags", default="data/premise-tags-v1.json",
                help="the strings the base blob was built from")
ap.add_argument("--out-dir", required=True)
ap.add_argument("--url", required=True)
ap.add_argument("--batch", type=int, default=48)
ap.add_argument("--probe", type=int, default=8, help="base rows to re-embed as the reuse guard")
ap.add_argument("--no-reuse", action="store_true", help="embed every title, ignoring the base blob")
args = ap.parse_args()

tags = json.load(open(args.tags, encoding="utf-8"))["tags"]
v1 = json.load(open(args.v1_tags, encoding="utf-8"))["tags"]
base_count, dim, base_raw = read_blob(args.base_vectors)
base_keys = json.load(open(args.base_keys, encoding="utf-8"))
if len(base_keys) != base_count:
    sys.exit(f"base sidecar disagrees: {base_count} vectors, {len(base_keys)} keys")
base_at = {k: i for i, k in enumerate(base_keys)}

# Reusable: in the base blob AND the string the base was built from is the string we want now.
reusable = set()
if not args.no_reuse:
    for k, i in base_at.items():
        if k in tags and k in v1 and compose(v1[k]) == compose(tags[k]):
            reusable.add(k)

# The guard. A sample of the base's own rows must come back byte-identical from the live service.
if reusable:
    sample = sorted(random.Random(0).sample(sorted(reusable), min(args.probe, len(reusable))))
    got = embed(args.url, [compose(tags[k]) for k in sample], )
    for k, live in zip(sample, got):
        i = base_at[k]
        stored = list(struct.unpack("<%db" % dim, base_raw[i * dim:(i + 1) * dim]))
        if stored != list(live):
            d = sum(1 for x, y in zip(stored, live) if x != y)
            sys.exit(f"reuse guard failed on {k}: {d}/{dim} dims differ from the base blob.\n"
                     f"The live embedder no longer reproduces {args.base_vectors}. Re-run with --no-reuse "
                     f"so the whole index comes from one embedder.")
    print(f"reuse guard: {len(sample)} base rows reproduce byte-identically", file=sys.stderr)

keys = sorted(tags)
todo = [k for k in keys if k not in reusable]
print(json.dumps({"titles": len(keys), "reused": len(reusable), "toEmbed": len(todo)}), file=sys.stderr)

fresh = {}
t0 = time.time()
for start in range(0, len(todo), args.batch):
    block = todo[start:start + args.batch]
    for k, v in zip(block, embed(args.url, [compose(tags[k]) for k in block])):
        if len(v) != dim:
            sys.exit(f"{k}: service returned {len(v)} dims, base blob is {dim}")
        fresh[k] = v
    done = start + len(block)
    if done % (args.batch * 20) == 0 or done == len(todo):
        rate = done / max(time.time() - t0, 1e-6)
        print(f"  {done}/{len(todo)}  {rate:.1f}/s  eta {(len(todo)-done)/max(rate,1e-6)/60:.1f}m",
              file=sys.stderr)

os.makedirs(args.out_dir, exist_ok=True)
vec_path = os.path.join(args.out_dir, "vectors-premise-v2.bin")
with open(vec_path, "wb") as fh:
    fh.write(struct.pack("<ii", len(keys), dim))
    for k in keys:
        if k in fresh:
            fh.write(struct.pack("<%db" % dim, *fresh[k]))
        else:
            i = base_at[k]
            fh.write(base_raw[i * dim:(i + 1) * dim])
json.dump(keys, open(os.path.join(args.out_dir, "premise-v2-ids.json"), "w"), indent=0)

print(json.dumps({
    "vectors": len(keys), "dims": dim, "reused": len(reusable), "embedded": len(fresh),
    "out": vec_path, "sidecar": os.path.join(args.out_dir, "premise-v2-ids.json"),
    "elapsedMin": round((time.time() - t0) / 60, 1),
}, indent=2))
