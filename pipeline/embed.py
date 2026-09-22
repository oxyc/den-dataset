#!/usr/bin/env python3
"""The EMBED pass — one document per title, through den-embed, into two append-only stores.

The document is composed in `pipeline/compose.py` (Wikidata's genres, the creators, our own tags and the
capped Wikipedia plot) and embedded by the service `DEN_EMBED_URL` names. The vectors land in
`index/labels.jsonl` + `index/vectors.jsonl`, line for line, which `finalize` turns into the shipped blob.

**The composition is not a set of options.** `{"docShape":"lean","dropDirector":true,"plotCap":3500}` is what
the shipped index was built with, and anything else is a different vector space in the same file. The
values were not written down at the time and had to be recovered by re-embedding probe titles and comparing
bytes against the shipped rows — 12/12 exact at these settings, 10/12 with the director clause kept, 2/12
at cap 1500 — so they are constants here, not arguments. `index/composition.json` records them, and a
store that records something else is refused rather than appended to.

**Nothing is written until three things agree, and the composition above.** Every gate decides before any
file is touched — the records of the space, the embedder and the composition, the repair below, the rows —
because a run that cannot do any work has no business recording or repairing anything:

  * **the embedder** — `index/embedder.json` holds what built the store, and a service that is not the
    same embedder (model, dims, epoch, token cap — never the build string) is refused: appending would mix
    two generations into one corpus. A store with rows and no record is refused too, because adopting
    today's service as its identity writes a guess down as a fact;
  * **the space** — the known-answer canary (`lib/denembed.py`) is run against the service, every run, and
    a single differing byte refuses. Its verdict is recorded as `index/embedding-space.json` and the
    embedding loop takes it as an argument: there is no path to a vector that did not pass through it. The
    stage used to check for that file AFTER the Swift binary ran, because a binary built before the gate
    embedded happily and recorded nothing; the gate and the writer are one process now, so the check is
    structural instead of after the fact;
  * **the fit** — den-embed truncates at `max_tokens` server-side and says nothing, so a plot cap the
    service would silently cut is refused. Plot is ~87% of the document; the cut would be most of it.

**Resume is the property that makes a 12-hour run interruptible.** A run skips every key already in the
stores, and first repairs a kill that landed between a label line and its vector line — or mid-line, which
leaves both files the same LENGTH with the last vector belonging to no title. The repair keeps the longest
prefix whose lines parse and name the same title, and refuses to drop more than an in-flight chunk could
have torn: past that it is a corrupt store, and truncating it would destroy hours of embedding.

`--dump-docs PATH` composes and embeds nothing, so the documents can travel to the den-embed that serves
live queries rather than the vectors coming back from a different one. It needs no service.
"""
import json
import os
import sys
import time

from . import artifacts, compose, finalize, jsonbytes
from .contract import REPO, StageError, bind
from lib import cache as caching
from lib import denembed, http

NAME = "embed"
PRODUCER = "pipeline/embed.py"
HOW = "./den stage embed --out-dir <dir>"
#: Writes into the out-dir and nowhere else. Expensive to repeat — a full corpus is ~12 hours — but
#: repeatable and resumable, which is a different thing from irreversible.
PUBLISHES = False
#: den-embed is self-hosted, so a re-embed costs hours and no money.
SPENDS = False

#: `labels-t02.json` is `--labels` here and `--vector-labels` to the store writer; it supplies each
#: title's tags. The file keeps one name and each reader keeps its own word for it.
INPUTS = (
    artifacts.VECTOR_LABELS.called("labels"),
    artifacts.ENRICHED.called("enriched_dir"),
    artifacts.DOC_FACTS,
)

#: The two append-only stores, and the three records that say which space and which document shape the
#: rows in them belong to.
OUTPUTS = (
    artifacts.EMBED_LABELS,
    artifacts.EMBED_VECTORS,
    artifacts.COMPOSITION,
    artifacts.EMBEDDER,
    artifacts.EMBEDDING_SPACE,
)

#: The document shape the shipped index was built with. The cap is the one value not pinned by the bytes:
#: `capped_plot` snaps back to the last ". ", so each probe title is insensitive across an interval and
#: intersecting them gives [3479..3534]. 3500 is the round number in that window.
SHIPPED_COMPOSITION = {"docShape": "lean", "dropDirector": True, "plotCap": 3500}

#: den-embed's per-request budget: it accepts a request while `sum(min(actual_tokens, max_tokens))` is
#: within this, and answers 413 otherwise.
TOKEN_BUDGET = 8192
#: What the serving box runs, permanently, and what the canary's answers were recorded at.
MAX_TOKENS = 1024
#: Documents per request. 8192/1024 is 8; 7 leaves the margin. At 15 — the Swift's own default — every
#: request is a 413 once the cap is 1024, and a 413 is not retried, so the run dies on its first flush.
CHUNK = 7
#: The composed document's non-plot half, in characters, for the fit check.
FACTS_AND_TAGS = 500
#: A tear loses at most the in-flight chunk. Anything larger is a corrupt store, not an interrupted write.
MAX_TEAR_REPAIR = 1000

BOUND = {bind(entry).name: bind(entry) for entry in INPUTS}


def say(message):
    print(f"  {message}", file=sys.stderr)


def canary_path():
    """This checkout's known answers, wherever the stage was invoked from. `DEN_EMBED_CANARY` names
    another copy — how the box runs it against a mounted one."""
    return os.environ.get("DEN_EMBED_CANARY") or os.path.join(REPO, "data", "embed-canary.json")


def read_json(path):
    """A record the store keeps about itself, or None when absent or unreadable — as the Swift read it."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_pretty(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    caching.write_atomically(path, jsonbytes.pretty(value).encode("utf-8"))


def has_rows(path):
    return os.path.exists(path) and bool(finalize.lines(path))


def gate_embedder(ctx, url):
    """The service against the store, the space and the cap. Decides and writes nothing. Returns the
    verified space, and the identity to record — None when the store already records one, which is kept
    as written: a newer runtime string is the same embedder, not a fact to overwrite the build with."""
    path = ctx.path(artifacts.EMBEDDER)
    now = denembed.identity(url)
    previous = denembed.stored_identity(read_json(path))
    if previous is not None and not denembed.same_embedder(previous, now):
        # Differing ONLY in the epoch means the file predates that field, and "restore the previous
        # service" would be impossible advice naming the same build on both sides.
        epoch_only = (previous["vectorEpoch"] == 0 and now["vectorEpoch"] > 0 and
                      all(previous[k] == now[k] for k in ("model", "dims", "maxTokens")))
        raise StageError(
            f"embed: this store was embedded by {denembed.label(previous)} but den-embed now reports "
            f"{denembed.label(now)} — appending would mix two embedders into one corpus. " +
            (f"These differ only in the epoch, so {path} predates that field rather than recording a "
             f"different embedder: if this service did build the store, add a vectorEpoch of "
             f"{now['vectorEpoch']} to that file." if epoch_only else
             "Either restore the previous service, or start a fresh --out-dir and re-embed."))
    if previous is None and has_rows(ctx.path(artifacts.EMBED_LABELS)):
        raise StageError(
            f"embed: {ctx.out_dir} holds an existing store but no {path}, so what embedded it is unknown and "
            f"appending {denembed.label(now)} may mix two embedders. Write that file with the identity that "
            f'built it — a corpus from before the Rust rewrite is {{"model":"bge-m3","dims":1024,'
            f'"runtime":"pre-3.0.0","maxTokens":0}} — or start a fresh --out-dir.')
    budget = now["maxTokens"] * 4
    cap = SHIPPED_COMPOSITION["plotCap"]
    if now["maxTokens"] > 0 and cap + FACTS_AND_TAGS > budget:
        raise StageError(
            f"embed: a plot cap of {cap} composes documents of roughly {cap + FACTS_AND_TAGS} chars, but "
            f"{denembed.label(now)} truncates at {now['maxTokens']} tokens (~{budget} chars) and would cut "
            f"them silently. Raise MAX_TOKENS on the service to {MAX_TOKENS} — its ceiling, above which it "
            f"exceeds the memory the container is given.")
    try:
        stamp = denembed.verify(canary_path(), url, say)
    except denembed.CanaryFailure as failure:
        raise StageError(f"embed: {failure}") from None
    say(f"embedder: {denembed.label(now)}")
    return stamp, (now if previous is None else None)


def gate_composition(ctx):
    """The same guard for how the document is composed, which the embedder identity cannot see. Decides
    and writes nothing; True when the store records no composition yet and this run must write one."""
    path = ctx.path(artifacts.COMPOSITION)
    recorded = read_json(path)
    valid = (isinstance(recorded, dict) and isinstance(recorded.get("docShape"), str)
             and isinstance(recorded.get("dropDirector"), bool) and isinstance(recorded.get("plotCap"), int))
    if valid:
        got = {key: recorded[key] for key in SHIPPED_COMPOSITION}
        if got != SHIPPED_COMPOSITION:
            raise StageError(
                f"embed: this store's documents were composed as {got} and this stage composes "
                f"{SHIPPED_COMPOSITION} — appending would put two document shapes in one vector space, which "
                f"no similarity score can separate afterwards. Start a fresh --out-dir.")
        return False
    if has_rows(ctx.path(artifacts.EMBED_LABELS)):
        # The suggested values are out-t02-cc0b's and nobody else's: several other stores have rows and no
        # record and were composed differently, so the message has to say whose they are.
        raise StageError(
            f"embed: {ctx.out_dir} holds rows but no {path}, so how its documents were composed is unknown. "
            f"If this store is out-t02-cc0b (the shipped one) its composition is "
            f'{{"docShape":"lean","dropDirector":true,"plotCap":3500}} — write that file. For any OTHER '
            f"store these values are wrong: recover them (docs/OPERATE.md, \"Recovering a store's "
            f"composition\"), or start a fresh --out-dir.")
    return True


def aligned(label_line, vector_line):
    try:
        rec = json.loads(label_line)
        finalize.parse_record(rec, "")
        tmdb_id, _ = finalize.vector_row(vector_line, "")
    except (ValueError, StageError):
        return False
    return rec["tmdbId"] == tmdb_id


def reconcile(labels_path, vectors_path):
    """Repair a torn pair of stores, or refuse one that is not merely torn."""
    if not (os.path.exists(labels_path) and os.path.exists(vectors_path)):
        return
    labels, vectors = finalize.lines(labels_path), finalize.lines(vectors_path)
    kept = 0
    while kept < min(len(labels), len(vectors)) and aligned(labels[kept], vectors[kept]):
        kept += 1
    dropping = max(len(labels), len(vectors)) - kept
    if dropping <= 0:
        return
    if dropping > MAX_TEAR_REPAIR:
        raise StageError(f"embed: the store diverges at line {kept + 1}; repairing that would discard "
                         f"{dropping} rows, which is a corrupt store rather than an interrupted write. Nothing "
                         f"was changed — inspect line {kept + 1} of {labels_path} and {vectors_path}.")
    say(f"repaired an interrupted write: dropped {dropping} unpaired row(s), store now {kept}")
    for lines, path in ((labels, labels_path), (vectors, vectors_path)):
        if len(lines) != kept:
            caching.write_atomically(path, "".join(line + "\n" for line in lines[:kept]).encode("utf-8"))


def stored_keys(path):
    keys = set()
    if os.path.exists(path):
        for line in finalize.lines(path):
            try:
                row = json.loads(line)
                keys.add(f"{row['mediaType']}:{row['tmdbId']}")
            except (ValueError, KeyError, TypeError):
                continue
    return keys


def inputs(ctx):
    """The labels per key, the doc facts, and the enriched dir — every input, required."""
    labels_path = ctx.require(BOUND[artifacts.VECTOR_LABELS.name].artifact)
    with open(labels_path, encoding="utf-8") as handle:
        records = json.load(handle).get("records") or []
    labels = {}
    for n, raw in enumerate(records):
        record = finalize.parse_record(raw, f"{labels_path} record {n}")
        labels[f"{record['mediaType']}:{record['tmdbId']}"] = record
    enriched = ctx.require(BOUND[artifacts.ENRICHED.name].artifact)
    # Without doc facts the document is not the CC0 shape at all, so the file is required, not optional.
    with open(ctx.require(artifacts.DOC_FACTS), encoding="utf-8") as handle:
        doc_facts = json.load(handle)
    return labels, enriched, doc_facts


def embed_into(stores, url, stamp, chunk, pause_ms, flushes):
    """The writer. It takes the verified space as an argument, so no code path reaches a vector without
    the canary having passed in this process."""
    if not stamp or not stamp.get("spaceId"):
        raise StageError("embed: no verified embedding space — the canary did not run, so nothing may be "
                         "written.")
    labels_handle, vectors_handle = stores
    written = 0
    for buffer in flushes:
        vectors = denembed.embed_many(url, [doc for _, _, doc in buffer])
        if len(vectors) != len(buffer):
            raise StageError(f"embed: den-embed returned {len(vectors)} vectors for {len(buffer)} documents")
        for (_, record, _), vector in zip(buffer, vectors):
            # Label then vector, per title: a kill between the two is the tear `reconcile` repairs.
            labels_handle.write(jsonbytes.compact(record) + "\n")
            labels_handle.flush()
            vectors_handle.write(jsonbytes.compact({"tmdbId": record["tmdbId"], "v": vector}) + "\n")
            vectors_handle.flush()
            written += 1
        if pause_ms:
            time.sleep(pause_ms / 1000)
    return written


def chunks(items, size, limit):
    """`items` in groups of `size`, stopping after `limit` in total. 0 means zero."""
    buffer, taken = [], 0
    for item in items:
        if limit is not None and taken >= limit:
            break
        buffer.append(item)
        taken += 1
        if len(buffer) >= size:
            yield buffer
            buffer = []
    if buffer:
        yield buffer


def run(ctx):
    """Embed what the stores do not hold yet. Returns the vectors store — or, with `--dump-docs`, the
    documents file."""
    os.makedirs(os.path.abspath(ctx.out_dir), exist_ok=True)
    labels, enriched, doc_facts = inputs(ctx)
    url = denembed.base_url()
    stamp = identity = None
    if not ctx.dump_docs:
        try:
            stamp, identity = gate_embedder(ctx, url)
        except (http.HTTPError, ValueError, KeyError) as unreachable:
            raise StageError(f"embed: den-embed at {url} could not be asked ({unreachable})") from None
    first_composition = gate_composition(ctx)
    say(f"composition: {SHIPPED_COMPOSITION}")

    # Every gate has decided. Only now is anything written — a record, a repair, a row.
    if stamp is not None:
        write_pretty(ctx.path(artifacts.EMBEDDING_SPACE), stamp)
    if identity is not None:
        write_pretty(ctx.path(artifacts.EMBEDDER), identity)
    if first_composition:
        write_pretty(ctx.path(artifacts.COMPOSITION), SHIPPED_COMPOSITION)

    labels_store, vectors_store = ctx.path(artifacts.EMBED_LABELS), ctx.path(artifacts.EMBED_VECTORS)
    reconcile(labels_store, vectors_store)
    done = stored_keys(labels_store)
    tally = {}
    docs = compose.documents(enriched, labels, doc_facts, done, SHIPPED_COMPOSITION["plotCap"], tally)
    groups = chunks(docs, CHUNK, ctx.limit)
    if ctx.dump_docs:
        written = 0
        os.makedirs(os.path.dirname(os.path.abspath(ctx.dump_docs)), exist_ok=True)
        with open(ctx.dump_docs, "a", encoding="utf-8") as handle:
            for buffer in groups:
                handle.write("".join(jsonbytes.compact({"key": k, "doc": d}) + "\n" for k, _, d in buffer))
                written += len(buffer)
        made = ctx.dump_docs
    else:
        os.makedirs(os.path.dirname(os.path.abspath(labels_store)), exist_ok=True)
        with open(labels_store, "a", encoding="utf-8") as lh, open(vectors_store, "a", encoding="utf-8") as vh:
            try:
                written = embed_into((lh, vh), url, stamp, CHUNK, ctx.pause_ms, groups)
            except http.HTTPError as refused:
                raise StageError(f"embed: den-embed refused a request ({refused}). Everything before it is "
                                 f"in the stores; re-run to continue from there.") from None
        made = vectors_store
    # One JSON line on stdout, which `scripts/embed-corpus-run.sh` reads `written` from to know when the
    # corpus is done.
    print(json.dumps({"written": written, "skipped": len(done), "missingLabel": tally.get("missing", 0),
                      "store": labels_store}))
    return made
