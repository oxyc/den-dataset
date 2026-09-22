#!/usr/bin/env python3
"""Whether Wikimedia Enterprise is asked: the account's monthly quota and a per-run breaker, one mechanism.

The account allows a fixed number of on-demand requests per calendar month, reset on the 1st, and an
overdrawn month answers every request 429 with no `Retry-After` and no rate-limit header — indistinguishable
from a throttle by the answer alone. A full enrich is ~47.6k titles with up to two English candidates each,
so one pass can spend more than the month. Two guards:

  * **the account's own count.** `get-user` reports `ondemand_requests_count` and `ondemand_limit`
    account-wide — the laptop, the homelab box and GitHub Actions share the account, and only the server
    sees all three — and asking it spends no quota. It is asked before the first Enterprise request of a
    run and again every `RECHECK_EVERY`, and at `limit - reserve` Enterprise is not asked again this run.
  * **a breaker** for what the count cannot see: `THROTTLES` 429s in a row, or one 401/403, stop this
    process asking for the rest of the run. When `get-user` itself fails it is the only guard, and that is
    said once rather than guessed around.

The bearer travels in `get-user`'s JSON BODY, never a URL, an argv or a log line, and of its answer only the
two on-demand counters are read: it also names the account and its APIs, which have no business in a log.
"""
import json
import os
import sys
import threading

from . import http

AUTH_HOST = "auth.enterprise.wikimedia.com"
USAGE_PATH = "/v1/get-user"

#: Enterprise requests between two looks at the account's count. `get-user` is free, so the only cost of
#: asking often is a round trip; 100 is about 50 titles at two candidates each, a few seconds of a drain.
RECHECK_EVERY = 100

#: Requests left unspent below the limit. It must cover what can be spent between two looks: this run's own
#: `RECHECK_EVERY`, plus whatever the other machines on the account spend in the same window, which nothing
#: here can see until the next look. 500 is five windows of this run's own spending — 1% of a 50,000 month.
DEFAULT_RESERVE = 500

#: Consecutive 429s before this process stops asking. Twice the enrichment's four grounding workers: one
#: throttle refuses every request in flight at once, so fewer than two full rounds with no answer between
#: them cannot tell a throttled account from a throttled moment. Each 429 before the trip costs one round
#: trip, because Enterprise is asked once and never retried.
THROTTLES = 8


class UsageUnknown(RuntimeError):
    """`get-user` gave no usable count. The message never carries the answer's body."""


def reserve(env=None):
    """`DEN_ENTERPRISE_RESERVE`, or `DEFAULT_RESERVE`. A value that is not a count is a refusal: read as
    anything else it would silently spend the month or silently stop asking."""
    env = os.environ if env is None else env
    raw = env.get("DEN_ENTERPRISE_RESERVE")
    if not raw:
        return DEFAULT_RESERVE
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise ValueError(f"DEN_ENTERPRISE_RESERVE={raw!r} is not a number of requests")
    return value


def account_usage(token):
    """`(ondemand_requests_count, ondemand_limit)` for the account `token` belongs to. Spends no quota."""
    try:
        payload = http.request(AUTH_HOST, USAGE_PATH, method="POST",
                               body=json.dumps({"access_token": token}).encode("utf-8"),
                               headers={"Content-Type": "application/json"})
    except http.HTTPError as error:
        raise UsageUnknown(f"get-user answered HTTP {error.status}" if error.status else
                           "get-user could not be reached") from None
    try:
        body = json.loads(payload.decode("utf-8"))
        count, limit = body["ondemand_requests_count"], body["ondemand_limit"]
    except (ValueError, KeyError, TypeError):
        raise UsageUnknown("get-user answered without ondemand_requests_count and ondemand_limit") from None
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (count, limit)):
        raise UsageUnknown("get-user's on-demand counters are not integers")
    return count, limit


class Gate:
    """This process's view of the account: its count as last read, and the breaker."""

    def __init__(self, recheck_every=RECHECK_EVERY, headroom=None):
        self._lock = threading.Lock()
        self._every, self._headroom = recheck_every, headroom
        self._throttled = 0
        self._since_check = None
        self._token = None
        #: Why Enterprise is not asked for the rest of the run; None while it is.
        self.off = None
        #: Why the account's count is unknown for this run; None while `get-user` answers.
        self.unknown = None
        #: `(count, limit)` at the first look and at the latest.
        self.first = self.latest = None
        #: Enterprise requests this process sent.
        self.sent_this_run = 0

    def headroom(self):
        if self._headroom is None:
            self._headroom = reserve()
        return self._headroom

    def reserve(self, token):
        """True when one Enterprise request may be sent now, counted as sent. Looks at the account first
        when it has not yet, or when `RECHECK_EVERY` requests have gone since it last did."""
        with self._lock:
            if self.off is not None:
                return False
            self._token = token
            if self.unknown is None and (self._since_check is None or self._since_check >= self._every):
                self._check()
                if self.off is not None:
                    return False
            self.sent_this_run += 1
            self._since_check = (self._since_check or 0) + 1
            return True

    def sent(self, request, *args, **kwargs):
        """`request(*args, **kwargs)`, its refusal counted towards the breaker before it is re-raised."""
        try:
            payload = request(*args, **kwargs)
        except http.HTTPError as error:
            self._refused(error.status)
            raise
        with self._lock:
            self._throttled = 0
        return payload

    def refresh(self):
        """Look at the account's count once more — the end-of-run figure — if one was ever read."""
        with self._lock:
            if self._token is not None and self.unknown is None:
                self._check()
            return self.first, self.latest

    def _check(self):
        try:
            count, limit = account_usage(self._token)
        except UsageUnknown as error:
            self.unknown = str(error)
            print(f"enterprise: the account's on-demand usage is unknown for this run ({error}); only the "
                  f"429/401 breaker guards the monthly quota", file=sys.stderr, flush=True)
            return
        self._since_check = 0
        self.latest = (count, limit)
        if self.first is None:
            self.first = self.latest
        if count >= limit - self.headroom():
            self._switch_off(f"the account has used {count:,} of its {limit:,} on-demand requests this month, "
                             f"and {self.headroom():,} are held in reserve for the other machines on it")

    def _refused(self, status):
        with self._lock:
            if status in (401, 403):
                # An expired or unentitled bearer does not start working again, and asking with it before
                # every action-API fetch only doubles the requests.
                self._switch_off(f"HTTP {status}: the bearer is expired or not entitled")
            elif status == 429:
                self._throttled += 1
                if self._throttled >= THROTTLES:
                    self._switch_off(f"{THROTTLES} throttled (429) answers in a row")
            # A 5xx or no answer says nothing about the account: neither counted nor a reset.

    def _switch_off(self, reason):
        if self.off is None:
            self.off = reason
            print(f"enterprise: Wikimedia Enterprise switched off for the rest of this run ({reason}); every "
                  f"plot from here comes from the free action API", file=sys.stderr, flush=True)


gate = Gate()
