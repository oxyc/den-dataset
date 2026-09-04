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
# Every entry is a GLOB, including facets: a literal path is not subject to nullglob, so `"$DIR"/facets.bin`
# stayed in the array when the file was absent and the uploader failed on it three times with a message
# about an upload rather than a missing file.
blobs=("$DIR"/facets*.bin "$DIR"/labels-*.json "$DIR"/vectors-*.bin "$DIR"/labels-*.json.gz "$DIR"/metadata-*.json)
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

# 1) CHECK LOCALLY, BEFORE UPLOADING ANYTHING.
#
# Blobs used to go up first and the checks ran after, so an abort left the moving release holding NEW blobs
# under the OLD meta. A box already at that meta is fine (atlas-dataset-sync short-circuits on `cmp`), but a
# lagging or fresh one fetches the old meta, downloads the new blobs, fails `sha256sum -c`, and its timer
# fails every tick until someone publishes again. Nothing here needs the network, so none of it belongs
# after the first upload.
manifest_files="$(mktemp)"
trap 'rm -f "$manifest_files"' EXIT
python3 -c '
import json, sys
meta = json.load(open(sys.argv[1]))
for key, name in meta.items():
    if key.endswith("File") and name:
        print(name, meta.get(key[:-4] + "Sha256", ""))
' "$meta" > "$manifest_files"

bad=0
while read -r name sha; do
  [ -z "$name" ] && continue
  if [ ! -f "$DIR/$name" ]; then
    echo "error: the meta declares '$name' but it is not in $DIR" >&2; bad=1; continue
  fi
  if [ -n "$sha" ]; then
    local_sha="$(shasum -a 256 "$DIR/$name" | cut -d' ' -f1)"
    if [ "$local_sha" != "$sha" ]; then
      echo "error: '$name' hashes to $local_sha but the meta declares $sha — consumers verify this and would refuse the whole refresh" >&2
      bad=1
    fi
  fi
done < "$manifest_files"
[ "$bad" -eq 0 ] || { echo "aborting: the meta does not describe what is in $DIR. Nothing uploaded." >&2; exit 1; }

# 2) REFUSE A MANIFEST THAT DROPS A FILE THE PUBLISHED ONE DECLARES.
#
# Nothing in this repo produces facets.bin or the premise blobs — their manifest keys survive only because
# `finalize` merges over the meta ALREADY IN THAT OUT-DIR. So finalizing into a FRESH dir (which is what the
# documented full re-embed and embed-corpus-run.sh both do) emits a manifest that simply does not mention
# them, and every check above passes vacuously: you cannot catch a missing file by iterating the keys of a
# manifest that no longer has the key.
#
# It is not a quiet degradation either. atlas-dataset-sync.sh deletes local blobs the new meta stops naming,
# so publishing one of these takes premise "More Like This", facet search and the poster sidecar dark on
# den-atlas — with no error on either side.
published_meta="$(mktemp)"
trap 'rm -f "$manifest_files" "$published_meta"' EXIT
if gh release download data-latest -R "$REPO" -p dataset.meta.json -O "$published_meta" --clobber 2>/dev/null; then
  dropped="$(python3 -c '
import json, sys
old = json.load(open(sys.argv[1]))
new = json.load(open(sys.argv[2]))
print(" ".join(sorted(k for k, v in old.items() if k.endswith("File") and v and not new.get(k))))
' "$published_meta" "$meta")"
  if [ -n "$dropped" ]; then
    echo "error: the published manifest declares files this one does not: $dropped" >&2
    echo "       Publishing would make den-atlas delete them. If they still exist, copy them into $DIR and" >&2
    echo "       re-run finalize there so their keys are carried forward; if dropping them is deliberate," >&2
    echo "       set DEN_ALLOW_DROPPING_BLOBS=1." >&2
    [ "${DEN_ALLOW_DROPPING_BLOBS:-0}" = "1" ] || exit 1
    echo "       DEN_ALLOW_DROPPING_BLOBS=1 — continuing." >&2
  fi
fi

# 3) BLOBS — everything the meta references. The meta is deliberately NOT in this batch.
for f in "${blobs[@]}"; do echo "→ $(basename "$f")"; upload_one "$f" || exit 1; done

# 4) VERIFY every file the meta names actually landed on the release, before publishing the meta.
#
# `data-latest` is a MOVING release, so a name check alone is trivially satisfied by the PREVIOUS publish's
# asset. The hashes were compared against the local copies in step 1; this is the narrower question of
# whether the uploads actually landed.
present="$(gh release view data-latest -R "$REPO" --json assets -q '.assets[].name')"
missing=0
while read -r name _sha; do
  [ -z "$name" ] && continue
  grep -qxF "$name" <<<"$present" \
    || { echo "error: meta references '$name' but it is NOT on the release" >&2; missing=1; }
done < "$manifest_files"
[ "$missing" -eq 0 ] || { echo "aborting: refusing to publish a meta that points at missing blobs (half-published release)" >&2; exit 1; }

# 5) META LAST — the atomic commit point. It declares the new datasetVersion + names the blobs, so it must be
# the final write. If any blob upload above failed, we already exited and the OLD meta still stands, so
# consumers keep serving the last-good dataset instead of a version that 404s. NEVER upload the meta on its own.
echo "→ dataset.meta.json (commit)"
upload_one "$meta" || exit 1
echo "done — consumers: den-atlas scripts/fetch-dataset.sh · Den app 'make sync-dataset'."
