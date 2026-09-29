#!/usr/bin/env python3
"""
Tests for media_utils.sniff / mostly_empty (no drive or database needed).

    python -m unittest discover -s tests -v
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import media_utils as mu                                                 # noqa: E402


class SniffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def write(self, name, data):
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def image_bytes(self, fmt):
        b = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(b, fmt)
        return b.getvalue()

    def test_real_formats_ignore_the_name(self):
        self.assertEqual(mu.sniff(self.write("a.png", self.image_bytes("JPEG"))), "jpeg")   # JPEG named .png
        self.assertEqual(mu.sniff(self.write("b.jpg", self.image_bytes("PNG"))), "png")
        self.assertEqual(mu.sniff(self.write("c.jpg", self.image_bytes("TIFF"))), "tiff")

    def test_videos_and_junk(self):
        self.assertEqual(mu.sniff(self.write("v.jpg", b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ")), "mov")
        self.assertEqual(mu.sniff(self.write("h.heic", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1")), "heic")
        self.assertEqual(mu.sniff(self.write("m.jpg", b"\x00\x00\x01\xba" + b"\x44" * 28)), "mpeg")
        self.assertEqual(mu.sniff(self.write("e.jpg", b"")), "empty")
        self.assertEqual(mu.sniff(self.write("z.jpg", b"\x00" * 64)), "zeros")
        self.assertEqual(mu.sniff(self.write("._x.jpg", b"\x00\x05\x16\x07" + b"\x00" * 28)), "appledouble")

    def test_mostly_empty(self):
        good = self.image_bytes("JPEG") + bytes(range(256)) * 1024        # real data to the end
        lost = self.image_bytes("JPEG") + b"\x00" * (1 << 18)             # header, then nothing
        padded = self.image_bytes("JPEG") + bytes(range(256)) * 2048 + b"\x00" * (1 << 17)
        self.assertFalse(mu.mostly_empty(self.write("good.jpg", good)))
        self.assertTrue(mu.mostly_empty(self.write("lost.jpg", lost)))
        self.assertFalse(mu.mostly_empty(self.write("padded.jpg", padded)))   # zero padding at the end only
        self.assertFalse(mu.mostly_empty(self.write("small.jpg", self.image_bytes("JPEG"))))  # too small to judge

    def test_decodes(self):
        self.assertTrue(mu.decodes(self.write("ok.jpg", self.image_bytes("JPEG"))))
        self.assertFalse(mu.decodes(self.write("bad.jpg", b"\xff\xd8\xff\xe1" + b"\x00" * 5000)))


if __name__ == "__main__":
    unittest.main()
