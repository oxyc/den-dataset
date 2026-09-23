#!/usr/bin/env bash
# DT-F — the weekly re-cluster: surface groups the embedding finds cohesive but the taxonomy has no word for.
#
#   pipeline/recluster-run.sh [OUT_DIR]        # default: ./out-t02
#
# Costs nothing but CPU (no API calls) — ~15 minutes over the 47.5k×1024 corpus at k=800, measured on a
# loaded laptop where the Swift it replaced took ~2 — so it is safe to run on a timer. The time is the price
# of reproducing the Swift's arithmetic bit for bit in pure Python (see `pipeline/recluster.py`). Writes candidates and stops: naming a cluster is a human judgement, and adding a label is a taxonomy
# bump, which under DT-F forces a whole-universe reclassification. Auto-adding labels here would silently
# trigger the most expensive pass in the system.
#
# Reading the report: sorted by COHESION, not size. Low label-purity alone just finds grab-bags — at a coarse
# k nearly every cluster is one. What is worth a human's time is a tight cluster (high cohesion) that no
# existing label explains (low purity). Work the top of the list.
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1
OUT_DIR="${1:-out-t02}"
K="${K:-800}"
MIN_SIZE="${MIN_SIZE:-25}"
REPORT="$OUT_DIR/recluster-$(date -u +%Y-%m-%d).json"

# Globbed for the same reason the vectors are: the name carries the taxonomy version, and hardcoding it
# breaks the weekly re-cluster on a taxonomy bump.
labels=""
for f in "$OUT_DIR"/labels-t*.json; do [ -f "$f" ] && labels="$f"; done
[ -n "$labels" ] || { echo "no labels-t*.json in $OUT_DIR" >&2; exit 1; }
# The plot vectors, not the premise ones — a glob plus a case, because `ls | grep` loses ls's exit
# status through the pipe and mangles any name a glob would have handled.
vectors=""
for v in "$OUT_DIR"/vectors-*.bin; do
  case "$v" in *premise*) continue ;; esac
  [ -f "$v" ] && { vectors="$v"; break; }
done
[ -f "$labels" ] || { echo "error: no labels in $OUT_DIR" >&2; exit 1; }
[ -n "$vectors" ] || { echo "error: no vectors blob in $OUT_DIR" >&2; exit 1; }

# recluster.py needs Python 3.12 (math.sumprod — see the script). A machine's `python3` is often older (macOS
# ships 3.9), so the first interpreter new enough runs it: $PYTHON if set, then the usual names on PATH.
py=""
for candidate in "${PYTHON:-python3}" python3 python3.14 python3.13 python3.12; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
    py="$candidate"
    break
  fi
done
[ -n "$py" ] || {
  echo "error: recluster.py needs Python 3.12 or newer (for math.sumprod), and none of ${PYTHON:+$PYTHON, }python3," \
       "python3.14, python3.13 or python3.12 on PATH is one. Install one, or set PYTHON to its path." >&2
  exit 1
}

"$py" pipeline/recluster.py --labels "$labels" --vectors "$vectors" \
                            --k "$K" --iterations 5 --min-size "$MIN_SIZE" --out "$REPORT"

echo "== tightest candidates =="
"$py" - "$REPORT" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))
for r in rows[:10]:
    # A cluster none of whose members carries a subgenre has no dominant label, and the report omits the key.
    print(f"  size={r['size']:4d}  cohesion={r['cohesion']:.2f}  purity={r['purity']:.2f}  "
          f"nearest existing label: {r.get('dominantLabel')!r}")
    print(f"      {', '.join(r['examples'][:6])}")
print(f"\n{len(rows)} candidate(s) → {sys.argv[1]}")
PY
