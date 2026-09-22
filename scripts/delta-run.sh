#!/usr/bin/env bash
# DT-F — the daily freshness pass: pick up newly-released titles and enrich them, ready to classify.
#
#   scripts/delta-run.sh [DAYS_BACK] [OUT_DIR]        # defaults: 14 days, ./out-t02
#
# Chains the existing subcommands rather than adding a parallel pipeline, so a delta is classified by exactly
# the same code (and thresholds) as the full backfill — a separate "fast path" would drift and start emitting
# labels the main run wouldn't.
#
#   den stage worklist --mode delta
#                           → new titles since the window, above the vote floor, minus what's published
#   enrich                  → TMDB + Wikipedia plot for those ids only
#   ---- THE PASS STOPS HERE ----
#   classify → embed → finalize → publish
#
# WHY IT STOPS: the second half BUYS — the classify stage is the one step that spends at a paid provider —
# so it is deliberately not unattended. This script ends at that boundary and prints the commands to finish,
# naming the batch ids it actually wrote.
#
# It used to run the whole chain and fail every single time: it ran the label step against `--batch-id 0`,
# an id `enrich` never assigns (batches start at 1), after already spending the TMDB and Wikipedia calls and
# checkpointing those ids as enriched. The titles were left enriched-but-unclassified and never published,
# and the next day did the same thing again.
#
# Costs real money per run — the vote floor is what keeps that bounded (a brand-new release with no votes has
# no plot worth classifying and would be re-billed daily until it earned some).
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
# shellcheck source=scripts/lib/den-env.sh
. scripts/lib/den-env.sh

DAYS_BACK="${1:-14}"
OUT_DIR="${2:-out-t02}"
# Applies to `enrich`, which re-checks the floor per title. The WORKLIST's floor is pinned in
# `pipeline/worklist.py` (VOTE_FLOOR), so the universe a delta collects does not move with an environment
# variable — the two agree at 50 and the test that holds them together is `worklist_test.py`.
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

# One invocation for both media: the stage builds the whole universe, and the two lists are pointed into
# `$OUT_DIR/delta/` with `--set` so a delta's forty rows are not written over the full run's 47k-title
# worklists under the same names. `--dataset-version` is required by `den` and unused here — neither
# worklist filename carries a version.
./den stage worklist --mode delta --since "$SINCE" \
     --out-dir "$OUT_DIR" --dataset-version delta \
     --set "vector_labels=$LABELS" \
     --set "universe_movie=$OUT_DIR/delta/universe-movie.json" \
     --set "universe_tv=$OUT_DIR/delta/universe-tv.json"

for media in movie tv; do
  worklist="$OUT_DIR/delta/universe-$media.json"
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

Next, by hand — the classify stage BUYS, so it is not run unattended:

  1. Dump the articles the classify pass reads:
       $BIN dump-articles --enriched-dir $OUT_DIR/enriched --out $OUT_DIR/articles.jsonl
  2. Classify — with --plan first, to see the call and cost plan:
       ./den stage classify --out-dir $OUT_DIR --dataset-version <ver> --plan
       ./den stage classify --out-dir $OUT_DIR --dataset-version <ver>
  3. Then embed, finalize and publish:
       ./den stage embed --out-dir $OUT_DIR --dataset-version <ver>
       $BIN finalize --out-dir $OUT_DIR
       scripts/publish-dataset.sh $OUT_DIR

(\`embed-corpus\` reads the shipped labels blob, so a new id is only embeddable once the classify pass's
rows have reached it — see docs/OPERATE.md for the order.)
EOF
