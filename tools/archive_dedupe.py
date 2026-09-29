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
                Same-name pairs in a folder (photo_organizer.py's
                "IMG_1.JPG" + "IMG_1_4d3d030b.JPG" collisions) are candidates too.
  2. Verify   - the candidates' real pixels are compared (similarity.py);
                only near pixel-identical pairs count. Burst shots don't.
                A same-name pair that matches only when turned 90 degrees is an
                old iPhoto rotate fix: listed with a review sheet, and moved
                (the sideways copy) only with --include-rotated.
  3. Keep     - copies are grouped and ONE file per group is kept: the
                largest; among (nearly) same-size copies the one in the best
                place, with a camera date, not re-saved by QuickTime/iPhoto/
                Photos, with the cleanest name, then the largest file.
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
import pickle
import re
import sys
from pathlib import Path
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


SUFFIX = re.compile(r"^(.+)_[0-9a-f]{8}(\.[^.]+)$")          # photo_organizer.py's collision suffix
RESAVED = re.compile(r"QuickTime|Photos|iPhoto|Aperture|Picasa|Photoshop|digiKam|Preview", re.I)
DROPBOX_NAME = re.compile(r"^(19|20)\d\d-\d\d-\d\d \d\d\.\d\d\.\d\d")  # Dropbox camera-upload conversions
SAME_SHOT_MAX_DIFF = 0.10     # same shutter press (date+sub-seconds+model): allow HEIC->JPG colour shifts
CACHE = Path.home() / "Library/Caches/photo-tools/fingerprints.pkl"


def _load_cache():
    try:
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    except (OSError, EOFError, pickle.UnpicklingError):
        return {}


def _save_cache(cache):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(CACHE)


def groups_of(pairs):
    """Connected groups of files linked by confirmed pairs."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in pairs:
        parent[find(a)] = find(b)
    out = collections.defaultdict(list)
    for x in parent:
        out[find(x)].append(x)
    return list(out.values())


def contact_sheet(pairs, lib, path, title, labels=("MOVE ", "KEEP ")):
    """Side-by-side preview: left = would be moved, right = kept."""
    from PIL import Image, ImageDraw, ImageOps
    pairs = pairs[:60]
    sheet = Image.new("RGB", (4 * 340, max(1, (len(pairs) + 3) // 4) * 215 + 20), "white")
    d = ImageDraw.Draw(sheet)
    d.text((5, 3), title, fill="black")
    for k, (a, b) in enumerate(pairs):
        x, y = (k % 4) * 340, (k // 4) * 215 + 20
        for j, i in enumerate((a, b)):
            try:
                im = Image.open(lib.path(i))
                im.draft("RGB", (300, 300))
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((160, 180))
                sheet.paste(im, (x + 5 + j * 165, y))
            except Exception:
                pass
            d.text((x + 5 + j * 165, y + 183), labels[j] + lib.rel(i)[1][:20],
                   fill="red" if j == 0 else "blue")
    sheet.save(path, quality=85)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--no-faces", action="store_true", help="don't move face crops")
    ap.add_argument("--include-rotated", action="store_true",
                    help="also move the sideways copy of rotated pairs (check the review sheet first)")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--workers", type=int, default=8, help="CPU workers for the fingerprint search")
    args = ap.parse_args()
    say, log_path = dk.open_log("archive_dedupe")
    try:
        root = dk.archive_dir()
        lib = dk.Library(dk.connect_readonly())
        ids = [i for i in lib.images if os.path.splitext(lib.rel(i)[1])[1].lower() not in SKIP_EXTS
               and not mu.is_face_crop(lib.rel(i)[1])]
        say(f"archive_dedupe  {root}  ({len(lib.images):,} files known to digiKam)")

        def size(i):
            w, h = lib.dims.get(i, (0, 0))
            return w * h

        def similar_size(a, b):
            return max(size(a), size(b)) <= min(size(a), size(b)) * 1.2

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

        # 1. candidate pairs: fingerprint matches (database only) + same-name collision pairs
        t = time.time()
        say(f"\n1. Searching digiKam fingerprints of {len(ids):,} photos...")
        cand = set()
        with Pool(args.workers, _init_worker, (dk.db_dir(), ids)) as pool:
            for a, near in pool.imap_unordered(_search, ids, chunksize=200):
                for gap, b in near:
                    if b not in lib.images:
                        continue
                    if size(b) > size(a) * 1.2 or (similar_size(a, b) and gap <= sim.HAAR_NEAR_IDENTICAL):
                        cand.add((min(a, b), max(a, b)))
        suffix_pairs = set()
        for i in ids:
            rel, name = lib.rel(i)
            m = SUFFIX.match(name)
            if m:
                album = lib.images[i][0]
                for base in {m.group(1) + m.group(2), m.group(1) + m.group(2).lower(), m.group(1) + m.group(2).upper()}:
                    j = lib.by_location.get((*lib.album_path[album], base))
                    if j and j != i:
                        suffix_pairs.add((min(i, j), max(i, j)))
        cand |= suffix_pairs
        say(f"   {len(cand):,} candidate pairs, {len(suffix_pairs):,} of them same-name pairs ({time.time() - t:.0f}s)")

        # 2. pixel check (reads only the candidate files; results cached between runs)
        need = sorted({x for p in cand for x in p})
        cache = _load_cache()

        def ckey(i):
            p = lib.path(i)
            st = p.stat()
            return f"{p}|{st.st_size}|{int(st.st_mtime)}"
        todo = [i for i in need if ckey(i) not in cache]
        say(f"\n2. Comparing real pixels of {len(need):,} files ({len(todo):,} not yet cached - "
            "reading those from the drive can take an hour)...")
        t = time.time()
        with Pool(4) as pool:
            for n, (iid, fp) in enumerate(pool.imap_unordered(_fingerprint, [(i, lib.path(i)) for i in todo], 50)):
                cache[ckey(iid)] = None if fp is None else (fp[0].astype("float16"), fp[1])
                if n and n % 10000 == 0:
                    say(f"   {n:,} of {len(todo):,}")
                    _save_cache(cache)
        _save_cache(cache)
        fps = {i: None if cache[ckey(i)] is None else (cache[ckey(i)][0].astype("float32"), cache[ckey(i)][1])
               for i in need}
        meta = mu.exiftool_json([lib.path(i) for i in need],
                                tags=["-Software", "-DateTimeOriginal", "-SubSecTimeOriginal", "-CreateDate", "-Model"])

        def shot(i):
            """(camera date, sub-seconds, camera model) - identifies one press of the shutter.
            Some early-2000s cameras record no model; it then has to be blank on both files."""
            m = meta.get(str(lib.path(i)), {})
            d = mu.parse_exif_date(m.get("DateTimeOriginal"))
            return (d, str(m.get("SubSecTimeOriginal") or ""), str(m.get("Model") or "")) if d else None

        def apple_edit(i):
            return lib.rel(i)[1].startswith("fullsizeoutput_")

        # Same-size pairs need proof beyond the picture: pixels alone can't tell a re-save from
        # a burst frame or a dark frame (tested on 2026-09-28: the two overlap completely).
        same, unproven = [], []
        for p in cand:
            fa, fb = fps.get(p[0]), fps.get(p[1])
            if fa is None or fb is None:
                continue
            if not similar_size(*p):
                if sim.same_photo(fa, fb):
                    same.append(p)                                   # thumbnail / resized copy
            elif p in suffix_pairs and sim.same_photo(fa, fb):
                same.append(p)                                       # re-save of the same file
            elif shot(p[0]) and shot(p[0]) == shot(p[1]) and sim.pixel_difference(fa, fb) < SAME_SHOT_MAX_DIFF:
                same.append(p)                                       # renumbered copy / HEIC+JPG
            elif apple_edit(p[0]) != apple_edit(p[1]) and sim.same_photo(fa, fb):
                same.append(p)                                       # Apple Photos edit + its original
                                                                     # (Photos strips the edit's date)
            elif sim.same_photo(fa, fb):
                unproven.append(p)                                   # looks the same - review only
        rotated = [p for p in suffix_pairs if p not in same and sim.same_photo_rotated(fps.get(p[0]), fps.get(p[1]))]
        say(f"   confirmed copies: {len(same):,} pairs; rotated copies: {len(rotated):,} pairs; "
            f"look-alikes without proof (review only): {len(unproven):,} pairs ({time.time() - t:.0f}s)")

        # 3. one keeper per group of copies
        def quality(i):
            """Higher is better, for copies of (nearly) the same size: best place, has a camera date,
            not re-saved by an app, the camera's original rather than Dropbox's date-named
            conversion (keeps the HEIC over its JPG copy), clean name, then the bigger file."""
            m = meta.get(str(lib.path(i)), {})
            name = lib.rel(i)[1]
            clean = not (SUFFIX.match(name) or mu.is_apple_preview(name) or name.startswith("fullsizeoutput_"))
            return (-place(i), mu.exif_date(m) is not None, not RESAVED.search(str(m.get("Software") or "")),
                    not DROPBOX_NAME.match(name), clean, lib.path(i).stat().st_size, -i)

        def best_of(group):
            top = max(size(i) for i in group)
            return max((i for i in group if size(i) * 1.2 >= top), key=quality)

        keep = {}
        for g in groups_of(same):
            k = best_of(g)
            keep.update({i: k for i in g if i != k})
        rot_keep = {}
        for a, b in rotated:
            if a in keep or b in keep:
                continue
            # the upright version is the one iPhoto/QuickTime re-saved after a rotate
            ra, rb = (bool(RESAVED.search(str(meta.get(str(lib.path(x)), {}).get("Software") or ""))) for x in (a, b))
            if ra != rb:
                upright, sideways = (a, b) if ra else (b, a)
                rot_keep[sideways] = upright
        if args.include_rotated:
            keep.update(rot_keep)
        # A group's keeper can itself be the sideways half of a rotated pair; point every copy
        # straight at the file that really stays, so tags are copied to it before anything moves.
        for a in list(keep):
            k, seen = keep[a], {a}
            while k in keep and k not in seen:
                seen.add(k)
                k = keep[k]
            keep[a] = k
        assert not set(keep.values()) & set(keep), "a kept file is also being moved"
        faces = [] if args.no_faces else [i for i in lib.images if mu.is_face_crop(lib.rel(i)[1])]
        orphans = [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs
                   if f.startswith("._") and mu.is_appledouble(os.path.join(d, f))]
        tag_moves = [(a, k) for a, k in keep.items() if lib.person_tags(a) - lib.person_tags(k)]

        say("\n3. Plan")
        by_kind = collections.Counter(
            "rotated (sideways) copy" if a in rot_keep else
            "Apple thumbnail" if mu.is_apple_preview(lib.rel(a)[1]) else
            "1024px export" if mu.is_1024_export(lib.rel(a)[1]) else
            "same-size copy (re-save / renumbered)" if similar_size(a, keep[a]) else "smaller copy" for a in keep)
        for k, v in by_kind.most_common():
            say(f"   {v:7,}  {k}")
        say(f"   {len(faces):7,}  face crops")
        say(f"   {len(orphans):7,}  '._' macOS metadata files")
        say(f"   people tags to carry over first: {len(tag_moves):,} copies")
        if rot_keep and not args.include_rotated:
            say(f"   {len(rot_keep):7,}  rotated pairs NOT included - check the review sheet, then re-run "
                "with --include-rotated")
        plan_csv = log_path.with_name(log_path.stem + "_plan.csv")
        with open(plan_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(("remove", "kept original", "why"))
            w.writerows(("/".join(lib.rel(a)), "/".join(lib.rel(k)), "rotated" if a in rot_keep else "copy")
                        for a, k in keep.items())
            w.writerows(("/".join(lib.rel(a)), "/".join(lib.rel(k)), "rotated - not included")
                        for a, k in rot_keep.items() if a not in keep)
            w.writerows(("/".join(lib.rel(a)), "/".join(lib.rel(b)), "look-alike without proof - both kept")
                        for a, b in unproven)
        if unproven:
            say(f"   {len(unproven):7,}  look-alike pairs kept (no metadata proof - could be bursts); see list")
        say(f"   full list: {plan_csv}")
        same_size = [(a, k) for a, k in keep.items() if a not in rot_keep and similar_size(a, k)]
        for label, pairs, labels in (("same_size", same_size, ("MOVE ", "KEEP ")),
                                     ("rotated", list(rot_keep.items()), ("MOVE ", "KEEP ")),
                                     ("lookalike", unproven, ("", ""))):
            if pairs:
                sheet = log_path.with_name(f"{log_path.stem}_{label}_review.jpg")
                how = "not moved - both kept" if label == "lookalike" else "left is moved, right is kept"
                contact_sheet(sorted(pairs, key=lambda p: lib.rel(p[0])), lib, sheet,
                              f"{label.replace('_', '-')} pairs (first 60 of {len(pairs):,}): {how}", labels)
                say(f"   review sheet: {sheet}")
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
