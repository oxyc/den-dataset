"""What the daily job has paid for, kept outside any one run (oxyc/den-dataset#187): never pay twice.

The job keeps nothing between runs, and a title is done only once a publish carries it. Before this, a run
that failed or was not published left its paid answers in an out-dir nobody kept, and the next run bought
them again (twice on 2026-09-30). The ledger is one prose-free file, `paid-state.json.gz`, which the
workflow downloads before a run and uploads after it whether or not the run publishes (the release asset
`paid-state`; the `daily` concurrency group keeps one writer). It holds:

  * `shards` — every Jev shard a run bought (classify with critique, critique, genres & moods), rows and
    manifest whole, laid back into the next out-dir before anything asks. The stages already skip what a
    shard in the out-dir answers: classify a title whose article and questions it holds
    (`classify.answered`), genres & moods a title any answers shard holds;
  * `files` — the franchise decisions, which the franchise stage already keeps durable; restored as the
    union of the ledger's and the out-dir's;
  * `answers` — per step, a title's answer with the request it answered, for the fan picks and the premise
    tags, which ask through `lib/llm.py`. A step reuses an answer only for the same request (same prompt,
    spec and model), so a new model or spec is the one way to pay again;
  * `batches` and `steps` — Batch jobs submitted and not yet collected, and each weekly step's last submit.

Nothing in it is prose: Jev rows carry section ids, digests and probabilities, never article text, and a
franchise states shard (which holds the text) is never kept.
"""
import gzip
import json
import os

from lib import cache as caching

from . import artifacts
from .franchise_decisions import totals

SCHEMA = "paid-answers-v1"
#: The paid Jev shards a run writes into its out-dir, each beside its manifest.
SHARDS = (artifacts.COMBINED, artifacts.DELTA, artifacts.GENRES_MOODS_ANSWERS)
#: The franchise stage's durable decisions, merged on restore.
FILES = (artifacts.FRANCHISE_DECISIONS, artifacts.FRANCHISE_LINE_DECISIONS)


def empty():
    return {"schema": SCHEMA, "shards": {}, "files": {}, "answers": {}, "batches": [], "steps": {}}


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
        #: The shards in the out-dir once it was restored; what `keep` takes is what appeared after that.
        self.before = None

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

    # --- Jev shards and franchise decisions ---------------------------------------------------------

    def restore(self, ctx):
        """Lay every kept shard and decisions file back into `ctx.out_dir`. Returns how many shards."""
        for name, shard in self.data["shards"].items():
            path = os.path.join(ctx.out_dir, name)
            if not os.path.exists(path):
                caching.write_atomically(path + ".manifest.json", shard["manifest"].encode("utf-8"))
                caching.write_atomically(path, shard["rows"].encode("utf-8"))
        for name, kept in self.data["files"].items():
            path = os.path.join(ctx.out_dir, name)
            caching.write_atomically(path, merged_decisions(path, kept).encode("utf-8"))
        self.before = shard_names(ctx)
        return len(self.data["shards"])

    def keep(self, ctx):
        """Take every shard this run bought, and the franchise decisions, into the ledger. Nothing without a
        ledger file, or before `restore` has said what was there already."""
        if not self.path or self.before is None:
            return
        for name in shard_names(ctx) - self.before:
            path = os.path.join(ctx.out_dir, name)
            if not os.path.exists(path + ".manifest.json"):
                continue
            with open(path, encoding="utf-8") as fh:
                rows = fh.read()
            with open(path + ".manifest.json", encoding="utf-8") as fh:
                manifest = fh.read()
            if rows.strip():
                self.data["shards"][name] = {"rows": rows, "manifest": manifest}
        for artifact in FILES:
            path = ctx.path(artifact)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    self.data["files"][artifact.filename] = fh.read()


def shard_names(ctx):
    return {os.path.basename(path) for artifact in SHARDS for path in ctx.paths(artifact)}


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
