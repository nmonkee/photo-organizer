#!/usr/bin/env python3
"""
stage_from_cloud.py - Safely copies files out of an on-demand/"stream" mode
cloud-sync folder (Google Drive, OneDrive, Dropbox Smart Sync, iCloud Drive,
etc.) into a real local folder, without risking your boot drive.

THE PROBLEM THIS SOLVES
------------------------
Cloud-sync clients running in "stream"/"online-only" mode keep files as
lightweight placeholders on disk - `ls` shows the real file size, but
nothing is actually downloaded until something reads the file's content.
When you read a large batch of these files (e.g. to hash/copy/organize
them), the client downloads each one into a LOCAL CACHE - and on most of
these clients, that cache lives on your boot drive, not wherever you're
trying to copy the files TO. If you're bulk-processing many gigabytes and
your boot drive doesn't have that much room to spare, the client's cache
can silently fill it up mid-operation, which can affect far more than the
task at hand.

This script copies files ONE AT A TIME (optionally with a small amount of
bounded parallelism) directly from the cloud-mounted source to a real
destination folder, checking free space on your boot volume before every
single file and immediately halting - not crashing, not corrupting
anything already copied - the moment that margin gets too thin. Re-running
the script resumes exactly where it left off (already-copied files with a
matching size are skipped), so you can free up space and continue.

USAGE
-----
  python stage_from_cloud.py <source_dir> <dest_dir> [options]

  Example (Google Drive on macOS):
    python stage_from_cloud.py \\
      "$HOME/Library/CloudStorage/GoogleDrive-you@example.com/My Drive/Photos" \\
      "/Volumes/Backup/Staging/Photos"

Options:
  --workers N       Parallel copy workers (default: 3). Higher = faster but
                     grows the cloud client's cache faster too - see
                     --min-free-gb.
  --min-free-gb N   Safety floor in GB on the monitored volume (default: 5).
                     Raise this if you use more workers, since with N
                     workers up to N files' worth of cache growth can occur
                     between checks.
  --monitor-path P  Which path's volume to watch free space on (default:
                     your home directory - this is where most cloud
                     clients' local cache lives on macOS/Windows/Linux).

NOTES
-----
- This only ever READS from the source and WRITES to the destination. It
  never deletes or modifies anything at the source - your cloud drive is
  never touched.
- Some cloud-sync file providers have a known quirk where a single stalled
  file can leave a worker thread stuck in an uninterruptible state that
  not even SIGKILL clears immediately - if `pkill` doesn't work right away,
  it's usually safe to just wait a minute; the OS clears it on its own.
- Once files are staged locally, feed the destination folder into
  photo_organizer.py as you would any local folder.
"""
import argparse
import concurrent.futures
import os
import shutil
import sys
import threading
import time
from pathlib import Path

check_lock = threading.Lock()
counters_lock = threading.Lock()
stop_flag = threading.Event()
counters = {'copied': 0, 'skipped': 0, 'errors': 0}


def boot_free_bytes(monitor_path: Path) -> int:
    return shutil.disk_usage(str(monitor_path)).free


def copy_one(src_path: Path, src_root: Path, dest_root: Path,
             min_free_bytes: int, monitor_path: Path):
    if stop_flag.is_set():
        return
    rel = src_path.relative_to(src_root)
    dest_path = dest_root / rel

    try:
        src_size = src_path.stat().st_size
    except OSError as e:
        with counters_lock:
            counters['errors'] += 1
        print(f"  STAT FAILED: {rel}: {e}")
        return

    if dest_path.exists() and dest_path.stat().st_size == src_size:
        with counters_lock:
            counters['skipped'] += 1
        return

    with check_lock:
        if stop_flag.is_set():
            return
        free = boot_free_bytes(monitor_path)
        if free < min_free_bytes:
            stop_flag.set()
            print(f"\nHALTING: free space on {monitor_path} "
                  f"({free / 1024**3:.2f}GB) dropped below the "
                  f"{min_free_bytes / 1024**3:.0f}GB safety floor before "
                  f"starting {rel}.")
            return

    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)
        with counters_lock:
            counters['copied'] += 1
    except Exception as e:
        with counters_lock:
            counters['errors'] += 1
        print(f"  COPY FAILED: {rel}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description='Safely stage files out of a cloud-sync "stream" folder '
                    'without risking your boot drive.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('source_dir', help='Cloud-mounted source folder')
    parser.add_argument('dest_dir', help='Real local destination folder')
    parser.add_argument('--workers', type=int, default=3,
                         help='Parallel copy workers (default: 3)')
    parser.add_argument('--min-free-gb', type=float, default=5.0,
                         help='Safety floor in GB (default: 5)')
    parser.add_argument('--monitor-path', default=None,
                         help='Path whose volume to watch free space on '
                              '(default: your home directory)')
    args = parser.parse_args()

    src_root = Path(args.source_dir).resolve()
    dest_root = Path(args.dest_dir).resolve()
    monitor_path = Path(args.monitor_path).resolve() if args.monitor_path else Path.home()
    min_free_bytes = int(args.min_free_gb * 1024**3)

    if not src_root.exists():
        print(f"Source not found: {src_root}", file=sys.stderr)
        sys.exit(1)
    dest_root.mkdir(parents=True, exist_ok=True)

    all_files = [Path(dirpath) / fn
                 for dirpath, _, filenames in os.walk(src_root)
                 for fn in filenames]
    total = len(all_files)
    print(f"Found {total} file(s) to copy. Using {args.workers} worker(s), "
          f"{args.min_free_gb:.0f}GB safety floor on {monitor_path}.")

    t0 = time.time()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(copy_one, p, src_root, dest_root,
                              min_free_bytes, monitor_path): p
                   for p in all_files}
        for _ in concurrent.futures.as_completed(futures):
            done += 1
            if done % 100 == 0 or done == total:
                elapsed = time.time() - t0
                free_gb = boot_free_bytes(monitor_path) / 1024**3
                with counters_lock:
                    c = dict(counters)
                print(f"  {done}/{total} processed (copied={c['copied']} "
                      f"skipped={c['skipped']} errors={c['errors']}) - "
                      f"free: {free_gb:.2f}GB - elapsed: {elapsed / 60:.1f}min")

    with counters_lock:
        c = dict(counters)
    if stop_flag.is_set():
        print(f"\nHALTED EARLY. copied={c['copied']} skipped={c['skipped']} "
              f"errors={c['errors']} of {total}. Re-run to resume.")
        sys.exit(2)
    else:
        print(f"\nDONE. copied={c['copied']} skipped={c['skipped']} "
              f"errors={c['errors']} of {total}.")


if __name__ == '__main__':
    main()
