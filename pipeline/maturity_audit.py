"""The immutable evidence needed to reproduce one generation's maturity observations.

The served store and ordinary corpus inputs do not need raw classification registers. A publish instead
puts them in one compressed, version-bound audit asset beside the corpus bundle. This keeps release pages
small while preserving the exact bytes after an authority changes its live feed.
"""
import gzip
import hashlib
import io
import json
import os
import tarfile

from . import artifacts, maturity, maturity_decisions, maturity_providers
from .contract import StageError


ASSET = "maturity-audit.tar.gz"
RECORD = "audit.json"
SOURCES = "maturity-sources.json"
DERIVED = "maturity.json"
DECISIONS = "maturity-authority-decisions.json"


def _digest(payload):
    return hashlib.sha256(payload).hexdigest()


def _read(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise StageError(f"maturity audit: cannot read {path}: {error}") from None


def _json(payload, name):
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, ValueError) as error:
        raise StageError(f"maturity audit: {name} is not JSON: {error}") from None


def _files(out_dir, version):
    """Return the named bytes that belong in the archive, refusing incomplete source evidence."""
    sources_payload = _read(os.path.join(out_dir, artifacts.MATURITY_SOURCES.filename))
    derived_payload = _read(os.path.join(out_dir, artifacts.MATURITY.filename.format(version=version)))
    decisions_payload = _read(maturity_decisions.PATH)
    sources = _json(sources_payload, SOURCES)
    rows = sources.get("sources") if isinstance(sources, dict) else None
    if not isinstance(rows, list):
        raise StageError("maturity audit: maturity-sources.json has no sources list")

    files = {SOURCES: sources_payload, DERIVED: derived_payload, DECISIONS: decisions_payload}
    seen = set()
    for number, row in enumerate(rows, 1):
        authority = row.get("authority") if isinstance(row, dict) else None
        provider = maturity_providers.BY_AUTHORITY.get(authority)
        if provider is None or authority in seen:
            raise StageError(f"maturity audit: source {number} names an unknown or duplicate authority")
        seen.add(authority)
        payload = _read(os.path.join(out_dir, provider.artifact.filename))
        if row.get("sha256") != _digest(payload) or row.get("bytes") != len(payload):
            raise StageError(f"maturity audit: {authority} snapshot does not match maturity-sources.json")
        name = f"sources/{os.path.basename(provider.artifact.filename)}"
        if name in files:
            raise StageError(f"maturity audit: duplicate archive name {name}")
        files[name] = payload
    return files


def _verify(files, facts_payload, expected_version=None):
    """Rebuild the derived observations from retained bytes and return the archive record."""
    required = {RECORD, SOURCES, DERIVED, DECISIONS}
    missing = sorted(required - set(files))
    if missing:
        raise StageError(f"maturity audit: archive has no {', '.join(missing)}")
    record = _json(files[RECORD], RECORD)
    if (not isinstance(record, dict) or set(record) != {"schema", "datasetVersion", "files"}
            or type(record.get("schema")) is not int or record["schema"] != 1):
        raise StageError("maturity audit: audit.json must have schema 1")
    version = record.get("datasetVersion") if isinstance(record, dict) else None
    if not isinstance(version, str) or not version or (expected_version and version != expected_version):
        raise StageError("maturity audit: archive names the wrong dataset version")
    recorded = record.get("files")
    actual_names = set(files) - {RECORD}
    if not isinstance(recorded, dict) or set(recorded) != actual_names:
        raise StageError("maturity audit: audit.json does not name exactly the retained files")
    for name, digest in recorded.items():
        if not isinstance(digest, str) or _digest(files[name]) != digest:
            raise StageError(f"maturity audit: {name} does not match audit.json")

    sources = _json(files[SOURCES], SOURCES)
    derived = _json(files[DERIVED], DERIVED)
    facts = _json(facts_payload, "facts.json")
    if not isinstance(sources, dict) or sources.get("schema") != 1:
        raise StageError("maturity audit: maturity-sources.json must have schema 1")
    if not isinstance(derived, dict) or derived.get("schema") != 1:
        raise StageError("maturity audit: maturity.json must have schema 1")
    decisions, decisions_digest = maturity_decisions.load_payload(files[DECISIONS])
    if derived.get("datasetVersion") != version:
        raise StageError("maturity audit: maturity.json names the wrong dataset version")
    derivation = derived.get("derivation") or {}
    if derivation.get("authorityDecisionsSha256") != decisions_digest:
        raise StageError("maturity audit: the retained authority decisions have the wrong digest")
    maturity_decisions.validate_targets(decisions, facts)

    source_rows = sources.get("sources") if isinstance(sources, dict) else None
    if not isinstance(source_rows, list):
        raise StageError("maturity audit: maturity-sources.json has no sources list")
    by_authority = {}
    for row in source_rows:
        authority = row.get("authority") if isinstance(row, dict) else None
        provider = maturity_providers.BY_AUTHORITY.get(authority)
        if provider is None or authority in by_authority:
            raise StageError("maturity audit: source manifest names an unknown or duplicate authority")
        name = f"sources/{os.path.basename(provider.artifact.filename)}"
        payload = files.get(name)
        if payload is None or row.get("sha256") != _digest(payload) or row.get("bytes") != len(payload):
            raise StageError(f"maturity audit: retained {authority} snapshot does not match its manifest")
        parsed = list(provider.records(payload))
        maturity_decisions.validate_source(decisions, authority, parsed)
        by_authority[authority] = parsed
    expected_sources = derivation.get("sourceSha256")
    manifest_sources = {row["authority"]: row["sha256"] for row in source_rows}
    if expected_sources != manifest_sources:
        raise StageError("maturity audit: maturity.json does not bind the retained source manifest")

    linked = [maturity.link_records(by_authority.get(provider.authority, ()), facts, decisions)[0]
              for provider in maturity_providers.PROVIDERS]
    rebuilt, _conflicts = maturity.combine(linked)
    if rebuilt != derived.get("records"):
        raise StageError("maturity audit: retained inputs do not reproduce maturity.json observations")
    return record


def build(out_dir, destination, version):
    """Validate, package, and verify one generation's audit evidence."""
    files = _files(out_dir, version)
    record = {"schema": 1, "datasetVersion": version,
              "files": {name: _digest(payload) for name, payload in sorted(files.items())}}
    files[RECORD] = (json.dumps(record, indent=1, sort_keys=True) + "\n").encode()
    facts_payload = _read(os.path.join(out_dir, artifacts.FACTS.filename.format(version=version)))
    _verify(files, facts_payload, expected_version=version)

    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    with open(destination, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, payload in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    info.mode = 0o644
                    info.mtime = 0
                    archive.addfile(info, io.BytesIO(payload))
    verify(destination, facts_payload, expected_version=version)
    return record


def verify(path, facts_payload, expected_version=None):
    """Verify hashes and reproduce observations directly from an audit archive."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            files = {}
            for member in archive.getmembers():
                if not member.isfile() or member.name.startswith("/") or ".." in member.name.split("/"):
                    raise StageError("maturity audit: archive contains an unsafe member")
                if member.name in files:
                    raise StageError(f"maturity audit: archive repeats {member.name}")
                files[member.name] = archive.extractfile(member).read()
    except (OSError, tarfile.TarError) as error:
        raise StageError(f"maturity audit: cannot read archive: {error}") from None
    return _verify(files, facts_payload, expected_version=expected_version)
