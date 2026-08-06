#!/usr/bin/env bash
# DT-F — the daily freshness pass: pick up newly-released titles, classify + embed only those, and republish.
#
#   scripts/delta-run.sh [DAYS_BACK] [OUT_DIR]        # defaults: 14 days, ./out-t02
#
# Chains the existing subcommands rather than adding a parallel pipeline, so a delta is classified by exactly
# the same code (and thresholds) as the full backfill — a separate "fast path" would drift and start emitting
# labels the main run wouldn't.
#
#   worklist --mode delta   → new titles since the window, above the vote floor, minus what's published
#   enrich                  → TMDB + Wikipedia plot for those ids only
#   assemble                → classify (same prompts/thresholds as the full run)
#   embed-corpus            → vectors for the new rows only
#   finalize                → labels + vectors + meta, with a bumped datasetVersion
#   publish-dataset.sh      → clobber the data-latest release; the homelab timer picks it up within 4h
#
# Requires TMDB_API_KEY and whatever `assemble` needs for the classifier. Costs real money per run — the
# vote floor is what keeps that bounded (a brand-new release with no votes has no plot worth classifying and
# would be re-billed daily until it earned some).
#
# Idempotent-ish: `assemble` skips ids already in the checkpoint, so a re-run after a partial failure resumes
# rather than reclassifying.
set -euo pipefail

DAYS_BACK="${1:-14}"
OUT_DIR="${2:-out-t02}"
VOTE_FLOOR="${VOTE_FLOOR:-50}"
SINCE="$(date -u -v-"${DAYS_BACK}"d +%Y-%m-%d 2>/dev/null || date -u -d "${DAYS_BACK} days ago" +%Y-%m-%d)"
BIN=".build/release/taxonomy-backfill"

[ -x "$BIN" ] || { echo "building release binary…"; swift build -c release; }
[ -f "$OUT_DIR/labels-t02.json" ] || { echo "error: no published labels in $OUT_DIR — run the full backfill first" >&2; exit 1; }

echo "== DT-F delta: titles since $SINCE (vote floor $VOTE_FLOOR) =="
mkdir -p "$OUT_DIR/delta"

total=0
for media in movie tv; do
  worklist="$OUT_DIR/delta/worklist-$media.json"
  "$BIN" worklist --mode delta --media "$media" --since "$SINCE" \
                  --vote-floor "$VOTE_FLOOR" --known "$OUT_DIR/labels-t02.json" --out "$worklist"
  count=$(python3 -c "import json,sys; print(len(json.load(open('$worklist'))))" 2>/dev/null || echo 0)
  total=$((total + count))
  [ "$count" -eq 0 ] && { echo "  $media: nothing new"; continue; }
  echo "  $media: $count new title(s) → enrich"
  "$BIN" enrich --worklist "$worklist" --vote-floor "$VOTE_FLOOR" --out-dir "$OUT_DIR"
done

# Nothing new is the common case on a quiet day: stop before spending anything on classify/embed/publish,
# and leave the current release untouched rather than republishing an identical artifact.
if [ "$total" -eq 0 ]; then
  echo "== nothing new since $SINCE — no classify, no publish =="
  exit 0
fi

echo "== classify + embed the new rows =="
"$BIN" assemble --batch-id 0 --out-dir "$OUT_DIR"
"$BIN" embed-corpus --labels "$OUT_DIR/labels-t02.json" --out-dir "$OUT_DIR" --chunk 8
"$BIN" finalize --out-dir "$OUT_DIR"

echo "== publish =="
scripts/publish-dataset.sh "$OUT_DIR"
echo "== done: $total new title(s); the homelab atlas-dataset-sync timer picks this up within 4h =="
