#!/usr/bin/env python3
"""The worklist stage — the universe it builds, and the ways a short one used to pass for a whole one.

Three of these are the failures the stage exists for, and each one exited 0 when it happened:

  * **the mode is chosen, never inherited.** The three modes are three different catalogues, and the one
    a default picks is the pilot's 500 titles. Enrichment is billed per title;
  * **a delta is handed the published labels.** Without them it re-enriches the whole catalogue — the one
    cost the pass exists to avoid — and reports an ordinary-looking count while doing it;
  * **a short universe is refused rather than written.** A truncated dump, a still-gzipped file or a saved
    error page parses to nothing or to half a catalogue, and `enrich` drains either as a finished run.

`discover` and `delta` are driven through a fake client that answers pages: what is under test here is the
paging, the de-dup and the skip, not TMDB. Their equivalence against the Swift command was measured
separately (oxyc/den-dataset#27) over live `/discover`, which CI has neither the key nor the toolchain
for; `export` is offline and deterministic, and was diffed byte for byte over a real 1,247,062-line daily
dump.
"""
import json
import os
import re
import tempfile
import unittest

import pipeline

from . import artifacts, floors, worklist
from .contract import Context, StageError, bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "testver"

#: Two real-shaped dump lines per media. The export's rows carry more than `id` — the parse takes that one
#: field — so the fixture keeps a second key rather than the minimum that would parse.
DUMPS = {
    "movie_ids.json": '{"id":11,"original_title":"Star Wars","popularity":41.2}\n'
                      '{"id":12,"original_title":"Finding Nemo","popularity":33.9}\n',
    "tv_series_ids.json": '{"id":1399,"original_name":"Game of Thrones","popularity":92.1}\n',
}


class FakeTMDB:
    """A `/discover` that answers from a scripted page list and records what it was asked.

    Pages are per media, so a test can give the two media different answers — which is the shape the real
    thing has and the shape a stage that reused one query would get wrong.
    """

    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def discover(self, media, params, page=1):
        self.asked.append((media, dict(params), page))
        pages = self.pages.get(media, [[]])
        rows = pages[page - 1] if page - 1 < len(pages) else []
        return [{"id": value} for value in rows], page, len(pages)


def write_inputs(out):
    """Every declared input, under its declared filename."""
    for name, text in DUMPS.items():
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    with open(os.path.join(out, "labels-t02.json"), "w", encoding="utf-8") as fh:
        json.dump({"records": [{"tmdbId": 11, "mediaType": "movie"},
                               {"tmdbId": 1399, "mediaType": "tv"}]}, fh)


def context(out, **kwargs):
    return Context(out_dir=out, dataset_version=VERSION, **kwargs)


class Staged(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.out = self.directory.name
        write_inputs(self.out)

    def tearDown(self):
        self.directory.cleanup()

    def universe(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)


class Declaration(unittest.TestCase):
    def test_the_labels_artifact_keeps_one_name_and_is_read_under_its_own(self):
        """`labels-t02.json` is `--known` here, `--labels` to the corpus join and `--vector-labels` to the
        store writer — one file, one pipeline-wide name, and the word is the reader's."""
        bound = {bind(e).name: bind(e) for e in worklist.INPUTS}["vector_labels"]
        self.assertEqual(bound.flag(), "--known")
        self.assertEqual(bound.artifact, artifacts.VECTOR_LABELS)

    def test_it_declares_a_worklist_per_media(self):
        """One file per media is what the fetch stage declares and drains, one universe at a time."""
        self.assertEqual([bind(e).name for e in worklist.OUTPUTS], ["universe_movie", "universe_tv"])
        self.assertEqual(sorted(worklist.MEDIA), ["movie", "tv"])

    def test_discovery_enumerates_at_the_lowest_floor_any_tier_admits_at(self):
        """At 50, discovery never listed a title the regional tier's 15 would admit, nor one IMDb admits
        that TMDB undercounts — the port from Swift lost the 15 exactly that way. The number is pinned as
        well as derived: a floors change that moves what discovery pays for should be a visible edit."""
        self.assertEqual(worklist.VOTE_FLOOR, 15)
        self.assertEqual(worklist.VOTE_FLOOR, min(floors.DEFAULT.tmdb, floors.DEFAULT.regional_tmdb))

    def test_the_daily_pass_admits_at_the_worldwide_floor(self):
        with open(os.path.join(REPO, "scripts", "delta-run.sh"), encoding="utf-8") as fh:
            self.assertIn(f"VOTE_FLOOR:-{floors.DEFAULT.tmdb}", fh.read())

    def test_it_does_not_write_the_shipped_catalogues_filename(self):
        """`scripts/build-worklist.py` owns `worklist-<media>.json` — the ids Den already ships, ordered by
        popularity. This stage enumerates all of TMDB: 1,247,062 movie ids against a corpus of 47,618.

        They used to share a filename, so whichever ran last decided which universe the next enrich billed
        for, and nothing downstream could tell them apart.
        """
        for artifact in worklist.OUTPUTS:
            self.assertNotIn("worklist", artifact.filename, "the shipped catalogue's name")
            self.assertIn("universe", artifact.filename)


class Mode(Staged):
    def test_a_run_that_did_not_say_which_universe_it_wants_is_refused(self):
        with self.assertRaises(StageError) as refused:
            worklist.universe(context(self.out), "movie")
        for mode in worklist.MODES:
            self.assertIn(mode, str(refused.exception))

    def test_a_mode_that_is_not_one_of_the_three_is_refused(self):
        with self.assertRaises(StageError) as refused:
            worklist.universe(context(self.out, mode="daily"), "movie")
        self.assertIn("--mode", str(refused.exception))


class Export(Staged):
    def test_it_builds_one_worklist_per_media_from_that_medias_dump(self):
        made = worklist.run(context(self.out, mode="export"))
        for media, expected in (("movie", [11, 12]), ("tv", [1399])):
            path = os.path.join(self.out, f"universe-{media}.json")
            self.assertIn(path, made)
            entries = self.universe(path)
            self.assertEqual([e["tmdbId"] for e in entries], expected)
            self.assertEqual({e["mediaType"] for e in entries}, {media})

    def test_a_dump_the_parse_could_not_finish_is_refused(self):
        """The failure an exit code cannot show: a line that does not read as an id is dropped, so a dump
        that arrived half-written becomes half a catalogue and nothing downstream can tell."""
        with open(os.path.join(self.out, "movie_ids.json"), "a", encoding="utf-8") as fh:
            fh.write('{"id":13,"original_ti\n')
        with self.assertRaises(StageError) as refused:
            worklist.run(context(self.out, mode="export"))
        self.assertIn("3 lines", str(refused.exception))
        self.assertIn("2 of them", str(refused.exception))

    def test_a_still_gzipped_dump_is_refused_rather_than_read_as_an_empty_universe(self):
        """The reason the `how` for the export includes `gunzip`: the fetch leaves it compressed, and a
        gzip container parses to no ids at all."""
        with open(os.path.join(self.out, "movie_ids.json"), "wb") as fh:
            fh.write(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03not really gzip")
        with self.assertRaises(StageError) as refused:
            worklist.run(context(self.out, mode="export"))
        self.assertIn("hole in it", str(refused.exception))

    def test_a_missing_dump_is_refused_with_what_fetches_it(self):
        os.remove(os.path.join(self.out, "movie_ids.json"))
        with self.assertRaises(StageError) as refused:
            worklist.run(context(self.out, mode="export"))
        self.assertIn("build-worklist.py", str(refused.exception))
        self.assertIn("gunzip", str(refused.exception))

    def test_an_empty_dump_is_a_refusal_not_a_finished_run(self):
        with open(os.path.join(self.out, "movie_ids.json"), "w", encoding="utf-8") as fh:
            fh.write("")
        with self.assertRaises(StageError) as refused:
            worklist.run(context(self.out, mode="export"))
        self.assertIn("empty", str(refused.exception))


class Discover(Staged):
    def test_it_pages_until_the_last_page_and_stops_at_the_pinned_count(self):
        pages = [[n for n in range(start, start + 200)] for start in (1, 201, 401, 601)]
        client = FakeTMDB({"movie": pages, "tv": [[9]]})
        rows = worklist.universe(context(self.out, mode="discover"), "movie", client)
        self.assertEqual(len(rows), worklist.DISCOVER_COUNT)
        self.assertEqual(rows[0]["tmdbId"], 1)
        # It asked for three pages and stopped: the fourth would be past the count.
        self.assertEqual([page for _media, _params, page in client.asked], [1, 2, 3])

    def test_it_asks_for_the_highest_vote_titles_above_the_pinned_floor(self):
        client = FakeTMDB({"movie": [[1]]})
        worklist.universe(context(self.out, mode="discover"), "movie", client)
        _media, params, _page = client.asked[0]
        self.assertEqual(params["sort_by"], "vote_count.desc")
        self.assertEqual(params["vote_count.gte"], str(worklist.VOTE_FLOOR))
        # A seed is not a window: a date filter here would silently make it one.
        self.assertNotIn("primary_release_date.gte", params)

    def test_an_id_on_two_pages_is_taken_once(self):
        client = FakeTMDB({"movie": [[1, 2], [2, 3]]})
        rows = worklist.universe(context(self.out, mode="discover"), "movie", client)
        self.assertEqual([r["tmdbId"] for r in rows], [1, 2, 3])

    def test_a_discover_that_found_nothing_is_refused(self):
        """`[]` is what an auth failure or a schema change looks like once it has been read as a page, and
        `enrich` drains an empty worklist as a finished run rather than as a failure."""
        with self.assertRaises(StageError) as refused:
            worklist.run(context(self.out, mode="discover"), FakeTMDB({"movie": [[]], "tv": [[]]}))
        self.assertIn("empty", str(refused.exception))


class Delta(Staged):
    def test_it_skips_what_is_already_published_for_that_media(self):
        """movie 11 and tv 1399 are in the fixture labels. The skip is media-qualified: TMDB's two id
        spaces overlap, so a bare id set would drop a series because a film shares its number."""
        client = FakeTMDB({"movie": [[11, 12]], "tv": [[11, 1399]]})
        ctx = context(self.out, mode="delta", since="2026-09-07")
        self.assertEqual([r["tmdbId"] for r in worklist.universe(ctx, "movie", client)], [12])
        self.assertEqual([r["tmdbId"] for r in worklist.universe(ctx, "tv", client)], [11])

    def test_it_collects_from_the_window_it_was_given(self):
        client = FakeTMDB({"movie": [[7]]})
        worklist.universe(context(self.out, mode="delta", since="2026-09-07"), "movie", client)
        _media, params, _page = client.asked[0]
        self.assertEqual(params["primary_release_date.gte"], "2026-09-07")
        self.assertEqual(params["vote_count.gte"], str(worklist.VOTE_FLOOR))

    def test_a_series_window_uses_the_field_a_series_has(self):
        """TMDB names it `first_air_date` for TV. Sending `primary_release_date` is not an error — it is
        an unfiltered query that looks like a filtered one, so the pass re-enriches everything."""
        client = FakeTMDB({"tv": [[7]]})
        worklist.universe(context(self.out, mode="delta", since="2026-09-07"), "tv", client)
        _media, params, _page = client.asked[0]
        self.assertEqual(params["first_air_date.gte"], "2026-09-07")
        self.assertNotIn("primary_release_date.gte", params)

    def test_a_delta_with_no_window_is_refused(self):
        with self.assertRaises(StageError) as refused:
            worklist.universe(context(self.out, mode="delta"), "movie", FakeTMDB({}))
        self.assertIn("--since", str(refused.exception))

    def test_a_delta_with_no_published_labels_is_refused(self):
        """Without them the pass re-enriches every title already shipped, at the per-title price the vote
        floor exists to bound, and nothing in its output says that is what happened."""
        os.remove(os.path.join(self.out, "labels-t02.json"))
        with self.assertRaises(StageError) as refused:
            worklist.universe(context(self.out, mode="delta", since="2026-09-07"), "movie", FakeTMDB({}))
        self.assertIn("./den stage finalize", str(refused.exception))

    def test_a_labels_file_that_names_no_records_is_refused_rather_than_read_as_nothing_published(self):
        """A wrong `--set vector_labels=…` is a file with no `records` in it. Read as an empty set it skips
        nothing, and the delta re-enriches the published catalogue at the per-title price. `docfacts`
        refuses the same shape, and so did the Swift command."""
        with open(os.path.join(self.out, "labels-t02.json"), "w", encoding="utf-8") as fh:
            json.dump({"taxonomyVersion": "t02"}, fh)
        client = FakeTMDB({"movie": [[12]]})
        with self.assertRaises(StageError) as refused:
            worklist.universe(context(self.out, mode="delta", since="2026-09-07"), "movie", client)
        self.assertIn("labels-t02.json", str(refused.exception))
        self.assertIn("no records", str(refused.exception))
        self.assertIn("./den stage finalize", str(refused.exception))
        self.assertEqual(client.asked, [])

    def test_a_delta_that_found_nothing_is_not_a_failure(self):
        """The answer on a quiet day. Refusing it would fail the daily pass for doing its job — which is
        why `scripts/delta-run.sh` counts titles that survived enrichment rather than worklist rows."""
        made = worklist.run(context(self.out, mode="delta", since="2026-09-07"),
                            FakeTMDB({"movie": [[]], "tv": [[]]}))
        self.assertIn("universe-movie.json", made)
        self.assertEqual(self.universe(os.path.join(self.out, "universe-movie.json")), [])

    def test_the_daily_pass_names_the_window_the_labels_and_both_outputs(self):
        """The one mode with a live caller. `scripts/delta-run.sh` runs this every day, so its invocation
        is the oracle for what a delta needs — and the two `--set` lines are not decoration: without them a
        delta's forty rows are written over the full run's 47k-title worklists under the same names.

        The labels override is the expensive one to lose. Point it at nothing and the pass re-enriches the
        whole published catalogue at the per-title price, reporting an ordinary-looking count.
        """
        with open(os.path.join(REPO, "scripts", "delta-run.sh"), encoding="utf-8") as fh:
            script = fh.read()
        invocation = script.split("./den stage worklist")[1].split("\n\n")[0]
        self.assertIn("--mode delta", invocation)
        self.assertIn('--since "$SINCE"', invocation)
        for name in ("vector_labels", "universe_movie", "universe_tv"):
            self.assertIn(f"--set \"{name}=", invocation, f"the daily pass does not point {name} anywhere")

    def test_the_hand_off_names_commands_that_exist_in_the_pipelines_order(self):
        """What the daily pass prints for a person to run next. It named `dump-articles` after that command
        was deleted — the binary answers `unknown command` — and skipped `docfacts`, whose absence composes
        a different vector space. Every stage from the dump on, in `STAGES` order — and no Swift binary, which
        no longer exists to answer."""
        with open(os.path.join(REPO, "scripts", "delta-run.sh"), encoding="utf-8") as fh:
            script = fh.read()
        hand_off = script.split("Next, by hand")[1]
        stages = list(dict.fromkeys(re.findall(r"\./den stage ([a-z]+)", hand_off)))
        self.assertEqual(stages, list(pipeline.STAGES[pipeline.STAGES.index("articles"):]))
        self.assertNotIn("swift build", script)
        self.assertNotIn("taxonomy-backfill", script)

    def test_the_media_the_daily_pass_enriches_are_the_media_this_stage_builds(self):
        """The stage writes both lists in one call and the script then drains each. A media the script
        loops over and the stage does not build is a `universe-<media>.json` that is never there."""
        with open(os.path.join(REPO, "scripts", "delta-run.sh"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn("for media in movie tv; do", script)
        self.assertEqual(sorted(worklist.MEDIA), ["movie", "tv"])


class Written(Staged):
    def test_the_file_is_the_swift_encoders_layout(self):
        """A golden, not a round trip through `write`: sorted keys, two-space indent, ` : ` and no trailing
        newline — what `taxonomy-backfill worklist` wrote, which is how the export was proven equal to it
        byte for byte over a 1.2M-line dump. Nothing else in CI holds the layout that proof rests on."""
        worklist.run(context(self.out, mode="export"))
        with open(os.path.join(self.out, "universe-movie.json"), "rb") as fh:
            self.assertEqual(fh.read(), b'[\n  {\n    "mediaType" : "movie",\n    "tmdbId" : 11\n  },\n'
                                        b'  {\n    "mediaType" : "movie",\n    "tmdbId" : 12\n  }\n]')

    def test_an_empty_universe_is_written_as_an_empty_list(self):
        """`[]`, where Swift wrote `[\\n\\n]`. Only a quiet delta writes one, nothing hashes it, and both
        parse to the same nothing — so the port's spelling is pinned rather than the encoder quirk."""
        worklist.run(context(self.out, mode="delta", since="2026-09-07"),
                     FakeTMDB({"movie": [[]], "tv": [[]]}))
        with open(os.path.join(self.out, "universe-movie.json"), "rb") as fh:
            self.assertEqual(fh.read(), b"[]")

    def test_a_write_that_fails_part_way_leaves_the_previous_universe(self):
        """In place, the file is truncated before the rows arrive, and `enrich` drains a short worklist as
        a finished run."""
        path = os.path.join(self.out, "universe-movie.json")
        worklist.write(path, [worklist.entry(11, "movie")])
        with self.assertRaises(TypeError):
            worklist.write(path, [worklist.entry(11, "movie"), {"tmdbId": object(), "mediaType": "movie"}])
        self.assertEqual(self.universe(path), [{"tmdbId": 11, "mediaType": "movie"}])

    def test_the_file_is_byte_stable_for_one_universe(self):
        """Two runs over one dump must produce one file. It is read by a person as often as by `enrich`,
        and a key order that moves makes every diff of two universes unreadable."""
        worklist.run(context(self.out, mode="export"))
        with open(os.path.join(self.out, "universe-movie.json"), "rb") as fh:
            first = fh.read()
        worklist.run(context(self.out, mode="export"))
        with open(os.path.join(self.out, "universe-movie.json"), "rb") as fh:
            self.assertEqual(fh.read(), first)


class Topology(unittest.TestCase):
    def test_the_worklists_are_owned_by_the_stage_that_writes_them(self):
        for name in ("universe_movie", "universe_tv"):
            self.assertEqual(getattr(artifacts, name.upper()).producer, "")
            self.assertEqual(pipeline.producers()[name], (worklist.PRODUCER, worklist.HOW, True))

    def test_the_producer_it_names_is_this_file(self):
        """A stage registered against a rule that is not there is an artifact nothing can rebuild — and
        `check-producers.py` refuses a publish on exactly that."""
        self.assertEqual(worklist.PRODUCER, "pipeline/worklist.py")
        self.assertTrue(os.path.isfile(os.path.join(REPO, worklist.PRODUCER)))

    def test_the_universe_is_built_before_anything_is_drawn_from_it(self):
        self.assertEqual(pipeline.STAGES[0], "worklist")

    def test_the_dumps_answer_for_themselves_until_something_here_fetches_them(self):
        """The seam: TMDB's daily export comes from outside this repo, and the only thing that pulls it
        leaves it gzipped — so the `how` an operator is sent to includes the step that fetch does not do."""
        for artifact in (artifacts.EXPORT_MOVIE, artifacts.EXPORT_TV):
            producer, how, _ = pipeline.producers()[artifact.name]
            self.assertEqual(producer, "scripts/build-worklist.py")
            self.assertIn("gunzip", how)
            self.assertTrue(os.path.isfile(os.path.join(REPO, producer)))


if __name__ == "__main__":
    unittest.main()
