"""The status bar face's expressions (2026-11-03, user request): Doom's ST_updateFaceWidget.

Doom picks one of 42 faces: a pain level from health (5 levels: hud.face_ranges) times 8
expressions, plus god mode and dead. Each tic, in priority order (a face of higher priority keeps
showing until its count runs out):
    dead        health 0                                       (priority 9; hud.py shows it)
    evil grin   a weapon picked up                             (8; no weapon pickups here yet)
    hurt        damagecount > 0 with an attacker: turn toward it (7), count TURNCOUNT (1 s).
                Head-on (within 45 degrees) is the rampage face; left or right the turned faces.
                Doom's "ouch" face tests health - oldhealth > 20, which never holds when hurt (a
                famous bug: it shows when you gain 20 health while hurt); our hits are 9 and 12.
    rampage     fire held 2 s (lastattackdown counts 70 tics), every tic after that (5)
    god         god mode (4; none here)
    idle        when the count runs out: one of the three straight faces at random (they glance
                left and right), for half a second (0). Here the count starts only once the random
                number runs (RND != 0, from its constant), so it doesn't depend on power-on time.
Tics become ticks (weapons.ticks): STRAIGHT 29, TURN 60, RAMPAGE 120.

Circuit. The bus carries FE, the expression (0..2 straight, 3 turned right, 4 turned left,
5 hurt head-on, 6 rampage; 5 and 6 show the same face, but 3..5 have priority 7), which the
status bar reads, and FB_k per monster: which side of the view monster k is on (0 within 45
degrees, 1 right, 2 left). FB comes from the raycaster (bearing_stage), which has each thing's
depth alpha and lateral offset L already: right is L > 0 and alpha <= L, left L < 0 and
alpha <= -L (both 0 while the pipeline fills: no bearing). It lands on the bus like the aim, from the camera a few ticks back.
M (monsters.py's network) carries
    FC   the face count, after this tick's decrement (Doom's st_facecount)
    RC   Doom's lastattackdown: 0 idle, RAMPAGE when fire starts, counting down to 1
    DC   Doom's damagecount, in 1/12 tics (a tic is 12/7 ticks): +12 per point of health lost,
         -7 a tick, at most 100 tics. Health is lost through monsters.py's damage queue, so the
         increments mirror its health deciders. (The red screen flash will read it.)
    ATK  the attacker: monster k + 1. A monster's shot writes 16 << k (the same clauses as its
         shot), the next tick ranges turn the highest bit into k + 1 (the last of simultaneous
         shots, as Doom's thinker order would), so for one tick ATK is raw and the hurt face holds.
Every decider is one tick deep and reads the bus (red) and M (green), gated by the restart like the
other bus state. Only with monsters (M; and its random number RND for the glances).

`step` is the same rules in plain Python.
"""
import math
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "conformance"))
sys.path.insert(0, str(HERE))
from bp import sig  # noqa: E402
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import EACH, GREEN, RED  # noqa: E402
import monsters as mo  # noqa: E402
import touch as tc  # noqa: E402
import weapons as wp  # noqa: E402

FE = sig("signal-skull")                                                       # bus
FB = [sig(n) for n in ("signal-moon", "signal-sun", "signal-ghost", "signal-mining")]   # bus, per monster
FC, RC, DC, ATK = (sig(n) for n in ("signal-clock", "signal-alarm", "signal-speed", "signal-weapon"))   # M
BUS = [FE] + FB
NET = [FC, RC, DC, ATK]
_GREEN = {s["name"] for s in NET} | {s["name"] for s in mo.NET}

STRAIGHT = wp.ticks(17)   # ST_STRAIGHTFACECOUNT (TICRATE / 2)
TURN = wp.ticks(35)       # ST_TURNCOUNT
RAMPAGE = wp.ticks(70)    # ST_RAMPAGEDELAY
UNIT, DECAY = 12, 7       # DC per tic, and per tick (12/7 ticks a tic)
CAP = 100 * UNIT          # damagecount stops at 100
RAW = 16                  # ATK from a shot: RAW << k
HURT = {1: 3, 2: 4, 0: 5}   # FB code -> expression
RAMP = 6
# The glances: RND (int32) in thirds.
THIRD = (1 << 32) // 3
LOOK = [(None, THIRD - (1 << 31)), (THIRD - (1 << 31), 2 * THIRD - (1 << 31)), (2 * THIRD - (1 << 31), None)]


def lump(pain, fe):
    """The face picture for pain level 0..4 and expression fe."""
    if fe <= 2:
        return f"STFST{pain}{fe}"
    return {3: f"STFTR{pain}0", 4: f"STFTL{pain}0"}.get(fe, f"STFKILL{pain}")


def lumps(pain):
    """[(lump, fe lo, fe hi)] for one pain level."""
    return [(lump(pain, e), e, e) for e in range(5)] + [(lump(pain, 5), 5, 6)]


def _c(s, comp, v, ct):
    w = GREEN if s["name"] in _GREEN else RED
    if isinstance(v, dict):
        return sem.cond(s, comp, v, w, ct, GREEN if v["name"] in _GREEN else RED)
    return sem.cond(s, comp, v, w, ct)


def dnf(clauses):
    out = []
    for terms in clauses:
        out += [_c(s, comp, v, ("or" if out else None) if j == 0 else "and") for j, (s, comp, v) in enumerate(terms)]
    return out


def specs(mon, anim):
    """[(label, spec, "bus" | "net")]: the face deciders (see the module docstring). mon: the
    monsters.Monsters; anim: the weapon's animation signal (A in 1 .. LOWER - 1 is Doom's
    attackdown: from a shot until the gun is ready again)."""
    n = len(mon.idx)
    spec = lambda clauses, outs: sem.decider_spec(dnf(clauses), outs)  # noqa: E731
    calm = [(DC, "=", 0)]
    free = [[(FE, "<", 3)], [(FE, ">", 5)]]          # no hurt face showing (priority below 6)
    hurt_face = [(FE, ">=", 3), (FE, "<=", 5)]
    firing = [(anim, ">=", 1), (anim, "<", wp.LOWER)]
    # Rampage this tick: calm, free, firing and lastattackdown at 1.
    r6 = [calm + f + firing + [(RC, "=", 1)] for f in free]
    not_r6 = [hurt_face, [(anim, "<", 1)], [(anim, ">=", wp.LOWER)], [(RC, "<", 1)], [(RC, ">", 1)]]
    out = []
    # The expression (bus).
    hold = [calm + [(FC, ">", 0)] + c for c in not_r6] + [[(DC, ">", 0), (ATK, ">=", RAW)]]
    out.append(("face hold", spec(hold, [sem.out(FE, networks=RED)]), "bus"))
    out.append(("face rampage", spec(r6, [sem.out(FE, constant=RAMP)]), "bus"))
    for e in (1, 2):
        lo, hi = LOOK[e]
        rnd = ([(mo.RND, ">=", lo)] if lo is not None else []) + ([(mo.RND, "<", hi)] if hi is not None else [])
        out.append((f"face look {e}", spec([calm + [(FC, "=", 0)] + rnd + c for c in not_r6], [sem.out(FE, constant=e)]),
                    "bus"))
    for code, e in HURT.items():
        cl = [[(DC, ">", 0), (ATK, "=", k + 1), (FB[k], "=", code)] for k in range(n)]
        out.append((f"face hurt {e}", spec(cl, [sem.out(FE, constant=e)]), "bus"))
    # The count (M).
    # (Down while > 1: an output summing to 0 shows as a 0 signal in the game, CLAUDE.md.)
    out.append(("face count", spec([calm + [(FC, ">", 1)] + c for c in not_r6],
                                   [sem.out(FC, networks=GREEN), sem.out(FC, constant=-1)]), "net"))
    # The idle cycle starts only once RND runs (its constant is on): a count running from power-on
    # would depend on how long the game ran before (the harness's warm-up is a tick longer than fsim's).
    out.append(("face count look", spec([calm + [(FC, "=", 0), (mo.RND, "!=", 0)] + c for c in not_r6],
                                        [sem.out(FC, constant=STRAIGHT - 1)]), "net"))
    out.append(("face count hurt", spec([[(DC, ">", 0)]], [sem.out(FC, constant=TURN - 1)]), "net"))
    # lastattackdown (M): frozen while a hurt face shows or damage is fresh.
    out.append(("rampage start", spec([calm + f + firing + [(RC, "=", 0)] for f in free], [sem.out(RC, constant=RAMPAGE)]),
                "net"))
    out.append(("rampage count", spec([calm + f + firing + [(RC, ">", 1)] for f in free],
                                      [sem.out(RC, networks=GREEN), sem.out(RC, constant=-1)]), "net"))
    out.append(("rampage hold", spec([[(DC, ">", 0)], hurt_face] + r6, [sem.out(RC, networks=GREEN)]), "net"))
    # damagecount (M).
    out.append(("damage count", spec([[(DC, ">", DECAY), (DC, "<=", CAP)]], [sem.out(DC, networks=GREEN),
                                                                           sem.out(DC, constant=-DECAY)]), "net"))
    out.append(("damage count cap", spec([[(DC, ">", CAP)]], [sem.out(DC, constant=CAP - DECAY)]), "net"))
    bit0, bit1 = mon.damage_bits()
    out.append(("damage count 1", spec(bit0, [sem.out(DC, constant=UNIT)]), "net"))
    out.append(("damage count 2", spec(bit1, [sem.out(DC, constant=2 * UNIT)]), "net"))
    # The attacker (M).
    for k in range(n):
        out.append((f"attacker {k}", sem.decider_spec(mon.dnf(mon.shot_clauses(k)), [sem.out(ATK, constant=RAW << k)]),
                    "net"))
    out.append(("attacker hold", spec([[(ATK, ">", 0), (ATK, "<", RAW)]], [sem.out(ATK, networks=GREEN)]), "net"))
    for k in range(n):
        out.append((f"attacker set {k}", spec([[(ATK, ">=", RAW << k), (ATK, "<", RAW << (k + 1))]],
                                              [sem.out(ATK, constant=k + 1)]), "net"))
    return out


# -- raycaster side

def bearing_stage(g, col, things):
    """Per monster k, FB_k: 1 if it is right of the view (outside 45 degrees), 2 if left, else 0,
    from the aim stage's network of alpha and L (col["aim G"]) and its negation. Returns the
    deciders (their outputs belong on the bus)."""
    ms = tc.monsters(things)
    if not ms:
        return []
    G = col["aim G"]
    NG = g.op("arith", sem.arith_spec(EACH, "*", -1, EACH, RED), red=[G], label="face bearing neg")
    out = []
    c = sem.cond
    for k, i in enumerate(ms):
        sa, sl = col["alpha"][i].signal, col["lat"][i].signal
        out.append(g.decider([c(sl, ">", 0, RED), c(sa, "<=", sl, RED, "and", RED)], [sem.out(FB[k], constant=1)],
                             red=[G], green=[NG], label=f"face right {k}"))
        out.append(g.decider([c(sl, "<", 0, RED), c(sa, "<=", sl, RED, "and", GREEN)], [sem.out(FB[k], constant=2)],
                             red=[G], green=[NG], label=f"face left {k}"))
    return out


def ideal_bearing(things, px, py, angle, xy):
    """FB per monster in floats from the player at (px, py) tiles facing angle (radians), monster
    positions xy {i: (x, y)}: (codes, margin) where margin is how near (tiles) a decision is to its
    boundary, so callers can skip near-ties."""
    dx, dy = math.cos(angle), math.sin(angle)
    codes, margin = [], 1e9
    for i in tc.monsters(things):
        tx, ty = xy[i]
        rx, ry = tx - px, ty - py
        a, lat = rx * dx + ry * dy, ry * dx - rx * dy
        margin = min(margin, abs(a - abs(lat)), abs(lat))
        codes.append(1 if 0 < lat and a <= lat else 2 if lat < 0 and a <= -lat else 0)
    return codes, margin


# -- the rules in plain Python

def step(mon, bus, net, shots, bits):
    """One tick: from the bus (health offset, "anim", "FE", "FB" list) and M ("FC", "RC", "DC",
    "ATK", "RND") at t - 1, the face state written at t: {"FE", "FC", "RC", "DC", "ATK"}. shots: per
    monster, its shot hits this tick (monsters.step); bits: the health the damage queue takes this
    tick (monsters.step's increment)."""
    fc, rc, dc, atk, rnd = (net.get(k, 0) for k in ("FC", "RC", "DC", "ATK", "RND"))
    fe = bus["FE"]
    a = bus["anim"]
    firing = 1 <= a < wp.LOWER
    hurt_face = 3 <= fe <= 5
    calm = dc == 0
    r6 = calm and not hurt_face and firing and rc == 1
    out = {}
    # the expression and the count
    if not calm:
        if atk >= RAW:
            out["FE"] = fe
        else:
            out["FE"] = HURT[bus["FB"][atk - 1]] if 1 <= atk <= len(bus["FB"]) else 0
        out["FC"] = TURN - 1
    elif r6:
        out["FE"], out["FC"] = RAMP, 0
    elif fc == 0:
        out["FE"] = next(e for e, (lo, hi) in enumerate(LOOK) if (lo is None or rnd >= lo) and (hi is None or rnd < hi))
        out["FC"] = STRAIGHT - 1 if rnd else 0
    else:
        out["FE"], out["FC"] = fe, fc - 1
    # lastattackdown
    if not calm or hurt_face or r6:
        out["RC"] = rc
    elif firing:
        out["RC"] = RAMPAGE if rc == 0 else rc - 1 if rc > 1 else rc
    else:
        out["RC"] = 0
    # damagecount
    out["DC"] = (min(dc, CAP) - DECAY if dc > DECAY else 0) + UNIT * bits
    # the attacker
    new = sum(RAW << k for k, s in enumerate(shots) if s)
    if atk >= RAW:
        hold = next(k + 1 for k in range(len(shots) - 1, -1, -1) if atk >= RAW << k)
    else:
        hold = atk
    out["ATK"] = new + hold
    return out
