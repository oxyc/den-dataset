#!/usr/bin/env bash
# Shared setup for the pipeline scripts: load den.env, and mint a Wikimedia Enterprise bearer.
#
#   . "$(dirname "$0")/lib/den-env.sh"
#   den_load_env || exit 1  # den.env → environment, TMDB_API_KEY required
#   enterprise_login        # optional 24h bearer; degrades to the free action API
#
# Sourced, not executed. It lived as three near-identical copies (enrich-run, enrich-all, and the copy
# delta-run never had — which is why the daily pass ran with no credentials at all).
#
# These signal failure with `return`, not `exit`, because a sourced `exit` would kill the caller from inside
# a helper. So a caller that does NOT set -e must check: `den_load_env || exit 1`. enrich-all.sh runs
# without -e on purpose (a failed batch must not abort the run), and inlining this without that check made a
# missing TMDB key print its message and carry on into six failing batches and 7 minutes of backoff.

# den.env holds TMDB_API_KEY and the optional Wikimedia Enterprise credentials. Every command that
# touches TMDB needs it, so a script that forgets to load it fails at its first HTTP call rather than
# here — which is what made the missing copy in delta-run.sh hard to see.
den_load_env() {
    [ -f den.env ] || { echo "missing den.env — cp den.env.example den.env and fill it" >&2; return 1; }
    set -a
    # shellcheck disable=SC1091  # generated per-machine, not in the repo
    . ./den.env
    set +a
    [ -n "${TMDB_API_KEY:-}" ] || { echo "TMDB_API_KEY is empty in den.env" >&2; return 1; }
}

# Mint a fresh 24h Enterprise bearer. A login failure must NOT kill the run: the free action API has the
# same plot coverage, just slower. Safe to call repeatedly — the token expires in 24h and a long run
# outlives it. Messages go to stderr so a caller teeing stdout to a log still records them.
enterprise_login() {
    unset WIKIMEDIA_ENTERPRISE_TOKEN
    [ -n "${WIKIMEDIA_ENTERPRISE_USERNAME:-}" ] && [ -n "${WIKIMEDIA_ENTERPRISE_PASSWORD:-}" ] || {
        echo "no Enterprise creds — using the free Wikipedia action API." >&2
        return 0
    }
    local tok
    # The credential JSON goes to curl on STDIN (--data @-), never as an argv arg: a `-d '{…password…}'`
    # is visible to every local user through `ps` for the life of the request.
    if tok=$(python3 -c 'import json,os;print(json.dumps({"username":os.environ["WIKIMEDIA_ENTERPRISE_USERNAME"],"password":os.environ["WIKIMEDIA_ENTERPRISE_PASSWORD"]}))' \
            | curl -fsSL https://auth.enterprise.wikimedia.com/v1/login \
                   -H "Content-Type: application/json" --data @- \
            | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])') && [ -n "$tok" ]; then
        export WIKIMEDIA_ENTERPRISE_TOKEN="$tok"
        echo "→ Enterprise token acquired (structured-contents plots)." >&2
    else
        echo "⚠ Enterprise login failed — falling back to the free Wikipedia action API." >&2
    fi
}
