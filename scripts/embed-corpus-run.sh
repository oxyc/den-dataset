#!/usr/bin/env bash
# Build the bge-m3 vector corpus SAFELY (the "semantic vectors now" path). Safety layers:
#   1. den-embed runs with a bounded per-request batch + token cap so a single request can't spike ONNX
#      activation memory.
#   2. embed-corpus streams enriched batches (one in memory at a time) + small --chunk requests + is RESUMABLE.
#   3. SEGMENTS: den-embed is restarted fresh every SEGMENT titles. ONNX Runtime's memory arena grows to its
#      peak and never shrinks, so a long-lived process creeps up (this is what OOM'd the machine before).
#      Restarting per segment reclaims it; embed-corpus resumes from its store, so no work is lost.
#   4. WATCHDOG: a segment that dies (OOM/hang) is retried after a fresh restart.
# Idempotent — safe to Ctrl-C and re-run.
#
#   scripts/embed-corpus-run.sh out-t02/labels-t02.json
#
# RUNS THE PUBLISHED CONTAINER, not a local checkout. den-embed was rewritten in Rust at 5cf9e72 and its
# server.py / run.sh / .venv are gone; this script still booted `uvicorn server:app`, so the whole
# whole-corpus re-embed path was dead — `boot_embed` failed instantly, the health poll burned 40 × 3 s, and
# the retry loop repeated that ~200 times (about seven hours) before exiting 1 with nothing written.
#
# The container is also the RIGHT thing to run: the model is baked into the image at a pinned revision, so
# the corpus is embedded by exactly the runtime the box serves queries with. That alignment is the invariant
# the whole retrieval design rests on (docs/OPERATE.md) — embedding the corpus with a hand-built binary and
# serving queries from the image is precisely how it silently breaks.
#
# WHAT THIS COSTS, STATED PLAINLY: the Rust service truncates at 512 tokens and the Python one it replaced
# had no token cap, so documents re-embedded here are shorter than the ones in the corpus shipping today.
# Raising the cap is not an option — den-embed's ceiling is 1024 tokens because peak RSS is 1219 MB there
# and 1598 MB at 2048, against a 1536 MB cgroup. So PLOT_CAP is set to fit instead, and `embed-corpus`
# refuses outright if it does not: a re-embed changes the corpus, and that has to be a decision rather than
# something discovered later in the retrieval quality. The alignment that matters still holds, because
# queries go through this same service.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
# shellcheck source=scripts/lib/den-env.sh
. scripts/lib/den-env.sh

LABELS="${1:?usage: embed-corpus-run.sh <existing labels-t02.json>}"
OUT_DIR="${OUT_DIR:-out-vecnow}"
ENRICHED_DIR="${ENRICHED_DIR:-out/enriched}"
# Docs per /embed/batch request. Bounded by den-embed's max_request_tokens (8192) against MAX_TOKENS per
# doc: 8192/512 = 16, and 15 leaves a margin.
#
# There is deliberately NO DEN_EMBED_MAX_BATCH here. It reads like a server-side micro-batch and is not one
# — den-embed's embed_many maps embed_one SERIALLY, so it bounds no memory whatsoever; it is purely a
# rejection threshold, returning 413 when a request carries more texts than it allows. Setting it to 8 while
# sending 15 docs meant every single request was rejected, and Transport treats 413 as definitive, so the
# whole-corpus re-embed failed on its first flush having written nothing. MAX_TOKENS is the actual memory
# bound, because inference is one document at a time.
CHUNK="${CHUNK:-15}"
MAX_TOKENS="${MAX_TOKENS:-512}"
# The plot cap must fit MAX_TOKENS or den-embed truncates the document server-side and says nothing — see
# `assertDocFits`, which refuses rather than letting that happen. 1500 also matches what `assemble` composes,
# which matters because both append to the same store.
PLOT_CAP="${PLOT_CAP:-1500}"
SEGMENT="${SEGMENT:-5000}"            # titles per den-embed lifetime, then restart it fresh
IMAGE="${DEN_EMBED_IMAGE:-ghcr.io/oxyc/den-embed:latest}"
RUNTIME="${DEN_EMBED_RUNTIME:-podman}"
PORT="${DEN_EMBED_PORT:-8791}"
NAME="den-embed-corpus-$$"
# Under the out-dir, not /tmp: predictable world-writable paths, and the logs belong with the run anyway.
EMBED_LOG="${EMBED_LOG:-$OUT_DIR/den-embed.log}"
ERR_LOG="${ERR_LOG:-$OUT_DIR/embed-corpus.err}"

export DEN_EMBED_URL="http://127.0.0.1:$PORT"

[ -f "$LABELS" ] || { echo "missing labels file: $LABELS"; exit 1; }
mkdir -p "$OUT_DIR"
command -v "$RUNTIME" >/dev/null || { echo "no $RUNTIME on PATH — set DEN_EMBED_RUNTIME=docker"; exit 1; }
# Checked UP FRONT, not when it is needed: the `metadata` step at the end calls TMDB, and a multi-hour
# unattended re-embed that finalizes and then exits on a missing key has wasted the whole run's tail.
# `|| exit 1` because this script runs without -e on purpose.
den_load_env || exit 1

# Kill by CONTAINER NAME, not by process pattern. The old `pkill -f "uvicorn server:app"` was a machine-wide
# pattern kill wired to the EXIT trap, so it could take out an unrelated uvicorn the operator was running.
stop_embed() { "$RUNTIME" rm -f "$NAME" >/dev/null 2>&1 || true; }

boot_embed() {
  stop_embed
  echo "booting fresh den-embed ($IMAGE, max_tokens=$MAX_TOKENS) …"
  "$RUNTIME" run -d --rm --name "$NAME" \
    -e DEN_EMBED_MAX_TOKENS="$MAX_TOKENS" \
    -e DEN_EMBED_HOST=0.0.0.0 \
    -p "127.0.0.1:$PORT:8080" "$IMAGE" >/dev/null 2>>"$EMBED_LOG" || {
      echo "could not start $IMAGE (see $EMBED_LOG)"; return 1; }
  # Probe /embed, not /health: /health is a constant that answers ok while the model is missing and every
  # /embed 500s, so a health gate would wave a completely broken container straight through. The first
  # request also loads a 555 MB model, hence the patience.
  for _ in $(seq 1 40); do
    if curl -fsS -m 20 -H 'content-type: application/json' \
         -d '{"text":"corpus embed readiness probe"}' "$DEN_EMBED_URL/embed" 2>/dev/null | grep -q '"dims"'; then
      return 0
    fi
    sleep 3
  done
  echo "den-embed did not serve (see $EMBED_LOG)"
  "$RUNTIME" logs --tail 20 "$NAME" 2>&1 | sed 's/^/    /'
  return 1
}

swift build -c release >/dev/null || { echo "build failed"; exit 1; }
BIN=.build/release/taxonomy-backfill
trap stop_embed EXIT
fails=0
MAX_FAILS="${MAX_FAILS:-6}"
: > "$ERR_LOG"    # fresh per run, so `tail` below cannot report a previous run's failure as this one's

for attempt in $(seq 1 200); do
  if ! boot_embed; then
    # Counts against MAX_FAILS like any other failure. It did not, so the cap bounded only embed-corpus's
    # own refusals while a container that boots but never serves still burned 200 x 40 probes — the very
    # seven-hour no-op the cap was added to end.
    fails=$((fails + 1))
    stop_embed
    [ "$fails" -ge "$MAX_FAILS" ] && { echo "$MAX_FAILS consecutive boot failures — stopping"; exit 1; }
    sleep $((fails * 15))
    continue
  fi
  # One segment: embed up to SEGMENT new titles, then exit so den-embed can be recycled.
  out=$("$BIN" embed-corpus --labels "$LABELS" --enriched-dir "$ENRICHED_DIR" --out-dir "$OUT_DIR" \
        --chunk "$CHUNK" --plot-cap "$PLOT_CAP" --limit "$SEGMENT" 2>>"$ERR_LOG") || {
    # Not every failure is a dead container. A refusal from embed-corpus itself — a mixed embedder, a plot
    # cap the service would truncate, a missing binary — is DETERMINISTIC, and retrying it 200 times boots
    # the 555 MB model 200 times to reach the same answer. That was the shape of the seven-hour no-op this
    # script used to be; the trigger was fixed and the amplifier was not.
    fails=$((fails + 1))
    echo "segment $attempt failed (attempt $fails of $MAX_FAILS) — see $ERR_LOG"
    tail -3 "$ERR_LOG" | sed "s/^/    /"
    stop_embed
    [ "$fails" -ge "$MAX_FAILS" ] && { echo "$MAX_FAILS consecutive failures — stopping"; exit 1; }
    sleep $((fails * 15))
    continue
  }
  fails=0
  echo "$out"
  # A parse failure must NOT read as "written: 0" → "everything is embedded" → finalize a PARTIAL store and
  # exit 0. Distinguish the two: empty means unparseable, and that is a hard stop.
  written=$(printf '%s' "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin)["written"])' 2>/dev/null)
  [ -n "$written" ] || { echo "could not read 'written' from embed-corpus output — stopping"; exit 1; }
  rss=$("$RUNTIME" stats --no-stream --format '{{.MemUsage}}' "$NAME" 2>/dev/null | head -1)
  echo "  segment done: +$written titles (den-embed mem ${rss:-?}); recycling den-embed"
  if [ "$written" -eq 0 ]; then
    echo "=== all titles embedded → finalizing ==="
    stop_embed
    "$BIN" finalize --out-dir "$OUT_DIR" || { echo "finalize failed — nothing published"; exit 1; }
    # finalize just minted a new datasetVersion, and the sidecar's filename carries it. This is the only
    # finalize in the repo that runs with no operator present, so it is the one that most needs the step
    # the runbook spells out — without it the manifest names the previous version's sidecar.
    "$BIN" metadata --out-dir "$OUT_DIR" || { echo "metadata failed — do not publish this out-dir"; exit 1; }
    exit 0
  fi
done
echo "gave up after 200 segments"; exit 1
