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
cd "$(dirname "$0")/.."

MEDIA="${1:-movie}"
SIZE="${2:-500}"
OUT_DIR="${OUT_DIR:-out}"
WORKLIST="$OUT_DIR/worklist-$MEDIA.json"
LOG="$OUT_DIR/enrich-$MEDIA.log"

[ -f den.env ] || { echo "missing den.env"; exit 1; }
set -a; source den.env; set +a
[ -n "${TMDB_API_KEY:-}" ] || { echo "TMDB_API_KEY empty in den.env"; exit 1; }
[ -f "$WORKLIST" ] || { echo "missing $WORKLIST — run scripts/build-worklist.py"; exit 1; }

swift build -c release >/dev/null
BIN=.build/release/taxonomy-backfill

# Mint a fresh 24h Enterprise bearer (degrade to the free action API on failure). Called once per batch.
enterprise_login() {
  unset WIKIMEDIA_ENTERPRISE_TOKEN
  [ -n "${WIKIMEDIA_ENTERPRISE_USERNAME:-}" ] && [ -n "${WIKIMEDIA_ENTERPRISE_PASSWORD:-}" ] || return 0
  local tok
  if tok=$(python3 -c 'import json,os;print(json.dumps({"username":os.environ["WIKIMEDIA_ENTERPRISE_USERNAME"],"password":os.environ["WIKIMEDIA_ENTERPRISE_PASSWORD"]}))' \
        | curl -fsSL https://auth.enterprise.wikimedia.com/v1/login -H "Content-Type: application/json" --data @- \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])') && [ -n "$tok" ]; then
    export WIKIMEDIA_ENTERPRISE_TOKEN="$tok"
  else
    echo "⚠ Enterprise login failed — this batch uses the free action API." | tee -a "$LOG"
  fi
}

echo "=== enrich-all $MEDIA (size $SIZE) starting $(date) ===" | tee -a "$LOG"
fails=0
batch=0
while true; do
  enterprise_login
  json=$("$BIN" enrich --worklist "$WORKLIST" --limit "$SIZE" --out-dir "$OUT_DIR" 2>>"$OUT_DIR/enrich-$MEDIA.err")
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
done
