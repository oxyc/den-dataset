"""The order the section groups run in. This module implements no section itself.

Three passes over the corpus, and they cannot be merged: the string dictionary has to be complete
before any id is handed out, and a dense table's width is the size of a vocabulary only the whole
corpus knows.

  1. intern  — every string any section will reference, into one dictionary
  2. freeze  — sort it, assign ids, and build the entity index the fact columns point at
  3. add     — one pass writing every group's columns, then the puts, in section-table order

The count asserts at the end are the rule this writer exists for: every section is sourced BY KEY from
its own artifact and its count held against that artifact. See `scripts/v2/build_store.py`'s docstring
for the joins that bought each one.
"""
import hashlib
import json
import os
import sys

from . import (aliases, awards, cards, entities, facets, facts, format, identity, labels, makers, scores,
               studios, vectors)
from .inputs import corpus_rows, labels_by_key, read_json


def run(args, inputs, prose_check, provenance_check):
    """Build the store named by `args.out`.

    `prose_check` and `provenance_check` are the publication guards, passed in rather than imported:
    they refuse on tables that belong to the command line, and a test reaches a guard by replacing one
    of those tables on that module.
    """
    print("reading the corpus …", file=sys.stderr)
    rows = {r["key"]: r for r in corpus_rows(args.corpus)}
    keys = identity.sorted_keys(rows)
    n = len(keys)
    print(f"  {n} titles", file=sys.stderr)

    # Before anything reads `facts.titles`: a dropped alias must reach neither `alias_titles` nor the
    # card's name fallback. See `store/aliases.py`.
    alias_record = aliases.apply(rows.values())
    print(f"  aliases: {alias_record['dropped']} dropped by data/alias-decisions.json, "
          f"{alias_record['undecided']} naming another title with no decision", file=sys.stderr)
    # Titles several Wikidata items claim with nothing chosen between them: the facts stage wrote them
    # with no Wikidata fields rather than mixing two works, so they have no card.
    # `check-wikidata-items.py --gate` refuses the publish while any is listed.
    item_record = {"ambiguous": sorted(key for key, row in rows.items()
                                       if (row.get("facts") or {}).get("wikidataCandidates")
                                       and not (row.get("facts") or {}).get("wikidataItem"))}
    print(f"  wikidata items: {len(item_record['ambiguous'])} titles with no decided item", file=sys.stderr)

    entity_table = read_json(args.entities)
    genre_map, facts_records = facts.read_genre_map(read_json(args.facts), args.facts)

    # The card is built from `facts` — Wikidata — and the TMDB metadata sidecar is no longer read at all.
    #
    # An earlier attempt at this left `card_year` at its sentinel on all 47,618 rows and nobody saw it,
    # which is why the keys read are the MEASURED ones rather than the plausible ones: `facts.titles.en`
    # is populated for 99.98% of the corpus and `facts.released`/`facts.started` for 99.59%, whereas
    # `facts.title` and `facts.year` — the names that failure reached for — do not exist. A column of
    # sentinels looks perfect from the outside, so the guard below counts names against the corpus.
    namable = sum(1 for r in rows.values() if (r.get("facts") or {}).get("titles"))

    # Which titles each vector blob must cover, from the two labels files — not the vector row order, the
    # blobs carry their own keys, but the independent record each key column is checked against. The plot
    # one is also the count the label sections are asserted against below: it is the titles with a plot
    # vector, each with its genres & moods, and a complete run embeds every title that has genres & moods.
    # Counting the corpus field would be asserting the corpus against itself.
    print("reading the label artifacts …", file=sys.stderr)
    labels_source = labels_by_key(args.vector_labels, "vector labels")
    premise_labels_source = {}
    if args.premise_labels:
        premise_labels_source = labels_by_key(args.premise_labels, "premise labels")

    # ONE dictionary. Splitting a controlled vocabulary out to keep u16 ids saved 1.84 MB of 123 MB
    # and bought two bare integer id spaces with nothing in the format telling them apart: a reader
    # resolving a vocabulary id against the free-text table gets a wrong but perfectly valid string,
    # silently. That is the failure this whole store exists to make impossible. u32 everywhere.
    strings = format.Strings()
    card_columns = cards.Cards()
    label_columns = labels.Labels()
    facet_columns = facets.Facets()
    score_columns = scores.Scores()
    fact_columns = facts.Facts(genre_map)

    for key in keys:
        r = rows[key]
        row_facts = r.get("facts") or {}
        titles = row_facts.get("titles") or {}
        label_columns.intern(strings, r)
        facet_columns.intern(strings, r)
        score_columns.intern(r)
        card_columns.intern(strings, titles)
        fact_columns.intern(strings, row_facts, titles)
    score_columns.freeze_vocabularies(strings)
    entities.intern(strings, entity_table)

    entity_index = entities.Entities(entity_table, rows.values())
    entity_index.intern_unnamed(strings)

    studio_list = studios.Studios(studios.load())
    studio_list.resolve(rows.values(), entity_index)
    studio_list.intern(strings)
    print(f"  iconic studios: {len(studio_list.kept)} of {len(studio_list.curated)} in "
          f"data/iconic-studios.json are credited by the corpus", file=sys.stderr)

    award_columns = awards.Awards(entity_table, *awards.load_merges())
    award_columns.resolve(keys, rows)
    award_columns.intern(strings)
    print(f"  awards: {award_columns.titles} titles at {len(award_columns.ceremonies)} ceremonies; "
          f"{award_columns.record['applied']} merges in data/award-ceremony-merges.json applied, "
          f"{len(award_columns.record['stale'])} name a ceremony no title does", file=sys.stderr)

    ordered_strings = strings.freeze()
    prose_complaint = prose_check(ordered_strings)
    if prose_complaint:
        sys.exit(prose_complaint)
    print(f"  {len(ordered_strings)} strings", file=sys.stderr)

    sec = format.Sections(n)
    identity.put(sec, keys, ordered_strings, n)

    for key in keys:
        r = rows[key]
        row_facts = r.get("facts") or {}
        titles = row_facts.get("titles") or {}
        card_columns.add(strings, row_facts, titles)
        label_columns.add(strings, key, r)
        facet_columns.add(strings, key, r)
        score_columns.add(key, r)
        fact_columns.add(strings, key, row_facts, titles, entity_index)

    card_columns.put(sec, n)
    label_columns.put(sec, n)
    facet_columns.put(sec, n)
    score_columns.put(sec, n, strings)
    fact_columns.put(sec, n)
    # The applicability columns land HERE, between `facts_has_vec` and `runtime`. A section's position in
    # the table is its position in the file, so where a group's sections are emitted is part of the
    # format rather than a matter of taste.
    facet_columns.put_applicability(sec, n)
    fact_columns.put_trailing(sec, n)

    entity_index.put(sec, strings, fact_columns.makers, fact_columns.entity_lists["cast"])
    makers.put(sec, fact_columns.makers)
    studio_list.put(sec, strings)
    award_columns.put(sec, strings)

    plot_hits, plot_rows, premise_hits, premise_rows = vectors.put(
        sec, keys, args, labels_source, premise_labels_source)

    # ---- the asserts that make a silent miss impossible -------------------------------------------
    # Compared against the artifacts themselves. A 50% threshold would have passed a join that lost
    # 23,000 rows, and "more than half worked" is not a standard anything here should meet.
    for what, got, want, source in (
        ("rows", n, facts_records, args.facts),
        # Every title with a plot vector carries genres & moods in the store, and no other title does.
        ("labelled titles", label_columns.with_labels, len(labels_source), args.vector_labels),
        # Names against the corpus, not against a sidecar: the count is taken from `rows`
        # before the writing loop, so it proves the naming happened rather than agreeing with
        # itself. A title whose `facts.titles` holds only blanks fails here.
        ("named titles", card_columns.named, namable, args.corpus),
        # Against the BLOB's own key column now, not a sidecar's record count: every vector the file
        # carries must have landed on a row of the store.
        ("plot vectors", plot_hits, plot_rows, args.vectors),
        ("premise vectors", premise_hits, premise_rows, args.premise_vectors or args.premise_labels),
    ):
        if got != want:
            sys.exit(f"{what}: {got} in the store, {want} in {source} — they must agree exactly")
    # Unresolved references, named and counted. A reference the entity table cannot resolve is dropped —
    # that is unavoidable when the table is short — but dropping it WITHOUT SAYING SO is how 2,680
    # franchise links disappeared into a section that looked perfectly well formed.
    unresolved = entity_index.unresolved
    if unresolved:
        print("unresolved entity references (dropped):", file=sys.stderr)
        for what, count in sorted(unresolved.items(), key=lambda kv: -kv[1]):
            print(f"  {what:14} {count}", file=sys.stderr)

    facet_columns.announce(n)
    gate_report = facet_columns.report()

    print(json.dumps({"titles": n, "withLabels": label_columns.with_labels,
                      "withNames": card_columns.named, "unresolved": dict(sorted(unresolved.items())),
                      "plotVectors": plot_hits, "premiseVectors": premise_hits,
                      "entities": len(entity_index.qids), "iconicStudios": len(studio_list.kept),
                      "awardTitles": award_columns.titles, "ceremonies": len(award_columns.ceremonies),
                      "strings": len(ordered_strings),
                      "sections": len(sec.order)}, indent=1), file=sys.stderr)

    # ---- assemble --------------------------------------------------------------------------------
    # What is about to be written must be exactly what declares a source. See `check_provenance`.
    provenance_check(sec.order)
    payload_bytes = format.write(args.out, sec, n, args.dataset_version)

    # Declare it in the manifest, here, from the bytes just written.
    #
    # NOT a field on Swift's `DatasetMeta`: every key that struct declares is owned, and `ManifestMerge`
    # drops an owned key the new manifest omits, so a key `finalize` does not compute would be erased by
    # the next `finalize`. `ManifestMerge` carries unowned keys forward instead, which is why `maxBatchId`
    # is stamped from a script too.
    #
    # No `storeGzFile`, ever: atlas MMAPS this file and a compressed one cannot be mapped.
    if args.stamp_meta:
        blob = open(args.out, "rb").read()
        meta = read_json(args.stamp_meta)
        meta["storeFile"] = os.path.basename(args.out)
        meta["storeSha256"] = hashlib.sha256(blob).hexdigest()
        meta["storeBytes"] = len(blob)
        # WHAT IT WAS BUILT FROM, in the manifest rather than in the store or a sidecar.
        #
        # In the store would change den-spec `wire/store-v1.md`, the committed fixture and both readers —
        # three repos, for a fact no reader of the store wants. In a sidecar it would be an undeclared
        # file in the out-dir, which is the exact class of artifact every guard here exists to refuse.
        # The manifest already travels with the store, is already stamped from here, and already survives
        # `prune-manifest.py` (`storeInputs` is not shaped like a per-blob claim).
        #
        # It is NOT a blob claim and must never become one: no `storeInputsFile`/`Sha256`/`Bytes`, or the
        # prune's keep-list would drop it and the publisher would stop seeing it.
        meta["storeInputs"] = inputs
        # And what the publication gates withheld. Same reasoning as `storeInputs`: it describes the
        # dataset rather than a file, so `prune-manifest.py` carries it forward, and it must never be
        # given a `File`/`Sha256`/`Bytes` name or the prune's keep-list would drop it. Without this the
        # only record of a 42% `ending` drop is a build log nobody kept.
        meta["facetGates"] = gate_report
        # Which alias decisions this store applied, and how many colliding aliases it ships undecided.
        # `check-alias-collisions.py --gate` refuses the publish unless the hash is the committed file's
        # and the count is zero. Same shape rule as the two above: no `File`/`Sha256`/`Bytes` name.
        meta["aliasDecisions"] = alias_record
        # Same shape rule; `check-wikidata-items.py --gate` refuses while `ambiguous` names a title.
        meta["wikidataItems"] = item_record
        # Same shape rule; `check-award-merges.py --gate` refuses while a merge is stale or the list
        # applied is not the committed one.
        meta["awardMerges"] = award_columns.record
        with open(args.stamp_meta, "w") as fh:
            json.dump(meta, fh, indent=1)
            fh.write("\n")

    print(json.dumps({"out": args.out, "bytes": format.HEADER_BYTES + payload_bytes,
                      "formatVersion": format.FORMAT_VERSION, "stamped": bool(args.stamp_meta),
                      "inputs": len(inputs or ())}, indent=1))
