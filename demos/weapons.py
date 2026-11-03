"""M4 weapons: the player's gun in front of the view, switching, and Doom's firing animations.

Art: Freedoom's weapon sprites (BSD; assets/freedoom/weapons.json), drawn where Doom draws its
player sprites (R_DrawPSprite: x = 1 - left offset, top row = viewheight / 2 - 68 - top offset in
a 320-wide view, 168 rows over the status bar or 200 without), box-filtered to the screen's view.
No bob.

Animation: Doom's weapon states (info.c), in tics (35 a second) converted to ticks (60 a second).
Each weapon fires from its ready frame; the flash is a second layer started by the fire states,
composed over the gun at build time. A composite is one (gun frame, flash frame, drop) picture;
the composites of every weapon are numbered 1..N.

Switching: Doom lowers the gun 6 pixels a tic (16 tics to WEAPONBOTTOM, 96 below), changes
weapon, and raises the new one the same way. The ready frame is drawn at DROPS steps of 12
pixels (12 rows at 320 x 200), so the gun moves about 17 times a second.

State (all on the player's state bus, see demos/game.py):
    W   weapon, an index into WEAPONS (0 = the pistol, the start weapon).
    A   animation tick: 0 = ready, 1..L while firing, LOWER.. lowering, BOTTOM down, RAISE..
        raising.
    D   switches queued: each "next weapon" press adds 1.
A press of fire (held) from ready starts the cycle (A = 1) if there's ammo. A counts up; at
A = R (Doom's A_ReFire) it restarts at 1 if fire is still held and there's ammo, otherwise it
runs on to L and back to 0. Ammo is spent at the ticks of Doom's fire actions (A = s), each with
its own ammo check (the chaingun's second shot).
While D > 0 a ready or firing gun starts lowering (a switch cancels the shot). At BOTTOM, each
tick with D > 0 steps W to the next weapon and takes 1 from D; with D = 0 the gun rises, then is
ready (and lowers again at once if presses came meanwhile). So tapping next several times
quickly costs one lower and one raise.

The view shows composite table[W][A]; raycast.build_graph(..., weapon=...) overlays it.
"""
import json
import math
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import RED  # noqa: E402

ASSETS = ROOT / "assets" / "freedoom" / "weapons.json"
TICS, UPS = 35, 60
KEYW = 256          # composite lookup key: W * KEYW + A (A < KEYW)
DROPS = 8           # lowered positions of the ready frame
DROP_PX = 96        # WEAPONBOTTOM - WEAPONTOP
LOWER, RAISE = 128, 192   # A of the first lowering / raising tick

AMMO_TYPES = ["bullets", "shells", "rockets", "cells"]

# Doom's weapons in "next weapon" order from the pistol. states: (frame, tics, actions) with
# actions "fire", "flash" (flash sequence 0) / "flashN", "refire" (A_ReFire on entering).
# flashes: [(frame, tics)] sequences. Freedoom's art, Doom's timing (plasma: flash A only).
WEAPONS = [
    {"name": "pistol", "slot": 2, "sprite": "PISG", "flash": "PISF", "ammo": "bullets", "cost": 1,
     "states": [("A", 4, ""), ("B", 6, "fire flash"), ("C", 4, ""), ("B", 5, "refire")],
     "flashes": [[("A", 7)]]},
    {"name": "shotgun", "slot": 3, "sprite": "SHTG", "flash": "SHTF", "ammo": "shells", "cost": 1,
     "states": [("A", 3, ""), ("A", 7, "fire flash"), ("B", 5, ""), ("C", 5, ""), ("D", 4, ""), ("C", 5, ""),
                ("B", 5, ""), ("A", 3, ""), ("A", 7, "refire")],
     "flashes": [[("A", 4), ("B", 3)]]},
    {"name": "chaingun", "slot": 4, "sprite": "CHGG", "flash": "CHGF", "ammo": "bullets", "cost": 1,
     "states": [("A", 4, "fire flash0"), ("B", 4, "fire flash1"), ("B", 0, "refire")],
     "flashes": [[("A", 5)], [("B", 5)]]},
    {"name": "rocket launcher", "slot": 5, "sprite": "MISG", "flash": "MISF", "ammo": "rockets", "cost": 1,
     "states": [("B", 8, "flash"), ("B", 12, "fire"), ("B", 0, "refire")],
     "flashes": [[("A", 3), ("B", 4), ("C", 4), ("D", 4)]]},
    {"name": "plasma rifle", "slot": 6, "sprite": "PLSG", "flash": "PLSF", "ammo": "cells", "cost": 1,
     "states": [("A", 3, "fire flash"), ("B", 20, "refire")],
     "flashes": [[("A", 4)]]},
    {"name": "BFG", "slot": 7, "sprite": "BFGG", "flash": "BFGF", "ammo": "cells", "cost": 40,
     "states": [("A", 20, ""), ("B", 10, "flash"), ("B", 10, "fire"), ("B", 20, "refire")],
     "flashes": [[("A", 11), ("B", 6)]]},
    {"name": "fist", "slot": 1, "sprite": "PUNG", "flash": None, "ammo": None, "cost": 0,
     "states": [("B", 4, ""), ("C", 4, "fire"), ("D", 5, ""), ("C", 4, ""), ("B", 5, "refire")],
     "flashes": []},
]


def ticks(tics):
    return round(tics * UPS / TICS)


SWITCH = ticks(DROP_PX // 6)   # ticks to lower (or raise): 16 tics
BOTTOM = LOWER + SWITCH        # A while down
RAISE_END = RAISE + SWITCH - 1


def drop_step(offset):
    """Doom's offset below WEAPONTOP (pixels) -> the drawn step 0..DROPS."""
    return min(DROPS, (offset + DROP_PX // DROPS // 2) * DROPS // DROP_PX)


def switch_steps():
    """{A: drop step} for the lowering, bottom and raising ticks (Doom: 6 pixels per tic)."""
    out = {}
    for e in range(SWITCH):
        tic = e * TICS // UPS
        out[LOWER + e] = drop_step(6 * (tic + 1))
        out[RAISE + e] = drop_step(DROP_PX - 6 * (tic + 1))
    out[BOTTOM] = DROPS
    return out


def timeline(wp):
    """A weapon's firing cycle in ticks: {frames: [(gun lump, flash lump or None)] for A = 1..L,
    ready: the A = 0 frame, L, R (refire check), fire: [A of each shot]}."""
    total = sum(t for _, t, _ in wp["states"])
    gun = []          # per tic
    flash_start = []  # (tic, sequence)
    fire, refire = [], None
    t = 0
    for frame, n, actions in wp["states"]:
        for a in actions.split():
            if a == "fire":
                fire.append(t)
            elif a.startswith("flash"):
                flash_start.append((t, wp["flashes"][int(a[5:] or 0)]))
            elif a == "refire":
                refire = t
        gun += [f"{wp['sprite']}{frame}0"] * n
        t += n
    flash = [None] * total
    for s, seq in flash_start:   # a later flash replaces an earlier one
        k = s
        for frame, n in seq:
            for _ in range(n):
                if k < total:
                    flash[k] = f"{wp['flash']}{frame}0"
                k += 1
        for k in range(k, total):
            if flash[k] and k >= s:
                flash[k] = None
    L = ticks(total)
    frames = []
    for e in range(L):
        tic = min(total - 1, e * TICS // UPS)
        frames.append((gun[tic], flash[tic]))
    return {"frames": frames, "ready": (f"{wp['sprite']}A0", None), "L": L, "R": ticks(refire),
            "fire": [ticks(f) + 1 for f in fire]}


class Art:
    def __init__(self):
        d = json.loads(ASSETS.read_text())
        self.palette = [tuple(c) for c in d["palette"]]
        self.lumps = d["sprites"]


class Weapons:
    """The weapons' composites for a w-wide view hv rows tall (status bar: the view is Doom's 168
    rows, else 200), colors graded by shade.rgb."""

    def __init__(self, w, hv, shade, bar=True):
        self.w, self.hv, self.shade = w, hv, shade
        self.vh = 168 if bar else 200
        self.art = Art()
        self.composites, self.table = pictures()
        self.wx = [_weights(X, 320, w) for X in range(w)]
        self.wy = [_weights(Y, self.vh, hv) for Y in range(hv)]
        self.pixels = [self._render(c) for c in self.composites]   # [{(x, y): packed}]
        self.rows = {}   # y -> [(composite id, [(x, packed)])]
        for cid, px in enumerate(self.pixels, 1):
            for y in sorted({y for _, y in px}):
                self.rows.setdefault(y, []).append((cid, sorted((x, v) for (x, yy), v in px.items() if yy == y)))

    def _canvas(self, comp):
        """The composite on Doom's 320 x vh view: rows of RGB or None (transparent)."""
        img = [[None] * 320 for _ in range(self.vh)]
        *comp, drop = comp
        for name in comp:
            if not name:
                continue
            s = self.art.lumps[name]
            x0 = 1 - s["left"]
            y0 = self.vh // 2 - 68 - s["top"] + drop * DROP_PX // DROPS
            for y, row in enumerate(s["rows"]):
                Y = y0 + y
                if not 0 <= Y < self.vh:
                    continue
                for x, i in enumerate(row):
                    X = x0 + x
                    if i >= 0 and 0 <= X < 320:
                        img[Y][X] = self.art.palette[i]
        return img

    def _render(self, comp):
        """Screen pixels of a composite: opaque where at least half the footprint is; the color is
        the opaque part's average, graded and packed (never 0)."""
        img = self._canvas(comp)
        out = {}
        for Y in range(self.hv):
            for X in range(self.w):
                acc, area, cover = [0.0, 0.0, 0.0], 0.0, 0.0
                for sy, wy in self.wy[Y]:
                    row = img[sy]
                    for sx, wx in self.wx[X]:
                        k = wx * wy
                        area += k
                        c = row[sx]
                        if c is not None:
                            cover += k
                            acc[0] += c[0] * k
                            acc[1] += c[1] * k
                            acc[2] += c[2] * k
                if cover * 2 >= area and cover > 0:
                    rgb = self.shade.rgb(tuple(round(v / cover) for v in acc))
                    out[X, Y] = ((rgb[0] << 16) | (rgb[1] << 8) | rgb[2]) or 1
        return out

    def composite(self, wi, a):
        return self.table[wi * KEYW + a]

    def width(self):
        """Composites in the busiest row (overlay slots per row)."""
        return max((len(v) for v in self.rows.values()), default=0)

    def chunk(self, y0, n):
        """{composite id: [(x, y, packed)]} for the composites with pixels in rows y0 .. y0 + n - 1."""
        out = {}
        for y in range(y0, y0 + n):
            for cid, px in self.rows.get(y, []):
                out.setdefault(cid, []).extend((x, y, v) for x, v in px)
        return out

    def overlay(self, img, cid):
        """Draw composite cid over img (rows of packed colors) in place (0: none)."""
        if not cid:
            return
        for (x, y), v in self.pixels[cid - 1].items():
            img[y][x] = v


def pictures():
    """The composites [(gun, flash, drop step)] (id = index + 1) and the table W * KEYW + A -> id."""
    comps, ids, table = [], {}, {}
    sw = switch_steps()
    for wi, tl in enumerate(map(timeline, WEAPONS)):
        assert tl["L"] < LOWER and RAISE_END < KEYW and BOTTOM < RAISE
        frames = {a: (*c, 0) for a, c in enumerate([tl["ready"]] + tl["frames"])}
        frames.update({a: (*tl["ready"], k) for a, k in sw.items()})
        for a, c in sorted(frames.items()):
            if c not in ids:
                comps.append(c)
                ids[c] = len(comps)
            table[wi * KEYW + a] = ids[c]
    return comps, table


def picture_count():
    return len(pictures()[0])


def _weights(X, src, dst):
    a, b = X * src / dst, (X + 1) * src / dst
    return [(s, min(b, s + 1) - max(a, s)) for s in range(int(a), min(src, int(b) + 1)) if min(b, s + 1) > max(a, s)]


def step(W, A, D, fire, nxt, ammo):
    """The rules in plain Python (independent of logic()): the next (W, A, D) and the ammo spent
    {type: amount}, from this tick's state. ammo: {type: count}."""
    wp, tl = WEAPONS[W], timeline(WEAPONS[W])
    have = not wp["ammo"] or ammo[wp["ammo"]] >= wp["cost"]
    spent = {wp["ammo"]: wp["cost"]} if wp["ammo"] and A in tl["fire"] and have else {}
    D2 = D + (1 if nxt > 0 else 0)
    if A >= LOWER:   # switching
        if A == BOTTOM:
            if D > 0:
                return (W + 1) % len(WEAPONS), A, D2 - 1, spent
            return W, RAISE, D2, spent
        if A == RAISE_END:
            return W, 0, D2, spent
        return W, A + 1, D2, spent
    if D > 0:
        return W, LOWER, D2, spent
    if (A == 0 or A == tl["R"]) and fire > 0 and have:
        return W, 1, D2, spent
    if A == 0 or A == tl["L"]:
        return W, 0, D2, spent
    return W, A + 1, D2, spent


# ---------------------------------------------------------------------------------------------
# Game logic: decider specs for the state machine (demos/game.py places and wires them).

def logic(W, A, D, FIRE, NEXT, ammo, start):
    """Deciders reading the state bus (red), as [(label, spec, target)], target "bus" (W, A and D,
    next tick) or "reg" (ammo spent, into the player register). ammo: {type: signal}, holding
    the count minus start[type]. weapons.step is the same rules in plain Python."""
    lines = [timeline(wp) for wp in WEAPONS]
    n = len(WEAPONS)
    c = sem.cond

    def have(wi, first=False):
        """There's ammo for weapon wi's shot (nothing for the fist)."""
        wp = WEAPONS[wi]
        if not wp["ammo"]:
            return []
        return [c(ammo[wp["ammo"]], ">=", wp["cost"] - start[wp["ammo"]], RED, None if first else "and")]

    out = []
    copy = lambda s: sem.out(s, networks=RED)  # noqa: E731
    # Queued switches: D + (next pressed) - (a step at the bottom).
    out.append(("queue hold", sem.decider_spec([c(D, ">", 0, RED)], [copy(D)]), "bus"))
    out.append(("queue add", sem.decider_spec([c(NEXT, ">", 0, RED)], [sem.out(D, constant=1)]), "bus"))
    out.append(("queue take", sem.decider_spec([c(A, "=", BOTTOM, RED), c(D, ">", 0, RED, "and")],
                                               [sem.out(D, constant=-1)]), "bus"))
    # Weapon: steps (wrapping to 0: no output) at the bottom while switches are queued.
    out.append(("weapon hold", sem.decider_spec([c(A, "!=", BOTTOM, RED), c(D, "<=", 0, RED, "or")], [copy(W)]), "bus"))
    out.append(("weapon next", sem.decider_spec([c(A, "=", BOTTOM, RED), c(D, ">", 0, RED, "and"),
                                                 c(W, "<", n - 1, RED, "and")], [copy(W), sem.out(W, constant=1)]), "bus"))
    # Animation: advance (copy A + 1), hold at the bottom, or jump (A = constant).
    adv, start_c = [], []
    for wi, tl in enumerate(lines):
        L, R = tl["L"], tl["R"]
        base = lambda: [c(W, "=", wi, RED, "or" if adv else None), c(D, "<=", 0, RED, "and")]  # noqa: E731
        adv += base() + [c(A, ">=", 1, RED, "and"), c(A, "<", R, RED, "and")]
        if R < L:
            adv += base() + [c(A, ">", R, RED, "and"), c(A, "<", L, RED, "and")]
            adv += base() + [c(A, "=", R, RED, "and"), c(FIRE, "<=", 0, RED, "and")]
            if WEAPONS[wi]["ammo"]:
                wp = WEAPONS[wi]
                adv += base() + [c(A, "=", R, RED, "and"),
                                 c(ammo[wp["ammo"]], "<", wp["cost"] - start[wp["ammo"]], RED, "and")]
        for at in (0, R):
            start_c += [c(W, "=", wi, RED, "or" if start_c else None), c(D, "<=", 0, RED, "and"),
                        c(A, "=", at, RED, "and"), c(FIRE, ">", 0, RED, "and")] + have(wi)
    adv += [c(A, ">=", LOWER, RED, "or"), c(A, "<", BOTTOM, RED, "and"),
            c(A, ">=", RAISE, RED, "or"), c(A, "<", RAISE_END, RED, "and")]
    out.append(("anim advance", sem.decider_spec(adv, [copy(A), sem.out(A, constant=1)]), "bus"))
    out.append(("anim start", sem.decider_spec(start_c, [sem.out(A, constant=1)]), "bus"))
    out.append(("anim lower", sem.decider_spec([c(A, "<", LOWER, RED), c(D, ">", 0, RED, "and")],
                                               [sem.out(A, constant=LOWER)]), "bus"))
    out.append(("anim down", sem.decider_spec([c(A, "=", BOTTOM, RED), c(D, ">", 0, RED, "and")], [copy(A)]), "bus"))
    out.append(("anim raise", sem.decider_spec([c(A, "=", BOTTOM, RED), c(D, "<=", 0, RED, "and")],
                                               [sem.out(A, constant=RAISE)]), "bus"))
    # Ammo: per weapon with ammo, at each shot's tick, if there's enough.
    for wi, (wp, tl) in enumerate(zip(WEAPONS, lines)):
        if not wp["ammo"]:
            continue
        conds = []
        for s in tl["fire"]:
            conds += [c(W, "=", wi, RED, "or" if conds else None), c(A, "=", s, RED, "and")] + have(wi)
        out.append((f"spend {wp['name']}", sem.decider_spec(conds, [sem.out(ammo[wp["ammo"]], constant=-wp["cost"])]),
                    "reg"))
    return out


if __name__ == "__main__":
    for wp in WEAPONS:
        tl = timeline(wp)
        print(f"{wp['name']:16} L {tl['L']:3} R {tl['R']:3} fire {tl['fire']}  "
              f"{len(set(tl['frames']) | {tl['ready']})} pictures")
    for size in ((64, 34), (160, 84), (320, 168)):
        from raycast import Shading  # noqa: E402
        wpn = Weapons(*size, Shading())
        rows = sorted(wpn.rows)
        print(f"{size}: {len(wpn.composites)} composites, rows {rows[0]}..{rows[-1]}, "
              f"{sum(len(v) for v in wpn.rows.values())} deciders, busiest row {wpn.width()}, "
              f"{sum(len(p) for p in wpn.pixels)} pixel outputs")
