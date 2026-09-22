#!/usr/bin/env python3
"""One outbound request, done properly. Every stage that leaves the machine goes through here.

The stdlib gives none of the following by default, and each one has already cost this pipeline a run:

  * **a timeout on every request.** `urlopen` defaults to none, so one hung socket stalls a twelve-hour
    drain forever and reports nothing at all;
  * **an identifying User-Agent.** The Wikimedia APIs throttle or refuse unidentified traffic, and the 403
    reads exactly like a dead article;
  * **retry on transient failures only.** A multi-day batch WILL meet 429/5xx/timeouts from these public
    APIs, and without retry a blip silently drops a title. A definitive answer — 404 on a stale sitelink,
    400, 413 — is re-raised immediately: retrying it just fails more slowly, which is the same lesson
    `pipeline/embed.py` records about den-embed's 413;
  * **`Retry-After`, when the server sends one.** Backing off on our own curve against a server that has
    said how long to wait is how a rate limit turns into a ban.

Connections are kept open per host and per thread. A fresh TLS handshake for each of ~80k Wikipedia
requests is the one real cost the stdlib makes a caller think about; `urllib.request` closes every
connection, so this uses `http.client` directly and keeps one alive.

No third-party dependency, deliberately: nothing here needs one, and a dependency file plus a CI install
step plus the homelab container should arrive with the stage that pays for them.
"""
import http.client
import json
import random
import threading
import time
import urllib.parse

#: Required by the Wikimedia APIs. The same string the Swift passes sent, so the traffic this pipeline
#: produces still identifies as one project rather than two.
USER_AGENT = "den-dataset/1.0 (github.com/oxyc/den-dataset)"

#: Seconds. Long enough for a WDQS query over a 100-id batch, short enough that a dead socket surfaces.
TIMEOUT = 60

#: Doubling from 0.5s, four attempts — the Swift `Transport.retrying` schedule, 0.5 + 1 + 2 = 3.5s of
#: waiting before the last attempt. Jitter goes ON TOP, up to `JITTER` of each step again, so a fleet of
#: workers backing off together does not re-collide and none of them waits less than the Swift pass did.
#: Jitter drawn from zero instead halves the expected wait, and a blip that outlasts it drops the title.
ATTEMPTS = 4
BASE_DELAY = 0.5
JITTER = 0.5
#: A server that names a wait can still name an unreasonable one. Past this the request gives up and says
#: why: sleeping that long stalls a worker for minutes, and retrying sooner than asked is the one thing a
#: rate-limited client must not do.
MAX_RETRY_AFTER = 120


class HTTPError(RuntimeError):
    """A non-2xx answer. `status` is what decides whether it is worth asking again."""

    def __init__(self, status, url, body=b"", reason=None):
        super().__init__(f"HTTP {status} for {url}" + (f": {reason}" if reason else ""))
        self.status = status
        self.url = url
        self.body = body


def is_transient(status):
    """Rate-limit (429), request-timeout (408) and any 5xx. Everything else is the server's answer."""
    return status in (408, 429) or 500 <= status <= 599


class _Connections:
    """One open connection per host, per thread.

    Per thread because `http.client` connections are not safe to share, and the fan-out here is a
    `ThreadPoolExecutor`. A connection that errors is dropped rather than reused: a half-consumed response
    leaves the next request reading the previous one's body.
    """

    def __init__(self):
        self._local = threading.local()

    def _pool(self):
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = self._local.pool = {}
        return pool

    def get(self, host, timeout, scheme="https"):
        pool = self._pool()
        connection = pool.get((scheme, host))
        if connection is None:
            kind = http.client.HTTPConnection if scheme == "http" else http.client.HTTPSConnection
            connection = pool[(scheme, host)] = kind(host, timeout=timeout)
        return connection

    def drop(self, host, scheme="https"):
        connection = self._pool().pop((scheme, host), None)
        if connection is not None:
            connection.close()


_connections = _Connections()


def _retry_after(response):
    """The server's own wait, in seconds, or None. Only the delta-seconds form is honoured — the HTTP-date
    form appears in practice only from caches, and mis-parsing it into a huge sleep is worse than jitter."""
    value = response.getheader("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


#: The request headers that make a GET conditional. A 304 is the answer to one of them, not an error.
CONDITIONAL = ("If-None-Match", "If-Modified-Since")


def request(host, path, params=None, method="GET", body=None, headers=None, timeout=TIMEOUT,
            attempts=ATTEMPTS, received=None, scheme="https"):
    """One request, retried while the failure is transient. Returns the response body as bytes.

    `params` is encoded with `quote_via=quote`, so a `+` in an article title stays a plus. `urlencode`'s
    default turns it into a form-encoded space, and every title carrying one ("Knife+Heart", "X+Y",
    "Survive Style 5+") came back `missingtitle` — a title that simply does not exist, as far as anything
    downstream could tell.

    `received`, when given, is a dict filled with the answer's `status`, `etag` and `last-modified`. A
    request that sent one of the `CONDITIONAL` headers returns an empty body on a 304, and `received` is
    how the caller tells "unchanged" from an empty file.

    `scheme` is `http` only for den-embed, which is self-hosted on the LAN or a container port and speaks
    no TLS. `host` may carry a port.
    """
    target = path
    if params:
        query = urllib.parse.urlencode(sorted(params.items()), quote_via=urllib.parse.quote)
        target = f"{path}?{query}"
    sent = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    sent.update(headers or {})

    url = f"{scheme}://{host}{path}"
    delay = BASE_DELAY
    for attempt in range(1, attempts + 1):
        last = attempt == attempts
        try:
            connection = _connections.get(host, timeout, scheme)
            connection.request(method, target, body=body, headers=sent)
            response = connection.getresponse()
            payload = response.read()
        except (http.client.HTTPException, OSError) as transport:
            # A dead connection is the ordinary way a kept-open socket ends; drop it and ask again.
            _connections.drop(host, scheme)
            if last:
                raise HTTPError(0, url) from transport
            wait = None
        else:
            unchanged = response.status == 304 and any(name in sent for name in CONDITIONAL)
            if 200 <= response.status <= 299 or unchanged:
                if received is not None:
                    received.update({"status": response.status, "etag": response.getheader("ETag"),
                                     "last-modified": response.getheader("Last-Modified")})
                return payload
            if last or not is_transient(response.status):
                raise HTTPError(response.status, url, payload)
            wait = _retry_after(response)
            if wait is not None and wait > MAX_RETRY_AFTER:
                raise HTTPError(response.status, url, payload,
                                f"the server asked for {wait:g}s before a retry, past the "
                                f"{MAX_RETRY_AFTER}s this client will wait — giving up")
        time.sleep(wait if wait is not None else delay + random.uniform(0, delay * JITTER))
        delay = min(delay * 2, MAX_RETRY_AFTER)
    raise HTTPError(0, url)


def get_json(host, path, params=None, timeout=TIMEOUT):
    """A GET whose body is parsed as JSON. A body that is not JSON is the caller's problem to describe —
    "the endpoint answered something else" and "the endpoint answered nothing" are different failures."""
    return json.loads(request(host, path, params, timeout=timeout).decode("utf-8"))
