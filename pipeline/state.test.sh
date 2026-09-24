#!/usr/bin/env bash
# `pipeline/state.sh` against a stub `gh` that keeps one release's assets in a directory: a save and a
# restore round-trip the out-dir, a second save leaves only its own parts, and a save of an empty out-dir is
# refused rather than written over the state.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "  ok: $1"; }
bad() { fail=$((fail + 1)); echo "  FAIL: $1" >&2; }

mkdir -p "$WORK/bin" "$WORK/release"
cat > "$WORK/bin/gh" <<'STUB'
#!/usr/bin/env bash
# Enough of `gh release` for state.sh: view, create, upload --clobber, download -p -D, delete-asset.
set -euo pipefail
rel="$RELEASE"
case "$2" in
  view) [ -f "$rel/.exists" ] || exit 1
        if [[ "$*" == *"--json assets"* ]]; then ls "$rel"; fi ;;
  create) touch "$rel/.exists" ;;
  upload) for a in "$@"; do [ -f "$a" ] && cp "$a" "$rel/"; done ;;
  download) p=""; d=""; prev=""
            for a in "$@"; do [ "$prev" = -p ] && p="$a"; [ "$prev" = -D ] && d="$a"; prev="$a"; done
            cp "$rel/$p" "$d/" ;;
  delete-asset) rm "$rel/$4" ;;
esac
STUB
chmod +x "$WORK/bin/gh"
export PATH="$WORK/bin:$PATH" RELEASE="$WORK/release" DEN_STATE_REPO=owner/state

src="$WORK/out"
mkdir -p "$src/enriched" "$src/index"
printf '[{"tmdbId":1}]' > "$src/enriched/batch-1.json"
printf 'vectors' > "$src/index/vectors.jsonl"
printf 'store' > "$src/den-abc.store"

STATE_RUN=1 bash "$HERE/state.sh" save "$src" >/dev/null && ok "saves" || bad "save failed"
bash "$HERE/state.sh" restore "$WORK/back" >/dev/null && ok "restores" || bad "restore failed"
diff -q "$src/enriched/batch-1.json" "$WORK/back/enriched/batch-1.json" >/dev/null \
  && diff -q "$src/index/vectors.jsonl" "$WORK/back/index/vectors.jsonl" >/dev/null \
  && ok "the out-dir comes back byte for byte" || bad "the restored out-dir differs"
[ ! -e "$WORK/back/den-abc.store" ] && ok "stores are left out: every run rebuilds them" || bad "a store was carried"

printf '[{"tmdbId":2}]' > "$src/enriched/batch-2.json"
STATE_RUN=2 bash "$HERE/state.sh" save "$src" >/dev/null
stale=("$RELEASE"/out-1.*)
[ ! -e "${stale[0]}" ] && [ "$(cat "$RELEASE/parts.txt")" = "out-2.tar.gz.aa" ] \
  && ok "a second save replaces the first's parts, and parts.txt names its own" || bad "stale parts: $(ls "$RELEASE")"

mkdir -p "$WORK/empty"
if bash "$HERE/state.sh" save "$WORK/empty" >/dev/null 2>&1; then bad "an empty out-dir was saved over the state"
else [ "$(cat "$RELEASE/parts.txt")" = "out-2.tar.gz.aa" ] && ok "an empty out-dir is refused, the state untouched" \
       || bad "refused, but the state moved"; fi

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
