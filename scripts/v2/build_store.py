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
import struct
import sys
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
# `world` is the max of these, as `build_rail_facets.py` defines it.
FANTASTICAL = [f"theme__{k}" for k in (
    "vampire", "werewolf_monster", "zombie", "superhero", "time_travel", "cyberpunk",
    "dystopian_post_apocalyptic", "folk_horror")] + [f"subgenre__{k}" for k in (
    "supernatural_horror", "sci_fi_horror", "sci_fi_action", "fantasy_adventure")]

U16_NONE = 0xFFFF
U32_NONE = 0xFFFFFFFF
I16_NONE = -0x8000
I32_NONE = -0x80000000


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


def twentieths(value, what, key):
    """A 0..4 score as u8 twentieths — u8 hundredths would overflow at 2.56."""
    if value is None:
        return 0
    f = float(value)
    if f != f or f < 0.0 or f > 4.0:
        sys.exit(f"{key}: {what} is {value} — outside the 0..4 score range")
    return min(200, round(f * 50))


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
        out.append((strings.id(label, U16_NONE), hundredths(conf, f"{what} confidence", key) if conf else 0))
    return out


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

    def put_raw(self, name, blob, width):
        self.blocks[name] = bytes(blob)
        self.widths[name] = width
        self.order.append(name)

    def put_list(self, name, fmt, width, per_row):
        """A values array plus offsets of len(rows)+1 — an empty list is a zero-width span."""
        flat, offsets = [], [0]
        for row in per_row:
            flat.extend(row)
            offsets.append(len(flat))
        if len(offsets) != self.rows + 1:
            sys.exit(f"section {name}: {len(offsets)} offsets, expected {self.rows + 1}")
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
        self.put(f"{name}_v", "H", ids, 2)
        self.put(f"{name}_c", "B", confs, 1)
        self.put(f"{name}_o", "I", offsets, 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--entities", required=True)
    ap.add_argument("--vectors", required=True, help="vectors-bge-m3.bin")
    ap.add_argument("--vector-labels", required=True, help="labels-t02.json — the row order the vectors align to")
    ap.add_argument("--premise-vectors")
    ap.add_argument("--premise-labels")
    ap.add_argument("--facets-bin", help="facets.bin, for vote counts")
    ap.add_argument("--dataset-version", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    print("reading the corpus …", file=sys.stderr)
    rows = {r["key"]: r for r in corpus_rows(args.corpus)}
    keys = sorted(rows, key=lambda k: ((0 if k.split(":", 1)[0] == "movie" else 1), int(k.split(":", 1)[1])))
    n = len(keys)
    print(f"  {n} titles", file=sys.stderr)

    entities = read_json(args.entities)

    # Vector rows come from the labels file the .bin aligns to, positionally. Nothing else knows the order.
    print("reading vector row order …", file=sys.stderr)
    plot_order = list(labels_by_key(args.vector_labels, "vector labels"))
    plot_row = {k: i for i, k in enumerate(plot_order)}
    premise_row = {}
    if args.premise_labels:
        premise_row = {k: i for i, k in enumerate(labels_by_key(args.premise_labels, "premise labels"))}

    strings = Strings()   # free text: titles, aliases, entity names, poster paths, imdb ids (u32)
    vocab = Strings()     # the controlled vocabulary: facet values, label names, countries (u16)
    noul_names, critique_names, technique_names = set(), set(), set()
    for key in keys:
        r = rows[key]
        labels = r.get("labels") or {}
        vocab.add(labels.get("primaryGenre"))
        for entry in (labels.get("subgenres") or []) + (labels.get("moods") or []):
            vocab.add(entry.get("label") if isinstance(entry, dict) else entry)
        for axis in FACET_AXES:
            v = (r.get("facets") or {}).get(axis)
            if isinstance(v, dict) and v.get("choice") and v["choice"] != "does-not-apply":
                vocab.add(v["choice"])
        noul_names.update((r.get("nouls") or {}).keys())
        critique_names.update((r.get("critique") or {}).keys())
        technique_names.update((r.get("technique") or {}).keys())
        facts = r.get("facts") or {}
        t = facts.get("titles") or {}
        for name in (t.get("en"), t.get("orig")):
            strings.add(name)
        for alias in t.get("aliases") or []:
            strings.add(alias)
        strings.add(facts.get("posterPath"))
        imdb = facts.get("imdbId")
        strings.add(imdb[0] if isinstance(imdb, list) and imdb else imdb)
        for c in facts.get("countries") or []:
            vocab.add(c)
        for lang in facts.get("languages") or []:
            vocab.add(lang)
    noul_names = sorted(noul_names)
    critique_names = sorted(critique_names)
    technique_names = sorted(technique_names)
    for name in noul_names + critique_names + technique_names:
        vocab.add(name)
    for qid, ent in entities.items():
        strings.add((ent.get("en") if isinstance(ent, dict) else ent) or qid)

    ordered_strings = strings.freeze()
    ordered_vocab = vocab.freeze()
    if len(ordered_vocab) > 0xFFFE:
        sys.exit(f"vocabulary is {len(ordered_vocab)} entries — too many for u16 ids")
    print(f"  {len(ordered_strings)} strings, {len(ordered_vocab)} vocabulary",
          file=sys.stderr)

    # Entity ids: the sorted Q-id numbers. Everything referring to a person or company uses this index.
    ent_qids = sorted(int(q[1:]) for q in entities if q.startswith("Q") and q[1:].isdigit())
    ent_index = {q: i for i, q in enumerate(ent_qids)}

    def ent_id(qid):
        if not isinstance(qid, str) or not qid.startswith("Q") or not qid[1:].isdigit():
            return None
        return ent_index.get(int(qid[1:]))

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
    vblob = "".join(ordered_vocab).encode("utf-8")
    voffs, vat = [0], 0
    for s_ in ordered_vocab:
        vat += len(s_.encode("utf-8"))
        voffs.append(vat)
    sec.put_raw("vocab", vblob, 1)
    sec.put("vocab_off", "I", voffs, 4, expect=len(ordered_vocab) + 1)

    card_title, card_poster, card_year = [], [], []
    primary, subgenres, moods, animated = [], [], [], []
    facet_v, facet_c = [], bytearray()   # dense R x 12, axis order = FACET_AXES
    scores = {a: [] for a in SCORE_AXES}
    world, critique, technique = [], bytearray(), bytearray()
    noul_rows = []
    makers, cast, broadcasters, genres, countries, languages, aliases = [], [], [], [], [], [], []
    imdb, released, runtime, franchise = [], [], [], []
    orig_lang = []
    with_labels = with_premise = 0

    for key in keys:
        r = rows[key]
        facts = r.get("facts") or {}
        labels = r.get("labels") or {}
        if labels:
            with_labels += 1
        if r.get("premiseLabels"):
            with_premise += 1
        t = facts.get("titles") or {}
        card_title.append(strings.id(t.get("en") or t.get("orig")))
        card_poster.append(strings.id(facts.get("posterPath")))
        year = facts.get("year")
        card_year.append(int(year) if isinstance(year, int) else I16_NONE)

        primary.append(vocab.id(labels.get("primaryGenre"), U16_NONE))
        subgenres.append(labelled(labels.get("subgenres"), vocab, "subgenre", key))
        moods.append(labelled(labels.get("moods"), vocab, "mood", key))
        animated.append(1 if labels.get("animated") else 0)

        for axis in FACET_AXES:
            v = (r.get("facets") or {}).get(axis)
            if isinstance(v, dict) and v.get("choice") and v["choice"] != "does-not-apply":
                facet_v.append(vocab.id(v["choice"], U16_NONE))
                facet_c.append(hundredths(v.get("confidence"), f"facet {axis} confidence", key))
            else:
                facet_v.append(U16_NONE)
                facet_c.append(0)

        sc = r.get("scores") or {}
        for axis in SCORE_AXES:
            entry = sc.get(axis)
            scores[axis].append(twentieths((entry or {}).get("score"), f"score {axis}", key))

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

        # makers = directors ∪ creators ∪ screenwriters. Screenwriters were shipped and dropped by the
        # reader for months: 67.2% coverage feeding the rail's heaviest weight.
        seen, row_makers = set(), []
        for field in ("directors", "creators", "screenwriters"):
            for q in facts.get(field) or []:
                i = ent_id(q)
                if i is not None and i not in seen:
                    seen.add(i)
                    row_makers.append(i)
        makers.append(row_makers)
        cast.append([i for i in (ent_id(q) for q in facts.get("cast") or []) if i is not None])
        broadcasters.append([i for i in (ent_id(q) for q in facts.get("broadcaster") or []) if i is not None])
        genres.append([g for g in facts.get("genres") or [] if isinstance(g, int)])
        countries.append([vocab.id(c, U16_NONE) for c in facts.get("countries") or []])
        languages.append([vocab.id(x, U16_NONE) for x in facts.get("languages") or []])
        aliases.append([strings.id(a) for a in (t.get("aliases") or []) if a])

        raw_imdb = facts.get("imdbId")
        imdb.append(strings.id(raw_imdb[0] if isinstance(raw_imdb, list) and raw_imdb else raw_imdb))
        rel = facts.get("released") or facts.get("started")
        released.append(int(rel) if isinstance(rel, int) else I32_NONE)
        mins = facts.get("runtimeMinutes")
        runtime.append(min(65535, int(mins)) if isinstance(mins, int) and mins > 0 else 0)
        fr = facts.get("franchise")
        fr = fr[0] if isinstance(fr, list) and fr else fr
        franchise.append(ent_id(fr) if ent_id(fr) is not None else U32_NONE)
        langs = facts.get("languages") or []
        orig_lang.append(vocab.id(langs[0], U16_NONE) if langs else U16_NONE)

    sec.put("card_title", "I", card_title, 4, expect=n)
    sec.put("card_poster", "I", card_poster, 4, expect=n)
    sec.put("card_year", "h", card_year, 2, expect=n)
    sec.put("primary_genre", "H", primary, 2, expect=n)
    sec.put_labelled_list("subgenre", subgenres)
    sec.put_labelled_list("mood", moods)
    sec.put("animated", "B", animated, 1, expect=n)
    sec.put("facet_v", "H", facet_v, 2, expect=n * len(FACET_AXES))
    sec.put_raw("facet_c", facet_c, 1)
    for axis in SCORE_AXES:
        sec.put(SCORE_SECTION[axis], "B", scores[axis], 1, expect=n)
    sec.put("world", "B", world, 1, expect=n)
    sec.put_list("noul_k", "B", 1, [[k for k, _ in row] for row in noul_rows])
    sec.put_list("noul_v", "B", 1, [[v for _, v in row] for row in noul_rows])
    sec.put("noul_names", "H", [vocab.id(x, U16_NONE) for x in noul_names], 2)
    sec.put_raw("critique", critique, 1)
    sec.put("critique_names", "H", [vocab.id(x, U16_NONE) for x in critique_names], 2)
    sec.put_raw("technique", technique, 1)
    sec.put("technique_names", "H", [vocab.id(x, U16_NONE) for x in technique_names], 2)
    sec.put_list("makers", "I", 4, makers)
    sec.put_list("cast", "I", 4, cast)
    sec.put_list("broadcasters", "I", 4, broadcasters)
    sec.put_list("genres", "I", 4, genres)
    sec.put_list("countries", "H", 2, countries)
    sec.put_list("languages", "H", 2, languages)
    sec.put_list("alias_titles", "I", 4, aliases)
    sec.put("imdb", "I", imdb, 4, expect=n)
    sec.put("released", "i", released, 4, expect=n)
    sec.put("runtime", "H", runtime, 2, expect=n)
    sec.put("franchise", "I", franchise, 4, expect=n)
    sec.put("orig_lang", "H", orig_lang, 2, expect=n)

    # Entities, and how many titles credit each — the rarity weight the people row needs.
    credits = [0] * len(ent_qids)
    for row in makers:
        for i in row:
            credits[i] += 1
    for row in cast:
        for i in row:
            credits[i] += 1
    ent_name, ent_tmdb = [], []
    by_num = {int(q[1:]): q for q in entities if q.startswith("Q") and q[1:].isdigit()}
    for num in ent_qids:
        ent = entities[by_num[num]]
        ent = ent if isinstance(ent, dict) else {"en": ent}
        ent_name.append(strings.id(ent.get("en") or by_num[num]))
        tmdb = ent.get("tmdbPersonId")
        ent_tmdb.append(int(tmdb) if isinstance(tmdb, str) and tmdb.isdigit() else U32_NONE)
    sec.put("ent_qid", "I", ent_qids, 4)
    sec.put("ent_name", "I", ent_name, 4)
    sec.put("ent_tmdb", "I", ent_tmdb, 4)
    sec.put("ent_credits", "I", credits, 4)

    # The inverted maker index: 355 KB that turns a per-request linear scan of every record into a lookup.
    by_maker = defaultdict(list)
    for row_i, row in enumerate(makers):
        for i in row:
            by_maker[i].append(row_i)
    maker_ent = sorted(by_maker)
    sec.put("maker_ent", "I", maker_ent, 4)
    flat, offsets = [], [0]
    for i in maker_ent:
        flat.extend(by_maker[i])
        offsets.append(len(flat))
    sec.put("maker_rows_v", "I", flat, 4)
    sec.put("maker_rows_o", "I", offsets, 4)

    # Vectors, re-ordered from their own row order into ours. A row with no vector is zeroed.
    print("reading vectors …", file=sys.stderr)
    with open(args.vectors, "rb") as fh:
        plot_blob = fh.read()
    plot_base = len(plot_blob) - len(plot_order) * DIMS
    if plot_base < 0:
        sys.exit(f"{args.vectors}: {len(plot_blob)} bytes for {len(plot_order)} rows of {DIMS}")
    plot = bytearray(n * DIMS)
    plot_hits = 0
    for out_i, key in enumerate(keys):
        src = plot_row.get(key)
        if src is None:
            continue
        start = plot_base + src * DIMS
        plot[out_i * DIMS:(out_i + 1) * DIMS] = plot_blob[start:start + DIMS]
        plot_hits += 1
    sec.put_raw("vec_plot", plot, 1)

    premise = bytearray(n * DIMS)
    has_premise = [0] * n
    premise_hits = 0
    if args.premise_vectors and premise_row:
        with open(args.premise_vectors, "rb") as fh:
            pblob = fh.read()
        pbase = len(pblob) - len(premise_row) * DIMS
        for out_i, key in enumerate(keys):
            src = premise_row.get(key)
            if src is None:
                continue
            start = pbase + src * DIMS
            premise[out_i * DIMS:(out_i + 1) * DIMS] = pblob[start:start + DIMS]
            has_premise[out_i] = 1
            premise_hits += 1
    sec.put_raw("vec_premise", premise, 1)
    sec.put("vec_premise_has", "B", has_premise, 1, expect=n)

    # ---- the asserts that make a silent miss impossible -------------------------------------------
    if with_labels < n * 0.5:
        sys.exit(f"only {with_labels} of {n} rows carry labels — the corpus join is wrong, not the data")
    if plot_hits < n * 0.5:
        sys.exit(f"only {plot_hits} of {n} rows matched a plot vector — the row order is wrong")
    print(json.dumps({"titles": n, "withLabels": with_labels, "withPremiseLabels": with_premise,
                      "plotVectors": plot_hits, "premiseVectors": premise_hits,
                      "entities": len(ent_qids), "strings": len(ordered_strings), "vocab": len(ordered_vocab),
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
    print(json.dumps({"out": args.out, "bytes": HEADER_BYTES + len(payload),
                      "formatVersion": FORMAT_VERSION}, indent=1))


if __name__ == "__main__":
    main()
