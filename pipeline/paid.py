#!/usr/bin/env python3
"""What the daily job has paid for and no publish carries yet, kept outside any one run (#187): never pay twice.

    pipeline/paid.py prune FILE    # after a publish: drop what the published dataset now carries

The job keeps nothing between runs, and a title is done only once a publish carries it. Before this, a run
that failed or was not published left its paid answers in an out-dir nobody kept, and the next run bought
them again (twice on 2026-09-30). The ledger is one file, `paid-state.json.gz`. The workflow downloads it
before a run and uploads it after, whether or not the run publishes, as an asset of the `paid-state` release
(`.github/workflows/daily.yml`; the `daily` concurrency group keeps one writer). It holds:

  * `shards` — every Jev shard a run bought (classify with critique, critique, genres & moods), laid back into
    the next out-dir before anything asks. The stages already skip what a shard in the out-dir answers:
    classify a title whose article and questions it holds (`classify.answered`), genres & moods a title any
    answers shard holds;
  * `files` — the franchise decisions, restored as the union of the ledger's and the out-dir's;
  * `answers` — per step, a title's answer with what it was bought for, for the fan picks and the premise
    tags, which ask through `lib/llm.py`. A new model or spec is the one way to pay for a title again;
  * `batches`, `intents` and `steps` — Batch jobs submitted and not yet collected, a submit whose response
    has not been seen, and each model step's state (`pipeline/model_steps.py`).

**The release is public, so the ledger carries nothing the repo keeps unpublished.** A Jev row's `title` and
`year` can be TMDB's (`pipeline/artifacts.py`: answer shards "are backed up, not published"), so they are
dropped when a shard is kept (`STRIPPED`); nothing that resumes a pass or joins the corpus reads them. What is
left is ids, digests, section headings and offsets, and model answers — what the published corpus and its
bundle already carry — and no prose: a franchise states shard, which holds article text, is never kept.

**What a publish carries leaves the ledger** (`prune`). Once a ready run has published, the dataset it
published holds every shard, decision and answer that run restored or bought, so the next run starts from the
bundle and keeping them would only let an old shard compete with a later re-classification. What a publish
does not carry stays: pending jobs, step state, the premise titles no model would tag, and for a title still
waiting for premise tags its kept answer and the classify shard its worklist is cut from.
"""
import gzip
import json
import os
import sys

if not __package__:
    # Run as a file by the workflow: the repo, not pipeline/, is the import root.
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from lib import cache as caching  # noqa: E402

from pipeline import artifacts  # noqa: E402
from pipeline.franchise_decisions import totals  # noqa: E402

SCHEMA = "paid-answers-v1"
#: The paid Jev shards a run writes into its out-dir, each beside its manifest.
SHARDS = (artifacts.COMBINED, artifacts.DELTA, artifacts.GENRES_MOODS_ANSWERS)
#: The franchise stage's durable decisions, merged on restore.
FILES = (artifacts.FRANCHISE_DECISIONS, artifacts.FRANCHISE_LINE_DECISIONS)
#: Row fields dropped when a shard is kept: they can be TMDB's, and nothing a kept shard is read for needs them.
STRIPPED = ("title", "year")


def empty():
    return {"schema": SCHEMA, "shards": {}, "files": {}, "answers": {}, "batches": [], "intents": [],
            "steps": {}}


def stripped(rows):
    """A shard's rows without `STRIPPED`, line for line; a row without them is kept as it was written."""
    out = []
    for line in rows.splitlines():
        if line.strip():
            row = json.loads(line)
            if any(field in row for field in STRIPPED):
                for field in STRIPPED:
                    row.pop(field, None)
                line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            out.append(line + "\n")
    return "".join(out)


class Ledger:
    def __init__(self, path):
        self.path = path
        self.data = empty()
        if path and os.path.exists(path):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                self.data = json.load(fh)
            if self.data.get("schema") != SCHEMA:
                raise ValueError(f"{path} is not a {SCHEMA} ledger")
            for name, value in empty().items():
                self.data.setdefault(name, value)
        #: Shards that were in the out-dir before the ledger was laid out and are not its own: an operator's
        #: whole-corpus shards, say. They are never taken into the ledger. None until `restore`.
        self.foreign = None

    def save(self):
        """Write the ledger atomically. A no-op without a path: an operator run keeps its own out-dir."""
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        body = json.dumps(self.data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        caching.write_atomically(self.path, gzip.compress(body, mtime=0))

    def answers(self, step):
        """The step's kept answers, `{title key: entry}`, mutable in place."""
        return self.data["answers"].setdefault(step, {})

    def pending_usd(self):
        """What the submitted, uncollected Batch jobs are projected to cost, a submit not yet confirmed
        included: it may have made its job."""
        return sum(job.get("projectedUSD") or 0.0 for job in self.data["batches"] + self.data["intents"])

    # --- Jev shards and franchise decisions ---------------------------------------------------------

    def restore(self, ctx):
        """Lay every kept shard and decisions file back into `ctx.out_dir`. Returns how many shards."""
        self.foreign = shard_names(ctx) - set(self.data["shards"])
        for name, shard in self.data["shards"].items():
            path = os.path.join(ctx.out_dir, name)
            if not os.path.exists(path):
                caching.write_atomically(path + ".manifest.json", shard["manifest"].encode("utf-8"))
                caching.write_atomically(path, shard["rows"].encode("utf-8"))
        for name, kept in self.data["files"].items():
            path = os.path.join(ctx.out_dir, name)
            caching.write_atomically(path, merged_decisions(path, kept).encode("utf-8"))
        return len(self.data["shards"])

    def keep(self, ctx):
        """Take every shard this run bought or added to, stripped, and the franchise decisions, into the
        ledger. A shard is compared by its rows, so one laid back and then resumed in place is kept again.
        Nothing without a ledger file, or before `restore` has said which shards are not the ledger's."""
        if not self.path or self.foreign is None:
            return
        for name in shard_names(ctx) - self.foreign:
            path = os.path.join(ctx.out_dir, name)
            if not os.path.exists(path + ".manifest.json"):
                continue
            with open(path, encoding="utf-8") as fh:
                rows = stripped(fh.read())
            with open(path + ".manifest.json", encoding="utf-8") as fh:
                manifest = fh.read()
            if rows and (self.data["shards"].get(name) or {}).get("rows") != rows:
                self.data["shards"][name] = {"rows": rows, "manifest": manifest}
        for artifact in FILES:
            path = ctx.path(artifact)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    self.data["files"][artifact.filename] = fh.read()

    def prune(self):
        """Drop what a publish of the run that used this ledger now carries. Returns what was dropped.

        What still waits for premise tags is not published, so it stays: the classify shard a waiting title's
        worklist is built from (the bundle has no sections), and its kept answer. So do a premise title no
        model would tag — its record is what stops it being asked again every run — and everything a pending
        job or unconfirmed submit holds."""
        premise = self.data["steps"].get("premise_tags") or {}
        held = set(premise.get("waiting") or ()) | {
            key for job in self.data["batches"] + self.data["intents"] for keys in job["chunks"].values()
            for key in keys}
        before = (len(self.data["shards"]), len(self.data["files"]))
        self.data["shards"] = {name: shard for name, shard in self.data["shards"].items()
                               if name.startswith("combined-") and held & shard_keys(shard)}
        self.data["files"] = {}
        dropped = {"shards": before[0] - len(self.data["shards"]), "files": before[1]}
        for step, kept in self.data["answers"].items():
            carried = [key for key, entry in kept.items()
                       if not entry.get("untaggable") and not (step == "premise_tags" and key in held)]
            dropped[step] = len(carried)
            for key in carried:
                del kept[key]
        for st in self.data["steps"].values():
            st.pop("reaskAnswers", None)
        return dropped


def shard_names(ctx):
    return {os.path.basename(path) for artifact in SHARDS for path in ctx.paths(artifact)}


def shard_keys(shard):
    """The title keys a kept classify shard's rows are for."""
    rows = map(json.loads, filter(str.strip, shard["rows"].splitlines()))
    return {f"{row['mediaType']}:{row['tmdbId']}" for row in rows}


def merged_decisions(path, kept):
    """The union of a kept decisions file and the out-dir's own, when both decided under the same questions
    and model; otherwise the out-dir's, since the ledger's answered other questions."""
    if not os.path.exists(path):
        return kept
    with open(path, encoding="utf-8") as fh:
        current = json.load(fh)
    old = json.loads(kept)
    same = all(old.get(k) == current.get(k) for k in ("schema", "questionsSha256", "model"))
    if not same:
        return json.dumps(current, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    decisions = dict(sorted({**old["decisions"], **current["decisions"]}.items()))
    return json.dumps({**current, "decisions": decisions, "usage": totals(decisions)},
                      ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def main(argv):
    if len(argv) == 2 and argv[0] == "prune":
        ledger = Ledger(argv[1])
        dropped = ledger.prune()
        ledger.save()
        print(f"paid: pruned what the publish carries: {json.dumps(dropped, sort_keys=True)}")
        return 0
    print("usage: pipeline/paid.py prune FILE", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
