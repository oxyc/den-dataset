#!/usr/bin/env python3
"""Refuse publication of an applied source correction without exact native derived artifacts."""
import argparse
import hashlib
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import cache as caching
from pipeline import embed, enrich, source_corrections
from pipeline.contract import StageError

NATIVE_NAMES = {
    "classify": re.compile(r"^combined-v1-r2.*\.jsonl$"),
    "critique": re.compile(r"^delta-v2.*\.jsonl$"),
    "genresMoods": re.compile(r"^genres-moods-v1.*\.jsonl$"),
    "premise": re.compile(r"^premise-increment/gen/out/batch-[0-9]+\.json$"),
    "document": re.compile(r"^index/labels\.jsonl$"),
    "vector": re.compile(r"^index/vectors\.jsonl$"),
    "withdrawal": re.compile(r"^withdrawn\.jsonl$"),
    "watcher": re.compile(r"^correction-watchers\.json$"),
}


def native_artifact(out_dir, kind, proof):
    relative = proof.get("artifact")
    if not isinstance(relative, str) or not NATIVE_NAMES[kind].match(relative):
        raise StageError(f"correction: {kind} proof names no recognized native stage output")
    root = os.path.realpath(out_dir)
    path = os.path.realpath(os.path.join(root, relative))
    if os.path.commonpath((root, path)) != root or not os.path.isfile(path):
        raise StageError(f"correction: {kind} native artifact is absent from the out-dir")
    if proof.get("artifactSha256") != source_corrections.file_digest(path):
        raise StageError(f"correction: {kind} proof does not hash its native stage output")
    return path


def rows(path):
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if path.endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    value = json.loads(text)
    return value if isinstance(value, list) else [value]


def row_key(row):
    if isinstance(row, dict) and isinstance(row.get("key"), str):
        return row["key"]
    if isinstance(row, dict) and row.get("mediaType") in ("movie", "tv") \
            and isinstance(row.get("tmdbId"), int):
        return enrich.key(row["mediaType"], row["tmdbId"])
    return None


def native_gate(path, out_dir):
    result = source_corrections.publication_gate(path)
    state = source_corrections.load(path)
    evidence = state["evidence"]
    article = source_corrections.read(os.path.join(path, "article-input.json"))
    article_sha = hashlib.sha256((article.get("text") or "").encode()).hexdigest()
    native = {}
    for kind in state["proofs"]:
        proof = source_corrections.read(os.path.join(path, f"proof-{kind}.json"))
        artifact = native_artifact(out_dir, kind, proof)
        if kind == "vector":
            document_proof = source_corrections.read(os.path.join(path, "proof-document.json"))
            labels = native_artifact(out_dir, "document", document_proof)
            found = embed.last_rows(labels, artifact).get(evidence["key"])
            if found is None or found[1] != proof.get("documentSha256"):
                raise StageError("correction: native vector does not bind the exact composed document")
            native[kind] = proof["artifactSha256"]
            continue
        matching = [row for row in rows(artifact) if row_key(row) == evidence["key"]]
        if not matching:
            raise StageError(f"correction: {kind} native output has no row for {evidence['key']}")
        row = matching[-1]
        if kind in ("classify", "critique", "genresMoods"):
            if row.get("articleSha256") != article_sha or row.get("article") != evidence["article"] \
                    or (row.get("language") or "en") != evidence["language"] \
                    or row.get("articleRevId") != evidence["revision"]:
                raise StageError(f"correction: {kind} native row was not made from the frozen article")
        if kind == "premise" and row.get("sourceDigestSha256") != evidence["sourceDigestSha256"]:
            raise StageError("correction: premise native row does not bind the correction source")
        if kind == "document" and source_corrections.digest(row) != proof.get("nativeRowSha256"):
            raise StageError("correction: document proof does not bind the native label row")
        if kind == "withdrawal" and row.get("reason") != proof.get("reason"):
            raise StageError("correction: native withdrawal differs from its receipt")
        if kind == "watcher" and (row.get("article"), row.get("language")) != \
                (evidence["article"], evidence["language"]):
            raise StageError("correction: native watcher lost the source identity")
        native[kind] = proof["artifactSha256"]
    return dict(result, nativeArtifacts=native)


def check(out_dir, stamp=False, record_published=False):
    root = os.path.join(out_dir, "corrections")
    gates = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isfile(os.path.join(path, "state.json")):
                continue
            state = source_corrections.load(path)
            if state["state"] == "appliedPending":
                gate = native_gate(path, out_dir)
                gates.append(gate)
                if record_published:
                    meta = source_corrections.read(os.path.join(out_dir, "dataset.meta.json"))
                    recorded = next((item for item in meta.get("sourceCorrections") or []
                                     if item.get("correctionDigestSha256") == gate["correctionDigestSha256"]), None)
                    if recorded != gate:
                        raise StageError("correction: published manifest does not carry the exact native gate")
                    source_corrections.record_published(
                        path, dict(gate, datasetVersion=meta.get("datasetVersion"),
                                   maxBatchId=meta.get("maxBatchId")))
    if stamp:
        meta_path = os.path.join(out_dir, "dataset.meta.json")
        meta = source_corrections.read(meta_path)
        meta["sourceCorrections"] = gates
        caching.write_atomically(meta_path, (json.dumps(meta, indent=1, sort_keys=True) + "\n").encode())
    return gates


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir")
    parser.add_argument("--stamp", action="store_true")
    parser.add_argument("--record-published", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(check(os.path.abspath(args.out_dir), args.stamp, args.record_published), sort_keys=True))
    except StageError as error:
        print(f"error: {error}. Nothing published.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
