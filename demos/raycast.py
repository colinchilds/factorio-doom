"""M3 raycaster: Freedoom-textured walls, floors and ceilings, a scripted camera, at any --size.

Everything runs as vectors whose lanes are screen columns (no per-column units):

  scalars   view angle -> camera vectors (signed bit-sliced lookups) and position
  rays      RAW = camx * plane + dir << 10 per column (a wire sum; camX is a constant vector),
            RD = RAW >> 10, never 0 by the choice of the rounding term (ray_base)
  march     for vertical grid lines and for horizontal ones separately: K steps, each one network
            of W lanes, all computed at once from the step index k. Step k's distance D_k and the
            map key of the cell entered (<< 12, with the position along the wall in the low bits)
            are each one division by RD. A range-lookup decider marks walls with HIT = -2^30. Two
            min trees per side: over HIT + D_k (the distance) and over HIT + key_k (keys grow
            with distance, so the same nearest hit: its texture column and wall type).
  split     the last min level splits by side: MV = D where vertical lines are nearer, MH the rest
  texture   per side, TK = type and texture column from the key; strip lookups gated by the side
            being nearer give tex/8 words of packed texels per column (8 x 4 bits), palettes the
            same way (one bus per texel value), times a brightness BRIGHT / (D + FOG)
  rows      per screen row y: V = D / c_y = 4 (v - tex/2) (texel row v), a mux picks word v >> 3,
            a shift by 4 (v & 7) puts the texel in the top nibble, and one decider per texel value
            copies its color bus. Out-of-wall rows (|V| >= 2 tex) get a word of all 1s, the
            floor/ceiling marker.
  flats     per row, the floor (or ceiling) point under each column is P + (H / |r|) * ray: its
            position within the tile times 2^32 is RAW * K_y + (P << 24) per axis (int32 products
            wrap, dropping the whole tiles). Deciders on its top bits pick a word of 8 packed flat
            texels, a shift picks the texel, and one decider per flat color, where the wall's texel
            is 15, outputs that color at the row's brightness (constant: the distance is H / |r|).
            Runs beside the march, so it adds no latency.

Lanes vanish when a value is 0, which would silently drop columns. Every vector that might hit 0
is combined lane-wise with one that is nonzero on every lane, or read by a decider that also
tests a full vector (lanes visited = the union over all Each operands of all conditions).

Known limitation: textures are mirrored on wall faces seen from the far side of one axis (u is
the position along the wall, not along the viewer's right).

  python3 demos/raycast.py --size 160x100 --verify   # fsim vs the netlist evaluation
"""
import argparse
import json
import math
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "conformance"))
sys.path.insert(0, str(HERE))
from bp import DEFAULT_SUBSTATION_QUALITY, Blueprint, encode, quality_arg  # noqa: E402
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import EACH, GREEN, LANE_SIGNALS, RED, Collect, Graph, Scalar  # noqa: E402
from sprite import nearest, quantize  # noqa: E402

MAP = """
#######BB#######
#..............#
#..MM......MM..#
#..MM......MM..#
#..............#
#......RR......#
B...B......B...B
B...B......B...B
B...B......B...B
#......RR......#
#..............#
#..MM......MM..#
#..MM......MM..#
#..............#
#..............#
#######BB#######
""".split()
TEXTURE_OF = {"#": "STARTAN3", "B": "BRICK7", "M": "MARBLE1", "R": "SP_HOT1"}   # Freedoom (BSD)
TEXTURES = ROOT / "assets" / "freedoom" / "textures.json"
K = 16                       # grid lines marched per axis (>= map size)
FP = 256                     # position units per tile
CENTER = (8 * FP, 7.5 * FP)  # camera path: circle around this point
RADIUS = int(1.25 * FP)
PLANE = 676                  # tan(fov/2) * 1024: about 66 degrees
HIT = -(1 << 30)
DS = 1 << 15                 # distance D: 2^15 per tile
FOG = 4 * DS                 # D at about half brightness
BRIGHT = 17 * FOG            # brightness = BRIGHT / (D + FOG) <= 17; texture channels <= 15
NEG = 4 * len(MAP)           # line base of map keys for rays going - (keys grow with distance)
COLORS = 14                  # texels 1..14 per texture; 15 marks floor / ceiling
DEFAULT_TEX = 64             # texture resolution (texels per tile, both ways)
FLAT_OF = {"floor": "FLOOR4_8", "ceiling": "TLITE6_4"}   # Freedoom flats, one per tile
FLAT_COLORS = 16             # flat texels 0..15
DEFAULT_FLAT = 16            # flat resolution (texels per tile, both ways); 0 = untextured


def pack(rgb):
    return (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]


def background(y, h):
    half = h // 2
    if y < half:
        t = (half - y) * 255 // half
        return (14 + 34 * t // 255, 14 + 34 * t // 255, 20 + 40 * t // 255)
    t = (y - half + 1) * 255 // (h - half)
    return (20 + 70 * t // 255, 18 + 50 * t // 255, 14 + 30 * t // 255)


def is_wall(c):
    return c != "."


def wall_types():
    """Wall characters in the map, in TEXTURE_OF order: index = texture type."""
    used = {c for row in MAP for c in row if is_wall(c)}
    return [c for c in TEXTURE_OF if c in used]


def cell_keys(side, x, y):
    """The two map keys of wall cell (x, y) for crossings of side 0 (vertical grid lines) or 1:
    the line coordinate of the cell entered, << 6, plus the across one. Rays going + use
    line + 1, rays going - use NEG - line, so along every ray the keys grow with distance and the
    nearest hit is the smallest key."""
    if side == 0:
        return 64 * (x + 1) + y, 64 * (NEG - x) + y
    return 64 * y + x + 1, 64 * (NEG - y) + x + 1


def wall_keys(side, ty=None):
    types = wall_types()
    return [k for y, row in enumerate(MAP) for x, c in enumerate(row)
            if is_wall(c) and (ty is None or types.index(c) == ty) for k in cell_keys(side, x, y)]


def trig(fn):
    return {a: round(1024 * fn(2 * math.pi * a / 256)) + 1024 for a in range(256)}


def camera_tables(offset=0):
    """Per view angle a (0..255, plus offset): direction DX, DY (scale 1024) and camera plane
    PLX, PLY."""
    t = {}
    for a in range(256):
        th = 2 * math.pi * ((a + offset) % 256) / 256
        dx, dy = round(1024 * math.cos(th)), round(1024 * math.sin(th))
        t[a] = {"DX": dx, "DY": dy, "PLX": (dy * -PLANE) >> 10, "PLY": (dx * PLANE) >> 10}
    return t


def ray_base(tabs, camx, d, pl):
    """Per angle: the ray base (D << 10) + e, with the rounding term e as close to 512 as possible
    such that no column's component (camx * PL + (D << 10) + e) >> 10 is 0. A 0 lane would
    vanish; this replaces forcing the components odd (an OR, one tick)."""
    out = {}
    for a, t in tabs.items():
        vs = [cx * t[pl] + (t[d] << 10) for cx in camx]
        for e in sorted(range(-1024, 2048), key=lambda e: (abs(e - 512), e)):
            if all((v + e) >> 10 for v in vs):
                out[a] = (t[d] << 10) + e
                break
        else:
            raise ValueError(f"angle {a}: no rounding term keeps every {d} ray nonzero")
    return out


def key_ranges(keys, shift, base=0):
    """Conditions: Each in [base + (k << shift), base + ((k + 1) << shift)) for any k in keys
    (consecutive keys merged into one range)."""
    runs = []
    for k in sorted(set(keys)):
        if runs and runs[-1][1] == k - 1:
            runs[-1][1] = k
        else:
            runs.append([k, k])
    conds = []
    for lo, hi in runs:
        conds += [sem.cond(EACH, ">=", base + (lo << shift), RED, "or" if conds else None),
                  sem.cond(EACH, "<", base + ((hi + 1) << shift), RED, "and")]
    return conds


# ---------------------------------------------------------------------------------------------
# Textures: Freedoom wall textures, one tile = 128 x 128 texture units, box-filtered to tex x tex
# and quantized to COLORS colors each, stored at 4 bits per channel (times a brightness <= 17).

def load_textures(tex):
    if 128 % tex or tex % 16:
        raise SystemExit(f"texture resolution {tex}: must divide 128 and be a multiple of 16")
    data = json.loads(TEXTURES.read_text())
    pal = data["palette"]
    out = []
    for ch in wall_types():
        t = data["textures"][TEXTURE_OF[ch]]
        tw, th, rows = t["width"], t["height"], t["rows"]
        sx, sy = 128 // tex, th / tex
        px = []
        for v in range(tex):
            line = []
            for u in range(tex):
                acc, n = [0, 0, 0], 0
                for yy in range(int(v * sy), max(int(v * sy) + 1, int((v + 1) * sy))):
                    for xx in range(u * sx, (u + 1) * sx):
                        i = rows[yy][xx % tw]
                        for c in range(3):
                            acc[c] += pal[i][c] if i >= 0 else 0
                        n += 1
                line.append(tuple(round(a / n) for a in acc))
            px.append(line)
        centers = quantize([c for r in px for c in r], COLORS)
        texels = [[1 + nearest(centers, c) for c in r] for r in px]
        c4 = [tuple(min(15, round(x / 17)) for x in c) for c in centers]
        out.append({"char": ch, "name": TEXTURE_OF[ch], "texels": texels, "c4": c4})
    return out


def side_color(c4, side):
    """Packed color at brightness 1: side 1 (horizontal grid lines) 3/4 as bright. Never 0 (a
    lamp reading 0 shows its default color)."""
    c = c4 if side == 0 else tuple(x * 3 // 4 for x in c4)
    return pack(c) or 1


def strip_word(texels, u, j):
    """Texels v = 8j .. 8j + 7 of column u, texel 8j + n at bits 28 - 4n: shifting left by 4n
    puts it in the top nibble."""
    return sem.i32(sum(texels[8 * j + n][u] << (28 - 4 * n) for n in range(8)))


def top_range(t, bits, wire=RED, first=None):
    """Conditions: the top `bits` bits of Each (on wire) are t, as one signed range. first: the
    connector of the first condition ("and" when appended to others)."""
    lo = sem.i32(t << (32 - bits))
    hi = lo + (1 << (32 - bits))
    conds = []
    if lo > -(1 << 31):
        conds.append(sem.cond(EACH, ">=", lo, wire, first))
    if hi < (1 << 31):
        conds.append(sem.cond(EACH, "<", hi, wire, "and" if conds else first))
    return conds


def nibble_range(t, wire=RED, first=None):
    """Conditions: the top nibble of Each (signed) is t."""
    return top_range(t, 4, wire, first)


# Flats (floor and ceiling): one flat per tile, box-filtered from 64x64 to f x f texels and quantized
# to FLAT_COLORS colors. A floor pixel's world position is P + (H / |r|) * ray (r = 2y + 1 - H),
# kept as its position within the tile times 2^32 (int32 products wrap, which drops the tile).

def load_flats(f):
    if f not in (8, 16, 32):
        raise SystemExit(f"flat resolution {f}: must be 8, 16 or 32 (or 0 for untextured)")
    data = json.loads(TEXTURES.read_text())
    pal = data["palette"]
    out = {}
    for part, name in FLAT_OF.items():
        rows, s = data["flats"][name], 64 // f
        px = [tuple(round(sum(pal[rows[yy][xx]][c] for yy in range(v * s, v * s + s) for xx in range(u * s, u * s + s))
                          / (s * s)) for c in range(3)) for v in range(f) for u in range(f)]
        centers = quantize(px, FLAT_COLORS)
        texels = [[nearest(centers, px[v * f + u]) for u in range(f)] for v in range(f)]
        c4 = [tuple(min(15, round(x / 17)) for x in c) for c in centers]
        out[part] = {"name": name, "texels": texels, "c4": c4}
    return out


def flat_word(texels, v, j):
    """Texels u = 8j .. 8j + 7 of flat row v, texel 8j + n at bits 28 - 4n."""
    return sem.i32(sum(texels[v][8 * j + n] << (28 - 4 * n) for n in range(8)))


def row_scale(y, h):
    """Flat sampling for screen row y: K with ray * K = the floor offset in 2^-32 tiles (RAW has
    the ray at 2^20), and the brightness at that distance (h / |r| tiles)."""
    r = abs(2 * y + 1 - h)
    return round(h * (1 << 12) / r), BRIGHT // (h * DS // r + FOG)


def flat_color(c4, br):
    return (pack(c4) * br) or 1


def scripted_camera(g):
    """M1's camera: circles CENTER and turns, driven by the clock."""
    T = g.clock()
    A = g.ss(g.ss(T, ">>", 1), "AND", 255, "view angle")
    B = g.ss(g.ss(T, ">>", 2), "AND", 255, "path angle")
    cos_t, sin_t = trig(math.cos), trig(math.sin)
    OX = g.ss(g.lookup_scalar(B, cos_t, 12, "cosB"), "-", 1024)
    OY = g.ss(g.lookup_scalar(B, sin_t, 12, "sinB"), "-", 1024)
    PX = g.ss(g.ss(g.ss(g.ss(OX, "*", RADIUS), ">>", 10), "+", int(CENTER[0])), "OR", 1, "px")
    PY = g.ss(g.ss(g.ss(g.ss(OY, "*", RADIUS), ">>", 10), "+", int(CENTER[1])), "OR", 1, "py")
    return A, PX, PY, {"T": T}


def slot_columns(n, hole, x_right=-2):
    """x of n slots (2 tiles wide) from x_right leftwards, skipping the substation lattice's
    columns: xs[0] is the rightmost."""
    colhole = lambda x: any(hole(x, y) for y in range(128))  # noqa: E731
    xs, x = [], x_right
    while len(xs) < n:
        if colhole(x) or colhole(x + 1):
            x -= 1
            continue
        xs.append(x)
        x -= 2
    return xs


def row_slots(nw, words, flats):
    """Slot names of a row pipeline, left to right. Each row-local network (below) must have its
    members within wire reach of each other, so the wall chain is interleaved into the end of
    the flat word deciders."""
    mux = [f"mux{j}" for j in range(nw)]
    pals = [f"pal{c}" for c in range(1, COLORS + 1)]
    if not flats:
        return ["v", "bg"] + mux + ["S", "shift", "pal15"] + pals
    pre, a = ["v", "bg"] + mux[:-1], words[:-1]
    k = min(len(pre), len(a))
    mid = a[:len(a) - k] + pre[:len(pre) - k] + [x for pair in zip(a[len(a) - k:], pre[len(pre) - k:]) for x in pair]
    return (["px", "py", "fx", "fy"] + mid + [words[-1], "sf1", "wd", mux[-1], "sf", "S", "tf", "shift"]
            + [f"fpal{c}" for c in range(FLAT_COLORS)] + pals)


def row_nets(nw, words, flats):
    """Networks within one row (lists of slot names) for the reach check."""
    mux = [f"mux{j}" for j in range(nw)]
    pals = [f"pal{c}" for c in range(1, COLORS + 1)] + (["pal15"] if not flats else [])
    fpals = [f"fpal{c}" for c in range(FLAT_COLORS)] if flats else []
    nets = [["v", "bg"] + mux + ["S"], mux + ["bg", "shift"], ["S", "shift"], ["shift"] + fpals + pals, fpals + pals]
    if flats:
        nets += [["px", "fx"], ["py", "fy"], ["fx", "sf1"] + words, ["fy"] + words, words + ["wd"], ["sf1", "sf"],
                 ["wd", "tf"], ["sf", "tf"], ["tf"] + fpals]
    return nets


def build_graph(w, h, camera=scripted_camera, g=None, hole=None, tex=DEFAULT_TEX, flat=DEFAULT_FLAT):
    """camera(g) -> (A, PX, PY, extra scalars): view angle 0..255 and an odd position (1/FP
    tiles) as scalars of g. extra may hold "angle_offset", added to A by the tables.

    The row stage and the texture lookup block are fixed nodes left of (and below) the screen,
    avoiding the substation lattice hole(x, y); the rest is placed by the compiler left of them
    (layout["x_right"])."""
    g = g or Graph()
    hole = hole or (lambda x, y: False)
    cols = LANE_SIGNALS[:w]
    vec = lambda vals, label="vec": g.const([(cols[c], v) for c, v in enumerate(vals)], label)  # noqa: E731
    ones = lambda: vec([1] * w, "ones")  # noqa: E731
    camx = [(2 * c + 1 - w) * 1024 // w for c in range(w)]
    assert all(camx)
    textures = load_textures(tex)
    NT, nw = len(textures), tex // 8
    flats = load_flats(flat) if flat else None

    # -- scalars: the camera vectors come straight out of signed lookups keyed by the view angle.
    A, PX, PY, extra = camera(g)
    # The march wants the position one tick after the angle; a later position would leave every
    # angle-derived vector waiting in delays, so delay the angle instead (shared).
    lag = max(PX.node.time, PY.node.time) - 1 - A.node.time
    if lag > 0:
        A = Scalar(g.delay(A.node, lag), A.signal)
    tabs = camera_tables(extra.pop("angle_offset", 0))
    look = lambda name, table: g.lookup_scalar(A, table, 32, name)  # noqa: E731
    PLX = look("PLX", {a: tabs[a]["PLX"] for a in range(256)})
    PLY = look("PLY", {a: tabs[a]["PLY"] for a in range(256)})
    DX10 = look("DX10", ray_base(tabs, camx, "DX", "PLX"))
    DY10 = look("DY10", ray_base(tabs, camx, "DY", "PLY"))

    # -- ray directions per column (scale 1024): RAW = camx * PL + D10 is a wire sum; its sign is
    # the ray's, and RD = RAW >> 10 is never 0 (ray_base).
    def ray(PL, D10):
        raw = [g.vs(vec(camx, "camx"), "*", PL), g.broadcast(ones(), D10)]
        return raw, g.vc(raw, ">>", 10, "ray")
    (RAWX, RDX), (RAWY, RDY) = ray(PLX, DX10), ray(PLY, DY10)

    def march(RAW, RD, RO, P, PO, side):
        """Crossings of the grid lines perpendicular to this axis, all K at once. RD: ray component
        along the axis (RAW: the same before >> 10, for its sign), RO: the other one; P/PO:
        position along / across (scale FP, odd). FR = P & 255, C = P >> 8 (the cell).

        Crossing k is at signed distance along_k = sel(256 (k + 1), -256 k) - FR along the axis
        (sel: first value where the ray goes +, second where -). From it, each one division:
          D_k = along_k * 2^17 / RD                          (DS = 2^15 per tile of ray length)
          key_k = along_k * 16 * RO / RD + 16 PO + line_k
        key_k is the map key of the cell entered (cell_keys) << 12, with the position across the
        line in its low 12 bits (the texture column). line_k = (C + k + 2) << 18 going + and
        (NEG + k + 1 - C) << 18 going - for side 0; side 1 has C + k + 1 going + and adds 1 << 12
        (its x + 1). So keys grow with k either way.

        A range lookup per step marks walls with HIT. Returns two leaf sets: HIT + D_k (the
        nearest hit has the smallest) and HIT + key_k (the same hit: the smallest key)."""
        FR = g.ss(P, "AND", FP - 1, "fr")
        C = g.ss(P, ">>", 8, "cell")
        Zps, Zns = g.new_scalar_signal(), g.new_scalar_signal()
        zp = g.ss(C, "<<", 18, "line z+", out=Zps)
        zn = g.ss(C, "*", -(1 << 18), "line z-", out=Zns)
        zap = g.ss(PO, "<<", 4, "across z+", out=Zps)
        zan = g.ss(PO, "<<", 4, "across z-", out=Zns)
        Zp = Scalar(Collect([zp.node, g.delay(zap.node, zp.node.time - zap.node.time)]), Zps)
        Zn = Scalar(Collect([zn.node, g.delay(zan.node, zn.node.time - zan.node.time)]), Zns)
        ZBp, ZBn = g.broadcast(ones(), Zp), g.broadcast(ones(), Zn)

        def sel(src, a, b, add, label):
            """Per lane: (a if src > 0 else b) + add, where add is a vector shared by all steps (on
            green, one network)."""
            copy = [sem.out(EACH, networks=GREEN)]
            return g.decider([sem.cond(EACH, ">", 0, RED)], [sem.out(EACH, constant=a)] + copy,
                             ([sem.out(EACH, constant=b)] if b else []) + copy, red=src, green=add, label=label)

        def line(src, cmp, a, add, label):
            """Per lane where src cmp 0: a + add."""
            return g.decider([sem.cond(EACH, cmp, 0, RED)], [sem.out(EACH, constant=a), sem.out(EACH, networks=GREEN)],
                             red=src, green=add, label=label)
        FR16 = g.vs(vec([-16] * w, "-16"), "*", FR, "-16 fr")
        FRD = g.vs(vec([-(DS * 4)] * w, "-DS fr"), "*", FR, "-DS fr")
        RDd = g.delay(RD, 1)
        x1 = 1 << 12 if side else 0
        hit_conds = key_ranges(wall_keys(side), 12)
        dleaves, kleaves = [], []
        for k in range(K):
            # FR16 is ready a tick after RAW, so it joins on the product's wire (one copy per step).
            along = g.decider([sem.cond(EACH, ">", 0, RED)], [sem.out(EACH, constant=4096 * (k + 1))],
                              [sem.out(EACH, constant=-4096 * k)] if k else None, red=RAW, label=f"along{side}.{k}")
            Q = g.vv(g.vv(RO, "*", [along, FR16], f"across{side}.{k}"), "/", RD, f"q{side}.{k}")
            Lp = line(RDd, ">", ((k + 2 - side) << 18) + x1, ZBp, f"line+{side}.{k}")
            Ln = line(RDd, "<", ((NEG + k + 1) << 18) + x1, ZBn, f"line-{side}.{k}")
            hit = g.decider(hit_conds, [sem.out(EACH, constant=HIT)], red=[Q, Lp, Ln], label=f"hit{side}.{k}")
            key = g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=[Q, Lp, Ln], label=f"key{side}.{k}")
            D = g.vv(sel(RDd, (k + 1) * DS * 1024, -k * DS * 1024, FRD, f"dist{side}.{k}"), "/", RD, f"d{side}.{k}")
            dleaves.append([hit, D])
            kleaves.append([hit, key])
        return dleaves, kleaves

    # Nearest hit per side: a min tree (lane-wise: copy red if smaller, else green). The key tree
    # pairs its leaves the other way round, so each hit decider feeds one red and one green network.
    def tree(level, name, swap=False):
        depth = 0
        while len(level) > 1:
            nxt = []
            for i, (a, b) in enumerate(zip(level[::2], level[1::2])):
                r, gr = (b, a) if swap and depth == 0 else (a, b)
                nxt.append(g.decider([sem.cond(EACH, "<", EACH, RED, None, GREEN)], [sem.out(EACH, networks=RED)],
                                     [sem.out(EACH, networks=GREEN)], red=r, green=gr, label=f"{name}.{depth}.{i}"))
            level = nxt + level[len(level) - len(level) % 2:]
            depth += 1
        return level[0]
    dV, kV = march(RAWX, RDX, RDY, PX, PY, 0)
    dH, kH = march(RAWY, RDY, RDX, PY, PX, 1)
    M0, M1 = tree(dV, "minV"), tree(dH, "minH")
    K0, K1 = tree(kV, "keyV", True), tree(kH, "keyH", True)
    # The last level is split by side: MV holds D on the lanes where a vertical line is nearer, MH
    # on the rest (ties). The constant 2^30 turns HIT + D into D.
    cmp = lambda op: [sem.cond(EACH, op, EACH, RED, None, GREEN)]  # noqa: E731
    MV = g.decider(cmp("<"), [sem.out(EACH, networks=RED), sem.out(EACH, constant=1 << 30)],
                   red=M0, green=M1, label="nearest V")
    MH = g.decider(cmp(">="), [sem.out(EACH, networks=GREEN), sem.out(EACH, constant=1 << 30)],
                   red=M0, green=M1, label="nearest H")

    # -- texture key per side: TK = (type + 1) << 12 | u << (12 - log2 tex), u the texture column
    # (from the key's low bits), type from range tests on the key (HIT + key).
    ustep = 4096 // tex
    def texkey(Ks, side):
        u = g.vc(Ks, "AND", 4096 - ustep, f"u{side}")
        types = [g.decider(key_ranges(wall_keys(side, ty), 12, HIT), [sem.out(EACH, constant=(ty + 1) << 12)],
                           red=Ks, label=f"type{side}.{ty}") for ty in range(NT)]
        return [u] + types
    sides = [(0, texkey(K0, 0), MV), (1, texkey(K1, 1), MH)]

    # Texture strips: one decider per (side, type, column, word), gated by the side being nearest,
    # so the words come out one tick after the split. Fixed block below the rows.
    words = [[] for _ in range(nw)]
    for j in range(nw):
        for ty, t in enumerate(textures):
            for u in range(tex):
                for side, TK, MS in sides:   # sides alternate, so each side's networks stay contiguous in the block
                    words[j].append(g.decider(
                        [sem.cond(EACH, "=", ((ty + 1) << 12) + u * ustep, RED), sem.cond(EACH, ">", 0, GREEN, "and")],
                        [sem.out(EACH, constant=strip_word(t["texels"], u, j))], red=TK, green=MS,
                        label=f"strip{side}.{t['name']}.{u}.{j}"))
    # Palettes per lane (the nearest side's texture), times brightness: one bus per texel value.
    pal = {c: [] for c in range(1, COLORS + 1)}
    for side, TK, MS in sides:
        for ty, t in enumerate(textures):
            rng = [sem.cond(EACH, ">=", (ty + 1) << 12, RED), sem.cond(EACH, "<", (ty + 2) << 12, RED, "and"),
                   sem.cond(EACH, ">", 0, GREEN, "and")]
            for c in range(1, COLORS + 1):
                if c <= len(t["c4"]):
                    pal[c].append(g.decider(rng, [sem.out(EACH, constant=side_color(t["c4"][c - 1], side))],
                                            red=TK, green=MS, label=f"pal{side}.{t['name']}.{c}"))
    BR = g.vv(vec([BRIGHT] * w, "bright"), "/", [MV, MH, vec([FOG] * w, "fog")], "brightness")
    CT = {c: g.vv(pal[c], "*", BR, f"color{c}") for c in pal}

    # -- rows. Texel row v = tex/2 + r * tex * D / (H * 2^16) for r = 2y + 1 - H, kept as
    # V = 4 (v - tex/2) = D / c_y: one division per row. The wall covers |V| < 2 tex.
    #
    # Flats: the pixel's position within its tile, times 2^32: F = RAW * K_y + (P << 24) per axis
    # (products wrap). Its top fb bits are the flat texel (u from X, v from Y); one decider per
    # word (flat row v, u >> 3) outputs 8 packed texels, and Sf = (FX >> (30 - fb)) AND 28 picks
    # the texel. Floor pixels are where the wall's texel is 15; one decider per flat color there.
    fb = flat.bit_length() - 1 if flat else 0
    words_of = [f"w{v}.{j}" for v in range(flat) for j in range(flat // 8)] if flat else []
    names = row_slots(nw, words_of, flat)
    xs = slot_columns(len(names), hole)
    X = dict(zip(reversed(names), xs))
    if flat:
        sums = [g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=raw, label=f"raw {a}")
                for a, raw in (("x", RAWX), ("y", RAWY))]
        Ps = [g.ss(P, "<<", 24, f"frac {a}") for a, P in (("x", PX), ("y", PY))]
    rows, palettes = [], []
    for y in range(h):
        r = 2 * y + 1 - h
        cy = round(h * (1 << 14) / (r * tex))
        V = g.vc([MV, MH], "/", cy, f"v{y}")
        mux = [g.decider([sem.cond(EACH, "!=", 0, GREEN), sem.cond(EACH, ">=", 4 * (8 * j - tex // 2), RED, "and"),
                          sem.cond(EACH, "<", 4 * (8 * j + 8 - tex // 2), RED, "and")],
                         [sem.out(EACH, networks=GREEN)], red=V, green=words[j], label=f"mux{y}.{j}") for j in range(nw)]
        bg = g.decider([sem.cond(EACH, "<", -2 * tex, RED), sem.cond(EACH, ">=", 2 * tex, RED, "or")],
                       [sem.out(EACH, constant=-1)], red=V, label=f"bg{y}")
        S = g.vc(V, "AND", 28, f"nibble{y}")
        T = g.vv(mux + [bg], "<<", S, f"texel{y}")
        node = {"v": V, "bg": bg, "S": S, "shift": T, **{f"mux{j}": m for j, m in enumerate(mux)}}
        pals = [g.decider(nibble_range(c), [sem.out(EACH, networks=GREEN)], red=T, green=CT[c], label=f"pal{y}.{c}")
                for c in pal]
        node.update({f"pal{c}": n for c, n in zip(pal, pals)})
        if not flat:
            fl = g.decider(nibble_range(15), [sem.out(EACH, constant=pack(background(y, h)))], red=T, label=f"floor{y}")
            node["pal15"] = fl
            pals = [fl] + pals
        else:
            part = flats["floor" if r > 0 else "ceiling"]
            ky, br = row_scale(y, h)
            # The raw rays, delayed so the flat texel lands with the wall's.
            at = T.time - 5
            if sums[0].time > at:
                raise ValueError("flat path too long for the row stage")
            RX, RY = (g.delay(n, at - n.time) for n in sums)
            pxy = [g.vc(RX, "*", ky, f"fx{y}*"), g.vc(RY, "*", ky, f"fy{y}*")]
            FX, FY = (g.vs(n, "+", P, f"f{a}{y}") for n, P, a in zip(pxy, Ps, "xy"))
            node.update({"px": pxy[0], "py": pxy[1], "fx": FX, "fy": FY})
            ws = []
            for v in range(flat):
                for j in range(flat // 8):
                    word = flat_word(part["texels"], v, j)
                    if not word:
                        continue
                    conds = top_range(j, fb - 3, RED) if fb > 3 else []
                    conds += top_range(v, fb, GREEN, "and" if conds else None)
                    n = g.decider(conds, [sem.out(EACH, constant=word)], red=FX, green=FY, label=f"flat{y}.{v}.{j}")
                    ws.append(n)
                    node[f"w{v}.{j}"] = n
            node["sf1"] = g.vc(FX, ">>", 30 - fb, f"fu{y}")
            node["sf"] = g.vc(node["sf1"], "AND", 28, f"fs{y}")
            node["wd"] = g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=ws, label=f"fword{y}")
            node["tf"] = Tf = g.vv(node["wd"], "<<", node["sf"], f"ftexel{y}")
            fpals = []
            for c, c4 in enumerate(part["c4"]):
                n = g.decider(nibble_range(15) + nibble_range(c, GREEN, "and"), [sem.out(EACH, constant=flat_color(c4, br))],
                              red=T, green=Tf, label=f"fpal{y}.{c}")
                node[f"fpal{c}"] = n
                fpals.append(n)
            pals = fpals + pals
        for name, n in node.items():
            n.fixed, n.pos = True, (X[name], y)
        rows.append(Collect(pals))
        palettes.append(pals)
        check_row_reach(X, node, nw, words_of, flat)
    # Color buses: the product and its alignment delay above their palette column.
    for c, n in CT.items():
        n.fixed, n.pos = True, (X[f"pal{c}"], -2)
        d = g.delay(n, 1)
        d.fixed, d.pos = True, (X[f"pal{c}"], -1)
    # The strip block: below the rows, filled column by column (word by word).
    block = [n for ws in words for n in ws]
    block_cols = sorted(xs)
    per_col = -(-len(block) // len(block_cols))
    for i, n in enumerate(block):
        n.fixed, n.pos = True, (block_cols[i // per_col], h + 2 + i % per_col)
    layout = {"x_right": min(xs) - 4, "palettes": palettes, "textures": textures, "tex": tex, "flats": flats,
              "flat": flat}
    return g, cols, rows, {**extra, "A": A, "PX": PX, "PY": PY, "tables": tabs}, layout


def check_row_reach(X, node, nw, words, flats):
    """Every row-local network's members, in x order, within wire reach (combinators are 2 wide)."""
    for net in row_nets(nw, words, flats):
        xs = sorted(X[n] for n in net if n in node)
        for a, b in zip(xs, xs[1:]):
            if b - a - 2 > 9:
                raise ValueError(f"row slots out of reach: {net} ({a} -> {b})")


def wire_screen(b, ent, cols, layout, w, h):
    """Lamps, each row fed by its palette deciders (whose outputs are chained)."""
    lattice = b.grid.hole
    for y, pals in enumerate(layout["palettes"]):
        es = sorted((ent[n.id][1] for n in pals), key=lambda e: -b.entities[e - 1]["position"]["x"])
        b.chain(es, 3)
        lamps = [b.lamp(x, y, {"use_colors": True, "color_mode": 2, "rgb_signal": cols[x]}, always_on=True)
                 for x in range(w) if not lattice(x, y)]
        b.wire(es[0], 3, lamps[0], 1)
        b.chain(lamps, 1)


def build(w, h, substation_quality=DEFAULT_SUBSTATION_QUALITY, tex=DEFAULT_TEX, flat=DEFAULT_FLAT):
    if w > len(LANE_SIGNALS):
        raise SystemExit(f"width {w} needs {w} lane signals, only {len(LANE_SIGNALS)}")
    b = Blueprint(f"Raycaster M3 {w}x{h}", substation_quality)
    lattice = b.grid.hole
    g, cols, rows, scal, layout = build_graph(w, h, hole=lattice, tex=tex, flat=flat)
    ent = g.compile(b, blocked=lattice, region=lambda x, y: x <= -2, x_right=layout["x_right"])
    wire_screen(b, ent, cols, layout, w, h)
    _substations(b)
    b.power()
    b.link_power()
    b.check_reach()
    return g, b, cols, rows, scal, layout


def _substations(b):
    """Substations on the lattice (see SubstationGrid) wherever they power something."""
    grid = b.grid
    tiles = b.occupied()
    xs = [x for x, _ in tiles]
    ys = [y for _, y in tiles]
    for sx in range(min(xs) - grid.period, max(xs) + grid.period + 1):
        for sy in range(min(ys) - grid.period, max(ys) + grid.period + 1):
            if (sx - grid.offset) % grid.period or (sy - grid.offset) % grid.period:
                continue
            area = range(-grid.d + 1, grid.d + 1)
            if any((sx + dx, sy + dy) in tiles for dx in area for dy in area):
                if not {(sx, sy), (sx + 1, sy), (sx, sy + 1), (sx + 1, sy + 1)} & tiles:
                    b.substation(sx, sy)


# ---------------------------------------------------------------------------------------------
# Reference images

def evaluate(g, cols, rows, T):
    outs = g.evaluate(T, rows)
    return [[o.get(sem.key(c), 0) for c in cols] for o in outs]


def ideal(g, scal, layout, w, h, T):
    """Float raycaster from the same camera scalars, textured and shaded the same way: a sanity
    check for the fixed-point math (not expected to match every pixel)."""
    vals = {k: g.evaluate(T, [scal[k].node])[0].get(sem.key(scal[k].signal), 0) for k in ("A", "PX", "PY")}
    vals.update(scal["tables"][vals["A"]])
    tex, textures, types, flats, f = layout["tex"], layout["textures"], wall_types(), layout["flats"], layout["flat"]
    px, py = vals["PX"] / FP, vals["PY"] / FP
    img = [[pack(background(y, h))] * w for y in range(h)]
    for c in range(w):
        camx = ((2 * c + 1 - w) * 1024 // w) / 1024
        rdx = (vals["DX"] + vals["PLX"] * camx) / 1024
        rdy = (vals["DY"] + vals["PLY"] * camx) / 1024
        if flats:
            for y in range(h):
                r = 2 * y + 1 - h
                d = h / abs(r)
                fx, fy = px + d * rdx, py + d * rdy
                u, v = int((fx - math.floor(fx)) * f), int((fy - math.floor(fy)) * f)
                part = flats["floor" if r > 0 else "ceiling"]
                img[y][c] = flat_color(part["c4"][part["texels"][v][u]], row_scale(y, h)[1])
        mx, my = int(px), int(py)
        ddx = abs(1 / rdx) if rdx else 1e30
        ddy = abs(1 / rdy) if rdy else 1e30
        sx, sy = (1 if rdx > 0 else -1), (1 if rdy > 0 else -1)
        tx = ((mx + 1 - px) if rdx > 0 else (px - mx)) * ddx
        ty = ((my + 1 - py) if rdy > 0 else (py - my)) * ddy
        while True:
            if tx < ty:
                d, side, tx, mx = tx, 0, tx + ddx, mx + sx
            else:
                d, side, ty, my = ty, 1, ty + ddy, my + sy
            if is_wall(MAP[my][mx]):
                break
        across = (py + d * rdy) if side == 0 else (px + d * rdx)
        u = int((across - math.floor(across)) * tex)
        t = textures[types.index(MAP[my][mx])]
        ds = d * DS
        br = int(BRIGHT / (ds + FOG))
        for y in range(h):
            v = tex / 2 + (2 * y + 1 - h) * tex * ds / (h * (1 << 16))
            if 0 <= v < tex:
                img[y][c] = br * side_color(t["c4"][t["texels"][int(v)][u] - 1], side)
    return img


def read_ppm(path):
    data = path.read_bytes()
    parts = data.split(b"\n", 3)
    w, h = map(int, parts[1].split())
    px = parts[3]
    return [[(px[3 * (y * w + x)] << 16) | (px[3 * (y * w + x) + 1] << 8) | px[3 * (y * w + x) + 2]
             for x in range(w)] for y in range(h)]


def verify(path, g, cols, rows, scal, layout, w, h, ticks, lattice, every=5):
    out_dir = ROOT / "out" / f"raycast_{w}x{h}_ppm"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.ppm"):
        f.unlink()
    subprocess.run([str(ROOT / "target" / "release" / "fsim"), "run", str(path), "--ticks", str(ticks),
                    "--ppm", str(out_dir)], check=True)
    frames = {int(p.stem): read_ppm(p) for p in sorted(out_dir.glob("*.ppm"))}
    holes = {(x, y) for x in range(w) for y in range(h) if lattice(x, y)}

    def diff(img, ref_img):
        return sum(1 for y in range(h) for x in range(w) if (x, y) not in holes and img[y][x] != ref_img[y][x])

    # Frame t shows the clock value t - lag; pick the lag that fits the last frames.
    last = sorted(frames)[-3:]
    lag = min(range(rows[0].time - 3, rows[0].time + 4),
              key=lambda k: sum(diff(frames[t], evaluate(g, cols, rows, t - k)) for t in last))
    checked = [t for t in sorted(frames) if t - lag >= 1][2::every]
    t0 = time.time()
    bad = []
    for t in checked:
        n = diff(frames[t], evaluate(g, cols, rows, t - lag))
        if n:
            bad.append((t, n))
    print(f"latency {lag} ticks; {len(checked)} frames x {w * h - len(holes)} lamps checked against the "
          f"netlist evaluation ({(time.time() - t0) / max(1, len(checked)):.1f} s each); {len(bad)} differ")
    for t, n in bad[:10]:
        print(f"  tick {t}: {n} lamps differ")
    worst = 0
    for t in checked[::max(1, len(checked) // 5)]:
        n = diff(evaluate(g, cols, rows, t - lag), ideal(g, scal, layout, w, h, t - lag))
        worst = max(worst, n)
    print(f"fixed point vs float raycaster: at most {worst} of {w * h} pixels differ "
          f"({100 * worst / (w * h):.2f}%) on sampled frames")
    return not bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="160x100")
    ap.add_argument("--ticks", type=int, default=400)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--every", type=int, default=5, help="check every n-th frame against the evaluation")
    ap.add_argument("--tex", type=int, default=DEFAULT_TEX, help="texture resolution (texels per tile)")
    ap.add_argument("--flat", type=int, default=DEFAULT_FLAT,
                    help="floor/ceiling texture resolution (texels per tile: 8, 16, 32; 0 = untextured)")
    quality_arg(ap)
    a = ap.parse_args()
    w, h = map(int, a.size.lower().split("x"))
    g, b, cols, rows, scal, layout = build(w, h, a.substation_quality, a.tex, a.flat)
    data = b.to_json()
    data["fsim"] = {"ticks": a.ticks, "doc": f"raycaster M3 {w}x{h}"}
    gen = HERE / "gen"
    gen.mkdir(exist_ok=True)
    path = gen / f"raycast_{w}x{h}.json"
    path.write_text(json.dumps(data, separators=(",", ":")))
    (gen / f"raycast_{w}x{h}.txt").write_text(encode(b.to_json()) + "\n")
    n_comb = sum(1 for e in b.entities if "combinator" in e["name"] and e["name"] != "constant-combinator")
    n_lamp = sum(1 for e in b.entities if e["name"] == "small-lamp")
    print(f"{path.relative_to(ROOT)}: {len(b.entities)} entities ({n_comb} combinators, {n_lamp} lamps, {g.relays} relay poles)")
    if a.verify and not verify(path, g, cols, rows, scal, layout, w, h, a.ticks, b.grid.hole, a.every):
        sys.exit(1)


if __name__ == "__main__":
    main()
