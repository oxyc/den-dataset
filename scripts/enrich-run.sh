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
cd "$(dirname "$0")/.."

MEDIA="${1:-movie}"          # movie | tv
LIMIT="${2:-150}"
OUT_DIR="${OUT_DIR:-out}"
WORKLIST="$OUT_DIR/worklist-$MEDIA.json"

[ -f den.env ] || { echo "missing den.env — cp den.env.example den.env and fill it"; exit 1; }
set -a; source den.env; set +a
[ -n "${TMDB_API_KEY:-}" ] || { echo "TMDB_API_KEY is empty in den.env"; exit 1; }
[ -f "$WORKLIST" ] || { echo "missing $WORKLIST — build it first"; exit 1; }

# Wikimedia Enterprise login → 24h bearer (optional). A login failure must NOT kill the run — degrade to the
# free action API, which has the same plot coverage. The `if …; then` guards the substitution against set -e.
if [ -n "${WIKIMEDIA_ENTERPRISE_USERNAME:-}" ] && [ -n "${WIKIMEDIA_ENTERPRISE_PASSWORD:-}" ]; then
  echo "logging into Wikimedia Enterprise as $WIKIMEDIA_ENTERPRISE_USERNAME …"
  # Pipe the credential JSON to curl on STDIN (--data @-), never as an argv arg — a `-d "{...password...}"`
  # would be visible to any local user via `ps`/`/proc/<pid>/cmdline` for the life of the request.
  if TOKEN=$(python3 -c 'import json,os;print(json.dumps({"username":os.environ["WIKIMEDIA_ENTERPRISE_USERNAME"],"password":os.environ["WIKIMEDIA_ENTERPRISE_PASSWORD"]}))' \
      | curl -fsSL https://auth.enterprise.wikimedia.com/v1/login -H "Content-Type: application/json" --data @- \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])') && [ -n "$TOKEN" ]; then
    export WIKIMEDIA_ENTERPRISE_TOKEN="$TOKEN"
    echo "→ Enterprise token acquired (structured-contents plots)."
  else
    echo "⚠ Enterprise login failed — falling back to the free Wikipedia action API."
  fi
else
  echo "no Enterprise creds — using the free Wikipedia action API."
fi

swift build -c release >/dev/null
echo "enrich: media=$MEDIA limit=$LIMIT out=$OUT_DIR"
.build/release/taxonomy-backfill enrich --worklist "$WORKLIST" --limit "$LIMIT" --out-dir "$OUT_DIR"
