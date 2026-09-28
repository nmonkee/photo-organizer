#!/usr/bin/env python3
"""
noexif_organise.py - sort the "NO EXIF" folder into things you can find.

THE PROBLEM THIS SOLVES
------------------------
photo_organizer.py puts every file it can't date into Photo Archives/NO EXIF.
Much of it isn't really undated, or isn't really a photo:

  * dated by filename    "2020-07-14 18.22.05.jpg" (Dropbox names) - or with
                         a usable creation date in its metadata
                         -> Year/Month folder, and the date is written into
                         the file's EXIF so every app sorts it correctly
  * screenshots          PNGs at iPhone/iPad screen sizes   -> Screenshots
  * scanned prints       25 Feb 2018 scanning session       -> Scanned prints
                         (their name/EXIF date is the SCAN date - never used)
  * Facebook downloads   n820540593_3360165_6049.jpg        -> Facebook downloads
  * book/calendar pages  12.tiff, 131.tiff                  -> Photo books & calendars
  * Apple previews       thumbnails whose original is gone  -> Apple previews (no original)
  * videos                                                  -> Videos (undated)
  * exact duplicates     byte-identical to another NO EXIF file -> holding folder
                         (people tags added to the copy that stays)

Anything else stays in NO EXIF. digiKam's records are updated with every
move, so people tags and faces follow the files.

    python tools/noexif_organise.py --dry-run
    python tools/noexif_organise.py

Needs digiKam closed for the real run (databases are backed up first).
"""
import argparse
import collections
import csv
import datetime
import hashlib
import os
import re
import subprocess
import sys

import digikam_db as dk
import media_utils as mu

# --------------------------------------------------------------------------- config
NOEXIF = "/NO EXIF"
FOLDERS = {"screenshot": "/Screenshots", "scanned print": "/Scanned prints",
           "Facebook download": "/Facebook downloads", "book/calendar page": "/Photo books & calendars",
           "Apple preview (no original)": "/Apple previews (no original)", "video": "/Videos (undated)"}
SCREEN_SIZES = {(2224, 1668), (1242, 2688), (1242, 2208), (640, 1136), (640, 960), (2048, 1536),
                (750, 1334), (828, 1792), (1170, 2532), (1284, 2778), (1179, 2556), (1290, 2796)}
SCREEN_SIZES |= {(h, w) for w, h in SCREEN_SIZES}
# -----------------------------------------------------------------------------------


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    return h.hexdigest()


def classify(name, meta):
    ext = os.path.splitext(name)[1].lower()
    if ext in mu.VIDEO_EXTS:
        return "video", None
    if mu.is_scanned_print(name):
        return "scanned print", None
    if re.match(r"n\d{6,}_\d+_\d+", name):
        return "Facebook download", None
    if re.match(r"\d+\.tiff?$", name, re.I):
        return "book/calendar page", None
    if ext == ".png" and (meta.get("ImageWidth"), meta.get("ImageHeight")) in SCREEN_SIZES:
        return "screenshot", None
    if mu.is_apple_preview(name):
        return "Apple preview (no original)", None
    m = re.match(r"((?:19|20)\d\d)-(\d\d)-(\d\d) (\d\d)\.(\d\d)\.(\d\d)", name)
    if m:
        try:
            return "dated by filename", datetime.datetime(*map(int, m.groups()))
        except ValueError:
            pass
    d = mu.exif_date(meta)
    if d and 1990 <= d.year <= datetime.date.today().year:
        return "dated by metadata", d
    return "undated photo", None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--no-exif-write", action="store_true", help="move dated files but don't write their date")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()
    say, log_path = dk.open_log("noexif_organise")
    try:
        root = dk.archive_dir()
        lib = dk.Library(dk.connect_readonly())
        root_id = next(r for r, p in lib.roots.items() if p == root)
        items = [i for i in lib.images if lib.rel(i)[0] == NOEXIF or lib.rel(i)[0].startswith(NOEXIF + "/")]
        say(f"noexif_organise  {root / NOEXIF.lstrip('/')}  ({len(items):,} files known to digiKam)")
        say("reading metadata...")
        meta = mu.exiftool_json([lib.path(i) for i in items])

        say("looking for byte-identical copies...")
        by_size = collections.defaultdict(list)
        for i in items:
            by_size[lib.path(i).stat().st_size].append(i)
        dup_of = {}
        for ids in (v for v in by_size.values() if len(v) > 1):
            by_hash = collections.defaultdict(list)
            for i in ids:
                by_hash[sha1(lib.path(i))].append(i)
            for same in (v for v in by_hash.values() if len(v) > 1):
                keep, *rest = sorted(same, key=lambda i: (len(lib.rel(i)[1]), lib.rel(i)[1]))
                dup_of.update({r: keep for r in rest})

        plan = []                      # (image id, category, date)
        for i in items:
            if i in dup_of:
                plan.append((i, "exact duplicate", None))
            else:
                plan.append((i, *classify(lib.rel(i)[1], meta.get(str(lib.path(i)), {}))))
        counts = collections.Counter(c for _, c, _ in plan)
        say("\nPlan:")
        for c, n in counts.most_common():
            where = ("holding folder" if c == "exact duplicate" else "stays in NO EXIF" if c == "undated photo"
                     else "Year/Month folders" if c.startswith("dated") else FOLDERS[c].lstrip("/"))
            say(f"   {n:6,}  {c:30} -> {where}")
        plan_csv = log_path.with_name(log_path.stem + "_plan.csv")
        with open(plan_csv, "w", newline="") as f:
            csv.writer(f).writerows([("file", "category", "date")] +
                                    [("/".join(lib.rel(i)), c, d.isoformat() if d else "") for i, c, d in plan])
        say(f"   full list: {plan_csv}")
        movers = [p for p in plan if p[1] != "undated photo"]
        if args.dry_run or not movers:
            say("\nNothing changed." if args.dry_run else "\nNothing to do.")
            return 0
        if not dk.confirm(f"Move {len(movers):,} files?", args.yes):
            say("Cancelled.")
            return 1

        conn, _ = dk.connect_for_writing(say)
        wlib = dk.Library(conn)
        hold = dk.holding_dir(f"NO EXIF duplicates {datetime.date.today()}")
        with conn:                                          # tags first, saved before files move
            for dup, keep in dup_of.items():
                dk.copy_person_tags(conn, wlib, dup, keep)
        dated, moved = [], collections.Counter()
        moved_csv = log_path.with_name(log_path.stem + "_moved.csv")
        with open(moved_csv, "w", newline="") as mf, conn:
            mlog = csv.writer(mf)
            mlog.writerow(("category", "moved from", "moved to"))
            for i, cat, d in movers:
                src = wlib.path(i)
                if cat == "exact duplicate":
                    dst = dk.move_out(src, root, hold)
                else:
                    folder = f"/{d.year}/{mu.MONTHS[d.month - 1]}" if d else FOLDERS[cat]
                    dst = dk.move_within_archive(conn, wlib, i, root_id, folder)
                    if d:
                        dated.append((i, dst, d))
                        conn.execute("UPDATE ImageInformation SET creationDate=? WHERE imageid=?",
                                     (d.strftime("%Y-%m-%dT%H:%M:%S.000"), i))
                mlog.writerow((cat, src, dst))
                moved[cat] += 1
        say(f"\nMoved: {dict(moved)}  (list: {moved_csv})")
        say(f"Database check: {conn.execute('PRAGMA integrity_check').fetchone()[0]}")

        if dated and not args.no_exif_write:
            failed = []
            for _, path, d in dated:
                v = d.strftime("%Y:%m:%d %H:%M:%S")
                r = subprocess.run(["exiftool", "-q", "-q", "-overwrite_original", f"-DateTimeOriginal={v}",
                                    f"-CreateDate={v}", str(path)], capture_output=True, text=True)
                if r.returncode:
                    # usually a damaged embedded thumbnail: rebuild the metadata block and retry
                    subprocess.run(["exiftool", "-q", "-q", "-overwrite_original", "-all=", "-tagsfromfile", "@",
                                    "-all:all", "-unsafe", "-icc_profile", str(path)], capture_output=True)
                    r = subprocess.run(["exiftool", "-q", "-q", "-overwrite_original", f"-DateTimeOriginal={v}",
                                        f"-CreateDate={v}", str(path)], capture_output=True, text=True)
                if r.returncode:
                    failed.append(path)
            say(f"EXIF dates written: {len(dated) - len(failed):,} of {len(dated):,}")
            for p in failed:
                say(f"   could not write date: {p}")
        say("\nNext: open digiKam and let it scan (new folders appear; changed files are re-read).")
        return 0
    except dk.Abort as e:
        say(f"\nSTOPPED: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
