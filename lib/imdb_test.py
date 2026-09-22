#!/usr/bin/env python3
"""IMDb's ratings dump: fetched conditionally, read strictly, kept only above the floor that is asked of it."""
import gzip
import json
import os
import tempfile
import time
import unittest

from . import http, imdb

DUMP = "tconst\taverageRating\tnumVotes\ntt0000001\t5.7\t2100\ntt0000002\t5.5\t300\ntt0000003\t6.1\t40939\n"


def gz(text):
    return gzip.compress(text.encode("utf-8"))


class Server:
    """`request` over a list of answers — `(status, body, etag)` or an exception — recording the headers."""

    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []

    def __call__(self, host, path, params=None, headers=None, received=None, **_):
        self.sent.append(dict(headers or {}))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body, etag = answer
        received.update({"status": status, "etag": etag, "last-modified": "Tue, 22 Sep 2026 00:00:00 GMT"})
        return body


class Dump(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env = {"DEN_CACHE_DIR": directory.name}
        self.path = os.path.join(directory.name, "imdb", imdb.FILENAME)
        imdb._loaded.clear()
        self.addCleanup(imdb._loaded.clear)

    def write(self, body):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as fh:
            fh.write(body)
        return self.path

    def test_the_first_fetch_keeps_the_dump_and_its_validators(self):
        path, note = imdb.refresh(self.env, Server((200, gz(DUMP), '"a"')))
        self.assertEqual((path, note), (self.path, "downloaded"))
        with open(path + ".meta.json") as fh:
            self.assertEqual(json.load(fh), {"etag": '"a"', "last-modified": "Tue, 22 Sep 2026 00:00:00 GMT"})

    def test_the_next_fetch_asks_whether_it_changed_and_keeps_the_copy_on_a_304(self):
        imdb.refresh(self.env, Server((200, gz(DUMP), '"a"')))
        server = Server((304, b"", None))
        _path, note = imdb.refresh(self.env, server)
        self.assertEqual(note, "unchanged")
        self.assertEqual(server.sent[0]["If-None-Match"], '"a"')
        self.assertEqual(server.sent[0]["If-Modified-Since"], "Tue, 22 Sep 2026 00:00:00 GMT")
        self.assertEqual(imdb.parse(self.path)["tt0000003"], 40939, "the 304's empty body wrote nothing")

    def test_a_first_fetch_sends_no_validator(self):
        server = Server((200, gz(DUMP), None))
        imdb.refresh(self.env, server)
        self.assertNotIn("If-None-Match", server.sent[0])
        self.assertNotIn("If-Modified-Since", server.sent[0])

    def test_no_dump_and_no_download_is_unavailable(self):
        with self.assertRaises(imdb.Unavailable) as refused:
            imdb.refresh(self.env, Server(http.HTTPError(503, "https://datasets.imdbws.com/x")))
        self.assertIn("HTTP 503", str(refused.exception))

    def test_a_failed_download_falls_back_to_the_copy_and_says_how_old_it_is(self):
        """A count only climbs, so yesterday's dump under-admits and never over-admits."""
        self.write(gz(DUMP))
        old = time.time() - 3 * 86400
        os.utime(self.path, (old, old))
        _path, note = imdb.refresh(self.env, Server(http.HTTPError(0, "https://datasets.imdbws.com/x")))
        self.assertTrue(note.startswith("stale: "), note)
        self.assertIn("3.0 day(s)", note)

    def test_rows_below_the_minimum_are_not_kept(self):
        votes = imdb.parse(self.write(gz(DUMP)), minimum=500)
        self.assertEqual(votes, {"tt0000001": 2100, "tt0000003": 40939})

    def test_a_changed_header_is_refused_rather_than_read_by_position(self):
        with self.assertRaises(imdb.Unavailable):
            imdb.parse(self.write(gz(DUMP.replace("numVotes", "votes"))))

    def test_a_truncated_or_ungzipped_dump_is_refused(self):
        for body in (gz(DUMP)[:-12], DUMP.encode("utf-8"), b"<html>maintenance</html>"):
            with self.subTest(body=body[:10]), self.assertRaises(imdb.Unavailable):
                imdb.parse(self.write(body))

    def test_an_absent_title_or_id_is_below_every_floor(self):
        ratings = imdb.Ratings({"tt1": 10}, "unchanged")
        self.assertEqual((ratings.get("tt1"), ratings.get("tt2"), ratings.get(None)), (10, 0, 0))

    def test_one_process_reads_the_dump_once(self):
        """A drain runs batch after batch in one interpreter."""
        server = Server((200, gz(DUMP), '"a"'))
        first = imdb.ratings(500, self.env, server)
        self.assertIs(imdb.ratings(500, self.env, server), first)
        self.assertEqual(len(server.sent), 1)

    def test_the_dump_lives_under_the_cache_root_and_never_in_an_out_dir(self):
        """The licence forbids redistribution; the cache root is local and never published."""
        self.assertEqual(imdb.directory(self.env), os.path.join(self.env["DEN_CACHE_DIR"], "imdb"))


if __name__ == "__main__":
    unittest.main()
