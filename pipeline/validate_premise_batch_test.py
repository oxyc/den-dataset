import unittest

from pipeline import validate_premise_batch as validate

# Four plots and the tags each one earns, in the shape a generation batch carries them. The pairing is the
# July run's batch 0058 in miniature (oxyc/den-dataset#184): a climbing rescue, a zombie siege, a vampire
# next door, two teenagers who build a woman on a computer.
TITLES = [
    ("movie:11618", "A team of climbers mounts a rescue on the mountain after a crevasse traps two siblings. "
                    "A reckless billionaire has endangered the expedition and oxygen is running out.",
     ["mountain-rescue-mission", "siblings-trapped-in-crevasse", "reckless-billionaire-endangers-team",
      "oxygen-running-out", "climbers-race-the-storm", "guilt-over-father", "rescue-team-assembled",
      "expedition-gone-wrong"]),
    ("movie:11678", "Years after the dead rose, survivors live in a fortified city while zombies evolve. "
                    "A scavenging crew leaves the walls as the horde begins a siege of the city.",
     ["zombie-siege-of-fortified-city", "evolving-zombies", "scavenging-crew-outside-walls",
      "survivors-class-divide", "horde-breaches-the-walls", "dead-rise-again", "city-under-siege",
      "crew-turns-on-its-boss"]),
    ("movie:11683", "A teenager discovers that his new neighbour is a vampire. Nobody believes him, so he "
                    "recruits a washed-up horror host to hunt the vampire next door.",
     ["teen-discovers-vampire-neighbour", "nobody-believes-the-teen", "washed-up-horror-host-recruited",
      "hunt-the-vampire-next-door", "vampire-stalks-teen", "crucifix-showdown", "friends-turned",
      "neighbour-hides-a-secret"]),
    ("movie:11797", "Two outcast teenagers use a computer to create the perfect woman, who comes to life and "
                    "teaches the nerds to win popularity and love.",
     ["teens-create-perfect-woman", "computer-brings-woman-to-life", "nerds-seek-popularity-and-love",
      "outcast-teenagers-transformed", "party-gets-out-of-hand", "magic-companion-teaches", "bullies-humbled",
      "wish-fulfilment-gone-wild"]),
]


def batch(order=None):
    """Input rows in title order; output rows whose TAGS come from `order` (an index per row)."""
    order = order or list(range(len(TITLES)))
    batch_in = [{"key": key, "plot": plot} for key, plot, _tags in TITLES]
    batch_out = [{"key": TITLES[i][0], "tags": TITLES[order[i]][2]} for i in range(len(TITLES))]
    return batch_in, batch_out


class ShiftCanaryTest(unittest.TestCase):
    def test_a_correct_batch_passes(self):
        batch_in, batch_out = batch()
        self.assertEqual(validate.shifted(batch_in, batch_out), [])
        fatal, _notes = validate.check(batch_in, batch_out, strict_language=True)
        self.assertEqual(fatal, [])

    def test_tags_one_place_forward_reject_the_batch_with_every_key_present(self):
        # Every row carries the NEXT title's tags; the last repeats its own, as July's batches did.
        batch_in, batch_out = batch([1, 2, 3, 3])
        self.assertEqual({r["key"] for r in batch_out}, {r["key"] for r in batch_in})
        fatal, _notes = validate.check(batch_in, batch_out, strict_language=True)
        self.assertEqual(len(fatal), 1)
        self.assertIn("movie:11618 .. movie:11683: 3 consecutive rows carry the next row's premise", fatal[0])

    def test_tags_one_place_back_reject_the_batch(self):
        batch_in, batch_out = batch([0, 0, 1, 2])
        self.assertIn("carry the previous row's premise", " ".join(validate.shifted(batch_in, batch_out)))

    def test_a_swapped_pair_rejects_the_batch(self):
        # Batch 0313's failure: two adjacent rows exchanged tags. Each points a different way, and the pair
        # is still two consecutive rows pointing at a neighbour.
        batch_in, batch_out = batch([0, 2, 1, 3])
        self.assertTrue(validate.shifted(batch_in, batch_out))

    def test_one_row_that_fits_a_neighbour_is_not_a_shift(self):
        # A sequel or a shared setting can make one row read like the next; one is chance, not a slip.
        batch_in, batch_out = batch([1, 1, 2, 3])
        self.assertEqual(validate.shifted(batch_in, batch_out), [])

    def test_a_plot_in_another_language_neither_points_nor_breaks(self):
        batch_in, batch_out = batch()
        batch_in[1]["plot"] = "Anni dopo il ritorno dei morti, i sopravvissuti vivono in una città fortificata."
        self.assertEqual(validate.shifted(batch_in, batch_out), [])

    def test_rows_with_no_tags_or_no_plot_are_skipped(self):
        batch_in, batch_out = batch()
        batch_in[0]["plot"] = ""
        batch_out[2]["tags"] = []
        self.assertEqual(validate.shifted(batch_in, batch_out), [])


if __name__ == "__main__":
    unittest.main()
