"""Sprite demo: a Freedoom monster walking toward and away from the camera, rendered by
combinators on a W x H lamp screen. It exercises the planned texture path at any screen size:

  scalar pipeline   clock -> walk frame, distance -> sprite height, 16.16 texel step, offsets
  column stage      lanes = screen columns: u = ((c - left) * step) >> 16, then a bit-sliced
                    lookup turns (frame, u) into NW packed strip words (8 x 4-bit texels each)
  row stage         per screen row y: v = ((y - top) * step) >> 16 is one scalar per row (all of
                    a sprite's columns share its depth), so the row picks word v >> 3 from the
                    column bus, shifts by (v & 7) * 4, masks 15, and maps texel -> RGB with one
                    decider per palette entry. Texel 0 (transparent) gets the row's floor/ceiling
                    color.

Everything is derived from W and H; the sprite is stored once at its source resolution.

  python3 demos/sprite.py --size 160x100            # -> demos/gen/sprite_160x100.{json,txt}
  python3 demos/sprite.py --size 64x40 --verify     # also run fsim and compare to the reference

The .txt file is the blueprint string to paste into the game. It brings its own substations but
no power source: hook it up to a grid (or an electric energy interface in editor mode).
"""
import argparse
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "conformance"))
from bp import (EACH, EVERYTHING, GREEN, GREEN_IN, GREEN_OUT, RED, RED_IN, RED_OUT, LETTERS,  # noqa: E402
                Blueprint, cond, encode, out, sig)

# Signals that exist, unhidden, in both vanilla and Space Age (harness/run.py --lanes). Virtual
# signals aren't in the list, so the named scalars (letters) can't collide with a lane.
LANE_SIGNALS = [sig(n, t) for t, n in json.loads((ROOT / "conformance" / "lane_signals.json").read_text())]
ASSETS = ROOT / "assets" / "freedoom" / "sprites.json"

PERIOD = 240          # ticks for one approach + retreat
WALK = 8              # ticks per walk frame
Z_NEAR, Z_RANGE = 256, 768
TEXEL_BITS = 4
PER_WORD = 32 // TEXEL_BITS
KEY_BASE, KEY_FRAME = 1 << 14, 1 << 12   # lookup key = KEY_BASE + KEY_FRAME * f + u
ROW_BASE = KEY_BASE >> 3                 # J = (v >> 3) + ROW_BASE
ONES = 16                                # palette keys are ONES + texel, so every lane exists


def i32(x):
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x & 0x80000000 else x


def tdiv(a, b):
    """Factorio integer division: truncates toward zero, x/0 = 0."""
    if b == 0:
        return 0
    q = abs(a) // abs(b)
    return i32(q if (a >= 0) == (b >= 0) else -q)


# ---------------------------------------------------------------------------------------------
# Sprite data: frames aligned on their offsets into one box, quantized to 15 colors + transparent.

def load_sprite(prefix):
    data = json.loads(ASSETS.read_text())
    names = [f"{prefix}{f}1" for f in "ABCD"]
    frames = [data["sprites"][n] for n in names]
    ax = max(s["left"] for s in frames)
    ay = max(s["top"] for s in frames)
    sw = max(ax - s["left"] + s["width"] for s in frames)
    sh = max(ay - s["top"] + s["height"] for s in frames)
    pal = data["palette"]
    boxes = []
    for s in frames:
        box = [[None] * sw for _ in range(sh)]
        for y, row in enumerate(s["rows"]):
            for x, i in enumerate(row):
                if i >= 0:
                    box[ay - s["top"] + y][ax - s["left"] + x] = tuple(pal[i])
        boxes.append(box)
    colors = quantize([c for b in boxes for r in b for c in r if c is not None], 15)
    texels = [[[0 if c is None else 1 + nearest(colors, c) for c in r] for r in b] for b in boxes]
    return names, sw, sh, colors, texels


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


def pack(rgb):
    v = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
    return v or 0x010101   # packed 0 would make the lamp fall back to its default color


def background(y, h):
    """Floor/ceiling color for screen row y."""
    half = h // 2
    if y < half:
        t = (half - y) * 255 // half          # 255 at the top, ~0 at the horizon
        return (14 + 34 * t // 255, 14 + 34 * t // 255, 20 + 40 * t // 255)
    t = (y - half + 1) * 255 // (h - half)    # ~0 at the horizon, 255 at the bottom
    return (20 + 90 * t // 255, 16 + 60 * t // 255, 12 + 36 * t // 255)


def strip_words(texels, sw, sh):
    """words[f][u][j] = texels (f, u, 8j .. 8j+7) packed 4 bits each, low nibble first."""
    nw = (sh + PER_WORD - 1) // PER_WORD
    words = []
    for f in range(len(texels)):
        cols = []
        for u in range(sw):
            col = []
            for j in range(nw):
                v = 0
                for n in range(PER_WORD):
                    y = j * PER_WORD + n
                    if y < sh:
                        v |= texels[f][y][u] << (TEXEL_BITS * n)
                col.append(i32(v))
            cols.append(col)
        words.append(cols)
    return nw, words


# ---------------------------------------------------------------------------------------------
# Scalar pipeline. Every stage reads the previous stage's network and passes all of it through,
# so every scalar on a network belongs to the same clock value T.

def scalar_stages(w, h, sw, sh):
    return [
        [("P", "T", "%", PERIOD), ("F", "T", "/", WALK)],
        [("D", "P", "-", PERIOD // 2), ("Fm", "F", "%", 4)],
        [("DD", "D", "*", "D"), ("FO", "Fm", "*", 1 << 28)],
        [("Zm", "DD", "*", Z_RANGE)],
        [("Zd", "Zm", "/", (PERIOD // 2) ** 2)],
        [("Z", "Zd", "+", Z_NEAR)],
        [("HS", h * 240, "/", "Z")],
        [("STEP", sh << 16, "/", "HS"), ("WSa", "HS", "*", sw), ("YBa", "HS", "*", 5)],
        [("WS", "WSa", "/", sh), ("YB", "YBa", "/", 8)],
        [("X0a", w, "-", "WS"), ("Y0a", "YB", "-", "HS")],
        [("XL", "X0a", "/", 2), ("YT", "Y0a", "+", h // 2)],
        [("XS", "XL", "*", "STEP"), ("YS", "YT", "*", "STEP"), ("NS", 1 << 30, "-", "STEP")],
        [("O1", "NS", "-", "XS"), ("OV", "NS", "-", "YS")],
        [("O", "O1", "+", "FO")],
    ]


OPS = {"+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b, "/": tdiv,
       "%": lambda a, b: 0 if b == 0 else i32(a - tdiv(a, b) * b), ">>": lambda a, b: a >> (b & 31),
       "<<": lambda a, b: a << (b & 31), "AND": lambda a, b: a & b}


def scalars(T, w, h, sw, sh):
    env = {"T": T}
    for stage in scalar_stages(w, h, sw, sh):
        new = {}
        for name, a, op, b in stage:
            va = env.get(a, 0) if isinstance(a, str) else a
            vb = env.get(b, 0) if isinstance(b, str) else b
            new[name] = i32(OPS[op](va, vb))
        env.update(new)
    return env


def reference(T, w, h, sw, sh, words, nw, colors):
    """The frame the circuit shows for clock value T, computed with the same integer ops."""
    s = scalars(T, w, h, sw, sh)
    step, o, ov = s["STEP"], s["O"], s["OV"]
    keys = {}
    for c in range(w):
        key = i32(i32((c + 1) * step) + o) >> 16
        f, u = divmod(key - KEY_BASE, KEY_FRAME)
        keys[c] = (f, u) if 0 <= f < len(words) and 0 <= u < sw else None
    img = []
    for y in range(h):
        V = i32(i32((y + 1) * step) + ov) >> 16
        J = V >> 3
        sh_amt = i32(V << 2) & 28
        j = J - ROW_BASE
        bg = pack(background(y, h))
        row = []
        for c in range(w):
            texel = 0
            if keys[c] and 0 <= j < nw:
                f, u = keys[c]
                texel = (words[f][u][j] >> sh_amt) & 15
            row.append(pack(colors[texel - 1]) if texel else bg)
        img.append(row)
    return img


# ---------------------------------------------------------------------------------------------
# Blueprint

def build(w, h, prefix):
    names, sw, sh, colors, texels = load_sprite(prefix)
    nw, words = strip_words(texels, sw, sh)
    if w + h > len(LANE_SIGNALS):
        raise SystemExit(f"{w}x{h} needs {w + h} lane signals, only {len(LANE_SIGNALS)} available")
    cols, rows = LANE_SIGNALS[:w], LANE_SIGNALS[w:w + h]
    b = Blueprint(f"Sprite demo {w}x{h} ({prefix}, Freedoom)")
    A = lambda tx, ty, *args, **kw: b.arithmetic(tx, ty, *args, direction=4, **kw)
    D = lambda tx, ty, *args, **kw: b.decider(tx, ty, *args, direction=4, **kw)

    # Row stage slots, east-facing (2x1), with a 2-tile substation gap after every 8 slots.
    n_slots = nw + 2 + 16
    width = 2 * n_slots + 2 * ((n_slots - 1) // 8)
    x0 = -width
    slot = lambda i: x0 + 2 * i + 2 * (i // 8)
    MUX, SHIFT, MASK, PAL = 0, nw, nw + 1, nw + 2
    assert slot(n_slots - 1) + 2 == 0

    names_sig = {}

    def S(name):
        if name not in names_sig:
            names_sig[name] = sig(LETTERS[len(names_sig)])
        return names_sig[name]
    S("T")

    # --- Scalar pipeline: the clock, then one column of combinators per stage, right to left,
    # ending just above the column/row path. Stage k sits at x = xs(k), rows y = -5, -6, ...
    stages = scalar_stages(w, h, sw, sh)
    n_st = len(stages)
    xs = lambda k: slot(nw) + 6 + 2 * (n_st - k)      # k = 0 is the clock
    clock_c = b.constant(xs(0) + 2, -5, [(S("T"), 1)])
    clock = D(xs(0), -5, [cond(S("T"), "<", 1 << 30, RED)], [out(S("T"), networks=RED)])
    b.wire(clock_c, 1, clock, RED_IN)
    b.wire(clock, RED_OUT, clock, RED_IN)
    prev_out = (clock, RED_OUT)
    for k, stage in enumerate(stages, 1):
        combs = [A(xs(k), -5, EACH, "+", 0, EACH, RED)]          # pass-through
        for m, (name, a, op, bb) in enumerate(stage, 1):
            first = a if isinstance(a, int) else S(a)
            second = bb if isinstance(bb, int) else S(bb)
            combs.append(A(xs(k), -5 - m, first, op, second, S(name),
                           None if isinstance(a, int) else RED, None if isinstance(bb, int) else RED))
        b.wire(*prev_out, combs[0], RED_IN)
        b.chain(combs, RED_IN)
        b.chain(combs, RED_OUT)
        prev_out = (combs[0], RED_OUT)

    # --- Column path (lanes = columns) and row path (lanes = rows), fed by the last stage.
    sx = slot(nw)                                     # x of the shift slot; the area above it
    ci = b.constant(sx + 6, -1, [(c, i + 1) for i, c in enumerate(cols)])
    c_b = A(sx + 4, -1, EACH, "*", S("STEP"), EACH, GREEN, RED)
    c_od = A(sx + 4, -2, S("O"), "+", 0, S("O"), RED)
    c_q = A(sx + 2, -1, EACH, "+", S("O"), EACH, RED, GREEN)
    c_u = A(sx, -1, EACH, ">>", 16, EACH, RED)
    b.wire(ci, 2, c_b, GREEN_IN)
    b.wire(c_b, RED_OUT, c_q, RED_IN)
    b.wire(c_od, GREEN_OUT, c_q, GREEN_IN)
    b.wire(c_q, RED_OUT, c_u, RED_IN)

    ri = b.constant(sx + 8, -3, [(r, i + 1) for i, r in enumerate(rows)])
    r_bv = A(sx + 6, -3, EACH, "*", S("STEP"), EACH, GREEN, RED)
    r_ovd = A(sx + 6, -4, S("OV"), "+", 0, S("OV"), RED)
    r_vq = A(sx + 4, -3, EACH, "+", S("OV"), EACH, RED, GREEN)
    r_v = A(sx + 2, -2, EACH, ">>", 16, EACH, RED)
    r_j = A(sx, -2, EACH, ">>", 3, EACH, RED)
    r_sa = A(sx + 2, -3, EACH, "<<", 2, EACH, RED)
    r_s = A(sx, -3, EACH, "AND", 28, EACH, RED)
    b.wire(ri, 2, r_bv, GREEN_IN)
    b.wire(r_bv, RED_OUT, r_vq, RED_IN)
    b.wire(r_ovd, GREEN_OUT, r_vq, GREEN_IN)
    b.wire(r_vq, RED_OUT, r_v, RED_IN)
    b.wire(r_v, RED_OUT, r_j, RED_IN)
    b.wire(r_v, RED_OUT, r_sa, RED_IN)
    b.wire(r_sa, RED_OUT, r_s, RED_IN)
    # The last stage's output network feeds both paths.
    for e in (r_ovd, r_bv, c_od, c_b):
        b.wire(*prev_out, e, RED_IN)
        prev_out = (e, RED_IN)

    # --- Lookup block: word j's bit-slice deciders stacked above mux slot j. Bit b of word j is
    # one decider: OR of every key whose word has that bit, outputting Each = 2^b.
    keys = [(KEY_BASE + KEY_FRAME * f + u, words[f][u]) for f in range(len(words)) for u in range(sw)]
    group_bottom = []
    for j in range(nw):
        stack = []
        for bit in range(32):
            hits = [k for k, ws in keys if (ws[j] >> bit) & 1]
            if not hits:
                continue
            conds = [cond(EACH, "=", k, RED, compare_type="or" if n else None) for n, k in enumerate(hits)]
            stack.append(D(slot(MUX + j), -1 - len(stack), conds, [out(EACH, copy=False, constant=i32(1 << bit))]))
        assert stack, f"word {j} is empty"
        b.chain(stack, RED_IN)
        b.chain(stack, GREEN_OUT)
        group_bottom.append(stack[0])
    b.chain(group_bottom, RED_IN)
    b.wire(c_u, RED_OUT, group_bottom[-1], RED_IN)

    # --- Screen rows.
    ones = b.constant(slot(PAL + 15), -1, [(c, ONES) for c in cols])
    holes = set()
    for sy in sorted({min(8 + 18 * j, h - 2) for j in range((h + 17) // 18)}):
        for sxx in sorted({min(8 + 18 * i, w - 2) for i in range((w + 17) // 18)}):
            b.substation(sxx, sy)
            holes |= {(sxx, sy), (sxx + 1, sy), (sxx, sy + 1), (sxx + 1, sy + 1)}
    above = None
    for y, rs in enumerate(rows):
        mux = [D(slot(MUX + j), y, [cond(rs, "=", ROW_BASE + j, RED)], [out(EVERYTHING, networks=GREEN)])
               for j in range(nw)]
        shift = A(slot(SHIFT), y, EACH, ">>", rs, EACH, GREEN, RED)
        mask = A(slot(MASK), y, EACH, "AND", 15, EACH, RED)
        bg = pack(background(y, h))
        pal = [D(slot(PAL), y, [cond(EACH, "=", ONES)], [out(EACH, copy=False, constant=bg)])]
        pal += [D(slot(PAL + t), y, [cond(EACH, "=", ONES + t)], [out(EACH, copy=False, constant=pack(colors[t - 1]))])
                for t in range(1, 16)]
        b.chain(mux, RED_IN)                     # J (row scalars)
        b.chain(mux, GREEN_OUT)                  # the chosen word
        b.wire(mux[-1], GREEN_OUT, shift, GREEN_IN)
        b.wire(shift, RED_OUT, mask, RED_IN)
        b.wire(mask, RED_OUT, pal[0], RED_IN)
        b.chain(pal, RED_IN)
        b.chain(pal, GREEN_IN)                   # ONES on every lane
        b.chain(pal, RED_OUT)
        lamps = [b.lamp(x, y, {"use_colors": True, "color_mode": 2, "rgb_signal": cols[x]}, always_on=True)
                 for x in range(w) if (x, y) not in holes]
        b.wire(pal[-1], RED_OUT, lamps[0], 1)
        b.chain(lamps, 1)
        if above is None:                        # row 0: connect to the producers above
            b.wire(r_j, RED_OUT, mux[-1], RED_IN)
            b.wire(r_s, RED_OUT, shift, RED_IN)
            b.wire(ones, 2, pal[-1], GREEN_IN)
            for j in range(nw):
                b.wire(group_bottom[j], GREEN_OUT, mux[j], GREEN_IN)
        else:
            a_mux, a_shift, a_pal = above
            b.wire(a_mux[0], RED_IN, mux[0], RED_IN)
            for j in range(nw):
                b.wire(a_mux[j], GREEN_IN, mux[j], GREEN_IN)
            b.wire(a_shift, RED_IN, shift, RED_IN)
            b.wire(a_pal[0], GREEN_IN, pal[0], GREEN_IN)
        above = (mux, shift, pal)
    b.power()
    b.link_power()
    b.check_reach()
    meta = {"w": w, "h": h, "sprite": prefix, "frames": names, "sw": sw, "sh": sh, "nw": nw,
            "colors": colors, "holes": sorted(holes)}
    return b, meta, words


def read_ppm(path):
    data = path.read_bytes()
    parts = data.split(b"\n", 3)
    w, h = map(int, parts[1].split())
    px = parts[3]
    return w, h, [[(px[3 * (y * w + x)] << 16) | (px[3 * (y * w + x) + 1] << 8) | px[3 * (y * w + x) + 2]
                   for x in range(w)] for y in range(h)]


def verify(path, meta, words, ticks):
    w, h = meta["w"], meta["h"]
    out_dir = ROOT / "out" / f"sprite_{w}x{h}_ppm"
    if out_dir.exists():
        for f in out_dir.glob("*.ppm"):
            f.unlink()
    subprocess.run([str(ROOT / "target" / "release" / "fsim"), "run", str(path), "--ticks", str(ticks),
                    "--ppm", str(out_dir)], check=True)
    frames = {int(p.stem): read_ppm(p)[2] for p in sorted(out_dir.glob("*.ppm"))}
    holes = {tuple(x) for x in meta["holes"]}
    colors = [tuple(c) for c in meta["colors"]]
    ref_cache = {}

    def ref(T):
        if T not in ref_cache:
            ref_cache[T] = reference(T, w, h, meta["sw"], meta["sh"], words, meta["nw"], colors)
        return ref_cache[T]

    def diff(img, T):
        r = ref(T)
        return sum(1 for y in range(h) for x in range(w) if (x, y) not in holes and img[y][x] != r[y][x])

    # The clock value a frame shows is the tick minus the pipeline latency. Many frames look the
    # same for several clock values, so pick the one latency that fits the late frames best, then
    # hold every frame after the pipeline fills to it.
    late = sorted(frames)[len(frames) // 2:]
    lag = min(range(0, 60), key=lambda k: sum(1 for t in late if diff(frames[t], t - k)))
    first = min(t for t in frames if t - lag >= 1)
    bad = [(t, n) for t in sorted(frames) if t >= first + 2 for n in [diff(frames[t], t - lag)] if n]
    checked = sum(1 for t in frames if t >= first + 2)
    print(f"latency {lag} ticks; {checked} frames x {w * h - len(holes)} lamps checked; "
          f"{len(bad)} frames differ from the reference")
    for t, n in bad[:200]:
        print(f"  tick {t}: {n} lamps differ")
    return not bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="160x100")
    ap.add_argument("--sprite", default="TROO")
    ap.add_argument("--ticks", type=int, default=PERIOD + 60)
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    w, h = map(int, a.size.lower().split("x"))
    b, meta, words = build(w, h, a.sprite)
    data = b.to_json()
    data["fsim"] = {"ticks": a.ticks, "doc": f"sprite demo {w}x{h}"}
    gen = HERE / "gen"
    gen.mkdir(exist_ok=True)
    path = gen / f"sprite_{w}x{h}.json"
    path.write_text(json.dumps(data, separators=(",", ":")))
    (gen / f"sprite_{w}x{h}.txt").write_text(encode(b.to_json()) + "\n")
    n_comb = sum(1 for e in b.entities if "combinator" in e["name"])
    n_lamp = sum(1 for e in b.entities if e["name"] == "small-lamp")
    print(f"{path.relative_to(ROOT)}: {len(b.entities)} entities ({n_comb} combinators, {n_lamp} lamps); "
          f"sprite {meta['sw']}x{meta['sh']}, {meta['nw']} words per column")
    if a.verify and not verify(path, meta, words, a.ticks):
        sys.exit(1)


if __name__ == "__main__":
    main()
