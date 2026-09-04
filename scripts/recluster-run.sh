#!/usr/bin/env bash
# DT-F — the weekly re-cluster: surface groups the embedding finds cohesive but the taxonomy has no word for.
#
#   scripts/recluster-run.sh [OUT_DIR]        # default: ./out-t02
#
# Costs nothing but CPU (no API calls) — ~30s over the 37k×1024 corpus at k=800 — so it is safe to run on a
# timer. Writes candidates and stops: naming a cluster is a human judgement, and adding a label is a taxonomy
# bump, which under DT-F forces a whole-universe reclassification. Auto-adding labels here would silently
# trigger the most expensive pass in the system.
#
# Reading the report: sorted by COHESION, not size. Low label-purity alone just finds grab-bags — at a coarse
# k nearly every cluster is one. What is worth a human's time is a tight cluster (high cohesion) that no
# existing label explains (low purity). Work the top of the list.
set -euo pipefail

OUT_DIR="${1:-out-t02}"
K="${K:-800}"
MIN_SIZE="${MIN_SIZE:-25}"
BIN=".build/release/taxonomy-backfill"
REPORT="$OUT_DIR/recluster-$(date -u +%Y-%m-%d).json"

[ -x "$BIN" ] || { echo "building release binary…"; swift build -c release; }

labels="$OUT_DIR/labels-t02.json"
# The plot vectors, not the premise ones — a glob plus a case, because `ls | grep` loses ls's exit
# status through the pipe and mangles any name a glob would have handled.
vectors=""
for v in "$OUT_DIR"/vectors-*.bin; do
  case "$v" in *premise*) continue ;; esac
  [ -f "$v" ] && { vectors="$v"; break; }
done
[ -f "$labels" ] || { echo "error: no labels in $OUT_DIR" >&2; exit 1; }
[ -n "$vectors" ] || { echo "error: no vectors blob in $OUT_DIR" >&2; exit 1; }

"$BIN" recluster --labels "$labels" --vectors "$vectors" \
                 --k "$K" --iterations 5 --min-size "$MIN_SIZE" --out "$REPORT"

echo "== tightest candidates =="
python3 - "$REPORT" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))
for r in rows[:10]:
    print(f"  size={r['size']:4d}  cohesion={r['cohesion']:.2f}  purity={r['purity']:.2f}  "
          f"nearest existing label: {r['dominantLabel']!r}")
    print(f"      {', '.join(r['examples'][:6])}")
print(f"\n{len(rows)} candidate(s) → {sys.argv[1]}")
PY
