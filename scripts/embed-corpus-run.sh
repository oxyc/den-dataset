#!/usr/bin/env bash
# Build the bge-m3 vector corpus SAFELY (the "semantic vectors now" path). Safety layers:
#   1. den-embed runs with a bounded per-request batch (DEN_EMBED_BATCH) + doc-length cap so a single request
#      can't spike ONNX activation memory.
#   2. embed-corpus streams enriched batches (one in memory at a time) + small --chunk requests + is RESUMABLE.
#   3. SEGMENTS: den-embed is restarted fresh every SEGMENT titles. ONNX Runtime's memory arena grows to its
#      peak and never shrinks, so a long-lived process creeps up (this is what OOM'd the machine before).
#      Restarting per segment reclaims it; embed-corpus resumes from its store, so no work is lost.
#   4. WATCHDOG: a segment that dies (OOM/hang) is retried after a fresh restart.
# Idempotent — safe to Ctrl-C and re-run.
#
#   scripts/embed-corpus-run.sh /path/to/labels-t01.json
#
set -uo pipefail
cd "$(dirname "$0")/.."

LABELS="${1:?usage: embed-corpus-run.sh <existing labels-t01.json>}"
OUT_DIR="${OUT_DIR:-out-vecnow}"
ENRICHED_DIR="${ENRICHED_DIR:-out/enriched}"
CHUNK="${CHUNK:-16}"          # docs per den-embed request (client side)
EMBED_BATCH="${EMBED_BATCH:-8}"       # docs the model processes at once (server) — bge-m3 attention is O(seq^2),
MAX_CHARS="${MAX_CHARS:-5000}"        # so bound BOTH the batch and the doc length to cap activation memory.
SEGMENT="${SEGMENT:-5000}"    # titles per den-embed lifetime, then restart it fresh
EMBED_DIR="${EMBED_DIR:-$HOME/Projects/Personal/den-embed}"
EMBED_LOG="${EMBED_LOG:-/tmp/den-embed.log}"

[ -f "$LABELS" ] || { echo "missing labels file: $LABELS"; exit 1; }

stop_embed() { pkill -f "uvicorn server:app" 2>/dev/null || true; sleep 2; }

boot_embed() {
  stop_embed
  echo "booting fresh den-embed (DEN_EMBED_BATCH=$EMBED_BATCH MAX_CHARS=$MAX_CHARS) …"
  ( cd "$EMBED_DIR" && DEN_EMBED_BATCH="$EMBED_BATCH" DEN_EMBED_MAX_CHARS="$MAX_CHARS" \
      nohup .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8791 > "$EMBED_LOG" 2>&1 & )
  for _ in $(seq 1 40); do curl -s -m 2 http://localhost:8791/health >/dev/null 2>&1 && return 0; sleep 3; done
  echo "den-embed failed to come up (see $EMBED_LOG)"; return 1
}

swift build -c release >/dev/null
BIN=.build/release/taxonomy-backfill
trap stop_embed EXIT

for attempt in $(seq 1 200); do
  boot_embed || { sleep 5; continue; }
  # One segment: embed up to SEGMENT new titles, then exit so den-embed can be recycled.
  out=$("$BIN" embed-corpus --labels "$LABELS" --enriched-dir "$ENRICHED_DIR" --out-dir "$OUT_DIR" \
        --chunk "$CHUNK" --limit "$SEGMENT" 2>>/tmp/embed-corpus.err) || {
    echo "segment $attempt failed (den-embed likely died) — restarting + resuming…"; stop_embed; continue; }
  echo "$out"
  written=$(printf '%s' "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("written",0))' 2>/dev/null || echo 0)
  rss=$(ps -o rss= -p "$(pgrep -f "uvicorn server:app" | head -1)" 2>/dev/null | awk '{printf "%.0f", $1/1024}')
  echo "  segment done: +$written titles (den-embed RSS ${rss:-?}MB); recycling den-embed"
  if [ "$written" = "0" ]; then
    echo "=== all titles embedded → finalizing ==="
    stop_embed
    "$BIN" finalize --out-dir "$OUT_DIR"
    exit 0
  fi
done
echo "gave up after 200 segments"; exit 1
