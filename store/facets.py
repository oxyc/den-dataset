"""Facets — the dense `facet_v` / `facet_c` arrays, the FACETS-V2 publication gates that decide what
reaches them, the tentative tier (`facet_tv` / `facet_tp`) beside them, and the `applic_*` columns the
gates judge against.

den-spec `wire/store-v2.md` § "Facets". The store carries an argmax and one confidence byte per axis,
so the gates cannot live in a reader: the runner-up probability clause 4 needs, and `validity`'s own
distribution clause 2 needs, are not in the file and never were.
"""
import sys
from collections import Counter, defaultdict

from .format import U32_NONE, hundredths

FACET_AXES = ("era", "setting", "scope", "ending", "pacing", "chronology",
              "continuity", "conflict", "ensemble", "tone", "timespan", "archetype")

# The two applicability questions, from the corpus pass.
APPLICABILITY = ("validity", "narrative_applicability")

# ---- the FACETS-V2 publication gates ---------------------------------------------------------------
#
# `docs/FACETS-V2.md` § "Publication gates": "The pilots support collection, not unconditional
# argmax publication." The corpus is the collection — it keeps every answer with its full distribution,
# so nothing here is unrecoverable and the store can be rebuilt with different thresholds in ~2 minutes.
# The store is the PUBLICATION, and these are the conditions the spec puts on it.
#
# Why here and not in den-atlas: the store carries the argmax and one confidence byte per axis. The
# runner-up probability, which clause 4's margin needs, and `validity`'s own distribution, which clause 2
# needs, are NOT in the store and never were. A reader physically cannot apply this gate. Putting it in
# the reader would also leave the one published artifact asserting things the spec says are not
# publishable, which is the defect this exists to close.

#: Clause 2 — "Publish plot facets only when `validity=correct-screen-work` has probability at least
#: 0.80." The PROBABILITY, not the self-reported `confidence`: they are separate fields and differ.
VALIDITY_MIN = 0.80
#: Clause 4 — "require probability at least 0.70 and a top-minus-runner-up margin of at least 0.25".
FACET_PROB_MIN = 0.70
FACET_MARGIN_MIN = 0.25
#: Clause 4 — "Exclude `does-not-apply` and `ending=unknown`". `does-not-apply` is excluded on every
#: axis and is checked on its own below; this is the per-axis half. `unknown` is `ending`'s way of
#: saying a work has not ended yet — a fact about the corpus, not an ending a viewer can browse to, and
#: the largest published `ending` value in the live index at 8,858 titles, *Game of Thrones* included.
NEVER_PUBLISHED = {("ending", "unknown")}
#: Clause 3 — "Suppress plot facets for talk, variety, game, news, or reality programs without a bounded
#: narrative." That is `narrative_applicability`'s own value for such a programme.
NO_NARRATIVE = "non-narrative-program"
#: Clause 3, second sentence — "Suppress archetype additionally for documentaries, anthologies,
#: open-ended series, and multi-arc works", "from deterministic metadata/content type or a separate
#: typed applicability result, not from archetype's own no-answer probability". `narrative_applicability`
#: IS that separate typed result, and its remaining value is the one archetype may be published for.
#: The spec's own counter-example — the documentary `Killer Inside: The Mind of Aaron Hernandez` at
#: `downfall` 0.76 — is exactly a row this excludes and no confidence threshold would have.
ARCHETYPE_REQUIRES = "bounded-fictional-narrative"

# ---- the tentative tier ----------------------------------------------------------------------------
#
# An answer the gates refused ONLY because it was uncertain — the right work, a narrative it applies to,
# a real value, but under clause 4's 0.70 — is still the model's best guess, and a filter that drops it
# loses most of an axis's minority values (oxyc/den-atlas#35: `bittersweet` keeps 47% of its titles, `open`
# 39%, `ambiguous` 30%). Those answers ship in `facet_tv` / `facet_tp`, apart from the published tier, so a
# reader can list them AFTER the confident ones and say which is which. Everything else a gate refused —
# the wrong work, a non-narrative programme, `does-not-apply`, `ending=unknown`, an archetype on an
# unbounded narrative — is not uncertainty, and gets no tentative value.

#: The refusals that mean "uncertain", and nothing else. `margin<0.25` cannot fire on a normalised
#: distribution once p >= 0.70, but a distribution with slack could reach it, and it is uncertainty too.
UNCERTAIN = {"p<0.70", "margin<0.25"}
#: The floor, from a blind grading of 216 tentative answers on the 2,000 most-voted films and series
#: (oxyc/den-atlas#35): a defensible answer 60% of the time at 0.40-0.50, 71% at 0.50-0.60, 80% at
#: 0.60-0.70. The published tier grades 9-10 out of 10.
TENTATIVE_PROB_MIN = 0.50
#: Where no floor helps, measured the same way. `scope` graded 7 of 14 at or above 0.50, a coin flip at
#: every band. `tone=clinical` was wrong on all five sampled titles, from 0.54 to 0.66 (Jack Reacher,
#: Black Widow, Half-Blood Prince). Without them the tier grades 80% defensible, 65% exact, on 150 titles.
TENTATIVE_AXES_EXCLUDED = {"scope"}
TENTATIVE_EXCLUDED = {("tone", "clinical")}


def row_applicability(row):
    """`(validity probability, narrative_applicability choice)` — the two facts the gates judge against.

    Both come from the `applicability` block, which the corpus pass answers for every title it read. A row
    that has no block at all reads as probability 0.0, which fails the validity clause and so publishes no
    facets — the same answer as an explicit "this is not the right work", and the safe one.
    """
    applic = row.get("applicability") or {}
    validity = applic.get("validity")
    probability = 0.0
    if isinstance(validity, dict):
        probability = float((validity.get("probabilities") or {}).get("correct-screen-work") or 0.0)
    narrative = applic.get("narrative_applicability")
    return probability, (narrative.get("choice") if isinstance(narrative, dict) else None)


def publishable(axis, facet, validity_probability, narrative):
    """Whether one axis of one title may be PUBLISHED, and if not, which clause refused it.

    Returns `(bool, reason)`. The clauses are FACETS-V2 § "Publication gates" 2-4, in the order written
    there; the first failure is the reason, so the counts this produces partition the corpus.

    Clause 1 — provenance — is not checked here. It is the bundle auditor's (`audit_combined_bundle.py`),
    which has the manifests, and a record only reaches the corpus by passing it.

    The margin is computed against the WHOLE distribution, `does-not-apply` included. Those values may
    not be published, but they are still hypotheses the model weighed, and dropping them from the
    comparison would inflate every margin by whatever mass sat on them.
    """
    if not isinstance(facet, dict):
        return False, "absent"
    choice = facet.get("choice")
    if not choice:
        return False, "absent"
    if choice == "does-not-apply":
        return False, "does-not-apply"
    if (axis, choice) in NEVER_PUBLISHED:
        return False, f"{axis}={choice}"
    if validity_probability < VALIDITY_MIN:
        return False, "validity<0.80"
    if narrative == NO_NARRATIVE:
        return False, "non-narrative-program"
    if axis == "archetype" and narrative != ARCHETYPE_REQUIRES:
        return False, f"archetype not-bounded ({narrative})"
    probabilities = facet.get("probabilities")
    if not isinstance(probabilities, dict) or choice not in probabilities:
        # The distribution is what clause 4 is written against. A choice without one cannot be judged,
        # and publishing it unjudged is the thing this function exists to stop.
        return False, "no distribution"
    top = float(probabilities[choice] or 0.0)
    if top < FACET_PROB_MIN:
        return False, "p<0.70"
    ordered = sorted((float(v or 0.0) for v in probabilities.values()), reverse=True)
    runner_up = ordered[1] if len(ordered) > 1 else 0.0
    if top - runner_up < FACET_MARGIN_MIN:
        return False, "margin<0.25"
    return True, None


def tentative(axis, facet, refused):
    """Whether an answer `publishable` refused as `refused` still ships in the tentative tier.

    Only an uncertain answer does, and only when it is the distribution's strict argmax at or above
    `TENTATIVE_PROB_MIN`: a tie names no answer, and a choice the model's own distribution ranks below
    another is not its best guess. Every other refusal already happened before the probability was read,
    so it reaches here as a reason outside `UNCERTAIN`.
    """
    if refused not in UNCERTAIN or axis in TENTATIVE_AXES_EXCLUDED:
        return False
    choice = facet["choice"]
    if (axis, choice) in TENTATIVE_EXCLUDED:
        return False
    probabilities = {k: float(v or 0.0) for k, v in facet["probabilities"].items()}
    top = probabilities[choice]
    return top >= TENTATIVE_PROB_MIN and all(v < top for k, v in probabilities.items() if k != choice)


class Facets:
    """The dense facet arrays, the applicability columns, and what the gates withheld."""

    def __init__(self):
        self.value = []              # dense R x 12, axis order = FACET_AXES
        self.confidence = bytearray()
        self.tentative_value = []    # the same shape: the tentative tier
        self.tentative_probability = bytearray()
        self.applic_v = []
        self.applic_c = bytearray()
        # What the publication gates published, shipped as tentative and withheld, per axis — reported at
        # the end of the build and stamped into the manifest. A gate that drops 42% of `ending` must say so
        # in a number, not leave the next reader to discover it as a coverage surprise.
        self.published = Counter()
        self.tentative = Counter()
        self.withheld = defaultdict(Counter)

    def intern(self, strings, row):
        """Interned under the SAME gates `add` applies, so a value no row publishes or ships as tentative
        never reaches the dictionary. Interning it anyway would leave a vocabulary in the store that no row
        uses, which reads from the outside as a value with zero titles rather than as one the gate
        withheld."""
        validity_probability, narrative = row_applicability(row)
        for axis in FACET_AXES:
            v = (row.get("facets") or {}).get(axis)
            ok, reason = publishable(axis, v, validity_probability, narrative)
            if ok or tentative(axis, v, reason):
                strings.add(v["choice"])
        for name in APPLICABILITY:
            choice = (row.get("applicability") or {}).get(name)
            if isinstance(choice, dict) and choice.get("choice"):
                strings.add(choice["choice"])

    def add(self, strings, key, row):
        # A value that does not clear the gates is written as absent — the SAME `U32_NONE` an axis the
        # model declined gets, and deliberately so: both mean "this store makes no claim here", which is
        # the only thing a reader may conclude from either. The store has one sentinel per axis and no
        # room for a second without changing den-spec `wire/store-v1.md` and both readers, and no reader
        # has a use for the distinction — a row must not list a title under `ending=tragic` because the
        # model guessed tragic at 0.44. The corpus keeps the full answer and the reason, so nothing is
        # lost, only unpublished.
        #
        # An answer refused only as uncertain may still land in the tentative tier, which is a separate
        # pair of sections for exactly that reason: `facet_v` keeps meaning "published", and a reader that
        # knows nothing of the tier reads the store as it always has.
        validity_probability, narrative = row_applicability(row)
        for axis in FACET_AXES:
            v = (row.get("facets") or {}).get(axis)
            ok, reason = publishable(axis, v, validity_probability, narrative)
            if ok:
                self.published[axis] += 1
                self.value.append(strings.id(v["choice"]))
                self.confidence.append(hundredths(v.get("confidence"), f"facet {axis} confidence", key))
            else:
                self.value.append(U32_NONE)
                self.confidence.append(0)
            if not ok and tentative(axis, v, reason):
                self.tentative[axis] += 1
                self.tentative_value.append(strings.id(v["choice"]))
                self.tentative_probability.append(
                    hundredths(v["probabilities"][v["choice"]], f"facet {axis} probability", key))
            else:
                if not ok:
                    self.withheld[axis][reason] += 1
                self.tentative_value.append(U32_NONE)
                self.tentative_probability.append(0)

        applic = row.get("applicability") or {}
        for name in APPLICABILITY:
            entry = applic.get(name)
            if isinstance(entry, dict) and entry.get("choice"):
                self.applic_v.append(strings.id(entry["choice"]))
                self.applic_c.append(hundredths(entry.get("confidence"), f"{name} confidence", key))
            else:
                self.applic_v.append(U32_NONE)
                self.applic_c.append(0)

    def put(self, sec, rows):
        sec.put("facet_v", "I", self.value, 4, expect=rows * len(FACET_AXES))
        sec.put_raw("facet_c", self.confidence, 1, expect=rows * len(FACET_AXES))
        sec.put("facet_tv", "I", self.tentative_value, 4, expect=rows * len(FACET_AXES))
        sec.put_raw("facet_tp", self.tentative_probability, 1, expect=rows * len(FACET_AXES))

    def put_applicability(self, sec, rows):
        sec.put("applic_v", "I", self.applic_v, 4, expect=rows * len(APPLICABILITY))
        sec.put_raw("applic_c", self.applic_c, 1, expect=rows * len(APPLICABILITY))

    def announce(self, rows):
        """Per axis, on stderr: published, tentative, withheld, and which clause did the withholding."""
        print("plot-facet publication gates (FACETS-V2 § Publication gates):", file=sys.stderr)
        for axis in FACET_AXES:
            kept, maybe = self.published[axis], self.tentative[axis]
            why = ", ".join(f"{r}={c}" for r, c in self.withheld[axis].most_common())
            print(f"  {axis:12} {kept:6d}/{rows} ({kept / rows * 100:5.1f}%)  tentative {maybe:6d} "
                  f"({maybe / rows * 100:5.1f}%)  withheld: {why}", file=sys.stderr)

    def report(self):
        """The same, for the manifest — so the only record of a 42% `ending` drop is not a build log
        nobody kept."""
        return {
            "validityMin": VALIDITY_MIN, "probabilityMin": FACET_PROB_MIN, "marginMin": FACET_MARGIN_MIN,
            "tentativeProbabilityMin": TENTATIVE_PROB_MIN,
            "tentativeExcluded": sorted(TENTATIVE_AXES_EXCLUDED)
            + sorted(f"{a}={v}" for a, v in TENTATIVE_EXCLUDED),
            "axes": {axis: {"published": self.published[axis], "tentative": self.tentative[axis],
                            "withheld": dict(sorted(self.withheld[axis].items()))}
                     for axis in FACET_AXES},
        }
