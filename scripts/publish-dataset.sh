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
shopt -s nullglob
assets=("$DIR"/labels-*.json "$DIR"/vectors-*.bin "$DIR"/*.gz "$DIR/dataset.meta.json")
[ ${#assets[@]} -ge 4 ] || { echo "error: expected labels/vectors/gz/meta in $DIR, found: ${assets[*]:-none}" >&2; exit 1; }

echo "publishing → $REPO data-latest:"
printf '  %s\n' "${assets[@]}"

# Create the release if it doesn't exist yet, then clobber its assets.
gh release view data-latest -R "$REPO" >/dev/null 2>&1 \
  || gh release create data-latest -R "$REPO" --title "Dataset (latest)" --notes "The published Den dataset artifact — labels + int8 vectors + meta + gzip. Consumed by den-atlas + the Den app."
gh release upload data-latest -R "$REPO" --clobber "${assets[@]}"
echo "done — consumers: den-atlas scripts/fetch-dataset.sh · Den app 'make sync-dataset'."
