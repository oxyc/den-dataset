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
import argparse
import datetime
import json
import os
import re
import tempfile
import unittest

import pipeline

from . import artifacts, daily, floors, worklist
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

    def __init__(self, pages, regional=None):
        self.pages = pages
        self.regional = regional or {}
        self.asked = []

    def discover(self, media, params, page=1):
        """A scripted page is a list of ids, or of `(id, vote_count)` pairs where the count matters. A query
        narrowed by origin is answered from `regional`, which names nothing unless a test scripts it."""
        self.asked.append((media, dict(params), page))
        pages = (self.regional if "with_origin_country" in params else self.pages).get(media, [[]])
        rows = pages[page - 1] if page - 1 < len(pages) else []
        return ([{"id": value} if isinstance(value, int) else {"id": value[0], "vote_count": value[1]}
                 for value in rows], page, len(pages))


def write_inputs(out):
    """Every declared input, under its declared filename."""
    for name, text in DUMPS.items():
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    entry = {"animated": False, "moods": [], "primaryGenre": "Drama", "subgenres": []}
    with open(os.path.join(out, "genres-moods.json"), "w", encoding="utf-8") as fh:
        json.dump({"titles": {"movie:11": entry, "tv:1399": entry}}, fh)


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
    def test_what_a_delta_skips_is_the_titles_with_genres_and_moods(self):
        """`genres-moods.json` is `--known` here — one file, one pipeline-wide name, and the word is the
        reader's."""
        bound = {bind(e).name: bind(e) for e in worklist.INPUTS}["genres_moods"]
        self.assertEqual(bound.flag(), "--known")
        self.assertEqual(bound.artifact, artifacts.GENRES_MOODS)

    def test_it_declares_a_worklist_per_media(self):
        """One file per media is what the fetch stage declares and drains, one universe at a time."""
        self.assertEqual([bind(e).name for e in worklist.OUTPUTS], ["universe_movie", "universe_tv"])
        self.assertEqual(sorted(worklist.MEDIA), ["movie", "tv"])

    def test_discovery_enumerates_at_the_lowest_floor_any_tier_admits_at(self):
        """At 50, discovery never listed a title the regional tier's 15 would admit, nor one its Wikipedia
        count admits that TMDB undercounts — the port from Swift lost the 15 exactly that way. The number is pinned as
        well as derived: a floors change that moves what discovery pays for should be a visible edit."""
        self.assertEqual(worklist.VOTE_FLOOR, 15)
        self.assertEqual(worklist.VOTE_FLOOR, min(floors.DEFAULT.tmdb, floors.DEFAULT.regional_tmdb))

    def test_the_daily_job_admits_at_the_default_floors(self):
        """`den daily` names no floor, so the fetch stage admits at `pipeline/floors.py`'s defaults."""
        day = daily.Day(argparse.Namespace(out_dir="out", mode="delta", since=None, revisit_weeks=None,
                                           spend=False), {}, datetime.datetime(2026, 9, 24))
        self.assertEqual((day.ctx.vote_floor, day.ctx.regional_vote_floor, day.ctx.wikipedia_floor,
                          day.ctx.regional_wikipedia_floor), (None, None, None, None))

    def test_it_does_not_write_the_shipped_catalogues_filename(self):
        """`pipeline/build_worklist.py` owns `worklist-<media>.json` — the ids Den already ships, ordered by
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
        self.assertIn("build_worklist.py", str(refused.exception))
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
        # It asked for three pages and stopped: the fourth would be past the count. Then the regional
        # query, which names nothing here, for its one page.
        self.assertEqual([page for _media, _params, page in client.asked], [1, 2, 3, 1])

    def test_it_asks_for_the_highest_vote_titles_above_the_pinned_floor(self):
        client = FakeTMDB({"movie": [[1]]})
        worklist.universe(context(self.out, mode="discover"), "movie", client)
        _media, params, _page = client.asked[0]
        self.assertEqual(params["sort_by"], "vote_count.desc")
        self.assertEqual(params["vote_count.gte"], str(worklist.VOTE_FLOOR))
        # A seed is not a window: a date filter here would silently make it one.
        self.assertNotIn("primary_release_date.gte", params)

    def test_a_discovered_row_carries_the_vote_count_the_query_selected_on(self):
        """The admission gate judges by this number (`pipeline/floors.py`). `/discover` has already stated
        it here, so carrying it means `enrich` does not ask TMDB for the same count again per title."""
        client = FakeTMDB({"movie": [[(1, 4321), (2, 15)]]})
        rows = worklist.universe(context(self.out, mode="discover"), "movie", client)
        self.assertEqual([(r["tmdbId"], r["voteCount"]) for r in rows], [(1, 4321), (2, 15)])

    def test_a_row_tmdb_stated_no_count_for_carries_none_rather_than_zero(self):
        """Zero is below every floor, so writing it would turn "the page said nothing" into "TMDB refused
        this title" — and the gate would then never judge it on its Wikipedia count alone."""
        client = FakeTMDB({"movie": [[7]]})
        rows = worklist.universe(context(self.out, mode="discover"), "movie", client)
        self.assertEqual(rows, [{"tmdbId": 7, "mediaType": "movie", "regional": False}])

    def test_a_row_is_regional_when_the_query_narrowed_to_the_regional_origins_names_it(self):
        """What picks the admission tier (`pipeline/floors.py`). A film's `/discover` row names no origin, so
        the same query is asked again with `with_origin_country` and every regional origin OR-ed."""
        client = FakeTMDB({"movie": [[(1, 40), (2, 40)]]}, regional={"movie": [[(2, 40)]]})
        rows = worklist.universe(context(self.out, mode="discover"), "movie", client)
        self.assertEqual([(r["tmdbId"], r["regional"]) for r in rows], [(1, False), (2, True)])
        narrowed = [params for _media, params, _page in client.asked if "with_origin_country" in params]
        self.assertEqual(narrowed[0]["with_origin_country"], "|".join(sorted(floors.REGIONAL_ORIGINS)))
        self.assertEqual({k: v for k, v in narrowed[0].items() if k != "with_origin_country"},
                         client.asked[0][1], "the same query, narrowed and nothing else")

    def test_an_export_row_carries_no_count_and_no_tier_because_the_dump_states_neither(self):
        """TMDB's daily dump states popularity, not votes or origins."""
        rows = worklist.universe(context(self.out, mode="export"), "movie")
        self.assertEqual([sorted(row) for row in rows], [["mediaType", "tmdbId"]] * 2)

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
        """movie 11 and tv 1399 have genres & moods in the fixture. The skip is media-qualified: TMDB's two id
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

    def test_a_delta_with_no_genres_and_moods_is_refused(self):
        """Without them the pass re-enriches every title already shipped, at the per-title price the vote
        floor exists to bound, and nothing in its output says that is what happened."""
        os.remove(os.path.join(self.out, "genres-moods.json"))
        with self.assertRaises(StageError) as refused:
            worklist.universe(context(self.out, mode="delta", since="2026-09-07"), "movie", FakeTMDB({}))
        self.assertIn("./den stage genres_moods", str(refused.exception))

    def test_a_file_that_names_no_titles_is_refused_rather_than_read_as_nothing_published(self):
        """A wrong `--set genres_moods=…` — the old labels file, say — has no `titles` in it. Read as an
        empty set it skips nothing, and the delta re-enriches the published catalogue at the per-title
        price. `docfacts` refuses the same shape, and so did the Swift command."""
        with open(os.path.join(self.out, "genres-moods.json"), "w", encoding="utf-8") as fh:
            json.dump({"taxonomyVersion": "t02", "records": []}, fh)
        client = FakeTMDB({"movie": [[12]]})
        with self.assertRaises(StageError) as refused:
            worklist.universe(context(self.out, mode="delta", since="2026-09-07"), "movie", client)
        self.assertIn("genres-moods.json", str(refused.exception))
        self.assertIn("holds no genres & moods titles", str(refused.exception))
        self.assertIn("./den stage genres_moods", str(refused.exception))
        self.assertEqual(client.asked, [])

    def test_a_delta_that_found_nothing_is_not_a_failure(self):
        """The answer on a quiet day. Refusing it would fail the daily job for doing its job."""
        made = worklist.run(context(self.out, mode="delta", since="2026-09-07"),
                            FakeTMDB({"movie": [[]], "tv": [[]]}))
        self.assertIn("universe-movie.json", made)
        self.assertEqual(self.universe(os.path.join(self.out, "universe-movie.json")), [])

    def test_the_daily_job_names_the_window_the_labels_and_both_outputs(self):
        """The one mode with a live caller. `den daily` runs this every day, so what it hands the stage is
        the oracle for what a delta needs — and the two universe overrides are not decoration: without them a
        delta's forty rows are written over the full run's 47k-title universes under the same names.

        The genres & moods it skips by are the out-dir's own, the file every later stage reads: point the
        stage at nothing and it re-enriches the whole published catalogue at the per-title price."""
        now = datetime.datetime(2026, 9, 24, 3, 23, tzinfo=datetime.timezone.utc)
        ctx = daily.Day(argparse.Namespace(out_dir="out", mode=None, since=None, revisit_weeks=None, spend=False),
                        {}, now).ctx
        self.assertEqual((ctx.mode, ctx.since), ("delta", "2026-09-17"))
        self.assertEqual(ctx.path(artifacts.UNIVERSE_MOVIE), os.path.join("out", "delta", "universe-movie.json"))
        self.assertEqual(ctx.path(artifacts.UNIVERSE_TV), os.path.join("out", "delta", "universe-tv.json"))
        self.assertEqual(ctx.path(artifacts.GENRES_MOODS), os.path.join("out", artifacts.GENRES_MOODS.filename))
        self.assertIn(artifacts.GENRES_MOODS, [bind(e).artifact for e in worklist.INPUTS])


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
        `check_producers.py` refuses a publish on exactly that."""
        self.assertEqual(worklist.PRODUCER, "pipeline/worklist.py")
        self.assertTrue(os.path.isfile(os.path.join(REPO, worklist.PRODUCER)))

    def test_the_universe_is_built_before_anything_is_drawn_from_it(self):
        self.assertEqual(pipeline.STAGES[0], "worklist")

    def test_the_dumps_answer_for_themselves_until_something_here_fetches_them(self):
        """The seam: TMDB's daily export comes from outside this repo, and the only thing that pulls it
        leaves it gzipped — so the `how` an operator is sent to includes the step that fetch does not do."""
        for artifact in (artifacts.EXPORT_MOVIE, artifacts.EXPORT_TV):
            producer, how, _ = pipeline.producers()[artifact.name]
            self.assertEqual(producer, "pipeline/build_worklist.py")
            self.assertIn("gunzip", how)
            self.assertTrue(os.path.isfile(os.path.join(REPO, producer)))


if __name__ == "__main__":
    unittest.main()
