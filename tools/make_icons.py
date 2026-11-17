"""Draw the Bon Temps icon: a minimalist take on the Acadiana flag that flies over
Lafayette City Hall (blue over red, white triangle at the hoist with a gold star,
three white fleurs-de-lis, a gold castle).

    python tools/make_icons.py
    -> docs/icon.svg, docs/icon-180.png, docs/icon-192.png, docs/icon-512.png

No extra libraries: the same shapes are written as SVG and drawn into PNGs.
"""
import math
import struct
import zlib
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
BLUE, RED, WHITE, GOLD = "#1F5BA6", "#C62F3E", "#FFFFFF", "#F4B41A"


def star(cx, cy, r_out, r_in):
    pts = []
    for i in range(10):
        r = r_out if i % 2 == 0 else r_in
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def fleur(cx, cy, s=0.95):
    parts = [
        [(0, -6), (1.7, -2.6), (1.3, 1), (0, 2), (-1.3, 1), (-1.7, -2.6)],   # center petal
        [(1.3, 0.6), (3.0, -3.2), (5.0, -1.6), (4.1, 1.4), (2.0, 1.9)],      # right petal
        [(-1.3, 0.6), (-3.0, -3.2), (-5.0, -1.6), (-4.1, 1.4), (-2.0, 1.9)],  # left petal
        [(-3.2, 1.8), (3.2, 1.8), (3.2, 3.1), (-3.2, 3.1)],                  # band
        [(-1.1, 3.1), (1.1, 3.1), (0, 5.8)],                                 # foot
    ]
    return [[(cx + x * s, cy + y * s) for x, y in p] for p in parts]


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


# Shapes in drawing order, on a 64 x 64 canvas.
SHAPES = [
    (BLUE, rect(0, 0, 64, 32)),
    (RED, rect(0, 32, 64, 64)),
    (WHITE, [(0, 0), (27, 32), (0, 64)]),
    (GOLD, star(9.2, 32, 6.2, 2.5)),
    *[(WHITE, p) for cx in (34, 44.5, 55) for p in fleur(cx, 15.5)],
    (GOLD, rect(37, 46, 52, 56)),           # castle body
    (GOLD, rect(37, 40.5, 41, 46)),         # left tower
    (GOLD, rect(42.5, 37.5, 46.5, 46)),     # middle tower
    (GOLD, rect(48, 40.5, 52, 46)),         # right tower
    (RED, rect(43.3, 50.5, 45.7, 56)),      # door
]


def inside(x, y, poly):
    hit = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def color_at(x, y):
    c = WHITE
    for fill, poly in SHAPES:
        if inside(x, y, poly):
            c = fill
    return rgb(c)


def write_svg(path):
    polys = "".join(
        f'<path fill="{fill}" d="M{" L".join(f"{x:.2f} {y:.2f}" for x, y in poly)}Z"/>'
        for fill, poly in SHAPES)
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<clipPath id="r"><rect width="64" height="64" rx="14"/></clipPath>'
        f'<g clip-path="url(#r)">{polys}</g></svg>\n', encoding="utf-8")
    print("wrote", path.name)


def write_png(size, path, ss=4):
    rows = []
    for j in range(size):
        row = bytearray([0])
        for i in range(size):
            acc = [0, 0, 0]
            for a in range(ss):
                for b in range(ss):
                    c = color_at((i + (a + .5) / ss) * 64 / size, (j + (b + .5) / ss) * 64 / size)
                    acc = [p + q for p, q in zip(acc, c)]
            row += bytes(round(v / ss / ss) for v in acc)
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))
    print("wrote", path.name)


if __name__ == "__main__":
    write_svg(DOCS / "icon.svg")
    for s in (180, 192, 512):
        write_png(s, DOCS / f"icon-{s}.png")
