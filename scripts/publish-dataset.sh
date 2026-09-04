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
# writes into the same out-dir (movie_ids.json.gz plus the two tv dumps, ~36 MB), publishing TMDB's
# raw export data as release assets from a repo that otherwise refuses to ship raw TMDB text.
# Every entry is a GLOB, including facets: a literal path is not subject to nullglob, so `"$DIR"/facets.bin`
# stayed in the array when the file was absent and the uploader failed on it three times with a message
# about an upload rather than a missing file.
blobs=("$DIR"/facets*.bin "$DIR"/labels-*.json "$DIR"/vectors-*.bin "$DIR"/labels-*.json.gz "$DIR"/metadata-*.json)
[ ${#blobs[@]} -ge 3 ] || { echo "error: expected labels/vectors/gz/metadata in $DIR, found: ${blobs[*]:-none}" >&2; exit 1; }

# The manifest decides what actually publishes (step 3), so list that rather than the glob results — the
# banner used to promise superseded sidecars that the upload pass then skipped.
echo "publishing → $REPO data-latest:"
python3 -c '
import json, sys
meta = json.load(open(sys.argv[1]))
for key, name in sorted(meta.items()):
    if key.endswith("File") and name:
        print("  " + name)
' "$meta"
echo "  $(basename "$meta")"

# Create the release if it doesn't exist yet.
gh release view data-latest -R "$REPO" >/dev/null 2>&1 \
  || gh release create data-latest -R "$REPO" --title "Dataset (latest)" --notes "The published Den dataset artifact — labels + int8 vectors + meta + gzip. Consumed by den-atlas + the Den app."

# Upload one asset with retries. A single multi-file `gh release upload` is all-or-nothing: if it dies partway
# (the ~38 MB vectors blob is the usual culprit), it aborts and you can't tell what landed. Per-file keeps
# progress and lets a transient failure retry just the slow one.
upload_one() {
  local f="$1" n=0
  # </dev/null: this is called from inside a `while read` fed by $manifest_files, so anything the uploader
  # read from stdin would consume the list being iterated. gh does not today; not depending on it is free.
  until gh release upload data-latest -R "$REPO" --clobber "$f" </dev/null; do
    n=$((n + 1)); [ "$n" -ge 3 ] && { echo "error: failed to upload $(basename "$f") after 3 tries" >&2; return 1; }
    echo "  retry $n for $(basename "$f")…" >&2; sleep 5
  done
}

# 1) CHECK LOCALLY, BEFORE UPLOADING ANYTHING.
#
# Blobs used to go up first and the checks ran after. Nothing in these checks needs the network, so running
# them first removes every LOCAL reason to abort mid-clobber — a bad hash, a missing file, a manifest that
# drops a key. It does not make publishing atomic: `gh release upload` clobbers asset by asset, so an upload
# that exhausts its retries in step 3 still leaves new blobs under the old meta. A box already at that meta
# short-circuits on `cmp` and is unaffected; a fresh or lagging one fetches the old meta, gets a mixed set of
# blobs, fails `sha256sum -c` and retries every 4h until someone republishes. Nothing is ever SERVED wrong —
# the sync refuses before touching its data dir — but re-publishing is the only way out.
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
download_err="$(mktemp)"
trap 'rm -f "$manifest_files" "$published_meta" "$download_err"' EXIT

# A guard that cannot tell "there is no release yet" from "I could not ask" is not a guard. Bare
# `2>/dev/null` collapsed an expired token, a 5xx, a rate limit and a `gh` too old for these flags all into
# "skip the check" — silently, and permanently in the last case.
have_published=0
if gh release download data-latest -R "$REPO" -p dataset.meta.json -O "$published_meta" --clobber \
     2>"$download_err"; then
  have_published=1
elif grep -qiE 'release not found|no assets|asset not found|not found' "$download_err"; then
  echo "no published manifest yet — nothing to compare against."
else
  echo "error: could not read the published manifest to compare against:" >&2
  sed 's/^/       /' "$download_err" >&2
  echo "       Refusing to publish blind. Fix the above, or set DEN_ALLOW_DROPPING_BLOBS=1 to skip." >&2
  [ "${DEN_ALLOW_DROPPING_BLOBS:-0}" = "1" ] || exit 1
fi

if [ "$have_published" -eq 1 ]; then
  dropped="$(python3 -c '
import json, sys
old = json.load(open(sys.argv[1]))
new = json.load(open(sys.argv[2]))
print(" ".join(sorted(k for k, v in old.items() if k.endswith("File") and v and not new.get(k))))
' "$published_meta" "$meta")"
  if [ -n "$dropped" ]; then
    echo "error: the published manifest declares files this one does not: $dropped" >&2
    echo "       Publishing would make den-atlas delete them." >&2
    echo "" >&2
    # metadataFile is the one dropped key `finalize` CANNOT restore — it is the key finalize removes.
    # DatasetMeta owns it, and an owned key the struct omits always wins the merge, so every finalize
    # strips it and only `metadata` writes it back. Advising a re-run of finalize here printed
    # instructions that reproduce this identical error, leaving DEN_ALLOW_DROPPING_BLOBS=1 as the only
    # exit — and that override is exactly what makes atlas-dataset-sync delete the sidecar. Forgetting
    # `metadata` after a finalize is the most-warned-about slip in this pipeline, so it is also the
    # likeliest way to arrive here.
    # ONE ordered recipe, not two independent blocks. Both used to fire for a fresh out-dir (which drops
    # every unowned key AND metadataFile), printing metadata first and finalize second — and running them
    # in that order strips metadataFile again, so the next publish failed identically. finalize must come
    # before metadata, always, because finalize is what mints the datasetVersion the sidecar is named for.
    echo "       Fix, in this order:" >&2
    echo "" >&2
    step=1
    case " $dropped " in
      *premise*|*facets*)
        echo "       $step. The premise and facets keys have no producer in this repo — they survive only by" >&2
        echo "          being merged forward from the manifest already at the target path. Copy the PUBLISHED" >&2
        echo "          MANIFEST (not the blobs) into $DIR, then re-run finalize:" >&2
        echo "" >&2
        echo "             gh release download data-latest -R $REPO -p dataset.meta.json -O $DIR/dataset.meta.json --clobber" >&2
        echo "             <taxonomy-backfill> finalize --out-dir $DIR" >&2
        echo "" >&2
        echo "          (Those blobs must also be in $DIR, or step 1 of this script will say so.)" >&2
        echo "" >&2
        step=$((step + 1))
        ;;
    esac
    case " $dropped " in
      *" metadataFile "*)
        echo "       $step. metadataFile: run the metadata step, which is what writes it. finalize cannot —" >&2
        echo "          finalize is the command that REMOVES it, so this has to come last." >&2
        echo "" >&2
        echo "             <taxonomy-backfill> metadata --out-dir $DIR" >&2
        echo "" >&2
        echo "          If labels and vectors did not change then datasetVersion did not either, the existing" >&2
        echo "          sidecar still applies, and --skip-fetch re-patches from it with no TMDB spend:" >&2
        echo "" >&2
        echo "             <taxonomy-backfill> metadata --skip-fetch --out-dir $DIR" >&2
        echo "" >&2
        step=$((step + 1))
        ;;
    esac
    # A dropped key matching neither branch would otherwise print no remedy at all — leaving the override
    # as the only visible option, which is the trap this whole message exists to avoid.
    case " $dropped " in
      *premise*|*facets*|*" metadataFile "*) : ;;
      *)
        echo "       $step. No specific remedy is known for these keys. They are carried forward from the" >&2
        echo "          manifest already in $DIR, so copy the published one there and re-run finalize:" >&2
        echo "" >&2
        echo "             gh release download data-latest -R $REPO -p dataset.meta.json -O $DIR/dataset.meta.json --clobber" >&2
        echo "             <taxonomy-backfill> finalize --out-dir $DIR" >&2
        echo "" >&2
        ;;
    esac

    echo "       If dropping them is deliberate, set DEN_ALLOW_DROPPING_BLOBS=1." >&2
    [ "${DEN_ALLOW_DROPPING_BLOBS:-0}" = "1" ] || exit 1
    echo "       DEN_ALLOW_DROPPING_BLOBS=1 — continuing." >&2
  fi
fi

# 3) BLOBS — driven by the MANIFEST, not by a parallel list of globs.
#
# The globs above decide what is *worth looking at*; the manifest decides what actually ships. When those
# two lists were separate, a declared file whose name matched no glob was hash-checked locally, never
# uploaded, and still passed step 4 — because `data-latest` is a moving release and the previous publish's
# same-named asset satisfies a name check. Consumers would then verify a stale blob against a new sha and
# wedge. den-atlas already models a `metadataGzFile` that no glob here covers.
while read -r name _sha; do
  [ -z "$name" ] && continue
  echo "→ $name"
  upload_one "$DIR/$name" || exit 1
done < "$manifest_files"

# Anything else the globs found that the manifest does not name. Uploaded, but never verified, because
# nothing declares a hash for them.
#
# Sidecars the manifest does not name are SKIPPED. `metadata` writes a new ~4.6 MB
# metadata-<datasetVersion>.json per publish and nothing deletes the old one, so a long-lived out-dir
# accumulates them and every run re-clobbered all of them. This removes the repeated UPLOAD cost only —
# assets already on the release stay there, so the release itself still grows one per datasetVersion.
for f in "${blobs[@]}"; do
  base="$(basename "$f")"
  grep -q "^$base " "$manifest_files" && continue
  case "$base" in
    metadata-*.json)
      echo "  skipping $base (the manifest does not name it)"
      continue ;;
  esac
  echo "→ $base (not named by the manifest)"
  upload_one "$f" || exit 1
done

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
