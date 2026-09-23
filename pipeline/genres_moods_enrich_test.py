"""`./den genres-moods prepare|merge` against a fixture out-dir, curated file, golden set and floors.

No network and no agent: the answers an agent would write are written here. The vocabulary is the real
`data/genres-moods-vocabulary.json` and `data/genres-moods-definitions.json`, so a change to either
reaches these tests.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from . import genres_moods_enrich as gm
from . import genres_moods_merge as gmm
from .article_sections import parse_sections
from .contract import StageError

HERE = os.path.dirname(os.path.abspath(__file__))

DEN = os.path.join(os.path.dirname(HERE), "den")
TODAY = "2026-09-22"
TEXT = "{t} is a film.\n\n== Plot ==\nA thief plans one last job.\n\n== Reception ==\nCritics were kind.\n"
GOOD = {"primary_genre": "Crime", "subgenres": [{"label": "Heist", "confidence": 0.9}],
        "moods": [{"label": "Tense/Edge-of-seat", "confidence": 0.8}]}


def entry(primary, subgenres=(), moods=(), source="july-relabel", animated=False):
    return {"animated": animated, "moods": [{"confidence": c, "label": l} for l, c in moods],
            "primaryGenre": primary, "primaryGenreSource": source, "source": source,
            "subgenres": [{"confidence": c, "label": l} for l, c in subgenres]}


class Fixture(unittest.TestCase):
    """An out-dir with an article dump, one classify shard and one enrichment batch; a curated file of 30
    gated titles plus the ones under test; a golden set over the gated titles and movie:2; recorded floors.

    movie:1 is labelled, movie:2 is curated with neither subgenres nor moods, movie:3 is new (and
    animated per its batch row); movie:4 is about another work, movie:5 has no article, movie:6's article changed
    since classify, movie:7 is new with no enrichment row, tv:8 is new and from 2025.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.out = os.path.join(self.dir, "out")
        os.makedirs(os.path.join(self.out, "enriched"))
        years = {"movie:1": 1990, "movie:2": 1995, "movie:3": 2001, "movie:4": 2003, "movie:5": 2004,
                 "movie:6": 2010, "movie:7": 2011, "tv:8": 2025}
        with open(os.path.join(self.out, "articles.jsonl"), "w") as arts, \
                open(os.path.join(self.out, "combined-v1-r2.jsonl"), "w") as cls:
            for key, year in years.items():
                media, tmdb_id = key.split(":")
                text = TEXT.format(t=f"T{tmdb_id}")
                sections = []
                for s in parse_sections(text):
                    role = {"Lead": "work-context", "Plot": "story-premise"}.get(s["heading"],
                                                                                "irrelevant-production-reception-navigation")
                    sections.append({"id": s["id"], "heading": s["heading"], "textSha256": s["textSha256"],
                                     "role": {"probabilities": {role: 1.0}}})
                validity = "other-screen-work" if key == "movie:4" else "correct-screen-work"
                cls.write(json.dumps({"mediaType": media, "tmdbId": int(tmdb_id), "title": f"T{tmdb_id}",
                                      "year": year, "sections": sections,
                                      "answers": {"validity": {"choice": validity}}}) + "\n")
                if key != "movie:5":
                    body = text.replace("last job", "final job") if key == "movie:6" else text
                    arts.write(json.dumps({"mediaType": media, "tmdbId": int(tmdb_id), "title": f"T{tmdb_id}",
                                           "article": f"T {tmdb_id}", "language": "en", "text": body}) + "\n")
        with open(os.path.join(self.out, "enriched", "batch-1.json"), "w") as fh:
            json.dump([{"mediaType": "movie", "tmdbId": 3, "animated": True},
                       {"mediaType": "tv", "tmdbId": 8, "animated": False}], fh)
        gated = [f"movie:{100 + i}" for i in range(30)]
        # movie:1 has subgenres and no moods: labelled, so not "missing".
        titles = {"movie:1": entry("Drama", [("Prison", 0.7)]),
                  "movie:2": entry("Crime", animated=True)}
        titles.update({k: entry("Crime", [("Heist", 0.9)], [("Tense/Edge-of-seat", 0.9)]) for k in gated})
        head = {"_": "fixture", "count": len(titles), "sources": {"july-relabel": "July."},
                "taxonomyVersion": "t02"}
        self.curated = os.path.join(self.dir, "curated.json")
        with open(self.curated, "w") as fh:
            fh.write(gmm.dump_curated(head, titles))
        self.golden = os.path.join(self.dir, "golden.json")
        with open(self.golden, "w") as fh:
            json.dump({"taxonomyVersion": "t02", "titles": [
                {"mediaType": "movie", "tmdbId": int(k.split(":")[1]), "primaryGenre": "Crime",
                 "subgenres": ["Heist"], "moods": ["Tense/Edge-of-seat"]} for k in gated + ["movie:2"]]}, fh)
        self.floors = os.path.join(self.dir, "floors.json")
        recorded = gmm.run_eval(self.curated, self.golden, self.floors, "--record")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.work = os.path.join(self.dir, "work")

    def prepare(self, **kw):
        lines = []
        record = gm.prepare(out_dir=self.out, work=self.work, curated=self.curated, today=TODAY,
                            out=lines.append, **kw)
        return record, "\n".join(lines)

    def answer(self, overrides=None, drop=(), sources=None):
        """Write every prepared batch's answers: GOOD for each key unless `overrides` names it."""
        with open(os.path.join(self.work, "prepare.json")) as fh:
            batches = json.load(fh)["batches"]
        for name, keys in batches.items():
            if name in drop:
                continue
            with open(os.path.join(self.work, "out", name), "w") as fh:
                json.dump([{"key": k, **(overrides or {}).get(k, GOOD)} for k in keys], fh)
            if sources is not None:
                with open(os.path.join(self.work, "out", name[:-5] + ".sources.json"), "w") as fh:
                    json.dump({k: sources.get(k, ["https://example.org/review"]) for k in keys}, fh)

    def merge(self, **kw):
        lines = []
        result = gmm.merge(self.work, kw.pop("model", "sonnet"), curated=self.curated, golden=self.golden,
                           floors=self.floors, today=TODAY, out=lines.append, **kw)
        return result, "\n".join(lines)

    def read(self, path=None):
        with open(path or self.curated, "rb") as fh:
            return fh.read()


class Selection(Fixture):
    def test_missing_takes_new_titles_and_empty_records_and_says_why_it_skipped_the_rest(self):
        record, printed = self.prepare()
        self.assertEqual([k for ks in record["batches"].values() for k in ks], ["movie:2", "movie:3", "tv:8"])
        for reason, key in (("not about the requested work (other-screen-work)", "movie:4"),
                            ("no article", "movie:5"), ("article changed", "movie:6"),
                            ("no enrichment row", "movie:7")):
            self.assertRegex(printed, rf"skipped 1: [^\n]*{re.escape(reason)}[^\n]*{key}")
        self.assertEqual(record["animated"], {"movie:3": True, "tv:8": False})

    def test_keys_takes_exactly_the_listed_titles(self):
        path = os.path.join(self.dir, "keys.txt")
        with open(path, "w") as fh:
            fh.write("movie:1\n\ntv:8\nmovie:1\n")
        record, _ = self.prepare(mode="keys", keys_file=path)
        self.assertEqual([k for ks in record["batches"].values() for k in ks], ["movie:1", "tv:8"])

    def test_keys_refuses_a_line_that_is_not_a_key(self):
        path = os.path.join(self.dir, "keys.txt")
        with open(path, "w") as fh:
            fh.write("movie:1\nThe Matrix\n")
        with self.assertRaisesRegex(StageError, "not mediaType:tmdbId"):
            self.prepare(mode="keys", keys_file=path)

    def test_since_takes_titles_released_in_that_year_or_later(self):
        record, _ = self.prepare(mode="since", since="2025-06-01")
        self.assertEqual([k for ks in record["batches"].values() for k in ks], ["tv:8"])
        with self.assertRaisesRegex(StageError, "YYYY-MM-DD"):
            gm.prepare(out_dir=self.out, work=os.path.join(self.dir, "w2"), mode="since", since="2025",
                       curated=self.curated, out=lambda _: None)

    def rerun(self, key, validity, started):
        """A later classify run answering `key` again, as a re-grounded title gets, with its manifest."""
        first = os.path.join(self.out, "combined-v1-r2.jsonl")
        with open(first + ".manifest.json", "w") as fh:
            json.dump({"runStartedAt": "2026-09-01T00:00:00Z"}, fh)
        with open(first) as fh:
            row = next(json.loads(line) for line in fh if json.loads(line)["tmdbId"] == int(key.split(":")[1]))
        row["answers"]["validity"]["choice"] = validity
        later = os.path.join(self.out, "combined-v1-r2-reground.jsonl")
        with open(later, "w") as fh:
            fh.write(json.dumps(row) + "\n")
        if started is not None:
            with open(later + ".manifest.json", "w") as fh:
                json.dump({"runStartedAt": started}, fh)

    def test_a_title_answered_twice_with_no_order_between_the_runs_is_refused(self):
        """Two runs answer movie:1 and the second records no start, so nothing says which is later."""
        self.rerun("movie:1", "correct-screen-work", started=None)
        with self.assertRaisesRegex(StageError, "duplicate key across classify shards: movie:1"):
            self.prepare()

    def test_a_later_run_answers_for_the_title(self):
        """movie:4's first run judged its article another work; a re-run on its own article says it is the
        requested one, so the later run decides — as the corpus join reads it. (The fixture has no
        enrichment row for movie:4, so it is still skipped, now for that reason instead.)"""
        self.rerun("movie:4", "correct-screen-work", started="2026-09-23T00:00:00Z")
        _, printed = self.prepare()
        self.assertNotRegex(printed, r"not about the requested work[^\n]*movie:4")
        self.assertRegex(printed, r"no enrichment row[^\n]*movie:4")

    def test_a_withdrawn_title_is_not_prepared(self):
        with open(os.path.join(self.out, "combined-v1-r2.jsonl.manifest.json"), "w") as fh:
            json.dump({"runStartedAt": "2026-09-01T00:00:00Z"}, fh)
        with open(os.path.join(self.out, "withdrawn.jsonl"), "w") as fh:
            fh.write(json.dumps({"mediaType": "movie", "tmdbId": 3, "reason": "lost its plot",
                                 "withdrawnAt": "2026-09-23T00:00:00Z"}) + "\n")
        record, _ = self.prepare()
        self.assertNotIn("movie:3", [k for ks in record["batches"].values() for k in ks])

    def test_a_prepared_work_dir_is_not_prepared_over(self):
        self.prepare()
        with self.assertRaisesRegex(StageError, "already holds"):
            self.prepare()


class BatchFormat(Fixture):
    def test_each_item_carries_the_premise_sections_and_nothing_else(self):
        self.prepare(batch=2)
        self.assertEqual(sorted(os.listdir(os.path.join(self.work, "in"))), ["batch-001.json", "batch-002.json"])
        with open(os.path.join(self.work, "in", "batch-001.json")) as fh:
            first = json.load(fh)[0]
        self.assertEqual(set(first), {"key", "title", "year", "media", "articleLanguage", "articleUrl", "premise"})
        self.assertEqual((first["key"], first["media"], first["year"]), ("movie:2", "film", 1995))
        self.assertEqual(first["articleUrl"], "https://en.wikipedia.org/wiki/T_2")
        self.assertEqual(first["premise"], "T2 is a film.\n\n## Plot\nA thief plans one last job.")

    def test_the_spec_defines_the_whole_vocabulary(self):
        self.prepare()
        with open(os.path.join(self.work, "SPEC.md")) as fh:
            spec = fh.read()
        vocab = gm.vocabulary()
        self.assertNotIn("{vocabulary}", spec)
        for family, labels in (("primaryGenres", vocab["primary"]), ("subgenres", vocab["sub"] + vocab["theme"]),
                               ("moods", vocab["mood"])):
            for label in labels:
                self.assertIn(f"- **{label}**: {vocab['definitions'][family][label]}", spec)

    def test_definitions_that_do_not_match_the_taxonomy_are_refused(self):
        with open(gm.DEFINITIONS) as fh:
            defs = json.load(fh)
        del defs["moods"]["Cozy"]
        defs["moods"]["Snug"] = "Invented."
        path = os.path.join(self.dir, "defs.json")
        with open(path, "w") as fh:
            json.dump(defs, fh)
        with self.assertRaisesRegex(StageError, r"undefined \['Cozy'\], unknown \['Snug'\]"):
            gm.vocabulary(definitions_path=path)


class Validator(unittest.TestCase):
    vocab = gm.vocabulary()
    items = [{"key": "movie:1"}, {"key": "movie:2"}]

    def problems(self, **change):
        answers = [{"key": "movie:1", **GOOD}, {"key": "movie:2", **GOOD, **change}]
        return gmm.check_batch(self.items, answers, self.vocab)

    def test_a_good_batch_has_no_problems(self):
        self.assertEqual(self.problems(), [])

    def test_each_refusal(self):
        cases = {
            "not a primary genre": {"primary_genre": "Animation"},
            "not in the subgenres vocabulary": {"subgenres": [{"label": "Cozy", "confidence": 0.9}]},
            "not in the moods vocabulary": {"moods": [{"label": "Heist", "confidence": 0.9}]},
            "not a number in [0, 1]": {"moods": [{"label": "Cozy", "confidence": 1.2}]},
            "at most 3": {"moods": [{"label": m, "confidence": 0.6} for m in
                                    ("Cozy", "Campy", "Tearjerker", "Feel-good")]},
            "repeats a label": {"moods": [{"label": "Cozy", "confidence": 0.6}, {"label": "Cozy", "confidence": 0.7}]},
            "must be exactly {label, confidence}": {"moods": [{"label": "Cozy", "confidence": 0.6, "why": "x"}]},
            "fields must be exactly": {"notes": "x"},
            "must be a list": {"moods": "Cozy"},
        }
        for message, change in cases.items():
            self.assertTrue(any(message in p for p in self.problems(**change)), (message, self.problems(**change)))
        self.assertTrue(any("not a number" in p for p in
                            self.problems(moods=[{"label": "Cozy", "confidence": True}])), "a bool is not a number")

    def test_every_key_exactly_once(self):
        one = [{"key": "movie:1", **GOOD}]
        self.assertIn("movie:2: not answered", gmm.check_batch(self.items, one, self.vocab))
        twice = one * 2 + [{"key": "movie:2", **GOOD}]
        self.assertIn("movie:1: answered 2 times", gmm.check_batch(self.items, twice, self.vocab))
        extra = twice[1:] + [{"key": "movie:9", **GOOD}]
        self.assertIn("movie:9: key not in the input batch", gmm.check_batch(self.items, extra, self.vocab))
        self.assertEqual(gmm.check_batch(self.items, {"movie:1": GOOD}, self.vocab),
                         ["top level must be a JSON array"])

    def test_sources_need_a_url_for_every_key(self):
        self.assertEqual(gmm.check_sources(self.items, {"movie:1": ["https://a.b/c"], "movie:2": ["http://d.e"]}), [])
        for bad in ({"movie:1": ["https://a.b/c"]}, {"movie:1": ["https://a.b/c"], "movie:2": []},
                    {"movie:1": ["https://a.b/c"], "movie:2": ["a review I read"]}):
            self.assertIn("movie:2: sources must list at least one http(s) URL", gmm.check_sources(self.items, bad))
        self.assertTrue(gmm.check_sources(self.items, {**{"movie:1": ["https://x.y"], "movie:2": ["https://x.y"]},
                                                       "movie:9": ["https://x.y"]}))


class ValidateScript(Fixture):
    def test_the_agents_validator_prints_ok_or_the_problems(self):
        self.prepare()
        self.answer()
        run = lambda *a: subprocess.run([sys.executable, "validate.py", *a], cwd=self.work,  # noqa: E731
                                        capture_output=True, text=True)
        good = run("out/batch-001.json")
        self.assertEqual((good.returncode, good.stdout.strip()), (0, "ok"), good.stderr)
        missing = run("--web", "out/batch-001.json")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("batch-001.sources.json: missing", missing.stdout)
        self.answer(overrides={"movie:2": {**GOOD, "primary_genre": "Animation"}})
        bad = run("out/batch-001.json")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("movie:2: primary_genre 'Animation' is not a primary genre", bad.stdout)


class Assemble(unittest.TestCase):
    vocab = gm.vocabulary()

    def test_july_thresholds_per_family_and_top_three(self):
        answer = {"primary_genre": "Crime",
                  "subgenres": [{"label": "Crime Thriller", "confidence": 0.55},   # blended subgenre: kept
                                {"label": "Neo-Noir", "confidence": 0.54},         # blended subgenre: dropped
                                {"label": "Heist", "confidence": 0.50},            # theme: kept
                                {"label": "Prison", "confidence": 0.49}],          # theme: dropped
                  "moods": [{"label": "Cozy", "confidence": 0.55}, {"label": "Campy", "confidence": 0.54}]}
        got = gmm.assemble(answer, self.vocab)
        self.assertEqual(got["subgenres"], [{"confidence": 0.55, "label": "Crime Thriller"},
                                            {"confidence": 0.5, "label": "Heist"}])
        self.assertEqual(got["moods"], [{"confidence": 0.55, "label": "Cozy"}])
        self.assertEqual(got["primaryGenre"], "Crime")

    def test_strongest_first_ties_by_label_three_at_most(self):
        answer = {"primary_genre": "Drama", "moods": [],
                  "subgenres": [{"label": "Prison", "confidence": 0.7}, {"label": "Heist", "confidence": 0.7},
                                {"label": "Biopic", "confidence": 0.9}, {"label": "Sports", "confidence": 0.6}]}
        got = [e["label"] for e in gmm.assemble(answer, self.vocab)["subgenres"]]
        self.assertEqual(got, ["Biopic", "Heist", "Prison"])


class Merge(Fixture):
    def test_provenance_animated_and_the_head(self):
        self.prepare()
        self.answer()
        result, printed = self.merge()
        self.assertTrue(result["gatePassed"], printed)
        with open(self.curated) as fh:
            blob = json.load(fh)
        source = "enrichment-2026-09-22-sonnet"
        self.assertEqual(result["source"], source)
        for key in ("movie:2", "movie:3", "tv:8"):
            self.assertEqual((blob["titles"][key]["source"], blob["titles"][key]["primaryGenreSource"]),
                             (source, source))
            self.assertNotIn("webSources", blob["titles"][key])
        self.assertEqual([blob["titles"][k]["animated"] for k in ("movie:2", "movie:3", "tv:8")], [True, True, False])
        self.assertEqual(blob["titles"]["movie:1"]["source"], "july-relabel")
        self.assertIn(source, blob["sources"])
        self.assertEqual(blob["count"], len(blob["titles"]))
        self.assertEqual((result["added"], result["changed"]), (["movie:3", "tv:8"], ["movie:2"]))
        self.assertEqual(result["shifts"][("mood", "Tense/Edge-of-seat")], 3)
        self.assertEqual(result["shifts"][("primary", "Crime")], 2, "movie:2 was Crime already")
        self.assertIn("before: Crime | — | —", printed)

    def test_untouched_lines_stay_byte_identical_and_in_place(self):
        before = self.read().decode().split("\n")
        self.prepare()
        self.answer()
        self.merge()
        after = self.read().decode().split("\n")
        self.assertEqual(len(after), len(before) + 2, "two titles added, one changed in place")
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        last = len(before) - 3
        # The head, movie:2 in place (line 2), the old last title gaining a comma, then the appended titles
        # overwrite what were the closing lines.
        self.assertEqual(changed, [0, 2, last, last + 1, last + 2])
        self.assertTrue(after[2].startswith('"movie:2":'), after[2])
        self.assertEqual(after[last], before[last] + ",")
        self.assertTrue(after[-4].startswith('"movie:3":') and after[-3].startswith('"tv:8":'), after[-4:])
        for line in after[1:-2]:
            (key, value), = json.loads("{" + line.rstrip(",") + "}").items()
            self.assertEqual(line.rstrip(","), f"{json.dumps(key)}:{json.dumps(value, sort_keys=True)}")
        head, titles = gm.read_curated(self.curated)
        self.assertEqual(gmm.dump_curated(head, titles).encode(), self.read())

    def test_a_result_below_the_floors_is_refused_and_nothing_is_written(self):
        curated, floors = self.read(), self.read(self.floors)
        self.prepare()
        self.answer(overrides={"movie:2": {**GOOD, "primary_genre": "Comedy"}})
        with self.assertRaisesRegex(StageError, "below the quality floors"):
            self.merge()
        self.assertEqual((self.read(), self.read(self.floors)), (curated, floors))
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".tmp-")], [])

    def test_a_pass_inside_the_tolerance_leaves_the_baseline_where_it_is(self):
        """The gate tolerates a small drop; the ratchet still only ever rises on its own. Recording the
        lower score here is what would turn the band into a slide — a merge a run, each one a fraction
        worse, each one moving the reference point down behind it."""
        self.prepare()
        self.answer()
        self.merge()
        floors = json.loads(self.read(self.floors))
        for metric in floors["baseline"]["mood"]:
            floors["baseline"]["mood"][metric] += 0.002
        with open(self.floors, "w") as fh:
            json.dump(floors, fh)
        raised = self.read(self.floors)
        # The same answers again: the file does not move, so what it scores is now 0.002 under.
        result, printed = self.merge()
        self.assertTrue(result["gatePassed"], printed)
        self.assertEqual(self.read(self.floors), raised, "a passing merge did not lower the baseline")
        self.assertIn("left where it was", printed)

    def test_accept_drop_writes_and_leaves_recording_the_floors_to_the_operator(self):
        floors = self.read(self.floors)
        self.prepare()
        self.answer(overrides={"movie:2": {**GOOD, "primary_genre": "Comedy"}})
        result, printed = self.merge(accept_drop=True)
        self.assertFalse(result["gatePassed"])
        self.assertEqual(json.loads(self.read())["titles"]["movie:2"]["primaryGenre"], "Comedy")
        self.assertEqual(self.read(self.floors), floors)
        self.assertIn("--record", printed)

    def test_a_pass_records_the_floors_against_the_new_file(self):
        self.prepare()
        self.answer()
        self.merge()
        with open(self.floors) as fh:
            recorded = json.load(fh)
        self.assertEqual(recorded["labelsSha256"], hashlib.sha256(self.read()).hexdigest())

    def test_a_missing_or_invalid_batch_writes_nothing(self):
        curated = self.read()
        self.prepare(batch=2)
        self.answer(drop=("batch-002.json",))
        with self.assertRaisesRegex(StageError, "1 of 2 batches do not validate"):
            self.merge()
        self.assertEqual(self.read(), curated)
        with self.assertRaisesRegex(StageError, "lower-case"):
            self.merge(model="Claude Sonnet")


class CommittedFile(unittest.TestCase):
    def test_the_committed_curated_file_round_trips_byte_for_byte(self):
        """What `merge` writes for the lines it does not touch is what is committed now."""
        with open(gm.CURATED, "rb") as fh:
            committed = fh.read()
        head, titles = gm.read_curated(gm.CURATED)
        self.assertEqual(gmm.dump_curated(head, titles).encode(), committed)


class Web(Fixture):
    def test_web_requires_sources_and_records_them(self):
        self.prepare()
        self.answer()
        with self.assertRaisesRegex(StageError, "do not validate"):
            self.merge(web=True)
        self.answer(sources={"movie:3": ["https://www.example.com/review"]})
        result, _ = self.merge(web=True)
        blob = json.loads(self.read())
        self.assertEqual(result["source"], "enrichment-2026-09-22-sonnet-web")
        self.assertEqual(blob["titles"]["movie:3"]["webSources"], ["https://www.example.com/review"])
        self.assertEqual(blob["titles"]["movie:3"]["source"], "enrichment-2026-09-22-sonnet-web")
        self.assertIn("webSources", blob["sources"]["enrichment-2026-09-22-sonnet-web"])


class Command(Fixture):
    def den(self, *args):
        return subprocess.run([sys.executable, DEN, "genres-moods", *args], capture_output=True, text=True)

    def test_prepare_and_merge_through_den(self):
        made = self.den("prepare", "--out-dir", self.out, "--work", self.work, "--batch", "2",
                        "--curated", self.curated)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertIn("prepared 3 titles in 2 batches", made.stdout)
        self.assertIn(f"Follow {self.work}/SPEC.md for batch NNN", made.stdout)
        self.answer()
        # Through `den`, the gate is the committed golden set, which this fixture does not cover.
        curated = self.read()
        refused = self.den("merge", "--model", "sonnet", "--work", self.work, "--curated", self.curated)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("below the quality floors", refused.stderr)
        self.assertEqual(self.read(), curated)


if __name__ == "__main__":
    unittest.main()
