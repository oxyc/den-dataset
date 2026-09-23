#!/usr/bin/env bash
# Publish the store as the den-dataset `data-latest` GitHub Release — the SINGLE SOURCE OF TRUTH for the
# dataset artifact, which den-atlas fetches. `data-latest` is a MOVING release: this clobbers its assets on
# every publish, so consumers always pull the current dataset.
#
# TWO FILES SHIP (oxyc/den#113): `den-<version>.store` and the `dataset.meta.json` describing it. The store
# carries what the per-blob artifacts carried — facts, labels, cards, facets, rail facets, the entity table,
# alias titles, and both vector matrices as sections — and atlas mmaps it. Everything else in the out-dir is
# an INPUT to that build and stays there; step 0 prunes their keys out of the manifest.
#
#   ./den stage finalize --out-dir out                           # labels-*.json + vectors-*.bin + manifest
#   python3 scripts/v2/build_store.py … --stamp-meta out/dataset.meta.json    # THE artifact
#   scripts/publish-dataset.sh [OUT_DIR]       # default: ./out, then ./data
#
# Requires `gh` authenticated with write access to the repo. The blobs are gitignored (large derived data),
# so they live as release assets, never in git.
#
# Run it FROM THE REPO ROOT: the ownership guard resolves producer paths (`pipeline/…`, `scripts/…`) and
# `git ls-files` against the working directory.
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

shopt -s nullglob
meta="$DIR/dataset.meta.json"

# 0) PRUNE — the release carries ONE artifact, `den-<ver>.store`, and the manifest that describes it
# (oxyc/den#113). The store holds what the per-blob artifacts held: facts, labels, cards, facets, rail
# facets, the entity table, alias titles, and both vector matrices as sections.
#
# Pruned here rather than never written, because the producers that write those keys are the producers that
# build the store's INPUTS — `finalize` writes `labels-t02.json` and declares it in the same pass, and
# `ManifestMerge` carries every unowned key forward. The blobs keep being built; they stop being published.
# The rule, and what it deliberately keeps, is in `prune-manifest.py`.
#
# This is also what removed the gzip pass that used to run here: a precompressed twin existed because atlas
# SERVED those JSON blobs to clients sending `Accept-Encoding: gzip`. Nothing is served from the release any
# more — atlas reads the store from disk and mmaps it, and a compressed file cannot be mapped.
retired="$(python3 "$(dirname "$0")/prune-manifest.py" --retired "$meta")"
if [ -n "$retired" ]; then
  # Keep the FIRST pre-cutover manifest and never clobber it.
  #
  # `facets*`, `premise*` and the premise scalars are UNOWNED: nothing in this repo produces them, and
  # they survived only because `finalize` merges over whatever meta was already in the out-dir. Once a
  # store-only publish has pruned them they are gone from the out-dir, so the next `finalize` re-owns
  # labels/vectors/metadata, makes `retired` non-empty again, and a `cp` here would overwrite the only
  # copy of those keys with a manifest that never had them. First write wins: that is the one holding the
  # contract that cannot be regenerated.
  if [ -e "$meta.prepublish" ]; then
    echo "keeping the existing $(basename "$meta").prepublish — it holds keys nothing can regenerate"
  else
    cp "$meta" "$meta.prepublish"
  fi
  # NOT a rollback on its own. It declares the LOCAL blob checksums, and a store-only publish uploads
  # none of those blobs — so re-uploading it alone hands the box a manifest whose shas do not match the
  # assets, and every sync tick then dies on `sha256sum -c`. It is exact only while the out-dir's blobs
  # are byte-identical to what is on the release. The rollback that works is the full asset restore in
  # oxyc/den `deploy/README.md` — blobs first, manifest last.
  echo "dropping from the manifest (the store carries what they held; the pre-cutover manifest is kept"
  echo "at $(basename "$meta").prepublish — for its KEYS, not as a rollback; see deploy/README.md):"
  echo "$retired" | sed 's/^/  /'
fi
python3 "$(dirname "$0")/prune-manifest.py" --prune "$meta"

# Every entry is a GLOB: a literal path is not subject to nullglob, so a missing file stayed in the array
# and the uploader failed on it three times with a message about an upload rather than a missing file.
#
# It is one glob now. The array decides what is *worth looking at* — the upload pass works from the
# MANIFEST — and the out-dir's other files (labels, vectors, metadata, premise, the TMDB daily-export dumps
# build-worklist.py drops in there) are the store's inputs, not publish candidates. Naming them here would
# print a "skipping" line for each on every publish, which is how a notice stops being read.
blobs=("$DIR"/den-*.store)
[ ${#blobs[@]} -ge 1 ] || { echo "error: no den-<version>.store in $DIR — it is the only artifact this release carries. Build it with scripts/v2/build_store.py --stamp-meta $meta" >&2; exit 1; }

# What actually publishes: the files the manifest names, plus whatever else the glob found that it does
# not (announced as such).
echo "publishing → $REPO data-latest:"
python3 -c '
import json, sys
meta = json.load(open(sys.argv[1]))
for key, name in sorted(meta.items()):
    if key.endswith("File") and name:
        print("  " + name)
' "$meta"
# A store the manifest does not name is the one thing the glob can still catch: `build_store.py` writes the
# file and only `--stamp-meta` declares it, so a run without that flag leaves an artifact here that no guard
# can see and atlas never loads.
for f in "${blobs[@]}"; do
  b="$(basename "$f")"
  grep -q "\"$b\"" "$meta" || echo "  $b (not named by the manifest)"
done
echo "  $(basename "$meta")"

# Create the release if it doesn't exist yet.
gh release view data-latest -R "$REPO" >/dev/null 2>&1 \
  || gh release create data-latest -R "$REPO" --title "Dataset (latest)" --notes "The published Den dataset artifact — the den-<version>.store den-atlas mmaps, and the manifest describing it."

# Upload one asset with retries. A single multi-file `gh release upload` is all-or-nothing: if it dies partway
# (the ~130 MB store is the usual culprit), it aborts and you can't tell what landed. Per-file keeps
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
# No STAGE writes facets.bin or the premise blobs. They have committed producers now
# (scripts/build-facets-bin.py, scripts/build-premise-tags.py, scripts/v2/embed_tags.py — see
# check-producers.py), but nothing in the run order calls them, so their manifest keys still survive only
# because `finalize` merges over the meta ALREADY IN THAT OUT-DIR. Finalizing into a FRESH dir (which is what the
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

# RECORD-COUNT GUARD. The blob-drop check below catches a whole FILE disappearing; it cannot see a file that
# is still declared but has lost rows. That happened: a facts rebuild built its records from the scrape
# checkpoint and silently dropped the 137 facts-only delta titles — exactly the records nothing else covers
# (no labels, no vectors, no facets row), so the loss was invisible from every other artifact and the only
# symptom was /recommend quietly losing library titles. Counting is the cheap check that catches it, and the
# same guard covers every future JSON blob.
#
# `<key>Records` is written into the manifest below, so the NEXT publish has something to compare against.
if [ "$have_published" -eq 1 ]; then
  shrunk="$(python3 "$(dirname "$0")/manifest-counts.py" --compare "$published_meta" "$meta" "$DIR")"
  if [ -n "$shrunk" ]; then
    echo "error: a published blob would lose records, or fall behind the corpus:" >&2
    echo "$shrunk" | sed 's/^/       /' >&2
    echo "       A file can stay declared and still lose rows — and it can keep every row it has while" >&2
    echo "       the corpus grows past it, which is how facets.bin fell 999 titles behind. Both are" >&2
    echo "       checked here; a 'coverage' line is the second kind." >&2
    echo "       If it is deliberate, set DEN_ALLOW_DROPPING_BLOBS=1." >&2
    [ "${DEN_ALLOW_DROPPING_BLOBS:-0}" = "1" ] || exit 1
  fi
fi

# IDENTITY GUARD. A `datasetVersion` must name ONE store, forever.
#
# It already failed to. Two different stores shipped as `den-5b1c3213b6a1.store` under datasetVersion
# 5b1c3213b6a1 with the same `builtAt` — 131,159,594 bytes (539beddf…) and then 135,146,098 (caa915c6…).
# Nothing caught it and nothing could: `check-filename-version.py` compares the version in the filename to
# the version in the manifest, and both agreed; `den-atlas check`'s mixed-generation test asks whether the
# artifacts share a datasetVersion, and they did. Every guard we had keyed on the identifier, and the
# identifier was the thing that had stopped being unique.
#
# The consequence is that a rollback cannot name what it restores, and a backup cannot prove it holds the
# generation it claims. With the store as the ONLY artifact, that identifier is the only handle anything
# has on it.
#
# So: republishing a version whose store bytes changed is refused unless you SAY WHY.
#
# It cannot simply demand a new version. `datasetVersion` names the GENERATION — the corpus — and
# `check-filename-version.py` requires every versioned filename to carry it, so a store rebuilt from an
# unchanged corpus by a fixed writer legitimately keeps the same version. That is almost certainly what
# produced the two stores above: sections were added, the store was rebuilt, the corpus never moved.
#
# The fault was never that it happened. It was that it happened SILENTLY. So `DEN_STORE_REBUILD=<reason>`
# lets it through and prints the reason, and the release carries it as `storeRebuild` — the generation
# stays honest, and the fact that its store was replaced is written down where a rollback can read it.
#
# Deliberately NOT DEN_ALLOW_DROPPING_BLOBS: that flag is for a deliberate shrink, it suppresses three
# guards at once, and "I meant to drop records" is not "I meant to replace the artifact".
if [ "$have_published" -eq 1 ]; then
  json_key() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$1" "$2"; }
  was_version="$(json_key "$published_meta" datasetVersion)"
  now_version="$(json_key "$meta" datasetVersion)"
  was_store="$(json_key "$published_meta" storeSha256)"
  now_store="$(json_key "$meta" storeSha256)"
  # Both sides must actually NAME a store. A manifest with no `storeSha256` at all is a dropped key, not a
  # collision, and the guard above reports that far more usefully than "a different store" would.
  if [ -n "$was_version" ] && [ "$was_version" = "$now_version" ] \
     && [ -n "$was_store" ] && [ -n "$now_store" ] && [ "$was_store" != "$now_store" ]; then
    if [ -n "${DEN_STORE_REBUILD:-}" ]; then
      echo "store rebuilt within datasetVersion $now_version: ${DEN_STORE_REBUILD}"
      echo "  was $was_store"
      echo "  now $now_store"
      python3 - "$meta" "$DEN_STORE_REBUILD" <<'PY'
import json, sys
meta_path, reason = sys.argv[1], sys.argv[2]
meta = json.load(open(meta_path))
# Recorded IN the manifest, so the release itself says its store was replaced. A rollback reading only
# the assets would otherwise have no way to tell this generation's store from the one it superseded.
meta["storeRebuild"] = reason
json.dump(meta, open(meta_path, "w"), indent=2, sort_keys=True)
PY
    else
      echo "error: datasetVersion $now_version is already published with a DIFFERENT store." >&2
      echo "       published:        $was_store" >&2
      echo "       about to publish: $now_store" >&2
      echo "       One version must name one store, or a rollback cannot name what it restores — and" >&2
      echo "       two stores have already shipped under one version, undetected, because every other" >&2
      echo "       guard here keys on that identifier." >&2
      echo "       If the corpus is unchanged and you rebuilt the store deliberately, say so:" >&2
      echo "         DEN_STORE_REBUILD='what changed in the writer' scripts/publish-dataset.sh $DIR" >&2
      echo "       If the CORPUS changed, it is a new generation and wants a new datasetVersion." >&2
      exit 1
    fi
  fi
fi

# Stamp the counts for next time, whether or not there was anything to compare against.
python3 "$(dirname "$0")/manifest-counts.py" --stamp "$meta" "$DIR"

# GROUNDING GUARD (oxyc/den-dataset#16). Every check above asks whether the right rows arrived, in the right
# shape, from the right producer, for this generation. None of them asks whether a row is about the title it
# is filed under.
#
# 1,066 titles in the shipped generation are grounded on a Wikipedia article that also grounds another title
# — five of them on the Wuthering Heights NOVEL, whose literary criticism is not the plot of any adaptation.
# They get that article's labels, facets and premise, so they are described by a story they do not tell. The
# cause was the source-work fallback keeping the longest of [own article, P144 source work]: one novel
# outweighed every adaptation's own article, so all of them inherited it. `reground` (pipeline/enrich.py) now
# reads the source work only for a title with no plot of its own. `check-plot-invariants.py` documents the
# mechanism and what it deliberately does not check.
#
# WARNS on the standing count and REFUSES an increase, which is the record-count guard's shape rather than
# the identity guard's: an absolute floor of zero would refuse every publish over a defect that has already
# shipped, and a gate like that gets switched off — the reasoning `check-plot-invariants.py` was written with,
# and the reason the quality gate below ratchets from the shipped scores. What must not happen silently is the number
# going UP, because the only thing that makes it worse is a re-ground, and #27 proposes running one daily.
#
# What it COUNTS is the borrowers: 336 of the 1,066 are the article's own subject and appear only because
# someone took their page, and a title whose recorded provenance cannot clear it counts too. Every row in
# the shipped generation predates that recording, so the count is unchanged at 1,066 until a re-enrich —
# at which point the per-title provenance census, printed alongside, names the ~2,075 mis-grounded titles
# directly instead of the 729 of them that happen to collide.
#
# `--facts` is deliberately NOT passed. It would enable the one-item exemption (31 titles are one Wikidata
# item behind several TMDB ids, which is why this can never ratchet to zero) but it also scopes the census
# to the shipped keys, which moves the baseline for a reason that has nothing to do with the grounding.
#
# `maxBatchId` is stamped just above, so the census is bounded to the batches this publish can actually see
# rather than to a live directory that keeps growing.
plot_guard=0
if [ -d "$DIR/enriched" ]; then
  plot_labels="$DIR/labels-$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1])).get("taxonomyVersion") or "")
' "$meta").json"
  if [ -f "$plot_labels" ]; then
    # Absent on the published side means this is the first publish since the guard existed: census only,
    # and the stamp below gives the NEXT one something to ratchet against.
    baseline=""
    if [ "$have_published" -eq 1 ]; then
      baseline="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1])).get("sharedPlotArticleTitles", ""))
' "$published_meta")"
    fi
    plot_args=(--enriched-dir "$DIR/enriched" --labels "$plot_labels" --stamp-meta "$meta")
    max_batch="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1])).get("maxBatchId", ""))
' "$meta")"
    [ -n "$max_batch" ] && plot_args+=(--max-batch-id "$max_batch")
    [ -n "$baseline" ] && plot_args+=(--shared-plot-baseline "$baseline")
    python3 "$(dirname "$0")/check-plot-invariants.py" "${plot_args[@]}" || plot_guard=$?
    # 2 is the regression; 1 is the pre-existing plot/labels mismatch this script has always reported.
    if [ "$plot_guard" -eq 2 ]; then
      if [ -n "${DEN_ALLOW_SHARED_PLOTS:-}" ]; then
        echo "shared-article grounding regressed deliberately: ${DEN_ALLOW_SHARED_PLOTS}"
        python3 - "$meta" "$DEN_ALLOW_SHARED_PLOTS" <<'PY'
import json, sys
meta_path, reason = sys.argv[1], sys.argv[2]
meta = json.load(open(meta_path))
# In the manifest, so the release itself says its grounding got worse and why. A later reader comparing
# two generations' counts would otherwise find an unexplained jump and no record of who accepted it.
meta["sharedPlotArticlesWaived"] = reason
json.dump(meta, open(meta_path, "w"), indent=1, sort_keys=True)
PY
      else
        echo "       Nothing uploaded." >&2
        exit 1
      fi
    fi
  else
    echo "grounding guard: SKIPPED — $plot_labels is not in $DIR. The labels are no longer published, but" >&2
    echo "                 they are still the store's input and scope the census to what shipped." >&2
  fi
else
  echo "grounding guard: SKIPPED — no $DIR/enriched. plotArticle lives only in the enriched batches, so" >&2
  echo "                 the census cannot be taken from a publish dir that does not carry them." >&2
fi

# OWNERSHIP GUARD. Every published artifact must have a producer committed in this repo. The record-count
# guard above catches a blob that LOSES rows; it cannot see one that was never rebuilt at all, because its
# count simply never moves. That is the failure that has now happened twice: facets.bin fell 999 titles behind
# the corpus, and facts-slim kept shipping the field set atlas parsed years ago — both generated once,
# elsewhere, and carried forward by every publish since.
#
# It also checks WHAT THE STORE WAS BUILT FROM. The prune above leaves one blob key, so the loop over the
# manifest covers the store and nothing else — while the labels, vectors, metadata, facts, corpus and
# enriched batches it reads are still built, just no longer declared. A store built from a stale one of
# those passed every guard here. `build_store.py --stamp-meta` records each input's path, sha256, size
# and mtime into `storeInputs`, and this re-hashes them against the tree (a changed input REFUSES, with
# DEN_ALLOW_STALE_STORE_INPUTS=1 as the deliberate override) and holds each input's producer to the
# recorded mtime (a warning, like the staleness warning above).
python3 "$(dirname "$0")/check-producers.py" "$meta" "$DIR"

# DEAD-GENERATION GUARD. Every guard above asks whether a blob is present, parseable, owned and the right
# size. None of them asks whether it belongs to THIS generation. The producer stamps the version into the
# filename, so a blob nobody rebuilt keeps the old one and is declared beside current artifacts while
# every other check passes — which is how `plot-facets-c85c707b0b18.json` came to be served under
# datasetVersion 5b1c3213b6a1.
python3 "$(dirname "$0")/check-filename-version.py" "$meta"

# INTERNAL-CONSISTENCY GUARD. Every check above asks whether a blob is right. This asks whether the
# manifest's own NUMBERS are — the ones den-atlas serves to the app in /dataset.json. `premiseCount` sat
# at 38,532 for months while the premise labels and vectors both held 44,531, because nothing models that
# key, so ManifestMerge carried it forward and no counter ever looked at it.
inconsistent="$(python3 "$(dirname "$0")/manifest-counts.py" --consistent "$meta" "$DIR")"
if [ -n "$inconsistent" ]; then
  echo "error: the manifest contradicts the files it describes:" >&2
  echo "$inconsistent" | sed 's/^/       /' >&2
  exit 1
fi

# ALIAS GATE. An alias is a Wikidata altLabel, and one that is really another title's name puts this title
# at the top of a search for that name — Taxi Driver carried "Alien" and ranked second for it. No rule tells
# a wrong one from a real release title, so a person decides each collision in data/alias-decisions.json and
# the store build applies them. This refuses a store that ships a collision nobody decided, or that applied
# decisions other than the committed ones; the refusal lists the steps to decide one.
python3 "$(dirname "$0")/check-alias-collisions.py" --gate "$meta" \
  || { echo "       Nothing uploaded." >&2; exit 1; }

# WIKIDATA ITEM GATE. Where several Wikidata items claim one TMDB id and nothing chooses between them, the
# title is written with no Wikidata fields rather than two works mixed, so it has no card. This refuses a
# store that ships one; the refusal points at data/wikidata-item-decisions.json.
python3 "$(dirname "$0")/check-wikidata-items.py" --gate "$meta" \
  || { echo "       Nothing uploaded." >&2; exit 1; }

# AWARD MERGE GATE. Some awarding bodies reach the corpus as two ceremonies (an organisation and its
# "Awards" group), and data/award-ceremony-merges.json joins each pair by hand. This refuses a store built
# from another list, or with an entry whose ceremony no title names any more.
python3 "$(dirname "$0")/check-award-merges.py" --gate "$meta" \
  || { echo "       Nothing uploaded." >&2; exit 1; }

# SHAPE GUARD. A producer can exist, be committed, be run correctly — and still emit a shape its consumer
# cannot read. One entity carrying `"aliases": "Adrian Anthony Lester"` where atlas types Vec<String> made a
# 27 MB facts file unparseable at its first entity; atlas does not partially load one, so it dropped the
# whole file and ran facts_unusable, losing people search, imdbId, countries and /recommend. Record counts,
# shas, gzip and the ownership guard all passed. Nothing read it the way atlas does.
#
# DORMANT: the prune above retires `factsFile`, so this loop visits nothing. The facts file is now a store
# INPUT rather than a published artifact — `build_store.py` reads it and fails loudly on a shape it cannot
# use — and what atlas parses is the store. Kept because a guard costs nothing while it has nothing to do,
# and re-arms itself the day a generation publishes facts beside the store again.
for facts_key in factsFile factsSlimFile; do
  facts_name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$meta" "$facts_key")"
  [ -n "$facts_name" ] || continue
  python3 "$(dirname "$0")/check-facts-schema.py" "$DIR/$facts_name"
done

# QUALITY GATE. Every check above asks whether the right number of records arrived in the right shape.
# This one asks whether they are CORRECT: the committed golden set, scored against these labels, per label
# family, against the baseline in `data/eval/quality-floors.json`.
#
# It moved here from the tvOS app (`ShippedDatasetEvalTests`), which scored the 45.8 MB index that app
# bundled. oxyc/den#113 Phase 2 deletes that bundle, and the only quality signal in the whole system would
# have gone with it as a side effect of a delivery change.
#
# ENFORCED, as a ratchet. The baseline is the scores of the labels that ship, recorded with the date and
# the labels' sha256, and a publish whose labels score more than the recorded tolerance under it is
# refused. The fixed floors this replaced sat above the shipped mood scores (micro .639 vs .640, macro
# .564 vs .580), so enforcing them would have refused a publish of unchanged labels, and the gate ran as a
# report that nothing acted on. Accepting a drop is `eval-taxonomy.py --record --accept-drop` and a
# commit, which the refusal prints.
#
# The labels are read from the OUT-DIR, not from `labelsFile` — the prune above retires that key, and
# reading it here would hand the scorer an empty path. `finalize` names the file after the taxonomy (`labels-<tax>.json`)
# and rewrites it in place every run, so the name is derivable and carries no version to go stale; naming it
# by glob instead would also match `labels-cc0.json`, the experimental index that scores a different corpus.
labels="$DIR/labels-$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1])).get("taxonomyVersion") or "")
' "$meta").json"
if [ -f "$labels" ]; then
  if ! python3 "$(dirname "$0")/eval-taxonomy.py" "$labels" --gate \
       --golden "$(dirname "$0")/../data/eval/golden-large.json"; then
    echo "       quality gate: the labels in $DIR score below what ships. Nothing uploaded." >&2
    exit 1
  fi
else
  echo "quality gate: SKIPPED — $labels is not in $DIR. The labels are no longer published, but they are" >&2
  echo "              still the store's input and the only thing the golden set can be scored against." >&2
fi

if [ "$have_published" -eq 1 ]; then
  # A key the prune RETIRES is not a dropped blob — it is the cutover, and it happens on every publish
  # until the published manifest is a pruned one too. Filtering them here rather than reaching for
  # DEN_ALLOW_DROPPING_BLOBS=1 matters: that override also disables the record-count/coverage guard and the
  # "could not read the published manifest" guard, so the one publish that changes what the release carries
  # would be the publish that checked the least. The retired set comes from `prune-manifest.py` itself, so
  # there is one answer to "is this key still published", not two that can disagree.
  dropped="$(python3 -c '
import json, sys
old = json.load(open(sys.argv[1]))
new = json.load(open(sys.argv[2]))
retired = set(sys.argv[3].split())
print(" ".join(sorted(k for k, v in old.items()
                      if k.endswith("File") and v and k not in retired and not new.get(k))))
' "$published_meta" "$meta" "$(python3 "$(dirname "$0")/prune-manifest.py" --retired "$published_meta" | tr '\n' ' ')")"
  if [ -n "$dropped" ]; then
    echo "error: the published manifest declares files this one does not: $dropped" >&2
    echo "       Publishing would make den-atlas delete them." >&2
    echo "" >&2
    # The recipe that used to stand here — copy the published manifest back, re-run `finalize`, then
    # `metadata` — restored the keys that are now retired, so following it would reinstate a declaration the
    # prune removes on the next line. What is left is the store, and the store has one producer.
    echo "       Every retired blob is already filtered out above, so what remains is an artifact the" >&2
    echo "       release still carries — in practice the store. Build it, which also declares it:" >&2
    echo "" >&2
    echo "          python3 scripts/v2/build_store.py … --out $DIR/den-<version>.store --stamp-meta $meta" >&2
    echo "" >&2
    echo "       If dropping them is deliberate, set DEN_ALLOW_DROPPING_BLOBS=1." >&2
    [ "${DEN_ALLOW_DROPPING_BLOBS:-0}" = "1" ] || exit 1
    echo "       DEN_ALLOW_DROPPING_BLOBS=1 — continuing." >&2
  fi
fi

# 3) BLOBS — driven by the MANIFEST, not by a parallel list of globs.
#
# The glob above decides what is *worth looking at*; the manifest decides what actually ships. When those
# two lists were separate, a declared file whose name matched no glob was hash-checked locally, never
# uploaded, and still passed step 4 — because `data-latest` is a moving release and the previous publish's
# same-named asset satisfies a name check. Consumers would then verify a stale blob against a new sha and
# wedge. That is why this loop reads the manifest and the glob is only ever a second opinion.
while read -r name _sha; do
  [ -z "$name" ] && continue
  echo "→ $name"
  upload_one "$DIR/$name" || exit 1
done < "$manifest_files"

# Anything the globs found that the manifest does not name is SKIPPED, not uploaded.
#
# The manifest is the contract. A file it does not name has no declared hash, no record count, no producer
# and no consumer: none of the guards above can see it, and nothing fetches it. Uploading it anyway grew
# `data-latest` to 458 MB, of which 170 MB was a previous datasetVersion's facts/labels/metadata plus build
# intermediates (facts-merged, facts-fields, facts-entities, facts-unversioned, labels-t02.pre-classify,
# labels-t02.stamped). They were re-clobbered on every publish for as long as they sat in the out-dir.
#
# This was already the rule for metadata-*.json alone, for exactly this reason; it is now the rule for
# everything. An artifact worth publishing is worth declaring — add it to the manifest and give it a
# producer, and the guards will cover it.
#
# Assets already on the release are NOT removed by this (a publish only ever adds or clobbers). Clearing
# the ones that predate this rule is a one-off, done by hand — and that includes the artifacts the prune
# retires: the first store-only publish stops NAMING labels/vectors/metadata/premise/facets, it does not
# delete them from `data-latest`. Consumers stop fetching them (they follow the manifest), and the assets
# are deleted by hand afterwards with `gh release delete-asset`.
for f in "${blobs[@]}"; do
  base="$(basename "$f")"
  grep -q "^$base " "$manifest_files" && continue
  echo "  skipping $base (the manifest does not name it)"
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
# den-atlas is the only thing that fetches the release. The Den app does not: it reads den-atlas's
# /dataset.json and queries den-atlas for everything the store holds.
echo "done — consumer: den-atlas (scripts/fetch-dataset.sh). The Den app reads it through den-atlas's /dataset.json."
