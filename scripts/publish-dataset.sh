#!/usr/bin/env bash
# Publish the finalize output as the den-dataset `data-latest` GitHub Release — the SINGLE SOURCE OF TRUTH
# for the dataset artifact. den-atlas (serving) and the Den app (bundled snapshot) both fetch from it, so
# neither depends on the other's source tree. `data-latest` is a MOVING release: this clobbers its assets on
# every publish, so consumers always pull the current dataset.
#
#   taxonomy-backfill finalize --out-dir out   # produces labels-*.json + vectors-*.bin + *.gz + dataset.meta.json
#   scripts/publish-dataset.sh [OUT_DIR]       # default: ./out, then ./data
#
# Requires `gh` authenticated with write access to the repo. The blobs are gitignored (large derived data),
# so they live as release assets, never in git.
set -euo pipefail

DIR="${1:-out}"
[ -f "$DIR/dataset.meta.json" ] || DIR="data"
[ -f "$DIR/dataset.meta.json" ] || { echo "error: no dataset.meta.json in ./out or ./data — run finalize first" >&2; exit 1; }

REPO="${DEN_DATASET_REPO:-oxyc/den-dataset}"

# Version-agnostic: glob the finalize output (labels-<tax>.json / vectors-<embed>.bin / <labels>.gz).
# dataset.meta.json is kept SEPARATE from the blobs on purpose (see the ordering below).
shopt -s nullglob
meta="$DIR/dataset.meta.json"
blobs=("$DIR"/facets.bin "$DIR"/labels-*.json "$DIR"/vectors-*.bin "$DIR"/*.gz "$DIR"/metadata-*.json)
[ ${#blobs[@]} -ge 3 ] || { echo "error: expected labels/vectors/gz/metadata in $DIR, found: ${blobs[*]:-none}" >&2; exit 1; }

echo "publishing → $REPO data-latest:"
printf '  %s\n' "${blobs[@]}" "$meta"

# Create the release if it doesn't exist yet.
gh release view data-latest -R "$REPO" >/dev/null 2>&1 \
  || gh release create data-latest -R "$REPO" --title "Dataset (latest)" --notes "The published Den dataset artifact — labels + int8 vectors + meta + gzip. Consumed by den-atlas + the Den app."

# Upload one asset with retries. A single multi-file `gh release upload` is all-or-nothing: if it dies partway
# (the ~38 MB vectors blob is the usual culprit), it aborts and you can't tell what landed. Per-file keeps
# progress and lets a transient failure retry just the slow one.
upload_one() {
  local f="$1" n=0
  until gh release upload data-latest -R "$REPO" --clobber "$f"; do
    n=$((n + 1)); [ "$n" -ge 3 ] && { echo "error: failed to upload $(basename "$f") after 3 tries" >&2; return 1; }
    echo "  retry $n for $(basename "$f")…" >&2; sleep 5
  done
}

# 1) BLOBS FIRST — everything the meta references. The meta is deliberately NOT in this batch.
for f in "${blobs[@]}"; do echo "→ $(basename "$f")"; upload_one "$f" || exit 1; done

# 2) VERIFY every file the meta will point at is actually on the release, BEFORE publishing the meta.
present="$(gh release view data-latest -R "$REPO" --json assets -q '.assets[].name')"
missing=0
while read -r name; do
  [ -z "$name" ] && continue
  grep -qxF "$name" <<<"$present" || { echo "error: meta references '$name' but it is NOT on the release" >&2; missing=1; }
done < <(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); [print(m[k]) for k in m if k.endswith("File") and m.get(k)]' "$meta")
[ "$missing" -eq 0 ] || { echo "aborting: refusing to publish a meta that points at missing blobs (half-published release)" >&2; exit 1; }

# 3) META LAST — the atomic commit point. It declares the new datasetVersion + names the blobs, so it must be
# the final write. If any blob upload above failed, we already exited and the OLD meta still stands, so
# consumers keep serving the last-good dataset instead of a version that 404s. NEVER upload the meta on its own.
echo "→ dataset.meta.json (commit)"
upload_one "$meta" || exit 1
echo "done — consumers: den-atlas scripts/fetch-dataset.sh · Den app 'make sync-dataset'."
