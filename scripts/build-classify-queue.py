"""The worklist a future classification pass would read. Both kinds of deferral, one file."""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
blob = json.load(open(f"{ROOT}/out-repass/labels-t02.stamped.json", encoding="utf-8"))
basis = {(r.get("mediaType", "movie"), r["tmdbId"]): r["labelBasis"] for r in blob["records"]}

d = f"{ROOT}/out-repass/enriched"
names = sorted((n for n in os.listdir(d) if n.startswith("batch-")), key=lambda n: int(n[6:-5]))
new = {}
for n in names:
    for r in json.load(open(os.path.join(d, n), encoding="utf-8")):
        new[(r["mediaType"], r["tmdbId"])] = r

rows = []
for k, r in new.items():
    if not r.get("hasWikiPlot"):
        continue
    b = basis.get(k, "none")
    if b == "current":
        continue
    rows.append({"mediaType": k[0], "tmdbId": k[1], "title": r.get("title"), "reason": b,
                 "voteCount": r.get("voteCount"), "plotChars": len(r.get("overview", "")),
                 "plotLanguage": r.get("plotLanguage")})
rows.sort(key=lambda x: (x["reason"] != "none", -(x["voteCount"] or 0)))

json.dump({"_": [
    "Titles whose labels were NOT read from the plot text the corpus now holds. Nothing here is broken;",
    "this is the worklist if we ever decide to spend a classification pass on it.",
    "",
    "  none      never classified, now has a plot. A pass ADDS labels — the only titles that gain a",
    "            taxonomy they lack. Listed first, by vote count, so a partial run takes the best of it.",
    "  olderPlot already labelled, and the 2026-09 re-ground then improved the plot underneath. A pass",
    "            only SHARPENS labels that already work, so this half is the cheap one to keep deferring.",
    "",
    "Everything not listed has labelBasis 'current' in labels-t02.json and needs no further reading.",
    "Vectors for every title here are already rebuilt from the new plot — embedding never needed an LLM."],
    "counts": {"none": sum(1 for r in rows if r["reason"] == "none"),
               "olderPlot": sum(1 for r in rows if r["reason"] == "olderPlot"), "total": len(rows)},
    "titles": rows}, open(f"{ROOT}/data/classify-queue.json", "w", encoding="utf-8"),
    indent=2, ensure_ascii=False)
print(f"{len(rows):,} queued  (none {sum(1 for r in rows if r['reason']=='none'):,} · "
      f"olderPlot {sum(1 for r in rows if r['reason']=='olderPlot'):,})")
