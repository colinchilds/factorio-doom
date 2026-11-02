"""M4 HUD: Freedoom's status bar across the bottom of the screen, showing live numbers.

The bar is Doom's layout (320 x 32 at 320 x 200) scaled to the screen: bar_height(H) rows of the
lamp screen (16 at 160 x 100, 32 at 320 x 200); the 3D view is cropped above it, keeping the
full-screen projection (raycast.build_graph(..., hud=...)). Art from Freedoom (BSD):
STBAR, the big red digits STTNUM, STTPRCNT, STARMS with the weapon numbers, the face STFST and
the small ammo digits STYSNUM (assets/freedoom/hud.json).

Everything that never changes (the bar, labels, percent signs, the ARMS panel with every weapon
owned, the maximum ammo counts) is one constant combinator per row. Every place that changes is a
slot: the digits of ammo (the current weapon's; none for the fist), health and armor, the four
ammo counts on the right, and the face.
Per (slot, glyph, bar row) one decider outputs, on that slot's columns, the difference between
the row with that glyph drawn and the static row: exact colors, composed and box-filtered from
the 320 x 32 art at build time, then color-graded like the rest of the screen. A slot shows at
most one glyph, so the row's network is the static row plus at most one delta per slot.

Conditions (scalars N = the value, one signal per number; three networks, all one tick from HS):
    hundreds g   100 g <= N < 100 (g + 1)           on N (HS delayed)
    tens g > 0   10 g <= N % 100 < 10 g + 10        on N % 100
    tens 0       N % 100 < 10 and N >= 100          (N % 100 on red, N on green)
    ones g       N % 10 = g                         on N % 10
    face         a health range per face (5 pain levels from Doom's formula, dead at <= 0)
so leading zeros stay blank and negative values show nothing. The deciders read HS delayed to
land on the view rows' tick, so the HUD and the view always show the same frame.

Where two slots' source pixels share a screen pixel (only at small sizes), the pixel belongs to
the slot covering more of it, and the other slot's glyph doesn't change it.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import GREEN, RED, Collect  # noqa: E402

ASSETS = ROOT / "assets" / "freedoom" / "hud.json"
BAR_W, BAR_H = 320, 32   # Doom's status bar, at 320 x 200

# Doom's status bar layout (bar coordinates; st_stuff.c): numbers right-aligned at x.
# (state key, right edge x, top y, font, digits)
NUMBERS = [("ammo", 44, 3, "STTNUM", 3), ("health", 90, 3, "STTNUM", 3), ("armor", 221, 3, "STTNUM", 3),
           ("bullets", 288, 5, "STYSNUM", 3), ("shells", 288, 11, "STYSNUM", 3),   # the ammo list
           ("rockets", 288, 17, "STYSNUM", 3), ("cells", 288, 23, "STYSNUM", 3)]
FACE = (143, 0)
# Doom's limits (ammo with a backpack); "ammo" is the current weapon's.
MAXIMA = {"health": 200, "armor": 200, "ammo": 600, "bullets": 400, "shells": 100, "rockets": 100, "cells": 600}
# A new game. No pickups yet, so the player starts with every weapon and full ammo.
# A new game: every weapon (no weapon pickups yet), some of each ammo, below Doom's caps
# (200, 50, 50, 300) so the ammo pickups (touch.py) have room.
START = {"health": 100, "armor": 0, "bullets": 50, "shells": 20, "rockets": 10, "cells": 100}


def static_parts():
    """(lump, x, y) drawn over STBAR that never change: percent signs, the ARMS panel with the
    weapon numbers (every weapon owned: yellow) and the maximum ammo."""
    parts = [("STARMS", 104, 0), ("STTPRCNT", 90, 3), ("STTPRCNT", 221, 3)]
    for i in range(6):   # weapons 2..7
        parts.append((f"STYSNUM{i + 2}", 111 + (i % 3) * 12, 4 + (i // 3) * 10))
    for y, mx in ((5, 200), (11, 50), (17, 50), (23, 300)):   # bullets, shells, rockets, cells
        parts += digits(mx, 314, y, "STYSNUM", 4)
    return parts


def digits(value, x_right, y, font, width):
    """Doom's STlib_drawNum: digits right-aligned at x_right, no leading zeros, 0 shows as 0."""
    if value < 0:
        return []
    s = str(value)
    return [(f"{font}{ch}", x_right - width * (len(s) - k), y) for k, ch in enumerate(s)]


def face_ranges(maximum):
    """[(lump, lo, hi)]: health lo..hi (None: unbounded) shows that face. Doom's pain level is
    (100 - min(health, 100)) * 5 // 101; dead at health <= 0."""
    out = [("STFDEAD0", None, 0)]
    for k in range(5):
        hs = [v for v in range(1, maximum + 1) if (100 - min(v, 100)) * 5 // 101 == k]
        out.append((f"STFST{k}1", hs[0], None if k == 0 else hs[-1]))
    return out


def face_of(health):
    for name, lo, hi in face_ranges(MAXIMA["health"]):
        if (lo is None or health >= lo) and (hi is None or health <= hi):
            return name


def bar_height(h):
    return max(1, round(BAR_H * h / 200))


class Art:
    def __init__(self):
        d = json.loads(ASSETS.read_text())
        self.palette = [tuple(c) for c in d["palette"]]
        self.lumps = d["sprites"]

    def width(self, name):
        return self.lumps[name]["width"]

    def blit(self, img, name, x0, y0):
        """Draw a lump (Doom picture offsets apply) into a 320 x 32 image of RGB tuples."""
        s = self.lumps[name]
        for y, row in enumerate(s["rows"]):
            for x, i in enumerate(row):
                X, Y = x0 + x - s["left"], y0 + y - s["top"]
                if i >= 0 and 0 <= X < BAR_W and 0 <= Y < BAR_H:
                    img[Y][X] = self.palette[i]

    def rect(self, name, x0, y0):
        s = self.lumps[name]
        x, y = x0 - s["left"], y0 - s["top"]
        return max(0, x), max(0, y), min(BAR_W, x + s["width"]), min(BAR_H, y + s["height"])


class Hud:
    """The status bar for a w-wide screen of height h, colors graded by shade.rgb."""

    def __init__(self, w, h, shade):
        self.w, self.hb, self.shade = w, bar_height(h), shade
        self.art = Art()
        # Separable area weights: screen column X covers source columns [X * 320 / w, (X + 1) * 320 / w).
        self.wx = [self._weights(X, BAR_W, w) for X in range(w)]
        self.wy = [self._weights(Y, BAR_H, self.hb) for Y in range(self.hb)]
        base = [[self.art.palette[i] if i >= 0 else (0, 0, 0) for i in r] for r in self.art.lumps["STBAR"]["rows"]]
        for name, x, y in static_parts():
            self.art.blit(base, name, x, y)
        self.base = base
        self.bg = self.final(base)
        self.slots = self._slots()
        self._own()
        self.deltas = {}   # (slot index, glyph) -> {(x, yb): delta}
        for i, s in enumerate(self.slots):
            X0, X1, Y0, Y1 = s["screen"]
            for gl, parts in s["glyphs"].items():
                img = [r[:] for r in base]
                for name, x, y in parts:
                    self.art.blit(img, name, x, y)
                fin = self.final(img, (X0, X1, Y0, Y1))
                d = {(x, y): fin[y][x] - self.bg[y][x] for y in range(Y0, Y1) for x in range(X0, X1)
                     if self.owner.get((x, y)) == i and fin[y][x] != self.bg[y][x]}
                self.deltas[i, gl] = d

    @staticmethod
    def _weights(X, src, dst):
        a, b = X * src / dst, (X + 1) * src / dst
        return [(s, min(b, s + 1) - max(a, s)) for s in range(int(a), min(src, int(b) + 1)) if min(b, s + 1) > max(a, s)]

    def final(self, img, region=None):
        """Box-filter img (320 x 32) to the bar, grade, pack: rows of lamp values (never 0)."""
        X0, X1, Y0, Y1 = region or (0, self.w, 0, self.hb)
        out = [[0] * self.w for _ in range(self.hb)]
        for Y in range(Y0, Y1):
            for X in range(X0, X1):
                acc, area = [0.0, 0.0, 0.0], 0.0
                for sy, wy in self.wy[Y]:
                    row = img[sy]
                    for sx, wx in self.wx[X]:
                        c, k = row[sx], wx * wy
                        acc[0] += c[0] * k
                        acc[1] += c[1] * k
                        acc[2] += c[2] * k
                        area += k
                rgb = self.shade.rgb(tuple(round(v / area) for v in acc))
                out[Y][X] = ((rgb[0] << 16) | (rgb[1] << 8) | rgb[2]) or 1
        return out

    def _screen_rect(self, x0, y0, x1, y1):
        """Screen pixels whose source footprint meets the source rect [x0, x1) x [y0, y1)."""
        sx, sy = BAR_W / self.w, BAR_H / self.hb
        return (int(x0 / sx), min(self.w, int(-(-x1 // sx)) + 1), int(y0 / sy), min(self.hb, int(-(-y1 // sy)) + 1))

    def _slots(self):
        """Every changing place: {key, place (1, 10, 100 or 'face'), glyphs {g: [(lump, x, y)]},
        src (source rect), screen (screen rect)}."""
        slots = []
        for key, xr, y, font, n in NUMBERS:
            wd = self.art.width(f"{font}0")
            for k in range(n):
                place = 10 ** k
                if place > MAXIMA[key]:
                    break
                x = xr - wd * (k + 1)
                top = MAXIMA[key] // place if place > 1 else 9
                glyphs = {gl: [(f"{font}{gl}", x, y)] for gl in range(0 if place < 100 else 1, min(9, top) + 1)}
                slots.append({"key": key, "place": place, "glyphs": glyphs})
        fx, fy = FACE
        slots.append({"key": "health", "place": "face",
                      "glyphs": {name: [(name, fx, fy)] for name, _, _ in face_ranges(MAXIMA["health"])}})
        for s in slots:
            rs = [self.art.rect(name, x, y) for parts in s["glyphs"].values() for name, x, y in parts]
            s["src"] = (min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs))
            s["screen"] = self._screen_rect(*s["src"])
        return slots

    def _own(self):
        """Each screen pixel belongs to the slot whose source rect covers most of it."""
        sx, sy = BAR_W / self.w, BAR_H / self.hb
        best = {}
        for i, s in enumerate(self.slots):
            x0, y0, x1, y1 = s["src"]
            X0, X1, Y0, Y1 = s["screen"]
            for Y in range(Y0, Y1):
                oy = min(y1, (Y + 1) * sy) - max(y0, Y * sy)
                for X in range(X0, X1):
                    ox = min(x1, (X + 1) * sx) - max(x0, X * sx)
                    if ox > 0 and oy > 0 and ox * oy > best.get((X, Y), (0, None))[0]:
                        best[X, Y] = (ox * oy, i)
        self.owner = {p: i for p, (_, i) in best.items()}
        self.shared = sum(1 for p in best if sum(1 for s in self.slots if self._covers(s, p)) > 1)

    def _covers(self, s, p):
        sx, sy = BAR_W / self.w, BAR_H / self.hb
        x0, y0, x1, y1 = s["src"]
        X, Y = p
        return min(x1, (X + 1) * sx) > max(x0, X * sx) and min(y1, (Y + 1) * sy) > max(y0, Y * sy)

    # -- reference ------------------------------------------------------------------------------

    def image(self, state):
        """The bar for state {health, ammo, armor}: the reference for the circuit's rows. Glyphs
        are picked the way Doom draws numbers (STlib_drawNum) and the face, independently of the
        deciders' conditions. Each slot's pixels are filtered from the bar with that slot's glyph
        drawn; a pixel two slots share shows its owner's (the circuit's rule; with no shared pixels,
        as at 320 x 200, this is the plain composed bar)."""
        drawn = []   # (lump, x, y)
        for key, xr, y, font, _ in NUMBERS:
            v = state[key]
            if 0 <= v <= MAXIMA[key]:
                drawn += digits(v, xr, y, font, self.art.width(f"{font}0"))
        drawn.append((face_of(state["health"]), *FACE))
        out = [r[:] for r in self.bg]
        for i, s in enumerate(self.slots):
            mine = [d for d in drawn if any(d == part for parts in s["glyphs"].values() for part in parts)]
            if not mine:
                continue
            img = [r[:] for r in self.base]
            for name, x, y in mine:
                self.art.blit(img, name, x, y)
            X0, X1, Y0, Y1 = s["screen"]
            fin = self.final(img, s["screen"])
            for y in range(Y0, Y1):
                for x in range(X0, X1):
                    if self.owner.get((x, y)) == i:
                        out[y][x] = fin[y][x]
        return out

    # -- circuit --------------------------------------------------------------------------------

    def build(self, g, hs, sigs, cols, t_out, y0, xs):
        """The bar's deciders in graph g. hs: a node whose output holds the values (sigs: {key:
        signal}); t_out: the tick the rows must land on (the view rows'); rows y0.. of the screen,
        deciders fixed at x = xs[0], xs[1], ... (right to left) in their row. Returns the rows
        (Collect, or None for a row with no slot pixels)."""
        lag = t_out - 2 - hs.time
        if lag < 0:
            raise ValueError("HUD input arrives too late for the view rows")
        HS = g.delay(hs, lag)
        N = g.delay(HS, 1)
        M100 = g.vc(HS, "%", 100, "hud n%100")
        M10 = g.vc(HS, "%", 10, "hud n%10")
        rows = []
        self.nodes = []
        for yb in range(self.hb):
            groups = {"face": [], 100: [], 10: [], 1: []}
            for i, s in enumerate(self.slots):
                S = sigs[s["key"]]
                for gl in s["glyphs"]:
                    d = sorted((x, v) for (x, y), v in self.deltas[i, gl].items() if y == yb)
                    if not d:
                        continue
                    outs = [sem.out(cols[x], constant=v) for x, v in d]
                    green = []
                    p = s["place"]
                    if p == "face":
                        lo, hi = next((lo, hi) for name, lo, hi in face_ranges(MAXIMA["health"]) if name == gl)
                        conds = ([sem.cond(S, ">=", lo, RED)] if lo is not None else []) + \
                                ([sem.cond(S, "<=", hi, RED, "and" if lo is not None else None)] if hi is not None else [])
                        red = N
                    elif p == 100:
                        conds, red = [sem.cond(S, ">=", 100 * gl, RED), sem.cond(S, "<", 100 * gl + 100, RED, "and")], N
                    elif p == 10 and gl:
                        conds, red = [sem.cond(S, ">=", 10 * gl, RED), sem.cond(S, "<", 10 * gl + 10, RED, "and")], M100
                    elif p == 10:
                        conds = [sem.cond(S, ">=", 0, RED), sem.cond(S, "<", 10, RED, "and"),
                                 sem.cond(S, ">=", 100, GREEN, "and")]
                        red, green = M100, [N]
                    else:
                        conds, red = [sem.cond(S, "=", gl, RED)], M10
                    n = g.decider(conds, outs, red=red, green=green, label=f"hud{yb}.{s['key']}.{p}.{gl}")
                    groups[p].append(n)
            # Grouped by input network, so each network's members line up across rows.
            nodes = groups["face"] + groups[100] + groups[10] + groups[1]
            if len(nodes) > len(xs):
                raise ValueError(f"HUD row needs {len(nodes)} slots, {len(xs)} given")
            for n, x in zip(nodes, xs):
                n.fixed, n.pos = True, (x, y0 + yb)
            self.nodes.append(nodes)
            rows.append(Collect(nodes) if nodes else None)
        return rows

    def width(self):
        """Deciders in the busiest row (slots needed left of the screen)."""
        best = 0
        for yb in range(self.hb):
            best = max(best, sum(1 for (i, gl), d in self.deltas.items() if any(y == yb for _, y in d)))
        return best
