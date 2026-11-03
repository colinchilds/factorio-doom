"""M4 shooting (2026-11-02, user request): hitscan along the middle of the view, monster health, and
Freedoom's pain and death animations.

Aim (in the raycaster, raycast.build_graph's `aim` hook; it already has every thing's depth and
lateral offset, and the wall distance per column):
    DC       the wall distance at the middle column, times 8 (alpha's scale, 2^18 per tile)
    A_i      per live monster: |L_i| < HIT_R, 0 < alpha_i < DC -> alpha_i, else BIG (an else-output)
    B_i      A_i < BIG and A_i below every other A_j (ties to the lower index) -> AIM = i + 1, and
             with alpha_i < MELEE also CLOSE = 1
B's outputs are wired to the camera port, which is the state bus itself, so AIM and CLOSE land on
the bus (from the camera a few ticks back, still newer than the frame on screen). Corpses don't
block shots (Doom clears MF_SHOOTABLE on death); items never do.

Monster state (bus, per thing index, held by deciders reading the bus like the weapon logic):
    DMG_i    damage taken. At a weapon's shot ticks (the same A values and ammo checks as its spend
             decider), AIM = i + 1 (and CLOSE for the fist) and alive -> DMG_i += the weapon's
             damage (Doom's mean; no randomness, no spread: every shotgun pellet hits).
    MS_i     animation clock: 0 standing, 1..PL pain (a hit from standing that doesn't kill),
             DEATH.. dying (the tick after DMG_i reaches the monster's health), then the corpse
             frame at CORPSE, held. Doom's state durations (info.c), tics converted to ticks.
The raycaster shows frame(MS_i): a lookup on the port added to the standing frame's id
(things.column_stage `frame`). Dead monsters stop hurting the player and stop being solid
(touch.py, game.player_graph).

`step` is the same rules in plain Python.
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "conformance"))
sys.path.insert(0, str(HERE))
from bp import sig  # noqa: E402
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import EACH, RED  # noqa: E402
import touch as tc  # noqa: E402
import weapons as wp  # noqa: E402

AIM = sig("signal-I")      # the monster in the crosshair: its thing index + 1
CLOSE = sig("signal-M")    # ... and it is within the fist's reach
DMG = [sig(f"signal-{c}") for c in ("red", "green", "blue", "yellow", "pink", "cyan", "white", "grey")]
MS = [sig(f"signal-{c}") for c in ("comma", "letter-dot", "exclamation-mark", "question-mark", "colon", "slash",
                                   "apostrophe", "quotation-mark")]
SIGNALS = [AIM, CLOSE] + DMG + MS
TILE = 1 << 18             # alpha and L per tile (things.column_stage)
HIT_R = 24 * TILE // 128   # a monster is hit within 24 Doom units of its centre (radius 20, plus a bit
                           # for the box's diagonal, which Doom's hitscan crosses)
# The fist: Doom reaches 64 units, to the monster's edge (radius 20), but our collision (the boxes
# carry a latency allowance, game.player_graph) stops the player 0.53-0.69 tile from a monster's
# centre per axis, so a punch reaches a tile.
MELEE = TILE
BIG = 1 << 30
DEATH = 64                 # MS of the first dying tick
# Doom's mean damage per shot (P_Random rolls); the shotgun's 7 pellets all hit, the rocket's
# splash is folded in.
DAMAGE = {"pistol": 10, "shotgun": 70, "chaingun": 10, "rocket launcher": 100, "plasma rifle": 22, "BFG": 450,
          "fist": 11}
MELEE_WEAPONS = {"fist"}
# Doom's monsters (info.c): spawn health, pain and death states as (frame, tics). Freedoom's art.
TYPES = {
    "TROO": {"hp": 60, "pain": [("TROOH1", 2), ("TROOH1", 2)],
             "death": [("TROOI0", 8), ("TROOJ0", 8), ("TROOK0", 6), ("TROOL0", 6)], "corpse": "TROOM0"},
    "POSS": {"hp": 20, "pain": [("POSSG1", 3), ("POSSG1", 3)],
             "death": [("POSSH0", 5), ("POSSI0", 5), ("POSSJ0", 5), ("POSSK0", 5)], "corpse": "POSSL0"},
}


def _seq(states, k):
    """The frame k ticks into a (frame, tics) sequence."""
    tic = k * wp.TICS // wp.UPS
    for name, n in states:
        if tic < n:
            return name
        tic -= n
    return states[-1][0]


def pain_len(t):
    return wp.ticks(sum(n for _, n in TYPES[t]["pain"]))


def corpse(t):
    return DEATH + wp.ticks(sum(n for _, n in TYPES[t]["death"]))


def hp(name):
    return TYPES[name[:4]]["hp"]


def frame(name, ms):
    """The frame of a monster standing as `name`, at clock ms."""
    t = TYPES[name[:4]]
    if ms <= 0:
        return name
    if ms < DEATH:
        return _seq(t["pain"], ms - 1)
    if ms < corpse(name[:4]):
        return _seq(t["death"], ms - DEATH)
    return t["corpse"]


def clock_values(name):
    t = name[:4]
    return [0] + list(range(1, pain_len(t) + 1)) + list(range(DEATH, corpse(t) + 1))


def frames(things):
    """Every frame the monsters can show (for things.load)."""
    out = set()
    for i in tc.monsters(things):
        out |= {frame(things[i][2], m) for m in clock_values(things[i][2])}
    return sorted(out)


def frame_offsets(name, fid):
    """{MS: frame id - the standing frame's id} (nonzero only), for the raycaster's lookup."""
    out = {m: fid[frame(name, m)] - fid[name] for m in clock_values(name)}
    assert all(v >= 0 for v in out.values()), "frame ids must grow from the standing frame"
    return {m: v for m, v in out.items() if v}


# ---------------------------------------------------------------------------------------------
# Aim: raycaster side

def aim_stage(g, col, D, lane, things, port):
    """The hitscan nodes (see the module docstring), reading the things' scalars (col: things.
    column_stage), the wall distance D (the [MV, MH] nodes) at the middle column `lane`, and each
    monster's DMG from the port. Returns the B nodes (their outputs belong on the bus)."""
    ms = tc.monsters(things)
    if not ms:
        return []
    DC = g.new_scalar_signal()
    dc = g.op("arith", sem.arith_spec(lane, "*", 8, DC, RED), red=D, label="aim wall")
    copies = []
    for i in ms:
        for s, what in ((col["alpha"][i], "a"), (col["lat"][i], "l")):
            copies.append(g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=[s.node], label=f"aim {what}{i}"))
    G = g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=copies, label="aim things")
    c = sem.cond
    A = []
    for i in ms:
        sa, sl = col["alpha"][i].signal, col["lat"][i].signal
        conds = [c(sl, ">", -HIT_R, RED), c(sl, "<", HIT_R, RED, "and"), c(sa, ">", 0, RED, "and"),
                 c(sa, "<", DC, RED, "and", RED), c(DMG[i], "<", hp(things[i][2]), RED, "and")]
        A.append(g.decider(conds, [sem.out(sa, networks=RED)], [sem.out(sa, constant=BIG)], red=[G, dc, port],
                           label=f"aim {i}"))
    B = []
    for k, i in enumerate(ms):
        sa = col["alpha"][i].signal
        conds = [c(sa, ">", 0, RED), c(sa, "<", BIG, RED, "and")]   # > 0: nothing before the pipeline fills
        for kj, j in enumerate(ms):
            if j != i:
                conds.append(c(sa, "<" if kj < k else "<=", col["alpha"][j].signal, RED, "and", RED))
        B.append(g.decider(conds, [sem.out(AIM, constant=i + 1)], red=A, label=f"aimed {i}"))
        B.append(g.decider(conds + [c(sa, "<", MELEE, RED, "and")], [sem.out(CLOSE, constant=1)], red=A,
                           label=f"aimed close {i}"))
    col["aim_rows"] = [[copies[2 * k], copies[2 * k + 1], A[k], B[2 * k], B[2 * k + 1]] for k in range(len(ms))]
    col["aim_rows"][0] = [dc, G] + col["aim_rows"][0]
    return B


# ---------------------------------------------------------------------------------------------
# Monster state: bus side

def _shots():
    """[(weapon index, A of a shot, damage)]."""
    out = []
    for wi, w in enumerate(wp.WEAPONS):
        for s in wp.timeline(w)["fire"]:
            out.append((wi, s, DAMAGE[w["name"]]))
    return out


def logic(things, W, A, ammo, start):
    """Deciders reading the bus and writing it, as [(label, spec, "bus")] (see weapons.logic)."""
    c = sem.cond
    ms = tc.monsters(things)
    if not ms:
        return []

    def have(wi):
        w = wp.WEAPONS[wi]
        return [c(ammo[w["ammo"]], ">=", w["cost"] - start[w["ammo"]], RED, "and")] if w["ammo"] else []

    def shot(i, wi, s, first):
        terms = [c(W, "=", wi, RED, None if first else "or"), c(A, "=", s, RED, "and")] + have(wi)
        terms.append(c(AIM, "=", i + 1, RED, "and"))
        if wp.WEAPONS[wi]["name"] in MELEE_WEAPONS:
            terms.append(c(CLOSE, ">", 0, RED, "and"))
        return terms

    out = [("damage hold", sem.decider_spec([c(DMG[ms[0]], ">=", 0, RED)], [sem.out(DMG[i], networks=RED) for i in ms]), "bus")]
    for i in ms:
        name = things[i][2]
        H, t = hp(name), name[:4]
        alive = c(DMG[i], "<", H, RED, "and")
        by_damage = {}
        for wi, s, d in _shots():
            by_damage.setdefault(d, []).append((wi, s))
        for d, lst in sorted(by_damage.items()):
            conds = []
            for wi, s in lst:
                conds += shot(i, wi, s, not conds) + [alive]
            out.append((f"damage {i} {d}", sem.decider_spec(conds, [sem.out(DMG[i], constant=d)]), "bus"))
        conds = []
        for wi, s, d in _shots():
            if d < H:
                conds += shot(i, wi, s, not conds) + [c(DMG[i], "<", H - d, RED, "and"), c(MS[i], "=", 0, RED, "and")]
        if conds:
            out.append((f"pain {i}", sem.decider_spec(conds, [sem.out(MS[i], constant=1)]), "bus"))
        out.append((f"die {i}", sem.decider_spec([c(DMG[i], ">=", H, RED), c(MS[i], "<", DEATH, RED, "and")],
                                                 [sem.out(MS[i], constant=DEATH)]), "bus"))
        adv = [c(MS[i], ">=", 1, RED), c(MS[i], "<", pain_len(t), RED, "and"), alive,
               c(MS[i], ">=", DEATH, RED, "or"), c(MS[i], "<", corpse(t), RED, "and")]
        out.append((f"anim {i}", sem.decider_spec(adv, [sem.out(MS[i], networks=RED), sem.out(MS[i], constant=1)]), "bus"))
        out.append((f"corpse {i}", sem.decider_spec([c(MS[i], "=", corpse(t), RED)], [sem.out(MS[i], networks=RED)]), "bus"))
    return out


def alive_terms(things, i):
    """Condition terms (signal, comparator, value) for monster i being alive, on the bus."""
    return [(DMG[i], "<", hp(things[i][2]))]


def step(things, W, A, ammo, aim, close, dmg, ms):
    """The rules in plain Python: the next (dmg, ms) lists (per thing index) from this tick's bus.
    ammo: {type: count}."""
    w = wp.WEAPONS[W]
    tl = wp.timeline(w)
    fires = A in tl["fire"] and (not w["ammo"] or ammo[w["ammo"]] >= w["cost"])
    dmg, ms = list(dmg), list(ms)
    for i in tc.monsters(things):
        name = things[i][2]
        H, t = hp(name), name[:4]
        d0, m = dmg[i], ms[i]
        hit = fires and aim == i + 1 and (close > 0 or w["name"] not in MELEE_WEAPONS) and d0 < H
        if hit:
            dmg[i] = d0 + DAMAGE[w["name"]]
        if d0 >= H:
            ms[i] = DEATH if m < DEATH else min(m + 1, corpse(t))
        elif hit and m == 0 and dmg[i] < H:
            ms[i] = 1
        elif 1 <= m < pain_len(t):
            ms[i] = m + 1
        else:
            ms[i] = 0
    return dmg, ms


def _wall(px, py, rx, ry, is_wall, grid):
    """Perpendicular wall distance along ray (rx, ry) (DDA, like raycast.ideal)."""
    mx, my = int(px), int(py)
    ddx, ddy = (abs(1 / rx) if rx else 1e30), (abs(1 / ry) if ry else 1e30)
    sx, sy = (1 if rx > 0 else -1), (1 if ry > 0 else -1)
    tx = ((mx + 1 - px) if rx > 0 else (px - mx)) * ddx
    ty = ((my + 1 - py) if ry > 0 else (py - my)) * ddy
    while True:
        if tx < ty:
            d, tx, mx = tx, tx + ddx, mx + sx
        else:
            d, ty, my = ty, ty + ddy, my + sy
        if is_wall(grid[my][mx]):
            return d


def ideal_aim(things, px, py, angle, dmg, is_wall, grid, w):
    """The hitscan in floats, independently of the circuit: (aim, close, margin) for the player at
    (px, py) tiles facing `angle` (radians), dmg per thing index, on a w-column screen (the wall
    test uses column w // 2's ray, as the circuit does). margin: how far (in tiles) the nearest
    decision boundary is (|L| vs HIT_R, depth vs the wall or another monster; 0 when the
    neighbouring columns' walls disagree, a ray grazing a corner), so callers can skip near-ties
    that fixed-point rounding may decide either way."""
    import math
    from raycast import PLANE
    dx, dy = math.cos(angle), math.sin(angle)
    pl = PLANE / 1024

    def wall(c):
        camx = ((2 * c + 1 - w) * 1024 // w) / 1024
        return _wall(px, py, dx - dy * pl * camx, dy + dx * pl * camx, is_wall, grid)
    wd = wall(w // 2)
    near = [wall(c) for c in (w // 2 - 1, w // 2 + 1)] + [wd]
    r, close = HIT_R / TILE, MELEE / TILE
    cands, margin = [], 1e9
    for i in tc.monsters(things):
        if dmg[i] >= hp(things[i][2]):
            continue
        rx, ry = things[i][0] - px, things[i][1] - py
        a, lat = rx * dx + ry * dy, ry * dx - rx * dy
        margin = min(margin, abs(abs(lat) - r))
        if abs(lat) < r and 0 < a:
            margin = min(margin, abs(a - wd))
            if min(near) <= a <= max(near):
                margin = 0
            if a < wd:
                cands.append((a, i))
    cands.sort()
    if len(cands) > 1:
        margin = min(margin, cands[1][0] - cands[0][0])
    if not cands:
        return 0, 0, margin
    a, i = cands[0]
    margin = min(margin, abs(a - close))
    return i + 1, int(a < close), margin
