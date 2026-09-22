"""`build-worklist.py` — where it looks for the shipped labels, and how it fetches TMDB's daily dumps.

No network: `urlopen` is replaced in every test that could reach it.
"""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))


def load(out_dir, labels=None):
    """The script as a module, imported under this environment (it reads OUT_DIR and LABELS at import)."""
    env = {"OUT_DIR": out_dir}
    if labels:
        env["LABELS"] = labels
    with mock.patch.dict(os.environ, env):
        if not labels:
            os.environ.pop("LABELS", None)
        spec = importlib.util.spec_from_file_location("build_worklist", os.path.join(HERE, "build-worklist.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class Labels(unittest.TestCase):
    def test_the_default_is_the_out_dirs_labels(self):
        """The labels `finalize` leaves in the out-dir, the file the worklist stage reads as `vector_labels`.
        The old default pointed into the den app's bundle, which no longer holds them."""
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(load(out).LABELS, os.path.join(out, "labels-t02.json"))

    def test_an_explicit_path_wins(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(load(out, labels="/elsewhere/labels-t02.json").LABELS, "/elsewhere/labels-t02.json")

    def test_missing_labels_refuse_before_any_download(self):
        with tempfile.TemporaryDirectory() as out:
            module = load(out)
            with mock.patch("urllib.request.urlopen") as urlopen, self.assertRaises(SystemExit) as refused:
                module.main()
            urlopen.assert_not_called()
            self.assertIn(os.path.join(out, "labels-t02.json"), str(refused.exception))


class Fetch(unittest.TestCase):
    def test_the_dumps_are_fetched_over_https(self):
        with tempfile.TemporaryDirectory() as out:
            module = load(out)
            seen = []

            def urlopen(req):
                seen.append(req.full_url)
                return contextlib.nullcontext(io.BytesIO(b"dump"))

            with mock.patch("urllib.request.urlopen", side_effect=urlopen), \
                    contextlib.redirect_stdout(io.StringIO()):
                module.fetch_export("movie")
            self.assertTrue(seen and seen[0].startswith("https://files.tmdb.org/"), seen)

    def test_a_failed_fetch_says_why(self):
        """Both days failing must name the errors; a bare "could not fetch" hides a TLS or network fault."""
        with tempfile.TemporaryDirectory() as out:
            module = load(out)
            boom = urllib.error.URLError("certificate verify failed")
            with mock.patch("urllib.request.urlopen", side_effect=boom), self.assertRaises(SystemExit) as failed:
                module.fetch_export("tv_series")
            self.assertIn("certificate verify failed", str(failed.exception))


if __name__ == "__main__":
    unittest.main()
