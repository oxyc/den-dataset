"""Other versions (oxyc/den-atlas#112): per title, the other screen versions of its story — remakes, and
other adaptations of the same book or play.

Derived from two Wikidata links and nothing else: P144 (based on), stated on the title, and P4969
(derivative work), stated on the source and naming the title (`derivedFrom`). Wikidata has no "remake of"
property; a remake is P144 pointing at the original film or series. Every title linked to one source is
one group, and the source itself belongs to it when it is a title here.

The rules below were measured on the published corpus (53,119 titles) against hand-judged samples; the
numbers are on oxyc/den-atlas#112. Most of the lost precision was spin-offs, not large groups:

  * A **character or a franchise** is no story. 527 links to Batman, Superman and Scooby-Doo join
    spin-offs, not versions.
  * A title that **follows** another (P155) is a sequel, not a version: *Dracula's Daughter*.
  * A **spin-off** is based on its parent show: a title sharing a P179 series or a P8345 media franchise
    with the screen work it is based on is left out (*Better Call Saul* and *Breaking Bad*).
  * Through anything but a **book or a play**, two titles are versions only when they differ in country or
    original language — a foreign remake — or share a name — a same-country remake. A spin-off is the same
    country under a new name (*Gen V*, *Wellington Paranormal*). A book's versions are not tested: *Emma*
    and *Clueless* share a country and no name.
  * A source with more than `CUTOFF` titles here contributes nothing. None does today (*A Christmas Carol*
    has 27); it stops a Bible-sized source from turning the row into noise as the corpus grows.

The relation is `remake` when the link runs through a screen work, else `source`. Links are symmetric.
The title's own curated franchise is NOT left out here: the reader does that at serve time, so a change to
the franchise derivation needs no change to this one.
"""
import itertools
import re
import unicodedata

#: A source whose every adaptation tells the story, so no remake test applies (`lib/wikidata_facts.SOURCE_KINDS`).
STORY = {"book", "play"}
#: A source that is no story at all.
NOT_A_STORY = {"character", "franchise"}
#: The most titles one source may group. Measured: the largest is 27.
CUTOFF = 40

_ARTICLE = re.compile(r"^(the|a|an|la|le|les|el|der|die|das) ")


def key_of(record):
    return f"{record['mediaType']}:{record['tmdbId']}"


def _name(text):
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    folded = re.sub(r"[^a-z0-9 ]+", " ", folded).strip()
    return re.sub(r"\s+", " ", _ARTICLE.sub("", folded)).strip()


def names(record):
    """Every name a title goes by, folded: its English and original title and its aliases."""
    titles = record.get("titles") or {}
    found = [titles.get("en"), titles.get("orig")] + list(titles.get("aliases") or [])
    return {_name(text) for text in found if text} - {""}


def series(record):
    return set(record.get("franchise") or []) | set(record.get("mediaFranchise") or [])


def remake_like(a, b):
    """Two titles linked through a screen work (or a comic, a game, ...) are versions of one story when they
    share a name, or differ in their countries or their original language."""
    if names(a) & names(b):
        return True
    countries_a, countries_b = set(a.get("countries") or []), set(b.get("countries") or [])
    if countries_a and countries_b and countries_a != countries_b:
        return True
    language_a, language_b = (a.get("languages") or [None])[0], (b.get("languages") or [None])[0]
    return bool(language_a and language_b and language_a != language_b)


def derive(records, sources):
    """Set `otherVersions` on every record that has one: `[{"key": "movie:1", "kind": "source"|"remake"}]`,
    sorted by key. `sources` is Q-id -> `{"kind": ..., "titles": [...]}`: the source work's kind and the
    TMDB keys it has itself, which make it a screen work (`pipeline/facts.version_sources`). Returns how many
    titles have a version."""
    by_key = {key_of(record): record for record in records}
    groups = {}
    for key, record in by_key.items():
        if record.get("follows"):
            continue
        for qid in set(record.get("basedOn") or []) | set(record.get("derivedFrom") or []):
            groups.setdefault(qid, set()).add(key)
    links = {}
    for qid in sorted(groups):
        # A work the facts stage did not resolve cannot be told from a character or a spin-off's parent, so
        # it groups nothing: a facts file from before `sources` existed has no other versions at all.
        source = sources.get(qid)
        if source is None:
            continue
        screen = bool(source.get("titles"))
        kind = "screen" if screen else source.get("kind")
        if kind in NOT_A_STORY:
            continue
        own = {key for key in source.get("titles") or [] if key in by_key and not by_key[key].get("follows")}
        parent = set().union(*(series(by_key[key]) for key in own))
        members = {key for key in groups[qid] if not series(by_key[key]) & parent} | own
        if len(members) < 2 or len(members) > CUTOFF:
            continue
        relation = "remake" if screen else "source"
        for a, b in itertools.combinations(sorted(members), 2):
            if kind not in STORY and not remake_like(by_key[a], by_key[b]):
                continue
            for one, other in ((a, b), (b, a)):
                if links.setdefault(one, {}).get(other) != "remake":
                    links[one][other] = relation
    for key, record in by_key.items():
        found = links.get(key)
        if found:
            record["otherVersions"] = [{"key": other, "kind": found[other]} for other in sorted(found)]
        else:
            record.pop("otherVersions", None)
    return len(links)
