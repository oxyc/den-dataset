#!/usr/bin/env python3
"""HTTP client for TypeSafe's System One API (`POST /v1/systemone`).

The rest of this pipeline has no LLM client at all — `main.swift` says so explicitly: "NO LLM key — the
labels come from Haiku subagents, not an API". Jev is an ordinary HTTP API, so one has to exist, and this is
it: small, synchronous, and deliberately not a framework.

Two properties are load-bearing and easy to lose:

**The key never appears in a URL, a log line or an exception.** `Redact.swift` keeps a list of secret
variable names for exactly this reason, and its `secretParams` list exists because putting a credential in a
query parameter also puts it in the response cache's filename. Here the key is read from the environment at
call time, sent only as an `Authorization` header, and `_scrub` runs over anything that gets printed.

**Output tokens are free; the state is what costs.** So the shape that saves money is one state with many
questions, not many calls. `ask()` takes a whole question map and the API evaluates them in parallel; the
docs are explicit that this "barely affects response time". Do not loop over questions.
"""
import http.client
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
KEY_VAR = "TYPESAFE_API_KEY"

# 429 and 529 are the documented retryable codes. 422 is a malformed request and retrying it just spends
# the rate limit on the same rejection; 401 is a bad key and will never improve.
RETRYABLE = {429, 529}
RETRY_STATUS_MAX = 6
# A server that names a wait is obeyed, up to this. Past it the call gives up and says why: sleeping longer
# stalls a worker for minutes, and retrying sooner than asked is the one thing a rate-limited client must not do.
MAX_RETRY_AFTER = 120


class TypeSafeError(RuntimeError):
    pass


def api_key(env_path=None):
    """The key from the environment, else from `den.env` — the convention `docs/OPERATE.md` already sets.

    Returns the value so a caller can send it; never log what comes back.
    """
    if key := os.environ.get(KEY_VAR):
        return key
    path = env_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "den.env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if m := re.match(rf"\s*(?:export\s+)?{KEY_VAR}\s*=\s*(.+?)\s*$", line):
                    return m.group(1).strip().strip("\"'")
    except FileNotFoundError:
        pass
    raise TypeSafeError(f"set {KEY_VAR} (in the environment or den.env)")


def _scrub(text, key):
    """Never print a key, even inside an error body that happened to echo the request."""
    return text.replace(key, "REDACTED") if key else text


def _retry_after(exc):
    """The server's own wait in seconds, or None. Only the delta-seconds form is honoured, as in
    `lib/http.py`: mis-parsing an HTTP-date into a huge sleep is worse than jitter."""
    try:
        seconds = float((exc.headers.get("Retry-After") or "").strip())
    except (AttributeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _backoff(attempt):
    # Full jitter: a fleet of workers that all back off on the same curve re-collides.
    return random.uniform(0, min(30, 2 ** attempt))


class TypeSafe:
    def __init__(self, key=None, model=MODEL, timeout=120, endpoint=ENDPOINT):
        self._key = key or api_key()
        self.model, self.timeout, self.endpoint = model, timeout, endpoint
        self.input_tokens = 0          # what we are billed for
        self.output_tokens = 0         # free, tracked only to see what the questions returned
        self.calls = 0
        self._usage_lock = threading.Lock()

    def ask(self, state, questions):
        """One state, every question you need, one call.

        `questions` is {name: {"type": …, "instructions": …, "criteria": …}} and the answers come back under
        the same names. Returns the `answers` map; usage is accumulated on the client.
        """
        return self.ask_with_metadata(state, questions)[0]

    def ask_with_metadata(self, state, questions):
        """Return ``(answers, metadata)`` while retaining the small ``ask`` compatibility surface.

        A resumable corpus run must record the model identifier returned by the provider, not merely the
        identifier requested by the caller.  Keeping that value local to this request also avoids a race when
        one client is shared by worker threads.
        """
        body = json.dumps({"state": state, "model": self.model, "questions": questions}).encode()
        req = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"Authorization": f"Bearer {self._key}",
                     "Content-Type": "application/json",
                     "User-Agent": "den-dataset"})
        for attempt in range(RETRY_STATUS_MAX):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as fh:
                    payload = json.load(fh)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE or attempt == RETRY_STATUS_MAX - 1:
                    detail = _scrub(exc.read().decode("utf-8", "replace")[:400], self._key)
                    raise TypeSafeError(f"HTTP {exc.code}: {detail}") from None
                wait = _retry_after(exc)
                if wait is not None and wait > MAX_RETRY_AFTER:
                    raise TypeSafeError(f"HTTP {exc.code}: the server asked for {wait:g}s before a retry, "
                                        f"past the {MAX_RETRY_AFTER}s this client will wait") from None
                time.sleep(_backoff(attempt) if wait is None else wait)
            except (http.client.HTTPException, OSError) as exc:
                # A request that got no HTTP answer. `urlopen` wraps a failure while SENDING in `URLError`,
                # but not one while waiting for or reading the answer: a server that closes the socket
                # raises `RemoteDisconnected`, a reset `ConnectionResetError`, a cut body `IncompleteRead`,
                # a stall `TimeoutError`. Catching only `URLError` let one dropped connection out of the
                # client as a raw traceback and ended a 47k-title run at 24k. All of them are OSError or
                # HTTPException (`URLError` and `TimeoutError` are OSError), and all are worth asking again.
                #
                # The API takes no idempotency key, and its docs do not say whether a request whose answer
                # never arrived was billed; TypeSafe's own SDKs retry connection errors and timeouts by
                # default. So a retry can at worst pay for one state twice (about $0.0004 at the corpus
                # pass's average), and `input_tokens` counts only calls that were answered.
                if attempt == RETRY_STATUS_MAX - 1:
                    raise TypeSafeError(f"transport ({type(exc).__name__}) after {RETRY_STATUS_MAX} "
                                        f"attempts: {_scrub(str(exc), self._key)}") from None
                time.sleep(_backoff(attempt))

        usage = payload.get("usage") or {}
        with self._usage_lock:
            self.input_tokens += usage.get("input_tokens") or 0
            self.output_tokens += usage.get("output_tokens") or 0
            self.calls += 1
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise TypeSafeError(f"no answers in response: {json.dumps(payload)[:300]}")
        metadata = {
            "model": payload.get("model"),
            "usage": {
                "input_tokens": usage.get("input_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or 0,
            },
        }
        return answers, metadata

    # $0.042 per million input tokens, output free. Stated on the models page; if it moves, this is the
    # one place to change it.
    RATE_PER_INPUT_TOKEN = 0.042 / 1_000_000

    @property
    def spend(self):
        return self.input_tokens * self.RATE_PER_INPUT_TOKEN

    def summary(self):
        return (f"{self.calls:,} calls · {self.input_tokens:,} input tokens "
                f"({self.output_tokens:,} output, free) · ${self.spend:,.2f}")
