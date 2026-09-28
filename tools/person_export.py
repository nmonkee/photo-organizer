#!/usr/bin/env python3
"""
person_export.py - copy every photo tagged with a person in digiKam into a
folder for that person.

THE PROBLEM THIS SOLVES
------------------------
digiKam knows who is in each photo, but the photos are spread across
hundreds of Year/Month folders. This gathers one person's photos into a
single folder (e.g. to share, or to make a book), without touching the
archive: files are copied, never moved, and digiKam's database is only read.

Then run person_sort.py on the new folder to remove copies and file the
photos by date.

    python tools/person_export.py "Jane Smith"                # -> ~/Pictures/Jane Smith
    python tools/person_export.py "Jane Smith" --dry-run
    python tools/person_export.py "John Smith" --dest ~/Desktop/John

Safe to re-run: photos already in the folder are skipped. Export into a new
folder rather than one person_sort.py has already sorted (sorted files are
renamed, so they would be copied again).
"""
import argparse
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

import digikam_db as dk

DEFAULT_DEST_PARENT = Path.home() / "Pictures"


def tagged_images(conn, person):
    tag_ids = [r[0] for r in conn.execute("SELECT id FROM Tags WHERE name = ? COLLATE NOCASE", (person,))]
    if not tag_ids:
        raise dk.Abort(f"No digiKam tag named {person!r}.")
    marks = ",".join("?" * len(tag_ids))
    return conn.execute(f"""
        SELECT DISTINCT a.albumRoot, a.relativePath, i.name
        FROM ImageTags it JOIN Images i ON i.id = it.imageid JOIN Albums a ON a.id = i.album
        WHERE it.tagid IN ({marks}) AND i.status = 1
        ORDER BY a.relativePath, i.name""", tag_ids).fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("person", help='digiKam people tag, e.g. "Jane Smith"')
    ap.add_argument("--dest", type=Path, help="destination folder (default: ~/Pictures/<person>)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be copied, copy nothing")
    args = ap.parse_args()

    say, log_path = dk.open_log("person_export")
    try:
        dest = (args.dest or DEFAULT_DEST_PARENT / args.person).expanduser()
        conn = dk.connect_readonly()
        roots = dk.resolve_album_roots(conn)
        images = tagged_images(conn, args.person)
        say(f"{len(images)} photos tagged {args.person!r}")

        # Same name from different folders: prefix the folder (2004_June_P6260227.JPG).
        name_counts = Counter(n.lower() for _, _, n in images)
        # The Mac's disk ignores case but the archive drive doesn't (IMG_1.JPG and IMG_1.jpg
        # can be different photos), so track names ignoring case and add _2, _3...
        used = set()
        copied = skipped = missing = 0
        if not args.dry_run:
            dest.mkdir(parents=True, exist_ok=True)
        for root_id, rel, name in images:
            src = roots[root_id] / rel.lstrip("/") / name if root_id in roots else None
            if not src or not src.is_file():
                missing += 1
                continue
            out = name
            if name_counts[name.lower()] > 1:
                prefix = rel.strip("/").replace("/", "_")
                out = f"{prefix}_{name}" if prefix else name
            stem, ext = os.path.splitext(out)
            k = 1
            while out.lower() in used:
                k += 1
                out = f"{stem}_{k}{ext}"
            used.add(out.lower())
            target = dest / out
            if target.exists() and target.stat().st_size == src.stat().st_size:
                skipped += 1
                continue
            if not args.dry_run:
                shutil.copy2(src, target)          # keeps the original timestamps
            copied += 1
        verb = "would copy" if args.dry_run else "copied"
        say(f"{verb} {copied}, already there {skipped}, missing {missing} -> {dest}")
        if missing:
            say("  (missing = the archive drive isn't mounted, or files moved since digiKam last scanned)")
        if not args.dry_run and copied:
            say(f"\nNext: python tools/person_sort.py \"{dest}\" --dry-run")
        return 0
    except dk.Abort as e:
        say(f"\nSTOPPED: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
