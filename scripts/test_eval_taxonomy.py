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

    def test_the_curated_file_shape_is_read(self):
        """`data/genres-moods-curated.json` keys its titles `mediaType:tmdbId`; a series and a film sharing
        a tmdbId stay two titles."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"taxonomyVersion": "t02", "titles": {
                "movie:95": {"primaryGenre": "Action", "subgenres": [], "moods": []},
                "tv:95": {"primaryGenre": "Horror", "subgenres": [], "moods": []}}}, fh)
        try:
            version, got = ev.labels_by_key(fh.name)
        finally:
            os.unlink(fh.name)
        self.assertEqual(version, "t02")
        self.assertEqual({k: r["primaryGenre"] for k, r in got.items()},
                         {("movie", 95): "Action", ("tv", 95): "Horror"})

    def test_a_confidence_floor_drops_the_labels_under_it(self):
        entries = [{"label": "Heist", "confidence": 0.9}, {"label": "Caper", "confidence": 0.3}]
        self.assertEqual(ev.named(entries, 0.5), {"Heist"})
        self.assertEqual(ev.named(entries, 0.0), {"Heist", "Caper"})


class Scoring(unittest.TestCase):
    def test_a_perfect_prediction_scores_one(self):
        g = golden([title(1, "Crime", subgenres=["Heist"], moods=["Tense"])])
        l = labels([record(1, "Crime", subgenres=[("Heist", 0.9)], moods=[("Tense", 0.9)])])
        counts, evaluated, missing, _ = ev.evaluate(g, l)
        self.assertEqual((evaluated, missing), (1, 0))
        for family in ("primaryGenre", "subgenre", "mood"):
            micro, macro, scored = ev.family_scores(counts[family], min_support=1)
            self.assertEqual((micro, macro, scored), (1.0, 1.0, 1), family)

    def test_themes_and_subgenres_are_scored_as_one_family(self):
        """The published taxonomy folds themes into subgenres and has no `themes` field, so a golden theme
        must be expected among the predicted subgenres — not counted as a miss."""
        g = golden([title(1, "Crime", subgenres=["Heist"], themes=["Cyberpunk"])])
        l = labels([record(1, "Crime", subgenres=[("Heist", 0.9), ("Cyberpunk", 0.8)])])
        counts, *_ = ev.evaluate(g, l)
        micro, _, _ = ev.family_scores(counts["subgenre"], min_support=1)
        self.assertEqual(micro, 1.0)

    def test_a_missing_prediction_is_a_false_negative_not_a_skip(self):
        g = golden([title(1, "Crime", moods=["Tense", "Bleak"])])
        l = labels([record(1, "Crime", moods=[("Tense", 0.9)])])
        counts, *_ = ev.evaluate(g, l)
        self.assertEqual(counts["mood"]["Bleak"], [0, 0, 1], "tp, fp, fn")

    def test_a_title_absent_from_the_labels_is_counted_as_missing_not_wrong(self):
        """A title the corpus does not hold has no prediction to be wrong about. Counting it as a failure
        would make a SMALLER corpus score worse rather than cover less, which is the opposite of true."""
        g = golden([title(1, "Crime"), title(2, "Drama")])
        counts, evaluated, missing, _ = ev.evaluate(g, labels([record(1, "Crime")]))
        self.assertEqual((evaluated, missing), (1, 1))
        micro, _, _ = ev.family_scores(counts["primaryGenre"], min_support=1)
        self.assertEqual(micro, 1.0, "the one title it could score was right")


class BlankGoldenEntries(unittest.TestCase):
    """23% of the golden set names no subgenres, and 23% names no moods. Those blanks are the labeller
    leaving a family unanswered, not a verdict that nothing applies — so scoring a prediction against them
    can only ever record false positives, and every label the producer adds there is wrong by
    construction."""

    def test_a_family_the_golden_entry_leaves_blank_does_not_punish_a_prediction(self):
        g = golden([title(1, "Western", moods=["Bleak"])])  # no subgenres and no themes
        l = labels([record(1, "Western", subgenres=[("Historical/Period Drama", 0.9)],
                           moods=[("Bleak", 0.9)])])
        counts, _, _, scored = ev.evaluate(g, l)
        self.assertEqual(dict(counts["subgenre"]), {}, "nothing to be right or wrong about")
        self.assertEqual(scored["subgenre"], 0)
        self.assertEqual(scored["mood"], 1, "the family the golden entry DOES answer still counts")

    def test_the_old_maths_is_what_punished_it(self):
        """Without the rule the same prediction is a false positive against an empty list — which is how
        the derive that filled 2,285 curated titles came out 0.0022 'worse'."""
        g = golden([title(1, "Western", moods=["Bleak"])])
        l = labels([record(1, "Western", subgenres=[("Historical/Period Drama", 0.9)])])
        counts, _, _, scored = ev.evaluate(g, l, score_blank_golden=True)
        self.assertEqual(counts["subgenre"]["Historical/Period Drama"], [0, 1, 0], "tp, fp, fn")
        self.assertEqual(scored["subgenre"], 1)

    def test_a_blank_golden_family_does_not_hide_a_missing_prediction_elsewhere(self):
        """Skipping is per family per title, not per title: the mood the golden set does name is still
        scored, and still missed."""
        g = golden([title(1, "Western", moods=["Bleak"])])
        counts, _, _, _ = ev.evaluate(g, labels([record(1, "Western")]))
        self.assertEqual(counts["mood"]["Bleak"], [0, 0, 1], "tp, fp, fn")

    def test_a_primary_genre_is_scored_even_when_the_record_has_none(self):
        """`primaryGenre` is one label, never blank in the golden set, and a record that has lost it must
        score as wrong rather than be skipped."""
        g = golden([title(1, "Crime")])
        counts, _, _, scored = ev.evaluate(g, labels([{"tmdbId": 1, "mediaType": "movie"}]))
        self.assertEqual(scored["primaryGenre"], 1)
        self.assertEqual(counts["primaryGenre"]["Crime"], [0, 0, 1], "tp, fp, fn")


class Support(unittest.TestCase):
    def test_support_counts_golden_positives_not_predictions(self):
        """Support is `tp + fn`. Using `tp + fp` would let a classifier that predicts a label EVERYWHERE
        inflate its own support past the threshold and be scored on a label the golden set barely has."""
        g = golden([title(i, "Crime", subgenres=["Prison"]) for i in range(1, 21)])
        # "Heist" predicted on all 20, genuinely on none.
        l = labels([record(i, "Crime", subgenres=[("Prison", 0.9), ("Heist", 0.9)])
                    for i in range(1, 21)])
        counts, *_ = ev.evaluate(g, l)
        _, _, scored = ev.family_scores(counts["subgenre"], min_support=10)
        self.assertEqual(scored, 1, "20 false positives are not 20 support; only Prison is scored")

    def test_a_sparse_label_is_dropped_before_it_swings_the_macro_mean(self):
        g = golden([title(1, "Crime", moods=["Cozy"])] + [title(i, "Crime", moods=["Tense"])
                                                          for i in range(2, 22)])
        l = labels([record(1, "Crime", moods=[("Wrong", 0.9)])]
                   + [record(i, "Crime", moods=[("Tense", 0.9)]) for i in range(2, 22)])
        counts, *_ = ev.evaluate(g, l)
        _, macro, scored = ev.family_scores(counts["mood"], min_support=10)
        self.assertEqual(scored, 1, "only Tense has 10+ golden positives")
        self.assertEqual(macro, 1.0, "the 1-title Cozy miss does not halve the mean")


class Vacuity(unittest.TestCase):
    """The ways a gate certifies nothing while reporting success."""

    def test_a_family_with_nothing_to_score_reports_zero_labels(self):
        """With no labels above support, both F1s are vacuously 1.0 — every denominator is empty. The
        caller turns a zero count into a FAILURE for exactly that reason; here we pin that it is visible."""
        g = golden([title(1, "Crime", moods=["Tense"])])
        counts, _, _, _ = ev.evaluate(g, labels([record(1, "Crime", moods=[("Tense", 0.9)])]))
        micro, macro, scored = ev.family_scores(counts["mood"], min_support=10)
        self.assertEqual((micro, macro, scored), (0.0, 0.0, 0))

    def test_no_overlap_at_all_exits_rather_than_reporting_a_perfect_score(self):
        g = golden([title(1, "Crime")])
        with self.assertRaises(SystemExit):
            ev.evaluate(g, labels([record(999, "Crime")]))


def rows(mood_hits, n=400):
    """`n` titles, all right on primary genre and subgenre, and right on mood for the first `mood_hits`.

    400, so that one title is a quarter of the 0.005 tolerance: a fixture of 30 could not express a drop
    small enough to be inside the band.
    """
    return [record(i, "Crime", subgenres=[("Heist", 0.9)],
                   moods=[("Tense" if i <= mood_hits else "Wrong", 0.9)]) for i in range(1, n + 1)]


class Gate(unittest.TestCase):
    """The ratchet: the baseline is the recorded score of what ships, the tolerance is a band under it,
    and the baseline moves only when an operator records deliberately."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.golden = os.path.join(self.dir, "golden.json")
        self.floors = os.path.join(self.dir, "floors.json")
        with open(self.golden, "w") as fh:
            json.dump(golden([title(i, "Crime", moods=["Tense"], subgenres=["Heist"])
                              for i in range(1, 401)]), fh)

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

    def baseline(self):
        with open(self.floors) as fh:
            return json.load(fh)["baseline"]

    def test_the_labels_the_baseline_was_recorded_on_pass(self):
        """The case the old fixed floors failed: what ships must pass its own baseline, or the gate can
        only ever run as a report."""
        self.assertEqual(self._run(rows(380), extra=["--record"])[0], 0)
        code, err = self._run(rows(380), extra=["--gate"])
        self.assertEqual(code, 0, err)

    def test_a_drop_inside_the_tolerance_passes_and_is_still_reported(self):
        """0.0026 under — the size of the genres & moods fill, or a rule-threshold nudge. Refusing it is
        what made the gate fire on movement rather than on quality."""
        self._run(rows(380), extra=["--record"])
        code, err = self._run(rows(378), extra=["--gate"])
        self.assertEqual(code, 0, err)
        self.assertIn("within the 0.005 tolerance", err, "a drop nobody is told about is how a band slides")

    def test_a_drop_past_the_tolerance_fails_the_gate(self):
        self._run(rows(380), extra=["--record"])
        code, err = self._run(rows(375), extra=["--gate"])
        self.assertEqual(code, 1)
        self.assertIn("mood microF1", err)
        self.assertIn("tolerance", err)
        self.assertIn("--accept-drop", err, "the refusal says how to accept a deliberate drop")

    def test_the_tolerance_is_a_band_around_the_baseline_not_a_step_downhill(self):
        """The whole reason the baseline is recorded separately. If the gate compared against the last
        RUN, two passes 0.0026 under each other would walk the scores down 0.0052 with nothing refused."""
        self._run(rows(380), extra=["--record"])
        before = self.baseline()
        self.assertEqual(self._run(rows(378), extra=["--gate"])[0], 0)
        self.assertEqual(self.baseline(), before, "a passing run must not re-record the baseline")
        code, err = self._run(rows(376), extra=["--gate"])
        self.assertEqual(code, 1, "a second step of the same size is measured from the same baseline")

    def test_an_improvement_passes_and_says_the_baseline_can_rise(self):
        self._run(rows(380), extra=["--record"])
        code, err = self._run(rows(390), extra=["--gate"])
        self.assertEqual(code, 0, err)
        self.assertIn("raise it", err)

    def test_the_baseline_ratchets_up_on_an_improvement(self):
        self._run(rows(380), extra=["--record"])
        before = self.baseline()["mood"]["microF1"]
        self.assertEqual(self._run(rows(390), extra=["--record"])[0], 0, "no --accept-drop needed to rise")
        self.assertGreater(self.baseline()["mood"]["microF1"], before)

    def test_record_refuses_to_lower_the_baseline_without_accept_drop(self):
        """Otherwise the tolerance is a slide: record after every run and the reference point follows the
        scores down, a fraction at a time, with no diff that says a drop was accepted."""
        self._run(rows(380), extra=["--record"])
        before = self.baseline()
        code, err = self._run(rows(378), extra=["--record"])
        self.assertEqual(code, ev.WOULD_LOWER, "its own code, so a caller can tell it from a write error")
        self.assertIn("would lower it", err)
        self.assertIn("--accept-drop", err)
        self.assertEqual(self.baseline(), before, "the baseline is untouched by a refused record")

    def test_record_accept_drop_lowers_the_baseline(self):
        self._run(rows(380), extra=["--record"])
        before = self.baseline()["mood"]["microF1"]
        self.assertEqual(self._run(rows(378), extra=["--record", "--accept-drop"])[0], 0)
        self.assertLess(self.baseline()["mood"]["microF1"], before)

    def test_the_record_names_the_labels_and_the_date(self):
        self._run(rows(380), extra=["--record"])
        with open(self.floors) as fh:
            recorded = json.load(fh)
        with open(os.path.join(self.dir, "labels.json"), "rb") as fh:
            import hashlib
            self.assertEqual(recorded["labelsSha256"], hashlib.sha256(fh.read()).hexdigest())
        self.assertRegex(recorded["measuredOn"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(recorded["tolerance"], ev.TOLERANCE)
        self.assertEqual(recorded["scoringRule"], ev.SCORING_RULE)

    def test_a_baseline_from_another_golden_set_is_not_compared(self):
        """Another golden set scores the same labels differently, so passing or failing against its
        baseline would be about the golden set, not the labels."""
        self._run(rows(380), extra=["--record"])
        with open(self.golden, "w") as fh:
            json.dump(golden([title(i, "Crime", moods=["Tense"], subgenres=["Heist"])
                              for i in range(1, 402)]), fh)
        code, err = self._run(rows(380), extra=["--gate"])
        self.assertEqual(code, 1)
        self.assertIn("goldenSha256", err)

    def test_a_baseline_recorded_under_other_scoring_maths_is_not_compared(self):
        """These scores were measured with blank golden families skipped. Comparing them with a baseline
        taken before that rule would pass on the change of ruler — by 0.03 to 0.06, on the real data."""
        self._run(rows(380), extra=["--record"])
        with open(self.floors) as fh:
            recorded = json.load(fh)
        recorded["scoringRule"] = "blank-golden-families-scored"
        with open(self.floors, "w") as fh:
            json.dump(recorded, fh)
        code, err = self._run(rows(380), extra=["--gate"])
        self.assertEqual(code, 1)
        self.assertIn("scoringRule", err)

    def test_a_baseline_is_not_recorded_from_a_run_with_nothing_to_score(self):
        """With no label above support every F1 is vacuous; recorded, it would certify anything."""
        code, err = self._run(rows(380), extra=["--record", "--min-support", "1000"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.floors))

    def test_without_gate_a_failure_reports_but_does_not_block(self):
        self._run(rows(380), extra=["--record"])
        code, err = self._run(rows(200))
        self.assertEqual(code, 0)
        self.assertIn("below the baseline", err)

    def test_a_mangled_prediction_is_still_refused(self):
        """The tolerance must not become a hole. Shuffling the mood a title is given — the labels are all
        still there, in the right numbers, on the wrong titles — is a regression the gate has to catch."""
        import random
        moods = ["Tense", "Cozy"]
        with open(self.golden, "w") as fh:
            json.dump(golden([title(i, "Crime", subgenres=["Heist"], moods=[moods[i % 2]])
                              for i in range(1, 401)]), fh)
        good = [record(i, "Crime", subgenres=[("Heist", 0.9)], moods=[(moods[i % 2], 0.9)])
                for i in range(1, 401)]
        self.assertEqual(self._run(good, extra=["--record"])[0], 0)
        mangled = [dict(r) for r in good]
        picked = [r["moods"] for r in mangled]
        random.Random(0).shuffle(picked)
        for row, mood in zip(mangled, picked):
            row["moods"] = mood
        code, err = self._run(mangled, extra=["--gate"])
        self.assertEqual(code, 1)
        self.assertIn("mood microF1", err)

    def test_a_family_the_golden_set_barely_answers_is_refused_rather_than_scored_thin(self):
        """Skipping blank golden families removes a penalty, so it needs the same hollowing-out guard the
        corpus coverage has: a golden set that stopped naming moods would otherwise score a handful of
        titles perfectly and certify the family."""
        titles = [title(i, "Crime", subgenres=["Heist"], moods=["Tense"] if i <= 40 else [])
                  for i in range(1, 401)]
        with open(self.golden, "w") as fh:
            json.dump(golden(titles), fh)
        code, err = self._run(rows(400), extra=["--record"])
        self.assertEqual(code, 1)
        self.assertIn("mood scored only 40 of 400", err)


class CommittedFloors(unittest.TestCase):
    """`data/eval/quality-floors.json` is what the publisher gates on, so it must be one `--gate` can read
    and must still describe the committed golden set."""

    def test_the_committed_baseline_describes_the_committed_golden_set(self):
        with open(ev.FLOORS) as fh:
            recorded = json.load(fh)
        self.assertEqual(recorded["goldenSha256"],
                         ev.sha256(os.path.join(HERE, "..", "data", "eval", "golden-large.json")),
                         "the golden set changed: re-record the baseline against it")
        self.assertEqual(recorded["minSupport"], ev.MIN_SUPPORT)
        self.assertEqual(recorded["scoringRule"], ev.SCORING_RULE,
                         "the scoring maths changed: re-record the baseline under it")
        self.assertGreater(recorded["tolerance"], 0.0)
        for family in ev.FAMILIES:
            for metric in ("microF1", "macroF1"):
                self.assertGreater(recorded["baseline"][family][metric], 0.0, (family, metric))

    def test_the_committed_genres_and_moods_are_what_the_baseline_was_recorded_on_and_pass_it(self):
        """The curated file is committed, so unlike the release asset it can be gated here: an edit to it
        that lowers a score fails CI instead of the next publish."""
        import contextlib, io, sys
        curated = os.path.join(HERE, "..", "data", "genres-moods-curated.json")
        with open(ev.FLOORS) as fh:
            recorded = json.load(fh)
        self.assertEqual(recorded["labelsSha256"], ev.sha256(curated),
                         "genres-moods-curated.json changed: gate it, then --record the baseline "
                         "against it")
        argv = sys.argv
        sys.argv = ["eval-taxonomy.py", curated, "--golden",
                    os.path.join(HERE, "..", "data", "eval", "golden-large.json"), "--gate"]
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = ev.main()
        finally:
            sys.argv = argv
        self.assertEqual(code, 0, err.getvalue())


if __name__ == "__main__":
    unittest.main()
