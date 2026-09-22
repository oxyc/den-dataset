#!/usr/bin/env python3
"""`den run`, end to end, over the committed fixture corpus in `pipeline/fixture-corpus/` — offline.

Every stage has its own suite; this is the one that runs them in ORDER, so the seams between them are
tested too: what one stage writes is what the next one reads, with nothing hand-built in between except
the seeds named below. It is oxyc/den-dataset#27's acceptance item 1.

**Nothing leaves the machine.** `lib/http.request` is replaced by `Upstreams`, which answers TMDB, the IMDb
ratings dump, Wikipedia and Wikidata out of `pipeline/fixture-corpus/upstream/` and refuses any request it
has no answer for. den-embed is a stand-in on a local port, reached over a real socket, with a canary made
for it. A socket guard refuses every non-loopback connection this process opens, so a client that stopped
going through `lib/http` fails here rather than reaching the internet.

The upstream answers are hand-written in each service's wire shape. The titles, people and plots are
invented: there is no TMDB text in them (no overview, no tagline — TMDB's terms), no Wikipedia prose
(CC BY-SA), no real IMDb counts (a non-transferable licence), and no credential. `TMDB_API_KEY` is set to a
string that is not one, because the fetch stage refuses to start without some value.

**What is seeded, and why.** Two kinds of input are copied into the out-dir before the run, each in its
own directory under `seeds/` so the kind is visible from the path:

  * `seeds/unproduced/` — the six inputs NO stage produces (`UNPRODUCED`). The audit in #27 found that
    `./den run` cannot run unattended because of exactly these. `UnproducedSeeds` fails the moment a stage
    starts producing one, so the seed is deleted rather than left to shadow the real producer.
  * `seeds/unbought/` — the classify pass's shard. Its stage exists but buys from a paid provider, and
    `den run` leaves it out without `--spend`; CI can never buy. `TheClassifyPassStillBuys` fails if that
    stops being true. The genres & moods stage's answer shard is the same kind of input — what its ask
    would buy — and the test WRITES it (`write_genres_moods_answer`), because one answer is 78 questions'
    worth of probabilities and would not review.

Nothing else is seeded: no stage reads what a later one writes (`NoStageReadsWhatALaterOneWrites`), so the
out-dir starts empty of every other artifact. The genres & moods stage reads two committed files instead of
an out-dir, and the test stands in for both, as it does for den-embed: `fixture-corpus/genres-moods-curated.json`
for `data/genres-moods-curated.json`, whose 47,539 real titles no fixture upstream could answer for, and a
passing quality gate, which needs ten golden titles per label to score anything and has its own suite
(`pipeline/genres_moods_test.py`).

**What `den run` does.** Every stage but the two that buy or publish, unattended, in one command: the
fetch drain over a universe holding a title below every floor, and the stages from `facts` on under the
version `finalize` derived, since it is given none (`test_den_run_runs_every_stage_it_is_allowed_to`). A
version given by hand is only checked against that one (`test_a_given_version_is_only_a_check`).
"""
import base64
import contextlib
import fnmatch
import gzip
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pipeline  # noqa: E402
from lib import denembed, http, tmdb as tmdb_api, wikipedia  # noqa: E402
from pipeline import artifacts, genres_moods  # noqa: E402
from pipeline.contract import bind  # noqa: E402

V2 = os.path.join(HERE, "scripts", "v2")
sys.path.insert(0, V2)
import audit_combined  # noqa: E402
import vector_blob  # noqa: E402

FIXTURE = os.path.join(HERE, "pipeline", "fixture-corpus")
UPSTREAM = os.path.join(FIXTURE, "upstream")
SEEDS = os.path.join(FIXTURE, "seeds")

#: The six inputs no stage produces, and the seed file(s) standing in for each. oxyc/den-dataset#27.
UNPRODUCED = {
    "export_movie": ("movie_ids.json",),
    "export_tv": ("tv_series_ids.json",),
    "delta": ("delta-v2.jsonl", "delta-v2.jsonl.manifest.json"),
    "delta_ids": ("facts-delta-ids.txt",),
    "premise_labels": ("labels-premise.json",),
    # A binary blob does not review, so the seed is its key list and `setUpClass` writes the blob.
    "premise_vectors": ("vectors-premise.json",),
}

#: A version given by hand. It cannot be the one `finalize` derives, so a stage that names its files by the
#: version must refuse it rather than obey it.
GIVEN_VERSION = "fixture"
DIMS = 1024
BELOW_FLOOR = "movie:900004"
CURATED = os.path.join(FIXTURE, "genres-moods-curated.json")
#: The title whose genres & moods come from a Jev answer rather than the curated file, and that answer's
#: strong labels: everything else it is asked is answered 0.05, under every threshold in the rule.
DERIVED = "tv:900005"
DERIVED_PRIMARY = "Mystery"
DERIVED_STRONG = {"Whodunit/Murder Mystery": 0.95, "Tense/Edge-of-seat": 0.9}


#: The sidecars of the two seeded Jev shards, which the corpus stage audits before it joins.
SEEDED_MANIFESTS = ("combined-v1-r2.jsonl.manifest.json", "delta-v2.jsonl.manifest.json")


def stamp_implementation(path):
    """Record, in a seeded shard's manifest, this tree's digests of the files the pass hashes.

    The seed stands in for a shard bought on the pass as this tree holds it, which is what these digests
    say. They are written at copy time rather than committed: committed, every edit to the pass would fail
    this test with the audit's advice — record a lineage entry — which is meant for shards someone paid for.
    """
    manifest = read_json(path)
    manifest["config"]["implementationSha256"] = {name: sha256(path)
                                                  for name, path in audit_combined.SOURCES.items()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)


def load_den():
    """The `den` entry point as a module — its file has no `.py`."""
    loader = importlib.machinery.SourceFileLoader("den_entry", os.path.join(HERE, "den"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("den_entry", loader))
    loader.exec_module(module)
    return module


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def embedding(text):
    """A deterministic int8 vector for any text — the stand-in's whole model."""
    raw, counter = b"", 0
    while len(raw) < DIMS:
        raw += hashlib.sha256(f"{counter}:{text}".encode()).digest()
        counter += 1
    return [b - 256 if b > 127 else b for b in raw[:DIMS]]


class EmbedStandIn:
    """den-embed on a local port: bge-m3's identity at the serving box's token cap, `embedding` as the model."""

    HEALTH = {"model": "bge-m3", "dims": DIMS, "vector_epoch": 1, "runtime": "fixture-stand-in",
              "max_tokens": 1024}

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply(EmbedStandIn.HEALTH)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.reply({"vectors": [embedding(text) for text in body["texts"]]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def canary(self, path):
        """A known-answer canary whose answers are this stand-in's — so the embed stage's gate runs for
        real, against a space that is honestly named as the fixture's rather than borrowing production's."""
        cases = [{"id": case, "why": "fixture", "text": text,
                  "v": base64.b64encode(bytes(x & 0xFF for x in embedding(text))).decode()}
                 for case, text in (("plot", "A keeper finds a ledger of ships."), ("cjk", "深夜のタクシー"))]
        doc = {"canarySet": "den-run-fixture-v1", "dims": DIMS, "cases": cases,
               "textsSha256": denembed.texts_sha256(cases), "spaceId": denembed.space_id("den-run-fixture-v1", cases),
               "embedder": {"max_tokens": self.HEALTH["max_tokens"]}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return doc

    def close(self):
        self.server.shutdown()
        self.server.server_close()


ENTITY = "http://www.wikidata.org/entity/"


def wiki_url(language, title):
    return f"https://{language}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"


class Wikidata:
    """The query service, over the statements in `upstream/wikidata.json`.

    Each query the pipeline sends is recognised by its SELECT clause and answered from the items the ids
    in its VALUES name, in the result shape WDQS returns. A query it does not recognise is a failure, not
    an empty result: an empty result is an ANSWER to every parser here, and would hide a new query.
    """

    def __init__(self, items):
        self.items = items
        self.by_tmdb = {(media, tmdb_id): qid for qid, item in items.items()
                        for media, tmdb_id in (item.get("tmdb") or {}).items()}

    def claims(self, qid, prop):
        return ((self.items.get(qid) or {}).get("claims") or {}).get(prop, [])

    def label(self, qid):
        return (self.items.get(qid) or {}).get("label") or qid

    def enwiki(self, qid):
        title = (self.items.get(qid) or {}).get("enwiki")
        return wiki_url("en", title) if title else None

    @staticmethod
    def value(v):
        return ENTITY + v if re.fullmatch(r"Q\d+", str(v)) else v

    def answer(self, query):
        head = query.split(" WHERE", 1)[0]
        items = re.search(r"VALUES \?item \{([^}]*)\}", query)
        if items:
            rows = []
            for qid in re.findall(r"wd:(Q\d+)", items.group(1)):
                rows += [dict(row, item=ENTITY + qid) for row in self.about_item(head, query, qid)]
        else:
            media = "movie" if "wdt:P4947 ?tmdb" in query else "tv" if "wdt:P4983 ?tmdb" in query else None
            ids = re.findall(r'"(\d+)"', re.search(r"VALUES \?tmdb \{([^}]*)\}", query).group(1))
            rows = []
            for tmdb_id in ids:
                qid = self.by_tmdb.get((media, int(tmdb_id)))
                if qid:
                    rows += [dict(row, tmdb=tmdb_id) for row in self.about_title(head, query, qid)]
        return {"head": {"vars": []},
                "results": {"bindings": [{name: {"type": "literal", "value": value}
                                          for name, value in row.items() if value is not None}
                                         for row in rows]}}

    def about_title(self, head, query, qid):
        def prop(pattern):
            return re.search(pattern, query).group(1)

        if "?sourceArticle" in head:
            source = next((self.enwiki(work) for work in self.claims(qid, "P144") if self.enwiki(work)), None)
            rows = [{"article": self.enwiki(qid), "sourceArticle": source,
                     "imdb": next(iter(self.claims(qid, "P345")), None)}]
            rows += [{"runtime": minutes} for minutes in self.claims(qid, "P2047")]
            rows += [{"creatorLabel": self.label(person)} for person in self.claims(qid, "P170")]
            for language, title in ((self.items[qid].get("sitelinks") or {}).items()):
                site = f"https://{language}.wikipedia.org/"
                if f"<{site}>" in query:
                    rows.append({"anyArticle": wiki_url(language, title), "anySite": site})
            return rows
        if head == "SELECT ?tmdb ?film":
            # One item per id here, so nothing is contested and the evidence query is never asked.
            return [{"film": ENTITY + qid}]
        if head == "SELECT ?tmdb ?imdb":
            return [{"imdb": imdb_id} for imdb_id in self.claims(qid, "P345")]
        if head == "SELECT ?tmdb ?code":
            through, via = prop(r"\?film wdt:(P\d+) \?v"), prop(r"\?v wdt:(P\d+) \?code")
            return [{"code": code} for item in self.claims(qid, through) for code in self.claims(item, via)]
        if "?filmLabel" in head:
            rows = [{"filmLabel": self.label(qid)}]
            rows += [{"released": stated["time"]} for stated in self.claims(qid, "P577")]
            if "wdt:P580 ?start" in query:
                rows += [{"start": stated["time"]} for stated in self.claims(qid, "P580")]
            return rows
        if head == "SELECT ?tmdb ?vLabel" and "UNION" not in query:
            return [{"vLabel": self.label(item)} for item in self.claims(qid, prop(r"\?film wdt:(P\d+) \?v \."))]
        if head == "SELECT ?tmdb ?v ?prec":
            return [{"v": stated["time"], "prec": str(stated["precision"])}
                    for stated in self.claims(qid, prop(r"p:(P\d+) \?st"))]
        if head == "SELECT ?tmdb ?v":
            return [{"v": self.value(v)} for v in self.claims(qid, prop(r"\?film wdt:(P\d+) \?v \."))]
        if "?orig" in head:
            rows = [{"article": self.enwiki(qid)}] if self.enwiki(qid) else []
            rows += [{"label": self.label(qid), "labelLang": "en"}] if self.items[qid].get("label") else []
            rows += [{"orig": title["text"], "origLang": title["lang"]} for title in self.claims(qid, "P1476")]
            return rows
        if head == "SELECT ?tmdb ?alias":
            return [{"alias": alias} for alias in self.items[qid].get("aliases") or []]
        raise AssertionError(f"the fixture's Wikidata does not know this query:\n{query}")

    def about_item(self, head, query, qid):
        if "?members" in head:
            # `?item wdt:P31/wdt:P279* ?class`, walked over the fixture's own statements.
            classes = set(re.findall(r"wd:(Q\d+)", re.search(r"VALUES \?class \{([^}]*)\}", query).group(1)))
            seen, frontier = set(), list(self.claims(qid, "P31"))
            while frontier:
                kind = frontier.pop()
                if kind not in seen:
                    seen.add(kind)
                    frontier += self.claims(kind, "P279")
            members = sum(1 for item in self.items if qid in self.claims(item, "P179"))
            return [{"members": str(members)}] if seen & classes and members else []
        if "?itemLabel ?pid" in head:
            return [{"itemLabel": self.label(qid), "pid": next(iter(self.claims(qid, "P4985")), None)}]
        if head == "SELECT ?item ?alias":
            return [{"alias": alias} for alias in (self.items.get(qid) or {}).get("aliases") or []]
        if "?typeLabel" in head:
            return [{"type": ENTITY + kind, "typeLabel": self.label(kind)} for kind in self.claims(qid, "P31")]
        raise AssertionError(f"the fixture's Wikidata does not know this item query: {head}")


class Upstreams:
    """`lib/http.request`, answered from the fixture. A request it has no answer for FAILS the test."""

    def __init__(self, local_request):
        self.local = local_request
        self.tmdb = read_json(os.path.join(UPSTREAM, "tmdb.json"))
        self.pages = read_json(os.path.join(UPSTREAM, "wikipedia.json"))
        self.wikidata = Wikidata(read_json(os.path.join(UPSTREAM, "wikidata.json")))
        with open(os.path.join(UPSTREAM, "imdb-ratings.tsv"), "rb") as fh:
            self.ratings = gzip.compress(fh.read(), mtime=0)
        self.hosts = set()

    def request(self, host, path, params=None, method="GET", body=None, headers=None, received=None,
                scheme="https", **_transport):
        if host.startswith("127.0.0.1:"):
            return self.local(host, path, params, method=method, body=body, headers=headers,
                              received=received, scheme=scheme, **_transport)
        self.hosts.add(host)
        params = params or {}
        if host == tmdb_api.HOST:
            media, tmdb_id = path.split("/")[-2:]
            found = self.tmdb.get(f"{media}:{tmdb_id}")
            if found is None:
                raise http.HTTPError(404, f"https://{host}{path}", b'{"status_code":34}')
            return json.dumps(found).encode()
        if host == "datasets.imdbws.com":
            if received is not None:
                received.update({"status": 200, "etag": None, "last-modified": None})
            return self.ratings
        if host.endswith(".wikipedia.org") and path == wikipedia.API_PATH:
            assert {k: v for k, v in params.items() if k != "page"} == wikipedia.PARSE_QUERY, params
            page = self.pages.get(host.split(".")[0], {}).get(params["page"])
            if page is None:
                return json.dumps({"error": {"code": "missingtitle"}}).encode()
            return json.dumps({"parse": {"title": params["page"], "revid": page["revid"],
                                         "wikitext": page["wikitext"]}}).encode("utf-8")
        if host == "query.wikidata.org":
            return json.dumps(self.wikidata.answer(body.decode("utf-8")), ensure_ascii=False).encode("utf-8")
        raise AssertionError(f"an upstream the fixture does not record was asked: {method} {host}{path}")


def offline_connect(real):
    """`socket.connect` for loopback only. A client that bypasses `lib/http` dies here, loudly."""
    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"the end-to-end run tried to reach {address}; it must stay offline")
        return real(sock, address)
    return connect


class Store:
    """The four sections these asserts read, and the header. den-core and den-atlas are the readers; this
    parses only what it asserts on."""

    def __init__(self, path):
        with open(path, "rb") as fh:
            self.blob = fh.read()
        magic, _format, _endian, _digest, count, self.rows, version, _ = struct.unpack(
            "<8sIIQII16s16s", self.blob[:64])
        assert magic == b"DENSTOR1", magic
        self.version = version.rstrip(b"\0").decode()
        self.table = {}
        for i in range(count):
            name, off, length, _width = struct.unpack("<16sQII", self.blob[64 + i * 32:96 + i * 32])
            self.table[name.rstrip(b"\0").decode()] = (off, length)

    def column(self, name, fmt, width):
        off, length = self.table[name]
        return list(struct.unpack(f"<{length // width}{fmt}", self.blob[off:off + length]))

    def keys(self):
        return [vector_blob.unpack_key(k) for k in self.column("keys", "Q", 8)]

    def flags(self, name):
        return dict(zip(self.keys(), self.column(name, "B", 1)))


def keys_of(records):
    return {f"{r['mediaType']}:{r['tmdbId']}" for r in records}


def write_genres_moods_answer(out, key):
    """The answer shard the genres & moods ask would buy for `key`, beside its manifest, in the shape
    `run_combined` writes: one Choice per pick, one Noul per label."""
    questions, mapping, _ = genres_moods.questions()
    answers = {}
    for qid, question in questions.items():
        if question["type"] == "noul":
            answers[qid] = {"type": "noul", "noul": DERIVED_STRONG.get(mapping[qid]["label"], 0.05)}
            continue
        choice = DERIVED_PRIMARY if qid == "gm__primary_genre" else "none-fits"
        rest = 0.1 / (len(question["criteria"]) - 1)
        answers[qid] = {"type": "choice", "choice": choice, "confidence": 0.9,
                        "probabilities": {c: 0.9 if c == choice else rest for c in question["criteria"]}}
    run = {"runId": "den-run-fixture", "configSha256": "den-run-fixture"}
    media, tmdb_id = key.split(":")
    shard = os.path.join(out, artifacts.GENRES_MOODS_ANSWERS.filename.replace("*", "-fixture"))
    with open(shard, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({**run, "mediaType": media, "tmdbId": int(tmdb_id), "answers": answers}) + "\n")
    config = {"labelQuestionMapping": mapping, "requestedModel": genres_moods.PINNED_MODEL,
              "globalQuestionsSha256": genres_moods.rc.sha256_text(genres_moods.rc.canonical(questions)),
              "taxonomyVersion": genres_moods.gm.vocabulary()["version"]}
    with open(shard + ".manifest.json", "w", encoding="utf-8") as fh:
        json.dump({**run, "config": config}, fh)


def gate_passes(candidate, golden, floors, flag):
    """The quality gate's stand-in: the verdict `scripts/eval-taxonomy.py --gate` gives a pass."""
    return subprocess.CompletedProcess([candidate, golden, floors, flag], 0, "", "")


class DenRun(unittest.TestCase):
    """One run of the whole pipeline over the fixture, and what its published-shape outputs must agree on."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="den-run-")
        cls.out = os.path.join(cls.tmp, "out")
        cls.meta = os.path.join(cls.out, artifacts.MANIFEST.filename)
        os.makedirs(cls.out)
        for kind in ("unproduced", "unbought"):
            for name in os.listdir(os.path.join(SEEDS, kind)):
                shutil.copy(os.path.join(SEEDS, kind, name), cls.out)
        for name in SEEDED_MANIFESTS:
            stamp_implementation(os.path.join(cls.out, name))
        write_genres_moods_answer(cls.out, DERIVED)
        spec = read_json(os.path.join(cls.out, "vectors-premise.json"))
        os.unlink(os.path.join(cls.out, "vectors-premise.json"))
        vector_blob.write(os.path.join(cls.out, artifacts.PREMISE_VECTORS.filename), spec["keys"],
                          bytes(x & 0xFF for key in spec["keys"] for x in embedding(f"premise {key}")),
                          spec["dims"])

        cls.embed = EmbedStandIn()
        canary = os.path.join(cls.tmp, "canary.json")
        cls.canary = cls.embed.canary(canary)
        env = {k: v for k, v in os.environ.items() if not k.startswith("WIKIMEDIA_ENTERPRISE")}
        env.update({"TMDB_API_KEY": "fixture-not-a-key", "DEN_CACHE_DIR": os.path.join(cls.tmp, "cache"),
                    "DEN_EMBED_URL": cls.embed.url, "DEN_EMBED_CANARY": canary})
        cls.upstreams = Upstreams(http.request)
        den = load_den()
        cls.transcript = []
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(http, "request", cls.upstreams.request), \
                mock.patch.object(socket.socket, "connect", offline_connect(socket.socket.connect)), \
                mock.patch.object(genres_moods, "CURATED", CURATED), \
                mock.patch.object(genres_moods.gmm, "run_eval", gate_passes):
            cls.drive(den)

    @classmethod
    def tearDownClass(cls):
        cls.embed.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def den(cls, den, *argv):
        """`den <argv>` in this process; `(exit code, what it said)`."""
        said = io.StringIO()
        with contextlib.redirect_stderr(said), contextlib.redirect_stdout(said):
            code = den.main(list(argv))
        cls.transcript.append((argv, code, said.getvalue()))
        return code, said.getvalue()

    @classmethod
    def expect(cls, code, said, argv):
        if code != 0:
            raise AssertionError(f"`den {' '.join(argv)}` refused:\n{said[-3000:]}")

    @classmethod
    def drive(cls, den):
        common = ("--out-dir", cls.out, "--stamp-meta", cls.meta)
        cls.run_code, cls.run_said = cls.den(den, "run", *common, "--mode", "export")
        #: The stages `den run` began, in the order it began them, read off its `==> <stage>` headers.
        cls.began = re.findall(r"^==> (\w+)$", cls.run_said, re.M)
        cls.facts_refusal = cls.den(den, "stage", "facts", *common, "--dataset-version", GIVEN_VERSION)

    # ---- how far `den run` gets -------------------------------------------------------------------------

    def test_den_run_runs_every_stage_it_is_allowed_to(self):
        """Unattended, given no version: the stages from `facts` on take the one `finalize` derived."""
        self.assertEqual(self.run_code, 0, f"den run refused:\n{self.run_said[-3000:]}")
        self.assertEqual(self.began, [m.NAME for m in pipeline.stages() if m.NAME not in ("classify", "publish")])

    def test_a_given_version_is_only_a_check(self):
        """One given by hand that disagrees with the manifest is refused rather than obeyed: the facts would
        be written under a name the corpus join and the store would not look for."""
        code, said = self.facts_refusal
        self.assertEqual(code, 1)
        self.assertIn(f"--dataset-version {GIVEN_VERSION} is not this out-dir's generation", said)
        self.assertTrue(os.path.exists(self.path(artifacts.STORE)), "the stages after it ran on the derived one")

    def test_den_run_drains_fetch_over_a_title_below_the_floor(self):
        """`enrich` once left a below-floor title pending for good, so `remaining` never reached 0 and the
        drain refused its second batch as "every title below the vote floor", leaving the other media
        undrained. The fixture's universe holds one, and `den run` goes on past fetch to finalize."""
        self.assertLess(self.began.index("fetch"), self.began.index("finalize"))
        self.assertIn(f"==> fetch: {os.path.join(self.out, artifacts.ENRICHED.filename)} "
                      f"(1 movie batch(es), 1 tv batch(es))", self.run_said)

    def test_fetch_writes_no_empty_batch(self):
        """Each attempt that admitted nothing wrote a `batch-N.json` holding `[]`."""
        enriched = os.path.join(self.out, artifacts.ENRICHED.filename)
        for name in os.listdir(enriched):
            self.assertTrue(read_json(os.path.join(enriched, name)), f"{name} holds no title")

    def test_den_run_leaves_out_exactly_the_stages_that_buy_or_publish(self):
        order = [m.NAME for m in pipeline.stages()]
        self.assertEqual([name for name in order if name not in self.began], ["classify", "publish"])

    # ---- the published shapes agree with each other ---------------------------------------------------

    def path(self, artifact):
        return os.path.join(self.out, artifact.filename.format(version=self.version))

    @property
    def version(self):
        return read_json(self.meta)["datasetVersion"]

    def labels(self):
        return keys_of(read_json(self.path(artifacts.VECTOR_LABELS))["records"])

    def facts(self):
        return read_json(self.path(artifacts.FACTS))

    def corpus(self):
        with gzip.open(self.path(artifacts.CORPUS), "rt", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_the_manifest_describes_the_labels_and_the_vectors_it_ships_beside(self):
        meta = read_json(self.meta)
        _count, dims, blob_keys, _blob, _base = vector_blob.read(self.path(artifacts.VECTORS))
        self.assertEqual(set(blob_keys), self.labels(), "every labelled title has exactly one plot vector")
        self.assertEqual(meta["count"], len(self.labels()))
        self.assertEqual(meta["dims"], dims)
        self.assertEqual(meta["labelsSha256"], sha256(self.path(artifacts.VECTOR_LABELS)))
        self.assertEqual(meta["vectorsSha256"], sha256(self.path(artifacts.VECTORS)))
        self.assertEqual(meta["embeddingSpace"], self.canary["spaceId"], "the space the canary verified")
        self.assertEqual(meta["embedderMaxTokens"], EmbedStandIn.HEALTH["max_tokens"])

    def test_the_facts_hold_both_passes_under_the_manifests_version(self):
        facts = self.facts()
        self.assertEqual(facts["datasetVersion"], self.version)
        with_vector = keys_of(r for r in facts["records"] if r["hasVector"])
        without = keys_of(r for r in facts["records"] if not r["hasVector"])
        self.assertEqual(with_vector, self.labels(), "the corpus pass covers exactly the labelled titles")
        with open(os.path.join(self.out, artifacts.DELTA_IDS.filename), encoding="utf-8") as fh:
            self.assertEqual(without, set(fh.read().split()), "the delta pass covers exactly the delta ids")

    def genres_moods(self):
        return read_json(self.path(artifacts.GENRES_MOODS))["titles"]

    def test_the_corpus_is_the_facts_joined_with_the_passes(self):
        facts, corpus = keys_of(self.facts()["records"]), self.corpus()
        self.assertEqual({row["key"] for row in corpus}, facts)
        self.assertEqual(len(corpus), len(facts), "one row per title")
        self.assertEqual({r["key"]: r["labels"] for r in corpus if r["labels"]}, self.genres_moods())
        self.assertEqual(set(self.genres_moods()), self.labels(), "every title with genres & moods was embedded")
        self.assertFalse([r["key"] for r in corpus if "premiseLabels" in r], "the premise copy is not joined")

    def test_the_genres_and_moods_jev_derived_reach_every_reader(self):
        """The reason for the switch: a title the curated file lacks gets its genres & moods from the
        genres & moods stage, and every stage after it reads them there — the embedded document, the
        labels `finalize` ships beside the vectors, the corpus and the store."""
        self.assertNotIn(DERIVED, read_json(CURATED)["titles"], "the fixture must not curate it")
        derived = self.genres_moods()[DERIVED]
        self.assertEqual((derived["source"], derived["primaryGenre"]), ("jev-v3", DERIVED_PRIMARY))
        self.assertEqual([s["label"] for s in derived["subgenres"]], ["Whodunit/Murder Mystery"])
        self.assertEqual([m["label"] for m in derived["moods"]], ["Tense/Edge-of-seat"])
        shipped = {f"{r['mediaType']}:{r['tmdbId']}": r
                   for r in read_json(self.path(artifacts.VECTOR_LABELS))["records"]}
        self.assertEqual({k: shipped[DERIVED][k] for k in ("primaryGenre", "subgenres", "moods")},
                         {k: derived[k] for k in ("primaryGenre", "subgenres", "moods")})
        with open(self.path(artifacts.EMBED_LABELS), encoding="utf-8") as fh:
            embedded = [json.loads(line) for line in fh if f'"tmdbId":{DERIVED.split(":")[1]}' in line]
        self.assertEqual([r["primaryGenre"] for r in embedded if r["mediaType"] == "tv"], [DERIVED_PRIMARY])
        row = next(r for r in self.corpus() if r["key"] == DERIVED)
        self.assertEqual(row["labels"], derived)

    def test_a_title_only_the_premise_labels_named_has_no_genres_and_moods(self):
        """tv:900006's genres & moods were only ever the premise pass's copy, which is no longer a source."""
        premise = keys_of(read_json(self.path(artifacts.PREMISE_LABELS))["records"])
        self.assertIn("tv:900006", premise)
        self.assertNotIn("tv:900006", self.genres_moods())
        self.assertIsNone(next(r for r in self.corpus() if r["key"] == "tv:900006")["labels"])

    def test_the_store_is_the_corpus_and_the_manifest_names_it(self):
        store_path = self.path(artifacts.STORE)
        store, meta = Store(store_path), read_json(self.meta)
        corpus = sorted(self.corpus(), key=lambda r: (r["mediaType"] != "movie", r["tmdbId"]))
        self.assertEqual(store.keys(), [row["key"] for row in corpus])
        self.assertEqual(store.rows, len(self.facts()["records"]))
        self.assertEqual(store.version, self.version)
        self.assertEqual(meta["storeFile"], os.path.basename(store_path))
        self.assertEqual(meta["storeSha256"], sha256(store_path))
        self.assertEqual(meta["storeBytes"], os.path.getsize(store_path))
        for entry in meta["storeInputs"]:
            self.assertEqual(entry["sha256"], sha256(entry["path"]), f"storeInputs {entry['arg']}")

    def test_a_critics_list_filed_under_p179_is_not_the_franchise(self):
        """movie:900001 is filed under a Wikimedia list article (Q98000000, the least Q-id) and a film
        trilogy. The series reaches the store; the list is named nowhere."""
        record = next(r for r in self.facts()["records"] if r["mediaType"] == "movie" and r["tmdbId"] == 900001)
        self.assertEqual(record["franchise"], ["Q98000001"])
        self.assertNotIn("Q98000000", self.facts()["entities"])
        store = Store(self.path(artifacts.STORE))
        offsets, values = store.column("franchise_o", "I", 4), store.column("franchise_v", "I", 4)
        row = store.keys().index("movie:900001")
        self.assertEqual(values[offsets[row]:offsets[row + 1]], [98000001])

    def test_every_shipped_title_has_a_vector_and_says_which(self):
        store = Store(self.path(artifacts.STORE))
        plot, premise = store.flags("vec_plot_has"), store.flags("vec_premise_has")
        premise_keys = keys_of(read_json(self.path(artifacts.PREMISE_LABELS))["records"])
        for key in store.keys():
            self.assertTrue(plot[key] or premise[key], f"{key} ships with no vector at all")
            self.assertEqual(bool(plot[key]), key in self.labels(), f"{key}: plot vector vs the plot labels")
            self.assertEqual(bool(premise[key]), key in premise_keys, f"{key}: premise vector vs its labels")
        self.assertEqual(store.flags("facts_has_vec"), plot, "the facts' hasVector stamp agrees with the blob")

    def test_nothing_below_the_floor_ships(self):
        """The title was offered — it is in the universe — and the gate kept it out of everything after."""
        universe = read_json(os.path.join(self.out, artifacts.UNIVERSE_MOVIE.filename))
        self.assertIn(BELOW_FLOOR, keys_of(universe), "the fixture must offer the title for this to prove anything")
        enriched = os.path.join(self.out, artifacts.ENRICHED.filename)
        batches = [row for name in sorted(os.listdir(enriched))
                   for row in read_json(os.path.join(enriched, name))]
        with open(os.path.join(self.out, artifacts.ARTICLES.filename), encoding="utf-8") as fh:
            dumped = [json.loads(line) for line in fh]
        with open(os.path.join(self.out, artifacts.EMBED_LABELS.filename), encoding="utf-8") as fh:
            embedded = [json.loads(line) for line in fh]
        for what, keys in (("the enriched batches", keys_of(batches)), ("the article dump", keys_of(dumped)),
                           ("the embed store", keys_of(embedded)), ("the labels", self.labels()),
                           ("the facts", keys_of(self.facts()["records"])),
                           ("the corpus", {r["key"] for r in self.corpus()}),
                           ("the store", set(Store(self.path(artifacts.STORE)).keys()))):
            self.assertNotIn(BELOW_FLOOR, keys, f"a title below every floor reached {what}")

    # ---- the pathologies the fixture was built to carry --------------------------------------------------

    def enriched(self, key):
        enriched = os.path.join(self.out, artifacts.ENRICHED.filename)
        rows = [row for name in sorted(os.listdir(enriched)) for row in read_json(os.path.join(enriched, name))]
        return next(row for row in rows if f"{row['mediaType']}:{row['tmdbId']}" == key)

    def test_each_title_took_the_path_it_is_in_the_fixture_for(self):
        store_keys = set(Store(self.path(artifacts.STORE)).keys())
        facts = {f"{r['mediaType']}:{r['tmdbId']}": r for r in self.facts()["records"]}
        # A film and a series sharing tmdbId 900001 are two titles all the way through.
        self.assertTrue({"movie:900001", "tv:900001"} <= store_keys)
        # Below TMDB's regional floor, admitted on IMDb's count, grounded on the French Wikipedia.
        jardin = self.enriched("movie:900002")
        self.assertEqual((jardin["plotLanguage"], jardin["plotArticleRole"]), ("fr", "own-other-language"))
        # An English article with no describing section: ships plotless, never dumped for the classifier.
        harbour = self.enriched("movie:900003")
        self.assertEqual((harbour["hasWikiPlot"], harbour["noPlotReason"]), (False, "noSection"))
        with open(os.path.join(self.out, artifacts.ARTICLES.filename), encoding="utf-8") as fh:
            self.assertNotIn("movie:900003", keys_of(json.loads(line) for line in fh))
        self.assertIn("movie:900003", self.labels())
        # The disambiguator comes off the display title; the non-Latin original title survives.
        self.assertEqual(facts["movie:900003"]["titles"]["en"], "Quiet Harbour")
        self.assertEqual(facts["tv:900005"]["titles"]["orig"], "東京ナイトリレー")
        # A premise vector and no plot vector, so a vectorless facts record.
        self.assertEqual(facts["tv:900006"]["hasVector"], False)
        self.assertIn("tv:900006", store_keys)

    def test_the_overview_is_wikipedias_or_nothing(self):
        """TMDB's text never enters a record: the fixture carries none, and `overview` is the plot the
        Wikipedia fetch read."""
        for key, body in read_json(os.path.join(UPSTREAM, "tmdb.json")).items():
            self.assertFalse({"overview", "tagline"} & set(body), f"{key}: the fixture must carry no TMDB text")
        self.assertTrue(self.enriched("movie:900001")["overview"].startswith("On a storm-bound island"))
        self.assertEqual(self.enriched("movie:900003")["overview"], "")

    def test_only_the_recorded_upstreams_were_asked(self):
        self.assertEqual(self.upstreams.hosts, {tmdb_api.HOST, "datasets.imdbws.com", "query.wikidata.org",
                                                "en.wikipedia.org", "fr.wikipedia.org", "ja.wikipedia.org"})


class UnproducedSeeds(unittest.TestCase):
    """The six inputs `pipeline/fixture-corpus/seeds/unproduced/` stands in for have no producing stage."""

    def test_no_stage_produces_a_seeded_input(self):
        owners = {bind(entry).artifact.name: module.NAME for module in pipeline.stages() for entry in module.OUTPUTS}
        for name, files in UNPRODUCED.items():
            self.assertNotIn(
                name, owners,
                f"stage `{owners.get(name)}` now produces `{name}`. Delete "
                f"{', '.join(os.path.join('pipeline/fixture-corpus/seeds/unproduced', f) for f in files)} "
                f"and its entry in UNPRODUCED, so the run builds it rather than reading a hand-made copy "
                f"(oxyc/den-dataset#27).")

    def test_each_seed_carries_the_name_its_artifact_is_read_under(self):
        catalogue = {artifact.name: artifact for artifact in artifacts.CATALOGUE}
        for name, files in UNPRODUCED.items():
            written = files[0].replace("vectors-premise.json", artifacts.PREMISE_VECTORS.filename)
            self.assertTrue(fnmatch.fnmatch(written, catalogue[name].filename),
                            f"{files[0]} would not be found as `{name}` ({catalogue[name].filename})")
            for seed in files:
                self.assertTrue(os.path.isfile(os.path.join(SEEDS, "unproduced", seed)), seed)
        self.assertEqual(sorted(os.listdir(os.path.join(SEEDS, "unproduced"))),
                         sorted(f for files in UNPRODUCED.values() for f in files),
                         "a file in seeds/unproduced/ that UNPRODUCED does not name")


class SeededManifests(unittest.TestCase):
    def test_a_seed_manifest_as_committed_is_refused_by_the_audit(self):
        """The seeds record no implementation digests of their own, and the audit refuses a manifest that
        records none — so the run's audit passes only on what `stamp_implementation` wrote."""
        for name in SEEDED_MANIFESTS:
            path = next(os.path.join(SEEDS, kind, name) for kind in ("unbought", "unproduced")
                        if os.path.isfile(os.path.join(SEEDS, kind, name)))
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "records no implementation"):
                audit_combined.validate_implementation(name, read_json(path)["config"])


class NoStageReadsWhatALaterOneWrites(unittest.TestCase):
    def test_a_fresh_out_dir_can_start(self):
        """`docfacts` and `embed` read `labels-t02.json`, which `finalize` writes after them, so a fresh
        out-dir could only start from a previous run's copy — the fixture seeded one. The one read of a
        later stage's output left is by design: a delta skips what the previous run's genres & moods name,
        because a delta extends an out-dir rather than starting one, and an export universe reads nothing."""
        stages = pipeline.stages()
        for i, module in enumerate(stages):
            later = {bind(e).artifact.name: m.NAME for m in stages[i + 1:] for e in m.OUTPUTS}
            reads = {bind(e).artifact.name for e in module.INPUTS} & set(later)
            if module.NAME == "worklist":
                reads -= {artifacts.GENRES_MOODS.name}
            self.assertEqual({name: later[name] for name in reads}, {},
                             f"{module.NAME} reads what a later stage writes")


class TheClassifyPassStillBuys(unittest.TestCase):
    def test_classify_is_left_out_of_an_unpaid_run(self):
        """`seeds/unbought/` stands in for a pass CI cannot buy. If classify stops spending, `den run` runs
        it and the seed shadows its output: delete the seed."""
        classify = pipeline.stage("classify")
        self.assertTrue(classify.SPENDS and not getattr(classify, "FREE_WITHOUT_SPEND", False),
                        "classify runs without --spend now: delete pipeline/fixture-corpus/seeds/unbought/")


if __name__ == "__main__":
    unittest.main()
