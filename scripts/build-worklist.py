#!/usr/bin/env python3
"""Build the popularity-ordered enrich worklists (FP-2).

The universe = the TMDB ids Den already ships (extracted from the bundled `labels-t02.json`), so a re-embed
covers exactly the current catalogue. Ordering = TMDB daily-export `popularity` desc, so enrich processes the
titles most likely to have a Wikipedia article first (the low-popularity tail rarely does — watch the per-batch
wikiPlot rate fall off and stop when it's not worth continuing).

    python3 scripts/build-worklist.py            # -> out/worklist-{movie,tv}.json

Env:
    LABELS  path to the shipped labels blob (default: ../den/Sources/DenKit/Resources/labels-t02.json)
    OUT_DIR output dir (default: out)
No API key needed — the daily exports are public static files.
"""
import gzip
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

OUT = os.environ.get("OUT_DIR", "out")
# The shipped labels blob. t02 is current; the t01 default this used to carry no longer exists, so the
# documented `python3 scripts/build-worklist.py` raised FileNotFoundError and step 2 of the re-embed
# runbook was blocked with nothing pointing at the cause.
LABELS = os.environ.get(
    "LABELS",
    os.path.expanduser("~/Projects/Personal/den/Sources/DenKit/Resources/labels-t02.json"),
)
UA = "den-dataset/1.0 (github.com/oxyc/den-dataset)"


def fetch_export(kind: str) -> str:
    """Download today's (or yesterday's) TMDB id+popularity export; return the local .gz path."""
    os.makedirs(OUT, exist_ok=True)
    dest = os.path.join(OUT, f"{kind}_ids.json.gz")
    now = datetime.now(timezone.utc)
    for day_offset in (0, 1):  # today's export is generated ~08:00 UTC; fall back a day if not up yet
        stamp = (now - timedelta(days=day_offset)).strftime("%m_%d_%Y")
        url = f"http://files.tmdb.org/p/exports/{kind}_ids_{stamp}.json.gz"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req) as r, open(dest, "wb") as f:
                f.write(r.read())
            print(f"  {kind}: fetched {stamp}")
            return dest
        except Exception:
            continue
    sys.exit(f"could not fetch {kind} export (tried today + yesterday)")


def popularity(path: str) -> dict:
    m = {}
    with gzip.open(path, "rt") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            m[o["id"]] = o.get("popularity", 0.0)
    return m


def main() -> None:
    labels = json.load(open(LABELS))["records"]
    print(f"universe: {len(labels)} shipped titles from {LABELS}")
    for media, kind in (("movie", "movie"), ("tv", "tv_series")):
        pop = popularity(fetch_export(kind))
        wl = [{"tmdbId": r["tmdbId"], "mediaType": media} for r in labels if r["mediaType"] == media]
        missing = sum(1 for e in wl if e["tmdbId"] not in pop)
        wl.sort(key=lambda e: pop.get(e["tmdbId"], 0.0), reverse=True)  # absent ids sink to the tail
        out = os.path.join(OUT, f"worklist-{media}.json")
        json.dump(wl, open(out, "w"))
        print(f"  {media}: {len(wl)} titles sorted popularity-desc ({missing} not in export -> tail) -> {out}")


if __name__ == "__main__":
    main()
