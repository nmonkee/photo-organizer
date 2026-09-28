#!/usr/bin/env python3
"""
Tests for the database-changing helpers in tools/digikam_db.py.

Runs on a throw-away copy of digiKam's database and a few photos copied from
the archive into a temporary folder - the real database and archive are only
read. Needs the archive drive mounted.

    python -m unittest discover -s tests -v
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import digikam_db as dk                                                 # noqa: E402


class WriteHelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="photo-tools-test-"))
        real = dk.connect_readonly()
        real_lib = dk.Library(real)
        # a photo with a people tag AND a face box, and a same-shaped photo without that tag
        row = real.execute("""
            SELECT p.imageid, p.tagid FROM ImageTagProperties p JOIN Images i ON i.id = p.imageid
            WHERE p.property = 'tagRegion' AND i.status = 1 LIMIT 1""").fetchone()
        cls.src_id, cls.tag = row
        w, h = real_lib.dims[cls.src_id]
        cls.dst_id = next(i for i, d in real_lib.dims.items()
                          if i in real_lib.images and i != cls.src_id and d == (w * 2, h * 2)
                          and cls.tag not in real_lib.person_tags(i)
                          ) if any(d == (w * 2, h * 2) for d in real_lib.dims.values()) else None
        if cls.dst_id is None:   # fall back to any photo of the same aspect ratio
            cls.dst_id = next(i for i, (a, b) in real_lib.dims.items()
                              if i in real_lib.images and i != cls.src_id and a and b and w and h
                              and abs(a / b - w / h) < 0.001 and cls.tag not in real_lib.person_tags(i))
        cls.archive = real_lib.roots[1]
        # copy the database and the two photos into the temp area
        (cls.tmp / "db").mkdir()
        copy = sqlite3.connect(cls.tmp / "db" / "digikam4.db")
        real.backup(copy)
        copy.close()
        cls.root = cls.tmp / "Photo Archives"
        for iid in (cls.src_id, cls.dst_id):
            rel, name = real_lib.rel(iid)
            (cls.root / rel.lstrip("/")).mkdir(parents=True, exist_ok=True)
            shutil.copy2(real_lib.path(iid), cls.root / rel.lstrip("/") / name)
        real.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.conn = sqlite3.connect(self.tmp / "db" / "digikam4.db")
        self.lib = dk.Library(self.conn)
        self.lib.roots = {1: self.root}          # point the copy at the temp folder

    def tearDown(self):
        self.conn.rollback()                     # each test starts from the same copy
        self.conn.close()

    def test_move_keeps_record_and_tags(self):
        before_tags = self.lib.person_tags(self.src_id)
        old = self.lib.path(self.src_id)
        new = dk.move_within_archive(self.conn, self.lib, self.src_id, 1, "/Test/Moved here")
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual(new.parent, self.root / "Test/Moved here")
        album, name = self.conn.execute("SELECT album, name FROM Images WHERE id=?", (self.src_id,)).fetchone()
        self.assertEqual(self.conn.execute("SELECT relativePath FROM Albums WHERE id=?", (album,)).fetchone()[0],
                         "/Test/Moved here")
        self.assertEqual(name, new.name)
        self.assertIn(("/Test",), self.conn.execute("SELECT relativePath FROM Albums").fetchall())  # parent made
        self.assertEqual(self.lib.person_tags(self.src_id), before_tags)
        new.rename(old)                          # put the file back for other tests

    def test_move_never_overwrites(self):
        target = self.root / "Test/Clash"
        target.mkdir(parents=True, exist_ok=True)
        name = self.lib.path(self.src_id).name
        (target / name).write_bytes(b"someone else")
        old = self.lib.path(self.src_id)
        new = dk.move_within_archive(self.conn, self.lib, self.src_id, 1, "/Test/Clash")
        self.assertEqual((target / name).read_bytes(), b"someone else")
        self.assertNotEqual(new.name, name)
        self.assertIn(" (2)", new.name)
        new.rename(old)

    def test_copy_tags_scales_face_box(self):
        counts = dk.copy_person_tags(self.conn, self.lib, self.src_id, self.dst_id)
        self.assertEqual(counts["with box"] + counts["already there"], len(self.lib.person_tags(self.src_id)))
        self.assertIn(self.tag, self.lib.person_tags(self.dst_id))
        src_box = self.conn.execute("SELECT value FROM ImageTagProperties WHERE imageid=? AND tagid=? AND "
                                    "property='tagRegion'", (self.src_id, self.tag)).fetchone()[0]
        dst_box = self.conn.execute("SELECT value FROM ImageTagProperties WHERE imageid=? AND tagid=? AND "
                                    "property='tagRegion'", (self.dst_id, self.tag)).fetchone()[0]
        import re
        s = list(map(int, re.findall(r'"(-?\d+)"', src_box)))
        d = list(map(int, re.findall(r'"(-?\d+)"', dst_box)))
        (ws, hs), (wd, hd) = self.lib.dims[self.src_id], self.lib.dims[self.dst_id]
        self.assertAlmostEqual(d[0], s[0] * wd / ws, delta=1)
        self.assertAlmostEqual(d[3], s[3] * hd / hs, delta=1)
        again = dk.copy_person_tags(self.conn, self.lib, self.src_id, self.dst_id)
        self.assertEqual(again["with box"] + again["tag only"], 0)          # no duplicates on re-run

    def test_move_out_keeps_path_and_refuses_overwrite(self):
        hold = self.tmp / "holding"
        src = self.lib.path(self.dst_id)
        backup = src.read_bytes()
        dst = dk.move_out(src, self.root, hold)
        self.assertEqual(dst, hold / src.relative_to(self.root))
        self.assertFalse(src.exists())
        src.write_bytes(backup)
        with self.assertRaises(dk.Abort):
            dk.move_out(src, self.root, hold)
        self.assertTrue(src.exists())
        dst.unlink()

    def test_write_refused_while_digikam_runs(self):
        original = dk.digikam_running
        dk.digikam_running = lambda: True
        try:
            with self.assertRaises(dk.Abort):
                dk.connect_for_writing(say=lambda m: None, directory=self.tmp / "db")
        finally:
            dk.digikam_running = original

    def test_write_connection_makes_backup(self):
        original, dk.BACKUP_PARENT = dk.BACKUP_PARENT, self.tmp / "backups"
        dk_running, dk.digikam_running = dk.digikam_running, lambda: False
        try:
            conn, backup = dk.connect_for_writing(say=lambda m: None, directory=self.tmp / "db")
            conn.close()
            self.assertTrue((backup / "digikam4.db").exists())
            n = sqlite3.connect(backup / "digikam4.db").execute("SELECT count(*) FROM Images").fetchone()[0]
            self.assertGreater(n, 1000)
        finally:
            dk.BACKUP_PARENT, dk.digikam_running = original, dk_running


if __name__ == "__main__":
    unittest.main()
