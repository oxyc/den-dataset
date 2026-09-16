"""Mark every title with whether its labels were read from the plot text the corpus now holds.

The 2026-09 Wikipedia re-ground changed the plot text under 19,095 already-labelled titles and grounded
9,010 that had none. Re-classifying the changed ones was deliberately deferred: a better plot does not
make an existing label wrong, because the tags describe the work and the work did not change. But
"deferred" only means something if a later run can find them, so the decision is recorded per title
rather than in a commit message.

  current   labels were read from exactly this plot text — nothing to revisit
  olderPlot labels predate the current plot text; re-reading MIGHT sharpen them
  none      never classified, and now has a plot — the only titles a pass would ADD labels to
"""
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
old_dir, new_dir = f"{ROOT}/out-t02/enriched", f"{ROOT}/out-repass/enriched"


def plots(d):
    """Newest batch wins, matching how EnrichedBatches folds a directory."""
    names = sorted((n for n in os.listdir(d) if n.startswith("batch-") and n.endswith(".json")),
                   key=lambda n: int(n[len("batch-"):-len(".json")]))
    out = {}
    for n in names:
        for r in json.load(open(os.path.join(d, n), encoding="utf-8")):
            out[(r["mediaType"], r["tmdbId"])] = r
    return out


old, new = plots(old_dir), plots(new_dir)
blob = json.load(open(f"{ROOT}/out-publish/labels-t02.json", encoding="utf-8"))

counts = {"current": 0, "olderPlot": 0}
for rec in blob["records"]:
    key = (rec.get("mediaType", "movie"), rec["tmdbId"])
    fresh = old.get(key, {}).get("overview", "") == new.get(key, {}).get("overview", "")
    rec["labelBasis"] = "current" if fresh else "olderPlot"
    counts[rec["labelBasis"]] += 1

blob["labelBasisNote"] = (
    "labelBasis says whether a record's labels were read from the plot text this corpus carries. "
    "'olderPlot' means the 2026-09 Wikipedia re-ground improved the plot after the labels were made; "
    "the labels are reused as-is and the titles are queued in data/classify-queue.json for a later pass.")
json.dump(blob, open(f"{ROOT}/out-repass/labels-t02.stamped.json", "w", encoding="utf-8"),
          ensure_ascii=False)

labelled = {(r.get("mediaType", "movie"), r["tmdbId"]) for r in blob["records"]}
unlabelled = [k for k, r in new.items() if k not in labelled and r.get("hasWikiPlot")]
print(f"  current   {counts['current']:>6,}  labels match the plot in the corpus")
print(f"  olderPlot {counts['olderPlot']:>6,}  labels predate the better plot — reused, queued")
print(f"  none      {len(unlabelled):>6,}  never classified — queued, a pass would ADD labels")
