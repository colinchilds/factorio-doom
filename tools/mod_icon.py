"""Draws the doom-input mod's shortcut icon (a joystick) as Factorio shortcut icons: a black
glyph with alpha, the full size and its half-size mipmap side by side. Stdlib only.

    python3 tools/mod_icon.py   # writes mod/doom-input/graphics/joystick-x56.png and -x24.png
"""
import math
import pathlib
import struct
import zlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "mod" / "doom-input" / "graphics"
SS = 6  # supersamples per axis


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def inside(x, y):
    """The glyph in unit coordinates (0..1, y down)."""
    # Base: a rounded slab, its top slightly narrower (a trapezoid with round corners).
    if 0.70 <= y <= 0.90:
        half = 0.40 - (0.90 - y) * 0.15
        r = 0.05
        if abs(x - 0.5) <= half - r or (abs(x - 0.5) <= half and r <= y - 0.70 <= 0.20 - r):
            return True
        for cx in (0.5 - half + r, 0.5 + half - r):
            for cy in (0.70 + r, 0.90 - r):
                if math.hypot(x - cx, y - cy) <= r:
                    return True
    # Fire button on the base, left.
    if math.hypot(x - 0.25, y - 0.66) <= 0.075:
        return True
    # Stick, leaning right, and its ball.
    if seg_dist(x, y, 0.52, 0.74, 0.62, 0.30) <= 0.055:
        return True
    if math.hypot(x - 0.635, y - 0.24) <= 0.15:
        return True
    return False


def render(n):
    """n x n alpha values (0..255)."""
    out = []
    for j in range(n):
        row = []
        for i in range(n):
            hits = sum(inside((i + (a + 0.5) / SS) / n, (j + (b + 0.5) / SS) / n)
                       for a in range(SS) for b in range(SS))
            row.append(round(255 * hits / (SS * SS)))
        out.append(row)
    return out


def png(path, size):
    """size and a half-size mipmap side by side, grey+alpha (black glyph)."""
    big, small = render(size), render(size // 2)
    w = size + size // 2
    raw = b""
    for j in range(size):
        line = bytearray([0])
        for i in range(w):
            a = big[j][i] if i < size else (small[j][i - size] if j < size // 2 else 0)
            line += bytes([0, a])
        raw += bytes(line)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, size, 8, 4, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(data)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    png(OUT / "joystick-x56.png", 56)
    png(OUT / "joystick-x24.png", 24)
    print("wrote", OUT)
