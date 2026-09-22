#!/usr/bin/env python3
"""The corpus stage — that it is a wrapper, and that the two things this port added still hold.

The join itself is tested where it lives, in `scripts/v2/test_consolidate_corpus.py`: every guard in it
was bought by a failure that happened, and none of them is reimplemented here. What is tested here is the
stage around it, which had to answer two questions the store stage did not:

  * **a repeatable input.** `--combined` and `--delta` take a SET of shards, and the eleven titles that
    vanished for a day vanished because a producer read one shard of three. So the declaration names the
    set, not a file, and the stage passes the flag once per member — checked against the script's own
    parser rather than against a copy of its argument list;
  * **an output that is the next stage's input.** `corpus` is written here and read by `store`, so the
    producer registry can say who owns it from the ORDER rather than from a `producer` written on the
    artifact. The test for that is that the artifact carries no producer of its own and the registry
    still answers.

The fixture is `scripts/v2/test_consolidate_corpus.py`'s, reused rather than rebuilt: a second definition
of what a valid pass shard looks like is a second thing to keep true.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

import pipeline

from . import artifacts, corpus, store
from .contract import Context, StageError, bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V2 = os.path.join(REPO, "scripts", "v2")

sys.path.insert(0, V2)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


import audit_combined  # noqa: E402  — the bundle auditor the stage runs, for its recorded lineage

script = load("consolidate_corpus", os.path.join(V2, "consolidate_corpus.py"))
fixture = load("test_consolidate_corpus", os.path.join(V2, "test_consolidate_corpus.py"))

VERSION = "testver"

#: What `write_inputs` lays down, by artifact. The names are the DECLARED ones, so a test that writes
#: these and then runs the stage with no overrides exercises the filename templates too.
FIXTURE_FILES = {
    "combined": ("combined-v1-r2.jsonl", "combined-v1-r2-token-fallback.jsonl"),
    "delta": ("delta-v2.jsonl", "delta-v2-rest.jsonl"),
    "facts": (f"facts-{VERSION}.json",),
    "genres_moods": ("genres-moods.json",),
}

#: Two pass shards, one title that carries facts and no pass row at all, and a delta answer — the joins
#: the stage has to hand over intact.
PASS_KEYS = ("movie:1", "movie:2", "tv:9")
FACTS_KEYS = PASS_KEYS + ("movie:77",)


#: The source files `run_combined.py` hashes into every shard it writes, by the name it records each one
#: under, and which the stage's audit reads back. Spelled here rather than imported so a fixture manifest is
#: built the way the pass builds one.
IMPLEMENTATION = {
    "run_combined.py": os.path.join(V2, "run_combined.py"),
    "article_sections.py": os.path.join(REPO, "pipeline", "article_sections.py"),
    "combined_questions.py": os.path.join(REPO, "pipeline", "combined_questions.py"),
    "typesafe_client.py": os.path.join(REPO, "lib", "typesafe_client.py"),
}


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def write_manifest(shard, implementation=None, started=None):
    """The sidecar the pass writes beside a shard, under the name it derives from `--out`.

    Only the provenance the stage's audit reads is filled in, plus the run start when a test orders
    shards by it; the rest of a real manifest belongs to the row-level readback, which is not a
    precondition of a join.
    """
    digests = {name: sha256(path) for name, path in IMPLEMENTATION.items()}
    digests.update(implementation or {})
    manifest = {"runId": "corpus-stage-test", "configSha256": "config-test",
                "config": {"implementationSha256": digests}}
    if started:
        manifest["runStartedAt"] = started
    with open(shard + ".manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)


def write_inputs(out):
    """Every declared input, under its declared filename — each pass shard with the sidecar it is audited by."""
    first, second = (os.path.join(out, n) for n in FIXTURE_FILES["combined"])
    fixture.write(first, [fixture.combined(1), fixture.combined(2)])
    fixture.write(second, [fixture.combined(9, media="tv")])
    fixture.write(os.path.join(out, FIXTURE_FILES["delta"][0]),
                  [{"mediaType": "movie", "tmdbId": 1,
                    "answers": {"critique__craft": {"p": 0.7}, "made_for_children": {"choice": "no"}}}])
    # The second shard: the corrected pass ran in two parts, because the first file's manifest pins a
    # client the connection fix changed and so cannot resume.
    fixture.write(os.path.join(out, FIXTURE_FILES["delta"][1]),
                  [{"mediaType": "tv", "tmdbId": 9,
                    "answers": {"critique__craft": {"p": 0.4}, "made_for_children": {"choice": "no"}}}])
    for name in FIXTURE_FILES["combined"] + FIXTURE_FILES["delta"]:
        write_manifest(os.path.join(out, name))
    fixture.facts_file(os.path.join(out, FIXTURE_FILES["facts"][0]), list(FACTS_KEYS),
                       entities={"Q42": {"en": "Ada Director"}})
    fixture.genres_moods_file(os.path.join(out, FIXTURE_FILES["genres_moods"][0]), list(PASS_KEYS))


def context(out, **kwargs):
    return Context(out_dir=out, dataset_version=VERSION, **kwargs)


class Declaration(unittest.TestCase):
    def test_the_stage_declares_exactly_what_the_script_reads(self):
        """The pin the derived registry hangs from, in the same shape the store stage uses: the names the
        stage hands over are the script's own argument names, so an input can only be added or dropped in
        one place without something going red."""
        self.assertEqual([bind(e).arg for e in corpus.INPUTS], list(script.INPUT_ARGS))

    def test_the_outputs_are_the_corpus_and_its_entity_sidecar(self):
        """The sidecar is written by the same run and is unreadable apart from the corpus, so a stage that
        declared only the corpus would leave 3.6 MB of entity names owned by nobody."""
        self.assertEqual([bind(e).name for e in corpus.OUTPUTS], ["corpus", "entities"])

    def test_the_genres_and_moods_reach_the_script_as_its_labels(self):
        """`genres-moods.json` is `--labels` to this join. The FILE keeps one name across the pipeline —
        that is what `--set` and the producer registry key on — and the flag is the reader's word for it.
        The premise labels are not handed over: they were a copy of the same genres & moods."""
        bound = {bind(e).name: bind(e) for e in corpus.INPUTS}["genres_moods"]
        self.assertEqual(bound.flag(), "--labels")
        self.assertEqual(bound.artifact, artifacts.GENRES_MOODS)
        self.assertNotIn(artifacts.PREMISE_LABELS, [bind(e).artifact for e in corpus.INPUTS])
        self.assertNotIn(artifacts.VECTOR_LABELS, [bind(e).artifact for e in corpus.INPUTS])


class CommandLine(unittest.TestCase):
    def test_the_scripts_own_parser_accepts_what_the_declaration_builds(self):
        """Run through the parser that will receive it, not against a copy of the argument list."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            parsed = script.build_parser().parse_args(corpus.argv(context(out))[2:])
            # No manifest here records a run start, so the order falls back to the paths, sorted: one
            # out-dir gives one command line. `Supersede` below covers shards the manifests order.
            self.assertEqual(parsed.combined,
                             sorted(os.path.join(out, n) for n in FIXTURE_FILES["combined"]))
            self.assertEqual(parsed.delta,
                             sorted(os.path.join(out, n) for n in FIXTURE_FILES["delta"]))
            self.assertEqual(parsed.labels, os.path.join(out, "genres-moods.json"))
            self.assertIsNone(parsed.withdrawn, "no tombstone file, no flag")
            self.assertEqual(parsed.out, os.path.join(out, f"corpus-{VERSION}.jsonl.gz"))

    def test_every_shard_of_a_set_is_handed_over(self):
        """The eleven-title bug, as a property of the declaration: the stage cannot pass one shard of a
        set, because it does not know the set as a file."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            command = corpus.argv(context(out))
            self.assertEqual(command.count("--combined"), 2)

    def test_the_title_only_delta_generation_is_not_joined(self):
        """`delta-v1` rows were answered with no article text at all: the pass patched out the function
        that builds the state, so Jev saw the title and nothing else, and those answers reach More Like
        This through the corpus. A v1 file left beside the corrected one must not be read."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            stale = os.path.join(out, "delta-v1.jsonl")
            fixture.write(stale, [{"mediaType": "movie", "tmdbId": 1,
                                   "answers": {"critique__craft": {"p": 0.1}}}])
            self.assertNotIn(stale, corpus.argv(context(out)))

    def test_a_shard_set_with_no_members_stops_the_stage(self):
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            for name in FIXTURE_FILES["combined"]:
                os.remove(os.path.join(out, name))
            with self.assertRaises(StageError) as refused:
                corpus.argv(context(out))
            self.assertIn("run_combined.py", str(refused.exception))

    def test_a_missing_required_input_stops_the_stage(self):
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            os.remove(os.path.join(out, FIXTURE_FILES["facts"][0]))
            with self.assertRaises(StageError) as refused:
                corpus.argv(context(out))
            self.assertIn("./den stage facts", str(refused.exception))

    def test_the_expected_count_is_only_passed_when_one_is_named(self):
        """`--expect` is the guard that refuses a short run. Passing it unasked would make every run
        assert a count nobody declared."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            self.assertNotIn("--expect", corpus.argv(context(out)))
            command = corpus.argv(context(out, expect=4))
            self.assertEqual(command[command.index("--expect") + 1], "4")


class BundleProvenance(unittest.TestCase):
    """The bundle audit, which nothing ran until it was wired in here.

    Three files in this repo call `audit_combined.py` the only thing between a corrupted bundle and a
    published dataset, and it was reachable from no stage and no CI step. The join is where it belongs:
    it is what turns the paid Jev shards into the corpus every later stage derives from.

    The check is over the shards' sidecar manifests, not over their rows. What a manifest pins that
    matters here is the pass's own source files — a bundle answered by a version of the pass nobody can
    account for is the corrupted-bundle case, and it is not detectable by looking at the rows.
    """

    def test_the_audited_inputs_are_the_paid_passes(self):
        """The other three inputs are derived locally and carry no manifest, so there is nothing to check
        them against; claiming to audit them would be a check that always passes."""
        self.assertEqual([a.name for a in corpus.AUDITED], ["combined", "delta"])

    def test_a_clean_bundle_needs_no_allowance(self):
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            self.assertEqual(corpus.audit_bundles(context(out)), [])

    def test_a_shard_with_no_manifest_stops_the_join(self):
        """The pass derives the sidecar's name from `--out`, so a shard under a name of its own is split
        from the only record of what produced its rows."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            os.remove(os.path.join(out, FIXTURE_FILES["combined"][0] + ".manifest.json"))
            with self.assertRaises(StageError) as refused:
                corpus.run(context(out, expect=4))
            self.assertIn("has no manifest", str(refused.exception))

    def test_an_unexplained_implementation_change_stops_the_join(self):
        """The edit nobody wrote down. Hashing cannot tell an edit that changes what the rows mean from
        one that does not, so an unrecorded difference is the end of the run."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            write_manifest(os.path.join(out, FIXTURE_FILES["combined"][0]),
                           implementation={"run_combined.py": "0" * 64})
            with self.assertRaises(StageError) as refused:
                corpus.run(context(out, expect=4))
            self.assertIn("run_combined.py", str(refused.exception))
            self.assertIn("implementation-lineage.json", str(refused.exception))

    def test_a_recorded_implementation_change_is_allowed_and_named(self):
        """The shipped case: `combined-v1-r2` was bought on the pass as #20 left it and `delta-v2` on the
        client as it was before #63. Re-stamping those manifests would erase the provenance of a run that
        was paid for once, so the exception is recorded instead — and the stage says which one it ran on.
        """
        recorded = audit_combined.load_lineage()["run_combined.py"][0]
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            write_manifest(os.path.join(out, FIXTURE_FILES["combined"][0]),
                           implementation={"run_combined.py": recorded["sha256"]})
            granted = corpus.audit_bundles(context(out))
            self.assertEqual([entry["shard"] for entry in granted], [FIXTURE_FILES["combined"][0]])
            self.assertEqual(granted[0]["sha256"], recorded["sha256"])
            self.assertTrue(granted[0]["why"])

    def test_the_audit_runs_before_the_join_does(self):
        """A refusal after the corpus is written is a corpus on disk that something downstream will read."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            write_manifest(os.path.join(out, FIXTURE_FILES["delta"][0]),
                           implementation={"typesafe_client.py": "0" * 64})
            with self.assertRaises(StageError):
                corpus.run(context(out, expect=4))
            self.assertFalse(os.path.exists(os.path.join(out, f"corpus-{VERSION}.jsonl.gz")))


class Topology(unittest.TestCase):
    def test_the_corpus_is_owned_by_the_stage_that_outputs_it(self):
        """The registry used to read a `producer` written on the artifact — a field that could name one
        script while the stage ran another. Now the stage that declares the artifact in `OUTPUTS` is the
        answer, and the artifact carries no producer to disagree with."""
        self.assertEqual(artifacts.CORPUS.producer, "")
        self.assertEqual(artifacts.ENTITIES.producer, "")
        registered = pipeline.producers()
        self.assertEqual(registered["corpus"], (corpus.PRODUCER, corpus.HOW, True))
        self.assertEqual(registered["entities"], (corpus.PRODUCER, corpus.HOW, True))

    def test_the_stage_runs_the_script_it_is_registered_as(self):
        """One spelling. A stage subprocessing a file other than the one the registry names is a store
        rebuilt by a rule nothing checked."""
        self.assertEqual(corpus.SCRIPT, os.path.join(REPO, corpus.PRODUCER))
        self.assertTrue(os.path.isfile(corpus.SCRIPT))

    def test_the_genres_and_moods_it_joins_are_the_genres_moods_stages(self):
        self.assertEqual(artifacts.GENRES_MOODS.producer, "")
        self.assertEqual(pipeline.producers()["genres_moods"][0], "pipeline/genres_moods.py")

    def test_the_corpus_is_built_before_the_store_reads_it(self):
        self.assertLess(pipeline.STAGES.index("corpus"), pipeline.STAGES.index("store"))
        self.assertIn(artifacts.CORPUS, [bind(e).artifact for e in store.INPUTS])


class Equivalence(unittest.TestCase):
    def hand_typed(self, out, target):
        """The command as `consolidate_corpus.py`'s own docstring writes it."""
        command = [sys.executable, os.path.join(V2, "consolidate_corpus.py")]
        for name in FIXTURE_FILES["combined"]:
            command += ["--combined", os.path.join(out, name)]
        for name in sorted(FIXTURE_FILES["delta"]):
            command += ["--delta", os.path.join(out, name)]
        command += ["--facts", os.path.join(out, FIXTURE_FILES["facts"][0]),
                    "--labels", os.path.join(out, FIXTURE_FILES["genres_moods"][0]),
                    "--expect", "4", "--out", target]
        done = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_stage_writes_the_bytes_the_hand_typed_command_writes(self):
        """`den stage corpus` against the command in the script's docstring, on one set of inputs.

        Byte-identical is the right bar: the writer sets `mtime=0` precisely so an unchanged corpus is
        an unchanged file, and a publish can skip re-uploading 40 MB. A wrapper that moved one byte would
        be a second writer.
        """
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            reference = os.path.join(out, "reference.jsonl.gz")
            self.hand_typed(out, reference)
            reference_entities = os.path.join(out, "reference-entities.json.gz")
            self.assertTrue(os.path.isfile(reference_entities))

            made = corpus.run(context(out, expect=4))
            self.assertEqual(made, os.path.join(out, f"corpus-{VERSION}.jsonl.gz"))
            self.assertEqual(sha256(made), sha256(reference))
            self.assertEqual(sha256(os.path.join(out, f"corpus-{VERSION}-entities.json.gz")),
                             sha256(reference_entities))

    def test_the_sidecar_lands_where_the_declaration_says_it_will(self):
        """The script derives the sidecar's path from `--out` rather than taking a flag for it, so the
        declaration and the derivation could disagree without anything noticing — and the sidecar would
        be written somewhere no stage and no publish guard looks."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            ctx = context(out, overrides={"corpus": os.path.join(out, "somewhere-else.jsonl.gz")})
            with self.assertRaises(StageError) as refused:
                corpus.run(ctx)
            self.assertIn("entities", str(refused.exception))

    def test_a_join_the_script_refuses_is_a_refusal_here(self):
        """The guards are the reason this file is trusted; a wrapper that swallowed one would be worse
        than no wrapper. `--expect` disagreeing with the row count is the cheapest of them to trip."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            with self.assertRaises(StageError):
                corpus.run(context(out, expect=999))


class Supersede(unittest.TestCase):
    """A re-grounded title's new classify and critique shards, folded in beside the shipped ones (#64).

    The fold-in names the new shards so they match the declared globs; their names say nothing about
    which run is newer, and here they sort BEFORE the shards they supersede.
    """

    MAIN, FALLBACK, NEW = ("2026-09-19T16:13:40+00:00", "2026-09-19T18:02:22+00:00",
                           "2026-09-23T09:00:00+00:00")
    REGROUND = ("combined-v1-r2-reground.jsonl", "delta-v2-reground.jsonl")

    def fold_in(self, out):
        write_inputs(out)
        # delta-v2-rest's stamp is an hour EARLIER than delta-v2's in UTC though it reads later as text.
        for name, started in ((FIXTURE_FILES["combined"][0], self.MAIN),
                              (FIXTURE_FILES["combined"][1], self.FALLBACK),
                              (FIXTURE_FILES["delta"][0], self.MAIN),
                              (FIXTURE_FILES["delta"][1], "2026-09-19T16:13:40+01:00")):
            write_manifest(os.path.join(out, name), started=started)
        combined_path, delta_path = (os.path.join(out, n) for n in self.REGROUND)
        fixture.write(combined_path, [fixture.combined(1, answers={"tone": {"choice": "hopeful"}})])
        fixture.write(delta_path, [{"mediaType": "movie", "tmdbId": 1,
                                    "answers": {"critique__craft": {"p": 0.95}}}])
        for path in (combined_path, delta_path):
            write_manifest(path, started=self.NEW)
        with open(os.path.join(out, "keys.txt"), "w", encoding="utf-8") as fh:
            fh.write("movie:2\n")
        with contextlib.redirect_stdout(io.StringIO()):
            script.withdraw(["--keys", os.path.join(out, "keys.txt"), "--reason", "#64 redirect",
                             "--out", os.path.join(out, "withdrawn.jsonl")],
                            now=datetime.fromisoformat("2026-09-22T20:00:00+00:00"))

    def test_the_stage_passes_the_shards_oldest_run_first(self):
        with tempfile.TemporaryDirectory() as out:
            self.fold_in(out)
            parsed = script.build_parser().parse_args(corpus.argv(context(out))[2:])
            self.assertEqual([os.path.basename(p) for p in parsed.combined],
                             ["combined-v1-r2.jsonl", "combined-v1-r2-token-fallback.jsonl",
                              "combined-v1-r2-reground.jsonl"])
            self.assertEqual([os.path.basename(p) for p in parsed.delta],
                             ["delta-v2-rest.jsonl", "delta-v2.jsonl", "delta-v2-reground.jsonl"],
                             "ordered as instants, not as strings")
            self.assertEqual(parsed.withdrawn, os.path.join(out, "withdrawn.jsonl"))

    def test_the_folded_in_shards_pass_the_bundle_audit(self):
        with tempfile.TemporaryDirectory() as out:
            self.fold_in(out)
            self.assertEqual(corpus.audit_bundles(context(out)), [])

    def test_the_stage_writes_the_bytes_the_hand_typed_command_writes_with_shards_in_name_order(self):
        """The join orders the shards by their manifests, so a hand-typed command that lists them in
        name order — the reverse of the run order — writes the same corpus as the stage."""
        with tempfile.TemporaryDirectory() as out:
            self.fold_in(out)
            reference = os.path.join(out, "reference.jsonl.gz")
            command = [sys.executable, os.path.join(V2, "consolidate_corpus.py")]
            for name in sorted(FIXTURE_FILES["combined"] + self.REGROUND[:1]):
                command += ["--combined", os.path.join(out, name)]
            for name in sorted(FIXTURE_FILES["delta"] + self.REGROUND[1:]):
                command += ["--delta", os.path.join(out, name)]
            command += ["--facts", os.path.join(out, FIXTURE_FILES["facts"][0]),
                        "--labels", os.path.join(out, FIXTURE_FILES["genres_moods"][0]),
                        "--withdrawn", os.path.join(out, "withdrawn.jsonl"),
                        "--expect", "4", "--out", reference]
            self.assertNotEqual(command, corpus.argv(context(out, expect=4))[:-2] + ["--out", reference])
            done = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            made = corpus.run(context(out, expect=4))
            self.assertEqual(sha256(made), sha256(reference))
            rows = {r["key"]: r for r in fixture.read(made)}
            self.assertEqual(rows["movie:1"]["facets"]["tone"]["choice"], "hopeful")
            self.assertEqual(rows["movie:1"]["critique"]["craft"], {"p": 0.95})
            self.assertEqual(rows["movie:2"]["facets"], {}, "withdrawn")
            report = json.loads(done.stdout)
            self.assertEqual([(s["shard"], s["supersedes"]) for s in report["combined"]["shards"]],
                             [("combined-v1-r2.jsonl", 0), ("combined-v1-r2-token-fallback.jsonl", 0),
                              ("combined-v1-r2-reground.jsonl", 1)])
            self.assertEqual(report["combined"]["withdrawn"], 1)


class Rows(unittest.TestCase):
    def test_the_stage_carries_the_joins_through_untouched(self):
        """Not a second test of the join — a check that the wrapper hands over every artifact, so the
        row it produces holds facts, genres & moods and the delta pass at once. A stage that dropped a
        flag would still write a corpus, and it would be quietly thinner."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            rows = {r["key"]: r for r in fixture.read(corpus.run(context(out, expect=4)))}
            self.assertEqual(sorted(rows), ["movie:1", "movie:2", "movie:77", "tv:9"])
            self.assertEqual(rows["movie:1"]["labels"]["primaryGenre"], "Crime")
            self.assertEqual(rows["movie:1"]["critique"]["craft"], {"p": 0.7})
            self.assertEqual(rows["movie:77"]["facts"]["countries"], ["US"], "the facts-only title")
            with open(os.path.join(out, f"corpus-{VERSION}-entities.json.gz"), "rb") as fh:
                import gzip
                self.assertEqual(json.loads(gzip.decompress(fh.read()))["Q42"]["en"], "Ada Director")


if __name__ == "__main__":
    unittest.main()
