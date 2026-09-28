#!/usr/bin/env python3
"""
archive_dedupe.py - remove near-duplicate copies (thumbnails, resized exports,
stripped "NO EXIF" copies) and face crops from the archive, keeping people tags.

THE PROBLEM THIS SOLVES
------------------------
photo_organizer.py removes byte-identical duplicates, but an old Apple
Photos library leaves many *different* files for one photo: a 360px
thumbnail, a 1024px export, an EXIF-stripped copy, Apple's face crops...
(on 27 Sep 2026 this was 87,340 of 164,689 files). This finds them and keeps
the best copy of each photo:

  1. Search   - digiKam's Haar fingerprints (no drive reads) list candidate
                copies that are bigger, or the same size but in a better
                place (Year/Month > named folders > NO EXIF > Apple previews).
  2. Verify   - the candidates' real pixels are compared (similarity.py);
                only near pixel-identical pairs count. Burst shots don't.
  3. Keep     - each copy is traced to the best file it copies.
  4. Tags     - people tags (with face boxes, scaled) on a copy are added to
                the kept original first, so no tag is lost.
  5. Move     - copies, Apple face crops and orphaned '._' files are moved to
                "<drive>/Photo Archives - removed <date>/" (same folder layout),
                never deleted. Delete that folder yourself once you're happy.

    python tools/archive_dedupe.py              # full check, report, then ask
    python tools/archive_dedupe.py --dry-run    # report only
    python tools/archive_dedupe.py --no-faces   # leave face crops alone

Needs digiKam closed for the real run (its database is updated, after a
backup). Afterwards open digiKam, let it scan, then Tools > Maintenance >
"Perform database cleaning".
"""
import argparse
import collections
import csv
import datetime
import os
import re
import sys
import tempfile
import time
from multiprocessing import Pool

import digikam_db as dk
import media_utils as mu
import similarity as sim

SKIP_EXTS = mu.RAW_EXTS | mu.VIDEO_EXTS          # never treated as copies to remove
_worker = {}


def _init_worker(db_dir, ids):
    _worker["index"] = sim.HaarIndex(db_dir, only_ids=set(ids))


def _search(iid):
    return iid, _worker["index"].nearest(iid, sim.HAAR_MAX_GAP, limit=15)


def _fingerprint(args):
    iid, path = args
    with tempfile.TemporaryDirectory() as tmp:
        return iid, sim.pixel_fingerprint(path, tmp)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--no-faces", action="store_true", help="don't move face crops")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--workers", type=int, default=8, help="CPU workers for the fingerprint search")
    args = ap.parse_args()
    say, log_path = dk.open_log("archive_dedupe")
    try:
        root = dk.archive_dir()
        lib = dk.Library(dk.connect_readonly())
        ids = [i for i in lib.images if os.path.splitext(lib.rel(i)[1])[1].lower() not in SKIP_EXTS]
        say(f"archive_dedupe  {root}  ({len(lib.images):,} files known to digiKam)")

        def size(i):
            w, h = lib.dims.get(i, (0, 0))
            return w * h

        def place(i):
            """Lower is a better home for the copy we keep: dated Year/Month folder, then the
            other named folders (Scanned prints, Screenshots...), then NO EXIF, and last of
            all Apple's previews - a preview never wins over a real file of the same size."""
            rel, name = lib.rel(i)
            if rel.startswith("/Apple previews") or mu.is_apple_preview(name):
                return 3
            if rel.startswith("/NO EXIF"):
                return 2
            return 0 if re.match(r"^/(19|20)\d\d(/|$)", rel) else 1

        def better(b, a):                       # is b a better file to keep than a?
            return size(b) > size(a) * 1.2 or (size(b) >= size(a) and place(b) < place(a))

        # 1. fingerprint search (database only)
        t = time.time()
        say(f"\n1. Searching digiKam fingerprints of {len(ids):,} photos...")
        cands = {}
        with Pool(args.workers, _init_worker, (dk.db_dir(), ids)) as pool:
            for iid, near in pool.imap_unordered(_search, ids, chunksize=200):
                if mu.is_face_crop(lib.rel(iid)[1]):
                    continue
                bs = [b for _, b in near if b in lib.images and not mu.is_face_crop(lib.rel(b)[1]) and better(b, iid)]
                if bs:
                    cands[iid] = bs[:3]
        say(f"   {len(cands):,} photos have a possible better copy ({time.time() - t:.0f}s)")

        # 2. pixel check (reads only the candidate files)
        need = sorted(set(cands) | {b for v in cands.values() for b in v})
        say(f"\n2. Comparing real pixels of {len(need):,} files (reads the drive - can take an hour)...")
        t, fps = time.time(), {}
        with Pool(4) as pool:
            for n, (iid, fp) in enumerate(pool.imap_unordered(_fingerprint, [(i, lib.path(i)) for i in need], 50)):
                fps[iid] = fp
                if n and n % 10000 == 0:
                    say(f"   {n:,} of {len(need):,}")
        copy_of = {a: next(b for b in bs if sim.same_photo(fps.get(a), fps.get(b)))
                   for a, bs in cands.items() if any(sim.same_photo(fps.get(a), fps.get(b)) for b in bs)}
        say(f"   confirmed copies: {len(copy_of):,} ({time.time() - t:.0f}s)")

        # 3. trace each copy to the file that is kept
        def keeper(a):
            seen = set()
            while a in copy_of and a not in seen:
                seen.add(a)
                a = copy_of[a]
            return a
        keep = {a: keeper(a) for a in copy_of}
        faces = [] if args.no_faces else [i for i in lib.images if mu.is_face_crop(lib.rel(i)[1])]
        orphans = [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs
                   if f.startswith("._") and mu.is_appledouble(os.path.join(d, f))]
        tag_moves = [(a, k) for a, k in keep.items() if lib.person_tags(a) - lib.person_tags(k)]

        say("\n3. Plan")
        by_kind = collections.Counter("Apple thumbnail" if mu.is_apple_preview(lib.rel(a)[1]) else
                                      "1024px export" if mu.is_1024_export(lib.rel(a)[1]) else "other copy"
                                      for a in keep)
        for k, v in by_kind.most_common():
            say(f"   {v:7,}  {k}")
        say(f"   {len(faces):7,}  face crops")
        say(f"   {len(orphans):7,}  '._' macOS metadata files")
        say(f"   people tags to carry over first: {len(tag_moves):,} copies")
        plan_csv = log_path.with_name(log_path.stem + "_plan.csv")
        with open(plan_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(("remove", "kept original"))
            w.writerows(("/".join(lib.rel(a)), "/".join(lib.rel(k))) for a, k in keep.items())
        say(f"   full list: {plan_csv}")
        if args.dry_run or not (keep or faces or orphans):
            say("\nNothing changed." if args.dry_run else "\nNothing to do.")
            return 0
        if not dk.confirm(f"Move {len(keep) + len(faces) + len(orphans):,} files to the holding folder?", args.yes):
            say("Cancelled.")
            return 1

        # 4 + 5. tags, then moves
        conn, _ = dk.connect_for_writing(say)
        wlib = dk.Library(conn)
        hold = dk.holding_dir(f"copies {datetime.date.today()}")
        tags = collections.Counter()
        moved = 0
        with conn:                              # tags are saved before any file moves
            for a, k in tag_moves:
                tags.update(dk.copy_person_tags(conn, wlib, a, k))
        moved_csv = log_path.with_name(log_path.stem + "_moved.csv")
        with open(moved_csv, "w", newline="") as mf:
            mlog = csv.writer(mf)
            mlog.writerow(("reason", "moved from", "moved to", "kept original"))
            for reason, items in (("copy", list(keep)), ("face crop", faces)):
                for i in items:
                    src = wlib.path(i)
                    if src and src.exists():
                        mlog.writerow((reason, src, dk.move_out(src, root, hold),
                                       wlib.path(keep[i]) if reason == "copy" else ""))
                        moved += 1
            for p in orphans:
                if os.path.exists(p):
                    mlog.writerow(("'._' file", p, dk.move_out(p, root, hold), ""))
                    moved += 1
        say(f"\nPeople tags carried over: {dict(tags)}")
        say(f"Moved {moved:,} files to: {hold}  (list: {moved_csv})")
        say(f"Database check: {conn.execute('PRAGMA integrity_check').fetchone()[0]}")
        say("Next: open digiKam, let it scan, then Tools > Maintenance > 'Perform database cleaning'.")
        return 0
    except dk.Abort as e:
        say(f"\nSTOPPED: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
