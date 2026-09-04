#!/usr/bin/env bash
# Piece-by-piece Wikipedia plot-finding (FP-2 enrich). Sources den.env, logs into Wikimedia
# Enterprise (if creds present) for a fresh 24h bearer token, then runs ONE enrich batch.
# Resumable: the enrich checkpoint tracks processed ids, so re-running advances to the next batch.
#
#   scripts/enrich-run.sh movie 150      # next 150 un-enriched movies
#   scripts/enrich-run.sh tv 150         # next 150 un-enriched TV titles
#
# Watch the JSON line it prints: `wikiPlot` vs `tagsOnly` = live plot-hit rate for that batch.
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
# shellcheck source=scripts/lib/den-env.sh
. scripts/lib/den-env.sh

MEDIA="${1:-movie}"          # movie | tv
LIMIT="${2:-150}"
OUT_DIR="${OUT_DIR:-out}"
WORKLIST="$OUT_DIR/worklist-$MEDIA.json"

den_load_env
[ -f "$WORKLIST" ] || { echo "missing $WORKLIST — build it first"; exit 1; }
enterprise_login

swift build -c release >/dev/null
echo "enrich: media=$MEDIA limit=$LIMIT out=$OUT_DIR"
.build/release/taxonomy-backfill enrich --worklist "$WORKLIST" --limit "$LIMIT" --out-dir "$OUT_DIR"
