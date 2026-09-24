#!/usr/bin/env python3
"""Franchise groups out of Wikidata alone: which titles are one franchise, which need Jev, and what Jev is shown.

No I/O. `pipeline/franchises.py` reads the facts and asks Wikidata; this module turns what they said into
groups, measured in oxyc/den-atlas#92 over the 2,000 most-voted titles.

**A group** is the titles one Wikidata item gathers: a P179 series (the facts' `franchise`), a P8345 media
franchise, a source's book series (reached through P144), or a P155/P156 sequel chain no series names.
A series' parents (`parents`) hold its members too: the Raimi trilogy's films are in "Spider-Man in film".

**Automatic** where Wikidata leaves one answer: the title's groups nest under ONE root, the root is neither
a suspected catalogue nor a suspected shared universe, and nothing links the title outside it. The
franchise is the root; the era is the root's child group the title is in.

**Asked** otherwise, with the title's candidate groups listed A to D (`candidates`). Three things send a
title to Jev, each measured:

- **A suspected catalogue.** A studio's films are typed a film series like a story's are, and no property
  on the item tells them apart (Studio Ghibli, the DC animated line, Disney). What does is the members: in
  a catalogue under a third of the titles share a word with its name, and no character, media franchise
  or sequel link is shared by two of them. That flags Ghibli and five thematic trilogies, and also Jack
  Ryan and Train to Busan, which lack characters on Wikidata: a trigger, not a verdict.
- **A suspected shared universe.** The owner's rule (oxyc/den-atlas#92): More Like This leaves out a
  title's primary franchise only, so Homecoming may still show other MCU films. A group holding two
  disjoint child groups whose name few of its titles share (the MCU, the DCEU, the Conjuring Universe) may
  be that universe rather than a franchise, and Jev says which.
- **A link outside the root**: two roots (Homecoming is in "Spider-Man in film" and in the MCU), a shared
  fictional character, or a shared book series. Characters are 40–50% right outside the spine: spin-offs,
  but also every adaptation of Robin Hood. So they only ever propose.

Grouping by connected component is wrong and never done here: the store's column alone joins 56 Marvel
titles into one component through the crossover films, and characters join a 56-title fairy-tale blob.
"""
import collections
import re

#: Groups listed to Jev per title, lettered in this order.
LETTERS = ("A", "B", "C", "D")
#: Members named per group in the candidates section: enough to recognise it, few enough to stay cheap.
LISTED = 12
#: A character in more works than this is a crossover figure (the Joker is in 30), not a franchise.
MAX_CHARACTER_WORKS = 60
#: Under this share of member titles sharing a word with the group's name, the name is not the story's.
NAME_SHARE = 1 / 3
_STOP = {"the", "a", "an", "of", "and", "in", "film", "films", "series", "feature", "trilogy", "movies",
         "movie", "saga", "collection", "animated", "original", "universe", "cinematic", "television"}
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def words(text):
    return {w for w in (m.group(0).lower() for m in _WORD.finditer(text or "")) if w not in _STOP and len(w) > 1}


class Title:
    """One title as grouping reads it. `series`, `franchises` and `sources` are Q-ids of groups it is in;
    `follows` the corpus keys it follows or is followed by; `characters` its fictional characters."""

    __slots__ = ("key", "name", "year", "media", "series", "franchises", "sources", "follows", "characters",
                 "people")

    def __init__(self, key, name="", year=None, series=(), franchises=(), sources=(), follows=(),
                 characters=(), people=()):
        self.key, self.name, self.year = key, name or "", year
        self.media = key.partition(":")[0]
        self.series, self.franchises, self.sources = tuple(series), tuple(franchises), tuple(sources)
        self.follows, self.characters, self.people = tuple(follows), tuple(characters), frozenset(people)


class Group:
    __slots__ = ("id", "kind", "name", "members")

    def __init__(self, gid, kind, name, members):
        self.id, self.kind, self.name, self.members = gid, kind, name, frozenset(members)

    def __repr__(self):
        return f"Group({self.id!r}, {self.kind}, {len(self.members)})"


def order_key(title):
    return (title.year if title.year is not None else 9999, title.key)


def build(titles, names, parents=None):
    """`{group id: Group}` over `titles` (`{key: Title}`). `names` names a Q-id; `parents` maps a group's
    Q-id to the bigger ones it is part of, and a parent holds every title its children hold. A group of one
    title groups nothing and is dropped; a sequel chain inside one series adds nothing and is dropped."""
    parents = parents or {}
    members = collections.defaultdict(set)
    kinds = {}
    for t in titles.values():
        for kind, qids in (("series", t.series), ("franchise", t.franchises), ("book-series", t.sources)):
            for q in qids:
                members[q].add(t.key)
                kinds.setdefault(q, kind)
    # A parent holds its children's titles, however deep, and a cycle ends the walk.
    for q in list(members):
        seen, stack = {q}, list(parents.get(q, ()))
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            members[p] |= members[q]
            kinds.setdefault(p, "franchise")
            stack.extend(parents.get(p, ()))
    groups = {q: Group(q, kinds[q], names.get(q) or q, keys) for q, keys in members.items() if len(keys) >= 2}
    for chain in _chains(titles):
        if not any(chain <= g.members for g in groups.values()):
            first = min((titles[k] for k in chain), key=order_key)
            gid = f"chain:{first.key}"
            groups[gid] = Group(gid, "chain", chain_name([titles[k].name for k in chain]) or first.name, chain)
    return groups


def chain_name(names):
    """The words every title of a sequel chain starts with, which is what a viewer calls it: "Carry On" for
    Carry On Sergeant, Nurse and Cleo. Empty when they share no first word."""
    split = [n.replace(":", " ").split() for n in names if n]
    common = []
    for column in zip(*split):
        if len({w.lower() for w in column}) != 1:
            break
        common.append(column[0])
    while common and common[-1].lower() in _STOP:
        common.pop()
    return " ".join(common)


def _chains(titles):
    """The P155/P156 chains among the corpus titles, each a set of keys."""
    link = collections.defaultdict(set)
    for t in titles.values():
        for other in t.follows:
            if other in titles and other != t.key:
                link[t.key].add(other)
                link[other].add(t.key)
    seen, out = set(), []
    for start in sorted(link):
        if start in seen:
            continue
        chain, stack = set(), [start]
        while stack:
            k = stack.pop()
            if k in chain:
                continue
            chain.add(k)
            stack.extend(link[k])
        seen |= chain
        out.append(frozenset(chain))
    return out


def name_share(group, titles):
    """The share of the group's titles that share a word with its name."""
    own = words(group.name)
    if not own:
        return 0.0
    return sum(1 for k in group.members if words(titles[k].name) & own) / len(group.members)


def shared_evidence(group, titles):
    """Whether two of the group's titles share a character or a media franchise, or two have a sequel link:
    what one story leaves and a studio's catalogue does not."""
    characters, franchises = collections.Counter(), collections.Counter()
    linked = 0
    for k in group.members:
        t = titles[k]
        characters.update(set(t.characters))
        franchises.update(set(t.franchises) - {group.id})
        linked += any(o in group.members for o in t.follows)
    return linked >= 2 or any(n >= 2 for n in characters.values()) or any(n >= 2 for n in franchises.values())


def children(group, groups):
    """The groups properly inside `group`, largest first."""
    inside = [g for g in groups.values() if g.id != group.id and g.members < group.members]
    return sorted(inside, key=lambda g: (-len(g.members), g.id))


def flags(groups, titles):
    """`{group id: "catalogue" | "universe"}` for the groups Wikidata cannot settle."""
    out = {}
    for g in groups.values():
        if g.kind == "chain" or len(g.members) < 3:
            continue
        share = name_share(g, titles)
        if share < NAME_SHARE and not shared_evidence(g, titles):
            out[g.id] = "catalogue"
            continue
        disjoint = []
        for c in children(g, groups):
            if len(c.members) >= 2 and all(not (c.members & d.members) for d in disjoint):
                disjoint.append(c)
        if len(disjoint) >= 2 and share < NAME_SHARE:
            out[g.id] = "universe"
    return out


def character_links(titles):
    """`{key: {other keys sharing a fictional character}}`, over characters in at most
    `MAX_CHARACTER_WORKS` corpus titles."""
    by = collections.defaultdict(set)
    for t in titles.values():
        for c in set(t.characters):
            by[c].add(t.key)
    out = collections.defaultdict(set)
    for keys in by.values():
        if 2 <= len(keys) <= MAX_CHARACTER_WORKS:
            for k in keys:
                out[k] |= keys - {k}
    return out


#: A word in more group names than this is too common to relate two groups ("Christmas", "Star").
RARE_NAME = 3


def related(groups):
    """`{group id: [other group ids]}` for disjoint groups whose names share an uncommon word: "Beck" (the
    series of the 1997– films) and "Martin Beck" (the novels the 1993 films adapt) are one franchise, and
    no Wikidata statement links them. Jev is shown both and says."""
    named = {g.id: words(g.name) for g in groups.values() if g.kind != "characters"}
    frequency = collections.Counter(w for ws in named.values() for w in ws)
    by_word = collections.defaultdict(set)
    for gid, ws in named.items():
        for w in ws:
            if frequency[w] <= RARE_NAME:
                by_word[w].add(gid)
    out = collections.defaultdict(set)
    for gids in by_word.values():
        for a in gids:
            for b in gids:
                if a != b and not (groups[a].members & groups[b].members):
                    out[a].add(b)
    return {gid: sorted(others) for gid, others in out.items()}


def plan(titles, groups, flagged=None):
    """`(automatic, asked)`. `automatic` is `{key: (franchise group id, era group id or None)}`; `asked` is
    `{key: [group ids, A to D]}`: every title whose franchise Wikidata leaves open, with its candidates."""
    flagged = flags(groups, titles) if flagged is None else flagged
    of = collections.defaultdict(list)
    for g in groups.values():
        for k in g.members:
            of[k].append(g)
    linked = character_links(titles)
    relatives = related(groups)
    automatic, asked = {}, {}
    for key in sorted(titles):
        mine = of.get(key, [])
        roots = [g for g in mine if not any(g.members < h.members for h in mine)]
        mine_keys = set().union(*(g.members for g in mine)) if mine else set()
        outside = linked.get(key, set()) - mine_keys
        kin = sorted({r for g in roots for r in relatives.get(g.id, ())} - {g.id for g in mine})
        if not mine and not outside:
            continue
        if len(roots) == 1 and roots[0].id not in flagged and not outside and not kin:
            root = roots[0]
            automatic[key] = (root.id, era(key, root, groups))
            continue
        asked[key] = candidates(key, mine, roots, outside, kin, titles, groups)
    return automatic, asked


def era(key, root, groups):
    """The era a title is in under `root`: the biggest group inside it that holds the title, or None."""
    eras = [c for c in children(root, groups) if key in c.members]
    return eras[0].id if eras else None


def candidates(key, mine, roots, outside, kin, titles, groups):
    """Up to four groups to show Jev, lettered A to D: the title's roots, largest first; the most specific
    group under them; a group its name relates to (`related`); and the titles a shared character links. A
    candidate is a group id, or `("characters", keys)` for the character link."""
    listed = [g.id for g in sorted(roots, key=lambda g: (-len(g.members), g.id))]
    specific = sorted((g for g in mine if g not in roots), key=lambda g: (len(g.members), g.id))
    listed += [g.id for g in specific[:1]]
    listed += sorted(kin, key=lambda gid: (-len(groups[gid].members), gid))[:1]
    out = listed[:len(LETTERS)]
    if outside and len(out) < len(LETTERS):
        out.append(("characters", tuple(sorted(outside | {key}))))
    return out


def section(key, candidate_ids, groups, titles):
    """The text of the "Franchise candidates" section a title's article is sent with: each candidate
    lettered, what Wikidata calls it, and its titles in release order."""
    lines = ["These are candidate franchise groups from Wikidata for the requested work. Each lists "
             "titles in release order."]
    for letter, cid in zip(LETTERS, candidate_ids):
        if isinstance(cid, tuple):
            keys, heading = cid[1], "titles sharing a fictional character with the requested work"
        else:
            g = groups[cid]
            keys = g.members
            heading = {"series": f"the Wikidata series \"{g.name}\"",
                       "franchise": f"the Wikidata media franchise \"{g.name}\"",
                       "book-series": f"adaptations of the book series \"{g.name}\"",
                       "chain": f"a sequel chain starting with \"{g.name}\""}[g.kind]
        ordered = sorted((titles[k] for k in keys if k in titles), key=order_key)
        shown = [f"{t.name} ({t.year if t.year is not None else 'n.d.'}, "
                 f"{'film' if t.media == 'movie' else 'TV series'})" for t in ordered[:LISTED]]
        more = f"; and {len(ordered) - LISTED} more" if len(ordered) > LISTED else ""
        lines.append(f"{letter}: {heading}, {len(ordered)} titles: {'; '.join(shown)}{more}.")
    return "\n".join(lines)
