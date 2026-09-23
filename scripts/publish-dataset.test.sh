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
# EVERY CASE RUNS THROUGH `run_publish`, so the same cases can be run a second time through
# `den stage publish` (oxyc/den-dataset#27) with `DEN_PUBLISH_VIA=stage`. That second run is what the
# publish stage's faithfulness rests on: the stage wraps a script whose output is a release, so there are
# no bytes to diff the way the corpus and store stages are diffed, and the only way to show a guard still
# refuses through the wrapper is to run the refusals through it. A wrapper that swallowed an exit code
# would satisfy every argv comparison and fail here.
#
# Run: scripts/publish-dataset.test.sh  ·  DEN_PUBLISH_VIA=stage scripts/publish-dataset.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PUBLISH="$HERE/publish-dataset.sh"
# The publisher's ownership guard resolves producer paths and `git ls-files` against the working directory,
# so the script only works from the repo root. Run from anywhere else, every case here failed on the
# ownership guard instead of the thing it tests — a test that passes only when it is invoked one particular
# way is a test that will one day be believed for the wrong reason.
cd "$HERE/.." || exit 1
pass=0
fail=0

ok() { pass=$((pass + 1)); echo "  ok: $1"; }
bad() { fail=$((fail + 1)); echo "  FAIL: $1" >&2; }

# A scratch signing key for the whole run: every publish signs the meta, and one without a key is refused.
# Inherited through the environment, which is how it reaches the publisher through the stage too. The
# signer refuses a key other than the pinned one, so the scratch key's public half stands in for the pin.
KEYDIR="$(mktemp -d)"
trap 'rm -rf "$KEYDIR"' EXIT
python3 "$HERE/sign-manifest.py" keygen "$KEYDIR/key.pem" >/dev/null || { echo "keygen failed" >&2; exit 1; }
export DEN_DATASET_SIGNING_KEY="$KEYDIR/key.pem"
DEN_DATASET_PUBLIC_KEY="$(python3 "$HERE/sign-manifest.py" public-key)" || { echo "public-key failed" >&2; exit 1; }
export DEN_DATASET_PUBLIC_KEY

# Does the meta's signature verify, with openssl, against the scratch key's public half?
signature_verifies() {
  python3 - "$HERE/sign-manifest.py" "$1" "$DEN_DATASET_SIGNING_KEY" <<'PY'
import base64, importlib.util, json, subprocess, sys, tempfile
spec = importlib.util.spec_from_file_location("sign_manifest", sys.argv[1])
sm = importlib.util.module_from_spec(spec); spec.loader.exec_module(sm)
meta = json.load(open(sys.argv[2]))
raw = meta.get("signature", "")
if not raw.startswith("ed25519:"):
    sys.exit(1)
with tempfile.TemporaryDirectory() as d:
    open(f"{d}/payload", "wb").write(sm.payload(meta))
    open(f"{d}/sig", "wb").write(base64.b64decode(raw[len("ed25519:"):]))
    subprocess.run(sm.openssl() + ["pkey", "-in", sys.argv[3], "-pubout", "-out", f"{d}/pub.pem"], check=True)
    done = subprocess.run(sm.openssl() + ["pkeyutl", "-verify", "-pubin", "-inkey", f"{d}/pub.pem", "-rawin",
                                          "-in", f"{d}/payload", "-sigfile", f"{d}/sig"], capture_output=True)
sys.exit(done.returncode)
PY
}

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
  # A real out-dir holds the store's INPUTS beside it — labels, vectors, the cc0 experimental pair — and
  # none of them publish any more. They are here so the cases below prove that: what lands on the release
  # is the store and the meta, with these sitting right next to them untouched.
  printf 'unmanifested' > "$DIR/vectors-bge-m3.bin"
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
  #
  # The labels also carry the golden set's own answers, so the quality gate scores them 1.0 against the
  # committed golden set and baseline — the real gate, passing, rather than a stubbed one. `$MOOD` replaces
  # every mood, which is how a case makes the labels worse than what ships.
  python3 - "$DIR/labels-t02.json" "$labels_records" "$HERE/../data/eval/golden-large.json" "${MOOD:-}" <<'MKLABELS'
import json, sys
path, n, golden_path, mood = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
rows = {("movie", i + 1): {"tmdbId": i + 1, "mediaType": "movie", "primaryGenre": "Crime"} for i in range(n)}
for t in json.load(open(golden_path))["titles"]:
    moods = [mood] if mood else t.get("moods") or []
    rows[(t["mediaType"], t["tmdbId"])] = {
        "tmdbId": t["tmdbId"], "mediaType": t["mediaType"], "primaryGenre": t["primaryGenre"],
        "subgenres": [{"label": l, "confidence": 1.0} for l in (t.get("subgenres") or []) + (t.get("themes") or [])],
        "moods": [{"label": l, "confidence": 1.0} for l in moods]}
with open(path, "w") as fh:
    json.dump({"taxonomyVersion": "t02", "records": list(rows.values())}, fh)
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
  python3 - "$DIR/dataset.meta.json" "$version" "$labels_sha" "$store_sha" "$labels_records" "$extra" \
    "$HERE/../data/alias-decisions.json" "$HERE/../data/award-ceremony-merges.json" <<'PY'
import hashlib, json, sys
path, version, labels_sha, store_sha, records, extra, decisions, merges = sys.argv[1:9]
meta = {
    "datasetVersion": version,
    "taxonomyVersion": "t02",
    "count": int(records),
    # Declared the way `finalize` still declares them — the publisher is what stops publishing them.
    "labelsFile": "labels-t02.json",
    "labelsSha256": labels_sha,
    "labelsRecords": int(records),
    "storeFile": f"den-{version}.store",
    "storeSha256": store_sha,
    "storeRecords": int(records),
    # What the store build stamps: the committed decisions applied, and no collision left undecided.
    "aliasDecisions": {"sha256": hashlib.sha256(open(decisions, "rb").read()).hexdigest(),
                       "dropped": 0, "undecided": 0},
    # And every title several Wikidata items claim has its item chosen.
    "wikidataItems": {"ambiguous": []},
    # And the committed award ceremony merges, none stale.
    "awardMerges": {"sha256": hashlib.sha256(open(merges, "rb").read()).hexdigest(),
                    "applied": 7, "stale": []},
}
if extra:
    meta.update(json.loads(extra))
with open(path, "w") as fh:
    json.dump(meta, fh)
PY
}

# The manifest as it is "already published", so the drop/shrink guards have a baseline.
publish_baseline() { cp "$DIR/dataset.meta.json" "$PUBLISHED_META"; }

# The publisher, invoked the way this run is testing it. Both forms take the publish dir as their only
# argument and leave the same two logs behind, so a case reads identically either way. The stage is given
# no `--dataset-version`: the store it uploads is named by the manifest, not by the declaration.
run_publish() {
  if [ "${DEN_PUBLISH_VIA:-script}" = "stage" ]; then
    PATH="$BIN:$PATH" python3 "$HERE/../den" stage publish --out-dir "$DIR" \
      > "$WORK/out.log" 2> "$WORK/err.log"
  else
    PATH="$BIN:$PATH" bash "$PUBLISH" "$DIR" > "$WORK/out.log" 2> "$WORK/err.log"
  fi
}

# --- the happy path, so a refusal below means something ------------------------------------------

echo "publish-dataset.sh (via ${DEN_PUBLISH_VIA:-script}):"
setup
write_meta
publish_baseline
if run_publish; then
  # THE CONTRACT (oxyc/den#113): the store and the manifest that describes it, and nothing else. The
  # labels this fixture's manifest declares are in the out-dir, hashed correctly, and must NOT go out.
  if [ "$(tr '\n' ' ' < "$UPLOADS")" = "den-aaaaaaaaaaaa.store dataset.meta.json " ]; then
    ok "a publish uploads the store and the meta, in that order, and nothing else"
  else
    bad "the happy path uploaded $(tr '\n' ' ' < "$UPLOADS")"
  fi
  # The meta is the commit point and must be LAST: if a blob upload failed, the old meta has to stand.
  if [ "$(tail -1 "$UPLOADS")" = "dataset.meta.json" ]; then
    ok "the meta is uploaded last, as the commit point"
  else
    bad "the meta was not uploaded last"
  fi
  if grep -q "labelsFile" "$DIR/dataset.meta.json"; then
    bad "the published meta still declares the labels"
  else
    ok "the meta it publishes no longer declares a blob the release does not carry"
  fi
  grep -q '"microF1"' "$WORK/out.log" \
    && ok "the labels were scored against the golden set and passed the quality baseline" \
    || bad "the happy path published without scoring the labels"
  grep -q "alias gate: the store applied" "$WORK/out.log" \
    && ok "the store's alias decisions were checked" \
    || bad "the happy path published without the alias gate"
  signature_verifies "$DIR/dataset.meta.json" \
    && ok "the meta it publishes carries a signature that verifies against the key" \
    || bad "the published meta is unsigned, or its signature does not verify"
else
  bad "the happy path failed: $(tail -3 "$WORK/err.log")"
fi
teardown

# --- signing (oxyc/den#127) -----------------------------------------------------------------------------
#
# Every publish went out unsigned, silently. With no key the publish is now refused, and only an explicit
# `--unsigned` lets one through.

setup
write_meta
publish_baseline
if DEN_DATASET_SIGNING_KEY="$WORK/no-such-key.pem" run_publish; then
  bad "a publish with no signing key went out"
else
  grep -q "no dataset signing key" "$WORK/err.log" && grep -q -- "--unsigned" "$WORK/err.log" \
    && ok "a publish with no signing key is refused, and the refusal names --unsigned" \
    || bad "refused, but not for the key: $(tail -3 "$WORK/err.log")"
fi
[ ! -s "$UPLOADS" ] && ok "…and nothing was uploaded" || bad "it uploaded $(wc -l < "$UPLOADS") asset(s) first"
teardown

# A key openssl cannot use is refused at the signing step, which runs before the first upload.
setup
write_meta
publish_baseline
printf 'not a key' > "$WORK/bad.pem"
if DEN_DATASET_SIGNING_KEY="$WORK/bad.pem" run_publish; then
  bad "a publish whose signing failed went out"
else
  grep -q "openssl could not" "$WORK/err.log" \
    && ok "a key that cannot sign is refused" \
    || bad "refused, but not for the signing: $(tail -3 "$WORK/err.log")"
fi
[ ! -s "$UPLOADS" ] && ok "…and nothing was uploaded" || bad "it uploaded $(wc -l < "$UPLOADS") asset(s) first"
teardown

# A real key that is not the one consumers pin: its signature would be refused everywhere.
setup
write_meta
publish_baseline
python3 "$HERE/sign-manifest.py" keygen "$WORK/other.pem" >/dev/null
if DEN_DATASET_SIGNING_KEY="$WORK/other.pem" run_publish; then
  bad "a meta signed with a key nobody pins went out"
else
  grep -q "not the dataset signing key" "$WORK/err.log" \
    && ok "a key other than the pinned one is refused" \
    || bad "refused, but not for the key: $(tail -3 "$WORK/err.log")"
fi
[ ! -s "$UPLOADS" ] && ok "…and nothing was uploaded" || bad "it uploaded $(wc -l < "$UPLOADS") asset(s) first"
teardown

# `--unsigned` is a flag of the script; the stage passes the out-dir alone, so through it there is no
# unsigned publish at all.
if [ "${DEN_PUBLISH_VIA:-script}" != "stage" ]; then
  setup
  # A signature left over from an earlier generation, which `finalize` would carry forward.
  write_meta aaaaaaaaaaaa 10 '{"signature": "ed25519:c3RhbGU="}'
  publish_baseline
  if DEN_DATASET_SIGNING_KEY="$WORK/no-such-key.pem" PATH="$BIN:$PATH" bash "$PUBLISH" "$DIR" --unsigned \
       > "$WORK/out.log" 2> "$WORK/err.log"; then
    grep -q "UNSIGNED" "$WORK/err.log" \
      && ok "--unsigned publishes without a key, and says so" \
      || bad "--unsigned published silently"
    grep -q '"signature"' "$DIR/dataset.meta.json" \
      && bad "--unsigned published a stale signature" \
      || ok "and drops a stale signature rather than publishing one that cannot verify"
  else
    bad "--unsigned was refused: $(tail -3 "$WORK/err.log")"
  fi
  teardown
fi

# --- labels that score below what ships ------------------------------------------------------------
#
# The quality gate ran as `|| true` for as long as it existed here, so a publish went out whatever the
# labels scored. The baseline is now the recorded scores of the shipped labels, and falling more than the
# recorded tolerance under any of them refuses before anything uploads.

setup
MOOD="Wrong" write_meta
publish_baseline
if run_publish; then
  bad "labels below the quality baseline published anyway"
else
  grep -q "below the baseline recorded" "$WORK/err.log" \
    && ok "labels scoring below the recorded quality baseline are refused" \
    || bad "refused, but not for the quality: $(tail -3 "$WORK/err.log")"
  grep -q "\-\-record" "$WORK/err.log" \
    && ok "and the refusal says how to accept a deliberate drop" \
    || bad "the quality refusal offered no way forward"
fi
[ ! -s "$UPLOADS" ] && ok "…and nothing was uploaded" || bad "it uploaded $(wc -l < "$UPLOADS") asset(s) first"
teardown

# --- the cutover: every retired key goes, and none of them reads as a dropped blob ------------------
#
# The publish that changes what `data-latest` carries is the one to get right. The published manifest still
# declares labels, vectors, metadata, premise and facets; this one declares none of them. That must NOT be
# reported as a dropped blob, because the operator's only way past that guard is DEN_ALLOW_DROPPING_BLOBS=1
# — which ALSO switches off the record-count guard and the "could not read the published manifest" guard,
# making the most consequential publish in the pipeline's history the one that checked the least.

setup
write_meta aaaaaaaaaaaa 10 '{"metadataFile":"metadata-aaaaaaaaaaaa.json","metadataSha256":"unused",
  "metadataRecords":10,"premiseVectorsFile":"vectors-premise.bin","premiseVectorsSha256":"unused",
  "premiseCount":10,"premiseDims":1024,"premiseEmbeddingModel":"bge-m3-premise",
  "facetsFile":"facets.bin","facetsSha256":"unused","labelsGzFile":"labels-t02.json.gz"}'
publish_baseline
write_meta aaaaaaaaaaaa 10   # this one declares only the labels finalize writes, and the store
if run_publish; then
  if [ "$(tr '\n' ' ' < "$UPLOADS")" = "den-aaaaaaaaaaaa.store dataset.meta.json " ]; then
    ok "the first store-only publish goes out with no override, and carries only the store"
  else
    bad "the cutover uploaded $(tr '\n' ' ' < "$UPLOADS")"
  fi
  # The rollback. Both copies of the old key set are otherwise gone — the prune rewrote the local manifest
  # and the publish clobbered the one on the release — while the blobs themselves are still on the release.
  if grep -q "labelsFile" "$DIR/dataset.meta.json.prepublish"; then
    ok "the manifest it replaced is kept beside it, so the cutover can be rolled back"
  else
    bad "no usable pre-publish manifest was kept"
  fi
else
  bad "the cutover was refused: $(tail -3 "$WORK/err.log")"
fi
teardown

# --- a hash that does not describe the file -------------------------------------------------------

setup
write_meta
publish_baseline
python3 - "$DIR/dataset.meta.json" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1]))
meta["storeSha256"] = "0" * 64
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
mv "$DIR/den-aaaaaaaaaaaa.store" "$DIR/den-cccccccccccc.store"
if run_publish; then
  bad "a manifest naming a missing blob published anyway"
else
  grep -q "is not in" "$WORK/err.log" \
    && ok "a manifest naming a blob the out-dir does not hold is refused" \
    || bad "refused, but not for the missing file: $(tail -2 "$WORK/err.log")"
fi
teardown

# --- an out-dir with no store at all ---------------------------------------------------------------
#
# The store is the only artifact now, so an out-dir without one has nothing to publish. Before, the floor
# counted blobs and a dir full of inputs satisfied it — a publish could go out carrying no store.

setup
write_meta
publish_baseline
rm "$DIR"/den-*.store
if run_publish; then
  bad "an out-dir with no store published something"
else
  grep -q "no den-<version>.store" "$WORK/err.log" \
    && ok "an out-dir holding the inputs but no store is refused" \
    || bad "refused, but not for the missing store: $(tail -2 "$WORK/err.log")"
fi
teardown

# --- dropping a key the published manifest declares -----------------------------------------------
#
# The failure this exists for: `atlas-dataset-sync.sh` DELETES local blobs the new meta stops naming, so a
# publish takes a feature dark on den-atlas with no error on either side. With one artifact left, the key
# that can go missing is the store's, and a manifest with no store is a dataset with nothing in it.

setup
write_meta
publish_baseline
python3 - "$DIR/dataset.meta.json" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1]))
for key in ("storeFile", "storeSha256", "storeRecords"):
    meta.pop(key)
json.dump(meta, open(sys.argv[1], "w"))
PY
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
cp "$DIR/den-aaaaaaaaaaaa.store" "$DIR/den-bbbbbbbbbbbb.store"
python3 - "$DIR/dataset.meta.json" "$DIR/den-bbbbbbbbbbbb.store" <<'PY'
import hashlib, json, sys
meta = json.load(open(sys.argv[1]))
meta["storeFile"] = "den-bbbbbbbbbbbb.store"
meta["storeSha256"] = hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest()
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

# --- one datasetVersion, one store ------------------------------------------------------------------
#
# This already happened and nothing saw it: two stores shipped as den-5b1c3213b6a1.store under
# datasetVersion 5b1c3213b6a1 with the same builtAt — 131,159,594 bytes, then 135,146,098. Every guard
# here keyed on the identifier, and the identifier was the thing that had stopped being unique. With the
# store as the only artifact, a rollback cannot name what it restores if a version can mean two files.

setup
write_meta aaaaaaaaaaaa
publish_baseline
# Same version, same filename, different bytes — the exact shape that shipped.
printf 'a different store' >> "$DIR/den-aaaaaaaaaaaa.store"
python3 - "$DIR/dataset.meta.json" "$DIR/den-aaaaaaaaaaaa.store" <<'PY'
import hashlib, json, os, sys
meta = json.load(open(sys.argv[1]))
meta["storeSha256"] = hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest()
meta["storeBytes"] = os.path.getsize(sys.argv[2])
json.dump(meta, open(sys.argv[1], "w"))
PY
if run_publish; then
  bad "a second, different store published under a version that already names one"
else
  grep -q "already published with a DIFFERENT store" "$WORK/err.log" \
    && ok "republishing a version whose store bytes changed is refused" \
    || bad "refused, but not for the identity: $(tail -2 "$WORK/err.log")"
fi
teardown

# --- and the override does NOT excuse it -------------------------------------------------------------
#
# DEN_ALLOW_DROPPING_BLOBS is for a deliberate shrink. There is no such thing as a deliberate silent
# identity collision, so the identity guard is deliberately not behind it.

setup
write_meta aaaaaaaaaaaa
publish_baseline
printf 'a different store' >> "$DIR/den-aaaaaaaaaaaa.store"
python3 - "$DIR/dataset.meta.json" "$DIR/den-aaaaaaaaaaaa.store" <<'PY'
import hashlib, json, os, sys
meta = json.load(open(sys.argv[1]))
meta["storeSha256"] = hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest()
meta["storeBytes"] = os.path.getsize(sys.argv[2])
json.dump(meta, open(sys.argv[1], "w"))
PY
if DEN_ALLOW_DROPPING_BLOBS=1 run_publish; then
  bad "the override let a version publish a second, different store"
else
  ok "the override does not excuse an identity collision"
fi
teardown

# --- a deliberate store rebuild, said out loud -------------------------------------------------------
#
# `datasetVersion` names the GENERATION, and check-filename-version.py requires every versioned filename
# to carry it — so a store rebuilt from an UNCHANGED corpus by a fixed writer legitimately keeps the same
# version. The fault was never that this happens; it was that it happened silently. So it is allowed when
# you say why, and the reason is written into the manifest the release carries.

setup
write_meta aaaaaaaaaaaa
publish_baseline
printf 'a rebuilt store' >> "$DIR/den-aaaaaaaaaaaa.store"
python3 - "$DIR/dataset.meta.json" "$DIR/den-aaaaaaaaaaaa.store" <<'PY'
import hashlib, json, os, sys
meta = json.load(open(sys.argv[1]))
meta["storeSha256"] = hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest()
meta["storeBytes"] = os.path.getsize(sys.argv[2])
json.dump(meta, open(sys.argv[1], "w"))
PY
if DEN_STORE_REBUILD="the label sections gained the premise pass" run_publish; then
  grep -q "store rebuilt within datasetVersion" "$WORK/out.log" \
    && ok "a stated rebuild is allowed and announced" \
    || bad "it published but said nothing: $(tail -3 "$WORK/out.log")"
  python3 -c '
import json, sys
meta = json.load(open(sys.argv[1]))
sys.exit(0 if meta.get("storeRebuild") == "the label sections gained the premise pass" else 1)
' "$DIR/dataset.meta.json" \
    && ok "and the reason is recorded in the manifest the release carries" \
    || bad "storeRebuild missing from the published manifest"
else
  bad "a stated rebuild was refused: $(tail -3 "$WORK/err.log")"
fi
teardown

# --- the override is deliberate, not accidental -----------------------------------------------------

setup
write_meta aaaaaaaaaaaa 100
publish_baseline
# A NEW version, because a store with different records is a different store — which the identity guard
# above now refuses to publish under the old one. That is the honest shape of a deliberate shrink: a new
# generation that happens to hold fewer records, not a version quietly meaning two files.
write_meta bbbbbbbbbbbb 50
if DEN_ALLOW_DROPPING_BLOBS=1 run_publish; then
  ok "DEN_ALLOW_DROPPING_BLOBS=1 lets a deliberate shrink through"
else
  bad "the override did not work: $(tail -3 "$WORK/err.log")"
fi
teardown

# --- a store built from an input that has since changed ----------------------------------------------
#
# The gap the store's input record closes (oxyc/den#113). `data-latest` carries one blob, so the manifest
# names one blob, so the ownership guard checks one blob — while the labels, vectors, metadata, facts and
# corpus the store is BUILT from are still produced and no longer declared. Re-run the labelling producer,
# rebuild nothing, publish: the store is a generation behind a file sitting in the same directory, and
# until now every guard here passed.

setup
write_meta
# The out-dir already holds `vectors-bge-m3.bin` as an unmanifested input; record the store as having been
# built from it, then change it, which is what re-running its producer looks like from here.
recorded_sha="$(shasum -a 256 "$DIR/vectors-bge-m3.bin" | cut -d' ' -f1)"
python3 - "$DIR/dataset.meta.json" "$DIR/vectors-bge-m3.bin" "$recorded_sha" <<'PY'
import json, os, sys
meta_path, vectors, sha = sys.argv[1:4]
meta = json.load(open(meta_path))
meta["storeInputs"] = [{"arg": "vectors", "path": vectors, "sha256": sha,
                        "bytes": os.path.getsize(vectors), "mtime": int(os.path.getmtime(vectors))}]
json.dump(meta, open(meta_path, "w"))
PY
publish_baseline
printf 're-embedded' > "$DIR/vectors-bge-m3.bin"
if run_publish; then
  bad "a store built from a since-changed input published anyway"
else
  grep -q "not built from the inputs in this tree" "$WORK/err.log" \
    && ok "a store whose recorded input has changed since the build is refused" \
    || bad "refused, but not for the input: $(tail -3 "$WORK/err.log")"
fi
[ ! -s "$UPLOADS" ] && ok "…and nothing was uploaded" || bad "it uploaded $(wc -l < "$UPLOADS") asset(s) first"
teardown

# --- and the same store, published from a dir that no longer holds the input --------------------------
#
# The publish dir is allowed to hold only the store and the manifest — that is the point of the cutover —
# so an input it cannot see must not be a refusal. It must also not be a silence: the record is then the
# only evidence the store matches anything.

setup
write_meta
python3 - "$DIR/dataset.meta.json" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1]))
meta["storeInputs"] = [{"arg": "vectors", "path": "elsewhere/vectors-bge-m3.bin", "sha256": "ab" * 32,
                        "bytes": 1, "mtime": 1}]
json.dump(meta, open(sys.argv[1], "w"))
PY
publish_baseline
rm "$DIR/vectors-bge-m3.bin"
if run_publish; then
  grep -q "not in this tree" "$WORK/err.log" \
    && ok "an input the publish dir does not hold is announced as unchecked, not refused" \
    || bad "it published but said nothing about the unchecked input"
else
  bad "an absent input was treated as a refusal: $(tail -3 "$WORK/err.log")"
fi
teardown

# --- a directory that was never finalized ------------------------------------------------------------
#
# An explicit argument is taken at its word. Falling back to ./data when the NAMED directory has no
# manifest meant a typo published ./data instead and reported success.

setup
if run_publish; then
  bad "an out-dir with no manifest published something"
else
  grep -q "no dataset.meta.json" "$WORK/err.log" \
    && ok "a named out-dir with no manifest is refused rather than falling back to ./data" \
    || bad "refused, but not for the missing manifest: $(tail -2 "$WORK/err.log")"
fi
teardown

# --- grounding: a title's plot must be about that title ----------------------------------------------
#
# oxyc/den-dataset#16. 1,066 titles in the shipped generation are grounded on a Wikipedia article that
# also grounds another title, so they carry that work's labels, facets and premise. The standing count
# warns — an absolute floor of zero would refuse every publish over a defect that has already shipped —
# but an INCREASE is refused, because the only thing that makes it worse is a re-ground and #27 proposes
# running one daily.

# An enriched tree grounding `$1` titles on one shared article, plus one title on its own.
write_enriched() {
  mkdir -p "$DIR/enriched"
  python3 - "$DIR/enriched/batch-1.json" "$1" <<'PY'
import json, sys
path, shared = sys.argv[1], int(sys.argv[2])
rows = [{"tmdbId": i + 1, "mediaType": "movie", "title": f"Wuthering Heights {i}", "hasWikiPlot": True,
         "overview": "the novel", "plotArticle": "Wuthering Heights", "plotLanguage": "en"}
        for i in range(shared)]
rows.append({"tmdbId": 900, "mediaType": "movie", "title": "Solaris", "hasWikiPlot": True,
             "overview": "its own plot", "plotArticle": "Solaris", "plotLanguage": "en"})
with open(path, "w") as fh:
    json.dump(rows, fh)
PY
}

setup
write_meta
write_enriched 2
publish_baseline
if run_publish; then
  # The count is stamped even on a clean publish — it is what the NEXT one ratchets against.
  if grep -q '"sharedPlotArticleTitles": 2' "$DIR/dataset.meta.json"; then
    ok "the shared-article count is stamped into the manifest as the next publish's baseline"
  else
    bad "no shared-article count was stamped: $(tail -3 "$WORK/out.log")"
  fi
  grep -q "another title : 2" "$WORK/out.log" \
    && ok "the standing violation is censused on every publish rather than staying invisible" \
    || bad "the census did not run: $(tail -5 "$WORK/out.log")"
else
  bad "a standing violation blocked the publish: $(tail -3 "$WORK/err.log")"
fi
teardown

# The regression. The published manifest says 2; this tree grounds 3 titles on the one article.
setup
write_meta
write_enriched 2
publish_baseline
python3 - "$PUBLISHED_META" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1]))
meta["sharedPlotArticleTitles"] = 2
json.dump(meta, open(sys.argv[1], "w"))
PY
write_enriched 3
if run_publish; then
  bad "grounding got worse and it published anyway"
else
  if grep -q "REGRESSED" "$WORK/err.log" && grep -q "Nothing uploaded" "$WORK/err.log"; then
    ok "a grounding regression is refused before anything is uploaded"
  else
    bad "refused, but not for the grounding: $(tail -3 "$WORK/err.log")"
  fi
  # A guard that refuses must name both sides and say what to do.
  grep -q "'Wuthering Heights'" "$WORK/out.log" \
    && ok "the refusal names the shared article and the titles on it" \
    || bad "the refusal did not name the article"
  grep -q "DEN_ALLOW_SHARED_PLOTS" "$WORK/err.log" \
    && ok "and says how to proceed deliberately" \
    || bad "the refusal offered no way forward"
  [ ! -s "$UPLOADS" ] \
    && ok "and nothing reached the release" \
    || bad "a refused publish still uploaded $(tr '\n' ' ' < "$UPLOADS")"
fi
teardown

# The override, which must state a reason and is recorded in the manifest.
setup
write_meta
write_enriched 2
publish_baseline
python3 - "$PUBLISHED_META" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1]))
meta["sharedPlotArticleTitles"] = 2
json.dump(meta, open(sys.argv[1], "w"))
PY
write_enriched 3
if DEN_ALLOW_SHARED_PLOTS="re-grounded the Brontë cluster on purpose" run_publish; then
  grep -q "sharedPlotArticlesWaived" "$DIR/dataset.meta.json" \
    && ok "an accepted regression is recorded in the manifest, with its reason" \
    || bad "the override published but left no record of why"
else
  bad "the override did not let a deliberate regression through: $(tail -3 "$WORK/err.log")"
fi
teardown

# An out-dir with no enriched tree. `plotArticle` lives only there, so the census cannot be taken — that
# must SKIP loudly, not refuse, or every publish from a dir that carries only the store would be blocked.
setup
write_meta
publish_baseline
if run_publish; then
  grep -q "grounding guard: SKIPPED" "$WORK/err.log" \
    && ok "a publish dir with no enriched tree skips the census and says so" \
    || bad "it published with no word about the skipped census"
else
  bad "a missing enriched tree was treated as a refusal: $(tail -3 "$WORK/err.log")"
fi
teardown

# --- aliases: a collision nobody decided -----------------------------------------------------------------
#
# An alias that is another title's name puts its title at the top of a search for that name (Taxi Driver
# carried "Alien"). The store build records how many it ships; any at all is a refusal that says how to
# decide them.

# `$1` replaces the stamped aliasDecisions record; `null` removes it.
set_alias_record() {
  python3 - "$DIR/dataset.meta.json" "$1" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1]))
record = json.loads(sys.argv[2])
if record is None:
    meta.pop("aliasDecisions")
else:
    meta["aliasDecisions"].update(record)
json.dump(meta, open(sys.argv[1], "w"))
PY
}

setup
write_meta
publish_baseline
set_alias_record '{"undecided": 3}'
if run_publish; then
  bad "a store shipping undecided alias collisions published anyway"
else
  grep -q "ships 3 alias(es) that are another title's name" "$WORK/err.log" \
    && ok "a store shipping an undecided alias collision is refused" \
    || bad "refused, but not for the aliases: $(tail -3 "$WORK/err.log")"
  grep -q "check-alias-collisions.py .* --review" "$WORK/err.log" && grep -q '"keep" or "drop"' "$WORK/err.log" \
    && ok "and the refusal says how to list and decide them" \
    || bad "the alias refusal offered no way forward"
fi
[ ! -s "$UPLOADS" ] && ok "…and nothing was uploaded" || bad "it uploaded $(wc -l < "$UPLOADS") asset(s) first"
teardown

# A drop decided after the store was built is not in the store. The record names the decisions it applied.
setup
write_meta
publish_baseline
set_alias_record "{\"sha256\": \"$(printf '%064d' 0)\"}"
if run_publish; then
  bad "a store built from other alias decisions published anyway"
else
  grep -q "the store applied a different" "$WORK/err.log" \
    && ok "a store built from alias decisions other than the committed ones is refused" \
    || bad "refused, but not for the decisions: $(tail -3 "$WORK/err.log")"
fi
teardown

# A store from a writer that never applied the decisions has no record at all — today's shipped store.
setup
write_meta
publish_baseline
set_alias_record null
if run_publish; then
  bad "a store with no alias record published anyway"
else
  grep -q "records no aliasDecisions" "$WORK/err.log" \
    && ok "a store that does not say it applied the alias decisions is refused" \
    || bad "refused, but not for the missing record: $(tail -3 "$WORK/err.log")"
fi
teardown

# --- wikidata items: a contested title nothing chose an item for ------------------------------------------
#
# Several Wikidata items claim one TMDB id and neither a rule nor a committed decision chose between them,
# so the title was written with no Wikidata fields and has no card.

# `$1` replaces the stamped wikidataItems record; `null` removes it.
set_item_record() {
  python3 - "$DIR/dataset.meta.json" "$1" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1]))
record = json.loads(sys.argv[2])
if record is None:
    meta.pop("wikidataItems")
else:
    meta["wikidataItems"] = record
json.dump(meta, open(sys.argv[1], "w"))
PY
}

setup
write_meta
publish_baseline
set_item_record '{"ambiguous": ["tv:6618", "tv:70837"]}'
if run_publish; then
  bad "a store shipping titles with no Wikidata item chosen published anyway"
else
  grep -q "2 title(s) ship with no Wikidata fields" "$WORK/err.log" && grep -q "tv:6618, tv:70837" "$WORK/err.log" \
    && ok "a store shipping a contested title with no item chosen is refused, naming it" \
    || bad "refused, but not for the items: $(tail -3 "$WORK/err.log")"
  grep -q "data/wikidata-item-decisions.json" "$WORK/err.log" \
    && ok "and the refusal points at the decisions file" \
    || bad "the item refusal did not name the decisions file"
fi
[ ! -s "$UPLOADS" ] && ok "…and nothing was uploaded" || bad "it uploaded $(wc -l < "$UPLOADS") asset(s) first"
teardown

setup
write_meta
publish_baseline
set_item_record null
if run_publish; then
  bad "a store with no wikidataItems record published anyway"
else
  grep -q "records no wikidataItems" "$WORK/err.log" \
    && ok "a store that does not say how many titles lack an item is refused" \
    || bad "refused, but not for the missing record: $(tail -3 "$WORK/err.log")"
fi
teardown

# --- award ceremony merges: a stale entry, another list, no record -----------------------------------------
#
# data/award-ceremony-merges.json joins bodies Wikidata splits across an organisation and its "Awards"
# group. An entry whose ceremony no title names any more fixes a split that has moved.

# `$1` is merged into the stamped awardMerges record; `null` removes it.
set_merge_record() {
  python3 - "$DIR/dataset.meta.json" "$1" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1]))
record = json.loads(sys.argv[2])
if record is None:
    meta.pop("awardMerges")
else:
    meta["awardMerges"].update(record)
json.dump(meta, open(sys.argv[1], "w"))
PY
}

setup
write_meta
publish_baseline
set_merge_record '{"stale": ["Q81565646"]}'
if run_publish; then
  bad "a store with a stale award ceremony merge published anyway"
else
  grep -q "1 merge(s) in .*award-ceremony-merges.json name a ceremony no title does" "$WORK/err.log" \
    && grep -q "Q81565646" "$WORK/err.log" \
    && ok "a stale award ceremony merge is refused, naming it" \
    || bad "refused, but not for the stale merge: $(tail -3 "$WORK/err.log")"
fi
[ ! -s "$UPLOADS" ] && ok "…and nothing was uploaded" || bad "it uploaded $(wc -l < "$UPLOADS") asset(s) first"
teardown

setup
write_meta
publish_baseline
set_merge_record '{"sha256": "0000"}'
if run_publish; then
  bad "a store built from another award merge list published anyway"
else
  grep -q "merges other than the committed" "$WORK/err.log" \
    && ok "a store built from another award merge list is refused" \
    || bad "refused, but not for the list: $(tail -3 "$WORK/err.log")"
fi
teardown

setup
write_meta
publish_baseline
set_merge_record null
if run_publish; then
  bad "a store with no awardMerges record published anyway"
else
  grep -q "records no awardMerges" "$WORK/err.log" \
    && ok "a store that does not say which award merges it applied is refused" \
    || bad "refused, but not for the missing record: $(tail -3 "$WORK/err.log")"
fi
teardown

echo ""
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
