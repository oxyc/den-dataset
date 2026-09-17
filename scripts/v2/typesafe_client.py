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
import json
import os
import random
import re
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


class TypeSafe:
    def __init__(self, key=None, model=MODEL, timeout=120, endpoint=ENDPOINT):
        self._key = key or api_key()
        self.model, self.timeout, self.endpoint = model, timeout, endpoint
        self.input_tokens = 0          # what we are billed for
        self.output_tokens = 0         # free, tracked only to see what the questions returned
        self.calls = 0

    def ask(self, state, questions):
        """One state, every question you need, one call.

        `questions` is {name: {"type": …, "instructions": …, "criteria": …}} and the answers come back under
        the same names. Returns the `answers` map; usage is accumulated on the client.
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
                # Full jitter: a fleet of workers that all back off on the same curve re-collides.
                time.sleep(random.uniform(0, min(30, 2 ** attempt)))
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == RETRY_STATUS_MAX - 1:
                    raise TypeSafeError(f"transport: {_scrub(str(exc), self._key)}") from None
                time.sleep(random.uniform(0, min(30, 2 ** attempt)))

        usage = payload.get("usage") or {}
        self.input_tokens += usage.get("input_tokens") or 0
        self.output_tokens += usage.get("output_tokens") or 0
        self.calls += 1
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise TypeSafeError(f"no answers in response: {json.dumps(payload)[:300]}")
        return answers

    # $0.042 per million input tokens, output free. Stated on the models page; if it moves, this is the
    # one place to change it.
    RATE_PER_INPUT_TOKEN = 0.042 / 1_000_000

    @property
    def spend(self):
        return self.input_tokens * self.RATE_PER_INPUT_TOKEN

    def summary(self):
        return (f"{self.calls:,} calls · {self.input_tokens:,} input tokens "
                f"({self.output_tokens:,} output, free) · ${self.spend:,.2f}")
