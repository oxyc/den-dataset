"""The parts the stage declarations do not hold, each edge traced to a line.

(from, to, label, where, line, must, inferred[, extra])

`where` is a den-dataset path, cited against the merged tree and resolved to the ref that carries that
line; or `repo@path` for a sibling checkout, read at its HEAD. `must` is a substring the cited line has to
contain — gen.py refuses to build when one does not, so a citation cannot silently drift. `extra` is a dict
of fields copied onto the edge (e.g. `previous_generation`).

Labels a reader sees lead with what a thing IS; a version-coded or internal name follows as the filename.
"""

N = {}  # id -> (label, kind, group, phase, licence)


def node(i, label, kind, group, phase, licence=""):
    N[i] = (label, kind, group, phase, licence)


# What each declared artifact is, in plain words; the filename follows on its own line.
PLAIN = {
    "export_movie": "TMDB movie id dump", "export_tv": "TMDB series id dump",
    "universe_movie": "movie ids to enrich", "universe_tv": "series ids to enrich",
    "enriched": "enriched title records", "enrich_checkpoint": "enrich resume state",
    "articles": "Wikipedia article dump",
    "combined": "Jev answers: facets, scores,<br/>genre/theme/mood yes-no",
    "combined_manifest": "Jev run provenance",
    "delta": "Jev answers: critique,<br/>technique, audience",
    "doc_facts": "director + genre facts<br/>for the embedding document",
    "embed_labels": "embed store: label rows", "embed_vectors": "embed store: vectors",
    "composition": "document-shape record", "embedder": "embedder identity",
    "embedding_space": "verified embedding space", "corpus": "per-title corpus", "entities": "entity names",
    "corpus_facts": "Wikidata facts,<br/>titles with a vector", "delta_ids": "ids needing facts,<br/>no vector",
    "delta_facts": "Wikidata facts,<br/>titles without a vector", "facts": "merged Wikidata facts",
    "vectors": "plot vectors", "vector_labels": "genre &amp; mood labels",
    "premise_vectors": "premise-tag vectors", "premise_labels": "genre &amp; mood labels,<br/>premise row order",
    "store": "the store", "manifest": "release manifest", "release": "data-latest release",
}

# ---- external sources ---------------------------------------------------------------------------
node("src_tmdb_export", "TMDB daily ID exports<br/>files.tmdb.org", "source", "g_tmdb", "p1", "tmdb")
node("src_tmdb_api", "TMDB API<br/>/discover · /{media}/{id}", "source", "g_tmdb", "p1", "tmdb")
node("src_wdqs", "Wikidata SPARQL<br/>query.wikidata.org", "source", "g_wiki", "p1", "wd")
node("src_wp_action", "Wikipedia action API<br/>{lang}.wikipedia.org", "source", "g_wiki", "p1", "wp")
node("src_wp_ent", "Wikimedia Enterprise<br/>structured-contents", "source", "g_wiki", "p1", "wp")
node("src_ent_auth", "Enterprise login<br/>auth.enterprise.wikimedia.com", "source", "g_wiki", "p1", "")
node("src_jev", "Jev decision model<br/>api.typesafe.ai (paid)", "source", "g_models", "p2", "model")
node("src_subagents", "Claude subagents<br/>genre &amp; mood labelling", "source", "g_models", "p2", "model")
node("src_haiku", "Haiku subagents<br/>premise tags", "source", "g_models", "p3", "model")
node("src_denembed", "den-embed<br/>self-hosted bge-m3", "source", "g_models", "p4", "")
node("src_imdb", "IMDb title.ratings<br/>datasets.imdbws.com", "source", "g_imdb", "p6", "imdb")

# ---- caches -------------------------------------------------------------------------------------
node("cache_tmdb", "TMDB response cache<br/>.cache/tmdb, 180 d", "cache", "g_tmdb", "p1", "tmdb")
node("cache_wiki", "Wikipedia + SPARQL cache<br/>.cache/wiki, 180 d", "cache", "g_wiki", "p1", "wp+wd")

# ---- committed files and hand inputs -------------------------------------------------------------
node("file_denenv", "credentials<br/>den.env", "file", "g_fetch", "p1", "")
node("ext_den_labels", "genre &amp; mood labels the app once bundled<br/>den …/labels-t02.json (deleted in fce66d6)", "file", "g_fetch", "p1", "model")
node("op_hand", "operator, by hand<br/>docs/OPERATE.md 6a", "file", "g_facts", "p5", "")
node("file_canary", "embedding known answers<br/>data/embed-canary.json", "file", "g_embed", "p4", "ours")
node("file_premise_v2", "premise tag strings, v2<br/>data/premise-tags-v2.json", "file", "g_classify", "p3", "model")
node("file_premise_v1", "premise tag strings, v1<br/>data/premise-tags-v1.json", "file", "g_classify", "p3", "model")
node("file_golden", "hand-labelled test set<br/>data/eval/golden-large.json", "file", "g_publish", "p6", "ours")
node("file_taxonomy", "genre &amp; mood vocabulary, read as data<br/>Sources/DenDataset/Taxonomy.swift", "file", "g_classify", "p2", "ours")
node("file_prompt", "Jev prompt<br/>scripts/v2/prompts/facets-v2.md", "file", "g_classify", "p2", "ours")
node("file_classify_vocab", "labelling vocabulary + instructions<br/>classify/vocab.json, SPEC.md (no code producer)", "file", "g_classify", "p2", "ours")
node("file_classify_ckpt", "retired classify checkpoint<br/>classify-checkpoint.json", "file", "g_embed", "p4", "ours")

# ---- the previous generation's labels, read before this run's finalize writes them -------------
node("vector_labels_prev", "genre &amp; mood labels, previous generation<br/>labels-t02.json (already in the out-dir)", "artifact", "g_embed", "p4", "model")

# ---- side passes outside STAGES ------------------------------------------------------------------
node("sc_build_worklist", "scripts/build-worklist.py", "script", "g_fetch", "p1")
node("sc_delta_run", "daily freshness pass<br/>scripts/delta-run.sh", "script", "g_fetch", "p1")
node("sc_backfill_prov", "scripts/backfill-plot-provenance.py", "script", "g_fetch", "p1")
node("sc_run_delta", "Jev critique pass<br/>scripts/v2/run_delta.py", "script", "g_classify", "p2")
node("sc_merge_classify", "scripts/v2/merge_classify_labels.py", "script", "g_classify", "p2")
node("sc_classify_backfill", "scripts/v2/build_classify_backfill.py", "script", "g_classify", "p2")
node("sc_premise_worklist", "scripts/v2/build_premise_worklist.py", "script", "g_classify", "p3")
node("sc_merge_premise", "scripts/v2/merge_premise_tags.py", "script", "g_classify", "p3")
node("sc_embed_premise", "scripts/v2/embed_premise_v2.py", "script", "g_classify", "p3")
node("sc_premise_labels", "scripts/v2/build_premise_labels.py", "script", "g_classify", "p3")
node("sc_merge_premise_cov", "scripts/merge-premise-coverage.py", "script", "g_classify", "p3")
node("sc_build_premise_tags", "scripts/build-premise-tags.py<br/>(registered producer)", "script", "g_classify", "p3")
node("sc_embed_tags", "scripts/v2/embed_tags.py<br/>(registered producer)", "script", "g_classify", "p3")
node("sc_derive_docfacts", "scripts/v2/derive_doc_facts.py", "script", "g_embed", "p4")
node("sc_embed_docs", "scripts/v2/embed_docs.py<br/>on the box", "script", "g_embed", "p4")
node("sc_import_box", "scripts/v2/import_box_vectors.py", "script", "g_embed", "p4")
node("sc_recluster", "scripts/recluster.py", "script", "g_embed", "p4")
node("sc_merge_facts", "scripts/merge-facts.py", "script", "g_facts", "p5")

# ---- intermediate files no stage declares -------------------------------------------------------
node("af_worklist_reembed", "shipped ids, popularity order<br/>worklist-{movie,tv}.json", "artifact", "g_fetch", "p1", "tmdb-ids")
node("af_classify_phase", "genre &amp; mood labelling batches<br/>classify/{in,out}/batch-*.json", "artifact", "g_classify", "p2", "model")
node("af_premise_batches", "premise tagging batches<br/>gen/{in,out}/batch-*.json", "artifact", "g_classify", "p3", "wp+model")
node("af_premise_v2_bin", "premise vectors as embedded<br/>vectors-premise-v2.bin + premise-v2-ids.json", "artifact", "g_classify", "p3", "model")
node("af_premise_v1_realigned", "v1 premise vectors, re-embedded<br/>vectors-premise-v1-realigned.bin + keys (no code producer)", "artifact", "g_classify", "p3", "model")
node("af_premise_ids", "premise row order<br/>premise-ids.json", "artifact", "g_classify", "p3", "ours")
node("af_coverage_fill", "coverage-fill premise vectors<br/>vectors-{label}.bin + keys", "artifact", "g_classify", "p3", "model")
node("af_docs", "composed documents<br/>docs.jsonl (--dump-docs)", "artifact", "g_embed", "p4", "wp+wd+model")
node("af_box_vectors", "vectors embedded on the box<br/>vectors.jsonl, keys.json, embedding-space.json", "artifact", "g_embed", "p4", "wp-emb")
node("af_report", "coverage report + gzip twin<br/>report.json, labels-t02.json.gz", "artifact", "g_embed", "p4", "ours")
node("af_recluster_report", "new-cluster candidates<br/>recluster-&lt;date&gt;.json", "artifact", "g_embed", "p4", "ours")
node("af_facts_ckpt", "facts scrape checkpoints<br/>facts-fields / -entities / -source-types", "artifact", "g_facts", "p5", "wd")

# ---- gates --------------------------------------------------------------------------------------
node("gate_spend", "--spend<br/>(SPENDS)", "gate", "g_classify", "p2")
node("gate_publish", "--publish<br/>(PUBLISHES)", "gate", "g_publish", "p6")
node("gate_mode", "--mode refusal", "gate", "g_fetch", "p1")
node("gate_classify_validate", "labelling batch validator<br/>validate_classify_batch.py", "gate", "g_classify", "p2")
node("gate_canary", "embed canary", "gate", "g_embed", "p4")
node("gate_embedgates", "embedder · composition<br/>· token-fit refusals", "gate", "g_embed", "p4")
node("gate_shipguard", "blocks prose fields in labels<br/>SHIP_GUARD", "gate", "g_embed", "p4")
node("gate_prose", "corpus prose refusal", "gate", "g_store", "p5")
node("gate_provenance", "no vendor-content sections<br/>PROVENANCE / VENDOR_ALLOWED", "gate", "g_store", "p5")
node("gate_guards", "publish guards<br/>producers · counts · identity<br/>· filename-version · consistency", "gate", "g_publish", "p6")
node("gate_grounding", "grounding ratchet<br/>check-plot-invariants.py", "gate", "g_publish", "p6")
node("gate_quality", "quality report<br/>eval-taxonomy.py (not enforced)", "gate", "g_publish", "p6")
node("gate_box_sync", "sha256 + den-atlas check<br/>on the staged set", "gate", "g_consumers", "p6")
node("gate_fetch_verify", "per-blob sha256", "gate", "g_consumers", "p6")
node("gate_signature", "signature vs pinned key", "gate", "g_consumers", "p6")

# ---- releases and consumers ---------------------------------------------------------------------
node("rel_corpus", "corpus release<br/>corpus-&lt;ver&gt; (by hand)", "release", "g_release", "p6", "wd+model")
node("con_atlas_fetch", "den-atlas<br/>scripts/fetch-dataset.sh", "consumer", "g_consumers", "p6")
node("con_box_sync", "den deploy/<br/>atlas-dataset-sync.sh", "consumer", "g_consumers", "p6")
node("con_atlas", "den-atlas<br/>mmaps the store", "consumer", "g_consumers", "p6")
node("con_edge", "den-edge<br/>/atlas relay + Den Web", "consumer", "g_consumers", "p6")
node("con_tv", "Den TV app", "consumer", "g_consumers", "p6")
node("con_signer", "den scripts/sign-dataset.swift<br/>(offline)", "consumer", "g_consumers", "p6")

A = "den-atlas"
D = "den"
E = "den-edge"
PREV = {"previous_generation": True}

EDGES = [
    # ---- p1: universe and enrichment ------------------------------------------------------------
    ("src_tmdb_export", "sc_build_worklist", "GET {kind}_ids_<date>.json.gz (plain http)", "scripts/build-worklist.py", 41, "files.tmdb.org", False),
    ("sc_build_worklist", "export_movie", "writes movie_ids.json.gz; gunzip by hand", "scripts/build-worklist.py", 37, "_ids.json.gz", False),
    ("sc_build_worklist", "export_tv", "writes tv_series_ids.json.gz; gunzip by hand", "scripts/build-worklist.py", 37, "_ids.json.gz", False),
    ("ext_den_labels", "sc_build_worklist", "LABELS default, read before any fetch", "scripts/build-worklist.py", 29, "labels-t02.json", False),
    ("sc_build_worklist", "af_worklist_reembed", "shipped ids, popularity order", "scripts/build-worklist.py", 73, "worklist-", False),
    ("af_worklist_reembed", "fetch", "--set universe_*=worklist-*.json", "docs/OPERATE.md", 188, "--set universe_movie=out/worklist-movie.json", True),
    ("src_tmdb_api", "worklist", "/discover pages (discover, delta)", "pipeline/worklist.py", 156, "client.discover(", False),
    ("gate_mode", "worklist", "no default universe", "pipeline/worklist.py", 94, "if ctx.mode not in MODES", False),
    ("src_tmdb_api", "fetch", "detail + keywords + credits per id", "pipeline/enrich.py", 166, "append_to_response", False),
    ("cache_tmdb", "fetch", "cached detail body", "lib/tmdb.py", 193, "self.cache.read", False),
    ("fetch", "cache_tmdb", "stores detail body (never /discover)", "lib/tmdb.py", 199, "self.cache.write", False),
    ("src_wdqs", "fetch", "mapping SPARQL: article, source work, sitelinks, creators, runtime", "pipeline/enrich.py", 360, "wikidata.mapping(", False),
    ("fetch", "cache_wiki", "stores SPARQL body", "lib/wikidata.py", 344, "cache.write", False),
    ("src_wp_action", "fetch", "action=parse wikitext, en then other languages", "lib/plot.py", 205, "wikipedia.fetch_parse", False),
    ("cache_wiki", "fetch", "cached action/SPARQL bodies", "pipeline/enrich.py", 326, "wikipedia.cache_for()", False),
    ("fetch", "cache_wiki", "stores action=parse body", "lib/wikipedia.py", 157, "cache.write", False),
    ("src_wp_ent", "fetch", "pre-sectioned plot; never cached", "lib/plot.py", 341, "ENTERPRISE_HOST", False),
    ("src_ent_auth", "fetch", "24 h bearer (enterprise_login)", "scripts/lib/den-env.sh", 42, "auth.enterprise.wikimedia.com", False),
    ("file_denenv", "fetch", "TMDB_API_KEY + Enterprise credentials", "scripts/lib/den-env.sh", 24, ". ./den.env", False),
    ("sc_delta_run", "worklist", "--mode delta --since, lists into delta/", "scripts/delta-run.sh", 67, "den stage worklist --mode delta", False),
    ("vector_labels_prev", "sc_delta_run", "published genre & mood labels as --known", "scripts/delta-run.sh", 53, "labels-t*.json", False, PREV),
    ("sc_delta_run", "enriched", "one pipeline.enrich batch per media", "scripts/delta-run.sh", 83, "python3 -m pipeline.enrich", False),
    ("enriched", "sc_backfill_prov", "--enriched-dir", "scripts/backfill-plot-provenance.py", 200, "--enriched-dir", False),
    ("cache_wiki", "sc_backfill_prov", "replays grounding from cached bodies", "scripts/backfill-plot-provenance.py", 203, "--cache-dir", False),
    ("sc_backfill_prov", "enriched", "adds plotArticleRole / Redirected", "scripts/backfill-plot-provenance.py", 201, "--out-dir", False),

    # ---- p2: articles, Jev answers, genre & mood labels -----------------------------------------
    ("src_wp_action", "st_articles", "whole article, re-read", "pipeline/articles.py", 195, "wikipedia.article_prose", False),
    ("cache_wiki", "st_articles", "answers nearly all of it", "pipeline/articles.py", 225, "wikipedia.cache_for()", False),
    ("classify", "src_jev", "one typed request per title", "scripts/v2/run_combined.py", 280, "client.ask_with_metadata", False),
    ("src_jev", "classify", "answers (jev-1.13.0)", "scripts/v2/typesafe_client.py", 28, "api.typesafe.ai", False),
    ("file_taxonomy", "classify", "vocabulary parsed + hashed into the manifest", "scripts/v2/combined_questions.py", 14, "Taxonomy.swift", False),
    ("file_prompt", "classify", "prompt", "scripts/v2/combined_questions.py", 15, "facets-v2.md", False),
    ("file_denenv", "classify", "TYPESAFE_API_KEY", "scripts/v2/typesafe_client.py", 50, "den.env", False),
    ("file_denenv", "sc_run_delta", "TYPESAFE_API_KEY", "scripts/v2/typesafe_client.py", 50, "den.env", False),
    ("file_taxonomy", "sc_run_delta", "--taxonomy default", "scripts/v2/run_delta.py", 36, "rc.TAXONOMY", False),
    ("file_prompt", "sc_run_delta", "--prompt default", "scripts/v2/run_delta.py", 35, "rc.PROMPT", False),
    ("articles", "sc_run_delta", "--articles", "scripts/v2/run_delta.py", 49, "rc.load_articles(args.articles)", False),
    ("enriched", "sc_run_delta", "--enriched-dir", "scripts/v2/run_delta.py", 50, "attach_enriched_evidence", False),
    ("sc_run_delta", "src_jev", "paid_run: no --spend gate", "scripts/v2/run_delta.py", 69, "rc.paid_run", False),
    ("sc_run_delta", "delta", "writes delta-v1*.jsonl", "scripts/v2/run_delta.py", 65, "args.out", False),
    ("af_classify_phase", "src_subagents", "in/batch-NNNN.json", "scripts/v2/classify/AGENT.md", 16, "classify/in/batch-NNNN.json", False),
    ("file_classify_vocab", "src_subagents", "the only labels allowed", "scripts/v2/classify/AGENT.md", 14, "vocab.json", False),
    ("src_subagents", "af_classify_phase", "out/batch-NNNN.json, one agent per batch", "scripts/v2/classify/AGENT.md", 17, "out/batch-NNNN.json", True),
    ("af_classify_phase", "sc_classify_backfill", "rows a batch dropped", "scripts/v2/build_classify_backfill.py", 40, "in_dir", False),
    ("file_classify_vocab", "sc_classify_backfill", "vocab.json", "scripts/v2/build_classify_backfill.py", 33, "vocab.json", False),
    ("sc_classify_backfill", "af_classify_phase", "re-batches them into in/", "scripts/v2/build_classify_backfill.py", 68, "json.dump(slice_", False),
    ("af_classify_phase", "gate_classify_validate", "each in/out pair", "scripts/v2/validate_classify_batch.py", 114, "in_dir", False),
    ("file_classify_vocab", "gate_classify_validate", "vocab.json", "scripts/v2/validate_classify_batch.py", 103, "vocab.json", False),
    ("gate_classify_validate", "sc_merge_classify", "merge acts on what the validator reports", "scripts/v2/merge_classify_labels.py", 12, "validate_classify_batch.py", False),
    ("af_classify_phase", "sc_merge_classify", "--phase in/ + out/", "scripts/v2/merge_classify_labels.py", 73, "in_dir, out_dir", False),
    ("file_classify_vocab", "sc_merge_classify", "vocab.json", "scripts/v2/merge_classify_labels.py", 66, "vocab.json", False),
    ("vector_labels_prev", "sc_merge_classify", "--labels: the labels to extend", "scripts/v2/merge_classify_labels.py", 69, "json.load(open(args.labels", False, PREV),
    ("sc_merge_classify", "vector_labels_prev", "adds ~9,010 titles in place (undeclared writer)", "scripts/v2/merge_classify_labels.py", 136, "json.dump(store, open(args.out", False, PREV),

    # ---- p3: premise tags -----------------------------------------------------------------------
    ("combined", "sc_premise_worklist", "--combined, every shard", "scripts/v2/build_premise_worklist.py", 114, "bundle(args.combined)", False),
    ("articles", "sc_premise_worklist", "--articles, section text", "scripts/v2/build_premise_worklist.py", 99, "open(args.articles", False),
    ("file_premise_v2", "sc_premise_worklist", "skip titles already tagged", "scripts/v2/build_premise_worklist.py", 69, "premise-tags-v2.json", False),
    ("file_premise_v1", "sc_premise_worklist", "skip tagged titles; merge-only list", "scripts/v2/build_premise_worklist.py", 148, "premise-tags-v1.json", False),
    ("sc_premise_worklist", "af_premise_batches", "gen/in: premise sections only", "scripts/v2/build_premise_worklist.py", 178, "batch-{i:04d}.json", False),
    ("af_premise_batches", "src_haiku", "one subagent per batch", "scripts/v2/llm_phase.py", 5, "written by one subagent each", True),
    ("src_haiku", "af_premise_batches", "gen/out/batch-NNNN.json", "scripts/v2/llm_phase.py", 5, "written by one subagent each", True),
    ("af_premise_batches", "sc_merge_premise", "--phase gen", "scripts/v2/merge_premise_tags.py", 87, "--phase", False),
    ("sc_merge_premise", "file_premise_v2", "writes the committed tags", "scripts/v2/merge_premise_tags.py", 4, "--out data/premise-tags-v2.json", False),
    ("file_premise_v2", "sc_embed_premise", "--tags", "scripts/v2/embed_premise_v2.py", 84, "premise-tags-v2.json", False),
    ("file_premise_v1", "sc_embed_premise", "--v1-tags: reuse unchanged strings", "scripts/v2/embed_premise_v2.py", 103, "args.v1_tags", False),
    ("af_premise_v1_realigned", "sc_embed_premise", "--base-vectors / --base-keys", "scripts/v2/embed_premise_v2.py", 85, "vectors-premise-v1-realigned.bin", False),
    ("file_canary", "sc_embed_premise", "canary gate first", "scripts/v2/embed_premise_v2.py", 100, "embed_canary.gate", False),
    ("sc_embed_premise", "src_denembed", "embeds tag strings", "scripts/v2/embed_premise_v2.py", 90, "--url", False),
    ("sc_embed_premise", "af_premise_v2_bin", "writes blob + row order", "scripts/v2/embed_premise_v2.py", 156, "vectors-premise-v2.bin", False),
    ("af_premise_v2_bin", "premise_vectors", "copied by hand; no code renames v2", "docs/OPERATE.md", 76, "vectors-premise-v2.bin", True),
    ("af_premise_v2_bin", "sc_premise_labels", "--ids premise-v2-ids.json", "scripts/v2/build_premise_labels.py", 38, "--ids", False),
    ("vector_labels", "sc_premise_labels", "--labels, record per title", "scripts/v2/build_premise_labels.py", 39, "--labels", False),
    ("sc_premise_labels", "premise_labels", "writes labels-premise.json in blob order", "scripts/v2/build_premise_labels.py", 68, "json.dump(out, open(args.out", False),
    ("sc_embed_tags", "af_coverage_fill", "writes vectors-{label}.bin", "scripts/v2/embed_tags.py", 164, "vectors-{args.label}.bin", False),
    ("af_coverage_fill", "sc_merge_premise_cov", "coverage-fill keys + vectors", "scripts/merge-premise-coverage.py", 52, "keys-coverage-fill.json", False),
    ("premise_vectors", "sc_merge_premise_cov", "base blob", "scripts/merge-premise-coverage.py", 44, "vectors-premise.bin", False),
    ("premise_labels", "sc_merge_premise_cov", "base labels", "scripts/merge-premise-coverage.py", 50, "labels-premise.json", False),
    ("af_premise_ids", "sc_merge_premise_cov", "base row order", "scripts/merge-premise-coverage.py", 49, "premise-ids.json", False),
    ("vector_labels", "sc_merge_premise_cov", "genre & mood labels for the filled rows", "scripts/merge-premise-coverage.py", 87, "labels-t02.json", False),
    ("sc_merge_premise_cov", "premise_vectors", "writes the merged blob (second writer)", "scripts/merge-premise-coverage.py", 83, "vectors-premise.bin", False),
    ("sc_merge_premise_cov", "premise_labels", "writes merged labels (second writer)", "scripts/merge-premise-coverage.py", 101, "labels-premise.json", False),
    ("sc_merge_premise_cov", "af_premise_ids", "writes merged row order", "scripts/merge-premise-coverage.py", 97, "premise-ids.json", False),
    ("af_premise_ids", "sc_build_premise_tags", "positional mediaType", "scripts/build-premise-tags.py", 26, "premise-ids.json", False),
    ("premise_labels", "sc_build_premise_tags", "READS it; the registry calls this its producer", "scripts/build-premise-tags.py", 27, "labels-premise.json", False),
    ("sc_build_premise_tags", "file_premise_v1", "writes the v1 tags", "scripts/build-premise-tags.py", 23, "premise-tags-v1.json", False),

    # ---- p4: embedding --------------------------------------------------------------------------
    ("finalize", "vector_labels_prev", "this run's labels are the next run's input", "pipeline/__init__.py", 35, "previous finalize", False, PREV),
    ("src_wdqs", "docfacts", "P57 director + P136 genre, 100 ids/query", "pipeline/docfacts.py", 140, "wikidata.doc_facts", False),
    ("cache_wiki", "docfacts", "cached SPARQL bodies", "pipeline/docfacts.py", 133, "wikidata.cache_for()", False),
    ("docfacts", "cache_wiki", "stores SPARQL body", "lib/wikidata.py", 141, "cache.write", False),
    ("facts", "sc_derive_docfacts", "--facts: the previous generation's facts", "scripts/v2/derive_doc_facts.py", 38, "--facts", False, PREV),
    ("sc_derive_docfacts", "doc_facts", "--out doc-facts.json", "scripts/v2/derive_doc_facts.py", 39, "--out", False),
    ("file_canary", "embed", "known answers, every run", "pipeline/embed.py", 174, "denembed.verify(canary_path()", False),
    ("gate_canary", "embed", "no vector without a verified space", "pipeline/embed.py", 272, "stamp.get(\"spaceId\")", False),
    ("gate_embedgates", "embed", "refuses a mixed store", "pipeline/embed.py", 147, "same_embedder", False),
    ("embed", "src_denembed", "GET /health identity", "lib/denembed.py", 60, "\"/health\"", False),
    ("embed", "src_denembed", "POST /embed/batch, 7 docs a request", "lib/denembed.py", 94, "/embed/batch", False),
    ("src_denembed", "embed", "int8[1024] per document", "pipeline/embed.py", 278, "denembed.embed_many", False),
    ("embed", "af_docs", "--dump-docs: compose, embed nothing", "pipeline/embed.py", 332, "open(ctx.dump_docs", False),
    ("af_docs", "sc_embed_docs", "--docs", "scripts/v2/embed_docs.py", 86, "--docs", False),
    ("file_canary", "sc_embed_docs", "--canary", "scripts/v2/embed_docs.py", 91, "--canary", False),
    ("sc_embed_docs", "src_denembed", "embeds on the serving box", "scripts/v2/embed_docs.py", 88, "--url", False),
    ("sc_embed_docs", "af_box_vectors", "writes vectors.jsonl (:97), embedding-space.json (:129), keys.json (:162)", "scripts/v2/embed_docs.py", 97, "\"vectors.jsonl\"", False),
    ("af_box_vectors", "sc_import_box", "--vectors / --embed-space", "scripts/v2/import_box_vectors.py", 57, "--vectors", False),
    ("file_canary", "sc_import_box", "space checked against the canary", "scripts/v2/import_box_vectors.py", 71, "embed_canary.load", False),
    ("vector_labels_prev", "sc_import_box", "--labels: record per title", "scripts/v2/import_box_vectors.py", 58, "--labels", False, PREV),
    ("sc_import_box", "embed_labels", "writes the index store", "scripts/v2/import_box_vectors.py", 59, "--out-dir", False),
    ("sc_import_box", "embed_vectors", "writes the index store", "scripts/v2/import_box_vectors.py", 59, "--out-dir", False),
    ("sc_import_box", "embedding_space", "carries the verified space", "scripts/v2/import_box_vectors.py", 63, "--embed-space", False),
    ("sc_import_box", "embedder", "--embedder-health as embedder.json", "scripts/v2/import_box_vectors.py", 60, "--embedder-health", False),
    ("finalize", "af_report", "written, read by nothing", "pipeline/finalize.py", 312, "report.json", False),
    ("manifest", "finalize", "merges over the existing manifest", "pipeline/finalize.py", 308, "merge_manifest", False),
    ("enrich_checkpoint", "finalize", "read by hardcoded name, undeclared", "pipeline/finalize.py", 224, "enrich-checkpoint.json", False),
    ("file_classify_ckpt", "finalize", "read by hardcoded name, undeclared", "pipeline/finalize.py", 224, "classify-checkpoint.json", False),
    ("gate_shipguard", "finalize", "refuses prose keys in labels", "pipeline/finalize.py", 269, "prohibited(labels)", False),
    ("vector_labels", "sc_recluster", "--labels", "scripts/recluster.py", 137, "--labels", False),
    ("vectors", "sc_recluster", "--vectors", "scripts/recluster.py", 138, "--vectors", False),
    ("sc_recluster", "af_recluster_report", "new-cluster candidates", "scripts/recluster.py", 139, "--out", False),

    # ---- p5: facts, corpus, store ---------------------------------------------------------------
    ("src_wdqs", "st_facts", "per-property SPARQL, 25 ids a batch", "pipeline/facts.py", 155, "wd.fetch_facts", False),
    ("cache_wiki", "st_facts", "cached sparql-facts bodies", "lib/wikidata_facts.py", 240, "cache.read", False),
    ("st_facts", "cache_wiki", "stores sparql-facts body", "lib/wikidata_facts.py", 249, "cache.write", False),
    ("src_wdqs", "st_facts", "titles, entity names, instance-of: uncached", "pipeline/facts.py", 160, "wd.titles(batch, media)", False),
    ("st_facts", "af_facts_ckpt", "checkpoints per pass", "pipeline/facts.py", 262, "facts-fields.json", False),
    ("st_facts", "sc_merge_facts", "corpus pass first; first file wins", "pipeline/facts.py", 301, "subprocess.run(argv(ctx))", False),
    ("gate_prose", "st_corpus", "refuses answer keys that look like prose", "scripts/v2/consolidate_corpus.py", 176, "refusing to write source prose", False),
    ("gate_provenance", "st_store", "no vendor-content section", "scripts/v2/build_store.py", 197, "vendor - VENDOR_ALLOWED", False),
    ("st_store", "manifest", "stamps store name, hash, inputs, facet-gate counts: only with --stamp-meta, which den leaves empty", "pipeline/store.py", 65, "--stamp-meta", False),

    # ---- p6: publish, releases, consumers -------------------------------------------------------
    ("publish", "manifest", "prunes retired keys; stamps counts (:275), grounding census (:322), storeRebuild (:256)", "scripts/publish-dataset.sh", 75, "--prune", False),
    ("release", "publish", "published meta, for comparison", "scripts/publish-dataset.sh", 177, "gh release download data-latest", False),
    ("gate_guards", "publish", "any refusal ends the publish", "scripts/publish-dataset.sh", 370, "check-producers.py", False),
    ("enriched", "gate_grounding", "--enriched-dir", "scripts/publish-dataset.sh", 322, "--enriched-dir", False),
    ("vector_labels", "gate_grounding", "--labels", "scripts/publish-dataset.sh", 322, "--labels", False),
    ("gate_grounding", "publish", "refuses an increase in shared articles", "scripts/publish-dataset.sh", 329, "check-plot-invariants.py", False),
    ("vector_labels", "gate_quality", "genre & mood labels scored", "scripts/publish-dataset.sh", 429, "eval-taxonomy.py", False),
    ("file_golden", "gate_quality", "--golden", "scripts/publish-dataset.sh", 430, "golden-large.json", False),
    ("gate_quality", "publish", "report only: || true", "scripts/publish-dataset.sh", 430, "|| true", False),
    ("corpus", "rel_corpus", "uploaded by hand", "LICENSES.md", 13, "corpus-<ver>", True),
    ("entities", "rel_corpus", "uploaded by hand", "LICENSES.md", 13, "corpus-<ver>", True),
    ("release", "con_atlas_fetch", "dataset.meta.json, then the store", A + "@scripts/fetch-dataset.sh", 13, "releases/download/data-latest", False),
    ("gate_fetch_verify", "con_atlas_fetch", "refuses a blob whose sha differs", A + "@scripts/fetch-dataset.sh", 136, "shasum -a 256", False),
    ("release", "con_box_sync", "daily timer on the box", D + "@deploy/atlas-dataset-sync.sh", 29, "releases/download/data-latest", False),
    ("gate_box_sync", "con_box_sync", "sha256 of every blob", D + "@deploy/atlas-dataset-sync.sh", 213, "sha256sum -c", False),
    ("gate_box_sync", "con_box_sync", "den-atlas check on the staged set", D + "@deploy/atlas-dataset-sync.sh", 240, "check /staged", False),
    ("con_atlas_fetch", "con_atlas", "./data/dataset.meta.json → storeFile", A + "@src/dataset.rs", 71, "dataset.meta.json", False),
    ("con_box_sync", "con_atlas", "the mounted data dir", D + "@deploy/atlas-dataset-sync.sh", 2, "mounted den-atlas dataset", False),
    ("src_imdb", "con_atlas", "joined on the store's imdb column at run time", A + "@src/ratings.rs", 25, "datasets.imdbws.com", False),
    ("con_atlas", "src_denembed", "query vectors: must be the same space", A + "@src/main.rs", 290, "EMBED_URL", False),
    ("con_atlas", "con_tv", "/dataset.json descriptor", A + "@src/handler.rs", 257, "/dataset.json", False),
    ("con_atlas", "con_tv", "/index/row · labels · score · /recommend · /embed", D + "@Sources/DenKit/Addons/AddonClient+Index.swift", 99, "index/row", False),
    ("gate_signature", "con_tv", "skips a pinned provider that fails", D + "@App/Den/Shell/AppModel+Indexes.swift", 132, "DatasetSignature.verify", False),
    ("con_atlas", "con_edge", "/atlas/* relayed for Den Web", E + "@src/relay.rs", 2, "/atlas/", False),
    ("manifest", "con_signer", "signs dataset.meta.json", D + "@scripts/sign-dataset.swift", 8, "sign <dataset.meta.json>", False),
    ("con_signer", "manifest", "signature pasted in by hand, then republished", D + "@scripts/sign-dataset.swift", 9, "Put it in the descriptor", True),
]

# Per artifact: what it holds, where it stands, and what guards it.
TABLE = {
    "export_movie": ("TMDB's daily movie id dump (one JSON per line)", "intermediate; gz left by build-worklist.py", "worklist refuses a dump that lost lines"),
    "export_tv": ("TMDB's daily series id dump", "intermediate", "same"),
    "universe_movie": ("worklist rows {tmdbId, mediaType}", "intermediate", "--mode refusal"),
    "universe_tv": ("worklist rows {tmdbId, mediaType}", "intermediate", "--mode refusal"),
    "enriched": ("batch-N.json: TMDB detail half + Wikipedia plot as overview + Wikidata creators/runtime", "intermediate; never published", "overview holds Wikipedia or nothing (tmdb.title_record)"),
    "enrich_checkpoint": ("processed keys, next batch number, totals", "intermediate", "unreadable checkpoint refuses"),
    "articles": ("whole Wikipedia article per grounded title, revId, sections, TMDB title/year", "intermediate; its release articles-2026-09-19 was deleted 2026-09-22", "only titles with a Wikipedia plot"),
    "combined": ("Jev answers: applicability, 12 story facets, 4 register scores, primary genre and a yes/no per subgenre, theme and mood (`nouls`), section roles", "intermediate; its release raw-2026-09-20 was deleted 2026-09-22", "--spend"),
    "combined_manifest": ("run id + hashes of input, prompt, vocabulary, model", "intermediate", "audit_combined.py"),
    "delta": ("Jev answers: critique, technique, depicts, audience", "intermediate; its release raw-2026-09-20 was deleted 2026-09-22", "none (outside STAGES)"),
    "doc_facts": ("Wikidata director + genre per title", "intermediate", "empty result refuses"),
    "embed_labels": ("append-only label rows, paired with vectors", "intermediate", "canary, embedder, composition"),
    "embed_vectors": ("append-only int8 vectors", "intermediate", "canary, embedder, composition"),
    "composition": ("the document shape the vectors were composed in", "intermediate", "embed refuses another shape"),
    "embedder": ("den-embed identity from /health", "intermediate", "embed refuses a different embedder"),
    "embedding_space": ("canary verdict: spaceId", "intermediate; stamped into the manifest", "canary"),
    "vector_labels": ("genre & mood labels per title: primary genre, subgenres, moods", "built; pruned from the manifest", "SHIP_GUARD"),
    "vector_labels_prev": ("the same file, as left by the previous run's finalize (plus merge_classify_labels.py additions)", "must already exist: a fresh out-dir refuses", "contract.require"),
    "vectors": ("plot vectors, keyed int8 blob (`DENVEC02`)", "built; pruned from the manifest", "finalize dim/alignment refusals"),
    "manifest": ("dataset.meta.json: version, embedder, store name/sha/rows, storeInputs", "published last, as the commit point", "publish guards, prune"),
    "corpus_facts": ("Wikidata facts for titles with a vector", "intermediate", "skipped batch refuses the merge"),
    "delta_ids": ("hand-written list of vectorless ids", "intermediate", "facts refuses without it"),
    "delta_facts": ("Wikidata facts for vectorless titles", "intermediate", "skipped batch refuses the merge"),
    "facts": ("merged facts, entities, genreMap", "built; factsFile pruned", "record-count guard (dormant)"),
    "corpus": ("one row per title: facts, genre & mood labels, premise labels, every model answer", "intermediate; hand-published as corpus-<ver>", "--expect, prose refusal"),
    "entities": ("Q-id → name for every referenced entity", "intermediate; hand-published", "check-producers glob"),
    "premise_vectors": ("embeddings of premise tags", "built; pruned from the manifest", "canary (in embed_premise_v2.py)"),
    "premise_labels": ("genre & mood labels in premise-blob row order", "built; pruned from the manifest", "row-order refusals"),
    "store": ("den-<ver>.store: every section den-atlas serves", "PUBLISHED on data-latest", "PROVENANCE, publish guards"),
    "release": ("data-latest GitHub release: store + meta", "PUBLISHED", "--publish"),
    "cache_tmdb": ("raw TMDB detail bodies, overviews included", "cached 180 d (compliance TTL)", "credentials stripped from keys"),
    "cache_wiki": ("action=parse bodies (host in key) + SPARQL bodies", "cached 180 d", "never Enterprise"),
    "af_worklist_reembed": ("the ids already shipped, popularity order", "intermediate", "none"),
    "af_classify_phase": ("genre & mood labelling batches and answers; the first in/ batches have no code producer", "intermediate", "validate_classify_batch.py; merge refuses invented keys"),
    "file_classify_vocab": ("the labels a labelling agent may use, and its instructions", "out-dir only; no code producer", "none"),
    "file_taxonomy": ("the genre & mood vocabulary, as a Swift source file the classify pass parses", "committed", "hashed into the Jev manifest"),
    "af_premise_batches": ("premise evidence batches and Haiku tags", "intermediate", "validate_premise_batch.py"),
    "af_premise_v2_bin": ("premise blob as embedded, and its row order", "intermediate", "canary + reuse probe"),
    "af_premise_v1_realigned": ("v1 premise strings re-embedded, the base v2 extends", "intermediate; no code producer", "reuse probe"),
    "af_premise_ids": ("premise blob row order", "intermediate", "alignment refusal"),
    "af_coverage_fill": ("premise vectors for titles v1 missed", "intermediate", "none"),
    "af_docs": ("composed documents, for embedding elsewhere", "intermediate", "none"),
    "af_box_vectors": ("vectors embedded on the box + verified space", "intermediate", "space checked against the canary"),
    "af_report": ("coverage report and gzip twin", "written, read by nothing", "none"),
    "af_recluster_report": ("new-cluster candidates for a human", "report", "none"),
    "af_facts_ckpt": ("per-pass scrape checkpoints", "intermediate", "unparseable checkpoint refuses"),
    "file_premise_v2": ("44,531 titles' premise tag strings", "committed", "none"),
    "file_premise_v1": ("37,533 titles' v1 tag strings", "committed", "none"),
    "file_canary": ("fixed texts and their exact int8 vectors", "committed", "is the gate"),
}

LICENCE = {
    "tmdb": "TMDB-derived: never publish",
    "tmdb-ids": "TMDB ids (identifiers)",
    "tmdb-fields": "carries TMDB title/year",
    "wp": "Wikipedia text, CC BY-SA",
    "wp-emb": "embedding of Wikipedia text",
    "wd": "Wikidata, CC0",
    "imdb": "IMDb: runtime join only",
    "model": "model output",
    "ours": "our bookkeeping",
}

DECLARED_LICENCE = {
    "export_movie": "tmdb", "export_tv": "tmdb", "universe_movie": "tmdb-ids", "universe_tv": "tmdb-ids",
    "enriched": "tmdb+wp+wd", "enrich_checkpoint": "ours", "articles": "wp+tmdb-fields",
    "combined": "model+tmdb-fields", "combined_manifest": "ours", "delta": "model+tmdb-fields",
    "doc_facts": "wd", "embed_labels": "model", "embed_vectors": "wp-emb", "composition": "ours",
    "embedder": "ours", "embedding_space": "ours", "vector_labels": "model", "vectors": "wp-emb",
    "manifest": "ours", "corpus_facts": "wd", "delta_ids": "tmdb-ids", "delta_facts": "wd", "facts": "wd",
    "corpus": "wd+model", "entities": "wd", "premise_vectors": "model", "premise_labels": "model",
    "store": "wd+model+wp-emb", "release": "wd+model+wp-emb",
}

LICENCE_CITES = [
    ("articles", "TMDB title/year copied into each article row", "pipeline/articles.py", 157, "\"title\": record.get(\"title\")"),
    ("combined", "TMDB title/year copied into each pass row", "scripts/v2/run_combined.py", 357, "\"title\": rec.get(\"title\")"),
    ("enriched", "title, year, genres, keywords, cast from the TMDB detail body", "lib/tmdb.py", 164, "_field(body, \"title\", str)"),
    ("store", "every section's source declared; VENDOR_ALLOWED is empty", "scripts/v2/build_store.py", 158, "VENDOR_ALLOWED = set()"),
    ("cache_tmdb", "180 d is a compliance boundary for TMDB", "lib/cache.py", 171, "TTL_DAYS = 180"),
]
