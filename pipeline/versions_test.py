"""`pipeline/versions.py`: each rule the #112 measurement chose, on the cases that chose it."""
import unittest

from pipeline import versions


def title(key, **facts):
    media, tmdb_id = key.split(":")
    return {"mediaType": media, "tmdbId": int(tmdb_id), **facts}


def derived(records, sources):
    versions.derive(records, sources)
    return {versions.key_of(r): {v["key"]: v["kind"] for v in r.get("otherVersions") or []} for r in records}


class DeriveTest(unittest.TestCase):
    def test_adaptations_of_one_book_are_versions_whatever_their_names(self):
        # Emma and Clueless share a country and no name: a book's versions are not tested.
        got = derived([title("movie:1", basedOn=["Q1"], countries=["GB"], titles={"en": "Emma"}),
                       title("movie:2", basedOn=["Q1"], countries=["GB"], titles={"en": "Clueless"}),
                       title("movie:3", countries=["GB"])],
                      {"Q1": {"kind": "book", "titles": []}})
        self.assertEqual(got, {"movie:1": {"movie:2": "source"}, "movie:2": {"movie:1": "source"}, "movie:3": {}})

    def test_a_remake_links_to_its_original_and_to_the_other_remakes(self):
        # Seven Samurai is a title here; both Magnificent Sevens are based on it.
        got = derived([title("movie:346", countries=["JP"], languages=["JA"]),
                       title("movie:966", basedOn=["Q189540"], countries=["US"], languages=["EN"],
                             titles={"en": "The Magnificent Seven"}),
                       title("movie:333484", basedOn=["Q189540"], countries=["US"], languages=["EN"],
                             titles={"en": "The Magnificent Seven"})],
                      {"Q189540": {"kind": "screen", "titles": ["movie:346"]}})
        self.assertEqual(got["movie:346"], {"movie:966": "remake", "movie:333484": "remake"})
        self.assertEqual(got["movie:966"], {"movie:346": "remake", "movie:333484": "remake"})

    def test_derived_from_links_like_based_on(self):
        # Infernal Affairs names The Departed as its derivative work; The Departed states nothing.
        got = derived([title("movie:10775", countries=["HK"]),
                       title("movie:1422", derivedFrom=["Q714057"], countries=["HK", "US"])],
                      {"Q714057": {"titles": ["movie:10775"]}})
        self.assertEqual(got["movie:1422"], {"movie:10775": "remake"})

    def test_a_character_or_a_franchise_is_no_story(self):
        records = [title("movie:1", basedOn=["Q2695156"], countries=["US"], titles={"en": "Batman"}),
                   title("movie:2", basedOn=["Q2695156"], countries=["US"], titles={"en": "Batman"})]
        for kind in ("character", "franchise"):
            self.assertEqual(derived(records, {"Q2695156": {"kind": kind, "titles": []}}),
                             {"movie:1": {}, "movie:2": {}}, kind)

    def test_a_sequel_is_not_a_version(self):
        got = derived([title("movie:138", basedOn=["Q41542"]),
                       title("movie:22440", basedOn=["Q41542"], follows=["Q279378"]),
                       title("movie:11868", basedOn=["Q41542"])],
                      {"Q41542": {"kind": "book", "titles": []}})
        self.assertEqual(got["movie:22440"], {})
        self.assertEqual(got["movie:138"], {"movie:11868": "source"})

    def test_a_spin_off_of_its_own_parent_show_is_left_out(self):
        # Better Call Saul and Breaking Bad share a media franchise; Metástasis remakes Breaking Bad.
        got = derived([title("tv:1396", countries=["US"], languages=["EN"], mediaFranchise=["Q113461786"]),
                       title("tv:60059", basedOn=["Q1079"], countries=["US"], languages=["EN"],
                             mediaFranchise=["Q113461786"]),
                       title("tv:72812", basedOn=["Q1079"], countries=["CO"], languages=["ES"])],
                      {"Q1079": {"kind": "screen", "titles": ["tv:1396"]}})
        self.assertEqual(got["tv:60059"], {})
        self.assertEqual(got["tv:1396"], {"tv:72812": "remake"})

    def test_through_a_screen_work_only_a_foreign_or_same_named_title_is_a_version(self):
        # Same country, new name: a spin-off (What We Do in the Shadows -> Wellington Paranormal).
        spin_off = derived([title("movie:1", countries=["NZ"], languages=["EN"],
                                  titles={"en": "What We Do in the Shadows"}),
                            title("tv:2", basedOn=["Q5"], countries=["NZ"], languages=["EN"],
                                  titles={"en": "Wellington Paranormal"})],
                           {"Q5": {"kind": "screen", "titles": ["movie:1"]}})
        self.assertEqual(spin_off["tv:2"], {})
        # Same country, same name, the article aside: a remake.
        same_name = derived([title("movie:1", countries=["US"], titles={"en": "A Star Is Born"}),
                             title("movie:2", basedOn=["Q5"], countries=["US"], titles={"en": "a star is born"})],
                            {"Q5": {"kind": "screen", "titles": ["movie:1"]}})
        self.assertEqual(same_name["movie:2"], {"movie:1": "remake"})
        # Another original language: a remake.
        foreign = derived([title("movie:1", countries=["FR"], languages=["FR"], titles={"en": "Nikita"}),
                           title("movie:2", basedOn=["Q5"], countries=["FR"], languages=["EN"],
                                 titles={"en": "Point of No Return"})],
                          {"Q5": {"kind": "screen", "titles": ["movie:1"]}})
        self.assertEqual(foreign["movie:2"], {"movie:1": "remake"})

    def test_a_source_past_the_cutoff_groups_nothing(self):
        records = [title(f"movie:{i}", basedOn=["Q1845"]) for i in range(versions.CUTOFF + 1)]
        got = derived(records, {"Q1845": {"kind": "book", "titles": []}})
        self.assertFalse(any(got.values()))
        got = derived(records[:versions.CUTOFF], {"Q1845": {"kind": "book", "titles": []}})
        self.assertEqual(len(got["movie:0"]), versions.CUTOFF - 1)

    def test_an_unresolved_source_groups_nothing(self):
        # A facts file from before `sources` existed: no other versions, rather than versions no rule checked.
        got = derived([title("movie:1", basedOn=["Q1"]), title("movie:2", basedOn=["Q1"])], {})
        self.assertEqual(got, {"movie:1": {}, "movie:2": {}})

    def test_a_remake_link_wins_over_a_shared_source_and_links_are_symmetric(self):
        # Let Me In: based on the novel and on the 2008 film; the 2022 series only on the novel.
        got = derived([title("movie:13310", basedOn=["Q628410"], countries=["SE"], languages=["SV"]),
                       title("movie:41402", basedOn=["Q628410", "Q144756"], countries=["US"], languages=["EN"]),
                       title("tv:136735", basedOn=["Q628410"], countries=["US"], languages=["EN"])],
                      {"Q628410": {"kind": "book", "titles": []},
                       "Q144756": {"kind": "screen", "titles": ["movie:13310"]}})
        self.assertEqual(got["movie:41402"], {"movie:13310": "remake", "tv:136735": "source"})
        self.assertEqual(got["movie:13310"], {"movie:41402": "remake", "tv:136735": "source"})
        for key, found in got.items():
            for other, kind in found.items():
                self.assertEqual(got[other][key], kind)

    def test_a_record_that_had_versions_loses_them_when_none_remain(self):
        records = [title("movie:1", otherVersions=[{"key": "movie:2", "kind": "source"}]), title("movie:2")]
        self.assertEqual(versions.derive(records, {}), 0)
        self.assertNotIn("otherVersions", records[0])


if __name__ == "__main__":
    unittest.main()
