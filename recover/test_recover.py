#!/usr/bin/env python3
"""Self-test: builds a fake "formatted flash drive" image, recovers files
from it and checks that every file comes back byte-for-byte.

    python3 recover/test_recover.py
"""

import hashlib
import io
import os
import random
import struct
import sys
import tempfile
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recover  # noqa: E402

rnd = random.Random(1234)


def jpeg(n, progressive=False):
    def seg(m, payload):
        return b"\xff" + bytes([m]) + struct.pack(">H", len(payload) + 2) + payload

    def entropy(k):
        data = rnd.randbytes(k).replace(b"\xff", b"\xff\x00")
        cut = len(data) // 2
        while data[cut - 1] == 0xFF:
            cut += 1
        return data[:cut] + b"\xff\xd0" + data[cut:]

    thumb = b"\xff\xd8" + seg(0xDA, b"\x00" * 4) + b"\x11\x22\xff\xd9"
    out = b"\xff\xd8" + seg(0xE1, b"Exif\x00\x00" + thumb + rnd.randbytes(100))
    out += seg(0xDB, bytes(65)) + seg(0xC0, b"\x08\x00\x10\x00\x10\x01\x01\x11\x00")
    out += seg(0xC4, bytes(20)) + seg(0xDA, b"\x01\x01\x00\x00\x3f\x00") + entropy(n)
    if progressive:
        out += seg(0xC4, bytes(20)) + seg(0xDA, b"\x01\x01\x00\x00\x3f\x00") + entropy(n)
    return out + b"\xff\xd9"


def png(w, h):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\x00" + rnd.randbytes(w * 3) for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def gif():
    return (b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0x80, 0, 0)
            + b"\x00\x00\x00\xff\xff\xff" + b"\x21\xf9\x04\x00\x00\x00\x00\x00"
            + b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0)
            + b"\x02\x02\x44\x01\x00" + b"\x3b")


def bmp(w, h):
    pixels = rnd.randbytes(w * 3 * h)
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    return b"BM" + struct.pack("<I4sI", 54 + len(pixels), bytes(4), 54) + dib + pixels


def pdf():
    filler = bytes(rnd.choice(b"abcdefgh \n") for _ in range(5000))
    return (b"%PDF-1.4\n1 0 obj\n<< /Length 5000 >>\nstream\n" + filler
            + b"\nendstream\nendobj\nxref\n0 1\ntrailer\n<<>>\nstartxref\n9\n%%EOF\n"
            + b"2 0 obj\n<<>>\nendobj\nxref\ntrailer\n<<>>\nstartxref\n99\n%%EOF\n")


def zip_file(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, name in enumerate(names):
            method = zipfile.ZIP_STORED if i % 2 else zipfile.ZIP_DEFLATED
            z.writestr(zipfile.ZipInfo(name), rnd.randbytes(3000) + b"a" * 3000,
                       compress_type=method)
    return buf.getvalue()


def sevenzip():
    packed, next_hdr = rnd.randbytes(3000), rnd.randbytes(50)
    sh = struct.pack("<QQI", len(packed), len(next_hdr), zlib.crc32(next_hdr))
    return b"7z\xbc\xaf\x27\x1c\x00\x04" + struct.pack("<I", zlib.crc32(sh)) + sh + packed + next_hdr


def _vint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def rar5():
    def block(htype, extra=b"", data=b""):
        fields = _vint(htype) + _vint(2 if data else 0) + (_vint(len(data)) if data else b"") + extra
        hdr = _vint(len(fields)) + fields
        return struct.pack("<I", zlib.crc32(hdr)) + hdr + data
    return (b"Rar!\x1a\x07\x01\x00" + block(1, _vint(0))
            + block(2, bytes(10), rnd.randbytes(5000)) + block(5, _vint(0)))


def rar4():
    data, name = rnd.randbytes(4000), b"a.txt"
    fields = struct.pack("<IIBIIBBHI", len(data), len(data), 0, 0, 0, 20, 0x30, len(name), 0x20) + name
    return (b"Rar!\x1a\x07\x00"
            + struct.pack("<HBHH", 0, 0x73, 0, 13) + bytes(6)
            + struct.pack("<HBHH", 0, 0x74, 0x8000, 7 + len(fields)) + fields + data
            + struct.pack("<HBHH", 0, 0x7B, 0x4000, 7))


def mp4(brand):
    def box(t, payload):
        return struct.pack(">I", 8 + len(payload)) + t + payload
    return (box(b"ftyp", brand + b"\x00\x00\x02\x00isomiso2") + box(b"free", b"")
            + box(b"mdat", rnd.randbytes(20000)) + box(b"moov", rnd.randbytes(500)))


def mkv():
    def el(eid, payload):
        return eid + b"\x01" + len(payload).to_bytes(7, "big") + payload
    return el(b"\x1a\x45\xdf\xa3", el(b"\x42\x82", b"matroska")) + el(b"\x18\x53\x80\x67", rnd.randbytes(30000))


def avi():
    def riff(form, payload):
        return b"RIFF" + struct.pack("<I", 4 + len(payload)) + form + payload
    return riff(b"AVI ", rnd.randbytes(10000)) + riff(b"AVIX", rnd.randbytes(8000))


def ole_doc():
    h = bytearray(512)
    h[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    h[0x18:0x20] = struct.pack("<HHHH", 0x3E, 3, 0xFFFE, 9)
    h[0x20:0x22] = struct.pack("<H", 6)
    h[0x2C:0x34] = struct.pack("<II", 1, 1)
    h[0x38:0x40] = struct.pack("<II", 4096, 0xFFFFFFFE)
    h[0x44:0x4C] = struct.pack("<II", 0xFFFFFFFE, 0)
    h[0x4C:0x200] = struct.pack("<109I", 0, *([0xFFFFFFFF] * 108))
    fat = struct.pack("<128I", 0xFFFFFFFD, 0xFFFFFFFE, 3, 4, 5, 0xFFFFFFFE, *([0xFFFFFFFF] * 122))
    d = bytearray(512)
    for i, name in enumerate(("Root Entry", "WordDocument")):
        enc = name.encode("utf-16-le") + b"\0\0"
        d[i * 128:i * 128 + len(enc)] = enc
    return bytes(h) + fat + bytes(d) + rnd.randbytes(4 * 512)


def build_image(path, files):
    cluster = 4096
    with open(path, "wb") as f:
        f.write(bytes(MB))  # wiped FAT area
        for data in files:
            f.write(data)
            slack = -f.tell() % cluster
            f.write(rnd.randbytes(slack))  # old data in the cluster tail
            f.write(rnd.randbytes(cluster * rnd.randint(0, 3)))
        f.write(bytes(MB))


MB = 1024 * 1024


def main():
    expected = [
        (jpeg(30000), "jpg"), (jpeg(20000, progressive=True), "jpg"),
        (png(60, 40), "png"), (gif(), "gif"), (bmp(64, 64), "bmp"),
        (pdf(), "pdf"),
        (zip_file(["a.bin", "b/c.bin", "d.txt"]), "zip"),
        (zip_file(["[Content_Types].xml", "word/document.xml", "word/x.bin"]), "docx"),
        (zip_file(["[Content_Types].xml", "xl/workbook.xml", "xl/x.bin"]), "xlsx"),
        (sevenzip(), "7z"), (rar4(), "rar"), (rar5(), "rar"),
        (mp4(b"isom"), "mp4"), (mp4(b"qt  "), "mov"), (mp4(b"heic"), "heic"),
        (mkv(), "mkv"), (avi(), "avi"), (ole_doc(), "doc"),
    ]
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "flash.img")
        build_image(img, [d for d, _ in expected])

        copy = recover.make_image(img, os.path.join(tmp, "copy.img"))
        with open(img, "rb") as a, open(copy, "rb") as b:
            assert a.read() == b.read(), "image copy differs"

        out = os.path.join(tmp, "out")
        recover.carve(img, out)
        got = {}
        for root, _, names in os.walk(out):
            for n in names:
                if n == "report.csv":
                    continue
                with open(os.path.join(root, n), "rb") as f:
                    got[hashlib.sha256(f.read()).hexdigest()] = n
        for data, ext in expected:
            name = got.pop(hashlib.sha256(data).hexdigest(), None)
            ok = name is not None and name.endswith("." + ext)
            failures += not ok
            print("%-5s %-6s %s" % (ext, len(data), "OK" if ok else "FAIL (%s)" % name))
        if got:
            print("Extra files:", sorted(got.values()))
            failures += len(got)

    assert recover.same_drive(r"\\.\E:", r"E:\out")
    assert recover.same_drive(r"\\.\e:", r"E:\out")
    assert not recover.same_drive(r"\\.\E:", r"D:\out")
    assert not recover.same_drive(r"D:\out\flash.img", r"D:\out\files")
    assert recover.normalize_source("e") == r"\\.\E:"
    assert recover.normalize_source("E:\\") == r"\\.\E:"
    assert recover.normalize_source("PhysicalDrive2") == r"\\.\PhysicalDrive2"

    print("\nFAILED: %d" % failures if failures else "\nAll checks passed.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
