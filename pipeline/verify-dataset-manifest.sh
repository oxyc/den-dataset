#!/usr/bin/env bash
# Authenticate the exact raw live-manifest bytes before any caller parses or uses them.
set -euo pipefail

[ "$#" -eq 1 ] || { echo "usage: $0 PATH/TO/dataset.meta.json" >&2; exit 2; }
manifest="$1"
[ -f "$manifest" ] || { echo "error: no manifest at $manifest" >&2; exit 1; }

repo="${DEN_DATASET_REPO:-oxyc/den-dataset}"
base="${DATASET_RELEASE_BASE:-https://github.com/$repo/releases/download/data-latest}"
curl_bin="${DEN_CURL:-curl}"
curl_max_time="${DEN_CURL_MAX_TIME:-60}"
case "$curl_max_time" in
  ''|*[!0-9]*) echo "error: DEN_CURL_MAX_TIME must be a positive integer" >&2; exit 2 ;;
esac
[ "$curl_max_time" -gt 0 ] \
  || { echo "error: DEN_CURL_MAX_TIME must be a positive integer" >&2; exit 2; }
cosign_bin="${DEN_COSIGN:-cosign}"
identity="https://github.com/oxyc/den-dataset/.github/workflows/daily.yml@refs/heads/main"
issuer="https://token.actions.githubusercontent.com"
digest="$(shasum -a 256 "$manifest" | cut -d' ' -f1)"
asset="dataset.meta.json.sha256-$digest.bundle"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Returns 44 only for a real HTTP 404. Transport failures and every other status are fatal and must not
# silently downgrade to the migration proof.
fetch() {
  local name="$1" out="$2" status
  if ! status="$("$curl_bin" -L --silent --show-error --max-time "$curl_max_time" \
      --output "$out" --write-out '%{http_code}' "$base/$name")"; then
    echo "error: transport failed while fetching $name" >&2; return 1
  fi
  case "$status" in
    200) return 0 ;;
    404) rm -f "$out"; return 44 ;;
    *) rm -f "$out"; echo "error: HTTP $status while fetching $name" >&2; return 1 ;;
  esac
}

cosign_verify() {
  "$cosign_bin" verify-blob --bundle "$1" --certificate-identity "$identity" \
    --certificate-oidc-issuer "$issuer" "$manifest" >/dev/null
}

# A manifest published by hand with the legacy Ed25519 signer (publish-dataset.sh without --keyless) has no
# digest-addressed proof, and the fixed-name bundle still describes the last keyless manifest. The signature
# in the manifest is checked against the dataset key every consumer pins, so the daily run can start from it
# and publish the next generation keyless, which restores a proof for the live bytes.
legacy_fallback() {
  local why="$1" python_bin="${DEN_PYTHON:-python3}"
  if "$python_bin" "$(dirname "$0")/sign_manifest.py" verify "$manifest" >/dev/null 2>"$tmp/legacy.err"; then
    echo "notice: $why; the manifest's legacy ed25519 signature verifies against the pinned dataset key" >&2
    echo "verified dataset.meta.json with its legacy ed25519 signature"
    exit 0
  fi
  echo "error: $why, and the legacy signature check failed: $(cat "$tmp/legacy.err")" >&2
  exit 1
}

proof="$tmp/$asset"
if fetch "$asset" "$proof"; then
  cosign_verify "$proof" \
    || { echo "error: the live manifest proof did not verify for the pinned daily workflow" >&2; exit 1; }
  echo "verified dataset.meta.json with $asset"
  exit 0
else
  status=$?
  [ "$status" -eq 44 ] || exit "$status"
fi

echo "notice: immutable proof absent; using the temporary fixed-name migration fallback" >&2
asset="dataset.meta.json.bundle"
proof="$tmp/$asset"
if fetch "$asset" "$proof"; then
  if cosign_verify "$proof"; then
    echo "verified dataset.meta.json with $asset"
    exit 0
  fi
  legacy_fallback "the live manifest ($digest) has no immutable proof and the fixed-name bundle was made for a different manifest"
else
  status=$?
  [ "$status" -eq 44 ] || exit "$status"
  legacy_fallback "no proof exists for the live manifest ($digest)"
fi
