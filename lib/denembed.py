#!/usr/bin/env python3
"""den-embed — the single embedding path for the corpus and for every live query — and the known-answer
canary that says which vector space a given service embeds into.

The int8 quantisation lives in the SERVICE and nowhere else: a vector comes back as ints in [-127, 127]
and is stored verbatim. `DEN_EMBED_URL` picks the service (default `http://localhost:8791`), and is the
operator's choice to make: it decides which space the corpus lands in, and the canary is what checks it.

## Identity is not enough, which is why the canary exists

`/health` reports model, dims, a vector epoch, a runtime string and the token cap. Two services are the
same EMBEDDER when they produce the same numbers — so equality is model, dims, epoch and cap, and never the
runtime string, which moves on every release and would invalidate a corpus over a log-line fix. But a
service can report an identical identity and still return different numbers, because what moved was its
configuration, its ONNX Runtime or its CPU. The only description of a space that cannot drift from the
space is a vector it produced: `data/embed-canary.json` holds a handful of fixed texts and the exact int8
vectors the serving box returns for them, and a run that does not reproduce every one byte for byte
writes nothing. `scripts/v2/embed_canary.py` owns that file and regenerates it; this reads it.

`max_tokens` is checked before any case, and is fatal on its own: it is where the service truncates, so at
a different cap the long cases embed a different text and their bytes cannot mean anything — a partial
failure that reads as a mystery rather than as a setting.
"""
import base64
import binascii
import hashlib
import json
import math
import os
import time
import urllib.parse

from . import http

DEFAULT_URL = "http://localhost:8791"


class CanaryFailure(RuntimeError):
    """The service does not embed into the space the committed answers describe — or the answers file is
    not the one it claims to be."""


def base_url(env=None):
    env = os.environ if env is None else env
    return env.get("DEN_EMBED_URL") or DEFAULT_URL


def _call(url, path, payload=None):
    parts = urllib.parse.urlsplit(url)
    target = parts.path.rstrip("/") + path
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    raw = http.request(parts.netloc, target, method="POST" if body is not None else "GET", body=body,
                       headers=headers, scheme=parts.scheme or "http")
    return json.loads(raw.decode("utf-8"))


def identity(url):
    """The service's `/health`, as the store records it. A field the service does not report is the
    generation before it: no runtime is the Python service, no epoch is epoch 0."""
    health = _call(url, "/health")

    def field(name, default):
        return default if health.get(name) is None else health[name]

    return {"model": field("model", "unknown"), "dims": field("dims", 0), "vectorEpoch": field("vector_epoch", 0),
            "runtime": field("runtime", "pre-3.0.0"), "maxTokens": field("max_tokens", 0)}


def stored_identity(raw):
    """An `index/embedder.json` body as the identity it records, or None when it is not one. A file from
    before `vectorEpoch` existed is epoch 0 — the same thing an absent `vector_epoch` from /health means."""
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), t) for k, t in
                                            (("model", str), ("dims", int), ("runtime", str), ("maxTokens", int))):
        return None
    return {"model": raw["model"], "dims": raw["dims"], "vectorEpoch": raw.get("vectorEpoch") or 0,
            "runtime": raw["runtime"], "maxTokens": raw["maxTokens"]}


def same_embedder(a, b):
    """The same NUMBERS, not the same build: `runtime` is left out on purpose."""
    return all(a[k] == b[k] for k in ("model", "dims", "vectorEpoch", "maxTokens"))


def label(found):
    return (f"{found['model']}/{found['dims']} epoch {found.get('vectorEpoch', 0)} ({found['runtime']}) "
            f"max_tokens={found['maxTokens']}")


def embed_many(url, texts):
    """`POST /embed/batch` — int8 vectors 1:1 with `texts`. A 413 is the service's answer, not a blip: the
    request exceeded its token budget and asking again fails the same way."""
    if not texts:
        return []
    vectors = _call(url, "/embed/batch", {"texts": texts})["vectors"]
    return [[max(-128, min(127, int(x))) for x in vector] for vector in vectors]


def vector_bytes(case):
    """A case's int8 row as bytes, or None when its base64 does not decode. Strict, as the Swift's
    `Data(base64Encoded:)` was: `b64decode` on its own skips a stray character and decodes what is left."""
    try:
        return base64.b64decode(case["v"], validate=True)
    except (binascii.Error, ValueError):
        return None


def texts_sha256(cases):
    digest = hashlib.sha256()
    for case in cases:
        digest.update(case["id"].encode("utf-8") + b"\0" + case["text"].encode("utf-8") + b"\n")
    return digest.hexdigest()


def space_id(canary_set, cases):
    """`<canarySet>:<sha256 over case ids and their vectors>` — the published name of the space. The texts
    are the instrument, not the answer, so they are not in it."""
    digest = hashlib.sha256(canary_set.encode("utf-8") + b"\n")
    for case in cases:
        # An undecodable vector digests as nothing, as the Swift's did; `verify` refuses it by name.
        digest.update(case["id"].encode("utf-8") + b"\0" + (vector_bytes(case) or b"") + b"\n")
    return f"{canary_set}:{digest.hexdigest()}"


def compare(expected, actual):
    """`(dims differing, cosine, max |delta|)`. Cosine is for a reader of the failure — rounding or a
    different space — and never decides: int8 dot products have no tolerance for either."""
    if len(expected) != len(actual):
        return max(len(expected), len(actual)), 0.0, 0
    differing = sum(1 for x, y in zip(expected, actual) if x != y)
    delta = max((abs(x - y) for x, y in zip(expected, actual)), default=0)
    dot = sum(x * y for x, y in zip(expected, actual))
    norms = math.sqrt(sum(x * x for x in expected)) * math.sqrt(sum(y * y for y in actual))
    return differing, (dot / norms if norms else 0.0), delta


def read_canary(path):
    """The canary file, or a refusal that says it is not one. The fields are the ones the Swift decoded;
    anything else in the file (`note`, `vectorEncoding`, `embedder`) is the generator's and optional."""
    try:
        with open(path, encoding="utf-8") as handle:
            canary = json.load(handle)
        cases = canary["cases"]
        well_formed = (all(isinstance(canary[key], str) for key in ("canarySet", "spaceId", "textsSha256"))
                       and isinstance(canary["dims"], int) and isinstance(cases, list)
                       and all(isinstance(case, dict) and all(isinstance(case.get(key), str)
                                                              for key in ("id", "why", "text", "v"))
                               for case in cases))
    except (OSError, ValueError, KeyError, TypeError) as broken:
        well_formed, why = False, f"{type(broken).__name__}: {broken}"
    else:
        why = "a field is missing or of the wrong type"
    if not well_formed:
        raise CanaryFailure(f"{path} is not a readable embedding canary ({why}). Restore it from git, or "
                            f"regenerate it with scripts/v2/embed_canary.py --regenerate against a known-good "
                            f"embedder.")
    return canary


def verify(path, url, log):
    """Embed every case and refuse unless all come back byte-identical. Returns the stamp a verified run
    records beside its vectors, which finalize puts in the manifest as `embeddingSpace`."""
    if not os.path.exists(path):
        raise CanaryFailure(f"no embedding canary at {path}, so nothing can say which space this service "
                            f"embeds into. Point DEN_EMBED_CANARY at data/embed-canary.json.")
    canary = read_canary(path)
    cases = canary["cases"]
    log(f"embed canary: {canary['canarySet']} — {len(cases)} cases from {path}")
    if texts_sha256(cases) != canary["textsSha256"]:
        raise CanaryFailure(f"{path}: its texts were edited without regenerating its vectors, so the "
                            f"committed answers describe texts that are no longer in it. Regenerate it with "
                            f"scripts/v2/embed_canary.py --regenerate against a known-good embedder.")
    if space_id(canary["canarySet"], cases) != canary["spaceId"]:
        raise CanaryFailure(f"{path}: its spaceId does not match the vectors it carries — the file was "
                            f"hand-edited. Regenerate it rather than correcting the digest.")
    service = identity(url)
    embedder = canary.get("embedder")
    cap = embedder.get("max_tokens") if isinstance(embedder, dict) else None
    if isinstance(cap, int) and not isinstance(cap, bool) and cap != service["maxTokens"]:
        raise CanaryFailure(f"the service truncates at {service['maxTokens']} tokens and these answers were "
                            f"recorded at {cap}. Every case longer than the smaller cap embeds a different "
                            f"text, so no comparison was attempted. Set MAX_TOKENS={cap}, or regenerate the "
                            f"canary at the new cap as part of a full re-embed.")
    failures = []
    for case in cases:
        raw = vector_bytes(case)
        if raw is None:
            raise CanaryFailure(f"{path}: case {case['id']} has an unreadable base64 vector")
        expected = [b - 256 if b > 127 else b for b in raw]
        got = embed_many(url, [case["text"]])
        differing, cosine, delta = compare(expected, got[0] if got else [])
        ok = differing == 0
        log(f"  {'ok  ' if ok else 'FAIL'} {case['id']}  {differing}/{len(expected)} dims differ  "
            f"cosine {cosine:.6f}  max|d| {delta}")
        if not ok:
            failures.append((case["id"], cosine))
    if failures:
        worst = min(cosine for _, cosine in failures)
        why = ("the same model doing slightly different arithmetic — a BLAS, a kernel, an ORT build. The "
               "vectors are close but not equal, and int8 dot products have no tolerance for that: it is "
               "still a different space." if worst >= 0.9999 else
               "a different embedding space. Check the service's configuration first — MAX_TOKENS is the "
               "setting that has actually caused this — then the CPU architecture, the ONNX Runtime "
               "version and the model.")
        raise CanaryFailure(f"embed canary FAILED on {len(failures)} of {len(cases)} cases "
                            f"({', '.join(case for case, _ in failures)}) against {label(service)}. {why} "
                            f"Refusing to write vectors: a corpus half in another space loads, ranks, and is "
                            f"wrong.")
    return {"spaceId": canary["spaceId"], "canarySet": canary["canarySet"], "dims": canary["dims"],
            "url": url, "verifiedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
