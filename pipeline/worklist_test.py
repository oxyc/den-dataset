#!/usr/bin/env python3
"""The worklist stage — that it is a wrapper, and that what it is responsible for still holds.

Which ids exist and in what order is `taxonomy-backfill worklist`'s question, tested where it lives. What
is tested here is the stage around it:

  * **the mode is chosen, never inherited.** The three modes are three different catalogues, and the one
    the command picks unasked is the pilot's 500 titles. Enrichment is billed per title, so a run that did
    not say which universe it wanted is refused;
  * **each mode is handed what it reads, and a delta is handed the published labels.** Without `--known` a
    delta re-enriches the whole catalogue — the one cost the pass exists to avoid — and reports an
    ordinary-looking count while doing it;
  * **a short universe is read back, not inferred from the exit code.** The command exits 0 on a dump it
    could not parse, and `enrich` reads an empty worklist as a finished run.

The command is a Swift binary, so the run tests drive a stub that honours the same flags and parses an
export dump the way `Worklist.parse` does: what is under test is the argument list and what the stage does
with the result. Byte equivalence against the real binary over a real TMDB daily dump was measured
separately (oxyc/den-dataset#27) and cannot run in CI, which has neither the toolchain nor the 28 MB
download.
"""
import json
import os
import stat
import tempfile
import unittest

import pipeline

from . import artifacts, worklist
from .contract import Context, StageError, bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "testver"

#: A stand-in for `taxonomy-backfill worklist`. It parses an export dump the way the real one does — every
#: line it cannot read as an id is dropped, silently — so the stage's completeness check is exercised
#: against the behaviour it exists for rather than against a number a stub was told to print.
STUB = '''#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
def flag(name, fallback=None):
    return argv[argv.index(name) + 1] if name in argv else fallback

out = flag("--out")
media = flag("--media")
with open(os.path.join(os.path.dirname(out), "stub-argv.jsonl"), "a") as fh:
    fh.write(json.dumps(argv) + "\\n")

if os.environ.get("DEN_STUB_EXIT"):
    sys.exit(int(os.environ["DEN_STUB_EXIT"]))

entries = []
if flag("--mode") == "export":
    with open(flag("--file")) as fh:
        for line in fh:
            try:
                entries.append({"tmdbId": json.loads(line)["id"], "mediaType": media})
            except Exception:
                continue
else:
    entries = [{"tmdbId": 1, "mediaType": media}]
if os.environ.get("DEN_STUB_EMPTY"):
    entries = []
with open(out, "w") as fh:
    json.dump(entries, fh)
print(f"worklist: {len(entries)} {media} ids -> {out}")
'''

#: Two real-shaped dump lines per media. The export's rows carry more than `id` — the reader takes that one
#: field — so the fixture keeps a second key rather than the minimum that would parse.
DUMPS = {
    "movie_ids.json": '{"id":11,"original_title":"Star Wars","popularity":41.2}\n'
                      '{"id":12,"original_title":"Finding Nemo","popularity":33.9}\n',
    "tv_series_ids.json": '{"id":1399,"original_name":"Game of Thrones","popularity":92.1}\n',
}


def write_stub(directory):
    """The stub, executable, and the environment that points the stage at it."""
    path = os.path.join(directory, "taxonomy-backfill-stub")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(STUB)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    os.environ["DEN_BACKFILL_BIN"] = path
    return path


def write_inputs(out):
    """Every declared input, under its declared filename."""
    for name, text in DUMPS.items():
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    with open(os.path.join(out, "labels-t02.json"), "w", encoding="utf-8") as fh:
        fh.write('{"records": []}')


def context(out, **kwargs):
    return Context(out_dir=out, dataset_version=VERSION, **kwargs)


def invocations(out):
    with open(os.path.join(out, "stub-argv.jsonl"), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines() if line]


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
        for name in ("DEN_STUB_EXIT", "DEN_STUB_EMPTY"):
            os.environ.pop(name, None)
        if self.previous is None:
            os.environ.pop("DEN_BACKFILL_BIN", None)
        else:
            os.environ["DEN_BACKFILL_BIN"] = self.previous
        self.directory.cleanup()


class Declaration(Staged):
    def test_every_flag_it_sends_is_a_flag_the_command_reads(self):
        """`Args` keeps a map of whatever begins with `--` and answers None for everything else, so an
        unknown flag is not an error — it is silently absent. A misspelled `--known` builds a delta that
        re-enriches the published catalogue and says nothing. This is the parser check the other stages get
        from running the real argument list through the script's own parser."""
        source = swift_source()
        for mode in worklist.MODES:
            for media in worklist.MEDIA:
                command = worklist.argv(context(self.out, mode=mode, since="2026-09-01"), media)
                for flag in [a for a in command if a.startswith("--")]:
                    self.assertIn(f'"{flag}"', source, f"{flag} is not a flag worklist reads in {mode}")

    def test_the_modes_it_offers_are_the_modes_the_command_switches_on(self):
        """A mode this stage names and the command does not falls through to `discover`, which builds a
        different universe under the name that was asked for."""
        source = swift_source()
        self.assertEqual(worklist.MODES, ("discover", "export", "delta"))
        for mode in ("delta", "export"):
            self.assertIn(f'case "{mode}":', source)
        # `discover` is the switch's default rather than a case of its own — which is also why this stage
        # refuses to let it be the default here.
        self.assertIn('args["--mode"] ?? "discover"', source)

    def test_the_vote_floor_it_pins_is_the_one_the_command_and_the_daily_pass_use(self):
        """Pinned rather than inherited: the universe this stage builds should not move because a default
        somewhere else moved."""
        self.assertEqual(worklist.VOTE_FLOOR, 50)
        self.assertIn('args.int("--vote-floor") ?? 50', swift_source())
        with open(os.path.join(REPO, "scripts", "delta-run.sh"), encoding="utf-8") as fh:
            self.assertIn('VOTE_FLOOR:-50', fh.read())

    def test_the_labels_artifact_keeps_one_name_and_reaches_the_command_under_its_own(self):
        """`labels-t02.json` is `--known` here, `--labels` to the corpus join and `--vector-labels` to the
        store writer — one file, one pipeline-wide name, and the flag is the reader's word for it."""
        bound = {bind(e).name: bind(e) for e in worklist.INPUTS}["vector_labels"]
        self.assertEqual(bound.flag(), "--known")
        self.assertEqual(bound.artifact, artifacts.VECTOR_LABELS)

    def test_it_declares_a_worklist_per_media(self):
        """`enrich` refuses a list that mixes them — a film and a series can share a tmdbId — so one
        combined output would be an artifact nothing downstream can read."""
        self.assertEqual([bind(e).name for e in worklist.OUTPUTS], ["universe_movie", "universe_tv"])
        self.assertEqual(sorted(worklist.MEDIA), ["movie", "tv"])


class CommandLine(Staged):
    def test_a_run_that_did_not_say_which_universe_it_wants_is_refused(self):
        with self.assertRaises(StageError) as refused:
            worklist.argv(context(self.out), "movie")
        for mode in worklist.MODES:
            self.assertIn(mode, str(refused.exception))

    def test_a_mode_the_command_does_not_have_is_refused_rather_than_passed_on(self):
        """The command switches on the string and falls through to `discover`, so an unknown mode there is
        not an error — it is the pilot's universe under another name."""
        with self.assertRaises(StageError) as refused:
            worklist.argv(context(self.out, mode="daily"), "movie")
        self.assertIn("--mode", str(refused.exception))

    def test_export_is_handed_that_medias_dump_and_nothing_else(self):
        for media, filename in (("movie", "movie_ids.json"), ("tv", "tv_series_ids.json")):
            command = worklist.argv(context(self.out, mode="export"), media)
            self.assertEqual(command[command.index("--file") + 1], os.path.join(self.out, filename))
            self.assertEqual(command[command.index("--media") + 1], media)
            self.assertEqual(command[command.index("--out") + 1],
                             os.path.join(self.out, f"universe-{media}.json"))
            # The dump carries no vote counts — the floor is enrich's question there, not this one's.
            self.assertNotIn("--vote-floor", command)
            self.assertNotIn("--since", command)

    def test_a_missing_dump_is_refused_with_what_fetches_it(self):
        os.remove(os.path.join(self.out, "movie_ids.json"))
        with self.assertRaises(StageError) as refused:
            worklist.argv(context(self.out, mode="export"), "movie")
        self.assertIn("build-worklist.py", str(refused.exception))
        self.assertIn("gunzip", str(refused.exception))

    def test_a_delta_carries_its_window_and_the_labels_it_must_skip(self):
        command = worklist.argv(context(self.out, mode="delta", since="2026-09-07"), "movie")
        self.assertEqual(command[command.index("--since") + 1], "2026-09-07")
        self.assertEqual(command[command.index("--known") + 1], os.path.join(self.out, "labels-t02.json"))
        self.assertEqual(command[command.index("--vote-floor") + 1], str(worklist.VOTE_FLOOR))

    def test_a_delta_asks_for_what_the_daily_pass_asks_for(self):
        """The one mode with a live caller. `scripts/delta-run.sh` runs this command every day, so its
        invocation is the oracle for what a delta needs — anything it passes that this stage does not is a
        narrower universe under the same name."""
        with open(os.path.join(REPO, "scripts", "delta-run.sh"), encoding="utf-8") as fh:
            tail = fh.read().split('"$BIN" worklist')[1]
        invocation = []
        for line in tail.split("\n"):
            invocation.append(line)
            if not line.rstrip().endswith("\\"):
                break
        wanted = {word for word in " ".join(invocation).split() if word.startswith("--")}
        command = worklist.argv(context(self.out, mode="delta", since="2026-09-07"), "movie")
        self.assertEqual(wanted - set(command), set())

    def test_a_delta_with_no_window_is_refused(self):
        with self.assertRaises(StageError) as refused:
            worklist.argv(context(self.out, mode="delta"), "movie")
        self.assertIn("--since", str(refused.exception))

    def test_a_delta_with_no_published_labels_is_refused(self):
        """Without them the pass re-enriches every title already shipped, at the per-title price the vote
        floor exists to bound, and nothing in its output says that is what happened."""
        os.remove(os.path.join(self.out, "labels-t02.json"))
        with self.assertRaises(StageError) as refused:
            worklist.argv(context(self.out, mode="delta", since="2026-09-07"), "movie")
        self.assertIn("taxonomy-backfill finalize", str(refused.exception))

    def test_discover_takes_the_floor_and_no_files(self):
        command = worklist.argv(context(self.out, mode="discover"), "tv")
        self.assertEqual(command[command.index("--vote-floor") + 1], str(worklist.VOTE_FLOOR))
        self.assertNotIn("--file", command)
        self.assertNotIn("--known", command)

    def test_a_missing_binary_is_refused_with_the_build_that_makes_it(self):
        os.environ["DEN_BACKFILL_BIN"] = os.path.join(self.out, "not-built")
        with self.assertRaises(StageError) as refused:
            worklist.argv(context(self.out, mode="export"), "movie")
        self.assertIn("swift build -c release", str(refused.exception))


class Run(Staged):
    def test_it_builds_one_worklist_per_media_from_that_medias_dump(self):
        made = worklist.run(context(self.out, mode="export"))
        for media, expected in (("movie", [11, 12]), ("tv", [1399])):
            path = os.path.join(self.out, f"universe-{media}.json")
            self.assertIn(path, made)
            with open(path, encoding="utf-8") as fh:
                entries = json.load(fh)
            self.assertEqual([e["tmdbId"] for e in entries], expected)
            self.assertEqual({e["mediaType"] for e in entries}, {media})
        self.assertEqual([command[command.index("--media") + 1] for command in invocations(self.out)],
                         ["movie", "tv"])

    def test_a_failed_run_is_a_refusal(self):
        os.environ["DEN_STUB_EXIT"] = "1"
        with self.assertRaises(StageError) as refused:
            worklist.run(context(self.out, mode="export"))
        self.assertIn("worklist", str(refused.exception))

    def test_a_run_that_built_no_universe_is_refused(self):
        """`[]` and exit 0 is what a still-gzipped dump, a truncated one or a saved error page produces,
        and `enrich` drains it as a finished run rather than as a failure."""
        os.environ["DEN_STUB_EMPTY"] = "1"
        with self.assertRaises(StageError) as refused:
            worklist.run(context(self.out, mode="discover"))
        self.assertIn("empty", str(refused.exception))

    def test_a_delta_that_found_nothing_is_not_a_failure(self):
        """The answer on a quiet day. Refusing it would fail the daily pass for doing its job — which is
        the reason `scripts/delta-run.sh` counts titles that survived enrichment rather than worklist
        rows."""
        os.environ["DEN_STUB_EMPTY"] = "1"
        made = worklist.run(context(self.out, mode="delta", since="2026-09-07"))
        self.assertIn("universe-movie.json", made)

    def test_this_stage_does_not_write_the_shipped_catalogues_filename(self):
        """`scripts/build-worklist.py` owns `worklist-<media>.json` — the ids Den already ships, ordered by
        popularity. This command enumerates all of TMDB: 1,246,659 movie ids against a corpus of 47,618.

        They used to share a filename, so whichever ran last decided which universe the next enrich billed
        for, and nothing downstream could tell them apart. Pinned as a rule rather than left to whoever
        edits the artifact next.
        """
        for artifact in worklist.OUTPUTS:
            self.assertNotIn("worklist", artifact.filename, "the shipped catalogue's name")
            self.assertIn("universe", artifact.filename)

    def test_a_dump_the_parse_could_not_finish_is_refused(self):
        """The failure the exit code cannot show: `Worklist.parse` drops every line it cannot decode, so a
        dump that arrived half-written becomes half a catalogue and nothing downstream can tell."""
        with open(os.path.join(self.out, "movie_ids.json"), "a", encoding="utf-8") as fh:
            fh.write('{"id":13,"original_ti\n')
        with self.assertRaises(StageError) as refused:
            worklist.run(context(self.out, mode="export"))
        self.assertIn("3 lines", str(refused.exception))
        self.assertIn("2 ids", str(refused.exception))


class Topology(unittest.TestCase):
    def test_the_worklists_are_owned_by_the_stage_that_writes_them(self):
        for name in ("universe_movie", "universe_tv"):
            self.assertEqual(getattr(artifacts, name.upper()).producer, "")
            self.assertEqual(pipeline.producers()[name], (worklist.PRODUCER, worklist.HOW, False))

    def test_the_producer_it_names_is_a_file_in_the_tree(self):
        """One spelling. A stage registered against a rule that is not there is an artifact nothing can
        rebuild — and `check-producers.py` refuses a publish on exactly that."""
        self.assertEqual(worklist.PRODUCER, artifacts.BACKFILL)
        self.assertTrue(os.path.isfile(os.path.join(REPO, worklist.PRODUCER)))

    def test_the_universe_is_built_before_anything_is_drawn_from_it(self):
        self.assertEqual(pipeline.STAGES[0], "worklist")

    def test_the_dumps_answer_for_themselves_until_something_here_fetches_them(self):
        """The seam: TMDB's daily export comes from outside this repo, and the only thing that pulls it
        leaves it gzipped — so the `how` an operator is sent to includes the step that fetch does not do."""
        for artifact in (artifacts.EXPORT_MOVIE, artifacts.EXPORT_TV):
            producer, how, _ = pipeline.producers()[artifact.name]
            self.assertEqual(producer, "scripts/build-worklist.py")
            self.assertIn("gunzip", how)
            self.assertTrue(os.path.isfile(os.path.join(REPO, producer)))


if __name__ == "__main__":
    unittest.main()
