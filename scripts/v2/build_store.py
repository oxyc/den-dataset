#!/usr/bin/env python3
"""Build `den-<version>.store` — the single artifact den-atlas loads.

  scripts/v2/build_store.py --corpus out-repass/corpus-<ver>.jsonl.gz \
      --entities out-repass/corpus-<ver>-entities.json.gz \
      --facets out-repass/facets.bin \
      --vectors out-repass/vectors-bge-m3.bin --vector-labels out-repass/labels-t02.json \
      --premise-vectors out-repass/vectors-premise.bin --premise-labels out-repass/labels-premise.json \
      --dataset-version <ver> --out out-repass/den-<ver>.store

The layout is den-spec `wire/store-v2.md`. That document is the contract; this is one of its two
implementations, and `den-atlas/src/store.rs` is the other. Change one and you change all three.

This file is the command line and the publication policy. The FORMAT is the `store/` package, one
module per section group in `wire/store-v2.md`, so the document's own headings say which file to open;
`store/build.py` is the order they run in.

## The rule the store exists to enforce

Every section is sourced BY KEY from its own artifact and its count asserted against that artifact. The
recurring failure in this pipeline is a join that misses and returns something anyway: eleven titles absent
from a derived blob for a day; 89 facts-only titles dropped by iterating the pass instead of joining;
`labels` null on all 47,529 rows because a lookup fell through to the wrapper dict. Each was silent, and
each passed the guards in force at the time. A count that does not match its source is fatal here.

`--stamp-meta` also records WHAT IT READ — every input's path, sha256, size and mtime, as `storeInputs`
in the manifest. See `store/inputs.py`: the inputs stopped being published artifacts, so they stopped
being covered by the ownership guard, and this record is what `check-producers.py` holds them to.

## What it publishes, which is not everything it reads

Plot facets pass the FACETS-V2 publication gates before they reach a section — see `store/facets.py`.
The corpus collects every answer with its full distribution; this file decides which of them the one
published artifact asserts. `--stamp-meta` records what each gate withheld, as `facetGates`.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from store import build  # noqa: E402
from store.cards import display_title, release_year  # noqa: E402  — re-exported; see below
from store.facets import FACET_AXES, publishable, row_applicability  # noqa: E402
from store.facts import title_imdb_id  # noqa: E402
from store.inputs import (INPUT_ARGS, build_inputs, input_digest,  # noqa: E402
                          labels_by_key)

# `display_title`, `release_year`, `title_imdb_id`, `publishable`, `row_applicability`, `FACET_AXES`,
# `INPUT_ARGS`, `input_digest` and `labels_by_key` are imported so that this file's module namespace is
# the whole surface the pipeline addresses the writer through: `scripts/check-producers.py` and
# `scripts/v2/migrate_vector_blob.py` load it by path, and `test_build_store.py` exercises the helpers
# against it. Each one's implementation is in the `store/` module named for its section group.

# ---- where every section's bytes come from ---------------------------------------------------------
#
# The store is a PUBLIC release asset on a public repo, so publishing it is redistribution — see
# LICENSES.md and oxyc/den#118. The count asserts in the writer hold each section against its source
# artifact; until this table nothing held the SET of sections against anything, so a column carrying
# vendor content could be added and would ship. "Someone will notice the new column" is the guard that
# failed.
#
# The table and the two guards that read it live HERE rather than in `store/`, because they refuse on
# names resolved through this module: `test_build_store.py` reaches a guard by loading this file and
# replacing `PROVENANCE` or `VENDOR_ALLOWED` on it, which is the only way to make a guard that fires on
# the writer's own constants fail on demand. Behind an import they would still run and could no longer
# be shown to.

#: The sources a section may declare.
#:
#:   wikidata   — a fact scraped from Wikidata (CC0)
#:   wikipedia  — derived from the English Wikipedia article text (CC BY-SA; see LICENSES.md)
#:   llm        — a model's answer over that text
#:   ours       — this pipeline's own bookkeeping: offsets, indices, counts, controlled vocabularies
#:   identifier — an id rather than content: the TMDB key, the IMDb id, a TMDB person id
#:   tmdb       — TMDB CONTENT, which is what #118 is removing
#:   imdb       — IMDb's dataset CONTENT: vote counts, ratings. Nothing here reads them, and IMDb's
#:                licence rules out a public database, so they may never ship. Not the `imdb` SECTION,
#:                which is the id (Wikidata's P345) — an `identifier`.
SOURCES = {"wikidata", "wikipedia", "llm", "ours", "identifier", "tmdb", "imdb"}

#: Every section, and its source. Asserted at assembly as `set(PROVENANCE) == set(sec.order)`, which
#: fails in BOTH directions: a new section with no entry here is fatal, and so is an entry for a section
#: that is no longer written. Listed in the order the writer emits them.
PROVENANCE = {
    "keys": "identifier",
    # The dictionary is a container, not a source: each string's provenance is the section that
    # references it. `prose_in_the_dictionary` is what bounds what can arrive here.
    "strings": "ours",
    "str_off": "ours",
    "card_title": "wikidata",
    "card_year": "wikidata",
    "primary_genre": "llm",
    "subgenre_v": "llm", "subgenre_c": "llm", "subgenre_o": "llm",
    "mood_v": "llm", "mood_c": "llm", "mood_o": "llm",
    "animated": "llm",
    "facet_v": "llm", "facet_c": "llm",
    "score_intensity": "llm", "score_humour": "llm",
    "score_weight": "llm", "score_complexity": "llm",
    "world": "llm",
    "noul_k_v": "llm", "noul_k_o": "llm",
    "noul_v_v": "llm", "noul_v_o": "llm",
    # The Noul/critique/technique/depicts/audience VOCABULARIES are our question taxonomy; the scores
    # against them are the model's.
    "noul_names": "ours",
    "critique": "llm", "critique_names": "ours",
    "technique": "llm", "technique_names": "ours",
    "depicts": "llm", "depicts_names": "ours",
    "audience": "llm", "audience_names": "ours",
    "makers_v": "wikidata", "makers_o": "wikidata",
    "directors_v": "wikidata", "directors_o": "wikidata",
    "creators_v": "wikidata", "creators_o": "wikidata",
    "writers_v": "wikidata", "writers_o": "wikidata",
    "cast_v": "wikidata", "cast_o": "wikidata",
    "broadcasters_v": "wikidata", "broadcasters_o": "wikidata",
    "composers_v": "wikidata", "composers_o": "wikidata",
    "dops_v": "wikidata", "dops_o": "wikidata",
    "distributors_v": "wikidata", "distributors_o": "wikidata",
    "companies_v": "wikidata", "companies_o": "wikidata",
    "locations_v": "wikidata", "locations_o": "wikidata",
    "subjects_v": "wikidata", "subjects_o": "wikidata",
    "instance_of_v": "wikidata", "instance_of_o": "wikidata",
    "based_on_v": "wikidata", "based_on_o": "wikidata",
    "based_kind_v": "wikidata", "based_kind_o": "wikidata",
    # Wikidata genre Q-ids mapped into TMDB's genre ID SPACE. The values are CC0; the vocabulary the ids
    # index is TMDB's, which is a numbering, not content.
    "genres_v": "wikidata", "genres_o": "wikidata",
    "countries_v": "wikidata", "countries_o": "wikidata",
    "languages_v": "wikidata", "languages_o": "wikidata",
    "alias_titles_v": "wikidata", "alias_titles_o": "wikidata",
    "imdb": "identifier",
    "released": "wikidata", "released_prec": "wikidata",
    "ended": "wikidata", "ended_prec": "wikidata",
    "episodes": "wikidata", "seasons": "wikidata",
    # A scrape-time claim about whether a vector was expected — this pipeline's bookkeeping, not a fact
    # about the work.
    "facts_has_vec": "ours",
    "applic_v": "llm", "applic_c": "llm",
    "runtime": "wikidata",
    "franchise_v": "wikidata", "franchise_o": "wikidata",
    "orig_lang": "wikidata",
    "ent_qid": "wikidata", "ent_name": "wikidata",
    "ent_tmdb": "identifier",
    "ent_credits": "ours",
    "ent_alias_v": "wikidata", "ent_alias_o": "wikidata",
    # IMDb's person id as Wikidata states it (P345): an id, not IMDb's dataset content.
    "ent_imdb": "identifier",
    # A person's P21, P569, P570, P27 and P106, as Wikidata states them (oxyc/den#136).
    "ent_gender_v": "wikidata", "ent_gender_o": "wikidata",
    "ent_citizen_v": "wikidata", "ent_citizen_o": "wikidata",
    "ent_occupation_v": "wikidata", "ent_occupation_o": "wikidata",
    "ent_born": "wikidata", "ent_born_prec": "wikidata",
    "ent_died": "wikidata", "ent_died_prec": "wikidata",
    "maker_ent": "wikidata",
    "maker_rows_v": "ours", "maker_rows_o": "ours",
    # `data/iconic-studios.json`: which studios, their names and their items are a judgement kept by hand.
    "studio_qid": "ours", "studio_name": "ours",
    "studio_ent_v": "ours", "studio_ent_o": "ours",
    # P166/P1411, each award filed under its ceremony by Wikidata's own P361/P31/P1027.
    "ceremony_qid": "wikidata", "ceremony_name": "wikidata",
    "award_v": "wikidata", "award_w": "wikidata", "award_o": "wikidata",
    # Embeddings of the article's plot text, and of the premise tags a model wrote from it.
    "vec_plot": "wikipedia",
    "vec_premise": "llm",
    "vec_premise_has": "ours", "vec_plot_has": "ours",
}

#: The sources that are a vendor's CONTENT. `identifier` is deliberately not one: an id is a join key,
#: which is the one thing both catalogue licences leave us.
VENDOR_SOURCES = {"tmdb", "imdb"}

#: The vendor-sourced sections the store may still carry. `card_title` and `card_year` have moved to
#: Wikidata and `votes` is gone, so this is EMPTY: the store carries identifiers and free facts, nothing
#: a vendor licenses. It stays as a set rather than being deleted, because the check it feeds is what
#: refuses the next column that tries.
#:
#: Removing a column is two edits — its PROVENANCE entry and its entry here — because an allowlist entry
#: for a section that is no longer written is fatal too.
VENDOR_ALLOWED = set()

#: The longest string the dictionary may hold, in bytes. Measured on the shipped store: 445,817 strings,
#: longest 217 (a performer's full name), only 34 over 120 — a Peter Greenaway title at 190 is the next.
#: 260 leaves 43 bytes of headroom and fails closed against a prose column: enriched overviews run to a
#: median of 3,321 characters, so one could not reach the dictionary without tripping this.
#:
#: Its honest limit, and the reason this is a BACKSTOP rather than the guard: a ~40-character tagline
#: would pass it unnoticed. `check_provenance` is the guard; this catches the case where prose arrives
#: through a section that already exists.
MAX_STRING_BYTES = 260


def check_provenance(names):
    """Refuse a store whose sections are not exactly the ones `PROVENANCE` declares.

    Three ways to fail, all fatal:

    * a section nothing declares — the case this exists for. A new column reaches a public release asset
      by being added to the writer and nothing else, so the only way to stop one carrying licensed
      content is to make an undeclared section impossible to build.
    * a declared section that is no longer written — a stale entry, which would let a later removal look
      like it had been reviewed when the table describes a store that does not exist.
    * a vendor-sourced section outside `VENDOR_ALLOWED`, in either direction.

    `names` is `sec.order`, so what is checked is what is about to be written rather than what this
    file appears to write.
    """
    written, declared = set(names), set(PROVENANCE)
    if written != declared:
        undeclared, stale = sorted(written - declared), sorted(declared - written)
        sys.exit(f"provenance: {len(undeclared)} section(s) declare no source ({undeclared}) and "
                 f"{len(stale)} declared section(s) are not written ({stale}). Every section says in "
                 f"PROVENANCE where its bytes come from; the table is compared as a SET, so neither "
                 f"adding a column nor removing one can skip it.")
    unknown = sorted(set(PROVENANCE.values()) - SOURCES)
    if unknown:
        sys.exit(f"provenance: {unknown} is not one of {sorted(SOURCES)}")
    vendor = {name for name, source in PROVENANCE.items() if source in VENDOR_SOURCES}
    if vendor - VENDOR_ALLOWED:
        sys.exit(f"provenance: {sorted(vendor - VENDOR_ALLOWED)} carries vendor content and is not in "
                 f"VENDOR_ALLOWED. The store is a public release asset, so shipping it redistributes "
                 f"that content — see LICENSES.md and oxyc/den#118. Source it from Wikidata or "
                 f"Wikipedia, or add it to the allowlist and say in #118 why it is there.")
    if VENDOR_ALLOWED - vendor:
        sys.exit(f"provenance: VENDOR_ALLOWED still permits {sorted(VENDOR_ALLOWED - vendor)}, which no "
                 f"section declares as vendor-sourced. Drop it from the allowlist — a permission for a "
                 f"column that no longer exists makes the remaining list read as longer than it is.")


def prose_in_the_dictionary(ordered):
    """The complaint when the string dictionary holds something too long to be a name, else `None`.

    Prose can only enter the store through `strings`: every other section is a number, an id, or an
    offset into this one. A bound on the longest entry is therefore a bound on the whole artifact, and
    it is the check that would still fire if a column carrying overviews were declared in `PROVENANCE`
    under an honest-looking source.
    """
    longest = max(ordered, key=lambda s: len(s.encode("utf-8")), default="")
    size = len(longest.encode("utf-8"))
    if size > MAX_STRING_BYTES:
        return (f"strings: the longest entry is {size} bytes, over the {MAX_STRING_BYTES}-byte bound — "
                f"{longest[:80]!r}…. This dictionary holds names, titles and controlled vocabulary; "
                f"something that long is prose, and the store may not publish it. If a real name is "
                f"genuinely this long, raise MAX_STRING_BYTES and say which one.")
    return None


def build_parser():
    """The argument list, so a test can hold it against `INPUT_ARGS` — an input the parser accepts and
    the record does not know about is an input nothing checks."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--entities", required=True)
    ap.add_argument("--facts", required=True,
                    help="facts-<ver>.json — for genreMap, and to assert the row count")
    ap.add_argument("--vectors", required=True, help="vectors-bge-m3.bin (DENVEC02: it names its own rows)")
    ap.add_argument("--vector-labels", required=True,
                    help="labels-t02.json — the PLOT vectors' key set, which finalize writes with each "
                         "title's genres & moods from genres-moods.json. No longer the vectors' row "
                         "order: the blob carries its own keys, and this is what that key column is "
                         "checked against. Also what the label sections are counted against: the corpus "
                         "`labels` field, joined from genres-moods.json, must cover exactly these titles.")
    ap.add_argument("--premise-vectors", help="vectors-premise.bin (DENVEC02)")
    ap.add_argument("--premise-labels", required=True,
                    help="labels-premise.json — the PREMISE vectors' key set, checked the same way. Its "
                         "genres & moods are a copy of the plot labels and are not read.")
    ap.add_argument("--dataset-version", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stamp-meta",
                    help="dataset.meta.json to declare the store in (storeFile/Sha256/Bytes). Without "
                         "this the store is written and nothing names it, so publish-dataset.sh "
                         "announces it as an unowned blob and den-atlas never loads it.")
    return ap


def main():
    args = build_parser().parse_args()

    # BEFORE the build reads them, so the record describes the bytes this run was handed. Only when the
    # record has somewhere to go: without `--stamp-meta` nothing would carry it, and hashing 400 MB of
    # inputs to throw the answer away would slow every fixture build for nothing.
    inputs = build_inputs(args) if args.stamp_meta else None

    build.run(args, inputs, prose_in_the_dictionary, check_provenance)


if __name__ == "__main__":
    main()
