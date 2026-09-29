#!/usr/bin/env python3
"""
media_utils.py - small shared helpers for the photo tools: recognising file
kinds by name, reading dates, opening images for comparison and batch
exiftool calls.

Nothing in here changes any file.
"""
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None  # some panoramas/scans are huge; we only read them

RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2"}
SIPS_EXTS = RAW_EXTS | {".heic"}          # Pillow can't open these; macOS sips can
IMAGE_EXTS = SIPS_EXTS | {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".bmp", ".webp"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".3gp", ".mts"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
MONTH_NUM = {m.lower(): i for i, m in enumerate(MONTHS, 1)}


# ------------------------------------------------------------------ names
def is_face_crop(name):
    """facetile_12a.jpeg (Apple Photos) and IMG_1_face0.jpg (face crops)."""
    return name.startswith("facetile_") or re.search(r"_face\d+[._]", name) is not None


def is_apple_preview(name):
    """Apple Photos' own thumbnails/previews: UNADJUSTEDNONRAW_thumb_1ac4.jpg,
    masterthumbnail_4c89.jpeg, <hash>_thumb_33cf.jpg ..."""
    return re.search(r"UNADJUSTEDNONRAW|masterthumbnail_|_(thumb|mini)_[0-9a-f]+\.", name, re.I) is not None


def is_1024_export(name):
    return re.search(r"_1024(_\d+)?\.", name) is not None


def is_scanned_print(name):
    """Prints scanned in the 25 Feb 2018 session (20180225104807337-1.jpg, p2018022511...).
    Their name AND their EXIF date are the SCAN date, not when the photo was taken."""
    return re.match(r"p?2018022\d{9,}", name) is not None


def camera_number(name):
    """IMG_0589 / DSC01369 / DSCN1234 / P6260227 -> normalised id, else None."""
    m = re.search(r"(IMG|DSCN|DSCF|DSC|P\d{3})_?(\d{3,})", name, re.I)
    return (m.group(1).upper() + m.group(2)) if m else None


def apple_thumb_id(name):
    """UNADJUSTEDNONRAW_thumb_1ac5 / _mini_1ac5 -> '1ac5': the large and small
    Apple Photos thumbnail of the same picture share this id."""
    m = re.search(r"_(?:thumb|mini)_([0-9a-f]+)\.", name, re.I)
    return m.group(1).lower() if m else None


def is_appledouble(path):
    """macOS '._name' resource-fork files: they carry a photo extension but aren't photos."""
    if not os.path.basename(path).startswith("._"):
        return False
    try:
        with open(path, "rb") as f:
            return b"Mac OS X" in f.read(32)
    except OSError:
        return False


# ------------------------------------------------------------------ what a file really is
TRUE_EXT = {"jpeg": ".jpg", "png": ".png", "tiff": ".tif", "heic": ".heic", "gif": ".gif",
            "webp": ".webp", "mov": ".mov", "mp4": ".mp4", "mpeg": ".mpg", "avi": ".avi"}
EXT_KIND = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".tif": "tiff", ".tiff": "tiff",
            ".heic": "heic", ".gif": "gif", ".webp": "webp", ".mov": "mov", ".mp4": "mp4", ".m4v": "mp4",
            ".mpg": "mpeg", ".mpeg": "mpeg", ".avi": "avi", ".3gp": "mp4"}


def sniff(path):
    """What the file's contents are, from its first bytes (ignores the name):
    'jpeg', 'png', 'tiff', 'heic', 'mov', 'mp4', 'mpeg', 'avi', 'webp', 'gif',
    'appledouble', 'empty', 'zeros' (no data) or 'unknown'."""
    with open(path, "rb") as f:
        h = f.read(32)
    if not h:
        return "empty"
    if h[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if h[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if h[4:8] == b"ftyp":
        brand = h[8:12]
        return "heic" if brand in (b"heic", b"heix", b"mif1", b"msf1") else "mov" if brand == b"qt  " else "mp4"
    if h[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"):
        return "mov"
    if h[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):
        return "mpeg"
    if h[:4] == b"RIFF":
        return "webp" if h[8:12] == b"WEBP" else "avi"
    if h[:3] == b"GIF":
        return "gif"
    if h[:4] == b"\x00\x05\x16\x07":
        return "appledouble"
    if h.count(0) == len(h):
        return "zeros"
    return "unknown"


def mostly_empty(path, samples=16, chunk=4096, threshold=0.9):
    """True when the file is (almost) all zero bytes throughout - its picture data was lost
    (e.g. a copy that failed part-way) even though the header looks fine. Sampled across
    the whole file: some apps pad good JPEGs to a fixed size with zeros at the END only,
    which must not count."""
    size = os.path.getsize(path)
    if size < samples * chunk * 2:
        return False
    with open(path, "rb") as f:
        f.seek(size - (1 << 16))                 # cheap first test: one read at the end
        tail = f.read()
        if tail.count(0) / len(tail) < 0.5:
            return False                          # real data at the end: not empty (normal photos
                                                  # are a few % zeros; damaged ones end ~88%)
    zeros = total = 0                             # end is zeros: padding, or lost data? sample it
    with open(path, "rb") as f:
        for k in range(1, samples + 1):
            f.seek(size * k // (samples + 2))
            data = f.read(chunk)
            zeros += data.count(0)
            total += len(data)
    return zeros / total >= threshold


def decodes(path):
    """True if the whole picture can be decoded (slow - use only to confirm a suspect)."""
    try:
        im = Image.open(path)
        im.load()
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ dates
def parse_exif_date(value):
    if not value or str(value).startswith("0000"):
        return None
    try:
        return datetime.strptime(str(value)[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def date_from_name(name):
    """Returns (datetime, precision) for dates embedded in a filename, else None.
    precision is 'second', 'day' or 'month'."""
    patterns = [
        (r"((?:19|20)\d\d)-(\d\d)-(\d\d) (\d\d)\.(\d\d)\.(\d\d)", "ymdHMS", "second"),  # Dropbox
        (r"((?:19|20)\d\d)_(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)", "ymdHMS", "second"),
        (r"((?:19|20)\d\d)(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)", "ymdHMS", "second"),
        (r"(\d\d)-(\d\d)-((?:19|20)\d\d)", "dmy", "day"),
    ]
    for pat, order, precision in patterns:
        m = re.search(pat, name)
        if not m:
            continue
        p = dict(zip(order, map(int, m.groups())))
        try:
            return datetime(p["y"], p["m"], p["d"], p.get("H", 0), p.get("M", 0), p.get("S", 0)), precision
        except ValueError:
            continue
    # album-path prefix added by person_export.py, e.g. 2013_August_IMG_0589.JPG
    m = re.match(r"((?:19|20)\d\d)_([A-Za-z]+)_", name)
    if m and m.group(2).lower() in MONTH_NUM:
        return datetime(int(m.group(1)), MONTH_NUM[m.group(2).lower()], 1), "month"
    return None


def exif_date(meta):
    """Best 'taken' date from an exiftool record (camera date first)."""
    for tag in ("DateTimeOriginal", "CreateDate", "MediaCreateDate"):
        d = parse_exif_date(meta.get(tag))
        if d:
            return d
    return None


# ------------------------------------------------------------------ exiftool
EXIF_TAGS = ["-FileName", "-ImageWidth", "-ImageHeight", "-DateTimeOriginal", "-CreateDate",
             "-MediaCreateDate", "-Model", "-Software"]


def exiftool_json(paths, tags=EXIF_TAGS, batch=300):
    """{path: exiftool record} for many files, in batches."""
    out = {}
    paths = [str(p) for p in paths]
    for i in range(0, len(paths), batch):
        r = subprocess.run(["exiftool", "-q", "-q", "-j", "-n", *tags, *paths[i:i + batch]],
                           capture_output=True, text=True)
        for m in json.loads(r.stdout or "[]"):
            out[m["SourceFile"]] = m
    return out


# ------------------------------------------------------------------ images
def open_image(path, tmpdir, size=256):
    """Open any photo for comparison, upright, at reduced size."""
    path = Path(path)
    if path.suffix.lower() in SIPS_EXTS:
        out = os.path.join(tmpdir, "preview.jpg")
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(size * 2), str(path), "--out", out],
                       capture_output=True, check=True)
        im = Image.open(out)
        im.load()
        return ImageOps.exif_transpose(im)   # sips keeps the Orientation tag but doesn't rotate
    im = Image.open(path)
    im.draft("L", (size, size))                # fast JPEG decode at reduced size
    return ImageOps.exif_transpose(im)
