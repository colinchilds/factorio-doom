"""M4 raycaster: Freedoom-textured walls, floors and ceilings, monsters and items, a scripted camera, at any --size.

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
            same way (one bus per texel value), times a brightness bright / (D + fog)
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
  things    monsters and items (demos/things.py): per column the nearest thing's box column and
            depth, per row its texel; an opaque one in front of the wall kills the wall's pixel
            through V and paints its own color. Timed back from V: no latency.
  view      View: the field of view, pixel aspect and eye height are build constants (default:
            Doom's pixel aspect 1.2 and eye at 40 units, so monsters stand taller than the eye).
  hud       the status bar (demos/hud.py) in the bottom rows: a constant row plus one decider per
            (digit or face slot, glyph, row); the view above it keeps the full screen's projection.

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
from bp import DEFAULT_SUBSTATION_QUALITY, QUALITY_LANES, Blueprint, encode, quality_arg  # noqa: E402
from compiler import semantics as sem  # noqa: E402
from compiler import netlist as nl  # noqa: E402
from compiler.netlist import EACH, GREEN, LANE_SIGNALS, RED, Collect, Graph, Node, Scalar  # noqa: E402
from sprite import DEFAULT_GAMMA, DEFAULT_SATURATION, c4 as to_c4, grade, nearest, quantize  # noqa: E402
import hud as hd  # noqa: E402
import things as th  # noqa: E402
import weapons as wp  # noqa: E402

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
PLANE = 676                  # tan(fov/2) * 1024 before View (2026-11-03): about 66 degrees
HIT = -(1 << 30)
DS = 1 << 15                 # distance D: 2^15 per tile
DEFAULT_FOG = 8.0            # tiles to half brightness (4 looked murky on lamps)
NEG = 4 * len(MAP)           # line base of map keys for rays going - (keys grow with distance)
COLORS = 14                  # texels 1..14 per texture; 15 marks floor / ceiling
DEFAULT_TEX = 64             # texture resolution (texels per tile, both ways)
FLAT_OF = {"floor": "FLOOR4_8", "ceiling": "TLITE6_4"}   # Freedoom flats, one per tile
FLAT_COLORS = 16             # flat texels 0..15
DEFAULT_FLAT = 16            # flat resolution (texels per tile, both ways); 0 = untextured
DEFAULT_ROWS = 6             # screen rows per network: the qualities of a lane signal (Space Age)


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


def camera_tables(offset=0, plane=PLANE):
    """Per view angle a (0..255, plus offset): direction DX, DY (scale 1024) and camera plane
    PLX, PLY (plane: tan(fov / 2) * 1024)."""
    t = {}
    for a in range(256):
        th = 2 * math.pi * ((a + offset) % 256) / 256
        dx, dy = round(1024 * math.cos(th)), round(1024 * math.sin(th))
        t[a] = {"DX": dx, "DY": dy, "PLX": (dy * -plane) >> 10, "PLY": (dx * plane) >> 10}
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


class Shading:
    """Screen colors (build parameters; constants in the blueprint, so no combinators or ticks):
    the palette grade (sprite.grade: gamma on the brightest channel, saturation) and the distance
    to half brightness. Brightness is bright / (D + fog) <= 17, with palette channels <= 15; with
    the muzzle light (LIGHT_K per extra light level, k) (bright + k D) / (D + fog), still <= 17."""

    def __init__(self, gamma=DEFAULT_GAMMA, saturation=DEFAULT_SATURATION, fog=DEFAULT_FOG, light=None):
        if not (gamma > 0 and saturation >= 0 and 0 < fog <= 1000):
            raise SystemExit(f"shading: need gamma > 0, saturation >= 0, 0 < fog <= 1000 (got {gamma}, {saturation}, {fog})")
        light = LIGHT_K if light is None else light
        if not 0 <= 2 * light <= 17:
            raise SystemExit(f"shading: the muzzle light must be 0..8 (got {light})")
        self.gamma, self.saturation, self.light = gamma, saturation, light
        self.fog = max(1, round(fog * DS))   # in D units
        self.bright = 17 * self.fog

    def c4(self, rgb):
        return to_c4(rgb, self.gamma, self.saturation)

    def level(self, d, k=0):
        """The brightness at distance d (D units), lit by k (LIGHT_K times the extra light level)."""
        return (self.bright + k * d) // (d + self.fog)

    def rgb(self, rgb):
        """An 8-bit color graded the same way (the HUD's colors)."""
        return grade(rgb, self.gamma, self.saturation)

    @classmethod
    def from_args(cls, a):
        return cls(a.gamma, a.saturation, a.fog, a.muzzle_light)


DEFAULT_VIEW = dict(fov=None, aspect=1.2, eye=40)   # user request 2026-11-03: taller monsters
# The muzzle light (user request 2026-11-03): per extra light level (Doom's A_Light1 = 1, A_Light2
# = 2) the brightness numerator gains LIGHT_K * D, so the dark part of the light curve shrinks by
# LIGHT_K / 17 per level. 4 is Doom's (1/8 of full per level at the half-bright distance, fog), and
# it hardly showed on lamps; 6 is a bit stronger (--muzzle-light).
LIGHT_K = 6


class View:
    """The view's geometry (build parameters; constants in the blueprint, so no combinators or
    ticks; user request 2026-11-03, the monsters looked short):
      fov     the horizontal field of view, degrees (Doom: 90)
      aspect  how much taller than wide a pixel shows the world (Doom's 320x200 on a 4:3 screen:
              1.2); None: the projection height is the screen height (before 2026-11-03, a
              squat 0.83 at 16:10 with the old 66 degrees)
      eye     the eye's height above the floor in Doom units (a tile, floor to ceiling, is 128;
              Doom: 41). Before 2026-11-03, 64 (half way up the wall).
    proj(w, h) is the projection height: rows per tile at distance 1."""

    def __init__(self, fov=DEFAULT_VIEW["fov"], aspect=DEFAULT_VIEW["aspect"], eye=DEFAULT_VIEW["eye"], plane=None):
        self.plane = plane or round(1024 * math.tan(math.radians((fov or 66.85) / 2)))
        self.fov = 2 * math.degrees(math.atan(self.plane / 1024))
        self.aspect, self.eye = aspect, eye
        if not (0 < self.fov < 150 and (aspect is None or 0.2 <= aspect <= 5) and 0 < eye < 128 and eye % 2 == 0
                and eye >= 64 - 2 * th.HP):
            raise SystemExit(f"view: need 0 < fov < 150, 0.2 <= aspect <= 5 and an even eye height in "
                             f"[{64 - 2 * th.HP}, 128) (got {self.fov:.1f}, {aspect}, {eye})")

    def proj(self, w, h):
        return round(self.aspect * w * 512 / self.plane) if self.aspect else h

    def wall_offset(self, tex):
        """4 tex (1 - e): V + this is 4 times the wall's texel row from the top."""
        if (self.eye * tex) % 32:
            raise SystemExit(f"eye height {self.eye}: eye * tex ({tex}) must be a multiple of 32")
        return 4 * tex - self.eye * tex // 32

    def flat_factor(self, r):
        """Row r's floor (r > 0) or ceiling distance, in units of proj / |r| tiles: 2e or 2(1 - e)."""
        return (self.eye if r > 0 else 128 - self.eye) / 64

    def thing_eye(self):
        """The things' texel row at eye level (rows 0..HP-1 from 64 units up down to the floor)."""
        return (64 - self.eye) // 2

    @classmethod
    def from_args(cls, a):
        return cls(a.fov, None if a.aspect == 0 else a.aspect, a.eye)




def view_args(ap):
    ap.add_argument("--fov", type=float, default=DEFAULT_VIEW["fov"], help="horizontal field of view, degrees (Doom: 90)")
    ap.add_argument("--aspect", type=float, default=DEFAULT_VIEW["aspect"],
                    help="pixel aspect: how much taller than wide the world shows (Doom: 1.2; 0 = the projection "
                         "height is the screen height)")
    ap.add_argument("--eye", type=int, default=DEFAULT_VIEW["eye"],
                    help="eye height above the floor in Doom units (a tile is 128 tall; Doom: 41)")


def shading_args(ap):
    ap.add_argument("--gamma", type=float, default=DEFAULT_GAMMA,
                    help="palette gamma on the brightest channel (1 = Freedoom's colors, lower = brighter)")
    ap.add_argument("--saturation", type=float, default=DEFAULT_SATURATION, help="palette saturation (1 = unchanged)")
    ap.add_argument("--fog", type=float, default=DEFAULT_FOG, help="distance in tiles at which walls are half as bright")
    ap.add_argument("--muzzle-light", type=int, default=LIGHT_K,
                    help="brightening per extra light level while a weapon flashes (0..8; 0 = none): the dark part "
                         "of the light curve shrinks by this / 17 per level")


# ---------------------------------------------------------------------------------------------
# Textures: Freedoom wall textures, one tile = 128 x 128 texture units, box-filtered to tex x tex
# and quantized to COLORS colors each, stored at 4 bits per channel (times a brightness <= 17).

def load_textures(tex, shade):
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
        c4 = [shade.c4(c) for c in centers]
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

def load_flats(f, shade):
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
        c4 = [shade.c4(c) for c in centers]
        out[part] = {"name": name, "texels": texels, "c4": c4}
    return out


def flat_word(texels, v, j):
    """Texels u = 8j .. 8j + 7 of flat row v, texel 8j + n at bits 28 - 4n."""
    return sem.i32(sum(texels[v][8 * j + n] << (28 - 4 * n) for n in range(8)))


def row_scale(y, h, shade, proj=None, f=1, k=0):
    """Flat sampling for screen row y of a view h rows tall: K with ray * K = the floor offset in
    2^-32 tiles (RAW has the ray at 2^20), and the brightness at that distance (f proj / |r| tiles;
    proj, the projection height in rows, is h unless the HUD crops the view or View sets it; f:
    View.flat_factor, 1 with the eye half way up; k: the muzzle light, Shading.level)."""
    proj = proj or h
    r = abs(2 * y + 1 - h)
    if f == 1:
        return round(proj * (1 << 12) / r), shade.level(proj * DS // r, k)
    return round(f * proj * (1 << 12) / r), shade.level(round(f * proj * DS) // r, k)


def flat_color(c4, br):
    """A flat texel's color at brightness br (never 0: a lamp reading 0 shows its default color)."""
    return (pack(c4) or 1) * max(1, br)


def scripted_hud(g, extra):
    """Status bar values for the scripted demo, from the clock: health sweeps 0..200 (every face),
    armor 0..200 at half the rate, the current ammo 0..600 in steps of 3, the ammo list likewise."""
    T = extra["T"]
    vals = {"health": g.ss(g.ss(T, ">>", 1), "%", 201, "health"),
            "armor": g.ss(g.ss(T, ">>", 2), "%", 201, "armor"),
            "ammo": g.ss(g.ss(T, "*", 3), "%", 601, "ammo"),
            "bullets": g.ss(g.ss(T, "*", 5), "%", 401, "bullets"),
            "shells": g.ss(T, "%", 101, "shells"),
            "rockets": g.ss(g.ss(T, ">>", 1), "%", 101, "rockets"),
            "cells": g.ss(g.ss(T, "*", 7), "%", 601, "cells")}
    t = max(v.node.time for v in vals.values())
    hs = g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=[g.delay(v.node, t - v.node.time) for v in vals.values()],
              label="hud state")
    return hs, {k: v.signal for k, v in vals.items()}


def scripted_weapon(g, extra):
    """The weapon for the scripted demo: every picture in turn, 16 ticks each."""
    n = wp.picture_count()
    return g.ss(g.ss(g.ss(extra["T"], ">>", 4), "%", n), "+", 1, "weapon picture")


def scripted_camera(g):
    """M1's camera: circles CENTER and turns, driven by the clock."""
    T = g.clock()
    A = g.ss(g.ss(T, ">>", 1), "AND", 255, "view angle")
    B = g.ss(g.ss(T, ">>", 2), "AND", 255, "path angle")
    cos_t, sin_t = trig(math.cos), trig(math.sin)
    OX = g.ss(g.lookup_scalar(B, cos_t, 12, "cosB"), "-", 1024)
    OY = g.ss(g.lookup_scalar(B, sin_t, 12, "sinB"), "-", 1024)
    X0 = g.ss(g.ss(g.ss(OX, "*", RADIUS), ">>", 10), "+", int(CENTER[0]), "x0")
    Y0 = g.ss(g.ss(g.ss(OY, "*", RADIUS), ">>", 10), "+", int(CENTER[1]), "y0")
    PX, PY = g.ss(X0, "OR", 1, "px"), g.ss(Y0, "OR", 1, "py")
    return A, PX, PY, {"T": T, "pos0": (X0, Y0, (0, 0))}


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


ROLE_GROUPS = [   # a chunk's columns, left to right toward the screen (prefix, ...); k...: its constants
    ("kky", "fxp", "fyp", "fx", "fy", "w", "fu", "fs", "wd", "tf", "kones", "fpal", "kbr", "lb", "fc", "kbg", "sel"),
    ("wk",),
    ("kcy", "v", "s", "mux", "e1", "kcsq", "tq", "tp", "tom", "kill", "kcs", "tv", "tnib", "tmux", "tt", "t"),
    ("e2", "pal"),
    ("wc",),
]


def role_key(name):
    """Sort key of a chunk role: its group and prefix (ROLE_GROUPS), then its numbers."""
    import re
    m = re.match(r"([a-z]+)(.*)", name)
    pre, rest = m.group(1), m.group(2)
    nums = tuple(int(x) for x in re.findall(r"\d+", rest))
    if pre == "tpal":   # woven with the wall palettes: one output network, two input networks
        return (3, 1, nums, 1)
    if pre == "wk":     # the first picture's kill next to V (most chunks have few)
        return (1, 0, tuple(-x for x in nums), 0)
    for gi, grp in enumerate(ROLE_GROUPS):
        if name in grp:
            return (gi, grp.index(name), (), 0)
        if pre in grp:
            return (gi, grp.index(pre), nums, 0)
    raise ValueError(f"unknown chunk role {name}")


def chunk_roles(units, k):
    """{role: (column, row)} shared by every view chunk: ROLE_GROUPS left to right, each group's
    roles filling columns of k rows, with an empty column after each group (room for relay poles)."""
    present = {r for u in units for r in u if not r.startswith("hud")}
    pos, c = {}, 0
    for gi in range(len(ROLE_GROUPS)):
        grp = sorted((r for r in present if role_key(r)[0] == gi), key=role_key)
        for i, r in enumerate(grp):
            pos[r] = (c + i // k, i % k)
        if grp:
            c += -(-len(grp) // k) + 1
    return pos, c


MARCH_COLS = ["fr16", "along", "across", "q", "lp", "ln", "hit", "key", "dist", "d"]


def place_march(mg, x_right, y_bottom, hole):
    """The march as a block ending at x_right, rows up to y_bottom: per side a header row (the
    vectors every step shares) and one row per step, its ops left to right in MARCH_COLS; then
    the min trees (distance and key levels interleaved, each node between its two inputs), the side
    split and the texture keys. Returns the block's left x."""
    ncols = len(MARCH_COLS) + 2 * len(mg["trees"]["minV"]) + 2
    xs = slot_columns(ncols, hole, x_right)[::-1]   # left to right
    rows_side = K + 2
    y0 = y_bottom - 2 * rows_side
    for side, (dn, kn) in enumerate((("minV", "keyV"), ("minH", "keyH"))):
        top = y0 + side * rows_side
        for c, name in enumerate(MARCH_COLS):
            hd = mg[side]["head"].get(name)
            if hd is not None and not hd.fixed:
                hd.fixed, hd.pos = True, (xs[c], top)
            for kk, st in enumerate(mg[side]["steps"]):
                st[name].fixed, st[name].pos = True, (xs[c], top + 1 + kk)
        prev = {dn: [top + 1 + kk for kk in range(K)], kn: [top + 1 + kk for kk in range(K)]}
        for lv in range(len(mg["trees"][dn])):
            for ti, tn in enumerate((dn, kn)):
                c = len(MARCH_COLS) + 2 * lv + ti
                ys = []
                for i, nd in enumerate(mg["trees"][tn][lv]):
                    y = round((prev[tn][2 * i] + prev[tn][2 * i + 1]) / 2)
                    while any(y == q for q in ys):
                        y += 1
                    ys.append(y)
                    nd.fixed, nd.pos = True, (xs[c], y)
                prev[tn] = ys
        c = len(MARCH_COLS) + 2 * len(mg["trees"][dn])
        mg["split"][side].fixed, mg["split"][side].pos = True, (xs[c], prev[dn][0] + (2 if side == 0 else -2))
        for i, nd in enumerate(mg["texkey"][side]):
            nd.fixed, nd.pos = True, (xs[c + 1], prev[kn][0] - 2 + i)
    return xs[0]


def place_chunks(g, w, chunks, units, area, lookups, hole, k, mgrid=None):
    """Fixed positions left of the screen. Chunk m's block is k rows tall at y = k m (so blocks may
    drift a row or two from their lamps when a chunk is shorter than k); its nodes take the same
    (column, row) per role in every chunk (chunk_roles), so a bus's consumers line up k rows
    apart. Status bar units (roles "hud...") fill columns from the screen leftwards. `area` nodes
    (spreads, per-color products, delays) go to the free spot nearest their consumers outside the
    blocks (above them, mostly); the lookup block above those. Returns x_right for the compiler's
    own placement (left of everything here)."""
    pos, width = chunk_roles(units, k)
    hud_w = max([-(-len(u) // k) for u in units if any(r.startswith("hud") for r in u)] or [0])
    xs = slot_columns(max(width, hud_w), hole)   # xs[0] next to the screen
    for m, u in enumerate(units):
        y0 = chunks[0][0] + k * m
        if any(r.startswith("hud") for r in u):
            for i, nd in enumerate(u.values()):
                nd.fixed, nd.pos = True, (xs[i // k], y0 + i % k)
            continue
        for r, nd in u.items():
            c, row = pos[r]
            nd.fixed, nd.pos = True, (xs[width - 1 - c], y0 + row)
    band = (min(xs), chunks[0][0], 0, chunks[0][0] + k * len(units))   # x0, y0, x1, y1 (exclusive)
    taken = {t for n in g.nodes if n.fixed and n.pos for t in _tiles(n)}
    slots = set(xs)   # inside the band only free role slots, not the gap columns (relay poles)
    consumers = {}
    for n in g.nodes:
        for s in n.red + n.green:
            for m in (s.nodes if isinstance(s, Collect) else [s]):
                consumers.setdefault(m.id, []).append(n)
    ok = lambda x, y: ((x in slots or not (band[0] - 1 <= x < band[2] and band[1] <= y < band[3]))  # noqa: E731
                       and x <= -2 and not hole(x, y) and not hole(x + 1, y)
                       and (x, y) not in taken and (x + 1, y) not in taken)
    top = 0
    for nd in sorted(area, key=lambda n: -(n.time or 0)):
        cs = [c.pos for c in consumers.get(nd.id, []) if c.pos]
        if not cs:
            raise ValueError(f"{nd} has no placed consumer")
        tx = sum(p[0] for p in cs) / len(cs)
        ty = min(p[1] for p in cs)
        best = None
        for r in range(0, 400):
            for dx in range(-r, r + 1):
                for dy in (-(r - abs(dx)), r - abs(dx)):
                    x, y = round(tx) + dx, round(ty) + dy
                    if ok(x, y):
                        best = (x, y)
                        break
                if best:
                    break
            if best:
                break
        nd.fixed, nd.pos = True, best
        taken |= {best, (best[0] + 1, best[1])}
        top = min(top, best[1])
        # Its constants (a combinator each) on the nearest free tiles.
        for c in nd.red + nd.green:
            if getattr(c, "kind", None) != "const" or c.fixed:
                continue
            spot = min(((best[0] + dx, best[1] + dy) for dx in range(-8, 10) for dy in range(-8, 9)
                        if (best[0] + dx, best[1] + dy) not in taken and not hole(best[0] + dx, best[1] + dy)
                        and best[0] + dx <= -2),
                       key=lambda p: abs(p[0] - best[0]) + abs(p[1] - best[1]))
            c.fixed, c.pos = True, spot
            taken.add(spot)
    y_area = top
    # Lookup block above: the sprite lookups in its bottom rows (near the things' other consumers),
    # then the strips column by column (each word bus a band of columns reaching down to its spreads).
    cols_ = sorted(xs)
    first, rest = lookups
    for i, nd in enumerate(first):
        nd.fixed, nd.pos = True, (cols_[i % len(cols_)], top - 2 - i // len(cols_))
    top -= -(-len(first) // len(cols_))
    per_col = -(-len(rest) // len(cols_))
    for i, nd in enumerate(rest):
        nd.fixed, nd.pos = True, (cols_[i // per_col], top - 2 - i % per_col)
    x_right = min(xs) - 4
    if mgrid:
        x_right = place_march(mgrid, x_right - 2, y_area, hole) - 4
    return x_right


def _tiles(n):
    x, y = n.pos
    return {(x, y), (x + 1, y)} if n.kind != "const" else {(x, y)}


def q_signal(s, quality):
    """Signal s at a quality (Space Age): rows of a chunk are qualities of their columns' lanes."""
    return dict(s, quality=quality) if quality != "normal" else s


def build_graph(w, h, camera=scripted_camera, g=None, hole=None, tex=DEFAULT_TEX, flat=DEFAULT_FLAT, things=None,
                shade=None, hud=None, weapon=None, rows_per_net=None, view=None):
    """camera(g) -> (A, PX, PY, extra scalars): view angle 0..255 and an odd position (1/FP
    tiles) as scalars of g. extra may hold "angle_offset", added to A by the tables, and
    "angle_sub": A counts 2^sub steps per view angle (0..(256 << sub) - 1). It must hold
    "pos0" = (X0, Y0, (ox, oy)) for things: the position as early as possible, PX ~ X0 + ox.
    things: [(x, y, frame)] (demos/things.py), or None. shade: Shading (colors, fog).
    hud(g, extra) -> (node, {"health": signal, "ammo": signal, "armor": signal}): the status bar's
    values (demos/hud.py), or None for no bar. The bar takes the bottom rows; the view above it
    keeps the full screen's projection (proj = h), so it is cropped, not squashed.
    weapon(g, extra) -> Scalar: the player's weapon picture (a composite id of demos/weapons.py),
    or None. A compositor per view row (one more tick) puts it in front of everything.
    extra may hold "light": light(g, extra) -> (key Scalar, {key: extra light level}), the muzzle
    light (Shading.level): walls, flats and things brighten while the key's level is nonzero.
    view: View (field of view, pixel aspect, eye height).

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
    shade = shade or Shading()
    textures = load_textures(tex, shade)
    NT, nw = len(textures), tex // 8
    flats = load_flats(flat, shade) if flat else None
    bar = hd.Hud(w, h, shade) if hud else None
    view = view or View()
    proj, hv = view.proj(w, h), h - (bar.hb if bar else 0)   # projection height; view rows
    arms = wp.Weapons(w, hv, shade, bar=bool(bar)) if weapon else None

    # -- scalars: the camera vectors come straight out of signed lookups keyed by the view angle.
    A, PX, PY, extra = camera(g)
    # Things: extra may add "frames" (more sprite frames to load), "frame" (things.column_stage)
    # and "aim" (aim(g, col, D, lane): hitscan nodes, combat.py).
    names = {n for _, _, n in things or ()} | set(extra.pop("frames", ()))
    sprites = th.load(sorted(names), shade) if things else None
    tcolors = len(sprites["colors"]) if things else 0
    frame, aim = extra.pop("frame", None), extra.pop("aim", None)
    # The march wants the position one tick after the angle; a later position would leave every
    # angle-derived vector waiting in delays, so delay the angle instead (shared).
    lag = max(PX.node.time, PY.node.time) - 1 - A.node.time
    if lag > 0:
        A = Scalar(g.delay(A.node, lag), A.signal)
    tabs = camera_tables(extra.pop("angle_offset", 0), view.plane)
    # The muzzle light: LK = LIGHT_K * level (walls: + LK * D on the brightness numerator; flats: a
    # decider per level and chunk) and LS = 512 / LK (things: + Dt / LS, Dt being 512 times their D).
    light = extra.pop("light", None)
    LK = LS = None
    if light:
        lkey, ltab = light(g, extra)
        lks = {k: shade.light * v for k, v in ltab.items() if v and shade.light}
        if lks:
            LK = g.lookup_scalar(lkey, lks, max(lks.values()).bit_length(), "light")
            # Rounded up, so Dt / LS <= LK * Dt / 512 and the brightness stays <= 17.
            LS = g.lookup_scalar(lkey, {k: -(-512 // v) for k, v in lks.items()},
                                 (-(-512 // min(lks.values()))).bit_length(), "light step")
    pos0 = extra.pop("pos0", None)
    hide = extra.pop("hide", None)   # hide(g, i, out): thing i's hide flag (picked-up items), see things.py
    # A may count in 2^sub steps per view angle (finer turning); the tables key on A directly.
    sub = extra.pop("angle_sub", 0)
    # A may also run a little outside one turn (angle_range: game.py keeps R wrapped near it).
    lo, hi = extra.pop("angle_range", (0, 256 << sub))
    look = lambda name, table: g.lookup_scalar(A, {k: table[(k % (256 << sub)) >> sub] for k in range(lo, hi)}, 32, name)  # noqa: E731
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
        c16 = vec([-16] * w, "-16")
        FRD = g.vs(vec([-(DS * 4)] * w, "-DS fr"), "*", FR, "-DS fr")
        RDd = g.delay(RD, 1)
        x1 = 1 << 12 if side else 0
        hit_conds = key_ranges(wall_keys(side), 12)
        dleaves, kleaves, steps = [], [], []
        for k in range(K):
            # -16 FR is ready a tick after RAW, so it joins on the product's wire (one copy per step).
            fr16 = g.vs(c16, "*", FR, f"-16 fr{side}.{k}")
            along = g.decider([sem.cond(EACH, ">", 0, RED)], [sem.out(EACH, constant=4096 * (k + 1))],
                              [sem.out(EACH, constant=-4096 * k)] if k else None, red=RAW, label=f"along{side}.{k}")
            across = g.vv(RO, "*", [along, fr16], f"across{side}.{k}")
            Q = g.vv(across, "/", RD, f"q{side}.{k}")
            Lp = line(RDd, ">", ((k + 2 - side) << 18) + x1, ZBp, f"line+{side}.{k}")
            Ln = line(RDd, "<", ((NEG + k + 1) << 18) + x1, ZBn, f"line-{side}.{k}")
            hit = g.decider(hit_conds, [sem.out(EACH, constant=HIT)], red=[Q, Lp, Ln], label=f"hit{side}.{k}")
            key = g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=[Q, Lp, Ln], label=f"key{side}.{k}")
            dist = sel(RDd, (k + 1) * DS * 1024, -k * DS * 1024, FRD, f"dist{side}.{k}")
            D = g.vv(dist, "/", RD, f"d{side}.{k}")
            dleaves.append([hit, D])
            kleaves.append([hit, key])
            steps.append({"fr16": fr16, "along": along, "across": across, "q": Q, "lp": Lp, "ln": Ln, "hit": hit,
                          "key": key, "dist": dist, "d": D})
        mgrid[side] = {"steps": steps, "head": {"fr16": c16, "lp": ZBp, "ln": ZBn, "dist": FRD, "q": RD, "d": RDd}}
        return dleaves, kleaves

    # Nearest hit per side: a min tree (lane-wise: copy red if smaller, else green). The key tree
    # pairs its leaves the other way round, so each hit decider feeds one red and one green network.
    mgrid = {}

    def tree(level, name, swap=False):
        depth = 0
        levels = mgrid.setdefault("trees", {}).setdefault(name, [])
        while len(level) > 1:
            nxt = []
            levels.append(nxt)
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
    # Rows come in chunks of k screen rows on one network: row q of a chunk is quality q of the
    # column's lane signal (Space Age). Per-column vectors reach the other qualities through
    # quality transfers (spread, one tick), so V lands one tick after the split's tick + 1.
    k = rows_per_net or DEFAULT_ROWS
    Q = QUALITY_LANES[:k]
    sp = 1 if k > 1 else 0
    t_V = MV.time + 1 + sp   # the chunks' V
    if things:
        col = th.column_stage(g, w, vec, camx, look, tabs, pos0, things, sprites, t_V - sp, shade, hide, frame,
                              view.plane, extra.pop("thing_pos", None), LS)
        if aim:
            aim(g, col, [MV, MH], cols[w // 2])

    # -- texture key per side: TK = (type + 1) << 12 | u << (12 - log2 tex), u the texture column
    # (from the key's low bits), type from range tests on the key (HIT + key).
    ustep = 4096 // tex
    def texkey(Ks, side):
        u = g.vc(Ks, "AND", 4096 - ustep, f"u{side}")
        types = [g.decider(key_ranges(wall_keys(side, ty), 12, HIT), [sem.out(EACH, constant=(ty + 1) << 12)],
                           red=Ks, label=f"type{side}.{ty}") for ty in range(NT)]
        return [u] + types
    sides = [(0, texkey(K0, 0), MV), (1, texkey(K1, 1), MH)]
    mgrid["split"] = [MV, MH]
    mgrid["texkey"] = [sides[0][1], sides[1][1]]

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
    # The muzzle light's product (LK * D) makes the brightness a tick later; the palettes wait for
    # it on their inputs (four delays, not one per color bus). The spread had a tick of slack.
    pal = {c: [] for c in range(1, COLORS + 1)}
    n_lit = len(g.nodes)
    psides = sides
    if LK:
        psides = [(side, g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=TK, label=f"delay texkey{side}"),
                   g.delay(MS, 1)) for side, TK, MS in sides]
    for side, TK, MS in psides:
        for ty, t in enumerate(textures):
            rng = [sem.cond(EACH, ">=", (ty + 1) << 12, RED), sem.cond(EACH, "<", (ty + 2) << 12, RED, "and"),
                   sem.cond(EACH, ">", 0, GREEN, "and")]
            for c in range(1, COLORS + 1):
                if c <= len(t["c4"]):
                    pal[c].append(g.decider(rng, [sem.out(EACH, constant=side_color(t["c4"][c - 1], side))],
                                            red=TK, green=MS, label=f"pal{side}.{t['name']}.{c}"))
    LD = [g.vs([MV, MH], "*", LK, "lit distance")] if LK else []
    BR = g.vv([vec([shade.bright] * w, "bright")] + LD, "/", [MV, MH, vec([shade.fog] * w, "fog")], "brightness")
    CT = {c: g.vv(pal[c], "*", BR, f"color{c}") for c in pal}

    # -- chunks: k screen rows per network, row q of a chunk on quality q of its columns' lanes.
    #
    # Walls: texel row v = tex/2 + r * tex * D / (proj * 2^16) for r = 2y + 1 - hv, kept as
    # V = 4 (v - tex/2) = D / c_y (c_y a constant vector over the chunk's lanes); the wall covers
    # |V| < 2 tex. A mux per word, a shift by V AND 28, one palette decider per texel value.
    # Out-of-wall lanes get no texel; the flat select (bounded out-of-wall V) copies the flat color.
    #
    # Flats: the pixel's position within its tile, times 2^32: F = RAW * K_y + (P << 24) per axis
    # (products wrap). Its top fb bits are the flat texel (u from X, v from Y); one decider per
    # word (flat row v, u >> 3) outputs 8 packed texels, Sf = (FX >> (30 - fb)) AND 28 picks the
    # texel, one decider per flat color gives its color, times the row's brightness (a constant
    # vector: the distance is proj / |r|). All from RAW, early: it's ready with V.
    #
    # Things and the weapon kill a pixel by adding KILL to V (no wall mux or flat select fires)
    # and paint their own colors at the palette tick. The weapon also kills the things' texel path
    # (its Vs, read on green, so one kill decider feeds both).
    if hv % 2:
        raise SystemExit(f"view height {hv}: must be even (the horizon falls between two rows)")
    lane = lambda x, q: q_signal(cols[x], Q[q])  # noqa: E731
    half = hv // 2
    split = lambda lo, hi: [(y, min(k, hi - y)) for y in range(lo, hi, k)]  # noqa: E731
    chunks = split(0, half) + split(half, hv)

    def cv(ch, f, label):
        """A constant over chunk ch's lanes: f(x, y) on column x of row y."""
        y0, n = ch
        return g.const([(lane(x, q), f(x, y0 + q)) for q in range(n) for x in range(w)], label)

    def at(src, t):
        if isinstance(src, (list, tuple)):
            return Collect([at(x, t) for x in src])
        return g.delay(src, t - src.time)
    bus_nodes = []

    def bus(src, t, label):
        """src's lanes on every quality (k > 1), landing at t."""
        n0 = len(g.nodes)
        out = g.spread(at(src, t - 1), Q, label) if sp else at(src, t)
        bus_nodes.append((label, g.nodes[n0:]))
        return out

    t_P = t_V + 3   # the palettes' output: the lamps' tick
    n_area = len(g.nodes)
    D6 = bus([MV, MH], t_V - 1, "D")
    W6 = [bus(words[j], t_V, f"word{j}") for j in range(nw)]
    CT6 = {c: bus(CT[c], t_P - 1, f"color{c}") for c in CT}
    if flat:
        fb = flat.bit_length() - 1
        sums = [g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=raw, label=f"raw {a}")
                for a, raw in (("x", RAWX), ("y", RAWY))]
        R6 = [bus(n, t_V - 7, f"raw {a}") for n, a in zip(sums, "xy")]
        Ps = [g.ss(P, "<<", 24, f"frac {a}") for a, P in (("x", PX), ("y", PY))]
    if things:
        Dt6 = bus(col["Dt"], t_V - 4, "thing depth")
        M6 = bus(col["masks"], t_V - 2, "thing mask")
        WB6 = [bus(n, t_V, f"thing word{j}") for j, n in enumerate(col["wsum"])]
        NSD6 = g.vc(Dt6, "/", -512, "-thing depth")
        # Thing brightness where it is in front of the wall, then per color.
        DD = [MV, MH, at(col["NSD"], MV.time)]
        BRS = at(col["BRS"], MV.time)
        VB = g.decider([sem.cond(EACH, ">", 0, GREEN)], [sem.out(EACH, networks=RED)], red=BRS, green=DD, label="thing visible")
        VB6 = bus(VB, t_P - 2, "thing visible")
        SC6 = {c: g.vc(VB6, "*", th.color(c4, 1), f"thing color{c}") for c, c4 in enumerate(sprites["colors"], 1)}
    if arms:
        WF = weapon(g, extra)
        KILL_W = at(WF.node, t_V - 1)
        COLOR_W = at(WF.node, t_P - 1)
    nib = lambda c, wire=RED, first=None: nibble_range(c, wire, first)  # noqa: E731
    lim = th.KILL >> 1   # V outside +-lim is a killed pixel
    # The wall covers -OV <= V < 4 tex - OV (OV = 2 tex with the eye half way up); V + OV is 4 times
    # the texel row from the top.
    OV = view.wall_offset(tex)
    out_of_wall = [sem.cond(EACH, "<", -OV, RED), sem.cond(EACH, ">=", -lim, RED, "and"),
                   sem.cond(EACH, ">=", 4 * tex - OV, RED, "or"), sem.cond(EACH, "<", lim, RED, "and")]
    # Things: texel row t (0..HP-1, from 64 units up to the floor) = te + Vs / 4 below the eye and
    # te - 1 + Vs / 4 above it (Vs < 0 there, truncated toward 0), te = View.thing_eye(). Rows above
    # the eye show things only when te > 0, and only as far up as a thing's top can reach (at NEAR).
    te = view.thing_eye()
    thing_up = lambda y: te > 0 and (hv - 1 - 2 * y) <= 2 * proj * te * (1 << 18) / (th.STEX * th.NEAR)  # noqa: E731
    rows, units = [], []
    for ch in chunks:
        y0, n = ch
        floor = y0 >= half
        node = {}
        cyf = lambda x, y: round(proj * (1 << 14) / ((2 * y + 1 - hv) * tex))  # noqa: E731
        node["kcy"] = cv(ch, cyf, "c_y")
        V = g.vv(D6, "/", node["kcy"], f"v{y0}")
        node["v"] = V
        kills, wk = [], []
        if arms:
            for cid, px in sorted(arms.chunk(y0, n).items()):
                kv = [sem.out(lane(x, y - y0), constant=th.KILL) for x, y, _ in px]
                wk.append(Node(g, "decider", sem.decider_spec([sem.cond(WF.signal, "=", cid, RED)], kv),
                               red=[KILL_W], time=t_V, label=f"weapon kill{y0}.{cid}"))
            node.update({f"wk{i}": d for i, d in enumerate(wk)})
        tchunk = things and (floor or thing_up(y0 + n - 1))
        toff = te - (0 if floor else 1)   # t = toff + Q
        if tchunk:
            node["kcsq"] = cv(ch, lambda x, y: 4 * th.row_divisor(y, hv, proj), "4 c_s")
            Qn = g.vv(Dt6, "/", node["kcsq"], f"tq{y0}")
            # P = 2^t (0 for t < 0 or t >= 32): the offset rides on P's input wire.
            qin = [Qn] + ([cv(ch, lambda x, y: toff, "t offset")] if toff else [])
            P2 = g.op("arith", sem.arith_spec(2, "^", EACH, EACH, None, RED), red=qin, label=f"tp{y0}")
            OM = g.vv(M6, "*", P2, f"tom{y0}")
            kill = g.decider([sem.cond(EACH, "<", 0, RED), sem.cond(EACH, ">", 0, GREEN, "and")],
                             [sem.out(EACH, constant=th.KILL)], red=OM, green=[D6, NSD6], label=f"kill{y0}")
            node.update({"tq": Qn, "tp": P2, "tom": OM, "kill": kill})
            kills.append(kill)
        Vk = [V] + kills + wk
        mux = [g.decider([sem.cond(EACH, "!=", 0, GREEN), sem.cond(EACH, ">=", 32 * j - OV, RED, "and"),
                          sem.cond(EACH, "<", 32 * j + 32 - OV, RED, "and")],
                         [sem.out(EACH, networks=GREEN)], red=Vk, green=W6[j], label=f"mux{y0}.{j}") for j in range(nw)]
        if OV % 32:   # (V + OV) AND 28: the offset (mod 32) rides on the shift's green input
            S = g.op("arith", sem.arith_spec(EACH, "AND", 28, EACH), red=Vk,
                     green=[cv(ch, lambda x, y: OV % 32, "wall offset")], label=f"nibble{y0}")
        else:
            S = g.vc(Vk, "AND", 28, f"nibble{y0}")
        T = g.vv(mux, "<<", S, f"texel{y0}")
        node.update({"s": S, "t": T, **{f"mux{j}": m for j, m in enumerate(mux)}})
        pals = [g.decider(nib(c), [sem.out(EACH, networks=GREEN)], red=T, green=CT6[c], label=f"pal{y0}.{c}") for c in CT]
        node.update({f"pal{c}": p for c, p in zip(CT, pals)})
        out = list(pals)
        # The floor or ceiling where V is out of the wall.
        if flat:
            part = flats["floor" if floor else "ceiling"]
            ff = view.flat_factor(1 if floor else -1)
            KY = node["kky"] = cv(ch, lambda x, y: row_scale(y, hv, shade, proj, ff)[0], "k_y")
            FXp, FYp = (g.vv(R, "*", KY, f"f{a}{y0}*") for R, a in zip(R6, "xy"))
            FX, FY = (g.vs(n, "+", Pa, f"f{a}{y0}") for n, Pa, a in zip((FXp, FYp), Ps, "xy"))
            node.update({"fxp": FXp, "fyp": FYp, "fx": FX, "fy": FY})
            ws = []
            for v in range(flat):
                for j in range(flat // 8):
                    word = flat_word(part["texels"], v, j)
                    if word:
                        conds = (top_range(j, fb - 3, RED) if fb > 3 else []) + top_range(v, fb, GREEN, "and" if fb > 3 else None)
                        ws.append(g.decider(conds, [sem.out(EACH, constant=word)], red=FX, green=FY, label=f"flat{y0}.{v}.{j}"))
                        node[f"w{v}.{j}"] = ws[-1]
            sf = g.vc(g.vc(FX, ">>", 30 - fb, f"fu{y0}"), "AND", 28, f"fs{y0}")
            wd = g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=ws, label=f"fword{y0}")
            Tf = g.vv(wd, "<<", sf, f"ftexel{y0}")
            ones = node["kones"] = cv(ch, lambda x, y: 1, "ones")
            fci = [g.decider(nib(c) + [sem.cond(EACH, "!=", 0, GREEN, "and")], [sem.out(EACH, constant=pack(c4) or 1)],
                             red=Tf, green=ones, label=f"fpal{y0}.{c}") for c, c4 in enumerate(part["c4"])]
            kbr = lambda x, y, k=0: max(1, row_scale(y, hv, shade, proj, ff, k)[1])  # noqa: E731
            node["kbr"] = cv(ch, kbr, "flat brightness")
            # The muzzle light: per level a decider adds the brightness gained on that row.
            lbs = []
            for lk in sorted(set(lks.values())) if LK else ():
                gain = [(lane(x, q), kbr(x, y0 + q, lk) - kbr(x, y0 + q)) for q in range(n) for x in range(w)]
                gain = [sem.out(s_, constant=v) for s_, v in gain if v]
                if gain:
                    tf = fci[0].time
                    lbs.append(Node(g, "decider", sem.decider_spec([sem.cond(LK.signal, "=", lk, RED)], gain),
                                    red=[at(LK.node, tf - 1)], time=tf, label=f"flat light{y0}.{lk}"))
            node.update({f"lb{i}": d for i, d in enumerate(lbs)})
            FC = g.vv(fci, "*", [node["kbr"]] + lbs, f"flat{y0}")
            node.update({"fu": sf.red[0], "fs": sf, "wd": wd, "tf": Tf, "fc": FC, **{f"fpal{c}": d for c, d in enumerate(fci)}})
        else:
            FC = node["kbg"] = cv(ch, lambda x, y: pack(background(y, hv)), "background")
        sel = g.decider(out_of_wall, [sem.out(EACH, networks=GREEN)], red=Vk, green=FC, label=f"flat select{y0}")
        # Its output crosses the weapon kills on their green inputs (which they don't read) to e1.
        e1 = g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, GREEN), green=[sel], label=f"delay flat select{y0}")
        e2 = g.delay(e1, 1)
        for d in wk:
            d.green, d.nominal = [sel], "green"
        node.update({"sel": sel, "e1": e1, "e2": e2})
        out.append(e2)
        if tchunk:
            # The thing's texel row: Vs = Dt / cs_y on green, where the weapon's kill joins it, and
            # the offset making Vs + 4 toff + (3 above the eye) four times the row t.
            node["kcs"] = cv(ch, lambda x, y: th.row_divisor(y, hv, proj), "c_s")
            Vs = g.op("arith", sem.arith_spec(EACH, "/", EACH, EACH, GREEN, RED),
                      red=[node["kcs"]], green=[at(Dt6, t_V - 1)], label=f"tv{y0}")
            vo = 4 * toff + (0 if floor else 3)
            Vsk = [Vs] + wk + ([cv(ch, lambda x, y: vo, "texel offset")] if vo else [])
            tm = [g.decider([sem.cond(EACH, "!=", 0, RED), sem.cond(EACH, ">=", 32 * j, GREEN, "and"),
                             sem.cond(EACH, "<", 32 * j + 32, GREEN, "and")], [sem.out(EACH, networks=RED)],
                            red=WB6[j], green=Vsk, label=f"tmux{y0}.{j}") for j in range(th.HP // 8)]
            tS = g.op("arith", sem.arith_spec(EACH, "AND", 28, EACH, GREEN), green=Vsk, label=f"tnib{y0}")
            tT = g.vv(tm, "<<", tS, f"ttexel{y0}")
            tp = [g.decider(nib(c), [sem.out(EACH, networks=GREEN)], red=tT, green=SC6[c], label=f"tpal{y0}.{c}")
                  for c in SC6]
            node.update({"tv": Vs, "tnib": tS, "tt": tT, **{f"tmux{j}": m for j, m in enumerate(tm)},
                         **{f"tpal{c}": p for c, p in zip(SC6, tp)}})
            out += tp
        if arms:
            wc = []
            for i, (cid, px) in enumerate(sorted(arms.chunk(y0, n).items())):
                wc.append(Node(g, "decider", sem.decider_spec([sem.cond(WF.signal, "=", cid, RED)],
                                                              [sem.out(lane(x, y - y0), constant=v) for x, y, v in px]),
                               red=[COLOR_W], time=t_P, label=f"weapon{y0}.{cid}"))
            node.update({f"wc{i}": d for i, d in enumerate(wc)})
            out += wc
        for o in out:
            if o.time != t_P:
                raise ValueError(f"{o.label} lands at {o.time}, the palettes at {t_P}")
        rows.append(Collect(out))
        units.append(node)

    # The status bar: chunks of its rows, landing with the view.
    bg = {}
    if bar:
        hs, sigs = hud(g, extra)
        bchunks = split(hv, h)
        hrows, hunits, hbg = bar.build_chunks(g, hs, sigs, lane, t_P, bchunks)
        rows += hrows
        units += hunits
        chunks += bchunks
        bg = dict(zip(range(len(chunks) - len(bchunks), len(chunks)), hbg))
        extra["hud"] = (hs, sigs)
    if arms:
        extra["weapon"] = WF
    if LK:
        extra["light"] = LK
    lookups = (col["lookups"] if things else [], [n for ws in words for n in ws])
    area = [n for n in g.nodes[n_area:] if n.kind != "const" and not any(n in u.values() for u in units)]
    area += [n for _, ns in bus_nodes for n in ns] + list(CT.values()) + [BR] + LD + [n for ps in pal.values() for n in ps]
    area += [n for n in g.nodes[n_lit:n_area] if n.kind != "const"]
    if things:
        area += col["wsum"] + [col["NSD"], col["BRS"]] + col["lit"]
    area = list({n.id: n for n in area if n.kind != "const"}.values())
    x_right = place_chunks(g, w, chunks, units, area, lookups, hole, k, mgrid)
    layout = {"x_right": x_right, "textures": textures, "tex": tex, "flats": flats, "flat": flat, "things": things,
              "sprites": sprites, "shade": shade, "hv": hv, "proj": proj, "view": view, "hud": bar, "bg": bg, "weapon": arms,
              "chunks": chunks, "lane": lane, "k": k}
    return g, cols, rows, {**extra, "A": A, "PX": PX, "PY": PY, "tables": tabs, "angle_sub": sub}, layout


def _footprint(b, e):
    """Tile footprint (x0, y0, x1, y1) of blueprint entity e (combinators face east here)."""
    en = b.entities[e - 1]
    x, y = en["position"]["x"], en["position"]["y"]
    wd, ht = (2, 1) if "combinator" in en["name"] and en["name"] != "constant-combinator" else (1, 1)
    return (x - wd / 2, y - ht / 2, x + wd / 2, y + ht / 2)


def wire_screen(b, ent, cols, layout, w, h, rows):
    """Lamps: each chunk's rows snake through one network fed by its output deciders (chained), a
    lamp reading its column's lane at its row's quality. A status bar chunk also gets its static
    colors from a constant combinator right of the screen."""
    lattice = b.grid.hole
    lane = layout["lane"]
    for ci, ((y0, n), r) in enumerate(zip(layout["chunks"], rows)):
        es = [ent[m.id][1] for m in r.nodes] if r else []
        lamps, right = [], None
        for q in range(n):
            xs = range(w) if q % 2 == 0 else range(w - 1, -1, -1)
            row = [(x, b.lamp(x, y0 + q, {"use_colors": True, "color_mode": 2, "rgb_signal": lane(x, q)}, always_on=True))
                   for x in xs if not lattice(x, y0 + q)]
            lamps += [e for _, e in row]
            right = max(row)[1]
        if es:
            # The outputs and the chunk's first lamp: a spanning tree, relay poles where needed.
            ends = [(e, 3, _footprint(b, e)) for e in es] + [(lamps[0], 1, _footprint(b, lamps[0]))]
            usable = lambda x, y: x <= -2 and not lattice(x, y)  # noqa: E731
            taken = b.occupied()
            for d, i, j in nl._mst([f for _, _, f in ends]):
                (a, ca, fa), (c, cc, fc) = ends[i], ends[j]
                if d > nl.REACH:
                    nl._relay(b, fa, (a, ca), fc, (c, cc), 1, usable, taken)
                else:
                    b.wire(a, ca, c, cc)
        b.chain(lamps, 1)
        if ci in layout["bg"]:
            y = y0 + n - 1
            x = w if not lattice(w, y) else w + 2
            b.wire(b.constant(x, y, layout["bg"][ci]), 1, right, 1)


def build(w, h, substation_quality=DEFAULT_SUBSTATION_QUALITY, tex=DEFAULT_TEX, flat=DEFAULT_FLAT, things=th.THINGS,
          shade=None, hud=None, weapon=None, rows=DEFAULT_ROWS, view=None):
    if w > len(LANE_SIGNALS):
        raise SystemExit(f"width {w} needs {w} lane signals, only {len(LANE_SIGNALS)}")
    b = Blueprint(f"Raycaster M4 {w}x{h}", substation_quality)
    lattice = b.grid.hole
    g, cols, rows_, scal, layout = build_graph(w, h, hole=lattice, tex=tex, flat=flat, things=things, shade=shade,
                                               hud=hud, weapon=weapon, rows_per_net=rows, view=view)
    rows = rows_
    ent = g.compile(b, blocked=lattice, region=lambda x, y: x <= -2, x_right=layout["x_right"])
    wire_screen(b, ent, cols, layout, w, h, rows)
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

def evaluate(g, cols, rows, T, layout, inputs=None):
    """The screen's lamp values from the netlist evaluation: each chunk's output (plus a status bar
    chunk's constant colors), lane by lane."""
    live = g.evaluate(T, [r for r in rows if r is not None], inputs)[::-1]
    outs = [live.pop() if r is not None else {} for r in rows]
    img = [[0] * len(cols) for _ in range(sum(n for _, n in layout["chunks"]))]
    for ci, ((y0, n), o) in enumerate(zip(layout["chunks"], outs)):
        base = {sem.key(sg): v for sg, v in layout["bg"].get(ci, [])}
        for q in range(n):
            for x in range(len(cols)):
                s = layout["lane"](x, q)
                img[y0 + q][x] = sem.i32(o.get(sem.key(s), 0) + base.get(sem.key(s), 0))
    return img


def hud_state(g, scal, T=0, inputs=None):
    """The status bar's values {health, ammo, armor} as the circuit sees them."""
    hs, sigs = scal["hud"]
    o = g.evaluate(T, [hs], inputs)[0]
    return {k: o.get(sem.key(s), 0) for k, s in sigs.items()}


def ideal(g, scal, layout, w, h, T):
    """Float raycaster from the same camera scalars, textured and shaded the same way: a sanity
    check for the fixed-point math (not expected to match every pixel)."""
    vals = {k: g.evaluate(T, [scal[k].node])[0].get(sem.key(scal[k].signal), 0) for k in ("A", "PX", "PY")}
    vals.update(scal["tables"][(vals["A"] % (256 << scal["angle_sub"])) >> scal["angle_sub"]])
    tex, textures, types, flats, f = layout["tex"], layout["textures"], wall_types(), layout["flats"], layout["flat"]
    px, py = vals["PX"] / FP, vals["PY"] / FP
    hv, proj = layout["hv"], layout["proj"]
    img = [[pack(background(y, hv))] * w for y in range(hv)]
    wall_d = []
    for c in range(w):
        camx = ((2 * c + 1 - w) * 1024 // w) / 1024
        rdx = (vals["DX"] + vals["PLX"] * camx) / 1024
        rdy = (vals["DY"] + vals["PLY"] * camx) / 1024
        view = layout["view"]
        if flats:
            for y in range(hv):
                r = 2 * y + 1 - hv
                d = view.flat_factor(r) * proj / abs(r)
                fx, fy = px + d * rdx, py + d * rdy
                u, v = int((fx - math.floor(fx)) * f), int((fy - math.floor(fy)) * f)
                part = flats["floor" if r > 0 else "ceiling"]
                img[y][c] = flat_color(part["c4"][part["texels"][v][u]],
                                       row_scale(y, hv, layout["shade"], proj, view.flat_factor(r))[1])
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
        wall_d.append(d)
        across = (py + d * rdy) if side == 0 else (px + d * rdx)
        u = int((across - math.floor(across)) * tex)
        t = textures[types.index(MAP[my][mx])]
        ds = d * DS
        br = int(layout["shade"].bright / (ds + layout["shade"].fog))
        for y in range(hv):
            v = view.wall_offset(tex) / 4 + (2 * y + 1 - hv) * tex * ds / (proj * (1 << 16))
            if 0 <= v < tex:
                img[y][c] = br * side_color(t["c4"][t["texels"][int(v)][u] - 1], side)
    if layout["things"]:
        th.ideal(img, vals, layout["things"], layout["sprites"], wall_d, w, hv, layout["shade"], proj, layout["view"],
                 layout.get("thing_xy"))
    if layout["weapon"]:
        F = scal["weapon"]
        layout["weapon"].overlay(img, g.evaluate(T, [F.node])[0].get(sem.key(F.signal), 0))
    if layout["hud"]:
        img += layout["hud"].image(hud_state(g, scal, T))
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
              key=lambda k: sum(diff(frames[t], evaluate(g, cols, rows, t - k, layout)) for t in last))
    checked = [t for t in sorted(frames) if t - lag >= 1][2::every]
    t0 = time.time()
    bad = []
    for t in checked:
        n = diff(frames[t], evaluate(g, cols, rows, t - lag, layout))
        if n:
            bad.append((t, n))
    print(f"latency {lag} ticks; {len(checked)} frames x {w * h - len(holes)} lamps checked against the "
          f"netlist evaluation ({(time.time() - t0) / max(1, len(checked)):.1f} s each); {len(bad)} differ")
    for t, n in bad[:10]:
        print(f"  tick {t}: {n} lamps differ")
    worst = bar_bad = 0
    hv = layout["hv"]
    for t in checked[::max(1, len(checked) // 5)]:
        ev, ref = evaluate(g, cols, rows, t - lag, layout), ideal(g, scal, layout, w, h, t - lag)
        worst = max(worst, sum(1 for y in range(hv) for x in range(w) if ev[y][x] != ref[y][x]))
        bar_bad += sum(1 for y in range(hv, h) for x in range(w) if ev[y][x] != ref[y][x])
    print(f"fixed point vs float raycaster: at most {worst} of {w * hv} view pixels differ "
          f"({100 * worst / (w * hv):.2f}%) on sampled frames")
    if layout["hud"]:
        print(f"status bar vs the composed bar: {bar_bad} pixels differ on those frames")
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
    ap.add_argument("--no-things", action="store_true", help="leave out the monsters and items")
    ap.add_argument("--no-hud", action="store_true", help="leave out the status bar (the view takes the whole screen)")
    ap.add_argument("--no-weapon", action="store_true", help="leave out the weapon in front of the view")
    quality_arg(ap)
    shading_args(ap)
    view_args(ap)
    a = ap.parse_args()
    w, h = map(int, a.size.lower().split("x"))
    g, b, cols, rows, scal, layout = build(w, h, a.substation_quality, a.tex, a.flat, None if a.no_things else th.THINGS,
                                           Shading.from_args(a), None if a.no_hud else scripted_hud,
                                           None if a.no_weapon else scripted_weapon, view=View.from_args(a))
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
