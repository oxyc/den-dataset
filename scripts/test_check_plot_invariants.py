"""`check-plot-invariants.py` — the shared-article grounding invariant (oxyc/den-dataset#16).

The invariant is that no two titles are grounded on the same Wikipedia article. It is violated 1,066 times
in the shipped generation, so what is tested here is that the census is CORRECT and that the ratchet
refuses an increase — not that the count is zero.

Every case below fails on the tree before the invariant existed: `shared_plot_articles` and
`report_shared_articles` are not defined there, and `main()` accepts neither `--shared-plot-baseline` nor
`--stamp-meta`.
"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    """The script, whose filename is not an importable module name."""
    spec = importlib.util.spec_from_file_location(
        "check_plot_invariants", os.path.join(HERE, "check-plot-invariants.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cpi = load()


def enriched(rec, **over):
    """One enriched row, grounded unless told otherwise.

    No `plotArticleRole` by default — that is the shipped corpus, enriched before the enrich pass recorded
    which candidate won. Cases that exercise the recorded decision pass it explicitly.
    """
    out = {"tmdbId": rec, "mediaType": "movie", "title": f"T{rec}", "hasWikiPlot": True,
           "overview": "plot", "plotArticle": "Wuthering Heights", "plotLanguage": "en"}
    out.update(over)
    return out


def owner(rec, **over):
    """A row whose recorded provenance says it is grounded on its OWN article, unmoved."""
    return enriched(rec, plotArticleRole="own", plotArticleRedirected=False, **over)


def borrower(rec, **over):
    """A row grounded on the Wikidata P144 source work — a novel, which is a different work."""
    return enriched(rec, plotArticleRole="source-work", plotArticleRedirected=False, **over)


class SharedPlotArticles(unittest.TestCase):
    def test_groups_only_articles_that_ground_more_than_one_title(self):
        grounded = {
            "movie:1": enriched(1),
            "movie:2": enriched(2),
            "movie:3": enriched(3, plotArticle="Solaris"),
        }
        groups = cpi.shared_plot_articles(grounded)
        self.assertEqual(groups, {("en", "Wuthering Heights"): ["movie:1", "movie:2"]},
                         "an article grounding exactly one title is not a violation")

    def test_the_same_article_in_two_languages_is_two_articles(self):
        """`Hamlet` on enwiki and on dewiki are different pages. Collapsing them would report a title
        grounded on its own German article as sharing with a different title's English one."""
        grounded = {
            "movie:1": enriched(1, plotArticle="Hamlet"),
            "movie:2": enriched(2, plotArticle="Hamlet", plotLanguage="de"),
        }
        self.assertEqual(cpi.shared_plot_articles(grounded), {},
                         "the language is part of the article's identity")

    def test_a_missing_language_reads_as_english(self):
        """`plotLanguage` was added after the first passes, so the oldest rows carry an English plot and no
        language. Bucketing those separately would hide the most-inherited groundings."""
        grounded = {
            "movie:1": enriched(1, plotLanguage=None),
            "movie:2": enriched(2),
        }
        self.assertEqual(sorted(cpi.shared_plot_articles(grounded)), [("en", "Wuthering Heights")])

    def test_a_title_with_no_plot_article_is_not_grouped(self):
        grounded = {"movie:1": enriched(1, plotArticle=None), "movie:2": enriched(2, plotArticle=None)}
        self.assertEqual(cpi.shared_plot_articles(grounded), {},
                         "no recorded article is unknown grounding, not shared grounding")

    def test_one_wikidata_item_behind_several_tmdb_ids_is_not_sharing(self):
        """`Don't Hug Me I'm Scared` 1-5 are ONE Wikidata item with five TMDB ids, and `Carlos` is in TMDB
        as both a movie and a series. They are one work on its own page, so the invariant can never ratchet
        to zero while they are counted."""
        grounded = {"movie:1": enriched(1), "movie:2": enriched(2)}
        self.assertEqual(cpi.shared_plot_articles(grounded, same_item={"movie:1": "tt2591814",
                                                                      "movie:2": "tt2591814"}), {})

    def test_two_items_on_one_article_are_still_sharing(self):
        grounded = {"movie:1": enriched(1), "movie:2": enriched(2)}
        self.assertEqual(
            sorted(cpi.shared_plot_articles(grounded, same_item={"movie:1": "tt0000001",
                                                                 "movie:2": "tt0000002"})),
            [("en", "Wuthering Heights")])

    def test_an_unknown_item_does_not_exempt_a_group(self):
        """A title absent from the facts file has no known identity. Exempting on that would turn a missing
        fact into a licence to share."""
        grounded = {"movie:1": enriched(1), "movie:2": enriched(2)}
        self.assertEqual(sorted(cpi.shared_plot_articles(grounded, same_item={"movie:1": "tt2591814"})),
                         [("en", "Wuthering Heights")])


class Borrowers(unittest.TestCase):
    """Which SIDE of a shared article is at fault.

    336 of the 1,066 are grounded on their own page, correctly, and are counted only because someone else
    borrowed it. Ratcheting all 1,066 asks the 336 to fix something they did not do.
    """

    def test_a_recorded_owner_is_not_counted(self):
        grounded = {"movie:1": owner(1), "movie:2": borrower(2)}
        groups = cpi.shared_plot_articles(grounded)
        _, out, _ = run_report(groups, grounded)
        self.assertIn("another title : 1", out, "only the borrower is counted")

    def test_provenance_that_was_never_recorded_counts(self):
        """The shipped corpus has no provenance at all. A reader that cannot tell must say so and count the
        title, not assume it owns the article — the standing 1,066 stays 1,066."""
        grounded = {"movie:1": enriched(1), "movie:2": enriched(2)}
        groups = cpi.shared_plot_articles(grounded)
        _, out, _ = run_report(groups, grounded)
        self.assertIn("another title : 2", out)
        self.assertIn("no recorded provenance", out, "and it says why it could not tell")

    def test_an_own_article_reached_through_a_redirect_is_a_borrower(self):
        """`Jarhead 2: Field of Fire` resolves to `Jarhead (film)`. The winning candidate is the title's
        own sitelink and the text is about another film, so no change to the source-work fall-through
        reaches this class."""
        grounded = {"movie:1": owner(1),
                    "movie:2": enriched(2, plotArticleRole="own", plotArticleRedirected=True)}
        groups = cpi.shared_plot_articles(grounded)
        _, out, _ = run_report(groups, grounded)
        self.assertIn("another title : 1", out)

    def test_an_unknown_redirect_counts(self):
        """The Enterprise endpoint names no page, so it cannot say whether a redirect moved the fetch.
        Counting it as an owner would clear a title on a question nothing answered."""
        grounded = {"movie:1": enriched(1, plotArticleRole="own", plotArticleRedirected=None),
                    "movie:2": borrower(2)}
        groups = cpi.shared_plot_articles(grounded)
        _, out, _ = run_report(groups, grounded)
        self.assertIn("another title : 2", out)


class ProvenanceCensus(unittest.TestCase):
    """The per-title check the collision census stands in for.

    2,075 titles are grounded on something that is not about them; the collision census sees 729 of them,
    because a title grounded on a novel that grounds no SECOND title collides with nobody.
    """

    def test_a_source_work_grounding_is_named_even_when_it_collides_with_nothing(self):
        grounded = {"movie:1": borrower(1, plotArticle="Silo (novel)")}
        census = cpi.plot_provenance(grounded)
        self.assertEqual(census["misgrounded"], ["movie:1"])
        self.assertEqual(census["unrecorded"], [])

    def test_a_redirect_is_named(self):
        grounded = {"movie:1": enriched(1, plotArticleRole="own", plotArticleRedirected=True)}
        self.assertEqual(cpi.plot_provenance(grounded)["misgrounded"], ["movie:1"])

    def test_an_own_grounding_is_clean(self):
        self.assertEqual(cpi.plot_provenance({"movie:1": owner(1)})["misgrounded"], [])

    def test_another_language_is_still_this_title(self):
        grounded = {"movie:1": enriched(1, plotArticleRole="own-other-language",
                                        plotArticleRedirected=False, plotLanguage="de")}
        self.assertEqual(cpi.plot_provenance(grounded)["misgrounded"], [])

    def test_a_record_with_no_recorded_role_is_unknown_not_clean(self):
        """Absent must mean "enriched before this was recorded". Reading it as `own` would report the whole
        shipped corpus as correctly grounded on the strength of a field nothing wrote."""
        census = cpi.plot_provenance({"movie:1": enriched(1)})
        self.assertEqual(census["misgrounded"], [])
        self.assertEqual(census["unrecorded"], ["movie:1"])

    def test_the_roles_are_counted(self):
        grounded = {"movie:1": owner(1), "movie:2": borrower(2), "movie:3": enriched(3)}
        census = cpi.plot_provenance(grounded)
        self.assertEqual(census["roles"], {"own": 1, "source-work": 1})

    def test_the_census_is_reported_and_stamped(self):
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [borrower(1, plotArticle="Silo (novel)"), owner(2)]})
            meta = os.path.join(dir, "dataset.meta.json")
            with open(meta, "w", encoding="utf-8") as fh:
                json.dump({}, fh)
            code, out, _ = run(["--enriched-dir", e, "--labels", labels, "--stamp-meta", meta])
            self.assertEqual(code, 0)
            self.assertIn("grounded on another work", out)
            self.assertIn("movie:1", out, "and it names the title, though nothing collides with it")
            with open(meta, encoding="utf-8") as fh:
                stamped = json.load(fh)
            self.assertEqual(stamped["misgroundedTitles"], 1)
            self.assertEqual(stamped["provenanceUnrecordedTitles"], 0)

    def test_a_corpus_with_no_provenance_says_it_cannot_tell(self):
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [enriched(1), enriched(2, plotArticle="Solaris")]})
            code, out, _ = run(["--enriched-dir", e, "--labels", labels])
            self.assertEqual(code, 0, "a corpus enriched before the field existed still publishes")
            self.assertIn("no recorded provenance", out)
            self.assertIn("2", out)


def write_case(dir, rows_by_batch, labelled=None):
    """An enriched tree + a labels blob, and the argv that checks them."""
    enriched_dir = os.path.join(dir, "enriched")
    os.makedirs(enriched_dir, exist_ok=True)
    for batch, rows in rows_by_batch.items():
        with open(os.path.join(enriched_dir, f"batch-{batch}.json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
    keys = labelled if labelled is not None else [
        r["tmdbId"] for rows in rows_by_batch.values() for r in rows]
    labels = os.path.join(dir, "labels-t02.json")
    with open(labels, "w", encoding="utf-8") as fh:
        json.dump({"records": [{"tmdbId": k, "mediaType": "movie"} for k in keys]}, fh)
    return enriched_dir, labels


def run_report(groups, grounded):
    """`report_shared_articles` with its output captured, returning `(count, stdout, stderr)`."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        count = cpi.report_shared_articles(groups, grounded)
    return count, out.getvalue(), err.getvalue()


def run(argv):
    """`main()` with argv, returning `(code, stdout, stderr)`."""
    out, err = io.StringIO(), io.StringIO()
    import sys
    argv_was = sys.argv
    sys.argv = ["check-plot-invariants.py"] + argv
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cpi.main()
    finally:
        sys.argv = argv_was
    return code, out.getvalue(), err.getvalue()


class NewestBatchWins(unittest.TestCase):
    def test_a_regrounded_title_leaves_the_census_on_its_new_article(self):
        """A later batch that re-grounds a title elsewhere must move it. Keeping the superseded row would
        report a violation on an article the shipped dataset no longer uses."""
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {
                1: [enriched(1), enriched(2)],
                2: [enriched(2, plotArticle="Solaris")],
            })
            code, out, _ = run(["--enriched-dir", e, "--labels", labels])
            self.assertEqual(code, 0)
            self.assertIn("another title : 0", out,
                          "movie:2 moved to its own article, so nothing is shared any more")

    def test_a_title_regrounded_to_no_plot_leaves_the_grounded_set(self):
        """`hasWikiPlot: false` in a later batch supersedes the grounded row. Leaving it in would keep
        counting the article its dead row named."""
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {
                1: [enriched(1), enriched(2)],
                2: [enriched(2, hasWikiPlot=False)],
            }, labelled=[1])
            code, out, _ = run(["--enriched-dir", e, "--labels", labels])
            self.assertIn("another title : 0", out)
            self.assertEqual(code, 0)

    def test_batches_are_ordered_numerically_not_lexically(self):
        """`batch-99` is older than `batch-177`. Sorting by name makes the winner depend on digit count,
        which is the bug `batch_files` already exists to avoid — asserted here because the census now
        depends on it too."""
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {
                99: [enriched(1, plotArticle="Solaris")],
                177: [enriched(1), enriched(2)],
            })
            code, out, _ = run(["--enriched-dir", e, "--labels", labels])
            self.assertIn("another title : 2", out,
                          "batch-177 is newest, so movie:1 is on Wuthering Heights with movie:2")


class Ratchet(unittest.TestCase):
    def test_a_standing_violation_warns_and_publishes(self):
        """The count is 1,066 today. An absolute floor of zero would refuse every publish over a defect that
        has already shipped, and a gate like that gets switched off."""
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [enriched(1), enriched(2)]})
            code, out, _ = run(["--enriched-dir", e, "--labels", labels, "--shared-plot-baseline", "2"])
            self.assertEqual(code, 0, "standing at the baseline is not a regression")
            self.assertIn("another title : 2", out, "and it is still reported, every time")

    def test_an_increase_is_refused(self):
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [enriched(1), enriched(2), enriched(3)]})
            code, _, err = run(["--enriched-dir", e, "--labels", labels, "--shared-plot-baseline", "2"])
            self.assertEqual(code, 2, "3 against a baseline of 2 is a regression")
            self.assertIn("REGRESSED", err)
            self.assertIn("DEN_ALLOW_SHARED_PLOTS", err, "a refusal must say what to do")

    def test_a_decrease_is_fine(self):
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [enriched(1), enriched(2)]})
            code, _, _ = run(["--enriched-dir", e, "--labels", labels, "--shared-plot-baseline", "9"])
            self.assertEqual(code, 0, "the number is allowed to go down")

    def test_no_baseline_only_censuses(self):
        """The first publish since the guard existed has nothing to ratchet against."""
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [enriched(1), enriched(2)]})
            code, out, _ = run(["--enriched-dir", e, "--labels", labels])
            self.assertEqual(code, 0)
            self.assertIn("another title : 2", out)

    def test_the_regression_names_both_sides_and_the_cause(self):
        """House style: a guard that refuses names both sides and says what to do."""
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [enriched(1), enriched(2), enriched(3)]})
            _, out, err = run(["--enriched-dir", e, "--labels", labels, "--shared-plot-baseline", "1"])
            self.assertIn("'Wuthering Heights'", out, "the shared article is named")
            self.assertIn("movie:1", out, "and so are the titles on it")
            self.assertIn("`reground` (pipeline/enrich.py)", err, "and the code that holds the mechanism")


class Stamping(unittest.TestCase):
    def test_the_counts_are_stamped_for_the_next_publish(self):
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [enriched(1), enriched(2), enriched(3, plotArticle="Solaris")]})
            meta = os.path.join(dir, "dataset.meta.json")
            with open(meta, "w", encoding="utf-8") as fh:
                json.dump({"datasetVersion": "abc"}, fh)
            run(["--enriched-dir", e, "--labels", labels, "--stamp-meta", meta])
            with open(meta, encoding="utf-8") as fh:
                stamped = json.load(fh)
            self.assertEqual(stamped["sharedPlotArticleTitles"], 2)
            self.assertEqual(stamped["sharedPlotArticles"], 1)
            self.assertEqual(stamped["datasetVersion"], "abc", "and nothing else in the manifest moves")

    def test_a_regression_is_still_stamped(self):
        """The stamp is what the NEXT publish ratchets against. Skipping it on a refusal would mean an
        accepted regression leaves no baseline, so the increase could then happen twice unnoticed."""
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [enriched(1), enriched(2), enriched(3)]})
            meta = os.path.join(dir, "dataset.meta.json")
            with open(meta, "w", encoding="utf-8") as fh:
                json.dump({}, fh)
            code, _, _ = run(["--enriched-dir", e, "--labels", labels,
                              "--stamp-meta", meta, "--shared-plot-baseline", "1"])
            self.assertEqual(code, 2)
            with open(meta, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["sharedPlotArticleTitles"], 3)


class Scope(unittest.TestCase):
    def test_facts_scope_bounds_the_census(self):
        """`--facts` scopes the existing invariants to what shipped; the census must use the same scope or
        it reports titles the dataset does not carry."""
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [enriched(1), enriched(2), enriched(3)]})
            facts = os.path.join(dir, "facts.json")
            with open(facts, "w", encoding="utf-8") as fh:
                json.dump({"records": [{"tmdbId": 1, "mediaType": "movie"},
                                       {"tmdbId": 2, "mediaType": "movie"}]}, fh)
            _, out, _ = run(["--enriched-dir", e, "--labels", labels, "--facts", facts])
            self.assertIn("another title : 2", out, "movie:3 is out of scope, so it is not counted")

    def test_facts_supplies_the_wikidata_identity_the_exemption_needs(self):
        """`imdbId` is the item's identity and lives only in the facts file — the enriched row has no
        Wikidata id at all, so without `--facts` the one-item groups cannot be told from real sharing."""
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [enriched(1), enriched(2)]})
            facts = os.path.join(dir, "facts.json")
            with open(facts, "w", encoding="utf-8") as fh:
                json.dump({"records": [{"tmdbId": 1, "mediaType": "movie", "imdbId": "tt2591814"},
                                       {"tmdbId": 2, "mediaType": "movie", "imdbId": "tt2591814"}]}, fh)
            _, without, _ = run(["--enriched-dir", e, "--labels", labels])
            self.assertIn("another title : 2", without)
            _, with_facts, _ = run(["--enriched-dir", e, "--labels", labels, "--facts", facts])
            self.assertIn("another title : 0", with_facts, "one item behind two TMDB ids is one work")

    def test_a_list_valued_imdb_id_is_read(self):
        """`imdbId` ships as `one_or_many`: a scalar for nearly every title, a list where Wikidata carries
        two. A reader that only handles the scalar silently stops exempting those."""
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, {1: [enriched(1), enriched(2)]})
            facts = os.path.join(dir, "facts.json")
            with open(facts, "w", encoding="utf-8") as fh:
                json.dump({"records": [{"tmdbId": 1, "mediaType": "movie", "imdbId": ["tt2591814"]},
                                       {"tmdbId": 2, "mediaType": "movie", "imdbId": "tt2591814"}]}, fh)
            _, out, _ = run(["--enriched-dir", e, "--labels", labels, "--facts", facts])
            self.assertIn("another title : 0", out)


def write_store(dir, vectors, sections=("keys", "vec_plot_has")):
    """A store with `keys` and `vec_plot_has` for `{key: has_vector}`, written by the real section writer."""
    import sys
    sys.path.insert(0, os.path.dirname(HERE))
    sys.path.insert(0, os.path.join(HERE, "v2"))
    from store import format
    import vector_blob
    keys = list(vectors)
    sec = format.Sections(len(keys))
    if "keys" in sections:
        sec.put("keys", "Q", [vector_blob.pack_key(k) for k in keys], 8)
    if "vec_plot_has" in sections:
        sec.put("vec_plot_has", "B", [1 if vectors[k] else 0 for k in keys], 1)
    path = os.path.join(dir, "den-abc.store")
    format.write(path, sec, len(keys), "abc")
    return path


class PlotVectorGate(unittest.TestCase):
    """oxyc/den-dataset#10: a title the store ships with a plot must have a plot vector.

    Spirited Away, One Piece, Bleach, Pokémon, Re:Zero and Off Campus shipped with a plot and none, and the
    check that would have seen it only warned. Every case here fails on the tree before `--store` existed.
    """

    def gate(self, rows_by_batch, vectors, extra=(), **store_kw):
        with tempfile.TemporaryDirectory() as dir:
            e, labels = write_case(dir, rows_by_batch)
            store = write_store(dir, vectors, **store_kw)
            return run(["--enriched-dir", e, "--labels", labels, "--store", store, *extra])

    def test_a_plot_with_no_vector_is_refused_and_named(self):
        code, _, err = self.gate({1: [enriched(129, title="Spirited Away", plotArticle="Spirited Away"),
                                      enriched(2, plotArticle="Solaris")]},
                                 {"movie:129": False, "movie:2": True})
        self.assertEqual(code, 3)
        self.assertIn("1 of the 2 titles", err)
        self.assertIn("movie:129", err)
        self.assertIn("'Spirited Away'", err, "the refusal names the title, not just the id")

    def test_every_plot_with_a_vector_passes(self):
        code, out, _ = self.gate({1: [enriched(1, plotArticle="A"), enriched(2, plotArticle="B")]},
                                 {"movie:1": True, "movie:2": True})
        self.assertEqual(code, 0)
        self.assertIn("plot-vector gate: all 2 titles", out)

    def test_a_title_without_a_plot_needs_no_vector(self):
        code, _, _ = self.gate({1: [enriched(1, hasWikiPlot=False)]}, {"movie:1": False})
        self.assertEqual(code, 0)

    def test_the_newest_batch_decides_whether_there_is_a_plot(self):
        """A later re-enrich that found no plot supersedes the older row that had one."""
        code, _, _ = self.gate({1: [enriched(1)], 2: [enriched(1, hasWikiPlot=False)]}, {"movie:1": False})
        self.assertEqual(code, 0)
        code, _, _ = self.gate({1: [enriched(1, hasWikiPlot=False)], 2: [enriched(1)]}, {"movie:1": False})
        self.assertEqual(code, 3, "and a plot that arrived later counts — that is how four of the six got in")

    def test_a_title_the_store_does_not_ship_is_out_of_scope(self):
        code, _, _ = self.gate({1: [enriched(1, plotArticle="A"), enriched(2, plotArticle="B")]},
                               {"movie:1": True})
        self.assertEqual(code, 0, "movie:2 is not in the store: an admission question, not a missing vector")

    def test_a_batch_past_max_batch_id_is_not_read(self):
        code, _, _ = self.gate({1: [enriched(1, hasWikiPlot=False)], 2: [enriched(1)]}, {"movie:1": False},
                               extra=("--max-batch-id", "1"))
        self.assertEqual(code, 0)

    def test_a_store_that_cannot_say_is_refused(self):
        code, _, err = self.gate({1: [enriched(1)]}, {"movie:1": True}, sections=("keys",))
        self.assertEqual(code, 3, "no vec_plot_has section is not a pass")
        self.assertIn("vec_plot_has", err)

    def test_it_wins_over_the_shared_article_ratchet(self):
        """2 has an override in the publisher (`DEN_ALLOW_SHARED_PLOTS`); this must not ride on it."""
        code, _, _ = self.gate({1: [enriched(1), enriched(2)]}, {"movie:1": False, "movie:2": True},
                               extra=("--shared-plot-baseline", "0"))
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
