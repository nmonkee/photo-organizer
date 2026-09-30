#!/usr/bin/env python3
"""
import_tags.py - carry people tags and face boxes from another digiKam database
(e.g. one made on a separate copy of the photos) into the main one.

THE PROBLEM THIS SOLVES
------------------------
Someone tagged faces in digiKam on a different collection - here the photos
from a relative's old laptop, with its own digikam4.db. After import_check.py has
copied the new photos into the archive (and found which ones the archive
already had), those tags would be lost unless moved across. This reads the
other database (never changing it) and, for each tagged photo, adds the same
people to the matching file in the main database:

  * a photo import_check.py copied in  -> same file, face boxes copied as they are
  * a photo the archive already had    -> the archive's version gets the tags;
                                          face boxes are scaled when the two have
                                          the same shape, otherwise tag only

People missing from the main database are created under People. Tags already
present are left alone, so re-running adds nothing twice.

    python tools/import_tags.py "/Volumes/Backup/Old Laptop/Photos" --dry-run
    python tools/import_tags.py "/Volumes/Backup/Old Laptop/Photos"

The other folder's digikam4.db is read from that folder. The matches come from
the newest import_check_*.csv / *_imported.csv logs (or pass --check-csv and
--imported-csv). Run after digiKam has scanned the imported photos in; needs
digiKam closed (its databases are backed up first).
"""
import argparse
import collections
import csv
import glob
import os
import re
import sqlite3
import sys

import digikam_db as dk

MATCHED = ("identical", "same shot", "same picture")


def newest(pattern):
    found = sorted(glob.glob(str(dk.LOG_DIR / pattern)))
    return found[-1] if found else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="the other collection's folder (holds its digikam4.db)")
    ap.add_argument("--check-csv", help="import_check.py result list (default: newest)")
    ap.add_argument("--imported-csv", help="import_check.py *_imported.csv (default: newest)")
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()
    say, log_path = dk.open_log("import_tags")
    try:
        src = os.path.abspath(os.path.expanduser(args.folder))
        other = sqlite3.connect(f"file:{os.path.join(src, 'digikam4.db')}?mode=ro&immutable=1", uri=True)
        root_path = other.execute("SELECT specificPath FROM AlbumRoots").fetchone()[0]
        people_pid = other.execute("SELECT id FROM Tags WHERE name='People' AND pid=0").fetchone()[0]
        tag_name = {i: n for i, n in other.execute("SELECT id, name FROM Tags WHERE pid=?", (people_pid,))
                    if n not in dk.PEOPLE_EXCLUDE}

        # (source file, person) -> [face boxes]  from the other database
        faces = collections.defaultdict(list)
        dims = {}
        for iid, rel, name, tid, w, h in other.execute("""
                SELECT i.id, a.relativePath, i.name, t.tagid, ii.width, ii.height
                FROM ImageTags t JOIN Images i ON i.id=t.imageid JOIN Albums a ON a.id=i.album
                LEFT JOIN ImageInformation ii ON ii.imageid=i.id WHERE i.status=1"""):
            if tid not in tag_name:
                continue
            path = os.path.normpath(os.path.join(src, rel.lstrip("/"), name))
            dims[path] = (w or 0, h or 0)
            boxes = [v for (v,) in other.execute("SELECT value FROM ImageTagProperties WHERE imageid=? "
                                                  "AND tagid=? AND property='tagRegion'", (iid, tid))]
            faces[(path, tag_name[tid])].extend(boxes)
        say(f"import_tags  {src}  (its root in digiKam: {root_path})")
        say(f"  tagged faces in the other database: {len(faces):,} on {len({p for p, _ in faces}):,} photos")

        # where each source file ended up
        check_csv = args.check_csv or newest("import_check_*[0-9].csv")
        imported_csv = args.imported_csv or newest("import_check_*_imported.csv")
        target = {}
        for row in csv.reader(open(check_csv)):
            if row[0] in MATCHED:
                target[os.path.normpath(row[1])] = (row[2], row[0])
        if imported_csv:
            for row in list(csv.reader(open(imported_csv)))[1:]:
                target[os.path.normpath(row[0])] = (row[1], "imported")
        say(f"  matches from: {os.path.basename(check_csv)}" +
            (f" + {os.path.basename(imported_csv)}" if imported_csv else ""))

        lib = dk.Library(dk.connect_readonly())
        plan, missing = [], collections.Counter()
        for (path, person), boxes in faces.items():
            t = target.get(path)
            tid = lib.find(t[0]) if t else None
            if not tid:
                missing["not imported yet" if t else "no match found"] += 1
                continue
            plan.append((path, person, boxes, tid, t[1]))
        counts = collections.Counter(p for _, p, _, _, _ in plan)
        say(f"  tags to carry over: {len(plan):,}  " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
        for k, v in missing.items():
            say(f"  skipped ({k}): {v:,}" + ("  <- open digiKam so it scans the imported photos, then re-run"
                                            if k == "not imported yet" else ""))
        if args.dry_run or not plan:
            say("\nNothing changed." if args.dry_run else "\nNothing to do.")
            return 0
        if not dk.confirm(f"Add {len(plan):,} people tags to the main database?", args.yes):
            say("Cancelled.")
            return 1

        conn, _ = dk.connect_for_writing(say)
        wlib = dk.Library(conn)
        people_main = conn.execute("SELECT id FROM Tags WHERE name='People' AND pid=0").fetchone()[0]
        done = collections.Counter()
        with conn, open(log_path.with_name(log_path.stem + "_added.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(("person", "added to", "from", "how"))
            for path, person, boxes, tid, how in plan:
                if person not in wlib.people:
                    cur = conn.execute("INSERT INTO Tags(pid, name) VALUES(?,?)", (people_main, person))
                    wlib.people[person] = cur.lastrowid
                t = wlib.people[person]
                if t in wlib.person_tags(tid):
                    done["already there"] += 1
                    continue
                conn.execute("INSERT INTO ImageTags(imageid, tagid) VALUES(?,?)", (tid, t))
                (ws, hs), (wd, hd) = dims.get(path, (0, 0)), wlib.dims.get(tid, (0, 0))
                same_shape = ws and hs and wd and hd and abs(wd / ws - hd / hs) / max(wd / ws, hd / hs) < 0.03
                if boxes and same_shape:
                    sx, sy = wd / ws, hd / hs
                    for v in boxes:
                        x, y, bw, bh = map(int, re.findall(r'(?:x|y|width|height)="(-?\d+)"', v))
                        nv = f'<rect x="{round(x * sx)}" y="{round(y * sy)}" width="{round(bw * sx)}" height="{round(bh * sy)}"/>'
                        for prop in ("tagRegion", "faceToTrain"):
                            conn.execute("INSERT INTO ImageTagProperties(imageid, tagid, property, value) "
                                         "VALUES(?,?,?,?)", (tid, t, prop, nv))
                    done["with face box"] += 1
                else:
                    done["tag only"] += 1
                w.writerow((person, wlib.path(tid), path, how))
        say(f"\nAdded: {dict(done)}")
        say(f"Database check: {conn.execute('PRAGMA integrity_check').fetchone()[0]}")
        say("Next: open digiKam - the people appear under People, with faces for digiKam to learn from.")
        return 0
    except dk.Abort as e:
        say(f"\nSTOPPED: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
