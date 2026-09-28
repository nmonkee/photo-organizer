#!/usr/bin/env python3
"""
photo-mirror – keep a mirror copy of the Photo Archives backup drive.

Plug in both drives, then run:

    photo-mirror              # dry run, show what would change, ask, then sync
    photo-mirror --yes        # no prompt (still refuses if something looks wrong)
    photo-mirror --check      # only check drives / rsync / digiKam, copy nothing
    photo-mirror --verify     # after syncing, compare every file by checksum (slow)
    photo-mirror --no-keep-removed   # don't keep old versions (see note below)

Safety:
  * Both volumes must be mounted AND have the expected volume UUIDs, so a renamed or
    different disk can never be written to (and nothing is ever written to the Mac's
    own disk if a drive is missing).
  * Refuses to run if the source looks empty or much smaller than the mirror.
  * Files deleted from, or changed on, the source are NOT destroyed on the mirror:
    the old copies are moved to  <mirror volume>/_rsync_removed/<date>/  instead.
  * If digiKam is running, its databases are skipped (they are mid-write) and you are
    told to re-run once digiKam is closed.
  * Runs rsync at background disk priority by default so it doesn't starve other apps.
  * _rsync_removed/ grows over time – delete old dated folders from it when you're happy.

Logs: ~/Library/Logs/photo-mirror/
"""
import argparse
import datetime as dt
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- config
SOURCE_VOLUME = "/Volumes/Backup 4TB"
SOURCE_UUID = "7DB63B40-461E-3830-8CB0-A83727463F9A"
MIRROR_VOLUME = "/Volumes/Backup Mirror 4TB "          # note: the name ends with a space
MIRROR_UUID = "2773AA18-ECF5-4F4E-B127-38F3CF08BD1E"
FOLDER = "Photo Archives"                                # synced folder on both volumes

RSYNC_CANDIDATES = ["/opt/homebrew/bin/rsync", "/usr/local/bin/rsync"]
ALWAYS_EXCLUDE = [".DS_Store", "._*", ".Spotlight-V100", ".fseventsd", ".Trashes",
                  ".TemporaryItems", ".DocumentRevisions-V100"]
DIGIKAM_DB_PATTERNS = ["digikam4.db*", "thumbnails-digikam.db*", "recognition.db*",
                       "similarity.db*"]
DELETE_WARN_FILES = 100        # ask again if more files than this would be removed/replaced
MIN_SOURCE_RATIO = 0.90        # refuse if source < 90% of mirror size (e.g. wrong/empty folder)
LOG_DIR = Path.home() / "Library/Logs/photo-mirror"
# -----------------------------------------------------------------------------------


class Abort(Exception):
    pass


def log_print(msg, logf=None):
    print(msg, flush=True)
    if logf:
        logf.write(msg + "\n")
        logf.flush()


def volume_uuid(mountpoint):
    out = subprocess.run(["diskutil", "info", "-plist", mountpoint], capture_output=True)
    if out.returncode != 0:
        return None
    return plistlib.loads(out.stdout).get("VolumeUUID")


def check_volume(label, mountpoint, expected_uuid):
    if not os.path.ismount(mountpoint):
        raise Abort(f"{label} drive is not mounted at '{mountpoint}'. Plug it in and try again.")
    uuid = volume_uuid(mountpoint)
    if uuid != expected_uuid:
        raise Abort(f"{label} drive at '{mountpoint}' has UUID {uuid}, expected {expected_uuid}.\n"
                    f"  This is not the expected disk – refusing to touch it.")


def find_rsync():
    for c in RSYNC_CANDIDATES + [shutil.which("rsync") or ""]:
        if c and os.access(c, os.X_OK):
            out = subprocess.run([c, "--version"], capture_output=True, text=True).stdout
            m = re.search(r"rsync\s+version\s+(\d+)\.(\d+)", out)
            if m and (int(m.group(1)), int(m.group(2))) >= (3, 1):
                return c, f"{m.group(1)}.{m.group(2)}"
    raise Abort("rsync 3.1+ not found. Install it with:  brew install rsync")


def digikam_running():
    return subprocess.run(["pgrep", "-x", "digikam"], capture_output=True).returncode == 0


def du_bytes(path):
    """Fast-ish used-space estimate from the volume (whole volume, not just the folder)."""
    st = os.statvfs(path)
    return (st.f_blocks - st.f_bfree) * st.f_frsize


def fmt_bytes(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024


def build_cmd(rsync, src, dst, backup_dir, excludes, dry_run, background, extra=()):
    """backup_dir=None -> removed/changed files are not kept."""
    cmd = []
    if background:
        cmd += ["taskpolicy", "-b"]
    cmd += [rsync, "-aX", "--delete", "--partial", "--human-readable",
            "--info=stats2" + (",progress2" if not dry_run else "")]
    if backup_dir:
        cmd += ["--backup", f"--backup-dir={backup_dir}"]
    if dry_run:
        cmd += ["--dry-run", "--itemize-changes"]
    for e in excludes:
        cmd += ["--exclude", e]
    cmd += list(extra) + [src, dst]
    return cmd


def parse_dry_run(output):
    """Count new / updated / deleted items from --itemize-changes output."""
    new = upd = dele = 0
    new_bytes_line = None
    for line in output.splitlines():
        if line.startswith("*deleting"):
            dele += 1
        elif re.match(r"^>f\+{9,}", line):
            new += 1
        elif re.match(r"^>f", line):
            upd += 1
        m = re.match(r"Total transferred file size:\s*(.+?) bytes", line)
        if m:
            new_bytes_line = m.group(1)
    return new, upd, dele, new_bytes_line


def main():
    ap = argparse.ArgumentParser(description="Mirror the Photo Archives backup drive.")
    ap.add_argument("--check", action="store_true", help="checks only, copy nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--verify", action="store_true", help="checksum-compare after syncing (slow)")
    ap.add_argument("--foreground", action="store_true",
                    help="run rsync at normal disk priority (faster when nothing else is busy)")
    ap.add_argument("--no-keep-removed", action="store_true",
                    help="don't keep old versions of removed/changed files in _rsync_removed "
                         "(use when e.g. digiKam has rewritten metadata in thousands of photos)")
    ap.add_argument("--include-digikam-db", action="store_true",
                    help="copy digiKam databases even if digiKam is running (not recommended)")
    # overrides, mainly for testing
    ap.add_argument("--source", help=argparse.SUPPRESS)
    ap.add_argument("--dest", help=argparse.SUPPRESS)
    ap.add_argument("--no-volume-check", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = LOG_DIR / f"photo-mirror_{stamp}.log"
    logf = open(log_path, "w")
    say = lambda m="": log_print(m, logf)

    try:
        # ---- preflight
        if not args.no_volume_check:
            check_volume("Source", SOURCE_VOLUME, SOURCE_UUID)
            check_volume("Mirror", MIRROR_VOLUME, MIRROR_UUID)
        src_dir = Path(args.source or os.path.join(SOURCE_VOLUME, FOLDER))
        dst_dir = Path(args.dest or os.path.join(MIRROR_VOLUME, FOLDER))
        mirror_root = dst_dir.parent
        if not src_dir.is_dir():
            raise Abort(f"Source folder not found: {src_dir}")
        if not any(p for p in src_dir.iterdir() if not p.name.startswith(".")):
            raise Abort(f"Source folder is empty: {src_dir} – refusing (this would wipe the mirror).")
        dst_dir.mkdir(exist_ok=True)

        rsync, ver = find_rsync()
        dk = digikam_running()
        excludes = list(ALWAYS_EXCLUDE)
        if dk and not args.include_digikam_db:
            excludes += DIGIKAM_DB_PATTERNS
        backup_dir = None if args.no_keep_removed else mirror_root / "_rsync_removed" / stamp
        excludes.append("/_rsync_removed/")  # never recurse into it if folder layout changes

        src_used, dst_used = du_bytes(src_dir), du_bytes(dst_dir)
        free = shutil.disk_usage(dst_dir).free
        say(f"photo-mirror  {stamp}")
        say(f"  source : {src_dir}")
        say(f"  mirror : {dst_dir}")
        say(f"  rsync  : {rsync} (v{ver})")
        say(f"  source volume used {fmt_bytes(src_used)} · mirror volume used {fmt_bytes(dst_used)}, free {fmt_bytes(free)}")
        if dk:
            say("  digiKam is RUNNING – " + ("databases WILL be copied (may be inconsistent)" if args.include_digikam_db
                                            else "its databases will be skipped; re-run after closing digiKam"))
        if not args.no_volume_check and dst_used > 50 * 2**30 and src_used < MIN_SOURCE_RATIO * dst_used:
            raise Abort(f"Source volume ({fmt_bytes(src_used)}) is much smaller than the mirror ({fmt_bytes(dst_used)}).\n"
                        f"  That usually means the wrong drive or missing files – refusing to sync.")
        if args.check:
            say("\nChecks passed. Nothing copied (--check).")
            return 0

        src, dst = str(src_dir) + "/", str(dst_dir) + "/"

        # ---- dry run
        say("\nDry run (working out what would change – may take a few minutes)...")
        dry = subprocess.run(build_cmd(rsync, src, dst, backup_dir, excludes, True, not args.foreground),
                             capture_output=True, text=True)
        logf.write(dry.stdout + dry.stderr)
        if dry.returncode not in (0, 24):
            raise Abort(f"Dry run failed (rsync exit {dry.returncode}):\n{dry.stderr.strip()[-2000:]}")
        new, upd, dele, size = parse_dry_run(dry.stdout)
        say(f"  new files: {new:,}   changed files: {upd:,}   removed from source: {dele:,}")
        if size:
            say(f"  data to copy: {size} bytes")
        if (dele or upd) and backup_dir:
            say(f"  (old versions of removed/changed files will be kept in {backup_dir})")
        elif dele or upd:
            say("  (--no-keep-removed: old versions of removed/changed files will NOT be kept)")
        if new == upd == dele == 0:
            say("\nMirror is already up to date.")
            return 0
        if dele + upd > DELETE_WARN_FILES:
            say(f"\n  WARNING: {dele + upd:,} files would be removed or replaced on the mirror.")
            sample = [l for l in dry.stdout.splitlines() if l.startswith("*deleting")][:15]
            for l in sample:
                say("    " + l)
            if backup_dir:
                say("  Keeping all their old versions needs extra space on the mirror; if that's a problem\n"
                    "  (e.g. digiKam rewrote tags inside photos) re-run with --no-keep-removed.")
            if args.yes:
                raise Abort("Too many removals for an unattended run – re-run without --yes and confirm.")
        if not args.yes:
            ans = input("\nProceed with sync? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                say("Cancelled.")
                return 1

        # ---- real run
        say(f"\nSyncing{' (background disk priority)' if not args.foreground else ''}... Ctrl-C is safe; re-run to resume.")
        cmd = build_cmd(rsync, src, dst, backup_dir, excludes, False, not args.foreground)
        logf.write("CMD: " + " ".join(shlex.quote(c) for c in cmd) + "\n")
        rc = subprocess.run(cmd).returncode
        if rc not in (0, 24):     # 24 = some source files vanished mid-run (fine for a live disk)
            raise Abort(f"rsync exited with code {rc}. Re-run to resume; see {log_path}")

        # ---- optional verify
        if args.verify:
            say("\nVerifying by checksum (reads every file on both drives – slow)...")
            ver_run = subprocess.run(build_cmd(rsync, src, dst, backup_dir, excludes, True, not args.foreground,
                                               extra=["--checksum"]), capture_output=True, text=True)
            logf.write(ver_run.stdout)
            n, u, d, _ = parse_dry_run(ver_run.stdout)
            say("  verify: all files match." if n == u == d == 0 else
                f"  verify: {n + u + d:,} differences – re-run photo-mirror; details in {log_path}")

        (mirror_root / ".photo-mirror-last-sync").write_text(
            f"{dt.datetime.now().isoformat(timespec='seconds')}\nsource={src_dir}\n"
            f"digikam_db_skipped={dk and not args.include_digikam_db}\n")
        say(f"\nDone. Log: {log_path}")
        if dk and not args.include_digikam_db:
            say("Reminder: digiKam was running, so its databases were not copied. Re-run after closing it.")
        return 0

    except Abort as e:
        say(f"\nSTOPPED: {e}")
        return 2
    except KeyboardInterrupt:
        say("\nInterrupted. Safe to re-run – rsync will resume where it left off.")
        return 130
    finally:
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
