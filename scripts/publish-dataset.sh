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

# An explicit argument is taken at its word. Falling back to ./data when the NAMED directory has no manifest
# meant `publish-dataset.sh out-t03` — a typo, or a dir not yet finalized — silently published ./data
# instead and reported success.
DIR="${1:-}"
if [ -n "$DIR" ]; then
  [ -f "$DIR/dataset.meta.json" ] || { echo "error: no dataset.meta.json in $DIR — run finalize first" >&2; exit 1; }
else
  DIR=out
  [ -f "$DIR/dataset.meta.json" ] || DIR="data"
  [ -f "$DIR/dataset.meta.json" ] || { echo "error: no dataset.meta.json in ./out or ./data — run finalize first" >&2; exit 1; }
fi

REPO="${DEN_DATASET_REPO:-oxyc/den-dataset}"

# Version-agnostic: glob the finalize output (labels-<tax>.json / vectors-<embed>.bin / <labels>.gz).
# dataset.meta.json is kept SEPARATE from the blobs on purpose (see the ordering below).
shopt -s nullglob
meta="$DIR/dataset.meta.json"
# `labels-*.json.gz`, not a bare `*.gz`: that also swept up the TMDB daily-export dumps build-worklist.py
# writes into the same out-dir (movie_ids + the two tv ones, ~36 MB), publishing TMDB's raw export data as
# release assets from a repo that otherwise refuses to ship raw TMDB text.
blobs=("$DIR"/facets.bin "$DIR"/labels-*.json "$DIR"/vectors-*.bin "$DIR"/labels-*.json.gz "$DIR"/metadata-*.json)
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

# 2) VERIFY, before publishing the meta, that every file it names is on the release AND that the local copy
# hashes to what the meta swears it does.
#
# Names alone are not enough. `data-latest` is a MOVING release, so the previous publish's asset satisfies a
# name check trivially: a blob the meta names but that is absent from $DIR is never uploaded, and last run's
# copy passes. A stale <x>Sha256 is worse than a missing file — both consumers hard-verify it
# (den/deploy/atlas-dataset-sync.sh runs sha256sum -c, and the app's index store drops a blob on mismatch),
# so the refresh does not degrade, it STOPS: den-atlas keeps serving the old dataset and the 4-hourly timer
# fails forever with the only signal in the journal.
#
# Read from a temp file, not a process substitution: `while read` fed by <(…) hides the generator's exit
# status from set -e, so an unparseable meta yielded zero lines and missing=0 — this gate waving through
# exactly the broken manifest it exists to catch.
manifest_files="$(mktemp)"
trap 'rm -f "$manifest_files"' EXIT
python3 -c '
import json, sys
meta = json.load(open(sys.argv[1]))
for key, name in meta.items():
    if key.endswith("File") and name:
        print(name, meta.get(key[:-4] + "Sha256", ""))
' "$meta" > "$manifest_files"

present="$(gh release view data-latest -R "$REPO" --json assets -q '.assets[].name')"
missing=0
while read -r name sha; do
  [ -z "$name" ] && continue
  if ! grep -qxF "$name" <<<"$present"; then
    echo "error: meta references '$name' but it is NOT on the release" >&2; missing=1; continue
  fi
  if [ ! -f "$DIR/$name" ]; then
    echo "error: meta references '$name' but it is not in $DIR — the release copy is from an earlier publish" >&2
    missing=1; continue
  fi
  if [ -n "$sha" ]; then
    local_sha="$(shasum -a 256 "$DIR/$name" | cut -d' ' -f1)"
    if [ "$local_sha" != "$sha" ]; then
      echo "error: '$name' hashes to $local_sha but the meta declares $sha — consumers verify this and would refuse the whole refresh" >&2
      missing=1
    fi
  fi
done < "$manifest_files"
[ "$missing" -eq 0 ] || { echo "aborting: refusing to publish a meta that does not describe what is on the release" >&2; exit 1; }

# 3) META LAST — the atomic commit point. It declares the new datasetVersion + names the blobs, so it must be
# the final write. If any blob upload above failed, we already exited and the OLD meta still stands, so
# consumers keep serving the last-good dataset instead of a version that 404s. NEVER upload the meta on its own.
echo "→ dataset.meta.json (commit)"
upload_one "$meta" || exit 1
echo "done — consumers: den-atlas scripts/fetch-dataset.sh · Den app 'make sync-dataset'."
