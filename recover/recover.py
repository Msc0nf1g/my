#!/usr/bin/env python3
"""Восстановление файлов с отформатированной флешки (поиск по сигнатурам).

Скрипт только ЧИТАЕТ флешку и ничего на неё не пишет. Он просматривает
её побайтно, находит начала известных форматов (фото, видео, документы,
архивы), определяет их длину по внутренней структуре и сохраняет копии
в указанную папку на другом диске.

Запуск без аргументов — интерактивный режим с вопросами.
    python recover.py
    python recover.py image E: D:\\flash.img      # копия флешки в файл
    python recover.py carve D:\\flash.img D:\\out  # поиск файлов в копии
    python recover.py carve E: D:\\out             # поиск прямо на флешке

Нужен только Python 3.8+ из python.org, ничего доустанавливать не надо.
"""

import argparse
import csv
import ntpath
import os
import re
import shutil
import struct
import sys
import time
import zlib

SECTOR = 512
KB = 1024
MB = 1024 * KB
GB = 1024 * MB

CACHE_SIZE = 1 * MB      # cache block for small structural reads
SCAN_CHUNK = 8 * MB      # how much of the drive is scanned per step
COPY_CHUNK = 4 * MB


# --------------------------------------------------------------------------
# Reading the drive
# --------------------------------------------------------------------------

def _device_size(f):
    try:
        end = f.seek(0, 2)
        f.seek(0)
        if end > 0:
            return end
    except OSError:
        pass
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes
            handle = msvcrt.get_osfhandle(f.fileno())
            length = ctypes.c_longlong(0)
            returned = wintypes.DWORD(0)
            IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
            ok = ctypes.windll.kernel32.DeviceIoControl(
                wintypes.HANDLE(handle), IOCTL_DISK_GET_LENGTH_INFO, None, 0,
                ctypes.byref(length), ctypes.sizeof(length),
                ctypes.byref(returned), None)
            if ok and length.value > 0:
                return length.value
        except Exception:
            pass
    return None


class Reader:
    """Read-only random access to a drive or image.

    Raw Windows devices only accept sector-aligned reads, so every read is
    widened to sector boundaries. Unreadable sectors are replaced by zeros.
    """

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb", buffering=0)
        self.size = _device_size(self.f)
        self.bad_sectors = 0
        self._cache_off = -1
        self._cache = b""

    def close(self):
        self.f.close()

    def _read_full(self, n):
        out = bytearray()
        while len(out) < n:
            b = self.f.read(n - len(out))
            if not b:
                break
            out += b
        return bytes(out)

    def _raw(self, off, n):
        try:
            self.f.seek(off)
            return self._read_full(n)
        except OSError:
            return self._raw_slow(off, n)

    def _raw_slow(self, off, n):
        out = bytearray()
        for s in range(off, off + n, SECTOR):
            if self.size is not None and s >= self.size:
                break
            try:
                self.f.seek(s)
                b = self.f.read(SECTOR)
            except OSError:
                if self.size is None:
                    break
                self.bad_sectors += 1
                b = bytes(SECTOR)
            if not b:
                break
            out += b
            if len(b) < SECTOR:
                break
        return bytes(out)

    def read_at(self, off, n):
        if n <= 0 or off < 0:
            return b""
        if self.size is not None:
            if off >= self.size:
                return b""
            n = min(n, self.size - off)
        base = off - off % CACHE_SIZE
        if off + n <= base + CACHE_SIZE:
            if base != self._cache_off:
                self._cache = self._raw(base, CACHE_SIZE)
                self._cache_off = base
            return self._cache[off - base:off - base + n]
        start = off - off % SECTOR
        end = -(-(off + n) // SECTOR) * SECTOR
        data = self._raw(start, end - start)
        return data[off - start:off - start + n]


def find_forward(r, start, pattern, end):
    """Absolute position of pattern in [start, end) or -1."""
    overlap = len(pattern) - 1
    p = start
    while p < end:
        d = r.read_at(p, min(4 * MB, end - p + overlap))
        if len(d) < len(pattern):
            return -1
        i = d.find(pattern)
        if i >= 0:
            return p + i if p + i < end else -1
        p += len(d) - overlap
    return -1


# --------------------------------------------------------------------------
# Format parsers. Each returns (length, extension) or None.
# --------------------------------------------------------------------------

def _skip_jpeg_entropy(r, p, end):
    """Position of the first real marker after entropy-coded data."""
    while p < end:
        data = r.read_at(p, MB)
        if len(data) < 2:
            return -1
        i = 0
        while True:
            i = data.find(b"\xff", i)
            if i < 0 or i + 1 >= len(data):
                break
            nb = data[i + 1]
            if nb != 0x00 and not (0xD0 <= nb <= 0xD7):
                return p + i
            i += 1
        p += len(data) - 1
    return -1


def carve_jpeg(r, pos, limit):
    end = pos + limit
    p = pos + 2
    seen_sos = False
    while p < end:
        b = r.read_at(p, 4)
        if len(b) < 2 or b[0] != 0xFF:
            return None
        m = b[1]
        if m == 0xFF:
            p += 1
            continue
        if m == 0x00:
            return None
        if m == 0xD9:
            return (p + 2 - pos, "jpg") if seen_sos else None
        if 0xD0 <= m <= 0xD7 or m == 0x01:
            p += 2
            continue
        if len(b) < 4:
            return None
        seglen = (b[2] << 8) | b[3]
        if seglen < 2:
            return None
        p += 2 + seglen
        if m == 0xDA:
            seen_sos = True
            p = _skip_jpeg_entropy(r, p, end)
            if p < 0:
                return None
    return None


def carve_png(r, pos, limit):
    p = pos + 8
    first = True
    while p - pos < limit:
        h = r.read_at(p, 8)
        if len(h) < 8:
            return None
        length, typ = struct.unpack(">I4s", h)
        if not typ.isalpha() or (first and typ != b"IHDR"):
            return None
        first = False
        p += 12 + length
        if typ == b"IEND":
            return (p - pos, "png") if p - pos <= limit else None
    return None


def carve_gif(r, pos, limit):
    h = r.read_at(pos, 13)
    if len(h) < 13:
        return None
    end = pos + limit
    p = pos + 13
    if h[10] & 0x80:
        p += 3 * (2 << (h[10] & 7))
    while p < end:
        b = r.read_at(p, 1)
        if not b:
            return None
        t = b[0]
        if t == 0x3B:
            return p + 1 - pos, "gif"
        if t == 0x21:
            p += 2
        elif t == 0x2C:
            d = r.read_at(p, 10)
            if len(d) < 10:
                return None
            p += 10
            if d[9] & 0x80:
                p += 3 * (2 << (d[9] & 7))
            p += 1
        else:
            return None
        while True:
            s = r.read_at(p, 1)
            if not s:
                return None
            p += 1 + s[0]
            if s[0] == 0:
                break
            if p >= end:
                return None
    return None


def carve_bmp(r, pos, limit):
    h = r.read_at(pos, 30)
    if len(h) < 30:
        return None
    size, reserved, data_off, dib = struct.unpack("<I4sII", h[2:18])
    if reserved != b"\0\0\0\0" or dib not in (12, 40, 52, 56, 108, 124):
        return None
    if not 14 + dib <= data_off < size <= limit:
        return None
    if dib == 12:
        planes, bpp = struct.unpack("<HH", h[22:26])
    else:
        planes, bpp = struct.unpack("<HH", h[26:30])
    if planes != 1 or bpp not in (1, 4, 8, 16, 24, 32):
        return None
    return size, "bmp"


_PDF_CONTINUES = re.compile(rb"\d+\s+\d+\s+obj|xref|trailer|startxref")


def carve_pdf(r, pos, limit):
    if not re.match(rb"%PDF-[12]\.\d", r.read_at(pos, 8)):
        return None
    end = pos + limit
    p = pos
    while True:
        e = find_forward(r, p, b"%%EOF", end)
        if e < 0:
            return None
        q = e + 5
        tail = r.read_at(q, 2)
        if tail[:2] == b"\r\n":
            q += 2
        elif tail[:1] in (b"\r", b"\n"):
            q += 1
        # Incremental updates append more objects after %%EOF.
        nxt = r.read_at(q, 64).lstrip(b" \t\r\n")
        if _PDF_CONTINUES.match(nxt):
            p = q
            continue
        return q - pos, "pdf"


def _zip_kind(r, pos, cd_start, cd_size):
    first = r.read_at(pos, 120)
    if b"mimetypeapplication/vnd.oasis.opendocument.text" in first:
        return "odt"
    if b"mimetypeapplication/vnd.oasis.opendocument.spreadsheet" in first:
        return "ods"
    if b"mimetypeapplication/vnd.oasis.opendocument.presentation" in first:
        return "odp"
    if b"mimetypeapplication/epub+zip" in first:
        return "epub"
    names = r.read_at(cd_start, min(cd_size, 4 * MB))
    if b"word/document" in names:
        return "docx"
    if b"xl/workbook" in names:
        return "xlsx"
    if b"ppt/presentation" in names:
        return "pptx"
    return "zip"


def carve_zip(r, pos, limit):
    h = r.read_at(pos, 30)
    if len(h) < 30:
        return None
    version, = struct.unpack("<H", h[4:6])
    name_len, = struct.unpack("<H", h[26:28])
    if version > 100 or name_len == 0:
        return None
    end = pos + limit
    p = pos + 30
    while True:
        e = find_forward(r, p, b"PK\x05\x06", end)
        if e < 0:
            return None
        rec = r.read_at(e, 22)
        if len(rec) < 22:
            return None
        (_, disk, _, _, entries, cd_size, cd_off,
         comment_len) = struct.unpack("<4sHHHHIIH", rec)
        start = None
        cd_start = None
        if 0xFFFFFFFF in (cd_size, cd_off) or entries == 0xFFFF:
            rec64_pos = e - 20 - 56
            loc = r.read_at(e - 20, 20)
            rec64 = r.read_at(rec64_pos, 56)
            if loc[:4] == b"PK\x06\x07" and rec64[:4] == b"PK\x06\x06":
                cd_size, cd_off = struct.unpack("<QQ", rec64[40:56])
                start = rec64_pos - cd_off - cd_size
        elif disk == 0:
            start = e - cd_off - cd_size
        # The central directory points back to the archive start: this
        # rejects end records that belong to some other archive.
        if start == pos:
            cd_start = pos + cd_off
            return e + 22 + comment_len - pos, _zip_kind(r, pos, cd_start, cd_size)
        p = e + 4


def carve_rar4(r, pos, limit):
    end = pos + limit
    p = pos + 7
    first = True
    saw_file = False
    while p < end:
        h = r.read_at(p, 11)
        if len(h) < 7:
            break
        _, typ, flags, hsize = struct.unpack("<HBHH", h[:7])
        if not 0x72 <= typ <= 0x7B or hsize < 7 or (first and typ != 0x73):
            break
        first = False
        add = 0
        if flags & 0x8000 or typ in (0x74, 0x7A):
            if len(h) < 11:
                break
            add, = struct.unpack("<I", h[7:11])
            if typ in (0x74, 0x7A) and flags & 0x100:
                hi = r.read_at(p + 32, 4)
                if len(hi) < 4:
                    break
                add += struct.unpack("<I", hi)[0] << 32
        p += hsize + add
        if typ == 0x74:
            saw_file = True
        if typ == 0x7B:
            return (p - pos, "rar") if p - pos <= limit else None
    return (p - pos, "rar") if saw_file and p - pos <= limit else None


def _vint(b, i):
    value = 0
    shift = 0
    while i < len(b) and shift < 64:
        byte = b[i]
        value |= (byte & 0x7F) << shift
        i += 1
        if not byte & 0x80:
            return value, i
        shift += 7
    return None, i


def carve_rar5(r, pos, limit):
    end = pos + limit
    p = pos + 8
    first = True
    saw_file = False
    while p < end:
        h = r.read_at(p, 64)
        if len(h) < 7:
            break
        hsize, i = _vint(h, 4)
        hstart = i
        htype, i = _vint(h, i)
        hflags, i = _vint(h, i)
        if None in (hsize, htype, hflags) or not 1 <= htype <= 5:
            break
        if hsize == 0 or hsize > 2 * MB or (first and htype not in (1, 4)):
            break
        first = False
        if hflags & 1:
            _, i = _vint(h, i)
        dsize = 0
        if hflags & 2:
            dsize, i = _vint(h, i)
            if dsize is None:
                break
        p += hstart + hsize + dsize
        if htype in (2, 3):
            saw_file = True
        if htype == 5:
            return (p - pos, "rar") if p - pos <= limit else None
    return (p - pos, "rar") if saw_file and p - pos <= limit else None


def carve_7z(r, pos, limit):
    h = r.read_at(pos, 32)
    if len(h) < 32 or h[6] != 0:
        return None
    start_crc, = struct.unpack("<I", h[8:12])
    if zlib.crc32(h[12:32]) != start_crc:
        return None
    next_off, next_size, next_crc = struct.unpack("<QQI", h[12:32])
    if next_size == 0 or next_size > 64 * MB:
        return None
    total = 32 + next_off + next_size
    if total > limit:
        return None
    if zlib.crc32(r.read_at(pos + 32 + next_off, next_size)) != next_crc:
        return None
    return total, "7z"


_MP4_TOP = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid",
            b"meta", b"pdin", b"moof", b"mfra", b"styp", b"sidx", b"ssix",
            b"pnot", b"junk", b"prfl", b"emsg", b"prft", b"udta"}


def carve_mp4(r, pos, limit):
    h = r.read_at(pos, 12)
    if len(h) < 12:
        return None
    first_size, = struct.unpack(">I", h[:4])
    brand = h[8:12]
    if not 8 <= first_size <= 4096 or not all(32 <= c < 127 for c in brand):
        return None
    end = pos + limit
    p = pos
    seen = set()
    while p < end:
        b = r.read_at(p, 16)
        if len(b) < 8:
            break
        size, typ = struct.unpack(">I4s", b[:8])
        if typ not in _MP4_TOP:
            break
        if size == 1:
            if len(b) < 16:
                break
            size, = struct.unpack(">Q", b[8:16])
            if size < 16:
                break
        elif size < 8:
            break
        seen.add(typ)
        p += size
    if p - pos > limit or len(seen) < 2 or b"mdat" not in seen:
        return None
    brand = brand.lower()
    if brand == b"qt  ":
        ext = "mov"
    elif brand in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        ext = "heic"
    elif brand.startswith(b"3g"):
        ext = "3gp"
    elif brand.startswith(b"m4a"):
        ext = "m4a"
    else:
        ext = "mp4"
    return p - pos, ext


def carve_riff(r, pos, limit):
    h = r.read_at(pos, 12)
    if len(h) < 12:
        return None
    size, = struct.unpack("<I", h[4:8])
    form = h[8:12]
    ext = {b"AVI ": "avi", b"WAVE": "wav", b"WEBP": "webp"}.get(form)
    if ext is None or size < 4:
        return None
    total = 8 + size
    if form == b"AVI ":
        # Large AVI files continue in extra RIFF 'AVIX' chunks.
        while True:
            nh = r.read_at(pos + total, 12)
            if len(nh) < 12 or nh[:4] != b"RIFF" or nh[8:12] != b"AVIX":
                break
            total += 8 + struct.unpack("<I", nh[4:8])[0]
    return (total, ext) if total <= limit else None


def _ebml_size(b, i):
    if i >= len(b) or b[i] == 0:
        return None, i, False
    first = b[i]
    length = 9 - first.bit_length()
    if i + length > len(b):
        return None, i, False
    value = first & (0xFF >> length)
    for k in range(1, length):
        value = (value << 8) | b[i + k]
    return value, i + length, value == (1 << (7 * length)) - 1


def carve_mkv(r, pos, limit):
    h = r.read_at(pos, 256)
    size, i, unknown = _ebml_size(h, 4)
    if size is None or unknown or size > 200:
        return None
    header = h[i:i + size]
    if b"webm" in header:
        ext = "webm"
    elif b"matroska" in header:
        ext = "mkv"
    else:
        return None
    p = pos + i + size
    s = r.read_at(p, 16)
    if s[:4] != b"\x18\x53\x80\x67":
        return None
    seg_size, j, unknown = _ebml_size(s, 4)
    if seg_size is None or unknown:
        return None
    total = p + j + seg_size - pos
    return (total, ext) if total <= limit else None


_FREESECT = 0xFFFFFFFF


def carve_ole(r, pos, limit):
    """Old Office formats: .doc, .xls, .ppt (Compound File Binary)."""
    h = r.read_at(pos, 512)
    if len(h) < 512:
        return None
    _, major, byte_order, shift = struct.unpack("<HHHH", h[0x18:0x20])
    if byte_order != 0xFFFE or (major, shift) not in ((3, 9), (4, 12)):
        return None
    ssz = 1 << shift
    num_fat, dir_start = struct.unpack("<II", h[0x2C:0x34])
    difat_start, num_difat = struct.unpack("<II", h[0x44:0x4C])
    if num_fat == 0 or num_fat * (ssz // 4) * ssz > 64 * limit:
        return None

    def sector(n):
        return r.read_at(pos + (n + 1) * ssz, ssz)

    fat_sectors = list(struct.unpack("<109I", h[0x4C:0x200]))
    nxt = difat_start
    for _ in range(min(num_difat, 100000)):
        if nxt >= 0xFFFFFFFA:
            break
        d = sector(nxt)
        if len(d) < ssz:
            return None
        vals = struct.unpack("<%dI" % (ssz // 4), d)
        fat_sectors += vals[:-1]
        nxt = vals[-1]
    fat_sectors = [s for s in fat_sectors if s < 0xFFFFFFFA][:num_fat]
    if len(fat_sectors) != num_fat:
        return None
    fat = []
    for s in fat_sectors:
        d = sector(s)
        if len(d) < ssz:
            return None
        fat.extend(struct.unpack("<%dI" % (ssz // 4), d))
    last = -1
    for idx in range(len(fat) - 1, -1, -1):
        if fat[idx] != _FREESECT:
            last = idx
            break
    if last < 0:
        return None
    total = (last + 2) * ssz
    if total > limit:
        return None

    names = b""
    s = dir_start
    for _ in range(1000):
        if s >= len(fat):
            break
        names += sector(s)
        s = fat[s]
    ext = "ole"
    for key, kind in (("WordDocument", "doc"), ("Workbook", "xls"),
                      ("Book", "xls"), ("PowerPoint Document", "ppt"),
                      ("__substg1.0_", "msg")):
        if key.encode("utf-16-le") in names:
            ext = kind
            break
    return total, ext


# (signature, offset of signature from file start, parser, max size,
#  whether the found length is reliable enough to skip the inside of the file)
SIGNATURES = [
    (b"\xff\xd8\xff", 0, carve_jpeg, 200 * MB, False),
    (b"\x89PNG\r\n\x1a\n", 0, carve_png, 200 * MB, True),
    (b"GIF87a", 0, carve_gif, 100 * MB, True),
    (b"GIF89a", 0, carve_gif, 100 * MB, True),
    (b"BM", 0, carve_bmp, 200 * MB, True),
    (b"%PDF-", 0, carve_pdf, 2 * GB, False),
    (b"PK\x03\x04", 0, carve_zip, 16 * GB, True),
    (b"Rar!\x1a\x07\x00", 0, carve_rar4, 16 * GB, True),
    (b"Rar!\x1a\x07\x01\x00", 0, carve_rar5, 16 * GB, True),
    (b"7z\xbc\xaf\x27\x1c", 0, carve_7z, 16 * GB, True),
    (b"ftyp", 4, carve_mp4, 64 * GB, True),
    (b"RIFF", 0, carve_riff, 16 * GB, True),
    (b"\x1a\x45\xdf\xa3", 0, carve_mkv, 64 * GB, True),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", 0, carve_ole, 2 * GB, True),
]

MIN_FILE_SIZE = 32


# --------------------------------------------------------------------------
# Main operations
# --------------------------------------------------------------------------

def normalize_source(s):
    s = s.strip().strip('"').strip()
    if re.fullmatch(r"[A-Za-z]:?\\?", s):
        return "\\\\.\\%s:" % s[0].upper()
    if re.fullmatch(r"(?i)physicaldrive\d+", s):
        return "\\\\.\\" + s
    return s


def same_drive(source, dest):
    """True if source is a drive letter device and dest is on that drive."""
    m = re.fullmatch(r"\\\\[.?]\\([A-Za-z]):", source)
    d = re.match(r"([A-Za-z]):", ntpath.splitdrive(dest)[0])
    return bool(m and d and m.group(1).upper() == d.group(1).upper())


def _abs(path):
    return os.path.abspath(os.path.expanduser(path.strip().strip('"')))


def _fmt_mb(n):
    return "%.0f МБ" % (n / MB)


class Progress:
    def __init__(self, total):
        self.total = total
        self.start = time.time()
        self.last = 0

    def show(self, done, found, force=False):
        now = time.time()
        if not force and now - self.last < 1:
            return
        self.last = now
        speed = done / max(now - self.start, 0.001) / MB
        if self.total:
            pct = 100.0 * done / self.total
            left = (self.total - done) / MB / speed if speed > 0 else 0
            line = "  %5.1f%%  %s из %s  %.1f МБ/с  осталось ~%d мин" % (
                pct, _fmt_mb(done), _fmt_mb(self.total), speed, left // 60)
        else:
            line = "  %s  %.1f МБ/с" % (_fmt_mb(done), speed)
        if found is not None:
            line += "  найдено: %d" % found
        print("\r" + line.ljust(78), end="", flush=True)


def open_source(source):
    try:
        return Reader(source)
    except PermissionError:
        sys.exit("\nНет доступа к %s. Запустите скрипт от имени администратора "
                 "(через recover.bat)." % source)
    except FileNotFoundError:
        sys.exit("\nНе найдено: %s. Проверьте букву диска или путь." % source)
    except OSError as e:
        sys.exit("\nНе удалось открыть %s: %s" % (source, e))


def make_image(source, dest):
    dest = _abs(dest)
    if same_drive(source, dest):
        sys.exit("Нельзя сохранять копию на ту же флешку — это затрёт данные.")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    r = open_source(source)
    if r.size:
        free = shutil.disk_usage(os.path.dirname(dest) or ".").free
        if free < r.size:
            sys.exit("Мало места: нужно %s, свободно %s." % (
                _fmt_mb(r.size), _fmt_mb(free)))
    print("Копирую %s -> %s" % (source, dest))
    progress = Progress(r.size)
    done = 0
    with open(dest, "wb") as out:
        while r.size is None or done < r.size:
            d = r.read_at(done, CACHE_SIZE)
            if not d:
                break
            out.write(d)
            done += len(d)
            progress.show(done, None)
    progress.show(done, None, force=True)
    print()
    r.close()
    print("Готово: %s." % _fmt_mb(done))
    if r.bad_sectors:
        print("Нечитаемых секторов: %d (заменены нулями)." % r.bad_sectors)
    return dest


def carve(source, out_dir):
    out_dir = _abs(out_dir)
    if same_drive(source, out_dir):
        sys.exit("Нельзя сохранять файлы на ту же флешку — это затрёт данные.")
    os.makedirs(out_dir, exist_ok=True)
    r = open_source(source)
    print("Ищу файлы на %s, сохраняю в %s" % (source, out_dir))
    progress = Progress(r.size)
    counts = {}
    found = 0
    skip_until = 0
    report_path = os.path.join(out_dir, "report.csv")
    with open(report_path, "w", newline="", encoding="utf-8") as rep:
        writer = csv.writer(rep)
        writer.writerow(["file", "type", "offset", "size"])
        off = 0
        while r.size is None or off < r.size:
            data = r.read_at(off, SCAN_CHUNK + 64)
            body = min(len(data), SCAN_CHUNK)
            if body <= 0:
                break
            hits = []
            for sig, rel, parser, limit, reliable in SIGNATURES:
                i = data.find(sig, rel)
                while i >= 0:
                    start = i - rel
                    if start >= body:
                        break
                    if start % SECTOR == 0:
                        hits.append((start, parser, limit, reliable))
                    i = data.find(sig, i + 1)
            hits.sort(key=lambda h: h[0])
            for start, parser, limit, reliable in hits:
                pos = off + start
                if pos < skip_until:
                    continue
                try:
                    res = parser(r, pos, limit)
                except (struct.error, IndexError, ValueError, OverflowError,
                        MemoryError):
                    res = None
                if not res or res[0] < MIN_FILE_SIZE:
                    continue
                length, ext = res
                found += 1
                counts[ext] = counts.get(ext, 0) + 1
                folder = os.path.join(out_dir, ext)
                os.makedirs(folder, exist_ok=True)
                name = "%05d.%s" % (found, ext)
                _copy_out(r, pos, length, os.path.join(folder, name))
                writer.writerow([os.path.join(ext, name), ext, pos, length])
                if reliable:
                    skip_until = pos + length
                progress.show(off + start, found)
            off += body
            progress.show(off, found)
    progress.show(off, found, force=True)
    print()
    r.close()
    print("\nГотово. Найдено файлов: %d" % found)
    for ext in sorted(counts):
        print("  %-5s %d" % (ext, counts[ext]))
    if r.bad_sectors:
        print("Нечитаемых секторов: %d." % r.bad_sectors)
    print("Файлы лежат в %s (по папкам с типами), список — report.csv." % out_dir)
    return counts


def _copy_out(r, pos, length, path):
    with open(path, "wb") as f:
        done = 0
        while done < length:
            d = r.read_at(pos + done, min(COPY_CHUNK, length - done))
            if not d:
                break
            f.write(d)
            done += len(d)


# --------------------------------------------------------------------------
# Interactive mode for double-click start
# --------------------------------------------------------------------------

def _ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        sys.exit(1)


def interactive():
    print("=" * 70)
    print(" Восстановление файлов с флешки")
    print(" Скрипт только читает флешку. НЕ копируйте ничего на неё сами!")
    print("=" * 70)
    src = ""
    while not src:
        src = _ask("\nБуква флешки (например, E): ")
    source = normalize_source(src)
    out = ""
    while not out:
        out = _ask("Папка для результата на ДРУГОМ диске "
                   "(например, D:\\recovered): ")
        if same_drive(source, _abs(out)):
            print("Это та же флешка. Выберите папку на другом диске.")
            out = ""
    out = _abs(out)
    ans = _ask("Сначала сделать полную копию флешки в файл? "
               "Рекомендуется, если на диске хватает места. [Д/н]: ").lower()
    if ans in ("", "д", "да", "y", "yes", "l"):
        source = make_image(source, os.path.join(out, "flash.img"))
        print()
    carve(source, os.path.join(out, "files"))


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        interactive()
        return
    ap = argparse.ArgumentParser(description="Восстановление файлов по сигнатурам.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_img = sub.add_parser("image", help="скопировать флешку в файл-образ")
    p_img.add_argument("source", help="буква диска (E:), PhysicalDriveN или файл")
    p_img.add_argument("dest", help="файл образа на другом диске")
    p_carve = sub.add_parser("carve", help="найти и сохранить файлы")
    p_carve.add_argument("source", help="буква диска (E:), PhysicalDriveN или файл-образ")
    p_carve.add_argument("out", help="папка для найденных файлов на другом диске")
    args = ap.parse_args(argv)
    source = normalize_source(args.source)
    if args.cmd == "image":
        make_image(source, args.dest)
    else:
        carve(source, args.out)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nОстановлено.")
