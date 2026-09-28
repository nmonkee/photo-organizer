#!/usr/bin/env python3
"""
similarity.py - find copies of the same photo (resized exports, thumbnails,
stripped "NO EXIF" copies, renumbered re-exports) without reading every file.

THE PROBLEM THIS SOLVES
------------------------
Exact-hash deduplication (photo_organizer.py) can't see that a 360x270
thumbnail and a 5184x3456 original are the same picture. Comparing pixels
of every pair of ~150,000 photos would take days. digiKam has already
computed a "Haar" fingerprint of every photo (similarity.db), so:

  1. Search:  compare fingerprints straight from digiKam's database - all
     photos in minutes, without touching the drive - to get a short list
     of candidate copies for each photo.
  2. Verify:  open only the candidates and compare real pixels (normalised
     64x64 greyscale, so brightness/contrast edits and resizing don't
     matter). Burst shots taken a second apart differ clearly here, true
     copies don't.

Thresholds were calibrated on one exported person folder (~1,600 photos) and a 48-pair visual
check of the archive: every true copy had a fingerprint gap <= 4.3 and a
pixel difference < 0.05; burst shots and similar scenes were well above.
"""
import struct
import sqlite3
from collections import defaultdict

import numpy as np
from PIL import Image

from media_utils import open_image

HAAR_MAX_GAP = 8.0         # fingerprint search cut-off (true copies <= 4.3; margin for safety)
PIXEL_MAX_DIFF = 0.05      # mean abs difference of normalised 64x64 greyscale: copies < 0.05
ASPECT_TOLERANCE = 0.08    # copies must have (nearly) the same shape

# digiKam's weights for "scanned" images, by coefficient band (haar.cpp)
_W = np.array([[5.00, 19.21, 34.37], [0.83, 1.26, 0.36], [1.01, 0.44, 0.45],
               [0.52, 0.53, 0.14], [0.47, 0.28, 0.18], [0.30, 0.14, 0.27]])


def _band(v):
    v = abs(v)
    return min(max(v // 128, v % 128), 5)


class HaarIndex:
    """digiKam's Haar fingerprints (similarity.db) with an inverted index for fast search.
    Blob layout (Qt, big-endian): int version, 3 doubles (average Y/I/Q), 3 x 40 ints."""

    def __init__(self, db_dir, only_ids=None):
        conn = sqlite3.connect(f"file:{db_dir / 'similarity.db'}?mode=ro", uri=True)
        ids, avgs, sigs = [], [], []
        for iid, blob in conn.execute("SELECT imageid, matrix FROM ImageHaarMatrix"):
            if blob is None or len(blob) != 508 or (only_ids is not None and iid not in only_ids):
                continue
            ids.append(iid)
            avgs.append(struct.unpack(">3d", blob[4:28]))
            sigs.append(struct.unpack(">120i", blob[28:508]))
        self.ids = np.array(ids)
        self.avgs = np.array(avgs)
        self.sigs = np.array(sigs).reshape(-1, 3, 40)
        self.row = {int(i): k for k, i in enumerate(self.ids)}
        inv = [defaultdict(list) for _ in range(3)]
        for k in range(len(self.ids)):
            for c in range(3):
                for v in self.sigs[k, c]:
                    inv[c][int(v)].append(k)
        self.inv = [{v: np.array(r, dtype=np.int32) for v, r in d.items()} for d in inv]

    def __len__(self):
        return len(self.ids)

    def nearest(self, image_id, max_gap=HAAR_MAX_GAP, limit=15):
        """[(gap, other image id)] closest first; gap 0 = identical fingerprint."""
        k = self.row.get(image_id)
        if k is None:
            return []
        score = (_W[0] * np.abs(self.avgs - self.avgs[k])).sum(1)
        for c in range(3):
            for v in self.sigs[k, c]:
                score[self.inv[c][int(v)]] -= _W[_band(int(v)), c]
        gap = score - score[k]
        near = np.nonzero(gap < max_gap)[0]
        near = near[np.argsort(gap[near])][:limit + 1]
        return [(round(float(gap[j]), 2), int(self.ids[j])) for j in near if j != k][:limit]


# ------------------------------------------------------------------ pixel check
def pixel_fingerprint(path, tmpdir):
    """(normalised 64x64 greyscale array, aspect ratio) or None if unreadable."""
    try:
        im = open_image(path, tmpdir).convert("L")
    except Exception:
        return None
    a = np.asarray(im.resize((64, 64), Image.LANCZOS), dtype=np.float32)
    return (a - a.mean()) / (a.std() + 1e-6), im.size[0] / im.size[1]


def pixel_difference(fa, fb):
    """Mean absolute difference of two pixel fingerprints (inf if shapes differ)."""
    (a, ra), (b, rb) = fa, fb
    if abs(ra - rb) / max(ra, rb) >= ASPECT_TOLERANCE:
        return float("inf")
    return float(np.abs(a - b).mean())


def same_photo(fa, fb):
    return fa is not None and fb is not None and pixel_difference(fa, fb) < PIXEL_MAX_DIFF
