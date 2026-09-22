#!/usr/bin/env python3
"""What a stage declares, and what the declaration is good for.

A stage is a module under `pipeline/` that names three things: the artifacts it READS (`INPUTS`), the
artifacts it WRITES (`OUTPUTS`), and a `run(ctx)`. `pipeline/__init__.py` holds the order.

The declaration is not documentation. Two things are built out of it, and both break loudly when it is
wrong:

  * the stage's own command line — `pipeline/store.py` turns each declared input into the flag that
    names it, so a declaration that has drifted from the writer fails at the argument parser;
  * the producer registry in `scripts/check-producers.py`, which used to be a second list kept by hand.
    That list drifted twice in one day — `metadata` stayed registered as a store input after the writer
    stopped reading it, then `enriched` did the same — and each time a test was the only thing that
    noticed. There is no second list now: the registry is read off the stages themselves.

That is what "derivable" buys. A registry nothing executes is a copy waiting to rot; a registry derived
from the declaration a stage runs on cannot say something the run does not.

Three shapes here exist because a stage needed them, in the order the stages landed:

  * a **shard set** (`Artifact.shards`) — one flag the reader takes many times. `--combined` names three
    files, and the eleven titles that went missing for a day went missing because a producer read one
    shard of three. A stage cannot make that mistake with a set it does not know as a file.
  * a **binding** (`Artifact.called`) — the flag ONE reader uses for a file the pipeline names something
    else. `labels-t02.json` is `--vector-labels` to the store writer and `--labels` to the corpus join.
    The file keeps one name, which is what `--set` and the registry key on; the word on the command line
    belongs to whoever is reading.
  * a **remote artifact** (`Artifact.remote`) — something a stage produces that is not in the out-dir.
    The publish stage's output is the moving `data-latest` release, and `OUTPUTS` still has to name it:
    it is what the stage makes, and the registry's answer to "what publishes the dataset" is read off
    that. Giving it a filename would put a path in the out-dir that nothing ever writes, so asking for
    one is refused the way asking a shard set for one path is.
"""
import dataclasses
import glob as globbing
import importlib
import os

#: The repo root, so a stage can name a producer the way git and the publisher do — repo-relative, from
#: the working directory the publish runs in.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class StageError(Exception):
    """A stage could not run. Its message names what is missing and the command that builds it."""


@dataclasses.dataclass(frozen=True)
class Artifact:
    """One file — or one set of shards — a stage reads or writes.

    `producer`/`how`/`dedicated` are the registration `check-producers.py` asks for. `dedicated` is false
    for a producer that builds several artifacts (`taxonomy-backfill` builds four), because the staleness
    warning is only meaningful for a producer edited FOR this artifact.

    `producer`/`how` are EMPTY for an artifact this pipeline produces. The stage that declares it in
    `OUTPUTS` owns it, and names the rule it runs once, beside the code that runs it; `registry()` reads
    the ownership off the order. They are filled in only for an input whose stage is not ported yet —
    the seam, and it closes one artifact at a time as the stages land.
    """

    #: What the whole pipeline calls this file: the key `--set` takes, and the key the producer registry
    #: is built on. A reader that spells it differently on its own command line says so with `called`.
    name: str
    #: The name it is written under, with `{version}` for the dataset version. For a shard set, a glob;
    #: for a remote artifact, the name the thing carries where it lives — the release tag.
    filename: str
    #: Repo-relative path of the committed file whose rule builds it — for an artifact no stage produces.
    producer: str = ""
    #: The command that rebuilds it, quoted in the refusal when it is missing or stale.
    how: str = ""
    dedicated: bool = True
    #: The `dataset.meta.json` key that declares it, for the artifacts a publish announces.
    manifest_key: str = ""
    #: False for an input a stage can run without.
    required: bool = True
    #: True for a SET of files carried by one repeated flag — the pass shards. `filename` is then a glob
    #: and `Context.paths` resolves the whole set, so no stage can hand over one shard of three.
    shards: bool = False
    #: True for an artifact that does not live in the out-dir — the published release. It has no path,
    #: so `Context.path` refuses one rather than inventing a plausible place it is not.
    remote: bool = False

    def flag(self):
        """The command-line flag that names it."""
        return "--" + self.name.replace("_", "-")

    def glob(self):
        """The filename with the version wild — how a publish dir is searched for it."""
        return self.filename.format(version="*")

    def called(self, arg):
        """This artifact, as one stage's command line names it. See `Binding`."""
        return Binding(self, arg)


@dataclasses.dataclass(frozen=True)
class Binding:
    """An artifact, plus the argument name ONE stage's reader uses for it.

    Declared in that stage's `INPUTS`/`OUTPUTS` so the command line is still built from the declaration
    and still dies at the reader's parser when the two disagree. The artifact keeps its own name, so the
    registry, `--set` and every other stage go on seeing one file.
    """

    artifact: Artifact
    #: The reader's argument name, e.g. `labels` for the file the pipeline calls `vector_labels`.
    arg: str

    @property
    def name(self):
        """The artifact's pipeline-wide name."""
        return self.artifact.name

    def flag(self):
        return "--" + self.arg.replace("_", "-")


def bind(entry):
    """Any `INPUTS`/`OUTPUTS` entry as a binding — a bare `Artifact` is one the reader spells the same."""
    return entry if isinstance(entry, Binding) else Binding(entry, entry.name)


@dataclasses.dataclass(frozen=True)
class Context:
    """Where a run reads and writes.

    `overrides` names a path for one artifact outright, which is how a test points a stage at a fixture
    and how an operator points at an out-dir that does not follow the naming.
    """

    out_dir: str
    dataset_version: str
    overrides: dict = dataclasses.field(default_factory=dict)
    #: The manifest to declare the outputs in. Without it a store is written and nothing names it.
    stamp_meta: str = ""
    #: The title count a join is held to, when one is declared. The corpus join refuses a run that wrote
    #: a different number, which is how a missing shard stops being a quieter corpus.
    expect: int | None = None
    #: Milliseconds the embed pass idles between requests, so a long run can share a machine. den-embed is
    #: already nice 20, but nice only orders CPU contention — it does not stop bge-m3 holding its
    #: activations resident, and a pause leaves real gaps the rest of the system can reclaim memory in. The
    #: cost is linear and was priced at 15 documents a request: ~2,600 flushes over the corpus, so each
    #: 1000ms adds ~45 minutes. This stage sends 7, so the same pause costs roughly twice that.
    pause_ms: int = 0
    #: Which universe the worklist builds — `export`, `discover` or `delta`. Not defaulted: they are three
    #: different catalogues, enrichment is billed per title, and the one the command picks unasked is the
    #: pilot's 500. See `pipeline/worklist.py`.
    mode: str = ""
    #: The date a `delta` collects titles from, `YYYY-MM-DD`. Per run by construction — `delta-run.sh`
    #: computes it from how many days back the pass is looking.
    since: str = ""
    #: Stop the embed pass after this many NEW titles. ONNX Runtime's memory arena grows to its peak and
    #: never shrinks, so a long-lived den-embed creeps up until it OOMs the machine; the run is segmented
    #: to let the service be restarted between segments, and the store is what makes that free.
    limit: int | None = None
    #: Validate and report, buying nothing. The classify pass is the one stage whose cost is money rather
    #: than time — $20.47 for the shipped corpus — so its launch procedure is written around a dry run
    #: that proves the input parses and prints the call plan before anything is paid for.
    plan: bool = False
    #: Which media type the enrichment drains, when only one is wanted. Empty drains both, which is what a
    #: full run needs: a worklist holds ONE media type, so a corpus is two drains and a stage that could
    #: only do one would leave the other to be remembered.
    media: str = ""
    #: The TMDB vote count the enrichment keeps a title above, when the default is not wanted. 0 re-includes
    #: the low-vote tail for a full-catalogue pass. Below-floor ids are deliberately not checkpointed — a
    #: vote count only climbs — so a worklist full of them cannot drain at the default, and this is the
    #: knob the refusal for that sends an operator to.
    vote_floor: int | None = None

    def path(self, artifact):
        if artifact.shards:
            raise StageError(f"{artifact.name} is a set of shards, not a file — ask for paths()")
        if artifact.remote:
            raise StageError(f"{artifact.name} is published as {artifact.filename}, not written to "
                             f"{self.out_dir} — it has no path here")
        override = self.overrides.get(artifact.name)
        if isinstance(override, (list, tuple)):
            raise StageError(f"{artifact.name} was pointed at {len(override)} paths and names one file")
        if override is not None:
            return override
        return os.path.join(self.out_dir, artifact.filename.format(version=self.dataset_version))

    def paths(self, artifact):
        """Every file a shard set names, sorted.

        Resolved by the declared glob rather than by a list an operator types, so the set cannot be short
        by one — the failure that took eleven titles out of a derived blob for a day. An override may
        name the members outright, which is how a pass written under another run's name is pointed at.
        """
        override = self.overrides.get(artifact.name)
        if override is not None:
            return tuple(override) if isinstance(override, (list, tuple)) else (override,)
        pattern = os.path.join(self.out_dir, artifact.filename.format(version=self.dataset_version))
        return tuple(sorted(globbing.glob(pattern)))

    def shard(self, artifact):
        """Where ONE pass writes into a shard set.

        `path` refuses a set because no single file is the set, and a pass still has to name one file. The
        name is not a free choice: the declared glob with an empty wildcard is the shard a pass writes,
        and the members that fill the wildcard are the quarantine shards a later repair adds. Deriving it
        from the glob the readers resolve the set by is what keeps a run inside the set they will find —
        a pass sent to a name of its own is a second manifest, and a second manifest is a second paid run.
        """
        if not artifact.shards:
            raise StageError(f"{artifact.name} is one file, not a set of shards — ask for path()")
        override = self.overrides.get(artifact.name)
        if isinstance(override, (list, tuple)):
            raise StageError(f"{artifact.name} was pointed at {len(override)} paths and one pass writes "
                             f"one shard — name the shard to write, or leave it to the declaration")
        if override is not None:
            return override
        return os.path.join(self.out_dir,
                            artifact.filename.format(version=self.dataset_version).replace("*", ""))

    def require(self, artifact):
        """The input's path, or a refusal that names what builds it.

        An optional input that is not there returns None — the stage leaves its flag off. A required one
        that is not there is the end of the run, and the message is the command that would fix it: the
        thing an operator wants from a missing input is not its name, it is who owns it.
        """
        path = self.path(artifact)
        if os.path.exists(path):
            return path
        if not artifact.required:
            return None
        raise StageError(f"{artifact.name}: {path} is missing. Build it with: {how_to_build(artifact)}")

    def require_all(self, artifact):
        """Every member of a shard set, or a refusal naming what writes them."""
        found = self.paths(artifact)
        if found:
            return found
        pattern = artifact.filename.format(version=self.dataset_version)
        raise StageError(f"{artifact.name}: nothing in {self.out_dir} matches {pattern}. "
                         f"Build it with: {how_to_build(artifact)}")


def how_to_build(artifact):
    """The command that rebuilds this artifact, as the pipeline's order answers it.

    Imported at call time rather than at module scope: `pipeline/__init__.py` imports this module, so the
    loop only closes while a stage is running, by which point every stage is loaded. The lookup matters
    because an artifact a stage produces carries no `how` of its own — the owning stage does.

    An artifact the pipeline does not name falls back to its own `how`, so a stage run against a fixture
    artifact still refuses with something useful rather than with a KeyError from the message-building.
    """
    from . import producers
    registered = producers().get(artifact.name)
    return registered[1] if registered else artifact.how


def load(name):
    """A stage module by name, validated."""
    module = importlib.import_module(f"{__package__}.{name}")
    validate(module, name)
    return module


def validate(module, name):
    """Refuse a module that is in `STAGES` without being a stage.

    The list in `pipeline/__init__.py` is what a reader is promised describes the pipeline. A name in it
    pointing at a module with no contract makes the list a lie in the one direction nothing else checks.
    """
    # PUBLISHES and SPENDS are declared rather than defaulted: `den run` skips those stages unless asked,
    # and a stage that forgot to say so would be swept back into every exploratory run — uploading to a
    # moving public release, or buying a corpus from a paid provider, because nobody wrote `False`. Both
    # failures are silent and neither is undone by noticing afterwards, so the omission is a refusal.
    for attribute in ("NAME", "PRODUCER", "HOW", "INPUTS", "OUTPUTS", "PUBLISHES", "SPENDS", "run"):
        if not hasattr(module, attribute):
            raise StageError(f"stage {name}: {module.__name__} declares no {attribute}")
    if module.NAME != name:
        raise StageError(f"stage {name}: {module.__name__} calls itself {module.NAME!r}")
    for field in ("INPUTS", "OUTPUTS"):
        for entry in getattr(module, field):
            if not isinstance(entry, (Artifact, Binding)):
                raise StageError(f"stage {name}: {field} holds {entry!r}, which is not an Artifact")
    if not module.OUTPUTS:
        raise StageError(f"stage {name}: declares no OUTPUTS, so nothing downstream can name what it made")
    for gate in ("PUBLISHES", "SPENDS"):
        if not isinstance(getattr(module, gate), bool):
            raise StageError(f"stage {name}: {gate} is {getattr(module, gate)!r}, not True or False")
    if not callable(module.run):
        raise StageError(f"stage {name}: run is not callable")
    return module


def registry(modules):
    """`{name: (producer, how, dedicated)}` for a run of stages — the producer registry, from the ORDER.

    An artifact a stage OUTPUTS is owned by that stage: the rule is the script the stage runs, declared
    once beside the code that runs it, so the registry cannot name a producer the run does not use. An
    artifact only READ comes from a stage that is not ported yet and answers for itself, until that stage
    lands and the ownership moves with it.

    Two stages claiming one artifact is refused rather than resolved, and so is an artifact that both
    names a producer and is produced here: a second answer is how a registry starts disagreeing with
    itself, and picking one silently is how the disagreement survives.
    """
    out, owner = {}, {}
    for module in modules:
        for entry in module.OUTPUTS:
            artifact = bind(entry).artifact
            if artifact.producer:
                raise StageError(
                    f"artifact {artifact.name} names its own producer ({artifact.producer}) and is also "
                    f"written by stage {module.NAME}. Clear the entry in pipeline/artifacts.py: the "
                    f"stage that writes it is the answer.")
            if artifact.name in owner:
                raise StageError(f"artifact {artifact.name} is written by two stages: {owner[artifact.name]} "
                                 f"and {module.NAME}")
            owner[artifact.name] = module.NAME
            out[artifact.name] = (module.PRODUCER, module.HOW, artifact.dedicated)

    for module in modules:
        for entry in module.INPUTS:
            artifact = bind(entry).artifact
            if artifact.name in out:
                continue
            if not artifact.producer:
                raise StageError(
                    f"artifact {artifact.name} is read by stage {module.NAME}, no stage writes it, and it "
                    f"names no producer. Nothing would rebuild it when its own inputs change — which is "
                    f"how facets.bin fell 999 titles behind.")
            out[artifact.name] = (artifact.producer, artifact.how, artifact.dedicated)
    return out
