#!/usr/bin/env python3
"""A known-answer test for the embedding space, run before anything writes a vector.

    pipeline/embed_canary.py --url http://den-embed:8080            # verify; exit 2 on any difference
    pipeline/embed_canary.py --url http://den-embed:8080 --regenerate --out -

## What this is for

`embeddingModel`, `dims` and `embedderRuntime` say "bge-m3", 1024 and a crate version for every generation
of den-embed that has ever run, including the ones whose output moved. So the manifest cannot tell two
spaces apart, and nothing else did either: oxyc/den-dataset#21 is a corpus half-embedded by two generations
at a measured 6.3/10 top-10 overlap, discovered by hand months later.

The things that move the vectors are not all version-shaped, and the list is open:

  * **`MAX_TOKENS`.** The one we know bites. It is where the service truncates, so it changes the vector
    for every text longer than the cap and for none shorter — which is why a config difference here has
    twice been mistaken for something else. `docs/OPERATE.md` has said for a while that the serving box
    must run at 1024 permanently, and that is the setting these answers were recorded at.
  * **CPU architecture — SUSPECTED, not established.** #21 measured arm64 against x86_64 at 525 of 1024
    dims and concluded the architecture did it. That comparison did not hold `MAX_TOKENS` fixed, which is
    the same confound that later produced two more wrong answers, so the 525 is real and its cause is not
    known. Two x86_64 hosts at a matching cap — AVX2 against AVX-512 — are byte-identical, so whatever
    #21 found, it is not simply "a different instruction set gives different kernels".
  * An ONNX Runtime upgrade, a model swap, a quantization change, a tokenizer change.
  * Whatever the next one turns out to be.

So this deliberately checks NONE of them by name. It embeds a fixed set of texts whose answers are
committed and compares bytes; whatever the reason the embedder in front of it has moved, the answers move
too. Naming a cause would only ever catch the causes already known, which is how the last two were missed.
`MAX_TOKENS` is the single exception, and only in the failure message, where it is the first thing worth
checking rather than the thing being tested.

## The rules

**Byte-identical is the bar.** Cosine is reported so a reader of the failure knows whether they are looking
at a rounding difference or at a different space, but both fail. int8 dot products are the whole retrieval
signal; there is no tolerance at which a different space is fine.

**It runs before the first write.** A partially-written blob is worse than no blob: it loads, it ranks, and
it is wrong. Every caller here gates on this before opening its output.

**Regenerating is a deliberate act.** `--regenerate` re-embeds the texts ALREADY IN THE FILE — it never
invents or edits them — and rewrites the vectors and `spaceId`. That is legitimate exactly when the space is
meant to move: den-embed's `VECTOR_EPOCH` is bumped, the model changes, or the pinned settings change and
the whole corpus is being re-embedded in the same pass. It is NOT legitimate as a way past a failing check,
because the published `dataset.meta.json` carries the `spaceId` — regenerating to get a run going renames
the space the corpus claims, which is the lie this file exists to prevent.

**Adding or editing a case means bumping `canarySet`.** `spaceId` is a digest over the case ids and their
vectors, so it identifies "these probes, these answers". Two datasets are comparable only when their
`canarySet` matches; the set name is carried in `spaceId` itself so that comparison cannot be made by
accident. `textsSha256` catches the other half — a text edited in the file without a regenerate, which
would otherwise fail as a mysterious space change.

## Notes on the service

den-embed caches vectors in process, keyed by the text, so a second run in the same process is a cache read
rather than an inference. The first run after any restart — which is every deploy — is real. It also
short-circuits blank text to a zero vector without touching the model, which is why no case here is blank.
`/embed/batch` runs per-item inference internally (CLS pooling is padding-invariant), so batching cannot
change an answer and one text per request is not a weaker check than a batch of eight.
"""
import argparse
import base64
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_CANARY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
                              "embed-canary.json")

#: The fields of `/health` that describe the space rather than the build. Reported on a failure as the
#: likely cause; never the verdict on their own, because all four can match while the vectors differ.
IDENTITY_FIELDS = ("model", "dims", "vector_epoch", "max_tokens")

#: `max_tokens` is the one identity field that is checked BEFORE the vectors, and it is fatal on its own.
#: It is where the service truncates, so at a different cap the long cases are answering a different
#: question and their bytes cannot mean anything — the run would fail on exactly the cases longer than the
#: smaller of the two caps, which reads as a mysterious partial failure rather than as a setting.
CAP_FIELD = "max_tokens"


def load(path):
    # Named explicitly rather than left as a traceback: the usual way to hit this is running from a
    # container, where the default path points at a checkout that is not mounted, and the writer is about
    # to embed for hours.
    if not os.path.exists(path):
        sys.exit(f"no embedding canary at {path}, so nothing can say which space this service embeds "
                 f"into. Pass --canary, or mount data/embed-canary.json beside the script.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def texts_sha256(doc):
    """A digest over the probe texts — the instrument, not the answers."""
    h = hashlib.sha256()
    for case in doc["cases"]:
        h.update(case["id"].encode("utf-8"))
        h.update(b"\0")
        h.update(case["text"].encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def space_id(canary_set, cases):
    """The published identity of the space: `<canarySet>:<sha256 over case ids and their vectors>`.

    The texts are deliberately NOT in the digest. They are the instrument; `canarySet` names the
    instrument, and folding them in would make the id change for a reworded probe that measured the same
    space. What is in it is what the embedder answered.
    """
    h = hashlib.sha256()
    h.update(canary_set.encode("utf-8"))
    h.update(b"\n")
    for case in cases:
        h.update(case["id"].encode("utf-8"))
        h.update(b"\0")
        h.update(base64.b64decode(case["v"]))
        h.update(b"\n")
    return f"{canary_set}:{h.hexdigest()}"


def encode_vector(values):
    """int8 values → base64. Two's complement, one byte a dimension, so the file holds the actual bytes
    a byte-identical comparison is about rather than a decimal rendering of them."""
    return base64.b64encode(bytes((v & 0xFF) for v in values)).decode("ascii")


def decode_vector(encoded):
    """base64 → int8 values."""
    return [b - 256 if b > 127 else b for b in base64.b64decode(encoded)]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def request(url, path, payload=None, retries=3, timeout=300):
    endpoint = url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(endpoint, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            # A 4xx is the same answer every time; only a 5xx or a transport failure is worth a retry.
            if e.code < 500 or attempt == retries - 1:
                sys.exit(f"{endpoint}: HTTP {e.code} — {e.read()[:300]!r}")
        except Exception:
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)


def embed_one(url, text):
    """One text through `/embed/batch` — the route the corpus writers use."""
    vectors = request(url, "/embed/batch", {"texts": [text]})["vectors"]
    if len(vectors) != 1:
        sys.exit(f"{url}: /embed/batch returned {len(vectors)} vectors for one text")
    return vectors[0]


def verify(doc, url):
    """Embed every case and compare. Returns `(ok, report)`; never raises on a mismatch — the caller
    decides what a failure means, and `main` is only one of them."""
    health = request(url, "/health")
    report = {
        "canarySet": doc["canarySet"],
        "spaceId": doc["spaceId"],
        "url": url,
        "health": health,
        "identityDrift": {},
        "cases": [],
        "ok": True,
    }

    recorded = doc.get("embedder") or {}
    for field in IDENTITY_FIELDS:
        if field in recorded and recorded[field] != health.get(field):
            report["identityDrift"][field] = {"expected": recorded[field], "got": health.get(field)}

    on_file = texts_sha256(doc)
    if on_file != doc.get("textsSha256"):
        report["ok"] = False
        report["textsEdited"] = {"expected": doc.get("textsSha256"), "got": on_file}
        return False, report

    if space_id(doc["canarySet"], doc["cases"]) != doc["spaceId"]:
        report["ok"] = False
        report["spaceIdMismatch"] = True
        return False, report

    if CAP_FIELD in report["identityDrift"]:
        report["ok"] = False
        return False, report

    for case in doc["cases"]:
        expected = decode_vector(case["v"])
        actual = embed_one(url, case["text"])
        differing = sum(1 for x, y in zip(expected, actual) if x != y)
        row = {
            "id": case["id"],
            "dims": len(expected),
            "gotDims": len(actual),
            "dimsDiffering": differing if len(actual) == len(expected) else None,
            "cosine": round(cosine(expected, actual), 6) if len(actual) == len(expected) else None,
            "maxAbsDelta": max((abs(x - y) for x, y in zip(expected, actual)), default=0)
            if len(actual) == len(expected) else None,
        }
        row["ok"] = len(actual) == len(expected) and differing == 0
        if not row["ok"]:
            report["ok"] = False
        report["cases"].append(row)

    return report["ok"], report


def verdict(report):
    """One line saying what KIND of failure this is. Diagnosis only — every one of them fails."""
    if report.get("textsEdited"):
        return ("the canary file's texts were edited without regenerating it, so these vectors answer "
                "texts that are no longer in the file — nothing here says anything about the embedder")
    if report.get("spaceIdMismatch"):
        return ("the canary file's spaceId does not match the vectors it carries — the file was "
                "hand-edited; regenerate it against a known-good embedder")
    if CAP_FIELD in report["identityDrift"]:
        drift = report["identityDrift"][CAP_FIELD]
        return (f"the service truncates at {drift['got']} tokens and these answers were recorded at "
                f"{drift['expected']}. Every case longer than the smaller cap embeds a different text, so "
                f"no comparison was attempted. Set MAX_TOKENS={drift['expected']}, or regenerate the "
                f"canary at the new cap as part of a full re-embed")
    cosines = [c["cosine"] for c in report["cases"] if c["cosine"] is not None and not c["ok"]]
    if any(c["gotDims"] != c["dims"] for c in report["cases"]):
        return "the service returned a different number of dimensions — a different model, not a drift"
    if cosines and min(cosines) >= 0.9999:
        return ("the same model doing slightly different arithmetic — a BLAS, a kernel, an ORT build. "
                "The vectors are close but not equal, and int8 dot products have no tolerance for that: "
                "it is still a different space")
    return ("a different embedding space. Check the service's configuration first — MAX_TOKENS is the "
            "setting that has actually caused this — then the CPU architecture, the ONNX Runtime version "
            "and the model")


def render(report, out):
    print(f"embed canary: {report['canarySet']} against {report['url']}", file=out)
    print(f"  expected space {report['spaceId']}", file=out)
    h = report["health"]
    print("  service: " + " ".join(f"{f}={h.get(f)}" for f in IDENTITY_FIELDS)
          + f" runtime={h.get('runtime')}", file=out)
    for field, drift in report["identityDrift"].items():
        print(f"  ! {field}: canary recorded {drift['expected']!r}, service reports {drift['got']!r}",
              file=out)
    for row in report["cases"]:
        mark = "ok  " if row["ok"] else "FAIL"
        print(f"  {mark} {row['id']:<16} {row['dimsDiffering']}/{row['dims']} dims differ  "
              f"cosine {row['cosine']}  max|d| {row['maxAbsDelta']}", file=out)
    if not report["ok"]:
        print(f"  verdict: {verdict(report)}", file=out)


def regenerate(doc, url):
    """Re-embed the texts already in the file. Nothing else about the file is touched."""
    health = request(url, "/health")
    dims = None
    for case in doc["cases"]:
        vector = embed_one(url, case["text"])
        if dims is None:
            dims = len(vector)
        elif len(vector) != dims:
            sys.exit(f"{case['id']}: {len(vector)} dims, but {dims} for the cases before it")
        case["v"] = encode_vector(vector)
    doc["dims"] = dims
    doc["embedder"] = {f: health.get(f) for f in IDENTITY_FIELDS}
    doc["embedder"]["runtime"] = health.get("runtime")
    doc["textsSha256"] = texts_sha256(doc)
    doc["spaceId"] = space_id(doc["canarySet"], doc["cases"])
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="the den-embed that is about to embed the corpus")
    ap.add_argument("--canary", default=DEFAULT_CANARY)
    ap.add_argument("--regenerate", action="store_true",
                    help="re-embed the file's own texts and rewrite its vectors — see the module docstring "
                         "for when that is legitimate")
    ap.add_argument("--out", help="where --regenerate writes; '-' for stdout (default: --canary in place)")
    ap.add_argument("--record", help="on success, write the verified space here (finalize stamps it into "
                                     "dataset.meta.json as embeddingSpace)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

    doc = load(args.canary)

    if args.regenerate:
        doc = regenerate(doc, args.url)
        out = args.out or args.canary
        body = json.dumps(doc, ensure_ascii=False, indent=1) + "\n"
        if out == "-":
            sys.stdout.write(body)
        else:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(body)
            print(json.dumps({"wrote": out, "spaceId": doc["spaceId"], "dims": doc["dims"],
                              "cases": len(doc["cases"])}, indent=2))
        return 0

    ok, report = verify(doc, args.url)
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        render(report, sys.stdout if ok else sys.stderr)
    if not ok:
        return 2
    if args.record:
        stamp = {"spaceId": doc["spaceId"], "canarySet": doc["canarySet"], "dims": doc["dims"],
                 "url": args.url, "embedder": report["health"],
                 "verifiedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        os.makedirs(os.path.dirname(os.path.abspath(args.record)), exist_ok=True)
        with open(args.record, "w", encoding="utf-8") as fh:
            json.dump(stamp, fh, indent=1, sort_keys=True)
            fh.write("\n")
    return 0


def gate(url, canary=None, record=None, out=sys.stderr):
    """The one call a writer makes: verify, or exit before anything is written.

    A writer must not be able to hold this wrong. There is no tolerance argument and no way to downgrade
    the result to a warning — a caller that wanted one would be asking to publish a corpus in a space it
    cannot name.
    """
    doc = load(canary or DEFAULT_CANARY)
    ok, report = verify(doc, url)
    render(report, out)
    if not ok:
        sys.exit("embed canary FAILED — refusing to write vectors. "
                 "See pipeline/embed_canary.py for what regenerating the canary does and does not mean.")
    if record:
        os.makedirs(os.path.dirname(os.path.abspath(record)), exist_ok=True)
        with open(record, "w", encoding="utf-8") as fh:
            json.dump({"spaceId": doc["spaceId"], "canarySet": doc["canarySet"], "dims": doc["dims"],
                       "url": url, "embedder": report["health"],
                       "verifiedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                      fh, indent=1, sort_keys=True)
            fh.write("\n")
    return doc["spaceId"]


if __name__ == "__main__":
    sys.exit(main())
