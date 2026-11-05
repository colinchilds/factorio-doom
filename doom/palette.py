"""Color helpers shared by the renderers: quantization, the lamp color grade, and packing."""

# Lamps show a dark frame around each lit square, so Freedoom's dark palette comes out murky. The
# grade lifts the brightest channel with a gamma curve (scaling all three, so hue holds and nothing
# clips) and saturates a little, before colors are stored at 4 bits per channel. Build parameters:
# raycast.Shading, --gamma and --saturation.
DEFAULT_GAMMA = 0.5
DEFAULT_SATURATION = 1.25


def nearest(colors, c):
    return min(range(len(colors)), key=lambda k: sum((a - b) ** 2 for a, b in zip(colors[k], c)))


def quantize(pixels, k):
    """Deterministic k-means in RGB, seeded by farthest-point picks from the most common color."""
    counts = {}
    for p in pixels:
        counts[p] = counts.get(p, 0) + 1
    distinct = sorted(counts, key=lambda c: (-counts[c], c))
    centers = [distinct[0]]
    while len(centers) < min(k, len(distinct)):
        centers.append(max(distinct, key=lambda c: min(sum((a - b) ** 2 for a, b in zip(c, m)) for m in centers)))
    for _ in range(20):
        sums = [[0, 0, 0, 0] for _ in centers]
        for c, n in counts.items():
            s = sums[nearest(centers, c)]
            for i in range(3):
                s[i] += c[i] * n
            s[3] += n
        centers = [tuple(round(s[i] / s[3]) for i in range(3)) if s[3] else centers[j] for j, s in enumerate(sums)]
    return centers


def grade(rgb, gamma=DEFAULT_GAMMA, saturation=DEFAULT_SATURATION):
    v = max(rgb)
    if v <= 0:
        return tuple(rgb)
    luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    sat = [max(0.0, luma + (x - luma) * saturation) for x in rgb]
    k = 255 * (v / 255) ** gamma / max(sat)
    return tuple(min(255, round(x * k)) for x in sat)


def c4(rgb, gamma=DEFAULT_GAMMA, saturation=DEFAULT_SATURATION):
    """A graded color at 4 bits per channel (times a brightness <= 17 on the screen)."""
    return tuple(min(15, round(x / 17)) for x in grade(rgb, gamma, saturation))


def pack(rgb):
    v = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
    return v or 0x010101   # packed 0 would make the lamp fall back to its default color
