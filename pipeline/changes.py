#!/usr/bin/env python3
"""The CHANGE SET: which titles moved since the dataset that is live, so a daily run redoes only those.

    ./den stage changes --out-dir DIR [--revisit-weeks N]

A full pass re-reads, re-labels and re-embeds ~47,600 titles to find out that almost none of them changed
(oxyc/den-dataset#5). The daily job (oxyc/den-dataset#27) runs every stage after this one over the titles
this names, and reuses what is already built for the rest.

**What the live dataset was built from is already recorded.** The enriched batches are append-only — a batch
is never written over (`enrich.run`, `refresh.write_batch`), and a re-fetch lands in a NEW batch that every
reader resolves newest-first — and the publish stamps the highest batch it could see into the manifest as
`maxBatchId` (`pipeline/manifest_counts.py --stamp`). So the state the live dataset saw is the newest record
per title among the batches up to that number, and the state now is the newest record per title among all
of them. The plan is the difference. Nothing is snapshotted and nothing accumulates: the plan is derived
from the batches and the live manifest every run, so a run that died leaves nothing to reconcile.

The boundary is sound for a PUBLISHED manifest, not merely plausible: the same publish runs the plot-vector
gate (`check_plot_invariants.py --store --max-batch-id`), which refuses a store that ships a title whose
plot is in those batches without its plot vector.

**What counts as changed**, per title, comparing the two newest records:

  * `plot`    — the Wikipedia plot text is different (compared by digest; see `fingerprint`);
  * `article` — it is read from a different article or another language's;
  * `gainedPlot` — it had no Wikipedia plot and has one now;
  * `item`    — `lib/wikidata.resolve` answers for it from a different Wikidata item.

A title whose article revision moved and whose plot did not is `revised` and counted, not listed: a
`fetch --refresh` records the new revision and nothing downstream has anything to redo — the refresh's own
rule. A revision or item that was never recorded (the Enterprise path names no revision; batches before
`wikidataItem` existed name no item) is unknown, not changed: an unknown compared with anything proves
nothing, and the refresh re-fetches exactly those titles until they are known.

**Withdrawn** is a title that had a plot and has none now. Its classify and critique rows were read from an
article it no longer has, so `pipeline/consolidate_corpus.py withdraw --keys changes/withdrawn.txt` stops
the join shipping them. The title keeps its facts, its genres & moods and its row, as the join's
tombstones already work.

**The weekly slice** (oxyc/den-dataset#5). `--revisit-weeks N` adds every title whose key hashes into this
week's slice of N, so the whole corpus is revisited once every N weeks with no state kept about what was
visited: the slice is a function of the key and the ISO week. It names titles whose Wikidata statements a
revision survey cannot see move — a new award, a corrected director — for the facts and doc-facts stages.
It never names a title for a paid pass: nothing about its plot changed.

**With no live manifest there is no baseline**, and every title is `added` — a first generation, which is
what a fresh out-dir is. The plan says so (`"baseline": null`) and the change report prints it. The daily
job always has one: it downloads `dataset.meta.json` from the `data-latest` release into `published/`.

What it writes, under `changes/`, rewritten whole every run:

  * `plan.json`     — the baseline, the counts, and every listed title with its reasons;
  * `keys.txt`      — added and changed: the titles every later stage runs for;
  * `withdrawn.txt` — what `consolidate_corpus.py withdraw` takes;
  * `revisit.txt`   — this week's slice, with `--revisit-weeks` only.

Keys only. The plan carries no text: an old batch's `overview` is TMDB's prose when `hasWikiPlot` is false,
so the digest is taken of grounded records only, and never leaves this process.
"""
import datetime
import hashlib
import json
import os

from lib import cache as caching

from . import artifacts, enrich
from .contract import StageError, how_to_build

NAME = "changes"
PRODUCER = "pipeline/changes.py"
HOW = "./den stage changes --out-dir <dir>"
#: Local files in, local files out.
PUBLISHES = False
SPENDS = False

#: The batches, and the live manifest that says which of them the live dataset saw. The manifest is
#: optional: without it every title is new, which is a first generation.
INPUTS = (artifacts.ENRICHED, artifacts.PUBLISHED_META)
OUTPUTS = (artifacts.CHANGES,)

PLOT, ARTICLE, GAINED, ITEM = "plot", "article", "gainedPlot", "item"
LOST = "lostPlot"


def order(key):
    """Movies before series, then by number — the store's row order, so a list reads the way it ships."""
    media, tmdb_id = key.split(":", 1)
    return media != "movie", int(tmdb_id)


def fingerprint(record):
    """What about a title a later stage reads: whether it has a plot, from which article, the plot's digest,
    the revision it was read at, and the Wikidata item answering for it.

    The digest is of the plot only when `hasWikiPlot` is true. Otherwise `overview` may be TMDB's prose in
    a batch written before oxyc/den-dataset#53, and nothing here has any business reading it.
    """
    grounded = bool(record.get("hasWikiPlot"))
    text = record.get("overview") if grounded else None
    revision = record.get("plotRevId")
    return {
        "plot": grounded,
        "article": record.get("plotArticle") if grounded else None,
        "language": (record.get("plotLanguage") or "en") if grounded else None,
        "revision": revision if isinstance(revision, int) and not isinstance(revision, bool) else None,
        "text": hashlib.sha256(text.encode("utf-8")).hexdigest() if isinstance(text, str) else None,
        "item": record.get("wikidataItem") or None,
    }


def snapshot(enriched_dir, through=None):
    """`{key: fingerprint}` — each title's newest record among the batches numbered up to `through` (all of
    them when None). Newest-wins is the rule every batch reader applies (`refresh.latest`)."""
    out = {}
    for number, name in enrich.batches(enriched_dir):
        if through is not None and number > through:
            break
        with open(os.path.join(enriched_dir, name), encoding="utf-8") as handle:
            for record in json.load(handle):
                out[enrich.key(record["mediaType"], record["tmdbId"])] = fingerprint(record)
    return out


def reasons(before, now):
    """Why `now` is a different title to a later stage than `before`, or `[]`."""
    found = []
    if now["plot"] and not before["plot"]:
        found.append(GAINED)
    elif now["plot"]:
        if (now["article"], now["language"]) != (before["article"], before["language"]):
            found.append(ARTICLE)
        if now["text"] != before["text"]:
            found.append(PLOT)
    if before["item"] and now["item"] and before["item"] != now["item"]:
        found.append(ITEM)
    return found


def diff(before, now):
    """`(added, changed, withdrawn, revised, unchanged)`. `before` is read from a prefix of the batches `now`
    is read from, so every title it holds, `now` holds."""
    added = sorted((key for key in now if key not in before), key=order)
    changed, withdrawn, revised, unchanged = {}, {}, 0, 0
    for key in sorted(before, key=order):
        was, is_now = before[key], now[key]
        if was["plot"] and not is_now["plot"]:
            withdrawn[key] = LOST
            continue
        why = reasons(was, is_now)
        if why:
            changed[key] = why
        elif was["revision"] is not None and is_now["revision"] is not None \
                and was["revision"] != is_now["revision"]:
            revised += 1
        else:
            unchanged += 1
    return added, changed, withdrawn, revised, unchanged


def week(today):
    """Weeks since 1970-01-05, a Monday — a count that rolls over at no year boundary, unlike ISO weeks."""
    return (today - datetime.date(1970, 1, 5)).days // 7


def in_slice(key, weeks, this_week):
    """Whether `key` is in this week's slice of a cycle `weeks` long. Every key is in exactly one slice."""
    bucket = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big") % weeks
    return bucket == this_week % weeks


def baseline(ctx, highest):
    """`(datasetVersion, maxBatchId)` of the live dataset, or None when no live manifest was given."""
    path = ctx.require(artifacts.PUBLISHED_META)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError) as error:
        raise StageError(f"changes: {path} is not a readable manifest ({error})") from None
    through = meta.get("maxBatchId") if isinstance(meta, dict) else None
    if not isinstance(through, int) or isinstance(through, bool) or through < 1:
        raise StageError(f"changes: {path} records no maxBatchId, so which batches the live dataset was built "
                         f"from cannot be known. Every publish stamps it (pipeline/manifest_counts.py "
                         f"--stamp); a manifest without one is not a published one.")
    if through > highest:
        raise StageError(f"changes: the live dataset was built from batches up to {through} and this out-dir "
                         f"holds batches up to {highest}. It is not the out-dir that built it.")
    return meta.get("datasetVersion"), through


def write_list(path, keys):
    caching.write_atomically(path, "".join(f"{key}\n" for key in keys).encode("utf-8"))


def run(ctx, now=None):
    """Plan, and write the plan. Returns its directory."""
    enriched = ctx.require(artifacts.ENRICHED)
    numbers = [number for number, _name in enrich.batches(enriched)]
    if not numbers:
        raise StageError(f"changes: {enriched} holds no batch. Build it with: {how_to_build(artifacts.ENRICHED)}")
    live = baseline(ctx, numbers[-1])
    current = snapshot(enriched)
    before = snapshot(enriched, through=live[1]) if live else {}
    added, changed, withdrawn, revised, unchanged = diff(before, current)

    revisit = []
    today = (now or datetime.datetime.now(datetime.timezone.utc)).date()
    if ctx.revisit_weeks:
        if ctx.revisit_weeks < 1:
            raise StageError(f"changes: --revisit-weeks {ctx.revisit_weeks} is not a cycle length")
        listed = set(added) | set(changed) | set(withdrawn)
        revisit = [key for key in sorted(current, key=order)
                   if key not in listed and in_slice(key, ctx.revisit_weeks, week(today))]

    keys = sorted(set(added) | set(changed), key=order)
    plan = {
        "baseline": {"datasetVersion": live[0], "maxBatchId": live[1]} if live else None,
        "throughBatch": numbers[-1],
        "counts": {"titles": len(current), "added": len(added), "changed": len(changed),
                   "withdrawn": len(withdrawn), "revised": revised, "unchanged": unchanged,
                   "keys": len(keys), "revisit": len(revisit)},
        "revisit": ({"weeks": ctx.revisit_weeks, "slice": week(today) % ctx.revisit_weeks,
                     "date": today.isoformat()} if ctx.revisit_weeks else None),
        "added": added,
        "changed": changed,
        "withdrawn": withdrawn,
    }
    directory = ctx.path(artifacts.CHANGES)
    os.makedirs(directory, exist_ok=True)
    write_list(os.path.join(directory, "keys.txt"), keys)
    write_list(os.path.join(directory, "withdrawn.txt"), list(withdrawn))
    stale = os.path.join(directory, "revisit.txt")
    if ctx.revisit_weeks:
        write_list(stale, revisit)
    elif os.path.exists(stale):
        os.unlink(stale)
    # Last, so a plan.json on disk always describes the lists beside it.
    caching.write_atomically(os.path.join(directory, "plan.json"),
                             (json.dumps(plan, indent=1) + "\n").encode("utf-8"))
    print(json.dumps(dict(plan["counts"], baseline=plan["baseline"]), sort_keys=True), flush=True)
    return (f"{directory} ({len(keys)} to run: {len(added)} added, {len(changed)} changed; "
            f"{len(withdrawn)} withdrawn; {len(revisit)} to revisit"
            f"{'; no live manifest, so every title is new' if not live else ''})")

