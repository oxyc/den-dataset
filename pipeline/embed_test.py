#!/usr/bin/env python3
"""The embed stage — that it is a wrapper, and that the three things it is responsible for still hold.

The composition, the resume and the canary are `embed-corpus`'s, tested where they live; what is tested
here is the stage around them:

  * **the composition is pinned, and the pin is checked in both directions.** `embed-corpus` parses its
    arguments with a reader that ignores what it does not recognise, so a misspelled flag is not an error
    — it is a different vector space. So the flags this stage emits are held against the literals the
    command reads, and the shape it pins is held against the Swift's own record of what the shipped store
    was composed as. Neither can drift without this going red;
  * **resume survives the wrapper.** A run appends to stores it found; a wrapper that cleared the out-dir,
    or pointed the second run somewhere else, would turn a 12-hour interruptible job into one that has to
    finish in one go. The test is that two runs against one out-dir leave two rows, not one;
  * **the records land where the declaration says.** Everything the command writes is derived from
    `--out-dir`, so an override that names one of them elsewhere splits a store from its identity.

The command itself is a Swift binary, so the run tests drive a stub that honours the same flags: what is
under test is the argument list and what the stage does with the result, and a stub is the only way to
exercise that without a toolchain and a live embedder. Byte equivalence against the real command through
the real den-embed was measured separately (oxyc/den-dataset#27) and cannot run in CI, which has neither.
"""
import json
import os
import stat
import tempfile
import unittest

import pipeline

from . import artifacts, corpus, embed, fetch, store
from .contract import Context, StageError, bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "testver"

#: A stand-in for `taxonomy-backfill embed-corpus` that honours the flags the real one does: it appends a
#: row to each append-only store, and records the composition ONLY on a fresh out-dir — which is where the
#: real command's own mismatch guard has nothing to compare against, and where a dropped flag therefore
#: becomes the recorded truth.
STUB = '''#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
out = argv[argv.index("--out-dir") + 1]
index = os.path.join(out, "index")
os.makedirs(index, exist_ok=True)
with open(os.path.join(out, "stub-argv.jsonl"), "a") as fh:
    fh.write(json.dumps(argv) + "\\n")

if os.environ.get("DEN_STUB_EXIT"):
    sys.exit(int(os.environ["DEN_STUB_EXIT"]))

composition = os.path.join(index, "composition.json")
if not os.path.exists(composition):
    with open(composition, "w") as fh:
        json.dump({"docShape": "lean" if "--doc-facts" in argv else "full",
                   # The variant: a stub that never sees the flag, standing in for a command that was
                   # handed a misspelling of it.
                   "dropDirector": "--doc-drop-director" in argv and not os.environ.get("DEN_STUB_IGNORE_DROP"),
                   "plotCap": int(argv[argv.index("--plot-cap") + 1])}, fh)
for name, row in (("labels.jsonl", {"mediaType": "movie"}), ("vectors.jsonl", {"v": [1]})):
    with open(os.path.join(index, name), "a") as fh:
        fh.write(json.dumps(row) + "\\n")
records = ["embedder.json"]
# The variant: a command built before the canary gate landed, which embeds and records no space.
if not os.environ.get("DEN_STUB_NO_SPACE"):
    records.append("embedding-space.json")
for name in records:
    with open(os.path.join(index, name), "w") as fh:
        json.dump({"canary": os.environ.get("DEN_EMBED_CANARY", "")}, fh)
'''


def write_stub(directory):
    """The stub, executable, and the environment that points the stage at it."""
    path = os.path.join(directory, "taxonomy-backfill-stub")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(STUB)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    os.environ["DEN_BACKFILL_BIN"] = path
    return path


def write_inputs(out):
    """Every declared input, under its declared filename. Contents are the command's business, not this
    stage's — what is under test is which files it is handed."""
    os.makedirs(os.path.join(out, "enriched"), exist_ok=True)
    for name in ("labels-t02.json", "doc-facts.json"):
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            fh.write("{}")


def context(out, **kwargs):
    return Context(out_dir=out, dataset_version=VERSION, **kwargs)


def lines(path):
    with open(path, encoding="utf-8") as fh:
        return [line for line in fh.read().splitlines() if line]


def swift_source():
    with open(os.path.join(REPO, "Sources", "taxonomy-backfill", "main.swift"), encoding="utf-8") as fh:
        return fh.read()


class Staged(unittest.TestCase):
    """A stub binary for the duration, restored afterwards so one test cannot leak into the next."""

    def setUp(self):
        self.previous = os.environ.get("DEN_BACKFILL_BIN")
        self.directory = tempfile.TemporaryDirectory()
        self.out = self.directory.name
        write_stub(self.out)
        write_inputs(self.out)

    def tearDown(self):
        for name in ("DEN_STUB_EXIT", "DEN_STUB_IGNORE_DROP", "DEN_STUB_NO_SPACE"):
            os.environ.pop(name, None)
        if self.previous is None:
            os.environ.pop("DEN_BACKFILL_BIN", None)
        else:
            os.environ["DEN_BACKFILL_BIN"] = self.previous
        self.directory.cleanup()


class Declaration(Staged):
    def test_every_flag_it_sends_is_a_flag_the_command_reads(self):
        """The command declares its flags per subcommand and refuses one it does not declare, so a
        misspelling here is now a refusal rather than a different document embedded happily. This still
        checks the stage's side of that: a flag the stage sends and the command dropped would fail the run
        at the box rather than here. It is the parser check the other stages get from running the real
        argument list through the script's own parser."""
        source = swift_source()
        for flag in [a for a in embed.argv(context(self.out, pause_ms=10, limit=5)) if a.startswith("--")]:
            self.assertIn(f'"{flag}"', source, f"{flag} is not a flag embed-corpus reads")

    def test_the_composition_it_pins_is_the_one_the_shipped_store_records(self):
        """The values were recovered by re-embedding probe titles against the shipped rows, and the command
        carries them in the message it prints for a store with no record. Held against that rather than
        against a second copy of the numbers."""
        source = swift_source()
        self.assertEqual(embed.SHIPPED_COMPOSITION,
                         {"docShape": "lean", "dropDirector": True, "plotCap": 3500})
        self.assertIn('"docShape":"lean"', source)
        self.assertIn('"dropDirector":true,"plotCap":3500', source)

    def test_the_chunk_fits_the_services_request_budget(self):
        """den-embed accepts a request while `sum(min(tokens, max_tokens))` is within the budget. The
        command's own default of 15 does not fit at the cap the box serves — every request is a 413, and
        the transport treats that as definitive, so the run dies on its first flush having written
        nothing."""
        self.assertLessEqual(embed.CHUNK * embed.MAX_TOKENS, embed.TOKEN_BUDGET)
        self.assertGreater(15 * embed.MAX_TOKENS, embed.TOKEN_BUDGET)

    def test_the_labels_artifact_keeps_one_name_and_reaches_the_command_under_its_own(self):
        """`labels-t02.json` is `--labels` here and `--vector-labels` to the store writer — one file, one
        pipeline-wide name, and the flag is the reader's word for it."""
        bound = {bind(e).name: bind(e) for e in embed.INPUTS}["vector_labels"]
        self.assertEqual(bound.flag(), "--labels")
        self.assertEqual(bound.artifact, artifacts.VECTOR_LABELS)
        self.assertIn(artifacts.VECTOR_LABELS, [bind(e).artifact for e in store.INPUTS])

    def test_it_declares_the_stores_and_the_records_that_name_their_space(self):
        """A stage that declared only the two stores would leave the composition, the embedder identity and
        the verified space owned by nobody — and those are what say which vector space the rows are in."""
        self.assertEqual([bind(e).name for e in embed.OUTPUTS],
                         ["embed_labels", "embed_vectors", "composition", "embedder", "embedding_space"])


class CommandLine(Staged):
    def test_the_composition_is_on_every_command_line(self):
        command = embed.argv(context(self.out))
        self.assertIn("--doc-drop-director", command)
        self.assertEqual(command[command.index("--plot-cap") + 1], "3500")
        self.assertEqual(command[command.index("--doc-facts") + 1],
                         os.path.join(self.out, "doc-facts.json"))
        self.assertEqual(command[command.index("--out-dir") + 1], self.out)
        self.assertEqual(command[command.index("--chunk") + 1], str(embed.CHUNK))

    def test_a_missing_doc_facts_stops_the_stage(self):
        """Absent, the command composes the FULL document shape instead — title, year and cast — which
        nothing ships. So it is not an optional input that degrades the run; it silently changes what the
        vectors mean."""
        os.remove(os.path.join(self.out, "doc-facts.json"))
        with self.assertRaises(StageError) as refused:
            embed.argv(context(self.out))
        self.assertIn("taxonomy-backfill doc-facts", str(refused.exception))

    def test_a_missing_enrichment_stops_the_stage(self):
        os.rmdir(os.path.join(self.out, "enriched"))
        with self.assertRaises(StageError) as refused:
            embed.argv(context(self.out))
        self.assertIn(fetch.HOW, str(refused.exception))

    def test_the_pause_and_the_limit_are_only_passed_when_they_are_named(self):
        """Neither has a safe default to pass unasked: a pause nobody chose costs hours over a corpus, and
        a limit nobody chose stops the run early and reports it as finished."""
        command = embed.argv(context(self.out))
        self.assertNotIn("--pause-ms", command)
        self.assertNotIn("--limit", command)
        command = embed.argv(context(self.out, pause_ms=250, limit=5000))
        self.assertEqual(command[command.index("--pause-ms") + 1], "250")
        self.assertEqual(command[command.index("--limit") + 1], "5000")

    def test_a_missing_binary_is_refused_with_the_build_that_makes_it(self):
        os.environ["DEN_BACKFILL_BIN"] = os.path.join(self.out, "not-built")
        with self.assertRaises(StageError) as refused:
            embed.argv(context(self.out))
        self.assertIn("swift build -c release", str(refused.exception))


class Run(Staged):
    def test_it_names_the_canary_so_the_gate_runs_wherever_the_stage_was_invoked(self):
        """The command resolves `data/embed-canary.json` against the working directory and fails closed
        when it is not there. Failing closed is right and being unable to find the file is not the failure
        anyone wants, so the stage names it — this checkout's answers, not whatever is beside the cwd."""
        embed.run(context(self.out))
        with open(os.path.join(self.out, "index", "embedder.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["canary"], os.path.join(REPO, "data", "embed-canary.json"))

    def test_a_failed_run_is_a_refusal(self):
        os.environ["DEN_STUB_EXIT"] = "1"
        with self.assertRaises(StageError) as refused:
            embed.run(context(self.out))
        self.assertIn("embed-corpus", str(refused.exception))

    def test_a_second_run_appends_to_the_stores_the_first_one_left(self):
        """Resume, as a property of the wrapper. The command skips what is already in the stores and
        reconciles a torn pair before appending, and all of that depends on finding them where it left
        them — so a stage that cleared the out-dir, or pointed the second run elsewhere, would turn an
        interruptible 12-hour job into one that has to finish in a single go."""
        embed.run(context(self.out))
        embed.run(context(self.out))
        for name in ("labels.jsonl", "vectors.jsonl"):
            self.assertEqual(len(lines(os.path.join(self.out, "index", name))), 2, name)
        invocations = [json.loads(line) for line in lines(os.path.join(self.out, "stub-argv.jsonl"))]
        self.assertEqual(len(invocations), 2)
        self.assertEqual(invocations[0], invocations[1], "the second run was handed a different command")

    def test_a_run_that_recorded_another_composition_is_refused(self):
        """The case the command cannot catch: on a fresh out-dir there is no previous record to disagree
        with, so a dropped flag becomes the recorded truth."""
        os.environ["DEN_STUB_IGNORE_DROP"] = "1"
        with self.assertRaises(StageError) as refused:
            embed.run(context(self.out))
        self.assertIn("dropDirector", str(refused.exception))

    def test_a_run_that_recorded_no_embedding_space_is_refused(self):
        """The canary keeps gating this path from the outside too. A binary built before the gate landed
        embeds the whole corpus and records no space, and it cannot complain about that itself — the code
        that would is the code that is missing. So a run with no verdict stops here."""
        os.environ["DEN_STUB_NO_SPACE"] = "1"
        with self.assertRaises(StageError) as refused:
            embed.run(context(self.out))
        self.assertIn("canary", str(refused.exception))

    def test_an_output_pointed_somewhere_the_command_cannot_write_is_refused(self):
        """Everything the command writes is derived from `--out-dir`. An override that names one of them
        elsewhere does not move it — it splits a store from the records that say which space it is in."""
        elsewhere = {"embed_vectors": os.path.join(self.out, "elsewhere", "vectors.jsonl")}
        with self.assertRaises(StageError) as refused:
            embed.run(context(self.out, overrides=elsewhere))
        self.assertIn("--out-dir", str(refused.exception))


class Topology(unittest.TestCase):
    def test_the_stores_are_owned_by_the_stage_that_writes_them(self):
        for name in ("embed_labels", "embed_vectors", "composition", "embedder", "embedding_space"):
            self.assertEqual(getattr(artifacts, name.upper()).producer, "")
            self.assertEqual(pipeline.producers()[name], (embed.PRODUCER, embed.HOW, False))

    def test_the_producer_it_names_is_a_file_in_the_tree(self):
        """One spelling. A stage registered against a rule that is not there is an artifact nothing can
        rebuild — and `check-producers.py` refuses a publish on exactly that."""
        self.assertEqual(embed.PRODUCER, artifacts.BACKFILL)
        self.assertTrue(os.path.isfile(os.path.join(REPO, embed.PRODUCER)))

    def test_the_vectors_are_embedded_before_the_corpus_is_joined_and_the_store_built(self):
        self.assertLess(pipeline.STAGES.index("embed"), pipeline.STAGES.index("corpus"))
        self.assertLess(pipeline.STAGES.index("corpus"), pipeline.STAGES.index("store"))

    def test_an_input_no_stage_produces_still_names_its_own_producer(self):
        """The seam: the Wikidata scrape is a stage that is not ported, so it answers for itself until it
        lands. The enrichment no longer does — the fetch stage writes it, so the registry names that stage's
        rule, which is what an operator handed a missing `out/enriched` is sent to run."""
        self.assertEqual(pipeline.producers()["doc_facts"],
                         (artifacts.DOC_FACTS.producer, artifacts.DOC_FACTS.how, False))
        self.assertEqual(artifacts.ENRICHED.producer, "")
        self.assertEqual(pipeline.producers()["enriched"], (fetch.PRODUCER, fetch.HOW, False))
        self.assertIn(artifacts.VECTOR_LABELS, [bind(e).artifact for e in corpus.INPUTS])


if __name__ == "__main__":
    unittest.main()
