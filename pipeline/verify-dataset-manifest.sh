#!/usr/bin/env bash
# Authenticate the exact raw live-manifest bytes before any caller parses or uses them.
set -euo pipefail

[ "$#" -eq 1 ] || { echo "usage: $0 PATH/TO/dataset.meta.json" >&2; exit 2; }
manifest="$1"
[ -f "$manifest" ] || { echo "error: no manifest at $manifest" >&2; exit 1; }

repo="${DEN_DATASET_REPO:-oxyc/den-dataset}"
base="${DATASET_RELEASE_BASE:-https://github.com/$repo/releases/download/data-latest}"
curl_bin="${DEN_CURL:-curl}"
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
  if ! status="$("$curl_bin" -L --silent --show-error --output "$out" --write-out '%{http_code}' "$base/$name")"; then
    echo "error: transport failed while fetching $name" >&2; return 1
  fi
  case "$status" in
    200) return 0 ;;
    404) rm -f "$out"; return 44 ;;
    *) rm -f "$out"; echo "error: HTTP $status while fetching $name" >&2; return 1 ;;
  esac
}

proof="$tmp/$asset"
if fetch "$asset" "$proof"; then
  :
else
  status=$?
  [ "$status" -eq 44 ] || exit "$status"
  asset="dataset.meta.json.bundle"
  proof="$tmp/$asset"
  echo "notice: immutable proof absent; using the temporary fixed-name migration fallback" >&2
  if fetch "$asset" "$proof"; then :; else
    status=$?; [ "$status" -eq 44 ] && echo "error: no proof exists for the live manifest" >&2
    exit "$status"
  fi
fi

"$cosign_bin" verify-blob --bundle "$proof" --certificate-identity "$identity" \
  --certificate-oidc-issuer "$issuer" "$manifest" >/dev/null \
  || { echo "error: the live manifest proof did not verify for the pinned daily workflow" >&2; exit 1; }
echo "verified dataset.meta.json with $asset"
