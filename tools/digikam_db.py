#!/usr/bin/env python3
"""
digikam_db.py - shared helpers for tools that read or change digiKam's database.

THE PROBLEM THIS SOLVES
------------------------
digiKam keeps people tags, face boxes and dates in its own SQLite database
(digikam4.db), keyed by each file's folder and name. Moving or deleting a
photo behind digiKam's back makes it look like the photo vanished and a new
untagged one appeared - the tags are lost. These helpers do the risky parts
once, carefully:

  * find the database and the archive (archive drive pinned by volume UUID,
    same as photo_mirror.py),
  * refuse to write while digiKam is running (it would overwrite our changes),
  * back up all three databases before any write,
  * move a file AND update digiKam's record of it in one step, so its tags
    follow it,
  * copy people tags (with their face boxes scaled to the other file's size)
    from a copy onto the original that's being kept.
"""
import datetime as dt
import os
import plistlib
import re
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- config
DIGIKAM_RC = Path.home() / "Library/Preferences/digikamrc"
DEFAULT_DB_DIR = Path.home() / "Pictures/digiKam DB"
ARCHIVE_VOLUME = "/Volumes/Backup 4TB"
ARCHIVE_UUID = "7DB63B40-461E-3830-8CB0-A83727463F9A"     # same drive as photo_mirror.py
ARCHIVE_FOLDER = "Photo Archives"
HOLDING_PREFIX = "Photo Archives - removed"                # holding folders live beside the archive
BACKUP_PARENT = Path.home() / "Pictures"                   # "digiKam DB backup <date>" goes here
LOG_DIR = Path.home() / "Library/Logs/photo-tools"
PEOPLE_EXCLUDE = ("Unknown", "Unconfirmed", "Ignored")     # digiKam's placeholder people tags
# -----------------------------------------------------------------------------------


class Abort(Exception):
    pass


# ------------------------------------------------------------------ logging / prompts
def open_log(tool):
    """Returns (say, log_path). say() prints and writes to the tool's log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{tool}_{dt.datetime.now():%Y-%m-%d_%H%M%S}.log"
    logf = open(log_path, "w")

    def say(msg=""):
        print(msg, flush=True)
        logf.write(msg + "\n")
        logf.flush()
    return say, log_path


def confirm(question, yes=False):
    if yes:
        return True
    return input(f"\n{question} [y/N] ").strip().lower() in ("y", "yes")


# ------------------------------------------------------------------ locations
def db_dir():
    """digiKam's database folder, from its settings (falls back to ~/Pictures/digiKam DB)."""
    try:
        for line in DIGIKAM_RC.read_text(errors="ignore").splitlines():
            if line.startswith("Database Name="):
                return Path(line.split("=", 1)[1].strip())
    except OSError:
        pass
    return DEFAULT_DB_DIR


def volume_uuid(mountpoint):
    out = subprocess.run(["diskutil", "info", "-plist", mountpoint], capture_output=True)
    return plistlib.loads(out.stdout).get("VolumeUUID") if out.returncode == 0 else None


def archive_dir(check=True):
    """The Photo Archives folder, after checking it's really the expected drive."""
    if check:
        if not os.path.ismount(ARCHIVE_VOLUME):
            raise Abort(f"Archive drive is not mounted at '{ARCHIVE_VOLUME}'.")
        if volume_uuid(ARCHIVE_VOLUME) != ARCHIVE_UUID:
            raise Abort(f"'{ARCHIVE_VOLUME}' is not the expected archive drive (UUID mismatch) - refusing.")
    path = Path(ARCHIVE_VOLUME) / ARCHIVE_FOLDER
    try:
        next(os.scandir(path), None)
    except PermissionError:
        raise Abort(f"macOS is refusing access to '{path}' (Operation not permitted).\n"
                    "  Fix: System Settings > Privacy & Security > Files and Folders > Terminal >\n"
                    "  switch 'Removable Volumes' off and on again, then re-run.")
    return path


def holding_dir(tag):
    """Where removed files go: '<archive volume>/Photo Archives - removed <tag>'."""
    return Path(ARCHIVE_VOLUME) / f"{HOLDING_PREFIX} {tag}"


def resolve_album_roots(conn):
    """Map digiKam AlbumRoots ids to real folders on this Mac (by volume UUID)."""
    uuids = {}
    for m in [Path("/")] + list(Path("/Volumes").iterdir()):
        out = subprocess.run(["diskutil", "info", "-plist", str(m)], capture_output=True)
        if out.returncode == 0:
            info = plistlib.loads(out.stdout)
            for k in ("DiskUUID", "VolumeUUID"):
                if k in info:
                    uuids.setdefault(info[k].lower(), m)
    roots = {}
    for root_id, identifier, specific in conn.execute("SELECT id, identifier, specificPath FROM AlbumRoots"):
        q = parse_qs(urlparse(identifier or "").query)
        base = uuids.get(q["uuid"][0].lower()) if "uuid" in q else Path(q["path"][0]) if "path" in q else None
        if base and (base / (specific or "").lstrip("/")).is_dir():
            roots[root_id] = base / (specific or "").lstrip("/")
    return roots


# ------------------------------------------------------------------ database access
def digikam_running():
    return subprocess.run(["pgrep", "-x", "digikam"], capture_output=True).returncode == 0


def connect_readonly(directory=None):
    return sqlite3.connect(f"file:{(directory or db_dir()) / 'digikam4.db'}?mode=ro", uri=True)


def connect_for_writing(say=print, directory=None):
    """Refuses while digiKam runs; backs up all databases first. Returns (conn, backup_dir)."""
    directory = directory or db_dir()
    if digikam_running():
        raise Abort("digiKam is running - quit it first (it would overwrite these changes).")
    backup = BACKUP_PARENT / f"digiKam DB backup {dt.datetime.now():%Y-%m-%d %H%M%S}"
    backup.mkdir(parents=True)
    for name in ("digikam4", "recognition", "similarity"):
        src = directory / f"{name}.db"
        if src.exists():
            subprocess.run(["sqlite3", str(src), f".backup '{backup}/{name}.db'"], check=True)
    say(f"  digiKam databases backed up to: {backup}")
    conn = sqlite3.connect(directory / "digikam4.db")
    return conn, backup


class Library:
    """A read snapshot of digiKam's image list with fast lookups."""

    def __init__(self, conn):
        self.conn = conn
        self.roots = resolve_album_roots(conn)
        self.album_path = {}                       # album id -> (root id, relative path)
        self.album_id = {}                         # (root id, relative path) -> album id
        for aid, root, rel in conn.execute("SELECT id, albumRoot, relativePath FROM Albums"):
            self.album_path[aid] = (root, rel)
            self.album_id[(root, rel)] = aid
        self.images = {}                           # id -> (album id, name)
        self.by_location = {}                      # (root, rel, name) -> id
        for iid, album, name in conn.execute("SELECT id, album, name FROM Images WHERE status=1"):
            if album in self.album_path:
                self.images[iid] = (album, name)
                self.by_location[(*self.album_path[album], name)] = iid
        self.dims = {i: (w or 0, h or 0) for i, w, h in
                     conn.execute("SELECT imageid, width, height FROM ImageInformation")}
        self.people = {n: i for i, n in conn.execute("SELECT id, name FROM Tags WHERE pid IN "
                       "(SELECT id FROM Tags WHERE name='People' AND pid=0)") if n not in PEOPLE_EXCLUDE}

    def rel(self, iid):
        album, name = self.images[iid]
        return self.album_path[album][1], name

    def path(self, iid):
        album, name = self.images[iid]
        root, rel = self.album_path[album]
        return self.roots[root] / rel.lstrip("/") / name if root in self.roots else None

    def find(self, path):
        """Image id for a file path inside an album root, or None."""
        path = Path(path)
        for root_id, root in self.roots.items():
            try:
                rel = path.parent.relative_to(root)
            except ValueError:
                continue
            return self.by_location.get((root_id, "/" + str(rel) if str(rel) != "." else "/", path.name))
        return None

    def person_tags(self, iid):
        ids = set(self.people.values())
        return {t for (t,) in self.conn.execute("SELECT tagid FROM ImageTags WHERE imageid=?", (iid,)) if t in ids}


# ------------------------------------------------------------------ writes (need connect_for_writing)
def get_album(conn, lib, root_id, rel):
    """Album id for a folder, creating digiKam's album record (and parents) if needed."""
    key = (root_id, rel)
    if key not in lib.album_id:
        parent = os.path.dirname(rel)
        if rel != "/" and parent != rel:
            get_album(conn, lib, root_id, parent)
        now = dt.datetime.now()
        cur = conn.execute("INSERT INTO Albums(albumRoot, relativePath, date, modificationDate) VALUES(?,?,?,?)",
                           (root_id, rel, now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%dT%H:%M:%S")))
        lib.album_id[key] = cur.lastrowid
        lib.album_path[cur.lastrowid] = key
    return lib.album_id[key]


def free_name(conn, folder, album_id, name):
    """name, or 'name (2)', 'name (3)'... - free both on disk and in digiKam's records."""
    stem, ext = os.path.splitext(name)
    k = 1
    while (folder / name).exists() or (album_id and conn.execute(
            "SELECT 1 FROM Images WHERE album=? AND name=?", (album_id, name)).fetchone()):
        k += 1
        name = f"{stem} ({k}){ext}"
    return name


def move_within_archive(conn, lib, iid, root_id, new_rel):
    """Move a digiKam-known file to another folder in the same album root, keeping its
    digiKam record (tags, faces, dates) attached. Returns the new path."""
    src = lib.path(iid)
    aid = get_album(conn, lib, root_id, new_rel)
    folder = lib.roots[root_id] / new_rel.lstrip("/")
    folder.mkdir(parents=True, exist_ok=True)
    name = free_name(conn, folder, aid, src.name)
    os.rename(src, folder / name)
    conn.execute("UPDATE Images SET album=?, name=? WHERE id=?", (aid, name, iid))
    lib.images[iid] = (aid, name)
    return folder / name


def rename_in_place(conn, lib, iid, new_name, category=None):
    """Rename a digiKam-known file within its folder, keeping its record (and tags). category:
    digiKam's item type (1 image, 2 video) when the rename changes what the file is."""
    src = lib.path(iid)
    album = lib.images[iid][0]
    name = free_name(conn, src.parent, album, new_name)
    os.rename(src, src.parent / name)
    conn.execute("UPDATE Images SET name=? WHERE id=?", (name, iid))
    if category:
        conn.execute("UPDATE Images SET category=? WHERE id=?", (category, iid))
    lib.images[iid] = (album, name)
    return src.parent / name


def move_out(src, archive_root, holding_root):
    """Move a file to the holding folder, keeping its relative path. Returns the new path."""
    dst = holding_root / Path(src).relative_to(archive_root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise Abort(f"Refusing to overwrite '{dst}' in the holding folder.")
    os.rename(src, dst)
    return dst


def copy_person_tags(conn, lib, src_id, dst_id):
    """Add src's people tags to dst (with face boxes scaled to dst's size when both images
    have the same shape). Returns counts {'with box':n, 'tag only':n, 'already there':n}."""
    counts = {"with box": 0, "tag only": 0, "already there": 0}
    (ws, hs), (wd, hd) = lib.dims.get(src_id, (0, 0)), lib.dims.get(dst_id, (0, 0))
    same_shape = ws and hs and wd and hd and abs(wd / ws - hd / hs) / max(wd / ws, hd / hs) < 0.03
    have = lib.person_tags(dst_id)
    for t in lib.person_tags(src_id):
        if t in have:
            counts["already there"] += 1
            continue
        conn.execute("INSERT INTO ImageTags(imageid, tagid) VALUES(?,?)", (dst_id, t))
        regions = conn.execute("SELECT value FROM ImageTagProperties WHERE imageid=? AND tagid=? "
                               "AND property='tagRegion'", (src_id, t)).fetchall()
        if regions and same_shape:
            sx, sy = wd / ws, hd / hs
            for (v,) in regions:
                x, y, w, h = map(int, re.findall(r'(?:x|y|width|height)="(-?\d+)"', v))
                nv = f'<rect x="{round(x * sx)}" y="{round(y * sy)}" width="{round(w * sx)}" height="{round(h * sy)}"/>'
                for prop in ("tagRegion", "faceToTrain"):
                    conn.execute("INSERT INTO ImageTagProperties(imageid, tagid, property, value) VALUES(?,?,?,?)",
                                 (dst_id, t, prop, nv))
            counts["with box"] += 1
        else:
            counts["tag only"] += 1
        have.add(t)
    return counts
