#!/usr/bin/env python3
"""
verify_archives.py - Proves, byte-for-byte, that every real media file in
one or more archives has already been safely preserved elsewhere on disk,
before you consider deleting the archive.

THE PROBLEM THIS SOLVES
------------------------
photo_organizer.py deletes an archive only after successfully extracting
and organizing every file it contains - but some archives are legitimately
recoverable only in part (e.g. a known bug in Python's zipfile module
misreads certain large ZIP64 archives as having "overlapping entries" and
refuses to extract them, even though the system `unzip` tool recovers them
fine) or otherwise get extracted with a warning attached. photo_organizer.py
plays it safe in that situation and never auto-deletes the archive - this
script is how you independently confirm it's actually safe to do so
yourself, rather than taking anyone's word for it.

It does this in two ways:
  1. Reads the archive's own authoritative directory listing (a zip's
     central directory, or a tar's header table) to get the true list of
     every real media file that should be inside - this listing is reliable
     even when actual data extraction has bugs.
  2. Re-extracts the archive fresh, hashes every recovered media file, and
     searches your target directory (matched first by file size, then by
     exact SHA256 content hash) for a file that is BYTE-IDENTICAL to it.

Only if every single real media entry is accounted for does it report the
archive as verified safe to delete. Anything unaccounted for is listed
explicitly so you can investigate by hand. This script only reads - it
never deletes anything itself.

USAGE
-----
  python verify_archives.py <archive1> [archive2 ...] --against <organized_dir>

  Example:
    python verify_archives.py "Old Backup.zip" "Photos 2019.tar.gz" \\
      --against "/Volumes/Backup/Photo Archives"
"""
import argparse
import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp',
              '.heic', '.heif', '.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf',
              '.orf', '.rw2'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.m4v',
              '.mpg', '.mpeg', '.3gp', '.mts', '.m2ts'}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
TAR_SUFFIXES = ('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def is_sidecar(name: str) -> bool:
    base = Path(name).name
    return base.startswith('._') or '__MACOSX' in Path(name).parts


def real_media_entries(archive_path: Path):
    """Authoritative list of real media (name, size) pairs from the
    archive's own directory listing - not from re-extracting."""
    entries = []
    name = archive_path.name.lower()
    if name.endswith(TAR_SUFFIXES):
        with tarfile.open(archive_path, 'r:*') as tf:
            for member in tf.getmembers():
                if not member.isfile() or is_sidecar(member.name):
                    continue
                if Path(member.name).suffix.lower() in MEDIA_EXTS:
                    entries.append(member.name)
    else:
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                if info.is_dir() or is_sidecar(info.filename):
                    continue
                if Path(info.filename).suffix.lower() in MEDIA_EXTS:
                    entries.append(info.filename)
    return entries


def extract_archive(archive_path: Path, extract_dir: Path):
    """Re-extracts using the same tools/fallbacks as photo_organizer.py."""
    name = archive_path.name.lower()
    if name.endswith(TAR_SUFFIXES):
        with tarfile.open(archive_path, 'r:*') as tf:
            tf.extractall(extract_dir, filter='data')
        return
    unzip_bin = shutil_which('unzip')
    if unzip_bin:
        subprocess.run([unzip_bin, '-q', '-o', str(archive_path), '-d', str(extract_dir)],
                        capture_output=True, text=True, timeout=1800)
    else:
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_dir)


def shutil_which(name):
    import shutil
    return shutil.which(name)


def build_size_index(root: Path):
    print(f"Indexing {root} by file size (one-time pass)...")
    index = {}
    count = 0
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                index.setdefault(p.stat().st_size, []).append(p)
            except OSError:
                continue
            count += 1
    print(f"  Indexed {count} file(s).")
    return index


def main():
    parser = argparse.ArgumentParser(
        description='Verify every real media file in one or more archives is '
                    'already safely preserved elsewhere before deleting them.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('archives', nargs='+', help='Archive file(s) to verify')
    parser.add_argument('--against', required=True,
                         help='Organized directory to search for matching content')
    args = parser.parse_args()

    target_root = Path(args.against).resolve()
    if not target_root.exists():
        print(f"Target directory not found: {target_root}", file=sys.stderr)
        sys.exit(1)

    size_index = build_size_index(target_root)
    hash_cache = {}

    def hash_of(p):
        if p not in hash_cache:
            hash_cache[p] = sha256_file(p)
        return hash_cache[p]

    all_verified = True

    for archive_str in args.archives:
        archive_path = Path(archive_str).resolve()
        print(f"\n{'=' * 70}\n{archive_path.name}\n{'=' * 70}")
        if not archive_path.exists():
            print("  MISSING - skipping")
            all_verified = False
            continue

        try:
            entries = real_media_entries(archive_path)
        except Exception as e:
            print(f"  COULD NOT READ ARCHIVE LISTING: {e}")
            all_verified = False
            continue
        print(f"  Real media entries in archive's own listing: {len(entries)}")

        with tempfile.TemporaryDirectory(dir=str(target_root)) as td:
            td_path = Path(td)
            try:
                extract_archive(archive_path, td_path)
            except Exception as e:
                print(f"  EXTRACTION FAILED: {e}")
                all_verified = False
                continue

            verified, missing = 0, []
            for entry_name in entries:
                extracted_path = td_path / entry_name
                if not extracted_path.exists():
                    missing.append((entry_name, "not recovered on re-extraction"))
                    continue
                target_hash = hash_of(extracted_path)
                candidates = size_index.get(extracted_path.stat().st_size, [])
                if any(hash_of(c) == target_hash for c in candidates):
                    verified += 1
                else:
                    missing.append((entry_name, "no matching content found in target"))

            print(f"  Verified present (byte-for-byte): {verified}/{len(entries)}")
            if missing:
                all_verified = False
                print(f"  UNVERIFIED ({len(missing)}):")
                for name, reason in missing[:20]:
                    print(f"    - {name}  [{reason}]")
                if len(missing) > 20:
                    print(f"    ... and {len(missing) - 20} more")
            else:
                print("  RESULT: 100% verified - safe to delete this archive.")

    print(f"\n{'=' * 70}")
    print("ALL ARCHIVES FULLY VERIFIED" if all_verified else
          "SOME ARCHIVES HAVE UNVERIFIED ENTRIES - see above")
    print('=' * 70)
    sys.exit(0 if all_verified else 1)


if __name__ == '__main__':
    main()
