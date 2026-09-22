#!/usr/bin/env python3
"""The publish stage — that it is the publisher's invocation, and nothing else.

`scripts/publish-dataset.sh` clobbers a public, moving release, and every guard standing in front of that
— the store-identity guard, the ownership guard, the manifest prune, the grounding ratchet, the per-asset
upload retries — was bought by a failure that had already shipped. So the stage adds nothing: it resolves
no input, re-checks no hash and pre-empts no refusal. The script already refuses a missing manifest ("run
finalize first") and a missing store (naming the writer that builds it), and a wrapper answering first
would be a second guard, free to drift from the one that matters.

That leaves no oracle of the kind the corpus and store stages had. Those wrap a writer, so "the stage
writes the bytes the hand-typed command writes" settles it; this one's output is a release, and there are
no bytes to compare. What can be pinned is the INVOCATION, and three parts of it are both load-bearing and
easy to get wrong:

  * the WORKING DIRECTORY. The ownership guard resolves producer paths (`Sources/…`, `scripts/…`) and
    `git ls-files` against it, so the script only works from the repo root. The stage runs it there
    whatever directory `den` was typed in — and therefore hands it an ABSOLUTE publish dir, or moving the
    invocation would move which directory gets published.
  * the ENVIRONMENT. Every override is a variable (`DEN_STORE_REBUILD`, `DEN_ALLOW_DROPPING_BLOBS`,
    `DEN_ALLOW_SHARED_PLOTS`, `DEN_ALLOW_STALE_STORE_INPUTS`) and so is the repo published to
    (`DEN_DATASET_REPO`). A stage that built its own environment would drop a stated rebuild — or publish
    to oxyc/den-dataset from a run that named somewhere else.
  * the EXIT CODE. A guard that stops firing because a wrapper swallowed a non-zero exit is the whole risk
    of this port.

The guards themselves are tested where they live, and `scripts/publish-dataset.test.sh` runs all of its
cases a second time through `den stage publish` (`DEN_PUBLISH_VIA=stage`). That, rather than anything
here, is the evidence that a refusal through the stage is the refusal through the script.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pipeline

from . import artifacts, publish, store
from .contract import Context, StageError, bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Stands in for the publisher and records how it was called. Exits with the code in `<self>.exit` when
#: that file is there, so a refusal can be staged without a release to refuse.
RECORDER = '''#!/usr/bin/env python3
import json, os, sys
with open(sys.argv[0] + ".json", "w") as fh:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}, fh)
code = sys.argv[0] + ".exit"
sys.exit(int(open(code).read()) if os.path.exists(code) else 0)
'''


def recorder(directory, exit_code=0):
    """A recorder in `directory`, executable, armed to exit with `exit_code`."""
    path = os.path.join(directory, "recorder.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(RECORDER)
    os.chmod(path, 0o755)
    if exit_code:
        with open(path + ".exit", "w", encoding="utf-8") as fh:
            fh.write(str(exit_code))
    return path


def recording(path):
    """What the recorder was handed, and then forget it — so two runs cannot read as one."""
    with open(path + ".json", encoding="utf-8") as fh:
        seen = json.load(fh)
    os.remove(path + ".json")
    return seen


class Declaration(unittest.TestCase):
    def test_the_output_is_the_release_and_the_release_is_not_a_file(self):
        """What `OUTPUTS` means for a stage whose effect is an upload.

        Every other artifact in the catalogue is a file in the out-dir. This one is the moving
        `data-latest` release, and declaring it as a filename would put a path in the out-dir that the
        stage never writes and a later reader would try to open. So it says it is remote, and asking for
        its path is refused the way asking a shard set for one path is.
        """
        self.assertEqual([bind(e).name for e in publish.OUTPUTS], ["release"])
        self.assertTrue(artifacts.RELEASE.remote)
        with self.assertRaises(StageError) as refused:
            Context(out_dir="out", dataset_version="v9").path(artifacts.RELEASE)
        self.assertIn("release", str(refused.exception))

    def test_the_release_is_owned_by_the_stage_that_publishes_it(self):
        """The same ownership-from-order the corpus proved, at the end of the pipeline rather than in the
        middle: the artifact carries no producer, and the registry answers with the rule the stage runs."""
        self.assertEqual(artifacts.RELEASE.producer, "")
        self.assertEqual(pipeline.producers()["release"], (publish.PRODUCER, publish.HOW, True))

    def test_the_store_it_publishes_is_the_store_stage_s(self):
        """The handover. `store` is declared here as an input and nowhere as a second producer, so what
        the publisher uploads is what the writer wrote."""
        self.assertIn(artifacts.STORE, [bind(e).artifact for e in publish.INPUTS])
        self.assertEqual(pipeline.producers()["store"], (store.PRODUCER, store.HOW, True))

    def test_the_manifest_is_the_finalize_stages(self):
        """The publisher rewrites `dataset.meta.json` (the prune, the counts, `storeRebuild`) but does not
        create it, so a refusal for a missing one has to name finalize rather than name the publisher."""
        self.assertIn(artifacts.MANIFEST, [bind(e).artifact for e in publish.INPUTS])
        self.assertEqual(artifacts.MANIFEST.producer, "")
        self.assertEqual(pipeline.producers()["manifest"][0], "pipeline/finalize.py")

    def test_publishing_is_the_last_thing_that_happens(self):
        self.assertEqual(pipeline.STAGES[-1], "publish")
        self.assertLess(pipeline.STAGES.index("store"), pipeline.STAGES.index("publish"))


class CommandLine(unittest.TestCase):
    def test_the_stage_runs_the_script_it_is_registered_as(self):
        """One spelling, and for this stage the spelling is the whole command — there are no flags to get
        wrong, only which file gets to clobber the release."""
        self.assertEqual(publish.SCRIPT, os.path.join(REPO, publish.PRODUCER))
        self.assertTrue(os.access(publish.SCRIPT, os.X_OK), "the publisher is run by its shebang")
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", publish.PRODUCER],
                                 cwd=REPO, capture_output=True, text=True)
        self.assertEqual(tracked.returncode, 0, "the publisher is not tracked by git")

    def test_the_scripts_own_usage_line_is_the_command_this_stage_builds(self):
        """Against the script's documentation of itself, the way the corpus stage is held to
        `consolidate_corpus.py`'s usage example — which had drifted, and was caught by running it. A
        publisher that grew a second positional would still run here, and would publish the wrong dir.
        """
        with open(publish.SCRIPT, encoding="utf-8") as fh:
            usage = [line.strip(" #\n") for line in fh if "[OUT_DIR]" in line]
        self.assertEqual(len(usage), 1, f"the publisher documents itself {len(usage)} times")
        self.assertTrue(usage[0].startswith(f"{publish.PRODUCER} [OUT_DIR]"), usage[0])

    def test_the_command_is_the_publisher_and_the_directory_to_publish(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(publish.argv(Context(out_dir=out, dataset_version="v9")),
                             [publish.SCRIPT, out])

    def test_the_publish_dir_is_made_absolute(self):
        """It is resolved before the working directory changes to the repo root, so `--out-dir out` means
        the operator's `out` and not the repo's."""
        with tempfile.TemporaryDirectory() as elsewhere:
            here = os.getcwd()
            os.chdir(elsewhere)
            try:
                command = publish.argv(Context(out_dir="out", dataset_version="v9"))
            finally:
                os.chdir(here)
            self.assertEqual(command[-1], os.path.join(os.path.realpath(elsewhere), "out"))


class Invocation(unittest.TestCase):
    """The stage's invocation against the documented one, recorded on both sides.

    There is no artifact to diff, so the equivalence proved here is of the call itself: argv, working
    directory and environment, from a `den` invoked somewhere other than the repo root — which is the only
    place the three can disagree.
    """

    def documented(self, script, publish_dir):
        """`scripts/publish-dataset.sh <dir>`, run from the repo root, as `docs/OPERATE.md` step 8 says.

        The publisher itself is swapped for the recorder: running the real one would clobber a public
        release, which is the one thing no test in this repo may do.
        """
        done = subprocess.run([script, publish_dir], cwd=REPO)
        self.assertEqual(done.returncode, 0)
        return recording(script)

    def through_the_stage(self, script, out_dir, cwd):
        """`den stage publish --out-dir <out_dir>`, typed in `cwd`."""
        here = os.getcwd()
        os.chdir(cwd)
        try:
            with mock.patch.object(publish, "SCRIPT", script):
                published = publish.run(Context(out_dir=out_dir, dataset_version="v9"))
        finally:
            os.chdir(here)
        return recording(script), published

    def test_the_stage_makes_the_call_the_documented_command_makes(self):
        """Identical recordings, from a stage invoked outside the repo. Every part matters: the argument
        is the directory named, the working directory is the repo root the ownership guard needs, and the
        environment is the operator's — where every override to every guard lives.
        """
        with tempfile.TemporaryDirectory() as elsewhere:
            script = recorder(elsewhere)
            out = os.path.join(os.path.realpath(elsewhere), "out")
            os.makedirs(out)
            by_hand = self.documented(script, out)
            by_stage, published = self.through_the_stage(script, "out", elsewhere)
            self.assertEqual(by_stage, by_hand)
            self.assertEqual(by_stage["cwd"], os.path.realpath(REPO))
            self.assertEqual(by_stage["argv"], [out])
            self.assertEqual(published, "data-latest")

    def test_the_overrides_reach_the_guards(self):
        """Each of these is how an operator says a refusal is deliberate, and `DEN_DATASET_REPO` decides
        which release is clobbered. They arrive because the environment is inherited rather than rebuilt,
        and a stage that rebuilt it would turn a stated rebuild back into a refusal — or publish to the
        default repo from a run that named another."""
        passed = {"DEN_STORE_REBUILD": "the label sections gained the premise pass",
                  "DEN_ALLOW_SHARED_PLOTS": "re-grounded on purpose",
                  "DEN_DATASET_REPO": "oxyc/den-dataset-staging"}
        with tempfile.TemporaryDirectory() as elsewhere:
            script = recorder(elsewhere)
            with mock.patch.dict(os.environ, passed):
                seen, _ = self.through_the_stage(script, elsewhere, elsewhere)
        for name, value in passed.items():
            self.assertEqual(seen["env"].get(name), value)


class Refusal(unittest.TestCase):
    def run_with_exit(self, code):
        with tempfile.TemporaryDirectory() as out:
            script = recorder(out, exit_code=code)
            with mock.patch.object(publish, "SCRIPT", script):
                return publish.run(Context(out_dir=out, dataset_version="v9"))

    def test_a_refusal_from_the_publisher_is_a_refusal_from_the_stage(self):
        """The whole risk of this port in one assertion. Every guard in the publisher refuses by exiting
        non-zero; a wrapper that returned anyway would report a publish that never happened, and the next
        thing to read the release would be den-atlas."""
        for code in (1, 2, 127):
            with self.assertRaises(StageError, msg=f"exit {code} was swallowed") as refused:
                self.run_with_exit(code)
            self.assertIn(str(code), str(refused.exception))
            self.assertIn(publish.PRODUCER, str(refused.exception))

    def test_a_clean_run_names_the_release(self):
        self.assertEqual(self.run_with_exit(0), artifacts.RELEASE.filename)


if __name__ == "__main__":
    unittest.main()
