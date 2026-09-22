#!/usr/bin/env python3
"""The fetch stage — the loop that decides when a drain is done, and the credentials it runs under.

One batch is `pipeline/enrich.py` and is tested there. What is tested here is the stage around it:

  * **the loop.** `scripts/enrich-all.sh` ran the batches and decided when to stop, and that decision is
    the stage's. Each of its three stopping rules is exercised — the retried abort, the stall with ids
    still being attempted, and the batch where every title is below the vote floor and no amount of
    waiting will help — against a scripted `batch`, because what is under test is the counting, and a
    real drain is ~120 batches of TMDB quota.
  * **the credential split.** The TMDB key and the Wikimedia bearer are asked about separately, because
    they arrive together only on a workstation.
  * **a drained run end to end**, through the real batch code, which returns before it builds a TMDB
    client: nothing is fetched and nothing is written.
"""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import pipeline

from . import artifacts, embed, enrich, fetch, worklist
from .contract import Context, StageError, bind

VERSION = "testver"
WORKLIST = {"movie": [{"tmdbId": 550, "mediaType": "movie"}],
            "tv": [{"tmdbId": 1396, "mediaType": "tv"}]}
UNIVERSE = {"movie": artifacts.UNIVERSE_MOVIE, "tv": artifacts.UNIVERSE_TV}
DRAINED = {"processed": ["movie:550", "tv:1396"], "nextBatch": 3}


def write_inputs(out, drained=False):
    for media, entries in WORKLIST.items():
        with open(os.path.join(out, UNIVERSE[media].filename), "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
    if drained:
        with open(os.path.join(out, artifacts.ENRICH_CHECKPOINT.filename), "w", encoding="utf-8") as fh:
            json.dump(DRAINED, fh)
        os.makedirs(os.path.join(out, artifacts.ENRICHED.filename), exist_ok=True)


def context(out, **kwargs):
    return Context(out_dir=out, dataset_version=VERSION, **kwargs)


class Staged(unittest.TestCase):
    """A temp out-dir with both universes; `batch` scripted and `pause` recorded rather than slept."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name
        write_inputs(self.out)
        self.slept, self.calls, self.steps = [], [], []
        for name, stub in (("pause", self.slept.append), ("batch", self.scripted)):
            patch = mock.patch.object(fetch, name, stub)
            patch.start()
            self.addCleanup(patch.stop)

    def script(self, *steps):
        self.steps = list(steps)

    def scripted(self, ctx, media):
        """One step per call; the last repeats, which is how a stall or an outage is spelled. Bounded, so a
        stopping rule that stops holding fails here instead of spinning forever."""
        self.calls.append(media)
        if len(self.calls) > 50:
            raise AssertionError("the drain never stopped")
        step = self.steps[min(len(self.calls), len(self.steps)) - 1] if self.steps else {"remaining": 0}
        if isinstance(step, Exception):
            raise step
        return step


class Declaration(unittest.TestCase):
    def test_it_declares_the_batches_and_the_checkpoint_that_resumes_them(self):
        """An absent checkpoint is not an empty one: it made a delta restart the numbering at 1 and
        overwrite two batches."""
        self.assertEqual([bind(e).name for e in fetch.OUTPUTS], ["enriched", "enrich_checkpoint"])

    def test_both_worklists_are_declared(self):
        self.assertEqual(sorted(bind(e).name for e in fetch.INPUTS), ["universe_movie", "universe_tv"])

    def test_a_media_the_worklists_do_not_come_in_is_refused(self):
        with self.assertRaises(StageError) as refused:
            fetch.media_types(context("out", media="anime"))
        self.assertIn("movie, tv", str(refused.exception))

    def test_it_never_asks_for_the_anime_exclusion(self):
        """Opt-IN, because excluding anime by default silently cost the corpus 1,498 titles."""
        with mock.patch.object(enrich, "run", return_value={"remaining": 0}) as ran, \
                mock.patch.object(fetch, "credentials", return_value=("k", None)):
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            write_inputs(directory.name)
            fetch.batch(context(directory.name), "movie")
        self.assertNotIn("exclude_anime", ran.call_args.kwargs)
        self.assertEqual(ran.call_args.kwargs["limit"], fetch.BATCH)
        self.assertEqual(ran.call_args.kwargs["vote_floor"], enrich.VOTE_FLOOR)

    def test_a_missing_worklist_stops_the_stage_and_names_who_builds_it(self):
        """A drain pointed at a worklist that is not there checkpoints nothing and reports `remaining` 0,
        which reads exactly like a finished run."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with mock.patch.object(fetch, "credentials", return_value=("k", None)):
            with self.assertRaises(StageError) as refused:
                fetch.batch(context(directory.name), "movie")
        self.assertIn(worklist.HOW, str(refused.exception))


class Credentials(unittest.TestCase):
    """The shell is the seam: `scripts/lib/den-env.sh` still reads `den.env` and mints the bearer."""

    def minted(self, environ, stdout=b"file-key\0bearer\0", code=0):
        completed = subprocess.CompletedProcess([], code, stdout=stdout)
        with mock.patch.object(fetch.subprocess, "run", return_value=completed) as ran:
            answer = fetch.credentials(environ)
        return answer, (ran.call_args[0][0][2] if ran.called else None)

    def test_the_two_credentials_are_asked_about_separately(self):
        """Keying the whole bootstrap on the TMDB key meant exporting it by hand skipped the Enterprise
        login too — a run that records less about its own rows, not just a slower one."""
        (key, bearer), script = self.minted({})
        self.assertEqual((key, bearer), ("file-key", "bearer"))
        self.assertIn("den_load_env", script)
        self.assertIn("enterprise_login", script)

        (key, bearer), script = self.minted({"TMDB_API_KEY": "k"}, stdout=b"k\0bearer\0")
        self.assertEqual((key, bearer), ("k", "bearer"))
        self.assertNotIn("den_load_env", script, "GitHub Actions has the key and no den.env to read")
        self.assertIn("enterprise_login", script)

        (key, bearer), script = self.minted({"TMDB_API_KEY": "k", "WIKIMEDIA_ENTERPRISE_TOKEN": "t"})
        self.assertEqual(((key, bearer), script), (("k", "t"), None), "nothing left to mint")

    def test_no_bearer_is_none_and_the_free_api_answers(self):
        (key, bearer), _script = self.minted({"TMDB_API_KEY": "k"}, stdout=b"k\0\0")
        self.assertIsNone(bearer)

    def test_no_key_anywhere_is_a_refusal_not_an_abort(self):
        """No den.env will not appear by waiting; the shell driver spent six backoffs finding that out."""
        with self.assertRaises(StageError):
            self.minted({}, stdout=b"", code=1)

    def test_the_secrets_never_reach_an_argv(self):
        _answer, script = self.minted({})
        self.assertNotIn("file-key", script)


class Loop(Staged):
    def test_it_keeps_running_batches_until_nothing_remains(self):
        self.script({"remaining": 900, "count": 500}, {"remaining": 400, "count": 500},
                    {"remaining": 0, "count": 400})
        self.assertEqual(fetch.drain(context(self.out), "movie"), 3)

    def test_an_aborted_batch_is_retried_with_a_backoff(self):
        """A transient Wikidata outage that outlived the retries used to kill a drain hours in."""
        self.script(enrich.Aborted("wdqs"), enrich.Aborted("wdqs"), {"remaining": 0, "count": 0})
        self.assertEqual(fetch.drain(context(self.out), "movie"), 1)
        self.assertEqual(self.slept, [30, 60])

    def test_six_aborts_in_a_row_stop_the_drain(self):
        self.script(enrich.Aborted("wdqs down"))
        with self.assertRaises(StageError) as refused:
            fetch.drain(context(self.out), "movie")
        self.assertIn("wdqs down", str(refused.exception))
        self.assertEqual(len(self.calls), fetch.ABORTS)

    def test_a_refusal_is_not_retried(self):
        """An unreadable checkpoint or a batch that would be overwritten says the same thing every time."""
        self.script(StageError("refusing to overwrite"))
        with self.assertRaises(StageError):
            fetch.drain(context(self.out), "movie")
        self.assertEqual((len(self.calls), self.slept), (1, []))

    def test_a_batch_that_moved_nothing_is_a_stall(self):
        """Transient ids are not checkpointed, so in an outage every id defers and `remaining` does not
        move. Counting only aborts, the loop spun with no sleep."""
        self.script({"remaining": 700, "count": 0, "deferred": 500})
        with self.assertRaises(StageError) as refused:
            fetch.drain(context(self.out), "movie")
        self.assertIn("700 still pending", str(refused.exception))
        self.assertEqual(self.slept, [60, 120, 180, 240, 300])

    def test_a_stall_that_clears_does_not_count_against_the_next_one(self):
        self.script({"remaining": 700, "count": 0}, {"remaining": 700, "count": 0},
                    {"remaining": 200, "count": 500}, {"remaining": 0, "count": 200})
        self.assertEqual(fetch.drain(context(self.out), "movie"), 4)
        self.assertEqual(self.slept, [60])

    def test_a_worklist_entirely_below_the_floor_stops_at_once_and_says_so(self):
        """It is indistinguishable from an outage by `remaining` alone, and reporting it as one cost two
        and a half hours of backoff while Wikipedia was answering fine."""
        self.script({"remaining": 700, "count": 0, "belowFloor": 500})
        with self.assertRaises(StageError) as refused:
            fetch.drain(context(self.out), "movie")
        self.assertIn("--vote-floor 0", str(refused.exception))
        self.assertEqual((self.slept, len(self.calls)), ([], 2))


class Run(Staged):
    """The scripted batch writes nothing, so the outputs the run checks for are laid down beforehand."""

    def setUp(self):
        super().setUp()
        write_inputs(self.out, drained=True)

    def test_it_drains_both_media_when_neither_is_named(self):
        """A corpus is both worklists; a step that has to be remembered twice gets half-done."""
        made = fetch.run(context(self.out))
        self.assertEqual(self.calls, ["movie", "tv"])
        self.assertIn("enriched", made)

    def test_naming_a_media_drains_only_that_one(self):
        fetch.run(context(self.out, media="tv"))
        self.assertEqual(self.calls, ["tv"])

    def test_a_run_whose_outputs_landed_elsewhere_is_refused(self):
        elsewhere = {"enrich_checkpoint": os.path.join(self.out, "elsewhere", "enrich-checkpoint.json")}
        with self.assertRaises(StageError) as refused:
            fetch.run(context(self.out, overrides=elsewhere))
        self.assertIn("--out-dir", str(refused.exception))


class Drained(unittest.TestCase):
    """The real batch code on a drained out-dir: it returns before it builds a TMDB client."""

    def test_a_drained_worklist_is_one_batch_that_fetches_nothing_and_writes_nothing(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out = directory.name
        write_inputs(out, drained=True)
        checkpoint = os.path.join(out, artifacts.ENRICH_CHECKPOINT.filename)
        with open(checkpoint, "rb") as fh:
            before = fh.read()
        environ = {"TMDB_API_KEY": "nothing-may-reach-tmdb", "WIKIMEDIA_ENTERPRISE_TOKEN": "unused"}
        with mock.patch.dict(os.environ, environ), \
                mock.patch.object(enrich.http, "request", side_effect=AssertionError("no request")):
            made = fetch.run(context(out, media="movie"))
        self.assertIn("1 movie batch(es)", made)
        self.assertEqual(os.listdir(os.path.join(out, "enriched")), [])
        with open(checkpoint, "rb") as fh:
            self.assertEqual(fh.read(), before, "a drained run rewrote the checkpoint")


class Topology(unittest.TestCase):
    def test_the_batches_and_the_checkpoint_are_owned_by_the_stage_that_writes_them(self):
        for name in ("enriched", "enrich_checkpoint"):
            self.assertEqual(getattr(artifacts, name.upper()).producer, "")
            self.assertEqual(pipeline.producers()[name], (fetch.PRODUCER, fetch.HOW, False))

    def test_the_producer_it_names_is_a_file_in_the_tree(self):
        self.assertTrue(os.path.isfile(os.path.join(fetch.REPO, fetch.PRODUCER)))

    def test_the_titles_are_enriched_before_anything_reads_their_plots(self):
        self.assertLess(pipeline.STAGES.index("fetch"), pipeline.STAGES.index("embed"))
        self.assertIn(artifacts.ENRICHED, [bind(e).artifact for e in embed.INPUTS])

    def test_the_universes_it_drains_are_owned_by_the_stage_that_builds_them(self):
        self.assertLess(pipeline.STAGES.index("worklist"), pipeline.STAGES.index("fetch"))
        for artifact in (artifacts.UNIVERSE_MOVIE, artifacts.UNIVERSE_TV):
            self.assertEqual(pipeline.producers()[artifact.name][:2], (worklist.PRODUCER, worklist.HOW))


if __name__ == "__main__":
    unittest.main()
