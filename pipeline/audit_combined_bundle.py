#!/usr/bin/env python3
"""Strictly validate a disjoint, exact union of manifested combined Jev shards."""
import argparse
import json
import os
import sys

if not __package__:
    # Run as a file: the repo, not pipeline/, is the import root.
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from pipeline.audit_combined import audit, validate_manifest
from pipeline.article_sections import sha256_text
from pipeline.run_combined import (article_key, attach_enriched_evidence, canonical, load_articles,
                                   sha256_file)


def load_manifest(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", required=True, help="canonical full-corpus article JSONL")
    parser.add_argument("--enriched-dir", required=True)
    parser.add_argument(
        "--out", action="append", required=True,
        help="one shard output; repeat for every shard (manifest defaults to <out>.manifest.json)",
    )
    args = parser.parse_args(argv)

    full_articles_sha = sha256_file(args.articles)
    full_records, full_keys = load_articles(args.articles)
    attach_enriched_evidence(full_records, args.enriched_dir)
    full_by_key = {article_key(record): record for record in full_records}
    sources, shard_summary = [], []
    for output in args.out:
        manifest_path = output + ".manifest.json"
        manifest = load_manifest(manifest_path)
        source_articles = manifest["config"].get("articles")
        source_enriched = manifest["config"].get("enrichedDir")
        if not isinstance(source_articles, str) or not isinstance(source_enriched, str):
            raise ValueError(f"{manifest_path}: shard source paths are missing")
        source_records, source_keys = load_articles(source_articles)
        enriched_sha = attach_enriched_evidence(source_records, source_enriched)
        validate_manifest(manifest, source_articles, enriched_sha)

        extra = source_keys - full_keys
        if extra:
            raise ValueError(f"{manifest_path}: {len(extra):,} source keys absent from full input")
        for record in source_records:
            key = article_key(record)
            if canonical(record) != canonical(full_by_key[key]):
                raise ValueError(f"{manifest_path}: source record {key} differs from full input")

        sources.append({"output": output, "manifest": manifest, "allowedKeys": source_keys})
        shard_summary.append({
            "output": os.path.abspath(output),
            "outputSha256": sha256_file(output),
            "manifest": os.path.abspath(manifest_path),
            "manifestSha256": sha256_file(manifest_path),
            "sourceArticles": source_articles,
            "sourceArticlesSha256": sha256_file(source_articles),
            "sourceKeys": len(source_keys),
            "runId": manifest["runId"],
            "configSha256": manifest["configSha256"],
        })

    summary = audit(full_records, sources=sources)
    if sha256_file(args.articles) != full_articles_sha:
        raise ValueError("full article input changed during bundle audit")
    for shard in shard_summary:
        for path_key, hash_key in (
            ("output", "outputSha256"),
            ("manifest", "manifestSha256"),
            ("sourceArticles", "sourceArticlesSha256"),
        ):
            if sha256_file(shard[path_key]) != shard[hash_key]:
                raise ValueError(f"{shard[path_key]} changed during bundle audit")
    summary["bundle"] = {
        "schemaVersion": "combined-jev-bundle-v1",
        "articles": os.path.abspath(args.articles),
        "articlesSha256": full_articles_sha,
        "enrichedEvidenceSha256": sha256_text(canonical({
            article_key(record): {
                "year": record.get("year"), "plotSections": record.get("plotSections") or [],
                "article": record.get("article"), "articleRevId": record.get("revId"),
                "extractorArticleRevId": record.get("extractorArticleRevId", record.get("revId")),
            }
            for record in full_records
        })),
        "shards": shard_summary,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
