# Photo Organizer

Deduplicates and organizes a large, messy photo/video archive - loose files,
zip archives, and tar archives all mixed together - into a clean
`Year/Month/[Country/]Town/` folder structure, using EXIF date and GPS data.

Built for the "20 years of backups scattered across drives and formats"
situation: multiple overlapping zip exports, camera-roll dumps, old iPhoto/
Photos libraries, tar.gz archives from ancient Linux boxes, and no idea
what's actually a duplicate of what.

## What it does

- **Deduplicates by content, not filename.** Every file is hashed
  (SHA256); exact byte-identical duplicates are deleted, regardless of what
  they're named or where they came from.
- **Organizes by EXIF date and GPS location** into
  `Year/Month/[Country/]Town/filename.jpg` (UK locations skip the country
  folder), falling back to `Year/Month/filename.jpg` if there's no GPS, or
  `NO EXIF/filename.jpg` if there's no usable date either.
- **Falls back to Google Photos Takeout sidecars** (`photo.jpg.json`) for
  date/GPS when a file has no usable EXIF of its own - Google Photos
  frequently strips EXIF from exported originals, keeping the real
  `photoTakenTime`/`geoData` only in these companion JSON files.
- **Extracts zip and tar-family archives** (`.zip`, `.tar`, `.tar.gz`,
  `.tgz`, `.tar.bz2`) and folds their contents into the same pipeline. An
  archive is deleted only once **every** media file it contained has been
  verified moved to its final destination or proven to be a duplicate
  already accounted for - if anything about an archive's extraction is
  uncertain, it's kept, never guessed about.
- **Never deletes a real, unique file.** The only things ever deleted are
  proven-duplicate files and archives whose entire contents are verified
  safe elsewhere.
- **Original filenames are always preserved**, with a short content-hash
  suffix appended only on a genuine name collision between two different
  files.
- **Always writes a full, untruncated audit report** before touching
  anything - even a real (non-dry-run) execution writes the report first.

## Requirements

```
pip install Pillow reverse_geocoder
brew install exiftool   # strongly recommended - handles HEIC/RAW/video
                         # EXIF far more reliably than Pillow alone
```

`exiftool` and the system `unzip` binary are used when available and
Photo Organizer falls back to pure-Python equivalents if they're missing,
but both are recommended - `unzip` in particular works around a real bug
in Python's `zipfile` module that causes some large (>4GB) real-world
zip archives to be misread as corrupt.

`reverse_geocoder` does entirely offline reverse geocoding (bundled
GeoNames city data) - no internet connection, no API keys, no rate limits,
no third-party usage policy to worry about when processing tens of
thousands of GPS-tagged photos.

## Usage

```
python photo_organizer.py "/path/to/Photo Archives" --dry-run
```

Always run `--dry-run` first. It does a full scan, hash, EXIF/GPS
extraction, and geocoding pass, and writes a complete report of every
planned move and deletion - without touching a single file. Read it before
doing a real run.

```
python photo_organizer.py "/path/to/Photo Archives"
```

Runs for real: moves files, deletes proven duplicates, and deletes fully-
verified archives. Requires typed `yes` confirmation unless you pass
`--yes` (needed for non-interactive/background runs).

**Important:** point this at the folder you want to *become* your organized
archive - it organizes directly inside the folder you give it (e.g. if you
point it at `.../Backup/Photo Archives`, you get
`.../Backup/Photo Archives/2020/June/...`), not into a new subfolder.

## Companion tools

Three related but genuinely separate problems live in `tools/` rather than
being crammed into the core script:

### `tools/stage_from_cloud.py`

If your source photos live in a cloud-sync folder in "stream"/"online-only"
mode (Google Drive, OneDrive, Dropbox Smart Sync, iCloud Drive), reading
them triggers the sync client to download each one into a local cache -
and that cache usually lives on your boot drive, which can silently fill
up mid-operation if you don't have much room to spare. This script safely
copies files out of a cloud-mounted folder into a real local folder,
checking your boot drive's free space before every file and halting
cleanly (not corrupting anything) if it gets too tight. Safe to re-run -
already-copied files are skipped.

```
python tools/stage_from_cloud.py "<cloud-mounted source>" "<local dest>"
```

Once staged, point `photo_organizer.py` at the destination folder.

### `tools/verify_archives.py`

photo_organizer.py is deliberately conservative: if an archive's
extraction produced any warning at all, it keeps the archive rather than
guessing that everything came out fine. This script lets you independently
prove, byte-for-byte, that an archive's contents are safe to discard -
using the archive's own authoritative directory listing (immune to the
extraction bugs that caused the warning) cross-checked against real
content hashes in your organized folder.

```
python tools/verify_archives.py "Old Backup.zip" --against "/path/to/Photo Archives"
```

Only delete an archive yourself after this reports 100% verified.

### `tools/photo_mirror.py`

Keeps a second drive as a mirror of the organized `Photo Archives` folder -
a backup of the backup. Plug both drives in (roughly monthly) and run it:
it does a dry run first, shows how many files are new, changed or removed,
asks before syncing, then copies with `rsync` at background disk priority
so it doesn't starve anything else using the drive. Safe to interrupt and
re-run - it resumes where it left off.

```
python tools/photo_mirror.py            # dry run, confirm, sync
python tools/photo_mirror.py --check    # check drives/rsync/digiKam only
python tools/photo_mirror.py --verify   # also checksum-compare every file (slow)
```

It is deliberately hard to point at the wrong disk:

- **Drives are pinned by volume UUID**, not just name (set in the config
  block at the top of the script). If either drive is missing, renamed or a
  different disk, it refuses - so nothing is ever written to the boot drive
  under an empty `/Volumes/...` path.
- **Nothing is lost on the mirror.** Files deleted from, or changed on, the
  source are moved into `_rsync_removed/<date>/` on the mirror drive rather
  than destroyed. Prune old dated folders when you're happy.
- **Refuses an empty or suspiciously small source**, and asks again (or
  blocks a `--yes` run) if more than 100 files would be removed or replaced.
- **digiKam-aware**: while digiKam is running its databases
  (`digikam4.db` etc.) are skipped, since a copy taken mid-write can be
  inconsistent - re-run once digiKam is closed. If digiKam has written tags
  into thousands of photos, use `--no-keep-removed` so the old versions
  aren't kept (they wouldn't fit).

Needs rsync 3.1+ (`brew install rsync`; macOS ships openrsync, which lacks
`-X` and `--info=progress2`). Logs go to `~/Library/Logs/photo-mirror/`.

## digiKam tools

These work alongside digiKam, which holds the people tags, face boxes and
dates for the organised archive. Anything that moves files also updates
digiKam's records, so tags follow the photos. Every tool that changes
anything has `--dry-run`, asks before acting, refuses to run while digiKam
is open, backs up digiKam's databases first
(`~/Pictures/digiKam DB backup <date>`), never deletes (removed files go to
`<drive>/Photo Archives - removed <what> <date>/`), and logs to
`~/Library/Logs/photo-tools/`. After a real run: open digiKam, let it scan,
then **Tools > Maintenance > "Perform database cleaning"**.

Extra requirement: `pip install numpy`.

### `tools/person_export.py` + `tools/person_sort.py`

Gather everyone photographed with one person into a folder, then tidy it:
keep the best copy of each photo, name it by the date taken, file it by
year (`2013/2013-08-06 08.07.13.jpg`), put undated ones in `Undated/`
(named after the most meaningful copy), scans in `Scanned prints/`, other
copies in `_duplicates/`, and delete face crops.

```
python tools/person_export.py "Jane Smith"          # copies to ~/Pictures/Jane Smith
python tools/person_sort.py ~/Pictures/"Jane Smith" --dry-run
python tools/person_sort.py ~/Pictures/"Jane Smith"
python tools/person_sort.py ~/Pictures/"Jane Smith" --resort   # undo + sort again
```

Sort locally, then move the finished folder to Google Drive to share.

### `tools/archive_dedupe.py`

Removes near-duplicates that exact hashing can't see - thumbnails, 1024px
exports, EXIF-stripped copies, Apple face crops - using digiKam's own
image fingerprints to find candidates (minutes, no drive reads) and a real
pixel comparison to confirm them (burst shots are not duplicates). People
tags on a copy are added to the kept original before it's moved.

```
python tools/archive_dedupe.py --dry-run
python tools/archive_dedupe.py
```

### `tools/noexif_organise.py`

Sorts `NO EXIF` into Year/Month (for Dropbox-style `2020-07-14 18.22.05.jpg`
names and metadata dates - the date is written into EXIF too), and
`Screenshots`, `Scanned prints`, `Facebook downloads`,
`Photo books & calendars`, `Apple previews (no original)` and
`Videos (undated)`. Byte-identical copies go to the holding folder.

### `tools/archive_audit.py`

Read-only health check: photos in the wrong Year/Month folder, photos whose
digiKam date is wrong (it often records when QuickTime/Photos last saved a
file rather than when it was taken), scanned prints outside
`Scanned prints`, and byte-identical files anywhere. Optional fixes:
`--fix-digikam-dates`, `--move-scans`, `--remove-identical`.

### Shared modules and tests

`digikam_db.py` (database access and safe writes), `similarity.py`
(fingerprint search + pixel check, with the thresholds and how they were
calibrated) and `media_utils.py` (file kinds, dates, exiftool). Tests for
the database-changing helpers run on a throw-away copy of the database:

```
python -m unittest discover -s tests -v
```

**If a tool reports "Operation not permitted" on the archive drive**: macOS
has cached a stale refusal. System Settings > Privacy & Security > Files
and Folders > Terminal > switch "Removable Volumes" off and on again.

**Scanned prints**: the February 2018 scanning session stamped every scan's
name *and* EXIF with the scan date. The tools never use that date.

## Design notes / why some things work the way they do

- **The zipfile ZIP64 bug**: Python's `zipfile` module misreads certain
  large (>4GB) real-world zip archives as having "overlapping entries" and
  refuses to extract most or all of their contents - even though they're
  perfectly valid. This isn't corruption; it's a known offset-calculation
  edge case that the system `unzip` (Info-ZIP) binary correctly
  recompensates for. photo_organizer.py prefers `unzip` for exactly this
  reason.
- **AppleDouble sidecars** (`._Foo.jpg`, `__MACOSX/` folders) that macOS's
  `tar`/`zip` tools embed alongside real files are filtered out - they
  carry a real media extension but are resource-fork metadata, not photos.
- **Collision suffixes are a last resort, not the norm.** Two different
  files can legitimately share a filename (e.g. the same photo re-exported
  through two different apps, with slightly different compression) - when
  this happens, both are kept, and only the second one gets a short hash
  suffix appended.
