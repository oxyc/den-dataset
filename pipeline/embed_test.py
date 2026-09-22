#!/usr/bin/env python3
"""The embed stage — its gates, its resume, and what it writes — against a den-embed on a local port.

The service is a stand-in (`lib/denembed_test.Service`): deterministic vectors, a `/health` a test can
change. What is under test is everything this stage decides — which rows it appends, what it refuses and
when — and none of that needs the real model. Equivalence with the Swift `embed-corpus` was measured
separately (oxyc/den-dataset#27): all 47,539 composed documents identical, and a 400-title slice embedded
through both, in two resumed segments each, gave the same stores row for row and the same finalized blob
byte for byte.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import pipeline

from . import artifacts, embed, fetch
from .contract import Context, StageError, bind
from lib.denembed_test import Service, canary_for, vector

LABEL = {"primaryGenre": "Drama", "source": "llm", "animated": False,
         "subgenres": [{"label": "Heist", "confidence": 0.9}], "moods": []}


def record(tmdb_id, media="movie", **extra):
    return dict(LABEL, tmdbId=tmdb_id, mediaType=media, **extra)


def lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines() if line]


class Staged(unittest.TestCase):
    """Inputs for three titles, a service, and a canary whose answers are that service's."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name
        self.service = Service(health={"model": "bge-m3", "dims": 8, "vector_epoch": 1, "runtime": "t/1",
                                       "max_tokens": 1024})
        self.addCleanup(self.service.close)
        canary = os.path.join(self.out, "canary.json")
        canary_for(self.service, canary)
        for name, value in (("DEN_EMBED_URL", self.service.url), ("DEN_EMBED_CANARY", canary)):
            previous = os.environ.get(name)
            os.environ[name] = value
            self.addCleanup(lambda n=name, p=previous: os.environ.pop(n) if p is None else
                            os.environ.__setitem__(n, p))
        self.write_labels([record(1, extra="dropped"), record(2), record(3, "tv")])
        os.makedirs(os.path.join(self.out, "enriched"))
        self.batch(1, [{"tmdbId": i, "mediaType": m, "overview": f"Plot {i}.", "hasWikiPlot": True}
                       for i, m in ((1, "movie"), (2, "movie"), (3, "tv"), (9, "movie"))])
        with open(os.path.join(self.out, "doc-facts.json"), "w", encoding="utf-8") as fh:
            json.dump({"movie:1": {"directors": ["D"], "genres": ["drama"]}}, fh)

    def write_labels(self, records):
        with open(os.path.join(self.out, "labels-t02.json"), "w", encoding="utf-8") as fh:
            json.dump({"taxonomyVersion": "t02", "count": len(records), "records": records}, fh)

    def batch(self, number, rows):
        with open(os.path.join(self.out, "enriched", f"batch-{number}.json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh)

    def run_stage(self, **kwargs):
        with redirect_stdout(io.StringIO()) as printed:
            made = embed.run(Context(out_dir=self.out, dataset_version="test", **kwargs))
        return made, json.loads(printed.getvalue().splitlines()[-1])

    def refused(self, **kwargs):
        with self.assertRaises(StageError) as refusal:
            self.run_stage(**kwargs)
        return str(refusal.exception)

    def store(self, name="labels.jsonl"):
        path = os.path.join(self.out, "index", name)
        return lines(path) if os.path.exists(path) else []

    def posts(self):
        return [body for method, path, body in self.service.requests if path.endswith("/embed/batch")]


class Writing(Staged):
    def test_every_labelled_title_lands_with_the_services_vector(self):
        _, summary = self.run_stage()
        self.assertEqual(summary["written"], 3)
        self.assertEqual(summary["missingLabel"], 1, "movie:9 is enriched and has no label")
        sent = [text for body in self.posts() for text in body["texts"]
                if text not in ("first text", "second text")]
        self.assertEqual(sent[0], "Genres: drama. Themes: Heist. Plot: Plot 1.")
        self.assertEqual([r["tmdbId"] for r in self.store()], [1, 2, 3])
        self.assertEqual([row["v"] for row in self.store("vectors.jsonl")], [vector(text) for text in sent])

    def test_the_stored_record_is_the_declared_shape(self):
        """A key the record does not declare never reaches the store, and so never the shipped labels."""
        self.run_stage()
        self.assertNotIn("extra", self.store()[0])
        self.assertEqual(sorted(self.store()[0]), ["animated", "mediaType", "moods", "primaryGenre", "source",
                                                   "subgenres", "tmdbId"])

    def test_every_request_fits_the_services_budget(self):
        """At the Swift's default of 15, every request past the cap is a 413, which is not retried."""
        self.write_labels([record(i) for i in range(1, 30)])
        self.batch(1, [{"tmdbId": i, "mediaType": "movie", "overview": "P.", "hasWikiPlot": True}
                       for i in range(1, 30)])
        self.run_stage()
        self.assertTrue(self.posts())
        self.assertLessEqual(max(len(body["texts"]) for body in self.posts()), embed.CHUNK)
        self.assertLessEqual(embed.CHUNK * embed.MAX_TOKENS, embed.TOKEN_BUDGET)
        self.assertGreater(15 * embed.MAX_TOKENS, embed.TOKEN_BUDGET)

    def test_the_records_that_name_the_space_are_written(self):
        self.run_stage()
        for artifact in (artifacts.COMPOSITION, artifacts.EMBEDDER, artifacts.EMBEDDING_SPACE):
            self.assertTrue(os.path.exists(Context(out_dir=self.out, dataset_version="t").path(artifact)))
        with open(os.path.join(self.out, "index", "composition.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{\n  "docShape" : "lean",\n  "dropDirector" : true,\n'
                                        '  "plotCap" : 3500\n}')


class Resume(Staged):
    def test_a_second_run_appends_only_what_is_missing(self):
        """The property that makes a 12-hour run interruptible: segment it, restart the service between
        segments, and the store is the union with nothing twice."""
        _, first = self.run_stage(limit=1)
        _, second = self.run_stage()
        _, third = self.run_stage()
        self.assertEqual((first["written"], second["written"], third["written"]), (1, 2, 0))
        self.assertEqual(sorted(r["tmdbId"] for r in self.store()), [1, 2, 3])

    def test_a_limit_of_zero_embeds_nothing(self):
        """The Swift embedded one title at `--limit 0`: its check ran after the first append."""
        _, summary = self.run_stage(limit=0)
        self.assertEqual(summary["written"], 0)
        self.assertEqual([body for body in self.posts() if body["texts"][0] not in ("first text", "second text")],
                         [], "only the canary was asked")
        self.assertEqual(self.store(), [])

    def test_a_label_written_without_its_vector_is_repaired(self):
        self.run_stage(limit=2)
        with open(os.path.join(self.out, "index", "labels.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record(3, "tv")) + "\n")
        self.run_stage()
        self.assertEqual([r["tmdbId"] for r in self.store()], [r["tmdbId"] for r in self.store("vectors.jsonl")])
        self.assertEqual(len(self.store()), 3)

    def test_a_torn_final_line_is_repaired_even_when_the_counts_match(self):
        """A kill mid-write leaves a truncated line that still counts as a line, so both files look equal
        in length while the last vector belongs to no title."""
        self.run_stage(limit=2)
        path = os.path.join(self.out, "index", "vectors.jsonl")
        with open(path, encoding="utf-8") as fh:
            rows = fh.read().splitlines()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(rows[0] + "\n" + rows[1][:12] + "\n")
        self.run_stage()
        self.assertEqual([r["tmdbId"] for r in self.store()], [r["tmdbId"] for r in self.store("vectors.jsonl")])

    def test_lines_that_parse_but_name_different_titles_are_a_tear_too(self):
        """Equal counts, every line valid JSON, and a shifted body: every title after the shift would ship
        its neighbour's vector. Alignment is by title, not by parse."""
        self.run_stage(limit=2)
        path = os.path.join(self.out, "index", "vectors.jsonl")
        with open(path, encoding="utf-8") as fh:
            rows = fh.read().splitlines()
        shifted = json.loads(rows[1])
        shifted["tmdbId"] = 3
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(rows[0] + "\n" + json.dumps(shifted) + "\n")
        self.run_stage()
        self.assertEqual([r["tmdbId"] for r in self.store()], [r["tmdbId"] for r in self.store("vectors.jsonl")])

    def test_a_divergence_past_one_chunk_is_refused_and_nothing_is_changed(self):
        """Truncating to a divergence at line 1 of a 1,001-row store is data loss, not a repair."""
        index = os.path.join(self.out, "index")
        os.makedirs(index)
        with open(os.path.join(index, "labels.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("{not json\n" + "".join(json.dumps(record(i)) + "\n" for i in range(1000)))
        with open(os.path.join(index, "vectors.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps({"tmdbId": i, "v": [0]}) + "\n" for i in range(-1, 1000)))
        with open(os.path.join(index, "labels.jsonl"), encoding="utf-8") as fh:
            before = fh.read()
        self.write_embedder_record()
        self.assertIn("diverges at line 1", self.refused())
        with open(os.path.join(index, "labels.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)

    def write_embedder_record(self):
        os.makedirs(os.path.join(self.out, "index"), exist_ok=True)
        with open(os.path.join(self.out, "index", "embedder.json"), "w", encoding="utf-8") as fh:
            json.dump({"model": "bge-m3", "dims": 8, "vectorEpoch": 1, "runtime": "t/0", "maxTokens": 1024}, fh)
        with open(os.path.join(self.out, "index", "composition.json"), "w", encoding="utf-8") as fh:
            json.dump(embed.SHIPPED_COMPOSITION, fh)


class Gates(Staged):
    def test_a_service_that_misses_one_canary_byte_writes_nothing(self):
        drifted = vector("second text")
        drifted[0] += 1
        self.service.override = {"second text": drifted}
        self.assertIn("embed canary FAILED", self.refused())
        self.assertEqual(self.store(), [])
        self.assertFalse(os.path.exists(os.path.join(self.out, "index", "embedding-space.json")))

    def test_a_different_embedder_is_refused_before_anything_is_appended(self):
        self.run_stage(limit=1)
        self.service.health = dict(self.service.health, vector_epoch=2)
        self.assertIn("mix two embedders", self.refused())
        self.assertEqual(len(self.store()), 1)

    def test_a_record_from_before_the_epoch_gets_advice_it_can_follow(self):
        self.run_stage(limit=1)
        with open(os.path.join(self.out, "index", "embedder.json"), "w", encoding="utf-8") as fh:
            json.dump({"model": "bge-m3", "dims": 8, "runtime": "t/0", "maxTokens": 1024}, fh)
        self.assertIn("add a vectorEpoch of 1", self.refused())

    def test_rows_with_no_record_of_what_embedded_them_are_refused(self):
        """Adopting today's service as their identity writes a guess down as a fact."""
        self.run_stage(limit=1)
        os.remove(os.path.join(self.out, "index", "embedder.json"))
        self.assertIn("what embedded it is unknown", self.refused())

    def test_a_store_composed_another_way_is_refused(self):
        self.run_stage(limit=1)
        with open(os.path.join(self.out, "index", "composition.json"), "w", encoding="utf-8") as fh:
            json.dump(dict(embed.SHIPPED_COMPOSITION, plotCap=1500), fh)
        self.assertIn("two document shapes", self.refused())

    def test_rows_with_no_record_of_their_composition_are_refused_naming_whose_values_are_known(self):
        self.run_stage(limit=1)
        os.remove(os.path.join(self.out, "index", "composition.json"))
        self.assertIn("out-t02-cc0b", self.refused())

    def test_a_service_that_would_cut_the_documents_is_refused(self):
        """Refused on the fit alone: the canary here was recorded at the same 512, so it would pass."""
        self.service.health = dict(self.service.health, max_tokens=512)
        canary_for(self.service, os.environ["DEN_EMBED_CANARY"])
        self.assertIn("a plot cap of 3500", self.refused())
        self.assertEqual(self.posts(), [], "nothing is embedded, not even the canary")

    def test_the_writer_needs_a_verified_space(self):
        """The stage used to check for the space record after the binary ran, because a binary built
        before the gate embedded without one. The writer takes the verdict as an argument now."""
        with self.assertRaises(StageError):
            embed.embed_into((io.StringIO(), io.StringIO()), self.service.url, None, embed.CHUNK, 0, [[]])


class Inputs(Staged):
    def test_a_missing_doc_facts_stops_the_stage(self):
        """Without them the document is not the CC0 shape at all."""
        os.remove(os.path.join(self.out, "doc-facts.json"))
        self.assertIn("./den stage docfacts", self.refused())

    def test_a_missing_enrichment_stops_the_stage(self):
        for name in os.listdir(os.path.join(self.out, "enriched")):
            os.remove(os.path.join(self.out, "enriched", name))
        os.rmdir(os.path.join(self.out, "enriched"))
        self.assertIn(fetch.HOW, self.refused())

    def test_dumping_documents_needs_no_service(self):
        """The documents travel to the den-embed that will serve them; standing one up here purely to write
        text would defeat the point."""
        self.service.close()
        os.environ["DEN_EMBED_URL"] = "http://127.0.0.1:9"
        docs = os.path.join(self.out, "docs.jsonl")
        made, summary = self.run_stage(dump_docs=docs)
        self.assertEqual((made, summary["written"]), (docs, 3))
        self.assertEqual([row["key"] for row in lines(docs)], ["movie:1", "movie:2", "tv:3"])
        self.assertEqual(self.store(), [])


class Topology(unittest.TestCase):
    def test_the_stores_are_owned_by_the_stage_that_writes_them(self):
        for name in ("embed_labels", "embed_vectors", "composition", "embedder", "embedding_space"):
            self.assertEqual(getattr(artifacts, name.upper()).producer, "")
            self.assertEqual(pipeline.producers()[name], (embed.PRODUCER, embed.HOW, False))
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(__file__)), embed.PRODUCER)))

    def test_the_composition_is_the_shipped_stores(self):
        self.assertEqual(embed.SHIPPED_COMPOSITION, {"docShape": "lean", "dropDirector": True, "plotCap": 3500})

    def test_the_labels_keep_one_name_and_are_read_under_their_own(self):
        bound = {bind(e).name: bind(e) for e in embed.INPUTS}["vector_labels"]
        self.assertEqual(bound.flag(), "--labels")

    def test_it_runs_after_the_doc_facts_and_before_finalize(self):
        order = list(pipeline.STAGES)
        self.assertLess(order.index("docfacts"), order.index("embed"))
        self.assertLess(order.index("embed"), order.index("finalize"))


if __name__ == "__main__":
    unittest.main()
