#!/usr/bin/env bash
# `publish-dataset.sh` — the publisher, against a fixture manifest and out-dir.
#
# It had no test, which is the shape of problem this repo keeps having: the script is the last thing
# between a bad artifact and every consumer, and the only way anyone knew a guard worked was that it had
# fired once. Each case below is a guard that must REFUSE, plus one happy path proving the refusals are
# not simply "this script always exits 1".
#
# `gh` is stubbed on PATH: it serves a fixture published manifest, records every upload, and answers the
# post-upload asset listing from what it recorded. Nothing here touches the network or a real release.
#
# Run: scripts/publish-dataset.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PUBLISH="$HERE/publish-dataset.sh"
pass=0
fail=0

ok() { pass=$((pass + 1)); echo "  ok: $1"; }
bad() { fail=$((fail + 1)); echo "  FAIL: $1" >&2; }

# A working tree for one case: a stub `gh`, an out-dir, and a "published" manifest for it to compare against.
setup() {
  WORK="$(mktemp -d)"
  DIR="$WORK/out"
  BIN="$WORK/bin"
  mkdir -p "$DIR" "$BIN"
  UPLOADS="$WORK/uploads"
  : > "$UPLOADS"
  cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
# Enough of `gh release` for the publisher: view (exists / list assets), download (the published
# manifest), upload (record the basename).
set -euo pipefail
case "$2" in
  view)
    if [[ "$*" == *"--json assets"* ]]; then cat "$UPLOADS"; fi
    exit 0 ;;
  download)
    out=""; prev=""
    for a in "$@"; do [ "$prev" = "-O" ] && out="$a"; prev="$a"; done
    if [ -f "$PUBLISHED_META" ]; then cp "$PUBLISHED_META" "$out"; exit 0; fi
    echo "release not found" >&2; exit 1 ;;
  upload)
    for a in "$@"; do [ -f "$a" ] && basename "$a" >> "$UPLOADS"; done
    exit 0 ;;
  create) exit 0 ;;
esac
exit 0
STUB
  chmod +x "$BIN/gh"
  # A real out-dir holds more than the manifest names, and the script has a floor on how many blobs its
  # globs must find before it will publish at all. Without something here, removing one manifested file
  # trips that floor instead of the per-file check the case is about.
  printf 'unmanifested' > "$DIR/vectors-bge-m3.bin"
  # The cc0 experimental pair, which really does sit in out-dirs beside the shipped one.
  printf '{"records":[]}' > "$DIR/labels-cc0.json"
  printf 'unmanifested' > "$DIR/vectors-cc0.bin"
  PUBLISHED_META="$WORK/published.json"
  export UPLOADS PUBLISHED_META
}

teardown() { rm -rf "$WORK"; }

# A blob and the manifest fields that describe it truthfully.
blob() {
  local name="$1" body="$2"
  printf '%s' "$body" > "$DIR/$name"
  shasum -a 256 "$DIR/$name" | cut -d' ' -f1
}

# A minimal but REAL manifest: the keys the guards actually read.
write_meta() {
  local version="${1:-aaaaaaaaaaaa}" labels_records="${2:-10}" extra="${3:-}"
  local labels_sha store_sha
  # The record COUNT has to be real: `manifest-counts.py --consistent` compares what the manifest
  # advertises against what the file holds, so a fixture with an empty records array is refused for that
  # rather than for the thing each case is testing — which is how this fixture was wrong the first time.
  python3 - "$DIR/labels-t02.json" "$labels_records" <<'MKLABELS'
import json, sys
path, n = sys.argv[1], int(sys.argv[2])
rows = [{"tmdbId": i + 1, "mediaType": "movie", "primaryGenre": "Crime"} for i in range(n)]
with open(path, "w") as fh:
    json.dump({"taxonomyVersion": "t02", "records": rows}, fh)
MKLABELS
  labels_sha="$(shasum -a 256 "$DIR/labels-t02.json" | cut -d' ' -f1)"
  # A REAL store-v1 header. `manifest-counts.py` reads `row_count` from the file rather than trusting
  # `storeRecords` beside it, and `MUST_COUNT` turns an unreadable store into a refusal — so a store that
  # is just some bytes fails every case for the wrong reason.
  python3 - "$DIR/den-$version.store" "$labels_records" <<'MKSTORE'
import struct, sys
path, rows = sys.argv[1], int(sys.argv[2])
head = bytearray(64)
head[0:8] = b"DENSTOR1"
struct.pack_into("<I", head, 8, 1)            # format_version
struct.pack_into("<I", head, 12, 0x01020304)  # endianness
struct.pack_into("<I", head, 28, rows)        # row_count
with open(path, "wb") as fh:
    fh.write(head)
MKSTORE
  store_sha="$(shasum -a 256 "$DIR/den-$version.store" | cut -d' ' -f1)"
  python3 - "$DIR/dataset.meta.json" "$version" "$labels_sha" "$store_sha" "$labels_records" "$extra" <<'PY'
import json, sys
path, version, labels_sha, store_sha, records, extra = sys.argv[1:7]
meta = {
    "datasetVersion": version,
    "count": int(records),
    "labelsFile": "labels-t02.json",
    "labelsSha256": labels_sha,
    "labelsRecords": int(records),
    "storeFile": f"den-{version}.store",
    "storeSha256": store_sha,
    "storeRecords": int(records),
}
if extra:
    meta.update(json.loads(extra))
with open(path, "w") as fh:
    json.dump(meta, fh)
PY
}

# The manifest as it is "already published", so the drop/shrink guards have a baseline.
publish_baseline() { cp "$DIR/dataset.meta.json" "$PUBLISHED_META"; }

run_publish() {
  PATH="$BIN:$PATH" bash "$PUBLISH" "$DIR" > "$WORK/out.log" 2> "$WORK/err.log"
}

# --- the happy path, so a refusal below means something ------------------------------------------

echo "publish-dataset.sh:"
setup
write_meta
publish_baseline
if run_publish; then
  if grep -qx "labels-t02.json" "$UPLOADS" && grep -qx "dataset.meta.json" "$UPLOADS"; then
    ok "a manifest that describes its out-dir publishes, blobs then meta"
  else
    bad "the happy path uploaded $(tr '\n' ' ' < "$UPLOADS")"
  fi
  # The meta is the commit point and must be LAST: if a blob upload failed, the old meta has to stand.
  if [ "$(tail -1 "$UPLOADS")" = "dataset.meta.json" ]; then
    ok "the meta is uploaded last, as the commit point"
  else
    bad "the meta was not uploaded last"
  fi
else
  bad "the happy path failed: $(tail -3 "$WORK/err.log")"
fi
teardown

# --- a hash that does not describe the file -------------------------------------------------------

setup
write_meta
publish_baseline
python3 - "$DIR/dataset.meta.json" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1]))
meta["labelsSha256"] = "0" * 64
json.dump(meta, open(sys.argv[1], "w"))
PY
if run_publish; then
  bad "a wrong sha256 published anyway"
else
  grep -q "hashes to" "$WORK/err.log" \
    && ok "a blob whose hash does not match the manifest is refused before anything uploads" \
    || bad "refused, but not for the hash: $(tail -2 "$WORK/err.log")"
fi
[ ! -s "$UPLOADS" ] && ok "…and nothing was uploaded" || bad "it uploaded $(wc -l < "$UPLOADS") asset(s) first"
teardown

# --- a manifest naming a blob that is not there ---------------------------------------------------

setup
write_meta
publish_baseline
rm "$DIR/labels-t02.json"
if run_publish; then
  bad "a manifest naming a missing blob published anyway"
else
  grep -q "is not in" "$WORK/err.log" \
    && ok "a manifest naming a blob the out-dir does not hold is refused" \
    || bad "refused, but not for the missing file: $(tail -2 "$WORK/err.log")"
fi
teardown

# --- dropping a key the published manifest declares -----------------------------------------------
#
# The failure this exists for: finalizing into a FRESH dir emits a manifest that simply does not mention
# the blobs nothing in this repo produces, and `atlas-dataset-sync.sh` DELETES local blobs the new meta
# stops naming — so a publish takes features dark on den-atlas with no error on either side.

setup
write_meta aaaaaaaaaaaa 10 '{"premiseVectorsFile":"vectors-premise.bin","premiseVectorsSha256":"unused"}'
publish_baseline
write_meta aaaaaaaaaaaa 10   # …and now without the premise key
if run_publish; then
  bad "a manifest dropping a published key went out"
else
  grep -q "declares files this one does not" "$WORK/err.log" \
    && ok "a manifest that drops a key the published one declares is refused" \
    || bad "refused, but not for the dropped key: $(tail -2 "$WORK/err.log")"
fi
teardown

# --- a blob that lost records ---------------------------------------------------------------------
#
# A file can stay declared and still shrink. A facts rebuild once dropped the 137 facts-only titles —
# invisible from every other artifact, and the only symptom was /recommend quietly losing library titles.

setup
write_meta aaaaaaaaaaaa 100
publish_baseline
write_meta aaaaaaaaaaaa 50
if run_publish; then
  bad "a shrunk blob published anyway"
else
  grep -q "would lose records" "$WORK/err.log" \
    && ok "a declared blob that lost records is refused" \
    || bad "refused, but not for the shrink: $(tail -2 "$WORK/err.log")"
fi
teardown

# --- a blob from a dead generation ----------------------------------------------------------------
#
# The live manifest really did carry `plot-facets-c85c707b0b18.json` under `datasetVersion 5b1c3213b6a1`:
# the file existed, its sha matched, its record count had not moved (nothing touched it) and its producer
# was registered. Only the NAME said it was a generation old.

setup
write_meta aaaaaaaaaaaa
publish_baseline
cp "$DIR/den-aaaaaaaaaaaa.store" "$DIR/facts-bbbbbbbbbbbb.json"
python3 - "$DIR/dataset.meta.json" "$DIR/facts-bbbbbbbbbbbb.json" <<'PY'
import hashlib, json, sys
meta = json.load(open(sys.argv[1]))
meta["factsFile"] = "facts-bbbbbbbbbbbb.json"
meta["factsSha256"] = hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest()
json.dump(meta, open(sys.argv[1], "w"))
PY
if run_publish; then
  bad "a blob from a dead datasetVersion published anyway"
else
  grep -q "dead generation" "$WORK/err.log" \
    && ok "a blob whose filename carries another datasetVersion is refused" \
    || bad "refused, but not for the version: $(tail -2 "$WORK/err.log")"
fi
teardown

# --- the override is deliberate, not accidental -----------------------------------------------------

setup
write_meta aaaaaaaaaaaa 100
publish_baseline
write_meta aaaaaaaaaaaa 50
if DEN_ALLOW_DROPPING_BLOBS=1 PATH="$BIN:$PATH" bash "$PUBLISH" "$DIR" \
     > "$WORK/out.log" 2> "$WORK/err.log"; then
  ok "DEN_ALLOW_DROPPING_BLOBS=1 lets a deliberate shrink through"
else
  bad "the override did not work: $(tail -3 "$WORK/err.log")"
fi
teardown

# --- a directory that was never finalized ------------------------------------------------------------
#
# An explicit argument is taken at its word. Falling back to ./data when the NAMED directory has no
# manifest meant a typo published ./data instead and reported success.

setup
if PATH="$BIN:$PATH" bash "$PUBLISH" "$DIR" > "$WORK/out.log" 2> "$WORK/err.log"; then
  bad "an out-dir with no manifest published something"
else
  grep -q "no dataset.meta.json" "$WORK/err.log" \
    && ok "a named out-dir with no manifest is refused rather than falling back to ./data" \
    || bad "refused, but not for the missing manifest: $(tail -2 "$WORK/err.log")"
fi
teardown

echo ""
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
