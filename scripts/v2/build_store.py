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

`--stamp-meta` also records WHAT IT READ — every input's path, sha256, size and mtime, as `storeInputs`
in the manifest. See `build_inputs`: the inputs stopped being published artifacts, so they stopped being
covered by the ownership guard, and this record is what `check-producers.py` holds them to.

## What it publishes, which is not everything it reads

Plot facets pass the FACETS-V2 publication gates before they reach a section — see `publishable`. The
corpus collects every answer with its full distribution; this file decides which of them the one
published artifact asserts. `--stamp-meta` records what each gate withheld, as `facetGates`.
"""
import argparse
import gzip
import hashlib
import json
import os
import struct
import sys
from datetime import date
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vector_blob  # noqa: E402  — beside this file; the blob layout, shared with the migration

FORMAT_VERSION = 1
MAGIC = b"DENSTOR1"
ENDIAN_CHECK = 0x01020304
HEADER_BYTES = 64
ENTRY_BYTES = 32
ALIGN = 8
DIMS = 1024
#: Above this many rows, an all-zero `votes` column is a missing `--enriched`, not a corpus of unknowns.
#: Below it, a synthetic fixture with no vote data is ordinary and says nothing.
VOTES_REQUIRED_ABOVE = 1000

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

# ---- the FACETS-V2 publication gates ---------------------------------------------------------------
#
# `scripts/v2/FACETS-V2.md` § "Publication gates": "The pilots support collection, not unconditional
# argmax publication." The corpus is the collection — it keeps every answer with its full distribution,
# so nothing here is unrecoverable and the store can be rebuilt with different thresholds in ~2 minutes.
# The store is the PUBLICATION, and these are the conditions the spec puts on it.
#
# Why here and not in den-atlas: the store carries the argmax and one confidence byte per axis. The
# runner-up probability, which clause 4's margin needs, and `validity`'s own distribution, which clause 2
# needs, are NOT in the store and never were. A reader physically cannot apply this gate. Putting it in
# the reader would also leave the one published artifact asserting things the spec says are not
# publishable, which is the defect this exists to close.

#: Clause 2 — "Publish plot facets only when `validity=correct-screen-work` has probability at least
#: 0.80." The PROBABILITY, not the self-reported `confidence`: they are separate fields and differ.
VALIDITY_MIN = 0.80
#: Clause 4 — "require probability at least 0.70 and a top-minus-runner-up margin of at least 0.25".
FACET_PROB_MIN = 0.70
FACET_MARGIN_MIN = 0.25
#: Clause 4 — "Exclude `does-not-apply` and `ending=unknown`". `does-not-apply` is excluded on every
#: axis and is checked on its own below; this is the per-axis half. `unknown` is `ending`'s way of
#: saying a work has not ended yet — a fact about the corpus, not an ending a viewer can browse to, and
#: the largest published `ending` value in the live index at 8,858 titles, *Game of Thrones* included.
NEVER_PUBLISHED = {("ending", "unknown")}
#: Clause 3 — "Suppress plot facets for talk, variety, game, news, or reality programs without a bounded
#: narrative." That is `narrative_applicability`'s own value for such a programme.
NO_NARRATIVE = "non-narrative-program"
#: Clause 3, second sentence — "Suppress archetype additionally for documentaries, anthologies,
#: open-ended series, and multi-arc works", "from deterministic metadata/content type or a separate
#: typed applicability result, not from archetype's own no-answer probability". `narrative_applicability`
#: IS that separate typed result, and its remaining value is the one archetype may be published for.
#: The spec's own counter-example — the documentary `Killer Inside: The Mind of Aaron Hernandez` at
#: `downfall` 0.76 — is exactly a row this excludes and no confidence threshold would have.
ARCHETYPE_REQUIRES = "bounded-fictional-narrative"
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


#: Every argument that names a file or directory this READS. The record below is built from it, and
#: `check-producers.py` maps each entry to the producer that builds it — so adding an input here is what
#: makes the new input owned and checked. `test_build_store.py` asserts this covers the parser's inputs
#: and `test_check_producers.py` asserts every one of them has a producer.
INPUT_ARGS = ("corpus", "entities", "facts", "metadata", "vectors", "vector_labels",
              "premise_vectors", "premise_labels", "enriched")


def file_sha256(path):
    """A file's hash, read in blocks — the inputs run to 135 MB and there is no reason to hold one."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def input_digest(path):
    """`(sha256, bytes, mtime)` for one build input — a file, or the enriched batch DIRECTORY.

    A directory is digested over its LISTING: `"<name> <sha256>\\n"` per batch file, in batch-number
    order, hashed. That is a content hash for a thing with no single file, and it moves when any batch is
    added, removed or rewritten — which is exactly what the record has to notice. Batch-number order
    rather than `sorted()`, for the same reason `read_votes` uses it: `batch-99` sorts after `batch-177`
    lexicographically, so a lexicographic digest would depend on how many digits a batch id has.
    """
    if os.path.isdir(path):
        names = sorted((n for n in os.listdir(path)
                        if n.startswith("batch-") and n.endswith(".json")),
                       key=lambda n: int(n[len("batch-"):-len(".json")]))
        listing = hashlib.sha256()
        total, newest = 0, 0
        for name in names:
            member = os.path.join(path, name)
            listing.update(f"{name} {file_sha256(member)}\n".encode())
            total += os.path.getsize(member)
            newest = max(newest, int(os.path.getmtime(member)))
        return listing.hexdigest(), total, newest
    return file_sha256(path), os.path.getsize(path), int(os.path.getmtime(path))


def build_inputs(args):
    """What this build READ: path, hash, size and mtime for every input argument.

    The store's inputs stopped being published artifacts when `data-latest` went store-only
    (oxyc/den#113), and the ownership guard went with them: `check-producers.py` walks the keys the
    MANIFEST names, so once the manifest named only the store, a store built from a labels file its
    producer had outgrown published perfectly clean. Every guard passed and none of them was looking at
    the thing that was stale.

    Recording it here is what puts them back in reach. `--stamp-meta` writes this into the manifest, so
    the publisher can re-hash each input against the tree and ask `check-producers.py` about its
    producer — and the mtime is recorded rather than read, so the producer question is still answerable
    in a publish dir holding nothing but the store and the manifest.
    """
    out = []
    for arg in INPUT_ARGS:
        path = getattr(args, arg)
        if not path:
            continue
        sha, size, mtime = input_digest(path)
        out.append({"arg": arg, "path": path, "sha256": sha, "bytes": size, "mtime": mtime})
    return out


def votes_are_missing(votes):
    """The complaint when a `votes` column is entirely zero at corpus scale, else `None`.

    A zero vote count is a real answer for an obscure title; a whole COLUMN of zeros is not. It is what
    an omitted `--enriched` produces, and `sec.put("votes", …, expect=n)` cannot see it — a row-count
    assert is satisfied by 47,618 zeros. atlas orders every browse row by `ln(votes)`, so the symptom is
    a corpus that sorts by tmdbId: *La Job* (tv:5) beside *Game of Thrones*, which is
    oxyc/den-dataset#22 one level up from the blob it was first found in.

    The store records its inputs now, but an input never PASSED is recorded as nothing — `store_inputs`
    skips a falsy path — so the ownership guard cannot see this one either. It is checked here, where
    the column is in hand.

    Bounded to real-corpus scale so the synthetic fixtures (here and in den-spec) need no flag declaring
    they have no vote data. Below the bound an all-zero column says nothing; above it, it is a missing
    argument.
    """
    if len(votes) > VOTES_REQUIRED_ABOVE and not any(votes):
        return (f"votes: all {len(votes)} rows are zero — every browse row would sort by tmdbId. Pass "
                f"--enriched so the vote counts are read, or say why a corpus this size has none.")
    return None


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


def row_applicability(row):
    """`(validity probability, narrative_applicability choice)` — the two facts the gates judge against.

    Both come from the `applicability` block, which the delta pass answers for every record. A row that
    has no block at all reads as probability 0.0, which fails the validity clause and so publishes no
    facets — the same answer as an explicit "this is not the right work", and the safe one.
    """
    applic = row.get("applicability") or {}
    validity = applic.get("validity")
    probability = 0.0
    if isinstance(validity, dict):
        probability = float((validity.get("probabilities") or {}).get("correct-screen-work") or 0.0)
    narrative = applic.get("narrative_applicability")
    return probability, (narrative.get("choice") if isinstance(narrative, dict) else None)


def publishable(axis, facet, validity_probability, narrative):
    """Whether one axis of one title may be PUBLISHED, and if not, which clause refused it.

    Returns `(bool, reason)`. The clauses are FACETS-V2 § "Publication gates" 2-4, in the order written
    there; the first failure is the reason, so the counts this produces partition the corpus.

    Clause 1 — provenance — is not checked here. It is the bundle auditor's (`audit_combined_bundle.py`),
    which has the manifests, and a record only reaches the corpus by passing it.

    The margin is computed against the WHOLE distribution, `does-not-apply` included. Those values may
    not be published, but they are still hypotheses the model weighed, and dropping them from the
    comparison would inflate every margin by whatever mass sat on them.
    """
    if not isinstance(facet, dict):
        return False, "absent"
    choice = facet.get("choice")
    if not choice:
        return False, "absent"
    if choice == "does-not-apply":
        return False, "does-not-apply"
    if (axis, choice) in NEVER_PUBLISHED:
        return False, f"{axis}={choice}"
    if validity_probability < VALIDITY_MIN:
        return False, "validity<0.80"
    if narrative == NO_NARRATIVE:
        return False, "non-narrative-program"
    if axis == "archetype" and narrative != ARCHETYPE_REQUIRES:
        return False, f"archetype not-bounded ({narrative})"
    probabilities = facet.get("probabilities")
    if not isinstance(probabilities, dict) or choice not in probabilities:
        # The distribution is what clause 4 is written against. A choice without one cannot be judged,
        # and publishing it unjudged is the thing this function exists to stop.
        return False, "no distribution"
    top = float(probabilities[choice] or 0.0)
    if top < FACET_PROB_MIN:
        return False, "p<0.70"
    ordered = sorted((float(v or 0.0) for v in probabilities.values()), reverse=True)
    runner_up = ordered[1] if len(ordered) > 1 else 0.0
    if top - runner_up < FACET_MARGIN_MIN:
        return False, "margin<0.25"
    return True, None


def title_labels(row):
    """The labelling a title got, from EITHER pass — `labels` (plot) or `premiseLabels`.

    Two passes label a title and only one of them was ever read here. 3 titles of 47,618 were labelled
    from their premise and never from a plot, so the store answered no primary genre, no subgenres and no
    moods for them while the legacy blobs answered all three — *Father and Sons*, *Two Sons of Ringo* and
    *Sítio do Picapau Amarelo*. They sit in the premise index, which is exactly where a reader asks.

    The plot record wins WHOLE where both passes answered, rather than field by field: within one pass the
    fields are coherent — an empty `subgenres` is the pass saying "none", not a gap — so filling one pass's
    empty list from the other's would emit a combination neither pass produced. It costs nothing on this
    corpus: all 44,528 titles both passes labelled agree byte-identically on all four fields, so this rule
    and any other give the same bytes for them, and the union is a pure addition of the 3.

    That agreement is a property of TODAY'S corpus, not a guarantee, so `disagreement()` below counts it
    and the build refuses rather than quietly choosing. A repass where the passes diverge is a decision
    about which labelling ships, and it should be made by someone rather than by the order of an `or`.
    """
    return (row.get("labels") or row.get("premiseLabels")) or {}


#: The fields a pass answers — the whole of what `title_labels` picks between.
LABEL_FIELDS = ("primaryGenre", "subgenres", "moods", "animated")


def disagreement(row):
    """The fields the two passes answer differently for one title, or `()` when they agree or only one
    answered.

    `title_labels` silently prefers the plot record, so this is the only thing that can see a divergence
    at all."""
    plot, premise = row.get("labels"), row.get("premiseLabels")
    if not plot or not premise:
        return ()
    return tuple(f for f in LABEL_FIELDS if plot.get(f) != premise.get(f))


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


def read_vectors(path, declared, what):
    """`(blob, base, row_of_key)` — the file, where row 0 starts, and which row each title owns.

    The row order used to come from `what`: a separate `labels-*.json` whose record order the blob was
    assumed to match. That assumption could not be checked, only relied on — regenerate the labels file
    with a different record order and every vector moves onto the wrong title, in a file that still loads
    and still returns real numbers for everything.

    A `DENVEC02` blob names its own rows, so the join is by key and the ordering assumption is gone. What
    replaces it is a check that can actually fail: the blob's key set must equal the key set `what`
    declares. A blob and a labels file from different generations now disagree loudly here instead of
    shifting the corpus silently.
    """
    count, dims, keys, blob, base = vector_blob.read(path)
    if dims != DIMS:
        sys.exit(f"{path}: header says {dims} dims, this store stores {DIMS}")
    declared = set(declared)
    found = set(keys)
    if found != declared:
        missing, extra = sorted(declared - found)[:3], sorted(found - declared)[:3]
        sys.exit(f"{path}: {count} vectors keyed for {len(found)} titles, but {what} declares "
                 f"{len(declared)} — {len(declared - found)} declared titles have no vector "
                 f"(e.g. {missing}) and {len(found - declared)} vectors name a title it does not "
                 f"(e.g. {extra}). The blob and its labels file must be the same generation.")
    return blob, base, {k: i for i, k in enumerate(keys)}


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


def build_parser():
    """The argument list, so a test can hold it against `INPUT_ARGS` — an input the parser accepts and
    the record does not know about is an input nothing checks."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--entities", required=True)
    ap.add_argument("--facts", required=True,
                    help="facts-<ver>.json — for genreMap, and to assert the row count")
    ap.add_argument("--metadata", required=True,
                    help="metadata-<ver>.json — the cards: title, posterPath, year")
    ap.add_argument("--vectors", required=True, help="vectors-bge-m3.bin (DENVEC02: it names its own rows)")
    ap.add_argument("--vector-labels", required=True,
                    help="labels-t02.json — the PLOT pass's key set. No longer the vectors' row order: "
                         "the blob carries its own keys, and this is what that key column is checked "
                         "against. Still a build input, for the two cross-checks only an independent "
                         "record of the pass can make — that the blob covers exactly the titles the pass "
                         "labelled, and that the corpus's `labels` field covers them too.")
    ap.add_argument("--premise-vectors", help="vectors-premise.bin (DENVEC02)")
    ap.add_argument("--premise-labels", required=True,
                    help="labels-premise.json — the PREMISE pass's key set, checked the same way. The "
                         "premise LABELS themselves come from the corpus `premiseLabels` field, which "
                         "consolidate_corpus.py joined from this same file. REQUIRED since the label "
                         "sections became the union of both passes: the corpus supplies the premise "
                         "labels either way, so without this the count assert compares a union against "
                         "the plot artifact alone and fails naming the wrong file.")
    ap.add_argument("--enriched",
                    help="the enriched/ batch directory, for TMDB vote counts. Without it the `votes` "
                         "section is all zeros and atlas cannot order a browse row by popularity.")
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

    # Which titles each PASS labelled. Not the vector row order — the blobs carry their own keys — but the
    # independent record the corpus's `labels` / `premiseLabels` fields are counted against below.
    print("reading the label artifacts …", file=sys.stderr)
    labels_source = labels_by_key(args.vector_labels, "vector labels")
    premise_labels_source = {}
    if args.premise_labels:
        premise_labels_source = labels_by_key(args.premise_labels, "premise labels")
    # The titles SOME pass labelled, from the two artifacts rather than from the corpus — the count the
    # store's label sections are asserted against below. Counting the corpus field would be asserting the
    # corpus against itself.
    labelled_keys = set(labels_source) | set(premise_labels_source)

    # ONE dictionary. Splitting a controlled vocabulary out to keep u16 ids saved 1.84 MB of 123 MB
    # and bought two bare integer id spaces with nothing in the format telling them apart: a reader
    # resolving a vocabulary id against the free-text table gets a wrong but perfectly valid string,
    # silently. That is the failure this whole store exists to make impossible. u32 everywhere.
    strings = Strings()
    noul_names, critique_names, technique_names = set(), set(), set()
    depicts_names, audience_names = set(), set()
    for key in keys:
        r = rows[key]
        labels = title_labels(r)
        strings.add(labels.get("primaryGenre"))
        for entry in (labels.get("subgenres") or []) + (labels.get("moods") or []):
            strings.add(entry.get("label") if isinstance(entry, dict) else entry)
        # Interned under the SAME gate the write loop applies, so a value no row publishes never
        # reaches the dictionary. Interning it anyway would leave a vocabulary in the store that no
        # row uses, which reads from the outside as a value with zero titles rather than as one the
        # gate withheld.
        validity_probability, narrative = row_applicability(r)
        for axis in FACET_AXES:
            v = (r.get("facets") or {}).get(axis)
            if publishable(axis, v, validity_probability, narrative)[0]:
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
    with_labels = with_plot_labels = with_premise = with_cards = 0
    divergent = []
    # What the publication gates published and withheld, per axis — reported below and stamped into the
    # manifest. A gate that drops 42% of `ending` must say so in a number, not leave the next reader to
    # discover it as a coverage surprise.
    facets_published = Counter()
    facets_withheld = defaultdict(Counter)

    for key in keys:
        r = rows[key]
        facts = r.get("facts") or {}
        # Counted off the SAME dict the sections below are written from, so the count proves the union
        # happened rather than agreeing with it by construction.
        labels = title_labels(r)
        if labels:
            with_labels += 1
        if r.get("labels"):
            with_plot_labels += 1
        if r.get("premiseLabels"):
            with_premise += 1
        fields = disagreement(r)
        if fields:
            divergent.append((key, fields))
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

        # The FACETS-V2 publication gates. A value that does not clear them is written as absent —
        # the SAME `U32_NONE` an axis the model declined gets, and deliberately so: both mean "this
        # store makes no claim here", which is the only thing a reader may conclude from either. The
        # store has one sentinel per axis and no room for a second without changing den-spec
        # `wire/store-v1.md` and both readers, and no reader has a use for the distinction — a row
        # must not list a title under `ending=tragic` because the model guessed tragic at 0.44.
        # The corpus keeps the full answer and the reason, so nothing is lost, only unpublished.
        validity_probability, narrative = row_applicability(r)
        for axis in FACET_AXES:
            v = (r.get("facets") or {}).get(axis)
            ok, reason = publishable(axis, v, validity_probability, narrative)
            if ok:
                facets_published[axis] += 1
                facet_v.append(strings.id(v["choice"]))
                facet_c.append(hundredths(v.get("confidence"), f"facet {axis} confidence", key))
            else:
                facets_withheld[axis][reason] += 1
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
    # A vote count of zero is a real answer for an obscure title; a whole COLUMN of zeros is not. It is
    # what `--enriched` omitted produces, and the row-count assert below cannot see it — `expect=n` is
    # satisfied by 47,618 zeros. atlas orders every browse row by `ln(votes)`, so the symptom would be a
    # corpus that sorts by tmdbId: *La Job* (tv:5) beside *Game of Thrones*, exactly the failure
    # oxyc/den-dataset#22 is about, one level up from the blob it was first seen in.
    #
    # The store records its inputs now, but an input never passed is recorded as nothing, so the
    # ownership guard cannot see this one either. Checked here, where the column is in hand.
    # Bounded to real-corpus scale, not to any store: a handful of synthetic titles legitimately have no
    # votes, and the fixtures that build them (here and in den-spec) should not have to carry a flag
    # saying so. At a thousand rows an all-zero column is not a corpus, it is a missing argument.
    votes_complaint = votes_are_missing(votes)
    if votes_complaint:
        sys.exit(votes_complaint)
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

    # Vectors, re-ordered from their own row order into ours by KEY. A row with no vector is zeroed.
    print("reading vectors …", file=sys.stderr)
    plot_blob, plot_base, plot_row = read_vectors(args.vectors, labels_source, args.vector_labels)
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
    premise_row = {}
    if args.premise_vectors and premise_labels_source:
        pblob, pbase, premise_row = read_vectors(args.premise_vectors, premise_labels_source,
                                                 args.premise_labels)
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
        ("plot labels", with_plot_labels, len(labels_source), args.vector_labels),
        # The union, against the union of the two artifacts' keys: a title EITHER pass labelled must carry
        # labels in the store. Checking only the plot side is how the premise-only titles went missing —
        # the plot count matched its artifact exactly while three titles held no labels at all.
        ("labelled titles", with_labels, len(labelled_keys),
         " ∪ ".join(p for p in (args.vector_labels, args.premise_labels) if p)),
        ("cards", with_cards, len(cards), args.metadata),
        # Against the BLOB's own key column now, not a sidecar's record count: every vector the file
        # carries must have landed on a row of the store.
        ("plot vectors", plot_hits, len(plot_row), args.vectors),
        ("premise vectors", premise_hits, len(premise_row), args.premise_vectors or args.premise_labels),
    ):
        if got != want:
            sys.exit(f"{what}: {got} in the store, {want} in {source} — they must agree exactly")
    # Where both passes labelled a title, `title_labels` takes the plot record whole. That is safe only
    # while the passes agree, which they do today for all 44,528 such titles. If a repass makes them
    # diverge, which labelling ships is a decision, and the `or` in `title_labels` would make it silently
    # by preferring whichever came first. Refuse instead, and name the titles.
    if divergent:
        shown = ", ".join(f"{k} ({'/'.join(f)})" for k, f in divergent[:5])
        sys.exit(f"the two labelling passes disagree on {len(divergent)} titles: {shown}"
                 f"{' …' if len(divergent) > 5 else ''} — `title_labels` would silently ship the plot "
                 f"record. Decide which pass wins for these and say so in the writer.")
    # Unresolved references, named and counted. A reference the entity table cannot resolve is dropped —
    # that is unavoidable when the table is short — but dropping it WITHOUT SAYING SO is how 2,680
    # franchise links disappeared into a section that looked perfectly well formed.
    if unresolved:
        print("unresolved entity references (dropped):", file=sys.stderr)
        for what, count in sorted(unresolved.items(), key=lambda kv: -kv[1]):
            print(f"  {what:14} {count}", file=sys.stderr)

    # The gates, per axis, before the summary — published, withheld, and which clause did the refusing.
    print("plot-facet publication gates (FACETS-V2 § Publication gates):", file=sys.stderr)
    for axis in FACET_AXES:
        kept = facets_published[axis]
        why = ", ".join(f"{r}={c}" for r, c in facets_withheld[axis].most_common())
        print(f"  {axis:12} {kept:6d}/{n} ({kept / n * 100:5.1f}%)  withheld: {why}", file=sys.stderr)

    gate_report = {
        "validityMin": VALIDITY_MIN, "probabilityMin": FACET_PROB_MIN, "marginMin": FACET_MARGIN_MIN,
        "axes": {axis: {"published": facets_published[axis],
                        "withheld": dict(sorted(facets_withheld[axis].items()))}
                 for axis in FACET_AXES},
    }

    print(json.dumps({"titles": n, "withLabels": with_labels, "withPlotLabels": with_plot_labels,
                      "withPremiseLabels": with_premise,
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
        with open(args.stamp_meta, "w") as fh:
            json.dump(meta, fh, indent=1)
            fh.write("\n")

    print(json.dumps({"out": args.out, "bytes": HEADER_BYTES + len(payload),
                      "formatVersion": FORMAT_VERSION, "stamped": bool(args.stamp_meta),
                      "inputs": len(inputs or ())}, indent=1))


if __name__ == "__main__":
    main()
