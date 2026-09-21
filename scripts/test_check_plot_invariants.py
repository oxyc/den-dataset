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
    """One enriched row, grounded unless told otherwise."""
    out = {"tmdbId": rec, "mediaType": "movie", "title": f"T{rec}", "hasWikiPlot": True,
           "overview": "plot", "plotArticle": "Wuthering Heights", "plotLanguage": "en"}
    out.update(over)
    return out


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
            self.assertIn("regroundOnWikipedia", err, "and the mechanism that caused it")


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


if __name__ == "__main__":
    unittest.main()
