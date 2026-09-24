#!/usr/bin/env python3
"""Franchise groups from Wikidata alone, on titles shaped like the cases oxyc/den-atlas#92 measured."""
import unittest

from . import franchise_groups as fg


class Groups(unittest.TestCase):
    def test_a_parent_holds_its_childrens_titles_and_a_lone_title_groups_nothing(self):
        titles = {
            "movie:1": fg.Title("movie:1", "Spider-Man", 2002, series=["Qraimi"]),
            "movie:2": fg.Title("movie:2", "Spider-Man 2", 2004, series=["Qraimi"]),
            "movie:3": fg.Title("movie:3", "The Amazing Spider-Man", 2012, series=["Qwebb"]),
            "movie:4": fg.Title("movie:4", "The Amazing Spider-Man 2", 2014, series=["Qwebb"]),
            "movie:5": fg.Title("movie:5", "Alone", 2000, series=["Qalone"]),
        }
        groups = fg.build(titles, {"Qfilm": "Spider-Man in film"}, {"Qraimi": ["Qfilm"], "Qwebb": ["Qfilm"]})
        self.assertEqual(groups["Qfilm"].members, {"movie:1", "movie:2", "movie:3", "movie:4"})
        self.assertNotIn("Qalone", groups)

    def test_a_sequel_chain_no_series_names_is_a_group_and_one_inside_a_series_is_not(self):
        titles = {
            "movie:1": fg.Title("movie:1", "Zombieland", 2009, follows=["movie:2"]),
            "movie:2": fg.Title("movie:2", "Zombieland: Double Tap", 2019, follows=["movie:1"]),
            "movie:3": fg.Title("movie:3", "Rocky", 1976, series=["Qrocky"], follows=["movie:4"]),
            "movie:4": fg.Title("movie:4", "Rocky II", 1979, series=["Qrocky"]),
        }
        groups = fg.build(titles, {"Qrocky": "Rocky"})
        self.assertEqual(groups["chain:movie:1"].members, {"movie:1", "movie:2"})
        self.assertEqual(groups["chain:movie:1"].name, "Zombieland")
        self.assertEqual(fg.chain_name(["Carry On Sergeant", "Carry On Nurse", "Carry On Cleo"]), "Carry On")
        self.assertEqual(fg.chain_name(["Pitch Black", "The Chronicles of Riddick"]), "")
        self.assertFalse(any(g.startswith("chain:movie:3") for g in groups))


class Plan(unittest.TestCase):
    def test_one_root_is_automatic_with_its_child_as_the_era(self):
        titles = {
            "movie:1": fg.Title("movie:1", "Spider-Man", 2002, series=["Qraimi"]),
            "movie:2": fg.Title("movie:2", "Spider-Man 2", 2004, series=["Qraimi"]),
            "movie:3": fg.Title("movie:3", "The Amazing Spider-Man", 2012, series=["Qwebb"]),
            "movie:4": fg.Title("movie:4", "The Amazing Spider-Man 2", 2014, series=["Qwebb"]),
        }
        groups = fg.build(titles, {"Qfilm": "Spider-Man in film"}, {"Qraimi": ["Qfilm"], "Qwebb": ["Qfilm"]})
        automatic, asked = fg.plan(titles, groups)
        self.assertEqual(automatic["movie:1"], ("Qfilm", "Qraimi"))
        self.assertEqual(automatic["movie:4"], ("Qfilm", "Qwebb"))
        self.assertEqual(asked, {})

    def test_a_studio_catalogue_is_asked_about_not_decided(self):
        """Studio Ghibli: typed a film series, its titles share no word with its name and nothing else."""
        titles = {f"movie:{i}": fg.Title(f"movie:{i}", name, 1984 + i, series=["Qghibli"])
                  for i, name in enumerate(["Spirited Away", "Princess Mononoke", "My Neighbor Totoro"])}
        groups = fg.build(titles, {"Qghibli": "Studio Ghibli Feature Films"})
        self.assertEqual(fg.flags(groups, titles), {"Qghibli": "catalogue"})
        automatic, asked = fg.plan(titles, groups)
        self.assertEqual(automatic, {})
        self.assertEqual(asked["movie:0"], ["Qghibli"])

    def test_a_trilogy_named_after_its_story_is_not_a_catalogue(self):
        titles = {f"movie:{i}": fg.Title(f"movie:{i}", f"The Hunger Games {i}", 2012 + i, series=["Qhg"])
                  for i in range(3)}
        groups = fg.build(titles, {"Qhg": "The Hunger Games"})
        self.assertEqual(fg.flags(groups, titles), {})

    def test_a_shared_universe_of_separate_stories_is_asked_about(self):
        titles = {
            "movie:1": fg.Title("movie:1", "Iron Man", 2008, series=["Qironman", "Qmcu"], characters=["Qtony"]),
            "movie:2": fg.Title("movie:2", "Iron Man 2", 2010, series=["Qironman", "Qmcu"], characters=["Qtony"]),
            "movie:3": fg.Title("movie:3", "Thor", 2011, series=["Qthor", "Qmcu"], characters=["Qthor_c"]),
            "movie:4": fg.Title("movie:4", "Thor: Ragnarok", 2017, series=["Qthor", "Qmcu"], characters=["Qthor_c"]),
        }
        groups = fg.build(titles, {"Qmcu": "Marvel Cinematic Universe", "Qironman": "Iron Man", "Qthor": "Thor"})
        self.assertEqual(fg.flags(groups, titles), {"Qmcu": "universe"})
        automatic, asked = fg.plan(titles, groups)
        self.assertEqual(asked["movie:1"], ["Qmcu", "Qironman"])

    def test_two_roots_are_asked_about(self):
        """Homecoming: in "Spider-Man in film" and in the MCU."""
        titles = {
            "movie:1": fg.Title("movie:1", "Spider-Man", 2002, series=["Qsm"]),
            "movie:2": fg.Title("movie:2", "Spider-Man: Homecoming", 2017, series=["Qsm", "Qmcu"]),
            "movie:3": fg.Title("movie:3", "Iron Man", 2008, series=["Qmcu"]),
        }
        groups = fg.build(titles, {"Qsm": "Spider-Man in film", "Qmcu": "Marvel Cinematic Universe"})
        automatic, asked = fg.plan(titles, groups)
        self.assertEqual(sorted(asked["movie:2"]), ["Qmcu", "Qsm"])
        self.assertNotIn("movie:2", automatic)

    def test_groups_whose_names_share_a_rare_word_are_shown_together(self):
        """Beck: the 1997– films' series and the novels the 1993 films adapt. Nothing on Wikidata links
        them, so both are asked, each shown the other."""
        titles = {
            "movie:1": fg.Title("movie:1", "Beck – Mannen med ikonerna", 1997, series=["Qbeck"]),
            "movie:2": fg.Title("movie:2", "Beck – Vita nätter", 1998, series=["Qbeck"]),
            "movie:3": fg.Title("movie:3", "Roseanna", 1993, sources=["Qnovels"]),
            "movie:4": fg.Title("movie:4", "The Man on the Balcony", 1993, sources=["Qnovels"]),
        }
        groups = fg.build(titles, {"Qbeck": "Beck", "Qnovels": "Martin Beck"})
        automatic, asked = fg.plan(titles, groups)
        self.assertEqual(automatic, {})
        self.assertEqual(asked["movie:1"], ["Qbeck", "Qnovels"])
        self.assertEqual(asked["movie:3"], ["Qnovels", "Qbeck"])

    def test_a_shared_character_outside_the_group_is_asked_about(self):
        titles = {
            "movie:1": fg.Title("movie:1", "Up", 2009, series=["Qup"], characters=["Qdug"]),
            "movie:2": fg.Title("movie:2", "Up 2", 2012, series=["Qup"]),
            "tv:3": fg.Title("tv:3", "Dug Days", 2021, characters=["Qdug"]),
        }
        groups = fg.build(titles, {"Qup": "Up"})
        automatic, asked = fg.plan(titles, groups)
        self.assertEqual(asked["movie:1"], ["Qup", ("characters", ("movie:1", "tv:3"))])
        self.assertEqual(asked["tv:3"], [("characters", ("movie:1", "tv:3"))])
        self.assertEqual(automatic["movie:2"], ("Qup", None))

    def test_the_section_lists_each_group_in_release_order(self):
        titles = {
            "movie:1": fg.Title("movie:1", "Beck – Mannen med ikonerna", 1997, series=["Qbeck"]),
            "movie:2": fg.Title("movie:2", "Beck – Spår i mörker", 1997, series=["Qbeck"]),
            "tv:3": fg.Title("tv:3", "Beck", 1997, series=["Qbeck"]),
        }
        groups = fg.build(titles, {"Qbeck": "Beck"})
        text = fg.section("movie:1", ["Qbeck"], groups, titles)
        self.assertIn('A: the Wikidata series "Beck", 3 titles: Beck – Mannen med ikonerna (1997, film); '
                      "Beck – Spår i mörker (1997, film); Beck (1997, TV series).", text)


if __name__ == "__main__":
    unittest.main()
