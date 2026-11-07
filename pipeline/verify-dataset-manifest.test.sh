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
out=""; url=""; prev=""
for arg in "$@"; do
  [ "$prev" = "--output" ] && out="$arg"
  prev="$arg"; [[ "$arg" = http* ]] && url="$arg"
done
name="${url##*/}"
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
export PATH="$BIN:$PATH" FAKE_ASSETS="$ASSETS" DATASET_RELEASE_BASE=https://fixture.invalid/data-latest
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

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
