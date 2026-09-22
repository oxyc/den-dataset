"""`eval-taxonomy.py` — the only check that measures label QUALITY rather than plumbing.

The maths is tested here against tiny synthetic sets, because CI has no `labels-t02.json`: it is a release
asset, not a committed one. The real scoring runs in the publish path, where the out-dir holds it.

Every test below is a way this check can LIE — score perfectly while measuring nothing, or score zero while
the data is fine. A quality gate that can do either is worse than none, because it is believed.
"""
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location("eval_taxonomy", os.path.join(HERE, "eval-taxonomy.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = load()


def golden(titles):
    return {"taxonomyVersion": "t02", "titles": titles}


def title(tmdb_id, primary, subgenres=(), themes=(), moods=()):
    return {"tmdbId": tmdb_id, "mediaType": "movie", "title": f"T{tmdb_id}", "primaryGenre": primary,
            "subgenres": list(subgenres), "themes": list(themes), "moods": list(moods)}


def labels(rows):
    """`(mediaType, tmdbId)` → record, in the PUBLISHED shape."""
    return {("movie", r["tmdbId"]): r for r in rows}


def record(tmdb_id, primary, subgenres=(), moods=()):
    return {"tmdbId": tmdb_id, "mediaType": "movie", "primaryGenre": primary,
            "subgenres": [{"label": l, "confidence": c} for l, c in subgenres],
            "moods": [{"label": l, "confidence": c} for l, c in moods]}


class Parsing(unittest.TestCase):
    """The failure that actually happened on this file's first run.

    The published shape is `{"label": …, "confidence": …}`. Reading it as a `[name, confidence]` pair takes
    the dict's first key — `"confidence"` — as the label for every entry. Nothing matches, every family
    scores 0.000, and the output is indistinguishable from a classifier that has completely collapsed.
    """
    def test_the_published_dict_shape_is_read(self):
        self.assertEqual(ev.named([{"label": "Heist", "confidence": 0.9}], 0.0), {"Heist"})

    def test_the_older_pair_and_bare_string_shapes_still_read(self):
        self.assertEqual(ev.named([["Heist", 0.9]], 0.0), {"Heist"})
        self.assertEqual(ev.named(["Heist"], 0.0), {"Heist"})

    def test_a_confidence_floor_drops_the_labels_under_it(self):
        entries = [{"label": "Heist", "confidence": 0.9}, {"label": "Caper", "confidence": 0.3}]
        self.assertEqual(ev.named(entries, 0.5), {"Heist"})
        self.assertEqual(ev.named(entries, 0.0), {"Heist", "Caper"})


class Scoring(unittest.TestCase):
    def test_a_perfect_prediction_scores_one(self):
        g = golden([title(1, "Crime", subgenres=["Heist"], moods=["Tense"])])
        l = labels([record(1, "Crime", subgenres=[("Heist", 0.9)], moods=[("Tense", 0.9)])])
        counts, evaluated, missing = ev.evaluate(g, l)
        self.assertEqual((evaluated, missing), (1, 0))
        for family in ("primaryGenre", "subgenre", "mood"):
            micro, macro, scored = ev.family_scores(counts[family], min_support=1)
            self.assertEqual((micro, macro, scored), (1.0, 1.0, 1), family)

    def test_themes_and_subgenres_are_scored_as_one_family(self):
        """The published taxonomy folds themes into subgenres and has no `themes` field, so a golden theme
        must be expected among the predicted subgenres — not counted as a miss."""
        g = golden([title(1, "Crime", subgenres=["Heist"], themes=["Cyberpunk"])])
        l = labels([record(1, "Crime", subgenres=[("Heist", 0.9), ("Cyberpunk", 0.8)])])
        counts, _, _ = ev.evaluate(g, l)
        micro, _, _ = ev.family_scores(counts["subgenre"], min_support=1)
        self.assertEqual(micro, 1.0)

    def test_a_missing_prediction_is_a_false_negative_not_a_skip(self):
        g = golden([title(1, "Crime", moods=["Tense", "Bleak"])])
        l = labels([record(1, "Crime", moods=[("Tense", 0.9)])])
        counts, _, _ = ev.evaluate(g, l)
        self.assertEqual(counts["mood"]["Bleak"], [0, 0, 1], "tp, fp, fn")

    def test_a_title_absent_from_the_labels_is_counted_as_missing_not_wrong(self):
        """A title the corpus does not hold has no prediction to be wrong about. Counting it as a failure
        would make a SMALLER corpus score worse rather than cover less, which is the opposite of true."""
        g = golden([title(1, "Crime"), title(2, "Drama")])
        counts, evaluated, missing = ev.evaluate(g, labels([record(1, "Crime")]))
        self.assertEqual((evaluated, missing), (1, 1))
        micro, _, _ = ev.family_scores(counts["primaryGenre"], min_support=1)
        self.assertEqual(micro, 1.0, "the one title it could score was right")


class Support(unittest.TestCase):
    def test_support_counts_golden_positives_not_predictions(self):
        """Support is `tp + fn`. Using `tp + fp` would let a classifier that predicts a label EVERYWHERE
        inflate its own support past the threshold and be scored on a label the golden set barely has."""
        g = golden([title(i, "Crime") for i in range(1, 21)])
        # "Heist" predicted on all 20, genuinely on none.
        l = labels([record(i, "Crime", subgenres=[("Heist", 0.9)]) for i in range(1, 21)])
        counts, _, _ = ev.evaluate(g, l)
        _, _, scored = ev.family_scores(counts["subgenre"], min_support=10)
        self.assertEqual(scored, 0, "20 false positives are not 20 support")

    def test_a_sparse_label_is_dropped_before_it_swings_the_macro_mean(self):
        g = golden([title(1, "Crime", moods=["Cozy"])] + [title(i, "Crime", moods=["Tense"])
                                                          for i in range(2, 22)])
        l = labels([record(1, "Crime", moods=[("Wrong", 0.9)])]
                   + [record(i, "Crime", moods=[("Tense", 0.9)]) for i in range(2, 22)])
        counts, _, _ = ev.evaluate(g, l)
        _, macro, scored = ev.family_scores(counts["mood"], min_support=10)
        self.assertEqual(scored, 1, "only Tense has 10+ golden positives")
        self.assertEqual(macro, 1.0, "the 1-title Cozy miss does not halve the mean")


class Vacuity(unittest.TestCase):
    """The ways a gate certifies nothing while reporting success."""

    def test_a_family_with_nothing_to_score_reports_zero_labels(self):
        """With no labels above support, both F1s are vacuously 1.0 — every denominator is empty. The
        caller turns a zero count into a FAILURE for exactly that reason; here we pin that it is visible."""
        g = golden([title(1, "Crime")])
        counts, _, _ = ev.evaluate(g, labels([record(1, "Crime")]))
        micro, macro, scored = ev.family_scores(counts["mood"], min_support=10)
        self.assertEqual((micro, macro, scored), (0.0, 0.0, 0))

    def test_no_overlap_at_all_exits_rather_than_reporting_a_perfect_score(self):
        g = golden([title(1, "Crime")])
        with self.assertRaises(SystemExit):
            ev.evaluate(g, labels([record(999, "Crime")]))


def rows(mood_hits):
    """30 titles, all right on primary genre and subgenre, and right on mood for the first `mood_hits`."""
    return [record(i, "Crime", subgenres=[("Heist", 0.9)],
                   moods=[("Tense" if i <= mood_hits else "Wrong", 0.9)]) for i in range(1, 31)]


class Gate(unittest.TestCase):
    """The ratchet: floors are the recorded scores of what ships, and a drop below them is refused."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.golden = os.path.join(self.dir, "golden.json")
        self.floors = os.path.join(self.dir, "floors.json")
        with open(self.golden, "w") as fh:
            json.dump(golden([title(i, "Crime", moods=["Tense"], subgenres=["Heist"])
                              for i in range(1, 31)]), fh)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def _run(self, labels_rows, extra=()):
        import contextlib, io, sys
        lpath = os.path.join(self.dir, "labels.json")
        with open(lpath, "w") as fh:
            json.dump({"taxonomyVersion": "t02", "records": labels_rows}, fh)
        argv = sys.argv
        sys.argv = ["eval-taxonomy.py", lpath, "--golden", self.golden, "--floors", self.floors, *extra]
        err, out = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                code = ev.main()
        finally:
            sys.argv = argv
        return code, err.getvalue()

    def test_the_labels_the_floors_were_recorded_on_pass(self):
        """The case the old fixed floors failed: what ships must pass its own floors, or the gate can only
        ever run as a report."""
        self.assertEqual(self._run(rows(20), extra=["--record"])[0], 0)
        code, err = self._run(rows(20), extra=["--gate"])
        self.assertEqual(code, 0, err)

    def test_a_drop_below_the_recorded_floor_fails_the_gate(self):
        self._run(rows(20), extra=["--record"])
        code, err = self._run(rows(19), extra=["--gate"])
        self.assertEqual(code, 1)
        self.assertIn("mood microF1", err)
        self.assertIn("--record", err, "the refusal says how to accept a deliberate drop")

    def test_an_improvement_passes_and_says_the_floors_can_rise(self):
        self._run(rows(20), extra=["--record"])
        code, err = self._run(rows(25), extra=["--gate"])
        self.assertEqual(code, 0, err)
        self.assertIn("raise them", err)

    def test_the_record_names_the_labels_and_the_date(self):
        self._run(rows(20), extra=["--record"])
        with open(self.floors) as fh:
            recorded = json.load(fh)
        with open(os.path.join(self.dir, "labels.json"), "rb") as fh:
            import hashlib
            self.assertEqual(recorded["labelsSha256"], hashlib.sha256(fh.read()).hexdigest())
        self.assertRegex(recorded["measuredOn"], r"^\d{4}-\d{2}-\d{2}$")

    def test_floors_from_another_golden_set_are_not_compared(self):
        """Another golden set scores the same labels differently, so passing or failing against its floors
        would be about the golden set, not the labels."""
        self._run(rows(20), extra=["--record"])
        with open(self.golden, "w") as fh:
            json.dump(golden([title(i, "Crime", moods=["Tense"], subgenres=["Heist"])
                              for i in range(1, 32)]), fh)
        code, err = self._run(rows(20), extra=["--gate"])
        self.assertEqual(code, 1)
        self.assertIn("goldenSha256", err)

    def test_floors_are_not_recorded_from_a_run_with_nothing_to_score(self):
        """With no label above support every F1 is vacuous; recorded, it would certify anything."""
        code, err = self._run(rows(20), extra=["--record", "--min-support", "100"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.floors))

    def test_without_gate_a_failure_reports_but_does_not_block(self):
        self._run(rows(20), extra=["--record"])
        code, err = self._run(rows(10))
        self.assertEqual(code, 0)
        self.assertIn("below the floors", err)


class CommittedFloors(unittest.TestCase):
    """`data/eval/quality-floors.json` is what the publisher gates on, so it must be one `--gate` can read
    and must still describe the committed golden set."""

    def test_the_committed_floors_describe_the_committed_golden_set(self):
        with open(ev.FLOORS) as fh:
            recorded = json.load(fh)
        self.assertEqual(recorded["goldenSha256"],
                         ev.sha256(os.path.join(HERE, "..", "data", "eval", "golden-large.json")),
                         "the golden set changed: re-record the floors against it")
        self.assertEqual(recorded["minSupport"], ev.MIN_SUPPORT)
        for family in ev.FAMILIES:
            for metric in ("microF1", "macroF1"):
                self.assertGreater(recorded["floors"][family][metric], 0.0, (family, metric))


if __name__ == "__main__":
    unittest.main()
