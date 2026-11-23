"""Draw the setup wizard's pictures (the berry logo on Kidberry's colours) as 24-bit BMPs, for install/kidberry.iss.

    python install/make_wizard_images.py OUTDIR
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_icon import pixel  # noqa: E402  (the logo, in a 32-unit box)

WARM = (0xfa, 0xf7, 0xf3)      # the app's background
BERRY_SOFT = (0xec, 0xe6, 0xf8)
WHITE = (0xff, 0xff, 0xff)


def bmp(width, height, colour_at) -> bytes:
    pad = (4 - width * 3 % 4) % 4
    rows = []
    for y in range(height - 1, -1, -1):          # BMP rows go bottom-up
        row = bytearray()
        for x in range(width):
            r, g, b = colour_at(x, y)
            row += bytes([b, g, r])
        rows.append(bytes(row) + b"\0" * pad)
    data = b"".join(rows)
    head = struct.pack("<2sIHHI", b"BM", 54 + len(data), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(data), 2835, 2835, 0, 0)
    return head + info + data


def logo_over(bg, size, ox, oy, ss=4):
    """colour_at() that draws the logo (size px, top-left ox, oy) over a background function."""
    def at(x, y):
        base = bg(x, y)
        if not (ox <= x < ox + size and oy <= y < oy + size):
            return base
        acc = [0.0, 0.0, 0.0]
        for sy in range(ss):
            for sx in range(ss):
                (cr, cg, cb), a = pixel((x - ox + (sx + .5) / ss) * 32 / size, (y - oy + (sy + .5) / ss) * 32 / size)
                for i, c in enumerate((cr, cg, cb)):
                    acc[i] += c * a + base[i] * (1 - a)
        return tuple(int(v / (ss * ss)) for v in acc)
    return at


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    w, h = 240, 459                               # the modern wizard's side picture, at 150% scaling

    def side(x, y):                               # warm, fading to a soft berry tint at the bottom
        t = y / h
        return tuple(int(WARM[i] + (BERRY_SOFT[i] - WARM[i]) * t) for i in range(3))
    (out / "wizard.bmp").write_bytes(bmp(w, h, logo_over(side, 150, (w - 150) // 2, 120)))
    (out / "wizard-small.bmp").write_bytes(bmp(110, 110, logo_over(lambda x, y: WHITE, 96, 7, 7)))
    print(f"wrote {out}\\wizard.bmp and wizard-small.bmp")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
