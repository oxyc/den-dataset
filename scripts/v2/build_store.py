#!/usr/bin/env python3
"""Build `den-<version>.store` — the single artifact den-atlas loads.

  scripts/v2/build_store.py --corpus out-repass/corpus-<ver>.jsonl.gz \
      --entities out-repass/corpus-<ver>-entities.json.gz \
      --facets out-repass/facets.bin \
      --vectors out-repass/vectors-bge-m3.bin --vector-labels out-repass/labels-t02.json \
      --premise-vectors out-repass/vectors-premise.bin --premise-labels out-repass/labels-premise.json \
      --dataset-version <ver> --out out-repass/den-<ver>.store

The layout is den-spec `wire/store-v1.md`. That document is the contract; this is one of its two
implementations, and `den-atlas/src/store.rs` is the other. Change one and you change all three.

## The rule this file exists to enforce

Every section is sourced BY KEY from its own artifact and its count asserted against that artifact. The
recurring failure in this pipeline is a join that misses and returns something anyway: eleven titles absent
from a derived blob for a day; 89 facts-only titles dropped by iterating the pass instead of joining;
`labels` null on all 47,529 rows because a lookup fell through to the wrapper dict. Each was silent, and
each passed the guards in force at the time. A count that does not match its source is fatal here.
"""
import argparse
import gzip
import hashlib
import json
import os
import struct
import sys
from datetime import date
from collections import defaultdict

FORMAT_VERSION = 1
MAGIC = b"DENSTOR1"
ENDIAN_CHECK = 0x01020304
HEADER_BYTES = 64
ENTRY_BYTES = 32
ALIGN = 8
DIMS = 1024

FACET_AXES = ("era", "setting", "scope", "ending", "pacing", "chronology",
              "continuity", "conflict", "ensemble", "tone", "timespan", "archetype")
SCORE_AXES = ("intensity", "humour", "emotional_weight", "complexity")
SCORE_SECTION = {"intensity": "score_intensity", "humour": "score_humour",
                 "emotional_weight": "score_weight", "complexity": "score_complexity"}
# Every Wikidata list field, kept whole. The store deliberately does NOT choose which of these a reader
# will want: facts-slim froze the field set its reader parsed at the time, and `screenwriters`,
# `composers` and `cinematographers` then sat in the file unread for months — the last two being exactly
# the "who made it feel like this" credits a people-led row needs. We paid to scrape them; they ship.
# corpus field -> section base name. Section names cap at 16 bytes, so the long ones are abbreviated
# HERE, once, rather than silently truncated at write time.
ENTITY_LISTS = {
    "cast": "cast",
    "broadcaster": "broadcasters",
    "composers": "composers",
    "cinematographers": "dops",
    "distributors": "distributors",
    "productionCompanies": "companies",
    "narrativeLocations": "locations",
    "mainSubjects": "subjects",
    "instanceOf": "instance_of",
    "basedOn": "based_on",
}
# The two applicability questions, and the audience Nouls from the delta pass.
APPLICABILITY = ("validity", "narrative_applicability")
# `world` is the max of these. The definition came from the rail-facets producer, which this file
# replaced and which is deleted — so this list is now the only place it is written down.
FANTASTICAL = [f"theme__{k}" for k in (
    "vampire", "werewolf_monster", "zombie", "superhero", "time_travel", "cyberpunk",
    "dystopian_post_apocalyptic", "folk_horror")] + [f"subgenre__{k}" for k in (
    "supernatural_horror", "sci_fi_horror", "sci_fi_action", "fantasy_adventure")]

U32_NONE = 0xFFFFFFFF
I16_NONE = -0x8000
I32_NONE = -0x80000000
SCORE_NONE = 0xFFFF   # distinct from a genuine 0.00
EPOCH = date(1970, 1, 1)
PRECISION = {"day": 0, "month": 1, "year": 2, "decade": 3, "century": 4}
PRECISION_NONE = 0xFF


class Strings:
    """The string dictionary. Ids are assigned AFTER sorting, so the output is byte-identical across runs."""

    def __init__(self):
        self._seen = set()
        self._ids = None

    def add(self, value):
        if value is not None:
            self._seen.add(value)

    def freeze(self):
        ordered = sorted(self._seen)
        self._ids = {s: i for i, s in enumerate(ordered)}
        return ordered

    def id(self, value, default=U32_NONE):
        if value is None:
            return default
        return self._ids[value]


def hundredths(value, what, key):
    """A probability as u8 hundredths, refusing anything the format cannot hold exactly."""
    if value is None:
        return 0
    f = float(value)
    if f != f or f < 0.0 or f > 1.0:
        sys.exit(f"{key}: {what} is {value} — not a probability")
    scaled = round(f * 100)
    if abs(f * 100 - scaled) > 1e-9:
        sys.exit(f"{key}: {what} is {value}, which has more than two decimals — the store stores "
                 f"hundredths, and rounding it here would be a silent precision loss. Fix the producer.")
    return scaled


def score_hundredths(value, what, key):
    """A 0..4 score as u16 hundredths.

    This was u8 twentieths, reasoning that hundredths overflow a u8 at 2.56. True, and the wrong
    conclusion: the corpus scores ARE hundredths — 401 distinct values, 0.00 to 4.00 in steps of 0.01 — so
    twentieths silently rounded 94,498 of 190,116 values, 49.7% of them, Game of Thrones losing all four
    axes. The answer to "does not fit in u8" is a wider integer, not a coarser unit. u16 costs 381 KB of a
    124 MB file.

    It also refuses more than two decimals, which the twentieths version did not — and that omission is
    precisely why the loss shipped: the same check existed six lines away and was not applied here.
    """
    if value is None:
        return SCORE_NONE
    f = float(value)
    if f != f or f < 0.0 or f > 4.0:
        sys.exit(f"{key}: {what} is {value} — outside the 0..4 score range")
    scaled = round(f * 100)
    if abs(f * 100 - scaled) > 1e-9:
        sys.exit(f"{key}: {what} is {value}, which has more than two decimals — storing it would round "
                 f"silently. Fix the producer, or widen the field deliberately.")
    return scaled


def read_votes(enriched_dir):
    """`key -> voteCount`, from the enriched batches.

    The same source and the same rule as `build-facets-bin.py`: BATCH-NUMBER order, last occurrence
    winning, matching `finalize`'s de-dup and `EnrichedBatches.orderedNames`. `sorted()` on the names is
    lexicographic — `batch-99.json` after `batch-177.json` — so the winner would depend on how many digits
    a batch id happens to have, and 97 keys disagree about voteCount across batches.

    Votes are why this exists: atlas orders every browse row by them, so a title without one sorts by
    tmdbId and lands *La Job* (tv:5) next to Game of Thrones. `facets.bin` carried them and fell 9,007
    titles behind the corpus; read from here the store has them for every title it holds.
    """
    if not enriched_dir:
        return {}
    votes = {}
    names = [n for n in os.listdir(enriched_dir) if n.startswith("batch-") and n.endswith(".json")]
    for name in sorted(names, key=lambda n: int(n[len("batch-"):-len(".json")])):
        with open(os.path.join(enriched_dir, name), encoding="utf-8") as fh:
            for d in json.load(fh):
                votes[f"{d['mediaType']}:{d['tmdbId']}"] = int(d.get("voteCount") or 0)
    return votes


def days_since_epoch(value, key):
    """`{"date": "2007-01-20", "precision": "day"}` → (days, precision code).

    The corpus stores a dated fact as an object, not an int. Reading it with `isinstance(v, int)` left
    `released` at i32::MIN on all 47,618 rows — 46,765 of which have a date — and nothing noticed, because
    a column of sentinels is structurally perfect.
    """
    if not isinstance(value, dict):
        return I32_NONE, PRECISION_NONE
    text = value.get("date")
    if not isinstance(text, str) or not text:
        return I32_NONE, PRECISION_NONE
    code = PRECISION.get(value.get("precision"), PRECISION_NONE)
    try:
        parts = text.lstrip("+-").split("-")
        year = int(parts[0]) * (-1 if text.startswith("-") else 1)
        month = int(parts[1]) if len(parts) > 1 and parts[1] != "00" else 1
        day = int(parts[2]) if len(parts) > 2 and parts[2] != "00" else 1
        return (date(year, month, day) - EPOCH).days, code
    except (ValueError, IndexError, OverflowError):
        sys.exit(f"{key}: released date {text!r} is not a date this writer understands")


def labelled(entries, strings, what, key):
    """`[{"label": ..., "confidence": ...}]` → `[(string id, hundredths)]`, tolerating a bare string."""
    out = []
    for entry in entries or []:
        if isinstance(entry, dict):
            label, conf = entry.get("label"), entry.get("confidence")
        else:
            label, conf = entry, None
        if not label:
            continue
        out.append((strings.id(label), hundredths(conf, f"{what} confidence", key) if conf else 0))
    return out


def read_vectors(path, expected_rows, what):
    """The blob, and where row 0 starts.

    The base used to be inferred as `len(blob) - rows * DIMS`. That is the same arithmetic the file
    already answers: both `.bin` files carry an 8-byte header stating rows and dims. Inferring it meant a
    file whose length disagreed with the labels count produced a plausible non-zero base and shifted every
    single vector by a constant — undetectable downstream, because each row still contains real numbers.
    Read what the file says, and refuse it when it disagrees.
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    if len(blob) < 8:
        sys.exit(f"{path}: {len(blob)} bytes, too short to be a vector blob")
    rows, dims = struct.unpack("<II", blob[:8])
    if dims != DIMS:
        sys.exit(f"{path}: header says {dims} dims, this store stores {DIMS}")
    if rows != expected_rows:
        sys.exit(f"{path}: header says {rows} rows, but {what} lists {expected_rows}. The blob and its "
                 f"label order must be the same generation — they align positionally.")
    if len(blob) != 8 + rows * dims:
        sys.exit(f"{path}: {len(blob)} bytes for {rows} x {dims}, expected {8 + rows * dims}")
    return blob, 8


def corpus_rows(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def read_json(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def labels_by_key(path, label):
    """Rows keyed by `media:tmdbId`. Shapes are NAMED, never guessed — guessing is how `labels` came back
    as the wrapper dict and every lookup silently missed."""
    blob = read_json(path)
    rows = None
    if isinstance(blob, dict):
        for key in ("records", "labels", "tags"):
            if isinstance(blob.get(key), (list, dict)):
                rows = blob[key]
                break
        else:
            rows = blob if all(":" in k for k in list(blob)[:8]) else None
    else:
        rows = blob
    if rows is None:
        sys.exit(f"{label}: {path} has no recognisable rows (keys: {sorted(blob)[:6]})")
    if isinstance(rows, dict):
        return rows
    out = {f"{r['mediaType']}:{r['tmdbId']}": r for r in rows if isinstance(r, dict) and "tmdbId" in r}
    if not out:
        sys.exit(f"{label}: {path} produced no keyed rows")
    return out


class Sections:
    """Named byte blocks, written in insertion order, each 8-byte aligned."""

    def __init__(self, rows):
        self.rows = rows
        self.order = []
        self.blocks = {}
        self.widths = {}

    def put(self, name, fmt, values, width, expect=None):
        if expect is not None and len(values) != expect:
            sys.exit(f"section {name}: {len(values)} values, expected {expect}")
        if len(name.encode()) > 16:
            sys.exit(f"section name too long: {name}")
        self.blocks[name] = struct.pack(f"<{len(values)}{fmt}", *values)
        self.widths[name] = width
        self.order.append(name)

    def put_raw(self, name, blob, width, expect=None):
        """`expect` is the element count, not bytes — a dense R x K section must say what K is."""
        if expect is not None and len(blob) != expect * width:
            sys.exit(f"section {name}: {len(blob)} bytes, expected {expect} x {width}")
        if not blob and expect != 0:
            sys.exit(f"section {name} is empty — an empty section is well formed and says nothing, "
                     f"which is how genres shipped at zero bytes past every check")
        self.blocks[name] = bytes(blob)
        self.widths[name] = width
        self.order.append(name)

    def put_list(self, name, fmt, width, per_row, allow_empty=False, expect_rows=None):
        """A values array plus offsets of len(rows)+1 — an empty list is a zero-width span.

        The offsets array being the right length proves nothing about the values array: `genres_v` was
        0 bytes beside a perfectly correct 47,619-entry offsets array, and that combination is a valid,
        entirely empty section. A values array of zero is fatal unless the caller says it may be.

        `expect_rows` for a list that is not per TITLE — the entity table has its own length, and
        checking it against the title count would refuse a correct section.
        """
        rows = self.rows if expect_rows is None else expect_rows
        flat, offsets = [], [0]
        for row in per_row:
            flat.extend(row)
            offsets.append(len(flat))
        if len(offsets) != rows + 1:
            sys.exit(f"section {name}: {len(offsets)} offsets, expected {rows + 1}")
        if not flat and not allow_empty:
            sys.exit(f"section {name}_v holds no values for {rows} rows — the field is missing from "
                     f"the source or its type is not what this writer expects")
        self.put(f"{name}_v", fmt, flat, width)
        self.put(f"{name}_o", "I", offsets, 4)

    def put_labelled_list(self, name, per_row):
        """A label list that carries the model's confidence per entry: ids and confidences share one
        offsets array, so row i owns both spans. `subgenres` and `moods` are scored, not bare strings —
        flattening the confidence away would lose the only signal separating a 0.7 label from a 0.3 one."""
        ids, confs, offsets = [], [], [0]
        for row in per_row:
            for label_id, conf in row:
                ids.append(label_id)
                confs.append(conf)
            offsets.append(len(ids))
        if len(offsets) != self.rows + 1:
            sys.exit(f"section {name}: {len(offsets)} offsets, expected {self.rows + 1}")
        if not ids:
            sys.exit(f"section {name}_v holds no values for {self.rows} rows")
        self.put(f"{name}_v", "I", ids, 4)
        self.put(f"{name}_c", "B", confs, 1)
        self.put(f"{name}_o", "I", offsets, 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--entities", required=True)
    ap.add_argument("--facts", required=True,
                    help="facts-<ver>.json — for genreMap, and to assert the row count")
    ap.add_argument("--metadata", required=True,
                    help="metadata-<ver>.json — the cards: title, posterPath, year")
    ap.add_argument("--vectors", required=True, help="vectors-bge-m3.bin")
    ap.add_argument("--vector-labels", required=True, help="labels-t02.json — the row order the vectors align to")
    ap.add_argument("--premise-vectors")
    ap.add_argument("--premise-labels")
    ap.add_argument("--enriched",
                    help="the enriched/ batch directory, for TMDB vote counts. Without it the `votes` "
                         "section is all zeros and atlas cannot order a browse row by popularity.")
    ap.add_argument("--dataset-version", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stamp-meta",
                    help="dataset.meta.json to declare the store in (storeFile/Sha256/Bytes). Without "
                         "this the store is written and nothing names it, so publish-dataset.sh "
                         "announces it as an unowned blob and den-atlas never loads it.")
    args = ap.parse_args()

    print("reading the corpus …", file=sys.stderr)
    rows = {r["key"]: r for r in corpus_rows(args.corpus)}
    keys = sorted(rows, key=lambda k: ((0 if k.split(":", 1)[0] == "movie" else 1), int(k.split(":", 1)[1])))
    n = len(keys)
    print(f"  {n} titles", file=sys.stderr)

    vote_counts = read_votes(args.enriched)
    if args.enriched:
        print(f"  {len(vote_counts)} vote counts", file=sys.stderr)

    entities = read_json(args.entities)

    # genreMap turns Wikidata genre Q-ids into TMDB genre ids, per media type. Without it the `genres`
    # section shipped as ZERO bytes: `isinstance(g, int)` is false for every "Q842256", so all 99,216
    # values were dropped and the empty section passed every check.
    facts_blob = read_json(args.facts)
    genre_map = facts_blob.get("genreMap") or {}
    facts_records = len(facts_blob.get("records") or [])
    if not genre_map:
        sys.exit(f"{args.facts} has no genreMap — the genres section would ship empty")

    # Cards come from the metadata sidecar, which is where title/posterPath/year actually live. Reading
    # them off `facts` left card_year and card_poster at their sentinels on all 47,618 rows: the keys
    # simply do not exist there, and a column of sentinels looks perfect from the outside.
    cards = labels_by_key(args.metadata, "metadata")

    # Vector rows come from the labels file the .bin aligns to, positionally. Nothing else knows the order.
    print("reading vector row order …", file=sys.stderr)
    labels_source = labels_by_key(args.vector_labels, "vector labels")
    plot_order = list(labels_source)
    plot_row = {k: i for i, k in enumerate(plot_order)}
    premise_row = {}
    if args.premise_labels:
        premise_row = {k: i for i, k in enumerate(labels_by_key(args.premise_labels, "premise labels"))}

    # ONE dictionary. Splitting a controlled vocabulary out to keep u16 ids saved 1.84 MB of 123 MB
    # and bought two bare integer id spaces with nothing in the format telling them apart: a reader
    # resolving a vocabulary id against the free-text table gets a wrong but perfectly valid string,
    # silently. That is the failure this whole store exists to make impossible. u32 everywhere.
    strings = Strings()
    noul_names, critique_names, technique_names = set(), set(), set()
    depicts_names, audience_names = set(), set()
    for key in keys:
        r = rows[key]
        labels = r.get("labels") or {}
        strings.add(labels.get("primaryGenre"))
        for entry in (labels.get("subgenres") or []) + (labels.get("moods") or []):
            strings.add(entry.get("label") if isinstance(entry, dict) else entry)
        for axis in FACET_AXES:
            v = (r.get("facets") or {}).get(axis)
            if isinstance(v, dict) and v.get("choice") and v["choice"] != "does-not-apply":
                strings.add(v["choice"])
        noul_names.update((r.get("nouls") or {}).keys())
        critique_names.update((r.get("critique") or {}).keys())
        technique_names.update((r.get("technique") or {}).keys())
        depicts_names.update((r.get("depicts") or {}).keys())
        audience_names.update((r.get("audience") or {}).keys())
        for name in APPLICABILITY:
            choice = (r.get("applicability") or {}).get(name)
            if isinstance(choice, dict) and choice.get("choice"):
                strings.add(choice["choice"])
        facts = r.get("facts") or {}
        for kind in facts.get("basedOnKind") or []:
            strings.add(kind)
        t = facts.get("titles") or {}
        for name in (t.get("en"), t.get("orig")):
            strings.add(name)
        for alias in t.get("aliases") or []:
            strings.add(alias)
        card_pre = cards.get(key) or {}
        strings.add(card_pre.get("title"))
        strings.add(card_pre.get("posterPath"))
        imdb = facts.get("imdbId")
        strings.add(imdb[0] if isinstance(imdb, list) and imdb else imdb)
        for c in facts.get("countries") or []:
            strings.add(c)
        for lang in facts.get("languages") or []:
            strings.add(lang)
    noul_names = sorted(noul_names)
    critique_names = sorted(critique_names)
    technique_names = sorted(technique_names)
    depicts_names = sorted(depicts_names)
    audience_names = sorted(audience_names)
    for name in noul_names + critique_names + technique_names + depicts_names + audience_names:
        strings.add(name)
    for qid, ent in entities.items():
        strings.add((ent.get("en") if isinstance(ent, dict) else ent) or qid)
        # And every other name they go by — people search indexes those too.
        if isinstance(ent, dict):
            for alias in ent.get("aliases") or []:
                strings.add(alias)

    # Every Q-id any record REFERS to, so the entity table can cover all of them — see below.
    referenced = set()
    for row in rows.values():
        facts_row = row.get("facts") or {}
        for field in ("directors", "creators", "screenwriters", *ENTITY_LISTS):
            for q in facts_row.get(field) or []:
                if isinstance(q, str) and q.startswith("Q") and q[1:].isdigit():
                    referenced.add(int(q[1:]))
    known = {int(q[1:]) for q in entities if q.startswith("Q") and q[1:].isdigit()}
    for num in referenced - known:
        strings.add(f"Q{num}")

    ordered_strings = strings.freeze()
    print(f"  {len(ordered_strings)} strings", file=sys.stderr)

    # Entity ids: the sorted Q-id numbers. Everything referring to a person or company uses this index.
    #
    # The UNION of the entity table and every Q-id a record refers to. 1,236 references (0.76%) name
    # something the table does not describe, and interning against the table alone dropped them — a cast
    # member nobody can name still connects two titles, and the rail counts that overlap by id without
    # ever needing the name. It is also what made 2,680 of 3,019 franchise references unresolvable.
    ent_qids = sorted(known | referenced)
    ent_index = {q: i for i, q in enumerate(ent_qids)}

    unresolved = defaultdict(int)

    def ent_id(qid, what=None):
        """An entity index, or None — counted, never silently discarded. 2,680 franchise references were
        dropped here without a word because the entity table does not contain franchise entities."""
        if not isinstance(qid, str) or not qid.startswith("Q") or not qid[1:].isdigit():
            return None
        found = ent_index.get(int(qid[1:]))
        if found is None and what:
            unresolved[what] += 1
        return found

    sec = Sections(n)
    noul_id = {name: i for i, name in enumerate(noul_names)}

    sec.put("keys", "Q", [((0 if k.split(":", 1)[0] == "movie" else 1) << 32) | int(k.split(":", 1)[1])
                          for k in keys], 8, expect=n)
    blob = "".join(ordered_strings).encode("utf-8")
    offs, at = [0], 0
    for s in ordered_strings:
        at += len(s.encode("utf-8"))
        offs.append(at)
    sec.put_raw("strings", blob, 1)
    sec.put("str_off", "I", offs, 4, expect=len(ordered_strings) + 1)

    card_title, card_poster, card_year, votes = [], [], [], []
    primary, subgenres, moods, animated = [], [], [], []
    facet_v, facet_c = [], bytearray()   # dense R x 12, axis order = FACET_AXES
    scores = {a: [] for a in SCORE_AXES}
    world, critique, technique = [], bytearray(), bytearray()
    noul_rows = []
    makers, genres, countries, languages, aliases = [], [], [], [], []
    imdb, released, released_prec, runtime, franchise = [], [], [], [], []
    ended, ended_prec, episodes, seasons, has_vector = [], [], [], [], []
    ent_lists = {field: [] for field in ENTITY_LISTS}
    based_kind = []
    applic_v, applic_c = [], bytearray()
    depicts, audience = bytearray(), bytearray()
    orig_lang = []
    with_labels = with_premise = with_cards = 0

    for key in keys:
        r = rows[key]
        facts = r.get("facts") or {}
        labels = r.get("labels") or {}
        if labels:
            with_labels += 1
        if r.get("premiseLabels"):
            with_premise += 1
        t = facts.get("titles") or {}
        card = cards.get(key) or {}
        card_title.append(strings.id(card.get("title") or t.get("en") or t.get("orig")))
        card_poster.append(strings.id(card.get("posterPath")))
        year = card.get("year")
        card_year.append(int(year) if isinstance(year, int) and -32767 <= year <= 32767 else I16_NONE)
        # u32, clamped: TMDB's largest is five figures, and a browse row only ever compares them.
        votes.append(min(0xFFFFFFFF, max(0, vote_counts.get(key, 0))))
        if card:
            with_cards += 1

        primary.append(strings.id(labels.get("primaryGenre")))
        subgenres.append(labelled(labels.get("subgenres"), strings, "subgenre", key))
        moods.append(labelled(labels.get("moods"), strings, "mood", key))
        animated.append(1 if labels.get("animated") else 0)

        for axis in FACET_AXES:
            v = (r.get("facets") or {}).get(axis)
            if isinstance(v, dict) and v.get("choice") and v["choice"] != "does-not-apply":
                facet_v.append(strings.id(v["choice"]))
                facet_c.append(hundredths(v.get("confidence"), f"facet {axis} confidence", key))
            else:
                facet_v.append(U32_NONE)
                facet_c.append(0)

        sc = r.get("scores") or {}
        for axis in SCORE_AXES:
            entry = sc.get(axis)
            scores[axis].append(score_hundredths((entry or {}).get("score"), f"score {axis}", key))

        nouls = r.get("nouls") or {}
        worst = 0
        pairs = []
        for name, entry in sorted(nouls.items()):
            value = hundredths((entry or {}).get("noul"), f"noul {name}", key)
            if value:
                pairs.append((noul_id[name], value))
            if name in FANTASTICAL:
                worst = max(worst, value)
        world.append(worst)
        noul_rows.append(pairs)

        crit = r.get("critique") or {}
        for name in critique_names:
            critique.append(hundredths((crit.get(name) or {}).get("noul"), f"critique {name}", key))
        tech = r.get("technique") or {}
        for name in technique_names:
            technique.append(hundredths((tech.get(name) or {}).get("noul"), f"technique {name}", key))
        dep = r.get("depicts") or {}
        for name in depicts_names:
            depicts.append(hundredths((dep.get(name) or {}).get("noul"), f"depicts {name}", key))
        aud = r.get("audience") or {}
        for name in audience_names:
            audience.append(hundredths((aud.get(name) or {}).get("noul"), f"audience {name}", key))
        applic = r.get("applicability") or {}
        for name in APPLICABILITY:
            entry = applic.get(name)
            if isinstance(entry, dict) and entry.get("choice"):
                applic_v.append(strings.id(entry["choice"]))
                applic_c.append(hundredths(entry.get("confidence"), f"{name} confidence", key))
            else:
                applic_v.append(U32_NONE)
                applic_c.append(0)

        # makers = directors ∪ creators ∪ screenwriters. Screenwriters were shipped and dropped by the
        # reader for months: 67.2% coverage feeding the rail's heaviest weight.
        seen, row_makers = set(), []
        for field in ("directors", "creators", "screenwriters"):
            for q in facts.get(field) or []:
                i = ent_id(q, "maker")
                if i is not None and i not in seen:
                    seen.add(i)
                    row_makers.append(i)
        makers.append(row_makers)
        media_key = "tv" if key.startswith("tv:") else "movie"
        row_genres = []
        for q in facts.get("genres") or []:
            if not isinstance(q, str):
                if isinstance(q, int) and q not in row_genres:
                    row_genres.append(q)
                continue
            entry = genre_map.get(q)
            if entry is None:
                unresolved["genre"] += 1
                continue
            # The FILM mapping, falling back to the series one — `genre.movie.or(genre.tv)`, which is
            # what den-atlas has always done, and the reader then folds a composite into its film parts.
            #
            # Taking the media-specific mapping instead looked more faithful and lost information: TMDB's
            # series genres are COMPOSITES (10765 "Sci-Fi & Fantasy", 10759 "Action & Adventure"), so
            # several distinct Wikidata genres collapse into one. Measured over the corpus it changed
            # 1,943 series and was a strict LOSS for 607 of them — Chilling Adventures of Sabrina went
            # from Drama/Horror/Fantasy to Drama/Sci-Fi&Fantasy, dropping Horror outright, and Scooby-Doo
            # lost Horror the same way. It also emitted composite ids that the clients' hide rules and
            # /recommend do not speak, since both work in film genres.
            #
            # One Wikidata genre can still be several TMDB genres — "romantic comedy" is Comedy AND
            # Romance — so a list is accepted and a bare int treated as a list of one.
            mapped = (entry.get("movie") or entry.get("tv")) if isinstance(entry, dict) else entry
            for value in (mapped if isinstance(mapped, list) else [mapped]):
                if isinstance(value, int) and value not in row_genres:
                    row_genres.append(value)
        # NOT sorted. The order is the genreMap's, and `/recommend` treats the first as the most
        # significant when it names a title's genre — so sorting quietly renamed things: Jupiter
        # Ascending went from Action to Adventure. Dedup preserving first-seen instead.
        seen, ordered = set(), []
        for g in row_genres:
            if g not in seen:
                seen.add(g)
                ordered.append(g)
        genres.append(ordered)
        countries.append([strings.id(c) for c in facts.get("countries") or []])
        languages.append([strings.id(x) for x in facts.get("languages") or []])
        # `en` and `orig` FIRST, then the aliases — the same three sources, in the same order, that the JSON
        # reader chained (`den-atlas/src/facts.rs`: `t.en.chain(t.orig).chain(t.aliases)`).
        #
        # This wrote `aliases` alone until 2026-09-21, and it is a DIFFERENT field:
        # `check-alias-collisions.py` treats `own_names = [orig, en]` as the set aliases are vetted against,
        # so the two never overlap by construction. The names dropped were therefore exactly a title's own.
        # atlas builds its display-title search index from these, so every title whose original name differs
        # from its TMDB one stopped being findable by that name — "Gisaengchung" for Parasite. The
        # record-by-record comparison could not see it: the names live in their own map, not on a record.
        row_titles, taken = [], set()
        for name in [t.get("en"), t.get("orig"), *(t.get("aliases") or [])]:
            if not isinstance(name, str) or not name.strip():
                continue
            ident = strings.id(name)
            if ident not in taken:
                taken.add(ident)
                row_titles.append(ident)
        aliases.append(row_titles)

        raw_imdb = facts.get("imdbId")
        imdb.append(strings.id(raw_imdb[0] if isinstance(raw_imdb, list) and raw_imdb else raw_imdb))
        days, prec = days_since_epoch(facts.get("released") or facts.get("started"), key)
        released.append(days)
        released_prec.append(prec)
        mins = facts.get("runtimeMinutes")
        runtime.append(min(65535, int(mins)) if isinstance(mins, int) and mins > 0 else 0)
        # The raw Q-ID, not an entity index. The entity table holds almost no franchise entities —
        # 2,680 of 3,019 references are unresolvable — so interning this dropped the franchise for
        # seven titles in eight, silently. A Q-id needs no table to be useful: two titles sharing one
        # are in the same series whether or not anything can name it.
        fr = facts.get("franchise")
        fr = fr[0] if isinstance(fr, list) and fr else fr
        fr_num = int(fr[1:]) if isinstance(fr, str) and fr.startswith("Q") and fr[1:].isdigit() else None
        franchise.append(fr_num if fr_num is not None else U32_NONE)
        for field in ENTITY_LISTS:
            ent_lists[field].append(
                [i for i in (ent_id(q, field) for q in facts.get(field) or []) if i is not None])
        based_kind.append([strings.id(k) for k in facts.get("basedOnKind") or [] if k])
        end_days, end_prec = days_since_epoch(facts.get("ended"), key)
        ended.append(end_days)
        ended_prec.append(end_prec)
        eps, sns = facts.get("episodes"), facts.get("seasons")
        episodes.append(min(65535, int(eps)) if isinstance(eps, int) and eps > 0 else 0)
        seasons.append(min(65535, int(sns)) if isinstance(sns, int) and sns > 0 else 0)
        has_vector.append(1 if facts.get("hasVector") else 0)
        langs = facts.get("languages") or []
        orig_lang.append(strings.id(langs[0]) if langs else U32_NONE)

    sec.put("card_title", "I", card_title, 4, expect=n)
    sec.put("card_poster", "I", card_poster, 4, expect=n)
    sec.put("card_year", "h", card_year, 2, expect=n)
    sec.put("votes", "I", votes, 4, expect=n)
    sec.put("primary_genre", "I", primary, 4, expect=n)
    sec.put_labelled_list("subgenre", subgenres)
    sec.put_labelled_list("mood", moods)
    sec.put("animated", "B", animated, 1, expect=n)
    sec.put("facet_v", "I", facet_v, 4, expect=n * len(FACET_AXES))
    sec.put_raw("facet_c", facet_c, 1, expect=n * len(FACET_AXES))
    for axis in SCORE_AXES:
        sec.put(SCORE_SECTION[axis], "H", scores[axis], 2, expect=n)
    sec.put("world", "B", world, 1, expect=n)
    sec.put_list("noul_k", "B", 1, [[k for k, _ in row] for row in noul_rows])
    sec.put_list("noul_v", "B", 1, [[v for _, v in row] for row in noul_rows])
    sec.put("noul_names", "I", [strings.id(x) for x in noul_names], 4, expect=len(noul_names))
    sec.put_raw("critique", critique, 1, expect=n * len(critique_names))
    sec.put("critique_names", "I", [strings.id(x) for x in critique_names], 4, expect=len(critique_names))
    sec.put_raw("technique", technique, 1, expect=n * len(technique_names))
    sec.put("technique_names", "I", [strings.id(x) for x in technique_names], 4, expect=len(technique_names))
    sec.put_raw("depicts", depicts, 1, expect=n * len(depicts_names))
    sec.put("depicts_names", "I", [strings.id(x) for x in depicts_names], 4, expect=len(depicts_names))
    sec.put_raw("audience", audience, 1, expect=n * len(audience_names))
    sec.put("audience_names", "I", [strings.id(x) for x in audience_names], 4, expect=len(audience_names))
    sec.put_list("makers", "I", 4, makers)
    for field, section in ENTITY_LISTS.items():
        sec.put_list(section, "I", 4, ent_lists[field])
    sec.put_list("based_kind", "I", 4, based_kind)
    sec.put_list("genres", "I", 4, genres)
    sec.put_list("countries", "I", 4, countries)
    sec.put_list("languages", "I", 4, languages)
    sec.put_list("alias_titles", "I", 4, aliases)
    sec.put("imdb", "I", imdb, 4, expect=n)
    sec.put("released", "i", released, 4, expect=n)
    sec.put("released_prec", "B", released_prec, 1, expect=n)
    sec.put("ended", "i", ended, 4, expect=n)
    sec.put("ended_prec", "B", ended_prec, 1, expect=n)
    sec.put("episodes", "H", episodes, 2, expect=n)
    sec.put("seasons", "H", seasons, 2, expect=n)
    # `facts_has_vector` is the FACTS field of that name — a scrape-time claim about whether a
    # vector was expected. It is NOT "this row has a plot vector": 9,010 rows carry a real
    # vector while this reads 0, and 3 read 1 with none. It sat two entries from
    # `vec_premise_has`, which does mean what it says, under a name that invited the confusion.
    sec.put("facts_has_vec", "B", has_vector, 1, expect=n)
    sec.put("applic_v", "I", applic_v, 4, expect=n * len(APPLICABILITY))
    sec.put_raw("applic_c", applic_c, 1, expect=n * len(APPLICABILITY))
    sec.put("runtime", "H", runtime, 2, expect=n)
    sec.put("franchise", "I", franchise, 4, expect=n)
    sec.put("orig_lang", "I", orig_lang, 4, expect=n)

    # Entities, and how many titles credit each — the rarity weight the people row needs.
    credits = [0] * len(ent_qids)
    for row in makers:
        for i in row:
            credits[i] += 1
    for row in ent_lists["cast"]:
        for i in row:
            credits[i] += 1
    ent_name, ent_tmdb, ent_alias = [], [], []
    by_num = {int(q[1:]): q for q in entities if q.startswith("Q") and q[1:].isdigit()}
    for num in ent_qids:
        # An entity the table does not describe: referenced by a record but with no entry. Its Q-id
        # stands in as its name — it still connects the titles that credit it, which is what the rail
        # and the cast overlap actually read.
        raw = entities.get(by_num.get(num, f"Q{num}"))
        ent = raw if isinstance(raw, dict) else ({"en": raw} if raw else {})
        ent_name.append(strings.id(ent.get("en") or f"Q{num}"))
        tmdb = ent.get("tmdbPersonId")
        ent_tmdb.append(int(tmdb) if isinstance(tmdb, str) and tmdb.isdigit() else U32_NONE)
        # The OTHER names a person goes by. People search indexes these as well as the `en` name —
        # 64,075 of 162,812 entities have them, 118,958 in all — so a store without them answers
        # "Michael James Vogel" with nothing while the JSON facts answered Mike Vogel.
        #
        # The strings, not hashes: the reader hashes them with `name_key`, which folds and normalises in
        # a way this writer would have to reimplement, and a second copy of that algorithm is exactly the
        # drift this format exists to prevent. They land in the shared dictionary, so the repeats cost
        # nothing, and the reader drops them once its index is built.
        ent_alias.append([strings.id(a) for a in (ent.get("aliases") or []) if a])
    entity_count = len(ent_qids)
    sec.put("ent_qid", "I", ent_qids, 4, expect=entity_count)
    sec.put("ent_name", "I", ent_name, 4, expect=entity_count)
    sec.put("ent_tmdb", "I", ent_tmdb, 4, expect=entity_count)
    sec.put("ent_credits", "I", credits, 4, expect=entity_count)
    sec.put_list("ent_alias", "I", 4, ent_alias, expect_rows=entity_count)

    # The inverted maker index: 355 KB that turns a per-request linear scan of every record into a lookup.
    by_maker = defaultdict(list)
    for row_i, row in enumerate(makers):
        for i in row:
            by_maker[i].append(row_i)
    maker_ent = sorted(by_maker)
    sec.put("maker_ent", "I", maker_ent, 4, expect=len(maker_ent))
    flat, offsets = [], [0]
    for i in maker_ent:
        flat.extend(by_maker[i])
        offsets.append(len(flat))
    sec.put("maker_rows_v", "I", flat, 4, expect=len(flat))
    sec.put("maker_rows_o", "I", offsets, 4, expect=len(maker_ent) + 1)

    # Vectors, re-ordered from their own row order into ours. A row with no vector is zeroed.
    print("reading vectors …", file=sys.stderr)
    plot_blob, plot_base = read_vectors(args.vectors, len(plot_order), args.vector_labels)
    plot = bytearray(n * DIMS)
    has_plot_row = [0] * n
    plot_hits = 0
    for out_i, key in enumerate(keys):
        src = plot_row.get(key)
        if src is None:
            continue
        start = plot_base + src * DIMS
        row = plot_blob[start:start + DIMS]
        if len(row) != DIMS:
            sys.exit(f"{key}: plot vector row {src} is {len(row)} bytes, not {DIMS}")
        plot[out_i * DIMS:(out_i + 1) * DIMS] = row
        has_plot_row[out_i] = 1
        plot_hits += 1
    sec.put_raw("vec_plot", plot, 1, expect=n * DIMS)

    premise = bytearray(n * DIMS)
    has_premise = [0] * n
    premise_hits = 0
    if args.premise_vectors and premise_row:
        pblob, pbase = read_vectors(args.premise_vectors, len(premise_row), args.premise_labels)
        for out_i, key in enumerate(keys):
            src = premise_row.get(key)
            if src is None:
                continue
            start = pbase + src * DIMS
            row = pblob[start:start + DIMS]
            if len(row) != DIMS:
                sys.exit(f"{key}: premise vector row {src} is {len(row)} bytes, not {DIMS}")
            premise[out_i * DIMS:(out_i + 1) * DIMS] = row
            has_premise[out_i] = 1
            premise_hits += 1
    sec.put_raw("vec_premise", premise, 1, expect=n * DIMS)
    sec.put("vec_premise_has", "B", has_premise, 1, expect=n)
    # The plain question, answerable without scanning 1024 bytes for a non-zero.
    sec.put("vec_plot_has", "B", has_plot_row, 1, expect=n)

    # ---- the asserts that make a silent miss impossible -------------------------------------------
    # Compared against the artifacts themselves. A 50% threshold would have passed a join that lost
    # 23,000 rows, and "more than half worked" is not a standard anything here should meet.
    for what, got, want, source in (
        ("rows", n, facts_records, args.facts),
        ("labels", with_labels, len(labels_source), args.vector_labels),
        ("cards", with_cards, len(cards), args.metadata),
        ("plot vectors", plot_hits, len(plot_order), args.vectors),
        ("premise vectors", premise_hits, len(premise_row), args.premise_labels),
    ):
        if got != want:
            sys.exit(f"{what}: {got} in the store, {want} in {source} — they must agree exactly")
    # Unresolved references, named and counted. A reference the entity table cannot resolve is dropped —
    # that is unavoidable when the table is short — but dropping it WITHOUT SAYING SO is how 2,680
    # franchise links disappeared into a section that looked perfectly well formed.
    if unresolved:
        print("unresolved entity references (dropped):", file=sys.stderr)
        for what, count in sorted(unresolved.items(), key=lambda kv: -kv[1]):
            print(f"  {what:14} {count}", file=sys.stderr)

    print(json.dumps({"titles": n, "withLabels": with_labels, "withPremiseLabels": with_premise,
                      "withCards": with_cards, "unresolved": dict(sorted(unresolved.items())),
                      "plotVectors": plot_hits, "premiseVectors": premise_hits,
                      "entities": len(ent_qids), "strings": len(ordered_strings),
                      "sections": len(sec.order)}, indent=1), file=sys.stderr)

    # ---- assemble --------------------------------------------------------------------------------
    table_bytes = ENTRY_BYTES * len(sec.order)
    at = HEADER_BYTES + table_bytes
    entries, body = [], []
    for name in sec.order:
        pad = (-at) % ALIGN
        if pad:
            body.append(b"\0" * pad)
            at += pad
        blk = sec.blocks[name]
        entries.append((name, at, len(blk), sec.widths[name]))
        body.append(blk)
        at += len(blk)

    table = b"".join(struct.pack("<16sQII", name.encode(), off, length, width)
                     for name, off, length, width in entries)
    payload = table + b"".join(body)
    digest = int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "little")
    header = struct.pack("<8sIIQII16s16s", MAGIC, FORMAT_VERSION, ENDIAN_CHECK, digest,
                         len(sec.order), n, args.dataset_version.encode()[:16], b"")
    if len(header) != HEADER_BYTES:
        sys.exit(f"header is {len(header)} bytes, expected {HEADER_BYTES}")

    for name, off, _, width in entries:
        if width > 1 and off % ALIGN:
            sys.exit(f"section {name} at {off} is not {ALIGN}-byte aligned")

    with open(args.out, "wb") as fh:
        fh.write(header)
        fh.write(payload)

    # Declare it in the manifest, here, from the bytes just written.
    #
    # NOT a field on Swift's `DatasetMeta`: `namingSidecar` there enumerates every field by hand while
    # `ownedKeys` comes from `CodingKeys`, so a new key with a default compiles, is treated as owned, and
    # is silently dropped by the next `metadata` run. `ManifestMerge` carries unowned keys forward
    # instead, which is why `maxBatchId` is stamped from a script too.
    #
    # No `storeGzFile`, ever: atlas MMAPS this file and a compressed one cannot be mapped.
    if args.stamp_meta:
        blob = open(args.out, "rb").read()
        meta = read_json(args.stamp_meta)
        meta["storeFile"] = os.path.basename(args.out)
        meta["storeSha256"] = hashlib.sha256(blob).hexdigest()
        meta["storeBytes"] = len(blob)
        with open(args.stamp_meta, "w") as fh:
            json.dump(meta, fh, indent=1)
            fh.write("\n")

    print(json.dumps({"out": args.out, "bytes": HEADER_BYTES + len(payload),
                      "formatVersion": FORMAT_VERSION, "stamped": bool(args.stamp_meta)}, indent=1))


if __name__ == "__main__":
    main()
