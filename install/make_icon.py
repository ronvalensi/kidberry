"""Draw Kidberry's icon (the berry logo) into kidberry/static/kidberry.ico. Pure Python, no image library.

    python install/make_icon.py
"""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "kidberry" / "static" / "kidberry.ico"
# The logo in a 32x32 box: (cx, cy, r, rgb) circles, back to front, then the leaf.
BERRIES = [(11, 16, 6, (0x7b, 0x5a, 0xd6)), (21, 16, 6, (0x6a, 0x4b, 0xc4)), (16, 23, 6.5, (0x8a, 0x6e, 0xe0))]
SHINE = [(9.5, 14.5, 1.6), (14.5, 21, 1.8)]
LEAF = (0x3f, 0x9b, 0x6a)


def leaf(x, y):
    # an ellipse leaf up and to the right, and a short stem
    dx, dy = x - 20.5, y - 7.5
    a = math.radians(-20)
    u, v = dx * math.cos(a) + dy * math.sin(a), -dx * math.sin(a) + dy * math.cos(a)
    if (u / 4.2) ** 2 + (v / 1.7) ** 2 <= 1:
        return True
    return 14.6 <= x <= 16.6 and 5.5 <= y <= 10.5


def pixel(x, y):
    """Colour (r, g, b, a) at a point of the 32-unit logo."""
    col, alpha = (0, 0, 0), 0.0
    if leaf(x, y):
        col, alpha = LEAF, 1.0
    for cx, cy, r, rgb in BERRIES:
        if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
            col, alpha = rgb, 1.0
    for cx, cy, r in SHINE:
        if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
            col = tuple(int(c + (255 - c) * .45) for c in col)
    return col, alpha


def render(size: int, ss: int = 4) -> bytes:
    rows = []
    scale = 32 / size
    for py in range(size):
        row = bytearray([0])                    # PNG filter: none
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    (cr, cg, cb), ca = pixel((px + (sx + .5) / ss) * scale, (py + (sy + .5) / ss) * scale)
                    r, g, b, a = r + cr * ca, g + cg * ca, b + cb * ca, a + ca
            n = ss * ss
            row += bytes([int(r / a) if a else 0, int(g / a) if a else 0, int(b / a) if a else 0, int(255 * a / n)])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    pngs = [render(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, png in zip(sizes, pngs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    OUT.write_bytes(head + entries + b"".join(pngs))
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
