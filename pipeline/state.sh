#!/usr/bin/env bash
# The daily job's out-dir, carried between runs in a PRIVATE repository's release (oxyc/den-dataset#27).
#
#   pipeline/state.sh save    OUT_DIR    # the out-dir → the `state` release of $DEN_STATE_REPO
#   pipeline/state.sh restore OUT_DIR    # the other way
#
# Private because the out-dir is not publishable: enriched batches from before oxyc/den-dataset#53 carry
# TMDB's overviews. That is also why it is never put in this public repository's Actions cache, which a pull
# request's workflow can restore. `gh` must be authenticated for $DEN_STATE_REPO (GH_TOKEN).
#
# The archive is split under the 2 GB asset limit. The parts are named by $STATE_RUN (the workflow's run id,
# or the time) and `parts.txt` is uploaded LAST, so it is the commit point: a save that dies part-way leaves
# the previous parts.txt naming the previous parts, which are deleted only after the new list is up. What is
# left out is rebuilt by every run: the stores, and the response cache, which lives outside the out-dir.
set -euo pipefail

mode="${1:-}"
out="${2:-}"
[ -n "$mode" ] && [ -n "$out" ] || { echo "usage: pipeline/state.sh save|restore OUT_DIR" >&2; exit 2; }
repo="${DEN_STATE_REPO:?set DEN_STATE_REPO to the private repository that holds the state}"
tag=state
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

case "$mode" in
  restore)
    gh release download "$tag" -R "$repo" -p parts.txt -D "$work"
    while read -r part; do
      [ -n "$part" ] && gh release download "$tag" -R "$repo" -p "$part" -D "$work"
    done < "$work/parts.txt"
    mkdir -p "$out"
    # shellcheck disable=SC2046  # one word per part, in order
    cat $(sed "s|^|$work/|" "$work/parts.txt") | gzip -dc | tar -x -C "$out"
    echo "restored $out from $repo ($(wc -l < "$work/parts.txt") part(s))"
    ;;
  save)
    [ -d "$out/enriched" ] || { echo "error: $out holds no enriched batches; refusing to save it over the state" >&2; exit 1; }
    run="${STATE_RUN:-$(date -u +%Y%m%dT%H%M%SZ)}"
    tar -C "$out" --exclude='./den-*.store' -cf - . | gzip -6 | split -b 1900m - "$work/out-$run.tar.gz."
    (cd "$work" && ls out-"$run".tar.gz.* > parts.txt)
    gh release view "$tag" -R "$repo" >/dev/null 2>&1 \
      || gh release create "$tag" -R "$repo" --title "den-dataset out-dir" --notes "The daily job's state. Private."
    old="$(gh release view "$tag" -R "$repo" --json assets -q '.assets[].name')"
    while read -r part; do
      gh release upload "$tag" -R "$repo" --clobber "$work/$part"
    done < "$work/parts.txt"
    gh release upload "$tag" -R "$repo" --clobber "$work/parts.txt"
    while read -r asset; do
      [ -z "$asset" ] || [ "$asset" = parts.txt ] || grep -qxF "$asset" "$work/parts.txt" \
        || gh release delete-asset "$tag" "$asset" -R "$repo" -y
    done <<< "$old"
    echo "saved $out to $repo ($(wc -l < "$work/parts.txt") part(s))"
    ;;
  *) echo "usage: pipeline/state.sh save|restore OUT_DIR" >&2; exit 2 ;;
esac
