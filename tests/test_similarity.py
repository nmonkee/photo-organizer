#!/usr/bin/env python3
"""
Tests for the pure comparison/grouping logic (no drive or database needed).

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import similarity as sim                                                 # noqa: E402
from archive_dedupe import groups_of, SUFFIX                             # noqa: E402


def fingerprint(img):
    """Same normalisation as similarity.pixel_fingerprint, from a raw array."""
    a = img.astype(np.float32)
    return (a - a.mean()) / (a.std() + 1e-6), img.shape[1] / img.shape[0]


class RotationTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(1)
        self.photo = rng.integers(0, 255, (64, 64))                     # "portrait" 3:4 photo
        self.fa = (fingerprint(self.photo)[0], 3 / 4)

    def test_rotated_copy_is_recognised(self):
        for k in (1, 3):
            fb = (fingerprint(np.rot90(self.photo, k))[0], 4 / 3)
            self.assertFalse(sim.same_photo(self.fa, fb))               # plain check says different
            self.assertTrue(sim.same_photo_rotated(self.fa, fb))

    def test_different_photo_is_not(self):
        other = np.random.default_rng(2).integers(0, 255, (64, 64))
        self.assertFalse(sim.same_photo_rotated(self.fa, (fingerprint(other)[0], 4 / 3)))

    def test_same_shape_is_not_a_rotation(self):
        self.assertEqual(sim.rotated_difference(self.fa, self.fa), float("inf"))

    def test_brightness_edit_is_same_photo(self):
        brighter = self.photo * 0.8 + 40          # contrast/brightness edit that stays within 0-255
        self.assertTrue(sim.same_photo(self.fa, (fingerprint(brighter)[0], 3 / 4)))


class GroupingTest(unittest.TestCase):
    def test_groups_join_chains_but_not_strangers(self):
        groups = sorted(sorted(g) for g in groups_of([(1, 2), (2, 3), (10, 11)]))
        self.assertEqual(groups, [[1, 2, 3], [10, 11]])

    def test_collision_suffix(self):
        self.assertEqual(SUFFIX.match("P4260004_4d3d030b.JPG").groups(), ("P4260004", ".JPG"))
        self.assertIsNone(SUFFIX.match("IMG_1234.JPG"))
        self.assertIsNone(SUFFIX.match("IMG_0589_2.jpg"))


if __name__ == "__main__":
    unittest.main()
