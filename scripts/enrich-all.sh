#!/usr/bin/env bash
# Drive the full enrich (plot-finding) run for one media type, batch by batch, until nothing remains.
# Resilient: a batch that aborts (e.g. a transient Wikidata outage that outlived its retries) is retried
# with backoff rather than killing the run; the enrich checkpoint makes every batch resumable.
#
#   scripts/enrich-all.sh movie 500     # all remaining movies, 500/batch
#   scripts/enrich-all.sh tv 500
#
# Per-batch JSON (wikiPlot / tagsOnly / deferred / remaining) is teed to out/enrich-<media>.log.
set -uo pipefail                        # NOT -e: a failed batch must not abort the whole run
cd "$(dirname "$0")/.." || exit 1
# shellcheck source=scripts/lib/den-env.sh
. scripts/lib/den-env.sh

MEDIA="${1:-movie}"
SIZE="${2:-500}"
OUT_DIR="${OUT_DIR:-out}"
# Optional vote-floor override (default = the tool's 50). VOTE_FLOOR=0 re-includes the low-vote tail for a
# full-catalogue re-embed; anime / no-overview titles are still dropped (those filters are independent).
FLOOR_ARG=""; [ -n "${VOTE_FLOOR:-}" ] && FLOOR_ARG="--vote-floor $VOTE_FLOOR"
WORKLIST="$OUT_DIR/worklist-$MEDIA.json"
LOG="$OUT_DIR/enrich-$MEDIA.log"

# `|| exit 1`: this script deliberately runs without -e, so a bare call would print its message and
# continue into six failing batches before giving up on the wrong cause.
den_load_env || exit 1
[ -f "$WORKLIST" ] || { echo "missing $WORKLIST — run scripts/build-worklist.py"; exit 1; }

swift build -c release >/dev/null
BIN=.build/release/taxonomy-backfill

echo "=== enrich-all $MEDIA (size $SIZE) starting $(date) ===" | tee -a "$LOG"
fails=0
batch=0
stalls=0
last_remaining=""
while true; do
  enterprise_login
  json=$("$BIN" enrich --worklist "$WORKLIST" --limit "$SIZE" --out-dir "$OUT_DIR" $FLOOR_ARG 2>>"$OUT_DIR/enrich-$MEDIA.err")
  code=$?
  if [ $code -ne 0 ]; then
    fails=$((fails + 1))
    echo "batch aborted (exit $code); retry #$fails after backoff (see enrich-$MEDIA.err)" | tee -a "$LOG"
    [ $fails -ge 6 ] && { echo "6 consecutive aborts — stopping for inspection" | tee -a "$LOG"; exit 1; }
    sleep $((fails * 30))
    continue
  fi
  fails=0
  batch=$((batch + 1))
  line=$(printf '%s\n' "$json" | tail -1)
  echo "$line" | tee -a "$LOG"
  remaining=$(printf '%s' "$line" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("remaining","?"))' 2>/dev/null || echo "?")
  [ "$remaining" = "0" ] && { echo "=== $MEDIA DONE ($batch batches) $(date) ===" | tee -a "$LOG"; break; }

  # A batch can exit 0 having made NO progress: ids that fail transiently (429/5xx) are deliberately not
  # checkpointed, so during a TMDB or Wikipedia outage every id defers and `remaining` does not move. The
  # loop above only counts non-zero EXITS, so it spun with no sleep — re-minting a token and re-issuing the
  # whole batch as fast as the upstream could refuse it, indefinitely.
  if [ "$remaining" = "$last_remaining" ]; then
    stalls=$((stalls + 1))
    echo "no progress ($remaining still pending) — attempt $stalls, backing off" | tee -a "$LOG"
    [ $stalls -ge 6 ] && { echo "6 batches with no progress — upstream is refusing; stopping" | tee -a "$LOG"; exit 1; }
    sleep $((stalls * 60))
  else
    stalls=0
  fi
  last_remaining="$remaining"
done
