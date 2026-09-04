#!/usr/bin/env bash
# DT-F — the daily freshness pass: pick up newly-released titles and enrich them, ready to classify.
#
#   scripts/delta-run.sh [DAYS_BACK] [OUT_DIR]        # defaults: 14 days, ./out-t02
#
# Chains the existing subcommands rather than adding a parallel pipeline, so a delta is classified by exactly
# the same code (and thresholds) as the full backfill — a separate "fast path" would drift and start emitting
# labels the main run wouldn't.
#
#   worklist --mode delta   → new titles since the window, above the vote floor, minus what's published
#   enrich                  → TMDB + Wikipedia plot for those ids only
#   ---- THE PASS STOPS HERE ----
#   assemble                → classify + embed + append to the index store
#   finalize                → labels + vectors + meta, with a bumped datasetVersion
#   publish-dataset.sh      → clobber the data-latest release; the homelab timer picks it up within 4h
#
# WHY IT STOPS: `assemble` needs Haiku vote passes at votes/batch-<id>-pass<n>.json, and nothing in this repo
# produces them — they come from a Claude Code run over DT-classification-prompt.md (in the den app repo). So
# the second half genuinely cannot be unattended. This script now ends at that boundary and prints the
# commands to finish, naming the batch ids it actually wrote.
#
# It used to run the whole chain and fail every single time: it assembled `--batch-id 0`, an id `enrich` never
# assigns (batches start at 1), after already spending the TMDB and Wikipedia calls and checkpointing those
# ids as enriched. The titles were left enriched-but-unclassified and never published, and the next day did
# the same thing again.
#
# Costs real money per run — the vote floor is what keeps that bounded (a brand-new release with no votes has
# no plot worth classifying and would be re-billed daily until it earned some).
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
# shellcheck source=scripts/lib/den-env.sh
. scripts/lib/den-env.sh

DAYS_BACK="${1:-14}"
OUT_DIR="${2:-out-t02}"
VOTE_FLOOR="${VOTE_FLOOR:-50}"
# How many new titles to enrich per media per run. `enrich` defaults to 150 and silently defers the rest, so
# a backlog would grow without ever saying so; state the limit and report what is left over.
LIMIT="${LIMIT:-150}"
SINCE="$(date -u -v-"${DAYS_BACK}"d +%Y-%m-%d 2>/dev/null || date -u -d "${DAYS_BACK} days ago" +%Y-%m-%d)"
BIN=".build/release/taxonomy-backfill"

den_load_env

# Always rebuild: `[ -x "$BIN" ] || swift build` would let a timer run a months-old binary against current
# sources. The build is a no-op when nothing changed.
swift build -c release >/dev/null

# The published labels blob, found rather than hardcoded — its name carries the taxonomy version, and a
# taxonomy bump would otherwise silently point `--known` at a file that no longer exists.
LABELS=""
for f in "$OUT_DIR"/labels-t*.json; do [ -f "$f" ] && LABELS="$f"; done
[ -n "$LABELS" ] || { echo "error: no labels-t*.json in $OUT_DIR — run the full backfill first" >&2; exit 1; }

echo "== DT-F delta: titles since $SINCE (vote floor $VOTE_FLOOR, limit $LIMIT/media, known $LABELS) =="
mkdir -p "$OUT_DIR/delta"

total=0
batches=""
left=0
for media in movie tv; do
  worklist="$OUT_DIR/delta/worklist-$media.json"
  "$BIN" worklist --mode delta --media "$media" --since "$SINCE" \
                  --vote-floor "$VOTE_FLOOR" --known "$LABELS" --out "$worklist"
  count=$(python3 -c "import json;print(len(json.load(open('$worklist'))))")
  [ "$count" -eq 0 ] && { echo "  $media: nothing new"; continue; }
  echo "  $media: $count candidate(s) → enrich"
  enterprise_login
  # `enrich` reports the batch it wrote. Capturing it is the whole fix for `--batch-id 0`: the id comes from
  # the enrich checkpoint's running counter, so it is only knowable at run time.
  json=$("$BIN" enrich --worklist "$worklist" --limit "$LIMIT" \
                       --vote-floor "$VOTE_FLOOR" --out-dir "$OUT_DIR" | tail -1)
  echo "  $json"
  batch=$(printf '%s' "$json" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("batchId",""))')
  n=$(printf '%s' "$json" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("count",0))')
  rem=$(printf '%s' "$json" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("remaining",0))')
  left=$((left + rem))
  if [ -n "$batch" ] && [ "$n" -gt 0 ]; then batches="$batches $batch"; total=$((total + n)); fi
done

# "Nothing new" counts titles that SURVIVED enrichment, not rows in the worklist. Every recent title enriched
# but never published — anime, no-overview, dropped-no-wiki — reappears in the delta worklist every day (it
# is not in the shipped labels), so a worklist-row count is essentially never zero and the old guard never
# fired: the chain ran, and failed, on days when there was genuinely nothing to do.
if [ "$total" -eq 0 ]; then
  echo "== nothing new to classify since $SINCE — no publish =="
  exit 0
fi

echo
echo "== enriched $total new title(s) into batch(es):$batches =="
[ "$left" -gt 0 ] && echo "   ($left still pending — re-run, or raise LIMIT)"
cat <<EOF

Next, by hand — these need the Haiku vote passes this repo cannot generate:

  1. For each batch above, run the DT classification prompt over
     $OUT_DIR/enriched/batch-<id>.json, saving each pass to
     $OUT_DIR/votes/batch-<id>-pass<n>.json
  2. Then, per batch:
       $BIN assemble --batch-id <id> --out-dir $OUT_DIR --require-wiki-plot
  3. Then once:
       $BIN finalize --out-dir $OUT_DIR
       scripts/publish-dataset.sh $OUT_DIR

(\`assemble\` classifies AND embeds — there is no separate embed step. \`embed-corpus\` is the whole-corpus
re-embed path; pointing it at a delta skips every new title as \`missingLabel\`, because new ids are not in
the shipped labels blob until \`finalize\` runs.)
EOF
