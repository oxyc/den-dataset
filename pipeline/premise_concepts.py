#!/usr/bin/env python3
"""Canonical premise concepts from free-form premise tags (oxyc/den-dataset#15).

The raw tags remain the embedding document.  This module builds the separate, discrete view used by
filters, counts and explanations:

1. embed every distinct surface form with the same canary-checked den-embed as the corpus;
2. retrieve a small set of more-established candidate concepts with deterministic sign-LSH;
3. let Jev choose one candidate or ``other``; and
4. resolve the resulting acyclic surface -> concept map.

The candidate order is deliberately acyclic: a form may point only to one with greater corpus frequency,
or to an earlier equally frequent form.  That makes a resumed/adjudicated map deterministic and lets the
materialiser collapse chains without inventing a tie-break after the model has answered.
"""
from collections import Counter, defaultdict
import hashlib
import json
import math
import mmap
import os
import struct


SCHEMA = "premise-concepts-v1"
VECTOR_SCHEMA = "premise-surface-vectors-v1"


def source_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def surface_forms(path, min_count=1):
    """Sorted ``[{form, count}]`` from a premise-tags artifact, refusing malformed tag values."""
    with open(path, encoding="utf-8") as fh:
        tags = json.load(fh).get("tags")
    if not isinstance(tags, dict):
        raise ValueError(f"{path}: tags is not an object")
    counts = Counter()
    for key, values in tags.items():
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise ValueError(f"{path}: {key} has malformed tags")
        counts.update(values)
    return [{"form": form, "count": counts[form]} for form in sorted(counts) if counts[form] >= min_count]


def write_json(path, value):
    """Atomic JSON checkpoint: a kill leaves either the old complete file or the new complete file."""
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        fh.write("\n")
    os.replace(temporary, path)


def initialise_vectors(tags_path, work, dims, embedding_space, min_count=2):
    """Create or validate the vector work set and return ``(forms, completed rows)``."""
    os.makedirs(work, exist_ok=True)
    forms_path = os.path.join(work, "forms.json")
    meta_path = os.path.join(work, "vectors.meta.json")
    vector_path = os.path.join(work, "vectors.i8")
    digest = source_digest(tags_path)
    if not os.path.exists(forms_path):
        write_json(forms_path, surface_forms(tags_path, min_count))
    with open(forms_path, encoding="utf-8") as fh:
        forms = json.load(fh)
    expected = {"schema": VECTOR_SCHEMA, "sourceSha256": digest, "minimumCount": min_count, "dims": dims,
                "count": len(forms), "embeddingSpace": embedding_space}
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        if any(meta.get(key) != value for key, value in expected.items()):
            raise ValueError(f"{work}: vector metadata describes another source or embedding space")
    else:
        write_json(meta_path, {**expected, "completed": 0})
    size = os.path.getsize(vector_path) if os.path.exists(vector_path) else 0
    if size % dims:
        raise ValueError(f"{vector_path}: {size} bytes is not a whole number of {dims}-dimensional rows")
    completed = size // dims
    if completed > len(forms):
        raise ValueError(f"{vector_path}: {completed} rows for only {len(forms)} forms")
    return forms, completed


def append_vectors(path, vectors, dims):
    with open(path, "ab") as fh:
        for vector in vectors:
            if len(vector) != dims or any(isinstance(v, bool) or not isinstance(v, int) or not -128 <= v <= 127
                                          for v in vector):
                raise ValueError("den-embed returned a malformed int8 vector")
            fh.write(bytes(v & 0xff for v in vector))
        fh.flush()
        os.fsync(fh.fileno())


def _positions(dims, width=64):
    """Stable dimension sample. Hash ordering avoids privileging the vector's first coordinates."""
    return sorted(range(dims), key=lambda i: hashlib.sha256(f"premise-lsh:{i}".encode()).digest())[:width]


def _signature(row, positions):
    signature = 0
    for bit, position in enumerate(positions):
        if row[position] >= 0:
            signature |= 1 << bit
    return signature


def _band(signature, table, bits, width):
    """A deterministic, overlapping subset of the signature for one LSH table."""
    start = (table * 17) % width
    key = 0
    for offset in range(bits):
        position = (start + offset * 11) % width
        key |= ((signature >> position) & 1) << offset
    return key


def _precedence(item):
    return (-item["count"], len(item["form"]), item["form"])


def _cosine(a, b, norm_a=None, norm_b=None):
    norm_a = norm_a or math.sqrt(sum(x * x for x in a))
    norm_b = norm_b or math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b) if norm_a and norm_b else 0.0


def candidate_rows(forms, vector_path, dims, min_count=2, source_min_count=2, options=6, threshold=0.92,
                   tables=16, bits=10, probe=12):
    """Yield candidate rows without an O(n²) scan.

    LSH uses 64 sampled sign bits only to retrieve and rank a probe set.  The final threshold and ordering
    use exact cosine over all dimensions.  Missing a bucket therefore loses recall but cannot create a
    false semantic match; Jev makes the final bounded decision.
    """
    if not (1 <= bits <= 20 and 1 <= probe and 1 <= options <= probe):
        raise ValueError("invalid LSH/options configuration")
    expected = len(forms) * dims
    if os.path.getsize(vector_path) != expected:
        raise ValueError(f"{vector_path}: expected {expected} bytes for {len(forms)} rows")
    positions = _positions(dims)
    with open(vector_path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        vectors = memoryview(raw).cast("b")
        signatures = []
        canonical = []
        buckets = [defaultdict(list) for _ in range(tables)]
        norms = {}
        for index, item in enumerate(forms):
            row = vectors[index * dims:(index + 1) * dims]
            signature = _signature(row, positions)
            signatures.append(signature)
            if item["count"] >= min_count:
                canonical.append(index)
                norms[index] = math.sqrt(sum(x * x for x in row))
                for table in range(tables):
                    buckets[table][_band(signature, table, bits, len(positions))].append(index)

        order = [_precedence(item) for item in forms]
        for index, item in enumerate(forms):
            # The discrete use starts with the reusable head. Singleton forms stay untouched in the raw
            # embedding document and map to themselves when materialised; adjudicating them would spend
            # roughly ten times as much while creating no reusable chip or count on their own.
            if item["count"] < source_min_count:
                continue
            possible = set()
            signature = signatures[index]
            for table in range(tables):
                possible.update(buckets[table].get(_band(signature, table, bits, len(positions)), ()))
            possible.discard(index)
            possible = [other for other in possible if order[other] < order[index]]
            possible.sort(key=lambda other: ((signature ^ signatures[other]).bit_count(), order[other]))
            row = vectors[index * dims:(index + 1) * dims]
            norm = math.sqrt(sum(x * x for x in row))
            scored = []
            candidate = None
            for other in possible[:probe]:
                candidate = vectors[other * dims:(other + 1) * dims]
                cosine = _cosine(row, candidate, norm, norms[other])
                if cosine >= threshold:
                    scored.append((cosine, other))
            scored.sort(key=lambda pair: (-pair[0], order[pair[1]]))
            if scored:
                yield {"form": item["form"], "count": item["count"],
                       "candidates": [{"concept": forms[other]["form"],
                                       "count": forms[other]["count"], "cosine": round(cosine, 6)}
                                      for cosine, other in scored[:options]]}
        # Sliced memoryviews keep the mmap exported; release the last loop bindings before closing it.
        del row
        del candidate
        vectors.release()


def choice_question(candidates):
    criteria = {f"c{i}": candidate["concept"] for i, candidate in enumerate(candidates)}
    criteria["other"] = "None of these is the same underlying premise concept."
    return {"canonical": {"type": "choice",
                          "instructions": ("Choose a candidate only when it expresses the same premise at "
                                           "the same specificity and could replace the surface form in a "
                                           "filter or explanation without adding, removing, or changing a "
                                           "participant role, relationship, setting, cause, mechanism, "
                                           "outcome, or other material qualifier. Word-order, grammatical, "
                                           "and transparent synonym variants are the intended matches. A "
                                           "broader, narrower, or merely related concept is other."),
                          "criteria": criteria}}


def candidates_digest(candidates):
    raw = json.dumps(candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_digest(record):
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_choice(answer, candidates):
    allowed = {f"c{i}" for i in range(len(candidates))} | {"other"}
    if not isinstance(answer, dict) or answer.get("choice") not in allowed:
        raise ValueError("Jev returned an invalid canonical Choice")
    choice = answer["choice"]
    return None if choice == "other" else candidates[int(choice[1:])]["concept"]


def verification_question():
    return {"relation": {"type": "choice",
                         "instructions": ("Classify the relation between the surface premise tag and the "
                                          "proposed canonical concept. Choose exact only if they are "
                                          "interchangeable at the same specificity, with every participant "
                                          "role, relationship, setting, cause, mechanism, outcome, and other "
                                          "material qualifier preserved."),
                         "criteria": {
                             "exact": "Same premise concept at the same specificity.",
                             "surface-broader": "The surface form is broader than the proposed concept.",
                             "surface-narrower": "The surface form is narrower than the proposed concept.",
                             "roles-or-direction-differ": "A participant role, relationship, or direction differs.",
                             "related-not-same": "Related, but not interchangeable for another reason.",
                         }}}


def validate_verification(answer):
    allowed = set(verification_question()["relation"]["criteria"])
    if not isinstance(answer, dict) or answer.get("choice") not in allowed:
        raise ValueError("Jev returned an invalid verification Choice")
    return answer["choice"]


def materialise(forms, decisions, source_sha256, model):
    """Resolve decision chains and return the persisted surface -> concept artifact."""
    known = {item["form"] for item in forms}
    direct = {}
    for form, concept in decisions.items():
        if form not in known or concept not in known:
            raise ValueError(f"decision names an unknown form: {form!r} -> {concept!r}")
        direct[form] = concept
    resolved = {}

    def resolve(form):
        trail, current = set(), form
        while current in direct and direct[current] != current:
            if current in trail:
                raise ValueError(f"canonical concept cycle at {current!r}")
            trail.add(current)
            current = direct[current]
        return current

    for item in forms:
        resolved[item["form"]] = resolve(item["form"])
    concepts = Counter()
    for item in forms:
        concepts[resolved[item["form"]]] += item["count"]
    return {"schema": SCHEMA, "sourceSha256": source_sha256, "model": model,
            "surfaceForms": len(forms), "concepts": len(concepts),
            "map": resolved, "conceptOccurrences": dict(sorted(concepts.items()))}


def concept_title_counts(tags_path, resolved):
    """Count titles per concept, de-duplicating forms that collapse inside one title."""
    with open(tags_path, encoding="utf-8") as fh:
        tags = json.load(fh).get("tags")
    if not isinstance(tags, dict):
        raise ValueError(f"{tags_path}: tags is not an object")
    counts = Counter()
    for key, values in tags.items():
        if not isinstance(values, list):
            raise ValueError(f"{tags_path}: {key} has malformed tags")
        counts.update({resolved[value] for value in values})
    return dict(sorted(counts.items()))
