#!/usr/bin/env python3
"""The classify stage — that it is a wrapper, and that the two things this port added still hold.

The pass itself is tested where it lives, in `pipeline/run_combined_test.py`: the typed-answer validator, the
oversized planner, the circuit breaker, the output lock and the manifest's refusal of a changed
configuration. None of them is reimplemented here. What is tested here is the stage around them, which had
to answer two questions the corpus and store stages did not:

  * **an output that is a SHARD SET.** `combined` is read as a set — three files today — and written one
    file at a time, so the stage needs a name to write that lands inside the set the join will glob. A pass
    sent to a name of its own is a second manifest, which is a second $20.47.
  * **an equivalence that cannot be byte comparison.** The corpus and store stages are diffed against their
    hand-typed commands by hashing what they wrote. This pass writes rows stamped with a fresh `runId`
    (uuid4) and a `runStartedAt` of now, and buys them from a provider, so two runs of one command are not
    byte-identical and a third-party model is not a fixture. The strongest property that DOES hold is the
    pass's own: a manifest built from the documented command's arguments is accepted, unchanged, by a run
    the stage launches. `manifest_config` hashes the article file, the enriched evidence, the prompt, the
    taxonomy, the model, the state ceiling and the question set, and `load_or_create_manifest` refuses a
    configuration that differs by any of them — so the stage handing over anything else is a refusal, not a
    quietly different pass.

The article fixture is `pipeline/run_combined_test.py`'s, reused rather than rebuilt.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import pipeline

from . import artifacts, classify, corpus, embed, fetch
from . import combined_questions as questions
from . import run_combined as script
from . import run_combined_test as fixture
from .contract import Context, StageError, bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VERSION = "testver"

#: The shard a pass writes: the declared glob with an empty wildcard.
SHARD = "combined-v1-r2.jsonl"

#: One ordinary title — a lead, a plot section and a reception section — which the pass plans as a single
#: call. The oversized path is `run_combined_test.py`'s; nothing about it is the stage's.
ARTICLE = "Lead.\n\n== Plot ==\nA happens.\n\n== Reception ==\nCritics liked it."


def write_inputs(out):
    """Every declared input, under its declared filename.

    The enriched directory is empty on purpose: the fixture record already carries `year` and
    `plotSections`, so the pass never reads the batches — but the flag is still handed over, and the stage
    still refuses a run whose enrichment is absent.
    """
    with open(os.path.join(out, artifacts.ARTICLES.filename), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(fixture.record(ARTICLE)) + "\n")
    os.makedirs(os.path.join(out, artifacts.ENRICHED.filename), exist_ok=True)


def hand_typed(out):
    """The arguments `docs/FACETS-V2.md` writes out, on this out-dir.

    The documented dry run names `--out out-repass/combined-v1.jsonl`; the shipped pass is the `-r2` rerun
    and the catalogue's glob is named for it, so the pipeline's shard name is what stands in for the path.
    Everything that decides what the pass BUYS — the article file, the enrichment, the prompt, the taxonomy
    and the model — is identical either way, and the manifest hashes all of it.
    """
    return ["--articles", os.path.join(out, artifacts.ARTICLES.filename),
            "--enriched-dir", os.path.join(out, artifacts.ENRICHED.filename),
            "--out", os.path.join(out, SHARD)]


def context(out, **kwargs):
    return Context(out_dir=out, dataset_version=VERSION, **kwargs)


def finished_pass(out):
    """A completed pass at the declared paths, manifested from the DOCUMENTED command's own arguments.

    Built with the pass's own `manifest_config` and `load_or_create_manifest` rather than with a literal,
    so the hashes are the ones a real run would compute. A run the stage launches against this either
    accepts the manifest as its own — which is the equivalence — or exits nonzero saying it belongs to a
    different configuration.
    """
    args = script.argument_parser().parse_args(hand_typed(out))
    global_qs, mapping, taxonomy = questions.global_questions(args.prompt, args.taxonomy)
    records, _ = script.load_articles(args.articles)
    evidence = script.attach_enriched_evidence(records, args.enriched_dir)
    config = script.manifest_config(args, global_qs, mapping, taxonomy, evidence)
    manifest = script.load_or_create_manifest(args.out + ".manifest.json", config)
    with open(args.out, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps({"mediaType": record["mediaType"], "tmdbId": record["tmdbId"],
                                 "runId": manifest["runId"],
                                 "configSha256": manifest["configSha256"]}) + "\n")
    return manifest


class Declaration(unittest.TestCase):
    def test_the_stage_declares_exactly_what_the_pass_parses(self):
        """The pin the derived registry hangs from. The pass has no `INPUT_ARGS` of its own to check
        against, so the check is its parser: every declared argument name is one the pass has."""
        parsed = script.argument_parser().parse_args(hand_typed("/nowhere"))
        for entry in (bind(e) for e in classify.PASSED):
            self.assertTrue(hasattr(parsed, entry.arg), f"{entry.arg} is not an argument of the pass")

    def test_the_outputs_are_the_shard_and_the_manifest_beside_it(self):
        """A stage that declared only the rows would leave the provenance for a $20 artifact owned by
        nobody — and the auditor reads it by a name derived from `--out`, not by one it is given."""
        self.assertEqual([bind(e).name for e in classify.OUTPUTS],
                         ["combined", "combined_manifest", "changed_articles"])

    def test_the_enrichment_keeps_one_name_and_reaches_the_pass_under_its_own(self):
        """`out/enriched` is `--enriched-dir` to this pass and to the embed pass, and the pipeline calls
        the directory `enriched` — one file, one name, and the flag is the reader's word for it."""
        bound = {bind(e).name: bind(e) for e in classify.INPUTS}["enriched"]
        self.assertEqual(bound.flag(), "--enriched-dir")
        self.assertEqual(bound.artifact, artifacts.ENRICHED)
        self.assertIn(artifacts.ENRICHED, [bind(e).artifact for e in embed.INPUTS])

    def test_the_stage_names_no_model_prompt_or_taxonomy(self):
        """They are constants the pass already holds, hashed into the manifest it refuses a change to. A
        second spelling here could only ever disagree with the one the registry and the audit read."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            command = classify.argv(context(out))
            for flag in ("--model", "--prompt", "--taxonomy", "--max-state-chars"):
                self.assertNotIn(flag, command)
        self.assertEqual(script.argument_parser().parse_args([
            "--articles", "a", "--out", "b"]).model, questions.PINNED_MODEL)
        self.assertFalse(questions.PINNED_MODEL.endswith("-latest"),
                         "the pass refuses a mutable alias, so its default must not be one")


class CommandLine(unittest.TestCase):
    def test_the_passes_own_parser_accepts_what_the_declaration_builds(self):
        """Run through the parser that will receive it, not against a copy of the argument list."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            parsed = script.argument_parser().parse_args(classify.argv(context(out))[2:])
            self.assertEqual(parsed.articles, os.path.join(out, "articles.jsonl"))
            self.assertEqual(parsed.enriched_dir, os.path.join(out, "enriched"))
            self.assertEqual(parsed.out, os.path.join(out, SHARD))
            self.assertFalse(parsed.plan)

    def test_the_shard_it_writes_is_one_the_corpus_join_will_glob(self):
        """The property that keeps a rerun from being a second paid pass: the name a run writes is derived
        from the same glob the readers resolve the set by, so the rows land inside the set rather than
        beside it under a name only this run knows."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            ctx = context(out)
            target = ctx.shard(artifacts.COMBINED)
            open(target, "w", encoding="utf-8").close()
            self.assertIn(target, ctx.paths(artifacts.COMBINED))
            self.assertIn(artifacts.COMBINED, [bind(e).artifact for e in corpus.INPUTS])

    def test_a_missing_article_dump_stops_the_stage(self):
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            os.remove(os.path.join(out, artifacts.ARTICLES.filename))
            with self.assertRaises(StageError) as refused:
                classify.argv(context(out))
            self.assertIn("./den stage articles", str(refused.exception))

    def test_a_missing_enrichment_stops_the_stage(self):
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            os.rmdir(os.path.join(out, artifacts.ENRICHED.filename))
            with self.assertRaises(StageError) as refused:
                classify.argv(context(out))
            # The drain is a stage, so the batches are owned rather than answering for themselves: the
            # refusal names what the pipeline runs, not the shell driver that stage replaced.
            self.assertIn("./den stage fetch", str(refused.exception))

    def test_the_dry_run_is_only_asked_for_when_it_is_asked_for(self):
        """`--plan` decides whether the stage buys anything. Passing it unasked would make every run a
        report; inheriting it would make a report look like a pass."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            self.assertNotIn("--plan", classify.argv(context(out)))
            self.assertIn("--plan", classify.argv(context(out, plan=True)))

    def test_a_shard_set_pointed_at_several_files_cannot_be_written(self):
        """`--set combined=…` repeated names the members of a set to READ. One pass writes one shard, so
        asking which of three to write is refused rather than answered by picking one."""
        with tempfile.TemporaryDirectory() as out:
            ctx = context(out, overrides={"combined": ["a.jsonl", "b.jsonl"]})
            with self.assertRaises(StageError) as refused:
                ctx.shard(artifacts.COMBINED)
            self.assertIn("one shard", str(refused.exception))


class Equivalence(unittest.TestCase):
    def test_the_stage_hands_over_the_documented_arguments(self):
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            self.assertEqual(classify.argv(context(out))[2:], hand_typed(out))

    def test_the_pass_accepts_the_stages_command_line_as_its_own_configuration(self):
        """The oracle. The rows a pass buys cannot be reproduced — a provider answers them and every row
        carries a fresh run id — so what is held to the documented command is the CONFIGURATION, which the
        pass hashes and refuses a change to.

        A manifest is built from the documented arguments and every title marked done. The stage then runs:
        the pass re-derives the hash from the command line the stage gave it, finds it equal, accepts the
        rows already on disk, buys nothing and exits clean. Any flag the stage added, dropped or spelled
        differently changes `configSha256` and the run exits saying so.
        """
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            manifest = finished_pass(out)
            made = classify.run(context(out))
            self.assertEqual(made, os.path.join(out, SHARD))
            with open(made, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual([row["runId"] for row in rows], [manifest["runId"]],
                             "the pass started a second run rather than resuming this one")

    def test_the_plan_prints_what_the_documented_dry_run_prints(self):
        """The half of the documented command that IS deterministic, byte for byte: same input, same
        questions, same planner, no provider and no output. It is what an operator reads before spending,
        so a stage that planned something other than what it would run would be worse than no plan.
        """
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            through_stage = subprocess.run(classify.argv(context(out, plan=True)),
                                           capture_output=True, text=True)
            by_hand = subprocess.run([sys.executable, classify.SCRIPT, *hand_typed(out), "--plan"],
                                     capture_output=True, text=True)
            self.assertEqual(through_stage.returncode, 0, through_stage.stderr)
            self.assertEqual(by_hand.returncode, 0, by_hand.stderr)
            self.assertEqual(through_stage.stdout, by_hand.stdout)
            self.assertEqual(json.loads(through_stage.stdout)["titles"], 1)
            self.assertEqual(sorted(os.listdir(out)), ["articles.jsonl", "enriched"],
                             "a plan wrote something")

    def test_a_plan_is_reported_as_a_plan_rather_than_as_a_shard(self):
        """`run` returns what the stage made, and a dry run made nothing. Returning the shard's path would
        put a file that does not exist in the run's own log line."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            made = classify.run(context(out, plan=True))
            self.assertNotIn(SHARD, os.listdir(out))
            self.assertIn("nothing bought", made)

    def test_a_pass_that_left_no_manifest_is_a_refusal(self):
        """Checked directly: a second run against a shard whose manifest is gone refuses for the pass's own
        reason — the rows name a run id nothing declares — and never reaches this guard. The failure this
        one is for is an output that landed somewhere the declaration does not describe, which leaves the
        rows and the provenance the auditor reads them with in two different directories.
        """
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            finished_pass(out)
            os.remove(os.path.join(out, SHARD + ".manifest.json"))
            with self.assertRaises(StageError) as refused:
                classify.check_outputs(context(out))
            self.assertIn("run_combined.py", str(refused.exception))


class Topology(unittest.TestCase):
    def test_the_pass_shards_are_owned_by_the_stage_that_writes_them(self):
        """`combined` used to name `run_combined.py` on the artifact — a field that could name one script
        while the stage ran another. The stage that declares it in `OUTPUTS` is the answer now."""
        self.assertEqual(artifacts.COMBINED.producer, "")
        self.assertEqual(artifacts.COMBINED_MANIFEST.producer, "")
        registered = pipeline.producers()
        self.assertEqual(registered["combined"], (classify.PRODUCER, classify.HOW, True))
        self.assertEqual(registered["combined_manifest"], (classify.PRODUCER, classify.HOW, True))

    def test_the_stage_runs_the_script_it_is_registered_as(self):
        self.assertEqual(classify.SCRIPT, os.path.join(REPO, classify.PRODUCER))
        self.assertTrue(os.path.isfile(classify.SCRIPT))

    def test_both_of_its_inputs_are_owned_by_the_stages_that_write_them(self):
        """The seam is CLOSED on both. Each was an artifact answering for itself because no stage wrote
        it; the enrichment drain and the article dump landed, and the ownership moved with them, so the
        registry names a stage rather than a script an operator would have to find.

        Kept as an assertion rather than deleted: an artifact going back to naming a producer would mean
        a stage stopped running what it claims to, and the derived registry is what would notice."""
        self.assertEqual(artifacts.ENRICHED.producer, "")
        self.assertEqual(artifacts.ARTICLES.producer, "")
        self.assertEqual(pipeline.producers()["enriched"][0], fetch.PRODUCER)
        self.assertEqual(pipeline.producers()["articles"][0], "pipeline/articles.py")

    def test_the_delta_pass_is_a_different_rule_and_has_its_own_stage(self):
        """`delta` is a second pass run by a second script. This stage writes `combined` and not the delta,
        so the delta is registered against the critique stage and the script it runs, not swept into a
        registration that would name the wrong file."""
        self.assertEqual(artifacts.DELTA.producer, "")
        self.assertNotIn(artifacts.DELTA, [bind(e).artifact for e in classify.OUTPUTS])
        self.assertEqual(pipeline.producers()["delta"][0], "pipeline/run_delta.py")

    def test_the_titles_are_classified_before_the_corpus_joins_them(self):
        self.assertLess(pipeline.STAGES.index("classify"), pipeline.STAGES.index("corpus"))


if __name__ == "__main__":
    unittest.main()
