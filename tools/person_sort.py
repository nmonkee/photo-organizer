#!/usr/bin/env python3
"""
person_sort.py - tidy a folder made by person_export.py.

THE PROBLEM THIS SOLVES
------------------------
An exported folder often holds several copies of each photo - the RAW, the
camera JPG, 1024px exports, Apple Photos thumbnails, stripped "NO EXIF"
copies, renumbered re-exports - plus face crops, all with unhelpful names.
This keeps the best copy of each photo, names it by the date it was taken
and files it by year:

    2013/2013-08-06 08.07.13.jpg      (dated photos)
    Undated/Family.jpg                (no date anywhere - named after the most
                                       meaningful copy, not "UNADJUSTEDNONRAW_thumb_1ac4")
    Scanned prints/p2018...jpg        (scans: their name/EXIF date is the scanning day)
    _duplicates/...                   (the other copies - check, then delete)

Face crops (facetile_*, *_face0.jpg) are deleted. A RAW-only photo is
converted to a full-size JPG (the RAW goes to _duplicates/).

How copies are recognised: a 256-bit difference hash plus camera file
numbers. Same number + similar image = same photo; different numbers are
different photos (burst shots) unless near pixel-identical (renumbered
re-exports); Apple's thumb/mini pairs are joined by their shared id; files
with no number join their single nearest match.

    python tools/person_sort.py ~/Pictures/"Jane Smith" --dry-run
    python tools/person_sort.py ~/Pictures/"Jane Smith"
    python tools/person_sort.py ~/Pictures/"Jane Smith" --resort   # undo + sort again

Only top-level files are processed. Every action is logged to _sort_log.csv
in the folder, which --resort uses to put everything back first.
"""
import argparse
import csv
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

import media_utils as mu

SAME_NUMBER_MAX_DIST = 20        # copies of one photo: <= ~20 of 256 bits differ after resizing
NO_NUMBER_MAX_DIST = 12          # un-numbered files attach only to a very close nearest match
DIFFERENT_NUMBER_MAX_DIST = 2    # renumbered re-exports are 0-2 bits apart; bursts are 3+


def dhash(im):
    a = np.asarray(im.convert("L").resize((17, 16), Image.LANCZOS), dtype=np.int16)
    return (a[:, 1:] > a[:, :-1]).ravel()


def group_copies(names, hashes):
    n = len(names)
    bits = np.array(hashes)
    dist = np.array([(bits != bits[i]).sum(1) for i in range(n)])
    numbers = [mu.camera_number(x) for x in names]
    thumb_ids = [mu.apple_thumb_id(x) for x in names]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if numbers[i] and numbers[j]:
                limit = SAME_NUMBER_MAX_DIST if numbers[i] == numbers[j] else DIFFERENT_NUMBER_MAX_DIST
                if dist[i, j] <= limit:
                    parent[find(i)] = find(j)
            if thumb_ids[i] and thumb_ids[i] == thumb_ids[j] and dist[i, j] <= SAME_NUMBER_MAX_DIST:
                parent[find(i)] = find(j)
    # Files without a number join only their nearest neighbour (preferring numbered files),
    # otherwise one thumbnail close to several burst shots would chain them all together.
    numbered = np.array([bool(x) for x in numbers])
    for i in range(n):
        if numbers[i]:
            continue
        d = dist[i].copy()
        d[i] = 1 << 30
        j = int(np.where(numbered, d, 1 << 30).argmin())
        if d[j] > NO_NUMBER_MAX_DIST:
            j = int(d.argmin())
        if d[j] <= NO_NUMBER_MAX_DIST:
            parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i, name in enumerate(names):
        groups[find(i)].append(name)
    return list(groups.values())


def pick_best(group, meta, folder):
    def score(name):
        m = meta.get(str(folder / name), {})
        pixels = (m.get("ImageWidth") or 0) * (m.get("ImageHeight") or 0)
        return (Path(name).suffix.lower() not in mu.RAW_EXTS, pixels, (folder / name).stat().st_size)
    return max(group, key=score)


def photo_date(group, best, meta, folder):
    """EXIF on the kept file, then on any copy, then dates in names."""
    ordered = [best] + [n for n in group if n != best]
    for name in ordered:
        d = mu.exif_date(meta.get(str(folder / name), {}))
        if d:
            return d, "second"
    for name in ordered:
        found = mu.date_from_name(name)
        if found:
            return found
    return None, None


def label_for(group, best):
    """Most meaningful original name: 'Family' beats 'DSC01678' beats 'UNADJUSTEDNONRAW_thumb_1ac4'."""
    def rank(name):
        stem = re.sub(r"^(NO EXIF_|\d{4}_[A-Za-z]+_)", "", Path(name).stem)
        stem = re.sub(r"\.(jpe?g|png|heic|tiff?)$", "", stem, flags=re.I)      # x.jpg.jpg
        generic = re.search(r"UNADJUSTEDNONRAW|masterthumbnail|fullsizeoutput|_(thumb|mini)_|"
                            r"^[\w+%-]{20,}$|^\d+$", stem)
        return (2 if generic else 1 if mu.camera_number(stem) else 0, name != best), stem
    return min(map(rank, group))[1]


def new_name(best, dt, precision, label):
    ext = Path(best).suffix.lower().replace(".jpeg", ".jpg").replace(".tiff", ".tif")
    if ext in mu.RAW_EXTS:
        ext = ".jpg"                      # RAW-only photo: a JPG is made from it
    if dt is None:
        return f"Undated/{label}{ext}"
    stem = {"second": dt.strftime("%Y-%m-%d %H.%M.%S"),
            "day": dt.strftime("%Y-%m-%d ") + label}.get(precision, dt.strftime("%Y-%m ") + label)
    return f"{dt.year}/{stem}{ext}"


def undo_previous_sort(folder, say):
    """Put every file logged in _sort_log.csv back at the top level under its original name."""
    log = folder / "_sort_log.csv"
    if not log.exists():
        return
    where, made = {}, []
    for action, src, dst in csv.reader(open(log)):
        if action == "delete":
            continue
        if action == "convert":
            made.append(dst)
            continue
        owner = next((o for o, p in where.items() if p == src), src) if "/" in src else src
        where[owner] = dst
    for p in made:
        (folder / p).unlink(missing_ok=True)
    restored = 0
    for original, p in where.items():
        if (folder / p).exists():         # some may have been deleted by hand
            (folder / p).rename(folder / original)
            restored += 1
    log.rename(folder / f"_sort_log_{datetime.now():%Y%m%d-%H%M%S}.csv")
    for d in sorted({(folder / p).parent for p in where.values()}, reverse=True):
        if d != folder and d.is_dir():
            (d / ".DS_Store").unlink(missing_ok=True)
            if not any(d.iterdir()):
                d.rmdir()
    say(f"undid previous sort: {restored} files back at the top level")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--resort", action="store_true", help="undo the previous sort, then sort again")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()
    folder = args.folder.expanduser().resolve()
    say = print
    if args.resort and args.dry_run:
        sys.exit("--resort can't be combined with --dry-run")
    if args.resort:
        if not args.yes and input("Undo the previous sort and sort again? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
        undo_previous_sort(folder, say)

    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in mu.IMAGE_EXTS)
    faces = [p.name for p in files if mu.is_face_crop(p.name)]
    photos = [p for p in files if not mu.is_face_crop(p.name)]
    say(f"{len(files)} images: {len(faces)} face crops, {len(photos)} photos")
    say("reading metadata...")
    meta = mu.exiftool_json(photos)
    say("comparing images...")
    names, hashes = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for p in photos:
            try:
                hashes.append(dhash(mu.open_image(p, tmp)))
                names.append(p.name)
            except Exception as e:          # unreadable: leave it where it is
                say(f"  skipping unreadable {p.name}: {e}")
    groups = group_copies(names, hashes) if names else []

    actions = [("delete", f, "") for f in faces]
    taken, undated = set(), 0
    for group in groups:
        best = group[0] if len(group) == 1 else pick_best(group, meta, folder)
        if any(mu.is_scanned_print(n) for n in group):
            # a scan's date (in its name and EXIF) is the scanning day, not the photo's
            ext = Path(best).suffix.lower().replace(".jpeg", ".jpg")
            target = f"Scanned prints/{Path(best).stem}{ext}"
        else:
            dt, precision = photo_date(group, best, meta, folder)
            undated += dt is None
            target = new_name(best, dt, precision, label_for(group, best))
        stem, ext = os.path.splitext(target)
        k = 1
        while target.lower() in taken:      # e.g. burst shots in the same second
            k += 1
            target = f"{stem} ({k}){ext}"
        taken.add(target.lower())
        if Path(best).suffix.lower() in mu.RAW_EXTS:
            actions += [("convert", best, target), ("duplicate", best, f"_duplicates/{best}")]
        else:
            actions.append(("keep", best, target))
        actions += [("duplicate", f, f"_duplicates/{f}") for f in group if f != best]

    kept = sum(a in ("keep", "convert") for a, _, _ in actions)
    dupes = sum(a == "duplicate" for a, _, _ in actions)
    say(f"{kept} distinct photos ({undated} undated), {dupes} extra copies, {len(faces)} face crops to delete")
    if args.dry_run:
        for action, src, dst in actions:
            if action in ("keep", "convert"):
                say(f"  {src} -> {dst}")
        return 0
    if not args.yes and input("\nApply this? [y/N] ").strip().lower() not in ("y", "yes"):
        say("Cancelled.")
        return 1

    for action, src, dst in actions:
        if action == "delete":
            (folder / src).unlink()
        elif action == "convert":
            (folder / dst).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "95",
                            str(folder / src), "--out", str(folder / dst)], capture_output=True, check=True)
        else:
            (folder / dst).parent.mkdir(parents=True, exist_ok=True)
            (folder / src).rename(folder / dst)
    with open(folder / "_sort_log.csv", "a", newline="") as f:
        csv.writer(f).writerows(actions)
    say(f"done - log written to {folder / '_sort_log.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
