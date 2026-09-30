#!/usr/bin/env python3
"""
import_check.py - before adding a folder of photos to the archive, find out
which ones the archive already has.

THE PROBLEM THIS SOLVES
------------------------
Photos from someone else's laptop, an old phone or a backup drive overlap
with the archive in ways a file comparison can't see: the same shot as an
iPhone HEIC on one side and a Dropbox JPEG on the other, a photo that was
emailed or sent by WhatsApp (resized, date stripped), an edited version.
This compares a folder against the whole archive in three passes, from
strictest to broadest, and reports each file as:

  identical    byte-for-byte the same file is already in the archive
  same shot    the same press of the shutter (camera date, sub-seconds and
               model match) - a different file of the same photo
  same picture no usable date, but the picture matches an archive photo
               (resized, re-saved, rotated, date stripped)
  new          not in the archive

Nothing is changed, unless you add --import-new. The full list goes to
~/Library/Logs/photo-tools/.

--import-new "Old Laptop" COPIES the new files (the source folder is never
changed) into Photo Archives/Old Laptop/, named the way photo_organizer.py
names the rest of the archive:
    <Year>/<Month>/<Town>/                 UK, from the photo's GPS
    <Year>/<Month>/<Country>/<Town>/       elsewhere
    <Year>/<Month>/                        no GPS
    NO EXIF/                               no date anywhere
The date is the camera date, else the Year/Month folder the file came from.
Then open digiKam so it scans them in.
Picture fingerprints of the archive are cached (shared with
archive_dedupe.py), so the first run reads the whole drive; later runs are
fast.

    python tools/import_check.py "/Volumes/Backup/Old Laptop/Photos"
"""
import argparse
import collections
import csv
import hashlib
import os
import sys
import tempfile
from multiprocessing import Pool

import numpy as np
from PIL import Image

import digikam_db as dk
import media_utils as mu
import similarity as sim
from archive_dedupe import _fingerprint, _load_cache, _save_cache

SAME_SHOT_MAX_DIFF = 0.2     # same shutter press: allow HEIC/JPEG and edit colour differences
DHASH_MAX_BITS = 30          # picture pre-filter before the pixel check (of 256 bits)


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    return h.hexdigest()


def dhash_bits(fp):
    """256-bit difference hash from a pixel fingerprint (same on both sides, so comparable)."""
    a = np.asarray(Image.fromarray(fp[0].astype("float32")).resize((17, 16), Image.BILINEAR))
    return np.packbits((a[:, 1:] > a[:, :-1]).ravel())


def folder_date(src, path):
    """(year, month number) from a source layout like .../2016/June/..., else None."""
    parts = os.path.relpath(path, src).split(os.sep)
    if len(parts) >= 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].lower() in mu.MONTH_NUM:
        return int(parts[0]), mu.MONTH_NUM[parts[1].lower()]
    return None


def import_new(src, root, subfolder, new, say, log_path, yes):
    """Copy new files into root/subfolder, named like photo_organizer.py names the archive."""
    import shutil
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import photo_organizer as po                                     # same location rules and naming
    meta = mu.exiftool_json(new, tags=["-DateTimeOriginal", "-CreateDate", "-MediaCreateDate",
                                       "-GPSLatitude", "-GPSLongitude"])
    gps = {p: (m["GPSLatitude"], m["GPSLongitude"]) for p, m in meta.items()
           if isinstance(m.get("GPSLatitude"), (int, float)) and isinstance(m.get("GPSLongitude"), (int, float))}
    geo = po.OfflineGeocoder()
    geo.resolve_all(list(gps.values()))
    base, plan, taken = root / subfolder, [], set()
    for p in sorted(new):
        d = mu.exif_date(meta.get(p, {}))
        ym = (d.year, d.month) if d else folder_date(src, p)
        if ym:
            dest = base / str(ym[0]) / mu.MONTHS[ym[1] - 1]
            loc = geo.get(*gps[p]) if p in gps else None
            if loc and loc.get("town"):
                dest = dest / po.MediaOrganizer._sanitize(loc["town"]) if loc["is_uk"] else \
                    dest / po.MediaOrganizer._sanitize(loc["country"]) / po.MediaOrganizer._sanitize(loc["town"])
        else:
            dest = base / po.NO_EXIF_DIR
        target = dest / os.path.basename(p)
        if target.exists() or str(target).lower() in taken:          # same rule as photo_organizer.py
            stem, ext = os.path.splitext(target.name)
            target = dest / f"{stem}_{sha1(p)[:8]}{ext}"
        taken.add(str(target).lower())
        plan.append((p, target))
    where = collections.Counter(str(t.parent.relative_to(base)).split(os.sep)[0] for _, t in plan)
    say(f"\nImport plan: copy {len(plan):,} new files into {base}")
    say("   " + ", ".join(f"{k}: {v}" for k, v in sorted(where.items())))
    say(f"   with a location folder: {sum(1 for _, t in plan if len(t.relative_to(base).parts) > 3):,}")
    if not dk.confirm("Copy them?", yes):
        say("Cancelled.")
        return 1
    with open(log_path.with_name(log_path.stem + "_imported.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(("copied from", "to"))
        for p, t in plan:
            t.parent.mkdir(parents=True, exist_ok=True)
            if t.exists():
                raise dk.Abort(f"Refusing to overwrite '{t}'.")
            shutil.copy2(p, t)
            w.writerow((p, t))
    say(f"Copied {len(plan):,} files. Next: open digiKam and let it scan them in.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="folder of photos to check against the archive")
    ap.add_argument("--import-new", metavar="SUBFOLDER",
                    help='copy the new files into "Photo Archives/SUBFOLDER/<Year>/<Month>/<Location>"')
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()
    say, log_path = dk.open_log("import_check")
    try:
        src = os.path.abspath(os.path.expanduser(args.folder))
        root = dk.archive_dir()
        files = sorted(os.path.join(d, f) for d, dirs, fs in os.walk(src)
                       if not any(part.startswith(".") for part in os.path.relpath(d, src).split(os.sep) if part != ".")
                       for f in fs if not f.startswith(".") and os.path.splitext(f)[1].lower() in mu.IMAGE_EXTS | mu.VIDEO_EXTS)
        say(f"import_check  {src}  ({len(files):,} photos/videos)  vs  {root}")
        result = {}

        # 1. byte-identical
        by_size = collections.defaultdict(list)
        for d, dirs, fs in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in fs:
                if not f.startswith("."):
                    p = os.path.join(d, f)
                    by_size[os.path.getsize(p)].append(p)
        for p in files:
            for q in by_size.get(os.path.getsize(p), []):
                if sha1(q) == sha1(p):
                    result[p] = ("identical", q)
                    break
        say(f"  1. identical:   {sum(r[0] == 'identical' for r in result.values()):,}")

        # 2. same shutter press (camera date + sub-seconds + model), confirmed by the picture
        lib = dk.Library(dk.connect_readonly())
        meta = mu.exiftool_json([p for p in files if p not in result],
                                tags=["-DateTimeOriginal", "-SubSecTimeOriginal", "-Model"])
        amodel = dict(lib.conn.execute("SELECT imageid, model FROM ImageMetadata"))
        by_date = collections.defaultdict(list)
        for iid, cd in lib.conn.execute("SELECT imageid, creationDate FROM ImageInformation"):
            if iid in lib.images and cd:
                by_date[cd[:19]].append(iid)
        with tempfile.TemporaryDirectory() as tmp:
            src_fp = {}
            for p in files:
                if p in result:
                    continue
                m = meta.get(p, {})
                d = mu.parse_exif_date(m.get("DateTimeOriginal"))
                if not d:
                    continue
                cands = [i for i in by_date.get(d.strftime("%Y-%m-%dT%H:%M:%S"), [])
                         if not amodel.get(i) or amodel[i] == (m.get("Model") or amodel[i])]
                if not cands or os.path.splitext(p)[1].lower() in mu.VIDEO_EXTS:
                    continue
                fa = src_fp.setdefault(p, sim.pixel_fingerprint(p, tmp))
                for c in cands:
                    fb = sim.pixel_fingerprint(lib.path(c), tmp)
                    if fa and fb and min(sim.pixel_difference(fa, fb), sim.rotated_difference(fa, fb)) < SAME_SHOT_MAX_DIFF:
                        result[p] = ("same shot", str(lib.path(c)))
                        break
        say(f"  2. same shot:   {sum(r[0] == 'same shot' for r in result.values()):,}")

        # 3. same picture anywhere in the archive (cached fingerprints + 256-bit pre-filter)
        rest = [p for p in files if p not in result and os.path.splitext(p)[1].lower() in mu.IMAGE_EXTS]
        arch = [i for i in lib.images if os.path.splitext(lib.rel(i)[1])[1].lower() in mu.IMAGE_EXTS - mu.RAW_EXTS]
        cache = _load_cache()

        def ckey(path):
            st = os.stat(path)
            return f"{path}|{st.st_size}|{int(st.st_mtime)}"
        todo = [(i, lib.path(i)) for i in arch if ckey(lib.path(i)) not in cache]
        say(f"  3. comparing pictures: {len(rest):,} files vs {len(arch):,} archive photos "
            f"({len(todo):,} archive fingerprints to compute - the first run reads the drive)...")
        with Pool(4) as pool:
            for n, (iid, fp) in enumerate(pool.imap_unordered(_fingerprint, todo, 50)):
                cache[ckey(lib.path(iid))] = None if fp is None else (fp[0].astype("float16"), fp[1])
                if n and n % 5000 == 0:
                    say(f"     {n:,} of {len(todo):,}")
                    _save_cache(cache)
        _save_cache(cache)
        afp, apath = [], []
        for i in arch:
            c = cache.get(ckey(lib.path(i)))
            if c is not None:
                afp.append((c[0].astype("float32"), c[1]))
                apath.append(str(lib.path(i)))
        abits = np.unpackbits(np.array([dhash_bits(f) for f in afp]), axis=1)
        with Pool(4) as pool:
            rfp = dict(zip(rest, pool.map(_fingerprint, [(p, p) for p in rest], 20)))
        for p in rest:
            fp = rfp[p][1] if rfp[p] else None
            if fp is None:
                continue
            bits = np.unpackbits(dhash_bits(fp))
            near = np.argsort((abits != bits).sum(1))[:8]
            for j in near:
                if (abits[j] != bits).sum() <= DHASH_MAX_BITS and (sim.same_photo(fp, afp[j]) or sim.same_photo_rotated(fp, afp[j])):
                    result[p] = ("same picture", apath[j])
                    break
        say(f"     same picture: {sum(r[0] == 'same picture' for r in result.values()):,}")

        for p in files:
            result.setdefault(p, ("new", ""))
        counts = collections.Counter(r[0] for r in result.values())
        say("\nSummary:")
        for k in ("identical", "same shot", "same picture", "new"):
            say(f"   {counts[k]:7,}  {k}")
        csv_path = log_path.with_suffix(".csv")
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(("result", "file", "already in archive as"))
            w.writerows((r[0], p, r[1]) for p, r in sorted(result.items()))
        say(f"Full list: {csv_path}")
        if args.import_new:
            return import_new(src, root, args.import_new, [p for p, r in result.items() if r[0] == "new"],
                              say, log_path, args.yes)
        return 0
    except dk.Abort as e:
        say(f"\nSTOPPED: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
