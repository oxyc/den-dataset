"""`seed_genres_moods_curated.py`: the September phase's subgenres & moods come from classify under the
rule measured on #56, every other field and every other title is left alone, and anything ambiguous is
refused with nothing written."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import seed_genres_moods_curated as seed  # noqa: E402
from combined_questions import taxonomy_questions  # noqa: E402

_, MAPPING, _ = taxonomy_questions()
SUB = sorted(q for q, m in MAPPING.items() if m["family"] == "subgenres")
THEME = sorted(q for q, m in MAPPING.items() if m["family"] == "thematic")
MOOD = sorted(q for q, m in MAPPING.items() if m["family"] == "moods")


def label(q):
    return MAPPING[q]["label"]


def answers(high=None):
    """Every taxonomy Noul at 0.1, except those in `high`."""
    out = {"primary_genre": {"type": "choice", "choice": "Comedy"}}
    out.update({q: {"type": "noul", "noul": (high or {}).get(q, 0.1)} for q in MAPPING})
    return out


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


EARLY, LATE = "2026-09-19T16:13:40+00:00", "2026-09-23T09:00:00+00:00"


def record(media, tmdb_id, primary="Drama", subgenres=(), moods=()):
    return {"animated": False, "mediaType": media, "primaryGenre": primary, "source": "llm", "tmdbId": tmdb_id,
            "subgenres": [{"confidence": 0.7, "label": l} for l in subgenres],
            "moods": [{"confidence": 0.6, "label": l} for l in moods]}


class Seed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        os.makedirs(os.path.join(self.dir, "phase", "out"))
        self.labels = os.path.join(self.dir, "labels-t02.json")
        self.curated = os.path.join(self.dir, "genres-moods-curated.json")
        self.tombstones = os.path.join(self.dir, "withdrawn.jsonl")
        # tv:95 and movie:95 share an id: only the September one (tv) may change.
        self.records = [record("movie", 95, "Action", subgenres=[label(SUB[0])], moods=[label(MOOD[0])]),
                        record("movie", 7, "Horror", subgenres=["Slasher/Stalker"]),
                        record("tv", 95, "Horror", subgenres=[label(SUB[1])], moods=[label(MOOD[1])])]
        self.september = ["tv:95", "movie:7"]
        self.shards = {"a.jsonl": [("movie", 95, answers()),
                                   ("movie", 7, answers({SUB[2]: 0.95, THEME[0]: 0.9, SUB[3]: 0.8,
                                                         SUB[4]: 0.85, MOOD[2]: 0.79}))],
                       "b.jsonl": [("tv", 95, answers({MOOD[3]: 0.9, MOOD[4]: 0.9}))]}
        #: Shard name → the runStartedAt its manifest records; a shard not named here has no manifest.
        self.started = {}
        #: (media, tmdbId, withdrawnAt) tombstones; none means no --withdrawn.
        self.withdrawn = []

    def write(self):
        with open(self.labels, "w", encoding="utf-8") as fh:
            fh.write(seed.dump_labels({"count": len(self.records), "records": self.records,
                                       "taxonomyVersion": "t02"}))
        with open(os.path.join(self.dir, "phase", "out", "batch-0000.json"), "w") as fh:
            json.dump([{"key": k, "primary_genre": "Drama"} for k in self.september], fh)
        for name, rows in self.shards.items():
            with open(os.path.join(self.dir, name), "w") as fh:
                for media, tmdb_id, ans in rows:
                    fh.write(json.dumps({"mediaType": media, "tmdbId": tmdb_id, "answers": ans}) + "\n")
        for name, when in self.started.items():
            with open(os.path.join(self.dir, name + ".manifest.json"), "w") as fh:
                json.dump({"runStartedAt": when}, fh)
        with open(self.tombstones, "w") as fh:
            for media, tmdb_id, when in self.withdrawn:
                fh.write(json.dumps({"mediaType": media, "tmdbId": tmdb_id, "reason": "#64",
                                     "withdrawnAt": when}) + "\n")

    def argv(self):
        argv = ["--labels", self.labels, "--phase", os.path.join(self.dir, "phase"), "--out", self.labels,
                "--curated", self.curated, "--withdrawn", self.tombstones]
        for name in self.shards:
            argv += ["--combined", os.path.join(self.dir, name)]
        return argv

    def run_seed(self, write=True, extra=()):
        if write:
            self.write()
        with contextlib.redirect_stdout(io.StringIO()):
            seed.main(self.argv() + list(extra))
        with open(self.labels, encoding="utf-8") as fh:
            labels = {seed.key_of(r): r for r in json.load(fh)["records"]}
        with open(self.curated, encoding="utf-8") as fh:
            return labels, json.load(fh)

    def refused(self, needle):
        self.write()
        before = read(self.labels)
        with self.assertRaises(SystemExit) as caught:
            self.run_seed()
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(read(self.labels), before, "a refusal writes nothing")
        self.assertFalse(os.path.exists(self.curated))

    def test_september_subgenres_and_moods_come_from_classify_strongest_first_capped_at_three(self):
        labels, _ = self.run_seed()
        got = labels["movie:7"]
        # 0.95, 0.9, 0.85 kept; 0.8 is the fourth, over the cap. Themes share the subgenre field.
        self.assertEqual(got["subgenres"], [{"confidence": 0.95, "label": label(SUB[2])},
                                            {"confidence": 0.9, "label": label(THEME[0])},
                                            {"confidence": 0.85, "label": label(SUB[4])}])
        # 0.79 is under the threshold, so the field is empty rather than the subagents' old one.
        self.assertEqual(got["moods"], [])

    def test_the_threshold_is_inclusive(self):
        self.shards["a.jsonl"][1] = ("movie", 7, answers({SUB[3]: 0.8}))
        labels, _ = self.run_seed()
        self.assertEqual(labels["movie:7"]["subgenres"], [{"confidence": 0.8, "label": label(SUB[3])}])

    def test_ties_are_broken_by_label(self):
        labels, _ = self.run_seed()
        self.assertEqual([m["label"] for m in labels["tv:95"]["moods"]], sorted([label(MOOD[3]), label(MOOD[4])]))

    def test_primary_genre_and_every_other_field_are_kept(self):
        labels, _ = self.run_seed()
        for key, old in (("movie:7", self.records[1]), ("tv:95", self.records[2])):
            for field in ("animated", "mediaType", "primaryGenre", "source", "tmdbId"):
                self.assertEqual(labels[key][field], old[field], (key, field))

    def test_a_july_title_is_untouched_even_when_it_shares_an_id_with_a_september_one(self):
        self.write()
        encoded = seed.dump_labels(self.records[0])
        labels, _ = self.run_seed()
        self.assertEqual(labels["movie:95"], self.records[0])
        self.assertIn(encoded.encode(), read(self.labels), "byte-identical in the output")

    def test_the_curated_file_says_where_each_part_came_from(self):
        labels, curated = self.run_seed()
        titles = curated["titles"]
        self.assertEqual(curated["count"], 3)
        self.assertEqual((titles["movie:95"]["source"], titles["movie:95"]["primaryGenreSource"]),
                         (seed.JULY, seed.JULY))
        self.assertEqual((titles["tv:95"]["source"], titles["tv:95"]["primaryGenreSource"]),
                         (seed.SWAP, seed.SEPTEMBER))
        self.assertEqual(set(curated["sources"]), {seed.JULY, seed.SEPTEMBER, seed.SWAP})
        for key, entry in titles.items():
            for field in ("animated", "primaryGenre", "subgenres", "moods"):
                self.assertEqual(entry[field], labels[key][field], (key, field))

    def test_the_curated_file_is_one_title_per_line_and_scores(self):
        self.run_seed()
        lines = read(self.curated).decode("utf-8").splitlines()
        self.assertEqual(sum(1 for l in lines if l.startswith('"movie:') or l.startswith('"tv:')), 3)
        spec = importlib.util.spec_from_file_location("ev", os.path.join(HERE, "..", "eval-taxonomy.py"))
        ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ev)
        version, by_key = ev.labels_by_key(self.curated)
        self.assertEqual((version, sorted(by_key)), ("t02", [("movie", 7), ("movie", 95), ("tv", 95)]))

    def test_a_september_title_with_no_classify_row_is_refused(self):
        self.shards["b.jsonl"] = []
        self.refused("no classify row")

    def test_a_key_in_two_shards_whose_runs_are_not_ordered_is_refused(self):
        self.shards["b.jsonl"].append(("movie", 95, answers()))
        self.refused("duplicate key across combined shards")

    def test_a_key_twice_in_one_shard_is_refused(self):
        self.shards["b.jsonl"].append(("tv", 95, answers()))
        self.refused("duplicate key within combined shard")

    def test_a_later_classify_run_supersedes_an_earlier_one(self):
        """The corpus join's rule: the run that started later answers for the title."""
        self.shards["c.jsonl"] = [("tv", 95, answers({MOOD[5]: 0.9}))]
        self.started = {"a.jsonl": EARLY, "b.jsonl": EARLY, "c.jsonl": LATE}
        labels, curated = self.run_seed()
        self.assertEqual(labels["tv:95"]["moods"], [{"confidence": 0.9, "label": label(MOOD[5])}])
        self.assertEqual(curated["titles"]["tv:95"]["moods"], labels["tv:95"]["moods"])

    def test_a_withdrawn_title_has_no_classify_row(self):
        """A title that lost its plot keeps no labels derived from the article it lost."""
        self.started = {"a.jsonl": EARLY, "b.jsonl": EARLY}
        self.withdrawn = [("tv", 95, LATE)]
        self.refused("no classify row")

    def test_a_row_asked_under_another_taxonomy_is_refused(self):
        ans = answers()
        del ans[MOOD[0]]
        self.shards["b.jsonl"] = [("tv", 95, ans)]
        self.refused("not usable")

    def seeded_then_reclassified(self):
        """The shipped state, then a re-classify run that answered BOTH September titles again. The
        curated file also carries an entry the enrichment merge rewrote since the seed."""
        self.run_seed()
        with open(self.curated, encoding="utf-8") as fh:
            head = json.load(fh)
        titles = head.pop("titles")
        titles["movie:95"] = dict(titles["movie:95"], source="hand-enrichment", moods=[])
        with open(self.curated, "w", encoding="utf-8") as fh:
            fh.write(seed.encode_curated(head, titles))
        self.shards["c.jsonl"] = [("tv", 95, answers({MOOD[5]: 0.9})), ("movie", 7, answers({SUB[5]: 0.9}))]
        self.started = {"a.jsonl": EARLY, "b.jsonl": EARLY, "c.jsonl": LATE}
        self.write_shards_only()
        self.keys = os.path.join(self.dir, "keys.txt")
        with open(self.keys, "w", encoding="utf-8") as fh:
            fh.write("tv:95\n")

    def write_shards_only(self):
        """Shards and manifests, leaving the labels and curated files as the last run wrote them."""
        labels, curated = read(self.labels), read(self.curated)
        self.write()
        with open(self.labels, "wb") as fh:
            fh.write(labels)
        with open(self.curated, "wb") as fh:
            fh.write(curated)

    def test_keys_rederives_only_the_listed_titles_and_leaves_every_other_line_alone(self):
        self.seeded_then_reclassified()
        labels_before = read(self.labels).decode()
        curated_before = read(self.curated).decode().splitlines()
        labels, curated = self.run_seed(write=False, extra=["--keys", self.keys])
        self.assertEqual(labels["tv:95"]["moods"], [{"confidence": 0.9, "label": label(MOOD[5])}])
        self.assertEqual(curated["titles"]["tv:95"]["moods"], labels["tv:95"]["moods"])
        self.assertEqual(labels["tv:95"]["primaryGenre"], "Horror", "only subgenres and moods move")
        # movie:7 was answered again too, and is not listed.
        self.assertEqual(labels["movie:7"]["subgenres"][0]["label"], label(SUB[2]))
        for key in ("movie:7", "movie:95"):
            self.assertIn(seed.dump_labels(labels[key]), labels_before, key)
        after = read(self.curated).decode().splitlines()
        self.assertEqual(len(after), len(curated_before))
        changed = [i for i, (a, b) in enumerate(zip(curated_before, after)) if a != b]
        self.assertEqual([after[i].split(":", 2)[:2] for i in changed], [['"tv', '95"']])
        self.assertEqual(curated["titles"]["movie:95"]["source"], "hand-enrichment", "the merge's entry")

    def test_keys_refuses_a_title_classify_never_labelled(self):
        self.seeded_then_reclassified()
        with open(self.keys, "a", encoding="utf-8") as fh:
            fh.write("movie:95\n")
        self.keys_refused("not September titles")

    def test_keys_refuses_a_title_the_enrichment_has_relabelled(self):
        self.seeded_then_reclassified()
        with open(self.curated, encoding="utf-8") as fh:
            head = json.load(fh)
        titles = head.pop("titles")
        titles["tv:95"]["source"] = "hand-enrichment"
        with open(self.curated, "w", encoding="utf-8") as fh:
            fh.write(seed.encode_curated(head, titles))
        self.keys_refused("are not classify-swap")

    def test_keys_refuses_a_withdrawn_title(self):
        self.seeded_then_reclassified()
        self.shards.pop("c.jsonl")
        self.withdrawn = [("tv", 95, LATE)]
        self.write_shards_only()
        self.keys_refused("no classify row")

    def keys_refused(self, needle):
        labels, curated = read(self.labels), read(self.curated)
        with self.assertRaises(SystemExit) as caught:
            self.run_seed(write=False, extra=["--keys", self.keys])
        self.assertIn(needle, str(caught.exception))
        self.assertEqual((read(self.labels), read(self.curated)), (labels, curated), "a refusal writes nothing")

    def test_the_curated_encoding_round_trips_the_committed_file(self):
        """`--keys` reads the committed file and writes it back through `encode_curated`, so an untouched
        title must come out as the same bytes."""
        committed = os.path.join(HERE, "..", "..", "data", "genres-moods-curated.json")
        with open(committed, encoding="utf-8") as fh:
            head = json.load(fh)
        titles = head.pop("titles")
        self.assertEqual(seed.encode_curated(head, titles).encode("utf-8"), read(committed))

    def test_a_september_title_missing_from_the_labels_is_refused(self):
        self.september.append("movie:404")
        self.refused("not in the labels")


if __name__ == "__main__":
    unittest.main()
