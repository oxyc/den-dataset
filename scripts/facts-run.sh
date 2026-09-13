#!/usr/bin/env bash
# Drive `facts` to completion across WDQS timeouts.
#
# A full-corpus pass is ~9k SPARQL requests over hours, and query.wikidata.org intermittently times out under
# load — one of those kills the process. The command checkpoints every batch and skips ids already present, so
# restarting resumes rather than repeating: the loop costs one batch per failure, not the run.
#
# Batch size 25, not the 100 default. A 100-id batch stalls: the identical query returns in ~1 s from
# another client, but from this process it hangs until the 60 s timeout and the run never clears its first
# batch on resume. 25 advances steadily and is politer to WDQS.
#
#   scripts/facts-run.sh <out-dir> <labels.json> [max-attempts]   (BATCH=25 by default)
set -euo pipefail
DIR="${1:?out-dir}"; LABELS="${2:?labels json}"; MAX="${3:-40}"
BIN=.build/release/taxonomy-backfill
for attempt in $(seq 1 "$MAX"); do
  if "$BIN" facts --labels "$LABELS" --out-dir "$DIR" --batch "${BATCH:-25}" >>"$DIR/facts.log" 2>&1; then
    echo "facts: complete on attempt $attempt"
    exit 0
  fi
  n=$(python3 -c "import json;print(len(json.load(open('$DIR/facts-fields.json'))))" 2>/dev/null || echo 0)
  echo "facts: attempt $attempt failed, $n titles checkpointed — retrying" >&2
  sleep 30
done
echo "facts: gave up after $MAX attempts" >&2
exit 1
