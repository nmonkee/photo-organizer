#!/usr/bin/env python3
"""
Photo Organizer - De-duplicates and organizes media files by EXIF data.

Features:
  - SHA256 content hashing for deduplication (loose files AND files inside
    .zip/.tar/.tar.gz/.tgz/.tar.bz2 archives)
  - EXIF extraction via exiftool (preferred - handles JPEG/HEIC/RAW/video) with a
    Pillow fallback for images if exiftool is not installed
  - Offline reverse geocoding (reverse_geocoder package) - no internet, no rate
    limits, no third-party usage-policy concerns
  - Organized folder hierarchy (built under "<source>/Photo Archives/"):
      Year/Month/Town/Image.jpg          (date + UK location)
      Year/Month/Country/Town/Image.jpg  (date + non-UK location)
      Year/Month/Image.jpg               (date only)
      NO EXIF/Town/Image.jpg             (no date + location)
      NO EXIF/Image.jpg                  (no date + no location)
  - Original filenames are always preserved; a short content-hash suffix is
    appended ONLY when two different files would otherwise collide at the same
    destination path.
  - Archives (zip or tar-family) are extracted, their contents folded into
    the same dedup/organize pipeline, and (only in a real run) an archive is
    deleted once EVERY media file it contained has been verified moved to
    its final destination (or proven to be a duplicate already accounted
    for elsewhere). An archive is never deleted if anything about its
    contents failed or is unclear.
  - A full, untruncated audit report is always written to disk (dry-run or
    not) BEFORE any file is touched.
  - Dry-run mode: plans everything and writes the audit report; changes
    nothing. A real run additionally requires typed confirmation (or --yes).
  - Media files are only ever deleted when proven to be an exact-content
    duplicate of a file that has already been placed at its destination.

Usage:
  python photo_organizer.py "/Volumes/Backup 4TB/Photo Archives" --dry-run
  python photo_organizer.py "/Volumes/Backup 4TB/Photo Archives"
  python photo_organizer.py "/Volumes/Backup 4TB/Photo Archives" --yes

Dependencies:
  pip install Pillow reverse_geocoder
  brew install exiftool   (strongly recommended - required for video EXIF and
                            for reliable HEIC/RAW support)
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple

# ── Optional imports ────────────────────────────────────────────────────────
try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAS_HEIF = True
except ImportError:
    HAS_HEIF = False

try:
    import reverse_geocoder as rg
    HAS_RG = True
except ImportError:
    HAS_RG = False

# ── Constants ────────────────────────────────────────────────────────────────
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp',
              '.heic', '.heif', '.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf',
              '.orf', '.rw2'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.m4v',
              '.mpg', '.mpeg', '.3gp', '.mts', '.m2ts', '.lrv'}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

NO_EXIF_DIR = "NO EXIF"

# Multi-part suffixes checked against the full lowercased filename, since
# Path.suffix only ever returns the LAST suffix (".tar.gz".suffix == ".gz").
TAR_SUFFIXES = ('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')

EXIF_DATE_TAGS = ['DateTimeOriginal', 'DateTime', 'DateTimeDigitized']


def sniff_media_extension(path: Path) -> Optional[str]:
    """Identifies real media content in a file that has NO extension at all,
    by inspecting its header bytes. Some real-world sources (older iMovie
    libraries in particular, which store imported camera footage as bare
    'clip-<timestamp>' files with no extension) hide genuine, often
    irreplaceable video/photo content this way - a plain suffix check would
    silently skip it entirely. Returns a dotted extension (e.g. '.mov') on a
    confident match, else None. Only ever called on files whose name
    already has no extension, so this never overrides a real one."""
    try:
        with open(path, 'rb') as f:
            head = f.read(32)
    except OSError:
        return None
    if head[:3] == b'\xff\xd8\xff':
        return '.jpg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    if len(head) >= 12 and head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in (b'qt  ', b'qt24'):
            return '.mov'
        if brand in (b'heic', b'heix', b'mif1', b'msf1'):
            return '.heic'
        return '.mp4'
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return '.avi'
    if head[:4] == b'\x00\x00\x01\xba':
        return '.mpg'  # MPEG program stream (older camcorders/point-and-shoots)
    return None

# ISO 3166-1 alpha-2 -> country name (reverse_geocoder returns alpha-2 codes)
ISO2_TO_COUNTRY = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AF": "Afghanistan",
    "AG": "Antigua and Barbuda", "AI": "Anguilla", "AL": "Albania", "AM": "Armenia",
    "AO": "Angola", "AQ": "Antarctica", "AR": "Argentina", "AS": "American Samoa",
    "AT": "Austria", "AU": "Australia", "AW": "Aruba", "AX": "Aland Islands",
    "AZ": "Azerbaijan", "BA": "Bosnia and Herzegovina", "BB": "Barbados",
    "BD": "Bangladesh", "BE": "Belgium", "BF": "Burkina Faso", "BG": "Bulgaria",
    "BH": "Bahrain", "BI": "Burundi", "BJ": "Benin", "BM": "Bermuda",
    "BN": "Brunei", "BO": "Bolivia", "BQ": "Bonaire", "BR": "Brazil",
    "BS": "Bahamas", "BT": "Bhutan", "BW": "Botswana", "BY": "Belarus",
    "BZ": "Belize", "CA": "Canada", "CC": "Cocos Islands",
    "CD": "DR Congo", "CF": "Central African Republic", "CG": "Congo",
    "CH": "Switzerland", "CI": "Ivory Coast", "CK": "Cook Islands", "CL": "Chile",
    "CM": "Cameroon", "CN": "China", "CO": "Colombia", "CR": "Costa Rica",
    "CU": "Cuba", "CV": "Cape Verde", "CW": "Curacao", "CX": "Christmas Island",
    "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany", "DJ": "Djibouti",
    "DK": "Denmark", "DM": "Dominica", "DO": "Dominican Republic", "DZ": "Algeria",
    "EC": "Ecuador", "EE": "Estonia", "EG": "Egypt", "EH": "Western Sahara",
    "ER": "Eritrea", "ES": "Spain", "ET": "Ethiopia", "FI": "Finland",
    "FJ": "Fiji", "FK": "Falkland Islands", "FM": "Micronesia", "FO": "Faroe Islands",
    "FR": "France", "GA": "Gabon", "GB": "United Kingdom", "GD": "Grenada",
    "GE": "Georgia", "GF": "French Guiana", "GG": "Guernsey", "GH": "Ghana",
    "GI": "Gibraltar", "GL": "Greenland", "GM": "Gambia", "GN": "Guinea",
    "GP": "Guadeloupe", "GQ": "Equatorial Guinea", "GR": "Greece",
    "GT": "Guatemala", "GU": "Guam", "GW": "Guinea-Bissau", "GY": "Guyana",
    "HK": "Hong Kong", "HN": "Honduras", "HR": "Croatia", "HT": "Haiti",
    "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland", "IL": "Israel",
    "IM": "Isle of Man", "IN": "India", "IO": "British Indian Ocean Territory",
    "IQ": "Iraq", "IR": "Iran", "IS": "Iceland", "IT": "Italy", "JE": "Jersey",
    "JM": "Jamaica", "JO": "Jordan", "JP": "Japan", "KE": "Kenya",
    "KG": "Kyrgyzstan", "KH": "Cambodia", "KI": "Kiribati", "KM": "Comoros",
    "KN": "Saint Kitts and Nevis", "KP": "North Korea", "KR": "South Korea",
    "KW": "Kuwait", "KY": "Cayman Islands", "KZ": "Kazakhstan", "LA": "Laos",
    "LB": "Lebanon", "LC": "Saint Lucia", "LI": "Liechtenstein", "LK": "Sri Lanka",
    "LR": "Liberia", "LS": "Lesotho", "LT": "Lithuania", "LU": "Luxembourg",
    "LV": "Latvia", "LY": "Libya", "MA": "Morocco", "MC": "Monaco",
    "MD": "Moldova", "ME": "Montenegro", "MF": "Saint Martin", "MG": "Madagascar",
    "MH": "Marshall Islands", "MK": "North Macedonia", "ML": "Mali",
    "MM": "Myanmar", "MN": "Mongolia", "MO": "Macau",
    "MP": "Northern Mariana Islands", "MQ": "Martinique", "MR": "Mauritania",
    "MS": "Montserrat", "MT": "Malta", "MU": "Mauritius", "MV": "Maldives",
    "MW": "Malawi", "MX": "Mexico", "MY": "Malaysia", "MZ": "Mozambique",
    "NA": "Namibia", "NC": "New Caledonia", "NE": "Niger", "NF": "Norfolk Island",
    "NG": "Nigeria", "NI": "Nicaragua", "NL": "Netherlands", "NO": "Norway",
    "NP": "Nepal", "NR": "Nauru", "NU": "Niue", "NZ": "New Zealand",
    "OM": "Oman", "PA": "Panama", "PE": "Peru", "PF": "French Polynesia",
    "PG": "Papua New Guinea", "PH": "Philippines", "PK": "Pakistan",
    "PL": "Poland", "PM": "Saint Pierre and Miquelon", "PR": "Puerto Rico",
    "PS": "Palestine", "PT": "Portugal", "PW": "Palau", "PY": "Paraguay",
    "QA": "Qatar", "RE": "Reunion", "RO": "Romania", "RS": "Serbia",
    "RU": "Russia", "RW": "Rwanda", "SA": "Saudi Arabia", "SB": "Solomon Islands",
    "SC": "Seychelles", "SD": "Sudan", "SE": "Sweden", "SG": "Singapore",
    "SH": "Saint Helena", "SI": "Slovenia", "SJ": "Svalbard and Jan Mayen",
    "SK": "Slovakia", "SL": "Sierra Leone", "SM": "San Marino", "SN": "Senegal",
    "SO": "Somalia", "SR": "Suriname", "SS": "South Sudan",
    "ST": "Sao Tome and Principe", "SV": "El Salvador", "SX": "Sint Maarten",
    "SY": "Syria", "SZ": "Eswatini", "TC": "Turks and Caicos Islands",
    "TD": "Chad", "TF": "French Southern Territories", "TG": "Togo",
    "TH": "Thailand", "TJ": "Tajikistan", "TK": "Tokelau", "TL": "Timor-Leste",
    "TM": "Turkmenistan", "TN": "Tunisia", "TO": "Tonga", "TR": "Turkey",
    "TT": "Trinidad and Tobago", "TV": "Tuvalu", "TW": "Taiwan",
    "TZ": "Tanzania", "UA": "Ukraine", "UG": "Uganda", "US": "United States",
    "UY": "Uruguay", "UZ": "Uzbekistan", "VA": "Vatican City",
    "VC": "Saint Vincent and the Grenadines", "VE": "Venezuela",
    "VG": "British Virgin Islands", "VI": "U.S. Virgin Islands", "VN": "Vietnam",
    "VU": "Vanuatu", "WF": "Wallis and Futuna", "WS": "Samoa", "YE": "Yemen",
    "YT": "Mayotte", "ZA": "South Africa", "ZM": "Zambia", "ZW": "Zimbabwe",
}
UK_CODES = {"GB"}


# ── EXIF Extractor ───────────────────────────────────────────────────────────
class EXIFExtractor:
    """Extracts date + GPS. Prefers exiftool (handles JPEG/HEIC/RAW/video
    uniformly); falls back to Pillow for still images if exiftool is absent."""

    def __init__(self):
        self.exiftool_path = self._find_exiftool()
        if not self.exiftool_path:
            print("WARNING: exiftool not found. Falling back to Pillow for still "
                  "images only; video files will have NO date/GPS metadata and "
                  "will land in 'NO EXIF'. Install with: brew install exiftool",
                  file=sys.stderr)

    @staticmethod
    def _find_exiftool() -> Optional[str]:
        for candidate in ["/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool"]:
            if os.path.exists(candidate):
                return candidate
        result = subprocess.run(["which", "exiftool"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None

    def extract(self, filepath: Path) -> dict:
        if self.exiftool_path:
            result = self._extract_via_exiftool(filepath)
        else:
            ext = filepath.suffix.lower()
            if ext in IMAGE_EXTS and HAS_PIL:
                result = self._extract_image_pil(filepath)
            else:
                result = {"date": None, "lat": None, "lon": None}

        if result['date'] is None or (result['lat'] is None and result['lon'] is None):
            sidecar = self._google_takeout_sidecar(filepath)
            if sidecar:
                if result['date'] is None and sidecar['date'] is not None:
                    result['date'] = sidecar['date']
                if result['lat'] is None and result['lon'] is None and sidecar['lat'] is not None:
                    result['lat'] = sidecar['lat']
                    result['lon'] = sidecar['lon']
        return result

    @staticmethod
    def _google_takeout_sidecar(filepath: Path) -> Optional[dict]:
        """Google Photos Takeout exports pair each media file with a
        '<filename>.json' sidecar holding 'photoTakenTime' and 'geoData' -
        metadata that's often more reliable than (or the only source of)
        the actual file's own EXIF, since Google Photos frequently strips
        EXIF from exported originals. Used only as a fallback when the
        file's own EXIF has no date and/or no GPS."""
        sidecar_path = filepath.parent / f"{filepath.name}.json"
        if not sidecar_path.exists():
            return None
        try:
            data = json.loads(sidecar_path.read_text())
        except Exception:
            return None

        date = None
        ts = data.get('photoTakenTime', {}).get('timestamp')
        if ts:
            try:
                date = datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
            except (ValueError, OSError):
                pass

        lat, lon = None, None
        for geo_key in ('geoData', 'geoDataExif'):
            geo = data.get(geo_key, {})
            g_lat, g_lon = geo.get('latitude'), geo.get('longitude')
            if g_lat not in (None, 0.0) or g_lon not in (None, 0.0):
                lat, lon = g_lat, g_lon
                break

        if date is None and lat is None:
            return None
        return {"date": date, "lat": lat, "lon": lon}

    def _extract_via_exiftool(self, filepath: Path) -> dict:
        try:
            result = subprocess.run(
                [self.exiftool_path, '-j', '-n', '-d', '%Y:%m:%d %H:%M:%S', str(filepath)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0 or not result.stdout.strip():
                return {"date": None, "lat": None, "lon": None}

            data = json.loads(result.stdout)
            if not data:
                return {"date": None, "lat": None, "lon": None}

            entry = data[0]
            date = None
            for key in ['DateTimeOriginal', 'CreateDate', 'MediaCreateDate', 'DateTimeDigitized']:
                val = entry.get(key)
                if val:
                    try:
                        date = datetime.strptime(str(val).split('+')[0].split('.')[0].strip(),
                                                  '%Y:%m:%d %H:%M:%S')
                        break
                    except ValueError:
                        continue

            lat, lon = None, None
            gps_lat = entry.get('GPSLatitude')
            gps_lon = entry.get('GPSLongitude')
            if gps_lat is not None and gps_lon is not None:
                lat = self._parse_coord(gps_lat, entry.get('GPSLatitudeRef'))
                lon = self._parse_coord(gps_lon, entry.get('GPSLongitudeRef'))

            return {"date": date, "lat": lat, "lon": lon}
        except Exception:
            return {"date": None, "lat": None, "lon": None}

    def _extract_image_pil(self, filepath: Path) -> dict:
        try:
            img = Image.open(filepath)
            exif = img.getexif()
            if not exif:
                return {"date": None, "lat": None, "lon": None}

            date = None
            for tag_id, value in exif.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag in EXIF_DATE_TAGS:
                    try:
                        date = datetime.strptime(str(value), '%Y:%m:%d %H:%M:%S')
                        break
                    except ValueError:
                        pass

            lat, lon = None, None
            try:
                gps_ifd = exif.get_ifd(0x8825)  # GPS IFD
                if gps_ifd:
                    lat = self._dms_to_decimal(gps_ifd.get(2))
                    lat_ref = str(gps_ifd.get(1, 'N')).upper()
                    lon = self._dms_to_decimal(gps_ifd.get(4))
                    lon_ref = str(gps_ifd.get(3, 'E')).upper()
                    if lat is not None and lat_ref == 'S':
                        lat = -lat
                    if lon is not None and lon_ref == 'W':
                        lon = -lon
            except Exception:
                pass

            return {"date": date, "lat": lat, "lon": lon}
        except Exception:
            return {"date": None, "lat": None, "lon": None}

    @staticmethod
    def _dms_to_decimal(value) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, (tuple, list)) and len(value) >= 3:
            deg, minutes, sec = float(value[0]), float(value[1]), float(value[2])
            return deg + minutes / 60 + sec / 3600
        return None

    @staticmethod
    def _parse_coord(value, ref) -> Optional[float]:
        """Parse exiftool GPS coordinate (numeric or 'D deg M' S\" ref' string)."""
        decimal = None
        if isinstance(value, (int, float)):
            decimal = float(value)
        else:
            s = str(value).strip()
            match = re.match(r'(-?\d+(?:\.\d+)?)[^\d]+(\d+(?:\.\d+)?)[^\d]+([\d.]+)', s)
            if match:
                deg, minutes, sec = (float(match.group(1)), float(match.group(2)),
                                      float(match.group(3)))
                decimal = abs(deg) + minutes / 60 + sec / 3600
                if deg < 0:
                    decimal = -decimal
            else:
                try:
                    decimal = float(s)
                except ValueError:
                    return None
        if decimal is None:
            return None
        ref_s = str(ref).strip().upper() if ref else ''
        if ref_s in ('S', 'W') and decimal > 0:
            decimal = -decimal
        return decimal


# ── Offline reverse geocoder ─────────────────────────────────────────────────
class OfflineGeocoder:
    """Batch offline reverse geocoding via the reverse_geocoder package
    (bundled GeoNames city dataset). No network calls, no rate limits."""

    def __init__(self):
        self.cache: Dict[Tuple[float, float], dict] = {}

    @staticmethod
    def _key(lat: float, lon: float) -> Tuple[float, float]:
        return (round(lat, 3), round(lon, 3))

    def resolve_all(self, coords: List[Tuple[float, float]]):
        """Batch-resolve a list of (lat, lon) pairs; populates self.cache."""
        if not coords:
            return
        if not HAS_RG:
            print("WARNING: reverse_geocoder not installed - geotagged files will "
                  "be treated as location-unknown. Install with: "
                  "pip install reverse_geocoder", file=sys.stderr)
            return

        unique = sorted({self._key(lat, lon) for lat, lon in coords})
        to_lookup = [k for k in unique if k not in self.cache]
        if not to_lookup:
            return

        print(f"Reverse geocoding {len(to_lookup)} unique location(s) offline...")
        results = rg.search(to_lookup, mode=1)
        for key, res in zip(to_lookup, results):
            cc = res.get('cc', '')
            country = ISO2_TO_COUNTRY.get(cc, cc or None)
            town = res.get('name') or None
            self.cache[key] = {
                'country': country,
                'town': town,
                'is_uk': cc in UK_CODES,
            }

    def get(self, lat: float, lon: float) -> Optional[dict]:
        return self.cache.get(self._key(lat, lon))


# ── Main Organizer ───────────────────────────────────────────────────────────
class MediaOrganizer:
    """Orchestrates scanning, dedup, geocoding, planning, reporting and execution."""

    def __init__(self, source_dir: str, dry_run: bool = False, assume_yes: bool = False):
        self.source_dir = Path(source_dir).resolve()
        # The source_dir the user points us at IS the "Photo Archives" root
        # (e.g. ".../Backup 4TB/Photo Archives") - organize directly inside
        # it as Year/Month/... rather than nesting another "Photo Archives"
        # subfolder underneath itself.
        self.dest_root = self.source_dir
        self.dry_run = dry_run
        self.assume_yes = assume_yes
        self.geocoder = OfflineGeocoder()
        self.exif = EXIFExtractor()
        self.hash_cache = self._load_hash_cache()

        self.extract_root: Optional[Path] = None
        # archive_path -> {'members': [media file dicts], 'other_files': int}
        self.archive_registry: Dict[Path, dict] = {}

        self.unique_files: List[dict] = []
        self.duplicates: List[dict] = []
        self.moves: List[dict] = []
        self.deletions: List[dict] = []
        self.archive_deletions: List[Path] = []
        self.archive_kept: List[Tuple[Path, str]] = []
        self.errors: List[str] = []

        self.assigned_destinations = set()

    def _is_scannable(self, p: Path) -> bool:
        """Excludes already-organized output (top-level Year/ and NO EXIF/
        folders directly under source_dir) and any (including stale,
        leftover-from-a-crash) temp extraction directories from being treated
        as source material on a re-run."""
        if any(part.startswith('.photo_org_tmp_') for part in p.parts):
            return False
        try:
            rel_parts = p.relative_to(self.source_dir).parts
        except ValueError:
            return True
        if rel_parts:
            first = rel_parts[0]
            if first == NO_EXIF_DIR or (len(first) == 4 and first.isdigit()):
                return False
        return True

    @staticmethod
    def _find_unzip() -> Optional[str]:
        for candidate in ["/usr/bin/unzip", "/opt/homebrew/bin/unzip", "/usr/local/bin/unzip"]:
            if os.path.exists(candidate):
                return candidate
        result = subprocess.run(["which", "unzip"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None

    @staticmethod
    def _is_archive(p: Path) -> bool:
        name = p.name.lower()
        return name.endswith('.zip') or name.endswith(TAR_SUFFIXES)

    @staticmethod
    def _is_macos_sidecar(p: Path) -> bool:
        """AppleDouble resource-fork sidecars (._Foo.jpg) and __MACOSX/
        folders that macOS's tar/zip tools embed alongside real files -
        these carry a real media extension but are metadata, not photos."""
        return p.name.startswith('._') or '__MACOSX' in p.parts

    @staticmethod
    def _detect_extensionless_ext(p: Path) -> Optional[str]:
        """If p has no extension at all, sniffs its content for a
        recognizable media signature. Read-only - never renames anything,
        so it's safe to call during --dry-run. The detected extension (if
        any) is appended to the computed destination filename in
        build_destination(); the actual on-disk rename then happens
        naturally as part of the normal move-to-destination operation,
        which itself only ever runs for a real (non-dry-run) execution."""
        if p.suffix:
            return None
        return sniff_media_extension(p)

    def _extract_archive(self, archive_path: Path, extract_dir: Path,
                          unzip_bin: Optional[str]) -> Optional[str]:
        """Extracts a .zip or tar-family archive into extract_dir. Returns a
        warning string on any problem (extraction still proceeds with
        whatever was recovered); returns None on a clean extraction."""
        name = archive_path.name.lower()

        if name.endswith(TAR_SUFFIXES):
            try:
                with tarfile.open(archive_path, 'r:*') as tf:
                    tf.extractall(extract_dir, filter='data')
                return None
            except Exception as e:
                return f"tar extraction error: {e}"

        # .zip
        if unzip_bin:
            # The system 'unzip' (Info-ZIP) correctly recompensates for a
            # known offset-calculation issue in some large (>4GB) ZIP64
            # archives that Python's zipfile module misreads as overlapping
            # entries and refuses to extract at all. Prefer it; fall back to
            # zipfile only if it's unavailable.
            try:
                result = subprocess.run(
                    [unzip_bin, '-q', '-o', str(archive_path), '-d', str(extract_dir)],
                    capture_output=True, text=True, timeout=1800
                )
                if result.returncode != 0 or result.stderr.strip():
                    return (result.stderr.strip() or f"unzip exited {result.returncode}")[:400]
                return None
            except subprocess.TimeoutExpired:
                return "unzip timed out after 30 minutes"
            except Exception as e:
                return f"unzip failed to run: {e}"
        else:
            try:
                with zipfile.ZipFile(archive_path, 'r') as zf:
                    zf.extractall(extract_dir)
                return None
            except Exception as e:
                return f"zipfile extraction error: {e}"

    # ── Hash cache (persistent, keyed only by real on-disk paths) ───────────
    def _cache_file(self) -> Path:
        return self.source_dir / ".photo_organizer_hash_cache.json"

    def _load_hash_cache(self) -> Dict[str, str]:
        cf = self._cache_file()
        if cf.exists():
            try:
                with open(cf, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save_hash_cache(self):
        if self.dry_run:
            return
        try:
            with open(self._cache_file(), 'w') as f:
                json.dump(self.hash_cache, f, indent=2)
        except IOError:
            pass

    def compute_hash(self, filepath: Path, cache_key: Optional[str] = None) -> str:
        """SHA256 hash of file content. Only cached when cache_key is a stable,
        persistent path (temp-extracted zip members must NOT be cached)."""
        if cache_key and cache_key in self.hash_cache:
            return self.hash_cache[cache_key]
        sha = hashlib.sha256()
        try:
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    sha.update(chunk)
            h = sha.hexdigest()
            if cache_key:
                self.hash_cache[cache_key] = h
            return h
        except IOError as e:
            self.errors.append(f"Hash failed for {filepath}: {e}")
            return ""

    # ── Scanning ──────────────────────────────────────────────────────────
    def scan_files(self):
        print(f"Scanning: {self.source_dir}")

        archive_files = [p for p in self.source_dir.rglob('*')
                          if p.is_file() and self._is_scannable(p) and self._is_archive(p)]
        all_media: List[dict] = []  # {'path', 'source_archive'}

        if archive_files:
            # Extract on the SAME volume as the source (never the system /tmp,
            # which may be a small boot-drive partition) - this also makes the
            # later move-to-destination a fast same-filesystem rename instead
            # of a slow cross-device copy.
            self.extract_root = Path(tempfile.mkdtemp(prefix=".photo_org_tmp_",
                                                        dir=str(self.source_dir)))
            print(f"Found {len(archive_files)} archive(s) - extracting to {self.extract_root}...")
            unzip_bin = self._find_unzip()
            for i, archive_path in enumerate(archive_files):
                extract_dir = self.extract_root / f"{i}_{archive_path.stem}"
                extract_dir.mkdir(parents=True, exist_ok=True)
                reg = self.archive_registry.setdefault(archive_path, {'members': [], 'other_files': 0})
                warnings = self._extract_archive(archive_path, extract_dir, unzip_bin)

                for p in extract_dir.rglob('*'):
                    if not p.is_file():
                        continue
                    if self._is_macos_sidecar(p):
                        continue
                    detected_ext = self._detect_extensionless_ext(p)
                    if p.suffix.lower() in MEDIA_EXTS or detected_ext:
                        all_media.append({'path': p, 'source_archive': archive_path,
                                           'detected_ext': detected_ext})
                    else:
                        reg['other_files'] += 1

                if warnings:
                    reg['extraction_warnings'] = warnings
                    self.errors.append(
                        f"Extraction warnings for {archive_path} (kept for safety, "
                        f"recovered contents still processed): {warnings}")
                print(f"  Extracted: {archive_path.name}" +
                      (" (with warnings - archive will be kept, not deleted)" if warnings else ""))

        # Loose media files (exclude the organized dest root and any temp extraction dirs)
        for p in self.source_dir.rglob('*'):
            if not p.is_file() or not self._is_scannable(p) or self._is_macos_sidecar(p):
                continue
            detected_ext = self._detect_extensionless_ext(p)
            if p.suffix.lower() in MEDIA_EXTS or detected_ext:
                all_media.append({'path': p, 'source_archive': None, 'detected_ext': detected_ext})

        print(f"Found {len(all_media)} media file(s) total (loose + inside archives)")

        # Stable ordering: prefer non-"copy" names, then loose files over zip
        # members, then shorter paths, then alphabetical - this only affects
        # which of several byte-identical copies is treated as "the original".
        def sort_key(item):
            p = item['path']
            stem = p.stem.lower()
            is_copy = '_copy' in stem or stem.endswith(' copy') or 'copy' in re.split(r'[ _]', stem)
            return (is_copy, item['source_archive'] is not None, len(p.parts), str(p).lower())

        all_media.sort(key=sort_key)

        print("Hashing files for deduplication...")
        hash_map: Dict[str, dict] = {}  # hash -> winning item
        for i, item in enumerate(all_media, 1):
            if i % 200 == 0 or i == len(all_media):
                print(f"  Hashed {i}/{len(all_media)}")
            path = item['path']
            cache_key = None if item['source_archive'] else str(path)
            h = self.compute_hash(path, cache_key=cache_key)
            if not h:
                if item['source_archive']:
                    self.archive_registry[item['source_archive']]['members'].append(
                        {'path': path, 'status': 'error'})
                continue

            if h in hash_map:
                winner = hash_map[h]
                self.duplicates.append({'path': path, 'original': winner['path'], 'hash': h})
                if item['source_archive']:
                    self.archive_registry[item['source_archive']]['members'].append(
                        {'path': path, 'status': 'duplicate', 'hash': h})
            else:
                hash_map[h] = item
                if item['source_archive']:
                    self.archive_registry[item['source_archive']]['members'].append(
                        {'path': path, 'status': 'pending', 'hash': h})

        self._save_hash_cache()
        print(f"Unique files: {len(hash_map)}, Duplicates: {len(self.duplicates)}")

        print("Extracting EXIF data...")
        coords_needed: List[Tuple[float, float]] = []
        for i, (h, item) in enumerate(hash_map.items(), 1):
            if i % 50 == 0:
                print(f"  Processed {i}/{len(hash_map)}")
            path = item['path']
            try:
                exif = self.exif.extract(path)
            except Exception as e:
                self.errors.append(f"EXIF error on {path}: {e}")
                exif = {"date": None, "lat": None, "lon": None}

            if exif['lat'] is not None and exif['lon'] is not None:
                coords_needed.append((exif['lat'], exif['lon']))

            self.unique_files.append({
                'path': path, 'hash': h, 'date': exif['date'],
                'lat': exif['lat'], 'lon': exif['lon'],
                'source_archive': item['source_archive'],
                'detected_ext': item.get('detected_ext'),
            })

        self.geocoder.resolve_all(coords_needed)
        for info in self.unique_files:
            info['town'], info['country'], info['is_uk'] = None, None, False
            if info['lat'] is not None and info['lon'] is not None:
                geo = self.geocoder.get(info['lat'], info['lon'])
                if geo:
                    info['town'], info['country'], info['is_uk'] = (
                        geo['town'], geo['country'], geo['is_uk'])

    # ── Destination building ─────────────────────────────────────────────
    def build_destination(self, info: dict) -> Path:
        date, town, is_uk = info['date'], info['town'], info['is_uk']
        filename = info['path'].name + (info.get('detected_ext') or '')

        if date:
            base = self.dest_root / date.strftime('%Y') / date.strftime('%B')
            if town:
                dest = (base / self._sanitize(town) / filename if is_uk else
                         base / self._sanitize(info['country'] or "Unknown")
                         / self._sanitize(town) / filename)
            else:
                dest = base / filename
        else:
            base = self.dest_root / NO_EXIF_DIR
            dest = base / self._sanitize(town) / filename if town else base / filename

        return self._resolve_collision(dest, info['hash'])

    @staticmethod
    def _sanitize(name: str) -> str:
        if not name:
            return "Unknown"
        name = re.sub(r'[<>:"|?*/\\]', '_', name).strip().title()
        return name or "Unknown"

    def _resolve_collision(self, dest: Path, file_hash: str) -> Path:
        if not dest.exists() and dest not in self.assigned_destinations:
            self.assigned_destinations.add(dest)
            return dest
        suffix = file_hash[:8] if file_hash else "00000000"
        new_dest = dest.parent / f"{dest.stem}_{suffix}{dest.suffix}"
        if new_dest == dest:
            new_dest = dest.parent / f"{dest.stem}_{suffix}_{len(self.assigned_destinations)}{dest.suffix}"
        return self._resolve_collision(new_dest, file_hash)

    # ── Planning (no disk writes - safe to run always) ───────────────────
    def plan(self):
        hash_to_destination: Dict[str, Path] = {}

        for info in self.unique_files:
            src = info['path']
            member = self._zip_member(info)
            dest = self.build_destination(info)

            if not info['source_archive'] and src.resolve() == dest.resolve():
                hash_to_destination[info['hash']] = dest
                if member:
                    member['status'] = 'moved'
                    member['destination'] = dest
                continue

            if dest.exists():
                existing_hash = self.compute_hash(dest, cache_key=str(dest))
                if existing_hash == info['hash']:
                    self.duplicates.append({'path': src, 'original': dest, 'hash': info['hash']})
                    hash_to_destination[info['hash']] = dest
                    if member:
                        member['status'] = 'duplicate'
                        member['destination'] = dest
                    continue
                dest = self._resolve_collision(dest, info['hash'])

            self.moves.append({
                'source': src, 'destination': dest, 'date': info['date'],
                'town': info['town'], 'country': info['country'], 'is_uk': info['is_uk'],
                'source_archive': info['source_archive'],
            })
            hash_to_destination[info['hash']] = dest
            if member:
                member['status'] = 'moved'
                member['destination'] = dest

        for dup in self.duplicates:
            self.deletions.append(dup)

        # Duplicates identified purely from an in-scan hash collision (i.e. two
        # files with identical content, neither of which is "the unique file"
        # that went through the loop above) never got a destination assigned
        # above. Backfill it from the winning file's resolved destination so
        # zip-deletion safety checks can verify their content survived.
        for reg in self.archive_registry.values():
            for m in reg['members']:
                if m['status'] == 'duplicate' and 'destination' not in m:
                    m['destination'] = hash_to_destination.get(m.get('hash'))

        # Decide which zips are safe to delete
        for archive_path, reg in self.archive_registry.items():
            if reg.get('extraction_warnings'):
                self.archive_kept.append(
                    (archive_path, f"extraction had warnings: {reg['extraction_warnings'][:150]}"))
                continue
            if not reg['members']:
                self.archive_kept.append((archive_path, "contained no media files"))
                continue
            bad = [m for m in reg['members']
                   if m['status'] not in ('moved', 'duplicate') or not m.get('destination')]
            if bad:
                self.archive_kept.append((archive_path, f"{len(bad)} member(s) unresolved/errored"))
            else:
                self.archive_deletions.append(archive_path)

    def _zip_member(self, info: dict) -> Optional[dict]:
        if not info['source_archive']:
            return None
        for m in self.archive_registry[info['source_archive']]['members']:
            if m['path'] == info['path']:
                return m
        return None

    # ── Reporting (always runs, before any execution) ────────────────────
    def write_report(self) -> Path:
        cats = {'date_loc': 0, 'date_only': 0, 'loc_only': 0, 'no_exif': 0}
        moved_hashes = {m['destination'] for m in self.moves}
        for f in self.unique_files:
            if f['date'] and f['town']:
                cats['date_loc'] += 1
            elif f['date']:
                cats['date_only'] += 1
            elif f['town']:
                cats['loc_only'] += 1
            else:
                cats['no_exif'] += 1

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        report_path = self.source_dir / f"photo_organizer_report_{ts}.txt"

        lines = []
        lines.append("=" * 70)
        lines.append(f"PHOTO ORGANIZER {'DRY-RUN ' if self.dry_run else ''}AUDIT REPORT")
        lines.append(f"Generated: {datetime.now().isoformat()}")
        lines.append(f"Source: {self.source_dir}")
        lines.append("=" * 70)
        lines.append("")
        lines.append("SUMMARY")
        lines.append(f"  Unique files:              {len(self.unique_files)}")
        lines.append(f"  Duplicate files (deleted): {len(self.deletions)}")
        lines.append(f"  Moves planned:             {len(self.moves)}")
        lines.append(f"  Archives found:            {len(self.archive_registry)}")
        lines.append(f"  Archives to delete:        {len(self.archive_deletions)}")
        lines.append(f"  Archives KEPT:             {len(self.archive_kept)}")
        lines.append(f"  Errors:                    {len(self.errors)}")
        lines.append("")
        lines.append("CATEGORIES (unique files)")
        lines.append(f"  Date + Town (UK):          {cats['date_loc']}")
        lines.append(f"  Date only:                 {cats['date_only']}")
        lines.append(f"  Location only (no date):   {cats['loc_only']}")
        lines.append(f"  No EXIF at all:            {cats['no_exif']}")
        lines.append("")

        lines.append("-" * 70)
        lines.append(f"PLANNED MOVES ({len(self.moves)})")
        lines.append("-" * 70)
        for m in self.moves:
            lines.append(f"{m['source']} -> {m['destination']}")

        lines.append("")
        lines.append("-" * 70)
        lines.append(f"DUPLICATES TO DELETE ({len(self.deletions)})")
        lines.append("-" * 70)
        for d in self.deletions:
            lines.append(f"{d['path']}  [duplicate of {d['original']}]")

        lines.append("")
        lines.append("-" * 70)
        lines.append(f"ARCHIVES TO DELETE AFTER VERIFIED EXTRACTION ({len(self.archive_deletions)})")
        lines.append("-" * 70)
        for z in self.archive_deletions:
            lines.append(str(z))

        lines.append("")
        lines.append("-" * 70)
        lines.append(f"ARCHIVES KEPT (not fully resolved) ({len(self.archive_kept)})")
        lines.append("-" * 70)
        for z, reason in self.archive_kept:
            lines.append(f"{z}  [{reason}]")

        if self.errors:
            lines.append("")
            lines.append("-" * 70)
            lines.append(f"ERRORS ({len(self.errors)})")
            lines.append("-" * 70)
            lines.extend(self.errors)

        report_path.write_text("\n".join(lines), encoding='utf-8')
        self.report_path = report_path

        # Console summary
        print("\n" + "=" * 60)
        print("AUDIT REPORT" + (" (DRY RUN)" if self.dry_run else ""))
        print("=" * 60)
        print(f"  Unique files:              {len(self.unique_files)}")
        print(f"  Duplicates to delete:      {len(self.deletions)}")
        print(f"  Moves planned:             {len(self.moves)}")
        print(f"  Archives to delete:        {len(self.archive_deletions)}")
        print(f"  Archives kept (unresolved):{len(self.archive_kept)}")
        print(f"  Errors:                    {len(self.errors)}")
        print(f"\nFull untruncated report written to:\n  {report_path}")
        print("=" * 60)
        return report_path

    # ── Execution ─────────────────────────────────────────────────────────
    def execute(self):
        pre_execution_error_count = len(self.errors)
        print("\nExecuting moves...")
        for i, m in enumerate(self.moves, 1):
            if i % 500 == 0 or i == len(self.moves):
                print(f"  Moved {i}/{len(self.moves)}")
            try:
                m['destination'].parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(m['source']), str(m['destination']))
            except Exception as e:
                self.errors.append(f"Move failed: {m['source']} -> {m['destination']}: {e}")

        print("Deleting proven duplicates...")
        for i, d in enumerate(self.deletions, 1):
            if i % 500 == 0 or i == len(self.deletions):
                print(f"  Deleted {i}/{len(self.deletions)}")
            try:
                if d['path'].exists():
                    d['path'].unlink()
            except Exception as e:
                self.errors.append(f"Delete failed: {d['path']}: {e}")

        print(f"Verifying and deleting {len(self.archive_deletions)} fully-resolved archive(s)...")
        for i, archive_path in enumerate(self.archive_deletions, 1):
            print(f"  [{i}/{len(self.archive_deletions)}] {archive_path.name}")
            reg = self.archive_registry[archive_path]
            ok = True
            for m in reg['members']:
                dest = m.get('destination')
                if not dest or not dest.exists():
                    ok = False
                    self.errors.append(
                        f"Refusing to delete {archive_path}: expected file missing at {dest}")
            if ok:
                try:
                    archive_path.unlink()
                except Exception as e:
                    self.errors.append(f"Archive delete failed: {archive_path}: {e}")
            else:
                self.errors.append(f"Archive kept due to verification failure: {archive_path}")

        print("Cleaning up empty directories...")
        self._cleanup_empty_dirs()

        if self.extract_root and self.extract_root.exists():
            shutil.rmtree(self.extract_root, ignore_errors=True)

        new_errors = self.errors[pre_execution_error_count:]
        if hasattr(self, 'report_path'):
            try:
                with open(self.report_path, 'a', encoding='utf-8') as f:
                    f.write("\n\n" + "-" * 70 + "\n")
                    f.write(f"EXECUTION LOG ({datetime.now().isoformat()})\n")
                    f.write("-" * 70 + "\n")
                    f.write(f"Moves attempted: {len(self.moves)}\n")
                    f.write(f"Duplicates deleted: {len(self.deletions)}\n")
                    f.write(f"Archives deleted: {len([z for z in self.archive_deletions if not z.exists()])}\n")
                    if new_errors:
                        f.write(f"\nEXECUTION ERRORS ({len(new_errors)}):\n")
                        f.write("\n".join(new_errors) + "\n")
                    else:
                        f.write("\nNo errors during execution.\n")
            except IOError:
                pass

        if self.errors:
            print(f"\nCompleted with {len(self.errors)} error(s):")
            for e in new_errors:
                print(f"  {e}")
            print(f"(Full log appended to {getattr(self, 'report_path', 'the report file')})")
        else:
            print("\nOrganization complete - no errors.")

    def cleanup_temp(self):
        if self.extract_root and self.extract_root.exists():
            shutil.rmtree(self.extract_root, ignore_errors=True)

    def _cleanup_empty_dirs(self):
        pass_num = 0
        changed = True
        while changed:
            pass_num += 1
            changed = False
            removed_this_pass = 0
            for dirpath in sorted(self.source_dir.rglob('*'), reverse=True):
                if dirpath.is_dir() and dirpath != self.dest_root and dirpath.exists():
                    try:
                        if not any(dirpath.iterdir()):
                            dirpath.rmdir()
                            changed = True
                            removed_this_pass += 1
                    except Exception:
                        pass
            print(f"  Pass {pass_num}: removed {removed_this_pass} empty director{'y' if removed_this_pass == 1 else 'ies'}")

    def run(self):
        self.scan_files()
        self.plan()
        self.write_report()

        if self.dry_run:
            print("\nDRY RUN COMPLETE - no files were moved or deleted.")
            self.cleanup_temp()
            return

        if not self.assume_yes:
            print(f"\nThis will move {len(self.moves)} file(s), permanently delete "
                  f"{len(self.deletions)} duplicate file(s), and permanently delete "
                  f"{len(self.archive_deletions)} archive(s).")
            answer = input("Type 'yes' to proceed: ").strip().lower()
            if answer != 'yes':
                print("Aborted - nothing was changed.")
                self.cleanup_temp()
                return

        self.execute()


# ── Entry point ───────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='De-duplicate and organize a photo/video archive by EXIF date and location.',
        epilog='Example: %(prog)s "/Volumes/Backup 4TB/Photo Archives" --dry-run'
    )
    parser.add_argument('source_dir', help='Root directory containing photos/videos/zips')
    parser.add_argument('--dry-run', action='store_true',
                         help='Plan and write the audit report only; change nothing')
    parser.add_argument('--yes', action='store_true',
                         help='Skip the interactive confirmation prompt for a real run')
    args = parser.parse_args()

    if not HAS_RG:
        print("NOTE: 'reverse_geocoder' is not installed - no photos will be "
              "matched to a Town/Country even if they have GPS data. "
              "Install with: pip install reverse_geocoder\n", file=sys.stderr)

    organizer = MediaOrganizer(args.source_dir, dry_run=args.dry_run, assume_yes=args.yes)
    organizer.run()


if __name__ == '__main__':
    main()
