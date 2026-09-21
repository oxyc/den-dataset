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
    noticed. There is no second list now: the registry is read off the artifacts the stages declare.

That is what "derivable" buys. A registry nothing executes is a copy waiting to rot; a registry derived
from the declaration a stage runs on cannot say something the run does not.
"""
import dataclasses
import importlib
import os

#: The repo root, so a stage can name a producer the way git and the publisher do — repo-relative, from
#: the working directory the publish runs in.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class StageError(Exception):
    """A stage could not run. Its message names what is missing and the command that builds it."""


@dataclasses.dataclass(frozen=True)
class Artifact:
    """One file a stage reads or writes, and the answer to "what builds this?".

    `producer`/`how`/`dedicated` are the registration `check-producers.py` asks for, declared HERE — at
    the one place the artifact is named — rather than copied into a registry beside it. `dedicated` is
    false for a producer that builds several artifacts (`taxonomy-backfill` builds four), because the
    staleness warning is only meaningful for a producer edited FOR this artifact.
    """

    #: What the stage that reads it calls it. For a store input this is the writer's argument name, so
    #: `vector_labels` becomes `--vector-labels`.
    name: str
    #: The name it is written under, with `{version}` for the dataset version.
    filename: str
    #: Repo-relative path of the committed file whose rule builds it.
    producer: str
    #: The command that rebuilds it, quoted in the refusal when it is missing or stale.
    how: str
    dedicated: bool = True
    #: The `dataset.meta.json` key that declares it, for the artifacts a publish announces.
    manifest_key: str = ""
    #: False for an input a stage can run without.
    required: bool = True

    def flag(self):
        """The command-line flag that names it."""
        return "--" + self.name.replace("_", "-")

    def glob(self):
        """The filename with the version wild — how a publish dir is searched for it."""
        return self.filename.format(version="*")

    def registration(self):
        """`(producer, how, dedicated)` — the shape `check-producers.py` registers."""
        return (self.producer, self.how, self.dedicated)


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

    def path(self, artifact):
        if artifact.name in self.overrides:
            return self.overrides[artifact.name]
        return os.path.join(self.out_dir, artifact.filename.format(version=self.dataset_version))

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
        raise StageError(f"{artifact.name}: {path} is missing. Build it with: {artifact.how}")


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
    for attribute in ("NAME", "INPUTS", "OUTPUTS", "run"):
        if not hasattr(module, attribute):
            raise StageError(f"stage {name}: {module.__name__} declares no {attribute}")
    if module.NAME != name:
        raise StageError(f"stage {name}: {module.__name__} calls itself {module.NAME!r}")
    for field in ("INPUTS", "OUTPUTS"):
        for artifact in getattr(module, field):
            if not isinstance(artifact, Artifact):
                raise StageError(f"stage {name}: {field} holds {artifact!r}, which is not an Artifact")
    if not module.OUTPUTS:
        raise StageError(f"stage {name}: declares no OUTPUTS, so nothing downstream can name what it made")
    if not callable(module.run):
        raise StageError(f"stage {name}: run is not callable")
    return module


def registry(artifacts):
    """`{name: (producer, how, dedicated)}` for a run of artifacts — the derived producer registry.

    A name declared twice with two different producers is refused rather than resolved. Two spellings of
    one artifact is how a registry drifts, and picking one silently is how the drift survives.
    """
    out = {}
    for artifact in artifacts:
        seen = out.get(artifact.name)
        if seen is not None and seen != artifact.registration():
            raise StageError(
                f"artifact {artifact.name} is declared twice with different producers: {seen} and "
                f"{artifact.registration()}. Declare it once and let both stages reference it.")
        out[artifact.name] = artifact.registration()
    return out
