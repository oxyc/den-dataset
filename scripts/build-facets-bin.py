#!/usr/bin/env python3
"""Build `facets.bin` (DFI2) — the on-device attribute index: country / language / year / voteCount.

Nothing in this repo produced this blob until now; it was generated once, elsewhere, and carried forward by
`finalize`'s unowned-key merge. That is why it silently fell 999 titles behind the corpus: nothing rebuilt it
when the corpus grew. Missing rows are not a soft failure — atlas orders browse rows by votes, so a title with
no row sorts by tmdbId and *La Job* (tv:5) lands next to Game of Thrones.

Format, read off the parser in DenKit `FacetSearch.swift` rather than from any doc:

    "DFI2" | u32 count | count x 15-byte records, little-endian:
      +0  i32  tmdbId
      +4  u8   mediaType   (1 = tv, else movie)
      +5  2    language    ascii, NUL-padded
      +7  2    country     ascii, NUL-padded
      +9  u16  year
      +11 u32  voteCount

A short or bad blob makes `FacetIndex(blob:)` return nil and facet search silently disappears, so this
verifies its own output by re-parsing it before writing.

    scripts/build-facets-bin.py <labels.json> <enriched-dir> <out.bin>
"""
import json
import os
import struct
import sys


def load_enriched(enriched_dir):
    """key -> (language, country, year, voteCount). These are TMDB facts (not expression): the ISO codes, the
    year and a vote count. Wikidata supplies all but the vote count if this ever needs to be TMDB-free."""
    out = {}
    # BATCH-NUMBER order, oldest first, LAST occurrence winning — matching `finalize`'s own de-dup and
    # `EnrichedBatches.orderedNames`. This was `sorted()` with first-wins, which is lexicographic:
    # `batch-99.json` sorts after `batch-177.json`, so the winner depended on how many digits a batch id
    # happened to have. 1,855 of 59,218 keys appear in more than one batch and 505 disagree about
    # `hasWikiPlot`; for the facets tuple specifically the flip moves 97 keys, all voteCount, all upward.
    for name in sorted(
        (n for n in os.listdir(enriched_dir) if n.startswith("batch-") and n.endswith(".json")),
        key=lambda n: int(n[len("batch-"):-len(".json")]),
    ):
        for d in json.load(open(os.path.join(enriched_dir, name))):
            key = f"{d['mediaType']}:{d['tmdbId']}"
            countries = d.get("originCountry") or []
            out[key] = ((d.get("originalLanguage") or "")[:2],
                        (countries[0] if countries else "")[:2],
                        d.get("year") or 0,
                        d.get("voteCount") or 0)
    return out


def main():
    labels_path, enriched_dir, dest = sys.argv[1], sys.argv[2], sys.argv[3]
    labels = json.load(open(labels_path))
    rows = labels["records"] if isinstance(labels, dict) else labels
    facts = load_enriched(enriched_dir)

    packed = bytearray()
    n = missing = 0
    for r in rows:
        key = f"{r['mediaType']}:{r['tmdbId']}"
        lang, country, year, votes = facts.get(key, ("", "", 0, 0))
        if key not in facts:
            missing += 1
        # A row is written even with no facts: its ABSENCE is what makes a title sort by id, which is the bug
        # this rebuild exists to fix. Empty codes simply do not match a country/decade filter.
        packed += struct.pack("<iB", r["tmdbId"], 1 if r["mediaType"] == "tv" else 0)
        packed += lang.encode("ascii", "ignore")[:2].ljust(2, b"\0")
        packed += country.encode("ascii", "ignore")[:2].ljust(2, b"\0")
        packed += struct.pack("<HI", min(year, 65535), min(votes, 4294967295))
        n += 1

    blob = b"DFI2" + struct.pack("<I", n) + bytes(packed)

    # Re-parse before writing: a short blob makes FacetIndex return nil and facet search vanishes silently.
    assert blob[:4] == b"DFI2", "magic"
    count = struct.unpack_from("<I", blob, 4)[0]
    assert count == n and len(blob) == 8 + count * 15, f"length {len(blob)} != 8 + {count}*15"
    first = struct.unpack_from("<i", blob, 8)[0]
    assert first == rows[0]["tmdbId"], f"first row {first} != {rows[0]['tmdbId']}"

    with open(dest, "wb") as f:
        f.write(blob)
    print(json.dumps({"rows": n, "noFacts": missing, "bytes": len(blob), "path": dest}))


if __name__ == "__main__":
    main()
