#!/usr/bin/env python3
"""The fetch stage — that it is a wrapper, that the loop it took over still stops for the right reasons,
and that the command line it builds is one the command accepts.

The enrichment itself is tested where it lives, in the Swift target: the checkpoint, the per-media
Wikidata lookup, the batch-clobber guard. None of it is reimplemented here. What is tested here is the
stage around it, which had to answer two questions the other wrapper stages did not:

  * **the loop.** `scripts/enrich-all.sh` ran the batches and decided when to stop, and that decision is
    now the stage's. So each of its three stopping rules is exercised — the retried abort, the stall with
    ids still being attempted, and the batch where every title is below the vote floor and no amount of
    waiting will help. Against a stub, because what is under test is the loop, and a real drain is ~120
    batches of TMDB quota.
  * **an equivalence that cannot be byte comparison over the artifact.** This pass fetches from TMDB and
    from live Wikipedia; two runs of one command do not agree, and a title's article can change between
    them. What CAN be held to the documented command is the argument list, and it is held to it against
    the real binary rather than against a copy of the flag names: `enrich` now declares its flags and
    refuses an unknown one, a value flag with no value and a stray bare word, so a run that reaches the
    checkpoint is proof the arguments are the ones the command declares.

    `Oracle` runs that real binary on a DRAINED out-dir — every worklist id already in the checkpoint.
    `enrich` returns on `pending.isEmpty` before it builds a TMDB client, so the whole documented path
    runs, the argument list is parsed by the parser that will see it in production, and no TMDB call is
    made and nothing is written. What that cannot prove is the fetching half: a partially-drained run
    reaches the network by construction, so the batch a real resume would write is out of reach here.
    Byte comparison is unavailable even for the report the drained run prints — it serialises a Swift
    `Dictionary`, so its key order changes from process to process — which is why that one is compared as
    a parsed report and why the loop reads it by key.
"""
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest

import pipeline

from . import artifacts, embed, fetch, worklist
from .contract import Context, StageError, bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILT = os.path.join(REPO, embed.BUILT)
VERSION = "testver"

#: One id per worklist, and the checkpoint that already holds it. `enrich` keys the checkpoint by media
#: because TMDB's movie and series id spaces overlap, so "the drained state" is that pair of spellings.
WORKLIST = {"movie": [{"tmdbId": 550, "mediaType": "movie"}],
            "tv": [{"tmdbId": 1396, "mediaType": "tv"}]}

#: Where each media's universe is written, taken from the declaration rather than spelled again here. The
#: file the stage hands to `--worklist` is whatever `pipeline/worklist.py` wrote; a fixture carrying its
#: own copy of that filename would go on passing against a file the stage no longer reads.
UNIVERSE = {"movie": artifacts.UNIVERSE_MOVIE, "tv": artifacts.UNIVERSE_TV}
DRAINED = {"processed": ["movie:550", "tv:1396"], "nextBatch": 3}

#: A stand-in for `taxonomy-backfill enrich`. It records its argument list, then plays one entry of
#: `DEN_STUB_SCRIPT` per invocation — a batch report to print, an `exit` code to fail with, or `silent` for
#: a batch that leaves nothing to read. The last entry repeats, which is how a stall or an outage is spelled.
STUB = '''#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
out = argv[argv.index("--out-dir") + 1]
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "stub-argv.jsonl"), "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
with open(os.path.join(out, "stub-argv.jsonl")) as fh:
    calls = len([line for line in fh if line.strip()])

script = json.loads(os.environ.get("DEN_STUB_SCRIPT") or "[]")
step = script[min(calls, len(script)) - 1] if script else {"remaining": 0, "count": 0}
if "exit" in step:
    sys.exit(step["exit"])
os.makedirs(os.path.join(out, "enriched"), exist_ok=True)
with open(os.path.join(out, "enrich-checkpoint.json"), "w") as fh:
    json.dump({"processed": [], "nextBatch": calls + 1}, fh)
if not step.get("silent"):
    print(json.dumps(step))
'''


def write_stub(directory):
    """The stub, executable, and the environment that points the stage at it."""
    path = os.path.join(directory, "taxonomy-backfill-stub")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(STUB)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    os.environ["DEN_BACKFILL_BIN"] = path
    return path


def write_inputs(out, drained=False):
    """Both worklists, under their declared filenames, and optionally the checkpoint that drains them."""
    for media, entries in WORKLIST.items():
        with open(os.path.join(out, UNIVERSE[media].filename), "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
    if drained:
        with open(os.path.join(out, artifacts.ENRICH_CHECKPOINT.filename), "w", encoding="utf-8") as fh:
            json.dump(DRAINED, fh)
        os.makedirs(os.path.join(out, artifacts.ENRICHED.filename), exist_ok=True)


def hand_typed(out, media, size=fetch.BATCH):
    """The arguments `scripts/enrich-all.sh` hands the command, on this out-dir.

    The flags and their order are the driver's, and `$SIZE` is the batch;
    `test_the_documented_driver_still_spells_it_this_way` holds those against the script itself rather than
    against anyone's memory of it. The PATH is not the driver's any more: its `$WORKLIST` is
    `$OUT_DIR/worklist-$MEDIA.json`, the file `scripts/build-worklist.py` writes, and the pipeline's
    universe is a different list under a different name. Taking it from the declaration is the point of the
    rename — a fixture that followed the driver here would drain the list the stage is no longer fed.
    """
    return ["--worklist", os.path.join(out, UNIVERSE[media].filename),
            "--limit", str(size),
            "--out-dir", out]


def context(out, **kwargs):
    return Context(out_dir=out, dataset_version=VERSION, **kwargs)


def enrich_flags():
    """The flags `Spec` declares for `enrich`, read off its own row of the table.

    The row, not the whole file: every other subcommand's flags are in there too, and a check that any
    subcommand somewhere declares `--labels` would pass for a stage sending it to one that does not.
    """
    with open(os.path.join(REPO, "Sources", "taxonomy-backfill", "main.swift"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index('name: "enrich",')
    return set(re.findall(r'"(--[a-z-]+)"', source[start:source.index("Subcommand(", start)]))


def driver():
    with open(os.path.join(REPO, "scripts", "enrich-all.sh"), encoding="utf-8") as fh:
        return fh.read()


def invocations(out):
    with open(os.path.join(out, "stub-argv.jsonl"), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class Staged(unittest.TestCase):
    """A stub binary and a key in the environment for the duration, both restored afterwards.

    `TMDB_API_KEY` is set because it is what tells the stage its credentials are already in hand — without
    it every batch would go through `scripts/lib/den-env.sh`, which wants a `den.env` beside the checkout.
    The stub never calls TMDB, so its value is irrelevant; the real binary's tests drain a finished
    worklist, which returns before a client is built.
    """

    def setUp(self):
        self.previous = {name: os.environ.get(name) for name in ("DEN_BACKFILL_BIN", "TMDB_API_KEY")}
        self.directory = tempfile.TemporaryDirectory()
        self.out = self.directory.name
        write_stub(self.out)
        write_inputs(self.out)
        os.environ["TMDB_API_KEY"] = "stub-key-nothing-here-calls-tmdb"
        # Nothing may sleep: the backoffs are minutes, and what is under test is the counting.
        self.slept = []
        self.sleeper = fetch.pause
        fetch.pause = self.slept.append

    def tearDown(self):
        fetch.pause = self.sleeper
        os.environ.pop("DEN_STUB_SCRIPT", None)
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.directory.cleanup()

    def script(self, *steps):
        os.environ["DEN_STUB_SCRIPT"] = json.dumps(list(steps))


class Declaration(Staged):
    def test_every_flag_it_sends_is_one_the_enrich_row_declares(self):
        """The pin. The command refuses a flag it has not declared, so a flag this stage invents is a run
        that dies at the first batch — this is where that is found instead."""
        declared = enrich_flags()
        self.assertIn("--worklist", declared)
        for media in fetch.MEDIA:
            sent = [a for a in fetch.argv(context(self.out, vote_floor=0), media) if a.startswith("--")]
            self.assertTrue(sent)
            for flag in sent:
                self.assertIn(flag, declared, f"{flag} is not a flag enrich declares")

    def test_it_never_sends_the_anime_exclusion(self):
        """Opt-IN in the command, because excluding anime by default silently cost the corpus 1,498 titles
        including the entire Ghibli catalogue. A stage that sent it unasked would reinstate that."""
        self.assertIn("--exclude-anime", enrich_flags())
        for media in fetch.MEDIA:
            self.assertNotIn("--exclude-anime", fetch.argv(context(self.out, vote_floor=0), media))

    def test_it_declares_the_batches_and_the_checkpoint_that_resumes_them(self):
        """A stage that declared only the batches would leave the resume state owned by nobody — and an
        absent checkpoint is not an empty one: it is what made a delta restart the numbering at 1 and
        overwrite two batches whose vote passes still named the titles they used to hold."""
        self.assertEqual([bind(e).name for e in fetch.OUTPUTS], ["enriched", "enrich_checkpoint"])

    def test_both_worklists_are_declared_and_reach_the_command_under_one_name(self):
        """The universe is two files because `enrich` refuses a batch that mixes media — a film and a
        series can share a tmdbId. Each is its own artifact; the flag is the reader's single word."""
        bound = {bind(e).name: bind(e) for e in fetch.INPUTS}
        self.assertEqual(sorted(bound), ["universe_movie", "universe_tv"])
        for entry in bound.values():
            self.assertEqual(entry.flag(), "--worklist")


class CommandLine(Staged):
    def test_it_hands_over_the_documented_arguments(self):
        for media in fetch.MEDIA:
            self.assertEqual(fetch.argv(context(self.out), media)[2:], hand_typed(self.out, media))

    def test_the_documented_driver_still_spells_it_this_way(self):
        """`hand_typed` is only an oracle while its flags are the driver's. Held against the script so a
        change there fails here rather than making this test agree with itself.

        The driver's `$WORKLIST` is deliberately NOT asserted against the stage's path. It is
        `$OUT_DIR/worklist-$MEDIA.json` — the list `scripts/build-worklist.py` writes, which enumerates the
        ids Den already ships — while the pipeline's universe enumerates everything TMDB has. The two once
        shared that filename and whichever tool ran last decided what the next enrich billed for, which is
        why the artifact is `universe-<media>.json` now. Holding the stage to the driver's path would hold
        it to the collision.
        """
        self.assertIn('enrich --worklist "$WORKLIST" --limit "$SIZE" --out-dir "$OUT_DIR"', driver())
        self.assertIn('FLOOR_ARG="--vote-floor $VOTE_FLOOR"', driver())
        self.assertIn('WORKLIST="$OUT_DIR/worklist-$MEDIA.json"', driver())
        # The divergence, asserted rather than the agreement: pointing the stage back at the driver's list
        # is what the rename exists to stop, so it fails here instead of billing an enrich for it.
        self.assertNotIn("worklist-movie.json", hand_typed(self.out, "movie")[1])

    def test_the_vote_floor_is_only_passed_when_it_is_named(self):
        """The driver passes it only when `VOTE_FLOOR` is set, and for the same reason: the command's
        default is 50, and a floor nobody chose either re-admits the low-vote tail or hides that a
        worklist cannot drain."""
        self.assertNotIn("--vote-floor", fetch.argv(context(self.out), "movie"))
        command = fetch.argv(context(self.out, vote_floor=0), "movie")
        self.assertEqual(command[command.index("--vote-floor") + 1], "0")

    def test_a_missing_worklist_stops_the_stage(self):
        """A drain pointed at a worklist that is not there checkpoints nothing and reports `remaining` 0,
        which reads exactly like a finished run."""
        os.remove(os.path.join(self.out, artifacts.UNIVERSE_MOVIE.filename))
        with self.assertRaises(StageError) as refused:
            fetch.argv(context(self.out), "movie")
        # The worklist stage owns the universe now, so the refusal sends an operator to the rule that
        # stage runs rather than to a `how` the artifact carried for itself.
        self.assertIn("taxonomy-backfill worklist", str(refused.exception))

    def test_a_media_the_worklists_do_not_come_in_is_refused(self):
        with self.assertRaises(StageError) as refused:
            fetch.media_types(context(self.out, media="anime"))
        self.assertIn("movie, tv", str(refused.exception))

    def test_a_missing_binary_is_refused_with_the_build_that_makes_it(self):
        os.environ["DEN_BACKFILL_BIN"] = os.path.join(self.out, "not-built")
        with self.assertRaises(StageError) as refused:
            fetch.argv(context(self.out), "movie")
        self.assertIn("swift build -c release", str(refused.exception))

    def test_the_two_credentials_are_asked_about_separately(self):
        """`TMDB_API_KEY` says nothing about the Wikimedia ones, and they arrive together only on a
        workstation: GitHub Actions supplies both as environment secrets with no `den.env` to read.

        Keying the whole bootstrap on the TMDB key meant exporting it by hand skipped `enterprise_login`
        too, and every plot came from the free action API. That is not a speed difference — the Enterprise
        path records no revision id and no resolved article, so it is the one that CANNOT see a redirect,
        and two runs of the same command would differ in what they know about their own rows.
        """
        command = fetch.argv(context(self.out), "movie")
        os.environ.pop("WIKIMEDIA_ENTERPRISE_TOKEN", None)

        # No TMDB key: the file is the only place it can come from, so read it, then log in.
        os.environ.pop("TMDB_API_KEY")
        full = fetch.bootstrap(command)
        self.assertEqual(full[:2], ["bash", "-c"])
        self.assertIn("den_load_env", full[2])
        self.assertIn("enterprise_login", full[2])
        self.assertEqual(full[4:], command)

        # Key present, no bearer — the Actions shape. Still log in, but do NOT demand a den.env that run
        # has no reason to own; insisting on the file is what would fail the job outright.
        os.environ["TMDB_API_KEY"] = "k"
        login = fetch.bootstrap(command)
        self.assertEqual(login[:2], ["bash", "-c"])
        self.assertNotIn("den_load_env", login[2])
        self.assertIn("enterprise_login", login[2])
        self.assertEqual(login[4:], command)

        # Both already held: nothing left for the shell to mint.
        os.environ["WIKIMEDIA_ENTERPRISE_TOKEN"] = "t"
        self.addCleanup(os.environ.pop, "WIKIMEDIA_ENTERPRISE_TOKEN", None)
        self.assertEqual(fetch.bootstrap(command), command)


class Loop(Staged):
    def test_it_keeps_running_batches_until_nothing_remains(self):
        """One `enrich` is one batch; the corpus is ~120 of them. A stage that ran one and returned would
        report a tenth of a run as a finished one."""
        self.script({"remaining": 900, "count": 500}, {"remaining": 400, "count": 500},
                    {"remaining": 0, "count": 400})
        self.assertEqual(fetch.drain(context(self.out, media="movie"), "movie"), 3)
        self.assertEqual(len(invocations(self.out)), 3)

    def test_every_batch_is_handed_the_same_out_dir_and_the_same_worklist(self):
        """Resume, as a property of the loop. The command skips what the checkpoint holds and numbers the
        next batch from the directory, so a loop that moved the out-dir between batches would re-enrich
        from scratch and overwrite the batches it had already written."""
        self.script({"remaining": 10, "count": 500}, {"remaining": 0, "count": 10})
        fetch.drain(context(self.out, media="movie"), "movie")
        first, second = invocations(self.out)
        self.assertEqual(first, second, "a later batch was handed a different command")

    def test_an_aborted_batch_is_retried_with_a_backoff(self):
        """A transient Wikidata outage that outlived the command's own retries used to kill a drain that
        was hours in. The checkpoint makes the retry free."""
        self.script({"exit": 1}, {"exit": 1}, {"remaining": 0, "count": 0})
        self.assertEqual(fetch.drain(context(self.out, media="movie"), "movie"), 1)
        self.assertEqual(self.slept, [30, 60])

    def test_six_aborts_in_a_row_stop_the_drain(self):
        self.script({"exit": 2})
        with self.assertRaises(StageError) as refused:
            fetch.drain(context(self.out, media="movie"), "movie")
        self.assertIn("exit 2", str(refused.exception))
        self.assertEqual(len(invocations(self.out)), fetch.ABORTS)

    def test_a_batch_that_exits_clean_having_moved_nothing_is_a_stall(self):
        """Ids that fail transiently are deliberately not checkpointed, so during an upstream outage every
        id defers and `remaining` does not move. Counting only non-zero EXITS, the loop spun with no sleep
        — re-minting a token and re-issuing the whole batch as fast as the upstream could refuse it."""
        self.script({"remaining": 700, "count": 0, "deferred": 500})
        with self.assertRaises(StageError) as refused:
            fetch.drain(context(self.out, media="movie"), "movie")
        self.assertIn("700 still pending", str(refused.exception))
        self.assertEqual(self.slept, [60, 120, 180, 240, 300])

    def test_a_stall_that_clears_does_not_count_against_the_next_one(self):
        self.script({"remaining": 700, "count": 0}, {"remaining": 700, "count": 0},
                    {"remaining": 200, "count": 500}, {"remaining": 0, "count": 200})
        self.assertEqual(fetch.drain(context(self.out, media="movie"), "movie"), 4)
        self.assertEqual(self.slept, [60])

    def test_a_worklist_that_is_entirely_below_the_floor_stops_at_once_and_says_so(self):
        """Below-floor ids are not checkpointed either — a vote count only climbs — so a worklist of them
        can never drain at that floor. It is indistinguishable from an outage by `remaining` alone, and
        reporting it as one cost two and a half hours of backoff while Wikipedia was answering fine."""
        self.script({"remaining": 700, "count": 0, "belowFloor": 500})
        with self.assertRaises(StageError) as refused:
            fetch.drain(context(self.out, media="movie"), "movie")
        self.assertIn("--vote-floor 0", str(refused.exception))
        self.assertEqual(self.slept, [], "it backed off over something waiting cannot fix")
        self.assertEqual(len(invocations(self.out)), 2)

    def test_a_batch_whose_report_cannot_be_read_stops_the_drain(self):
        """The driver defaulted this to a literal `?`, which is neither 0 nor the previous value, so an
        unreadable report read as progress: the loop could not tell a finished drain from a batch whose
        output it had lost."""
        self.script({"silent": True})
        with self.assertRaises(StageError) as refused:
            fetch.drain(context(self.out, media="movie"), "movie")
        self.assertIn("no report", str(refused.exception))


class Run(Staged):
    def test_it_drains_both_media_when_neither_is_named(self):
        """A corpus is both worklists. `docs/OPERATE.md` says "movie 150; then tv 150" — a step that has
        to be remembered twice is a step that gets half-done."""
        made = fetch.run(context(self.out))
        worklists = [call[call.index("--worklist") + 1] for call in invocations(self.out)]
        self.assertEqual([os.path.basename(path) for path in worklists],
                         [UNIVERSE["movie"].filename, UNIVERSE["tv"].filename])
        self.assertIn(os.path.join(self.out, "enriched"), made)

    def test_naming_a_media_drains_only_that_one(self):
        fetch.run(context(self.out, media="tv"))
        worklists = [call[call.index("--worklist") + 1] for call in invocations(self.out)]
        self.assertEqual([os.path.basename(path) for path in worklists], [UNIVERSE["tv"].filename])

    def test_a_run_whose_outputs_landed_elsewhere_is_refused(self):
        """Both are derived by the command from `--out-dir`. An override that names one elsewhere does not
        move it — it splits the batches from the state that says which ids they cover."""
        elsewhere = {"enrich_checkpoint": os.path.join(self.out, "elsewhere", "enrich-checkpoint.json")}
        with self.assertRaises(StageError) as refused:
            fetch.run(context(self.out, overrides=elsewhere))
        self.assertIn("--out-dir", str(refused.exception))


@unittest.skipUnless(os.path.exists(BUILT), f"taxonomy-backfill is not built at {BUILT}")
class Oracle(unittest.TestCase):
    """The real command, on a drained out-dir.

    `enrich` returns on `pending.isEmpty` before it builds a TMDB client, so this runs the documented path
    through the parser that will see it in production without a key, a network call or a written byte.
    """

    def setUp(self):
        self.previous = {name: os.environ.get(name) for name in ("DEN_BACKFILL_BIN", "TMDB_API_KEY")}
        self.directory = tempfile.TemporaryDirectory()
        self.out = self.directory.name
        write_inputs(self.out, drained=True)
        os.environ["DEN_BACKFILL_BIN"] = BUILT
        os.environ["TMDB_API_KEY"] = "deliberately-invalid-nothing-here-may-reach-tmdb"
        # The drain's backoff is 30 seconds and grows. A test that reaches it is a test that found
        # something, and it should say so in a second rather than in three minutes.
        self.sleeper = fetch.pause
        fetch.pause = lambda seconds: None

    def tearDown(self):
        fetch.pause = self.sleeper
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.directory.cleanup()

    def enrich(self, *arguments):
        return subprocess.run([BUILT, "enrich", *arguments], cwd=REPO, capture_output=True, text=True)

    def test_the_binary_declares_its_flags(self):
        """Everything below is only an oracle against a command that refuses what it has not declared. A
        binary built before that landed accepts any argument list, so it would pass while proving nothing
        — rebuild it rather than reading a pass here as one."""
        listed = self.enrich("--help")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        for flag in enrich_flags():
            self.assertIn(flag, listed.stdout)

    def test_the_command_accepts_the_argument_list_the_stage_builds(self):
        """The equivalence. Not byte comparison of the artifact — this pass fetches from TMDB and from
        live Wikipedia, so two runs of one command do not agree and a title's article can change between
        them. What holds is that the arguments the stage builds are the ones the command declares, proved
        by the parser that will receive them rather than by a list copied out of the table."""
        result = self.enrich(*fetch.argv(context(self.out), "movie")[2:])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.strip()), {"remaining": 0, "count": 0})

    def test_it_reports_the_same_batch_the_documented_command_reports(self):
        """The same drained out-dir, from the stage's arguments and from `enrich-all.sh`'s.

        Compared as a report and not as bytes, because the bytes are not stable even between two runs of
        ONE command: `JSON.line` serialises a Swift `Dictionary`, whose key order is per-process, so the
        same batch prints `{"count":0,"remaining":0}` and `{"remaining":0,"count":0}` on consecutive runs.
        Asserting bytes here would be asserting a coincidence — which is also why the loop reads the
        report by key rather than positionally.
        """
        through_stage = self.enrich(*fetch.argv(context(self.out), "movie")[2:])
        by_hand = self.enrich(*hand_typed(self.out, "movie"))
        self.assertEqual(through_stage.returncode, 0, through_stage.stderr)
        self.assertEqual(by_hand.returncode, 0, by_hand.stderr)
        self.assertEqual(json.loads(through_stage.stdout), json.loads(by_hand.stdout))

    def test_a_flag_the_table_does_not_declare_is_refused(self):
        """What makes the test above worth anything. The command reads its own declarations, so an
        argument list this stage invented dies at the first batch — which is the point of building it
        from the declaration."""
        refused = self.enrich(*fetch.argv(context(self.out), "movie")[2:], "--plot-cap", "3500")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("unknown flag --plot-cap", refused.stderr)

    def test_a_value_flag_with_no_value_is_refused(self):
        """The other silence the declaration closed: `--limit --out-dir out` used to make `--limit` bare
        and hand its value to the next flag, leaving the default standing."""
        arguments = fetch.argv(context(self.out), "movie")[2:]
        refused = self.enrich(*[a for a in arguments if a != str(fetch.BATCH)])
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("takes a value", refused.stderr)

    def test_a_drained_worklist_is_one_batch_that_fetches_nothing_and_writes_nothing(self):
        """`den stage fetch` end to end, through the stage's own loop, over the real command. A resumed
        run that has nothing left to do must cost nothing and must still terminate — the loop reads that
        off the command's own report, which is the only thing that knows."""
        checkpoint = os.path.join(self.out, artifacts.ENRICH_CHECKPOINT.filename)
        with open(checkpoint, "rb") as fh:
            before = fh.read()
        made = fetch.run(context(self.out, media="movie"))
        self.assertIn("1 movie batch(es)", made)
        self.assertEqual(os.listdir(os.path.join(self.out, "enriched")), [],
                         "a drained run wrote a batch, so it fetched titles it already had")
        with open(checkpoint, "rb") as fh:
            self.assertEqual(fh.read(), before, "a drained run rewrote the checkpoint")

    def test_a_worklist_the_command_would_refuse_is_refused_by_the_run(self):
        """A mixed worklist is the one input the command rejects outright: the vote files written from it
        carry no media type, so an id that exists as both a film and a series would be labelled once and
        applied to both. The loop must surface that rather than retry it six times."""
        mixed = os.path.join(self.out, artifacts.UNIVERSE_MOVIE.filename)
        with open(mixed, "w", encoding="utf-8") as fh:
            json.dump(WORKLIST["movie"] + WORKLIST["tv"], fh)
        with open(os.path.join(self.out, artifacts.ENRICH_CHECKPOINT.filename), "w", encoding="utf-8") as fh:
            json.dump({"processed": [], "nextBatch": 1}, fh)
        with self.assertRaises(StageError) as refused:
            fetch.drain(context(self.out, media="movie"), "movie")
        self.assertIn("aborted", str(refused.exception))


class Topology(unittest.TestCase):
    def test_the_batches_and_the_checkpoint_are_owned_by_the_stage_that_writes_them(self):
        """`enriched` used to name `scripts/enrich-run.sh` on the artifact — a field free to name one
        thing while the run did another. The stage that declares it in OUTPUTS is the answer now."""
        for name in ("enriched", "enrich_checkpoint"):
            self.assertEqual(getattr(artifacts, name.upper()).producer, "")
            self.assertEqual(pipeline.producers()[name], (fetch.PRODUCER, fetch.HOW, False))

    def test_the_producer_it_names_is_a_file_in_the_tree(self):
        self.assertEqual(fetch.PRODUCER, artifacts.BACKFILL)
        self.assertTrue(os.path.isfile(os.path.join(REPO, fetch.PRODUCER)))

    def test_the_titles_are_enriched_before_anything_reads_their_plots(self):
        self.assertLess(pipeline.STAGES.index("fetch"), pipeline.STAGES.index("embed"))
        self.assertIn(artifacts.ENRICHED, [bind(e).artifact for e in embed.INPUTS])

    def test_the_universes_it_drains_are_owned_by_the_stage_that_builds_them(self):
        """The seam this input sat behind is closed. The worklist build landed as a stage while this one
        was in flight, so the two files stopped answering for themselves the way `enriched`'s fields just
        emptied — and a drain that finds one missing is sent to the stage that writes it, not to a `how`
        the artifact carried."""
        self.assertLess(pipeline.STAGES.index("worklist"), pipeline.STAGES.index("fetch"))
        for artifact in (artifacts.UNIVERSE_MOVIE, artifacts.UNIVERSE_TV):
            self.assertEqual(artifact.producer, "")
            self.assertEqual(pipeline.producers()[artifact.name],
                             (worklist.PRODUCER, worklist.HOW, False))


if __name__ == "__main__":
    unittest.main()
