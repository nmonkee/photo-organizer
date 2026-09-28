#!/usr/bin/env python3
"""
archive_audit.py - health check of the organised Photo Archives, with optional fixes.

THE PROBLEM THIS SOLVES
------------------------
After years of imports, exports and tools, three things quietly go wrong:

  1. Photos in the wrong Year/Month folder - or, far more often, digiKam
     showing the wrong date: for photos re-saved by QuickTime/Photos it
     records the date the file was last edited, not when it was taken, so
     its timeline sorts them wrongly even though the folder is right.
  2. Scanned prints filed under the day they were scanned (their name and
     EXIF date are the scanning session, 25 Feb 2018).
  3. Byte-identical copies of the same file in two places (videos included).

Checks only read. Each fix needs digiKam closed, backs its databases up
first, asks before changing anything and logs every change:

    python tools/archive_audit.py                          # all checks (identical-file check is slow)
    python tools/archive_audit.py --skip-identical         # quick checks only
    python tools/archive_audit.py --fix-digikam-dates      # set digiKam's date from the photo's camera date
    python tools/archive_audit.py --move-scans             # move stray scans into "Scanned prints"
    python tools/archive_audit.py --remove-identical       # keep one of each identical set; move the
                                                           # rest to the holding folder (tags kept)

Full lists go to ~/Library/Logs/photo-tools/archive_audit_<date>.csv.
"""
import argparse
import collections
import csv
import datetime
import hashlib
import os
import re
import sys

import digikam_db as dk
import media_utils as mu


def folder_date(rel):
    """(year, month or None) for '/2013/August/...' style folders, else None."""
    m = re.match(r"^/((?:19|20)\d\d)(?:/([A-Za-z]+))?", rel)
    if not m:
        return None
    return int(m.group(1)), mu.MONTH_NUM.get((m.group(2) or "").lower())


def check_dates(lib, conn, say, rows):
    """Compare digiKam's date with the folder; confirm mismatches with the file's own EXIF."""
    created = dict(conn.execute("SELECT imageid, creationDate FROM ImageInformation"))
    suspects = []
    for iid in lib.images:
        rel, name = lib.rel(iid)
        fd, cd = folder_date(rel), created.get(iid)
        if not fd or not cd or mu.is_scanned_print(name):
            continue
        y, mo = int(cd[:4]), int(cd[5:7])
        if y != fd[0] or (fd[1] and mo != fd[1]):
            suspects.append(iid)
    say(f"  digiKam date differs from folder for {len(suspects):,} photos - reading their EXIF...")
    meta = mu.exiftool_json([lib.path(i) for i in suspects])
    counts = collections.Counter()
    fixes = []
    for iid in suspects:
        rel, name = lib.rel(iid)
        fd = folder_date(rel)
        taken = mu.exif_date(meta.get(str(lib.path(iid)), {}))
        if not taken:
            kind = "no camera date (folder from file date - leave)"
        elif taken.year == fd[0] and (not fd[1] or taken.month == fd[1]):
            kind = "digiKam date wrong (folder is right)"
            fixes.append((iid, taken))
        else:
            kind = "MISFILED (camera date disagrees with folder)"
        counts[kind] += 1
        rows.append((kind, f"{rel}/{name}", created.get(iid), taken.isoformat() if taken else ""))
    for k, v in counts.most_common():
        say(f"    {v:6,}  {k}")
    return fixes


def check_scans(lib, say, rows):
    stray = [iid for iid in lib.images if mu.is_scanned_print(lib.rel(iid)[1])
             and not lib.rel(iid)[0].startswith("/Scanned prints")]
    say(f"    {len(stray):6,}  scanned prints outside 'Scanned prints'")
    for iid in stray:
        rows.append(("stray scan", "/".join(lib.rel(iid)), "", ""))
    return stray


def _hash(path, whole):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        if whole:
            for b in iter(lambda: f.read(8 << 20), b""):
                h.update(b)
        else:                                  # first + last MB: cheap first pass
            h.update(f.read(1 << 20))
            f.seek(max(0, os.path.getsize(path) - (1 << 20)))
            h.update(f.read())
    return h.hexdigest()


def check_identical(root, say, rows):
    by_size = collections.defaultdict(list)
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".")]      # skip .dtrash etc.
        for f in files:
            if not f.startswith("."):
                p = os.path.join(d, f)
                by_size[os.path.getsize(p)].append(p)
    groups = []
    for size, ps in by_size.items():
        if size == 0 or len(ps) < 2:
            continue
        quick = collections.defaultdict(list)
        for p in ps:
            quick[_hash(p, False)].append(p)
        for qs in (v for v in quick.values() if len(v) > 1):
            full = collections.defaultdict(list)
            for p in qs:
                full[_hash(p, True)].append(p)
            groups += [v for v in full.values() if len(v) > 1]
    extra = sum(len(g) - 1 for g in groups)
    space = sum((len(g) - 1) * os.path.getsize(g[0]) for g in groups)
    say(f"    {extra:6,}  identical extra copies in {len(groups):,} groups ({space / 1e9:.1f} GB)")
    for g in groups:
        for p in g[1:]:
            rows.append(("identical copy", os.path.relpath(p, root), os.path.relpath(g[0], root), ""))
    return groups


def keep_first(group, lib):
    """Order an identical set so the one to keep comes first: most people tags, then a name
    without a '_d261fc3f' collision suffix, then the shortest name."""
    def rank(p):
        name = os.path.basename(p)
        iid = lib.find(p)
        tags = len(lib.person_tags(iid)) if iid else 0
        suffixed = re.search(r"_[0-9a-f]{8}\.[^.]+$", name) is not None
        return (-tags, suffixed, len(name), name)
    return sorted(group, key=rank)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-identical", action="store_true", help="skip the (slow) identical-file check")
    ap.add_argument("--fix-digikam-dates", action="store_true", help="set digiKam's date to the camera date")
    ap.add_argument("--move-scans", action="store_true", help="move stray scanned prints to 'Scanned prints'")
    ap.add_argument("--remove-identical", action="store_true",
                    help="keep one file of each identical set, move the others to the holding folder")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()
    say, log_path = dk.open_log("archive_audit")
    rows = [("finding", "file", "digiKam date / same as", "camera date")]
    try:
        root = dk.archive_dir()
        lib = dk.Library(dk.connect_readonly())
        say(f"archive_audit  {root}  ({len(lib.images):,} photos known to digiKam)\n")
        say("1. Dates vs folders")
        fixes = check_dates(lib, lib.conn, say, rows)
        say("2. Scanned prints")
        stray = check_scans(lib, say, rows)
        identical = []
        if args.remove_identical and args.skip_identical:
            raise dk.Abort("--remove-identical needs the identical-file check (drop --skip-identical).")
        if not args.skip_identical:
            say("3. Identical files (reads every same-size file - slow)")
            identical = check_identical(root, say, rows)
        csv_path = log_path.with_suffix(".csv")
        with open(csv_path, "w", newline="") as f:
            csv.writer(f).writerows(rows)
        say(f"\nFull list: {csv_path}")

        if not (args.fix_digikam_dates or args.move_scans or args.remove_identical):
            return 0
        todo = []
        if args.remove_identical and identical:
            todo.append(f"move {sum(len(g) - 1 for g in identical):,} identical copies to the holding folder")
        if args.fix_digikam_dates and fixes:
            todo.append(f"set digiKam's date for {len(fixes):,} photos to their camera date")
        if args.move_scans and stray:
            todo.append(f"move {len(stray):,} scanned prints to 'Scanned prints'")
        if not todo:
            say("\nNothing to fix.")
            return 0
        say("\nFixes: " + "; ".join(todo))
        if not dk.confirm("Apply these fixes?", args.yes):
            say("Cancelled.")
            return 1
        conn, _ = dk.connect_for_writing(say)
        lib = dk.Library(conn)
        with conn:
            if args.fix_digikam_dates:
                for iid, taken in fixes:
                    conn.execute("UPDATE ImageInformation SET creationDate=? WHERE imageid=?",
                                 (taken.strftime("%Y-%m-%dT%H:%M:%S.000"), iid))
                say(f"  digiKam dates corrected: {len(fixes):,}")
            if args.move_scans:
                root_id = next(r for r, p in lib.roots.items() if p == root)
                for iid in stray:
                    old = lib.path(iid)
                    new = dk.move_within_archive(conn, lib, iid, root_id, "/Scanned prints")
                    say(f"  moved {old.relative_to(root)} -> {new.relative_to(root)}")
            if args.remove_identical and identical:
                hold = dk.holding_dir(f"{datetime.date.today()} identical")
                moved, tags = 0, collections.Counter()
                with open(log_path.with_name(log_path.stem + "_moved.csv"), "w", newline="") as mf:
                    mlog = csv.writer(mf)
                    mlog.writerow(("moved from", "moved to", "identical to (kept)"))
                    for group in identical:
                        keep, *extra = keep_first(group, lib)
                        kid = lib.find(keep)
                        for p in extra:
                            if not os.path.exists(p):
                                continue
                            pid = lib.find(p)
                            if pid and kid:
                                tags.update(dk.copy_person_tags(conn, lib, pid, kid))
                            mlog.writerow((p, dk.move_out(p, root, hold), keep))
                            moved += 1
                say(f"  identical copies moved to '{hold}': {moved:,}; people tags carried over: {dict(tags)}")
        say(f"  database check: {conn.execute('PRAGMA integrity_check').fetchone()[0]}")
        say(f"\nDone. Open digiKam and let it finish scanning. Log: {log_path}")
        return 0
    except dk.Abort as e:
        say(f"\nSTOPPED: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
