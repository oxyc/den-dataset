#!/usr/bin/env bash
# Drive the facts stage to completion across WDQS timeouts.
#
# A full-corpus pass is ~9k SPARQL requests over hours, and query.wikidata.org intermittently times out under
# load. The stage checkpoints every batch and resumes past ids already scraped; a batch that fails is dropped
# whole and the stage then refuses to merge, because a pass that skipped batches is not finished. So the
# loop is what finishes it: each attempt sweeps up what the last one skipped, for the cost of those batches.
#
#   pipeline/facts-run.sh <out-dir> [max-attempts]
#
# The out-dir must hold labels-t02.json, dataset.meta.json and facts-delta-ids.txt (docs/OPERATE.md step 6a);
# set LABELS to point at labels elsewhere. The dataset version is the manifest's — the stage reads it there.
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
DIR="${1:?out-dir}"; MAX="${2:-40}"
for attempt in $(seq 1 "$MAX"); do
  if ./den stage facts --out-dir "$DIR" \
       ${LABELS:+--set "vector_labels=$LABELS"} >>"$DIR/facts.log" 2>&1; then
    echo "facts: complete on attempt $attempt"
    exit 0
  fi
  n=$(python3 -c "import json;print(len(json.load(open('$DIR/facts-fields.json'))))" 2>/dev/null || echo 0)
  echo "facts: attempt $attempt failed, $n corpus titles checkpointed — retrying" >&2
  sleep 30
done
echo "facts: gave up after $MAX attempts" >&2
exit 1
