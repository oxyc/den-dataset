#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERIFY="$ROOT/pipeline/verify-dataset-manifest.sh"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"; ASSETS="$WORK/assets"; mkdir -p "$BIN" "$ASSETS"
MANIFEST="$WORK/dataset.meta.json"
printf '{"datasetVersion":"old"}\n' > "$MANIFEST"

cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
out=""; url=""; max_time=""; prev=""
for arg in "$@"; do
  [ "$prev" = "--output" ] && out="$arg"
  [ "$prev" = "--max-time" ] && max_time="$arg"
  prev="$arg"; [[ "$arg" = http* ]] && url="$arg"
done
name="${url##*/}"
printf '%s %s\n' "$name" "$max_time" >> "$FAKE_CURL_LOG"
if [ "${FAKE_TRANSPORT_ERROR:-}" = "$name" ]; then exit 7; fi
if [ -f "$FAKE_ASSETS/$name" ]; then cp "$FAKE_ASSETS/$name" "$out"; printf 200
else printf 404; fi
STUB
cat > "$BIN/cosign" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
bundle=""; identity=""; issuer=""; manifest=""; prev=""
for arg in "$@"; do
  [ "$prev" = "--bundle" ] && bundle="$arg"
  [ "$prev" = "--certificate-identity" ] && identity="$arg"
  [ "$prev" = "--certificate-oidc-issuer" ] && issuer="$arg"
  prev="$arg"; [[ "$arg" != -* ]] && [ "$arg" != verify-blob ] && manifest="$arg"
done
read -r expected_digest expected_identity expected_issuer < "$bundle"
[ "$(shasum -a 256 "$manifest" | cut -d' ' -f1)" = "$expected_digest" ]
[ "$identity" = "$expected_identity" ]
[ "$issuer" = "$expected_issuer" ]
STUB
chmod +x "$BIN/curl" "$BIN/cosign"
export PATH="$BIN:$PATH" FAKE_ASSETS="$ASSETS" FAKE_CURL_LOG="$WORK/curl.log" \
  DATASET_RELEASE_BASE=https://fixture.invalid/data-latest
identity=https://github.com/oxyc/den-dataset/.github/workflows/daily.yml@refs/heads/main
issuer=https://token.actions.githubusercontent.com
digest() { shasum -a 256 "$MANIFEST" | cut -d' ' -f1; }
write_proof() { printf '%s %s %s\n' "$(digest)" "${2:-$identity}" "$issuer" > "$ASSETS/$1"; }
run_verify() { "$VERIFY" "$MANIFEST" >"$WORK/out" 2>"$WORK/err"; }
pass=0; fail=0
ok() { echo "ok  $1"; pass=$((pass + 1)); }
bad() { echo "not ok  $1"; fail=$((fail + 1)); }

asset="dataset.meta.json.sha256-$(digest).bundle"
write_proof "$asset"
run_verify && ok "digest-addressed proof verifies" || bad "digest-addressed proof was refused"

: > "$FAKE_CURL_LOG"
DEN_CURL_MAX_TIME=7 run_verify
if [ "$(awk 'NR == 1 { print $2 }' "$FAKE_CURL_LOG")" = 7 ]; then
  ok "proof fetches carry the configured finite deadline"
else
  bad "proof fetch omitted the configured finite deadline"
fi
if DEN_CURL_MAX_TIME=0 run_verify; then
  bad "a zero deadline disabled bounded proof I/O"
elif grep -q 'must be a positive integer' "$WORK/err"; then
  ok "the deadline cannot be disabled with zero"
else
  bad "a zero proof deadline failed without its configuration reason"
fi

rm -f "$ASSETS/$asset"; write_proof dataset.meta.json.bundle
run_verify && grep -q 'migration fallback' "$WORK/err" && ok "404 alone uses the fixed migration proof" || bad "fixed migration proof failed"

rm -f "$ASSETS/dataset.meta.json.bundle"; write_proof "$asset"
printf '{"datasetVersion":"altered"}\n' > "$MANIFEST"
mv "$ASSETS/$asset" "$ASSETS/dataset.meta.json.sha256-$(digest).bundle"
run_verify && bad "altered manifest was accepted" || ok "altered manifest cannot reuse an old proof"

rm -f "$ASSETS"/*; printf '{"datasetVersion":"old"}\n' > "$MANIFEST"
run_verify && bad "missing proofs were accepted" || ok "missing digest and fixed proofs are refused"

write_proof "$asset" https://github.com/oxyc/den-dataset/.github/workflows/fork.yml@refs/heads/main
run_verify && bad "wrong workflow identity was accepted" || ok "wrong workflow identity is refused"

rm -f "$ASSETS"/*; write_proof dataset.meta.json.bundle
FAKE_TRANSPORT_ERROR="$asset" run_verify && bad "transport failure downgraded to fixed proof" \
  || { grep -q 'transport failed' "$WORK/err" && ok "non-404 transport failure never falls back" || bad "transport failure reason missing"; }

# oxyc/den-dataset#314: a manifest published by hand with the legacy Ed25519 signer has no immutable proof and
# the fixed-name bundle describes an older manifest (cosign: "invalid signature when validating ASN.1 encoded
# signature"). Its signature, checked against the pinned key, is what lets the daily run start from it.
SIGN="$ROOT/pipeline/sign_manifest.py"
KEY="$WORK/signing.pem"
python3 "$SIGN" keygen "$KEY" >/dev/null
DEN_DATASET_PUBLIC_KEY="$(DEN_DATASET_SIGNING_KEY="$KEY" python3 "$SIGN" public-key)"
export DEN_DATASET_PUBLIC_KEY
legacy_manifest() {
  printf '{"datasetVersion":"live","taxonomyVersion":"t","storeSha256":"abc"}\n' > "$MANIFEST"
  DEN_DATASET_SIGNING_KEY="$KEY" python3 "$SIGN" sign "$MANIFEST" >/dev/null
}
# A fixed-name bundle for an older manifest: cosign (the stub) rejects it for these bytes.
stale_fixed_bundle() { printf '%s %s %s\n' "0000" "$identity" "$issuer" > "$ASSETS/dataset.meta.json.bundle"; }

rm -f "$ASSETS"/*; legacy_manifest; stale_fixed_bundle
if run_verify && grep -q 'legacy ed25519 signature verifies' "$WORK/err"; then
  ok "legacy-signed manifest with a stale fixed bundle is accepted, loudly"
else
  bad "legacy-signed manifest with a stale fixed bundle was refused: $(cat "$WORK/err")"
fi

rm -f "$ASSETS"/*; legacy_manifest
run_verify && ok "legacy-signed manifest with no proof at all is accepted" \
  || bad "legacy-signed manifest with no proof was refused: $(cat "$WORK/err")"

rm -f "$ASSETS"/*; legacy_manifest; stale_fixed_bundle
sed 's/"live"/"swapped"/' "$MANIFEST" > "$MANIFEST.tmp" && mv "$MANIFEST.tmp" "$MANIFEST"
if run_verify; then
  bad "a manifest altered after signing was accepted"
elif grep -q 'does not verify against the pinned dataset key' "$WORK/err"; then
  ok "altered legacy-signed manifest is refused with the signature reason"
else
  bad "altered manifest failed without the signature reason: $(cat "$WORK/err")"
fi

# An immutable proof that exists and fails is a refusal, never a downgrade to the legacy signature.
rm -f "$ASSETS"/*; legacy_manifest
write_proof "dataset.meta.json.sha256-$(digest).bundle" https://github.com/oxyc/den-dataset/.github/workflows/fork.yml@refs/heads/main
run_verify && bad "an invalid immutable proof downgraded to the legacy signature" || ok "invalid immutable proof never falls back to the legacy signature"

rm -f "$ASSETS"/*; legacy_manifest; stale_fixed_bundle
DEN_DATASET_PUBLIC_KEY="ed25519:$(printf '%032d' 0 | base64)" run_verify \
  && bad "a signature from a different key was accepted" || ok "legacy signature from a key other than the pinned one is refused"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
