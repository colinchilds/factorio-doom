"""The game (M2 movement, M3 textured walls): the raycaster driven by the input pad. Walk with
the D-pad, turn, slide along walls.

    screen (W x H lamps) | pad (demos/input_pad.py) | player logic below the pad

Player state is one register, REG: a decider `RESTART = 0 -> Everything` that adds its own output
(green loop) to the increments on its red input, and drops it all on a restart (Use pressed while
dead: RESTART on its green input for a tick; every state held on the bus drops too). It holds X and Y (displacement from SPAWN, 1/FP
tiles) and R (turn, 1024 per revolution, unbounded; the view angle is ((R >> 2) + A0) & 255,
with the >> 2 folded into the lookup tables keyed on R & 1023).

Player logic (a netlist graph, ext inputs REG and the pad's HELD network). Nothing on the input
path waits for collision; from REG, every tick, whatever is held (lat = 2 ticks behind REG):
    V+, V-   the step forward and back for the current angle, SPEED * (cos, sin)
    corners  for each corner of the player's box (half-size BOX = MARGIN + (lat + 2) * SPEED, so
             it covers how far REG moves meanwhile): in a wall? Range tests on X and Y.
    gates    per axis and direction: button held, the step's component points that way, and both
             box corners on that side are free -> the component goes into REG. Each axis alone,
             so you slide along walls and can't cut corners.
    turn     deciders -> REG directly.
    REG(t) = REG(t-1) + gates(HELD(t-2), REG(t-2-lat)) + turn(HELD(t-2)): a press shows up in REG
    two ticks after HELD, walking or turning.

REG also holds the status bar's numbers as offsets from a new game's (hud.START): HEALTH, ARMOR
and the four ammo counts. For now only the weapons change them (ammo); shooting comes later.

The state bus is REG's red output network. Besides REG it carries the weapon state (weapons.py),
held by deciders that read the bus and write back onto it, one tick per step:
    relays   FIRE (held) and NEXT (pressed) from the pad onto the bus (Each would bring every button)
    W, A, D  the weapon, its animation tick and queued switches (weapons.logic)
    spend    per weapon, at its shots' ticks: ammo -cost into REG (as an increment)

Monsters (monsters.py, 2026-11-03) keep their positions on the bus and their working signals
(offsets, sight lines, random numbers, flags, the damage queue) on a network of their own, M,
around a pole below the turn deciders: their deciders read the bus on red and M on green.

The raycaster (demos/raycast.py build_graph) takes its camera from the bus through a relay line
over the top of the screen to a pole left of it (its ext input). The status bar (demos/hud.py)
reads the same port, adding hud.START on the wire, and the weapon picture comes from W and A.

    python3 demos/game.py --size 64x40 --verify   # register vs reference, frames vs evaluation
"""
import argparse
import json
import math
import pathlib
import random
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "conformance"))
sys.path.insert(0, str(HERE))
from bp import DEFAULT_SUBSTATION_QUALITY, Blueprint, encode, quality_arg, sig  # noqa: E402
from compiler import netlist  # noqa: E402
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import EACH, GREEN, RED, Collect, Graph, Node, Scalar  # noqa: E402
import combat as cb  # noqa: E402
import hud as hd  # noqa: E402
import input_pad  # noqa: E402
import monsters as mo  # noqa: E402
import raycast  # noqa: E402
import touch as tc  # noqa: E402
import weapons as wp  # noqa: E402
from raycast import FP, MAP, trig  # noqa: E402

RED_IN, GREEN_IN, RED_OUT, GREEN_OUT = 1, 2, 3, 4
SX, SY, SR = sig("signal-X"), sig("signal-Y"), sig("signal-R")
HEALTH, ARMOR = sig("signal-H"), sig("signal-V")   # status bar values (offsets from hud.START)
AMMO = {"bullets": sig("signal-B"), "shells": sig("signal-J"), "rockets": sig("signal-K"), "cells": sig("signal-C")}
STATE = [SX, SY, SR, HEALTH, ARMOR] + list(AMMO.values())   # REG
WEAPON, ANIM, QUEUE = sig("signal-G"), sig("signal-T"), sig("signal-Q")   # weapon state (on the bus)
FIRE, NEXT, USE = sig(input_pad.FIRE), sig("signal-N"), sig("signal-U")   # relayed buttons (on the bus)
TURNC = sig("signal-L")    # ticks a turn button has been held (the turn counter, turn_counter)
RESTART = sig("signal-Z")  # one tick: Use pressed while dead (on the bus and REG's green input)
DEAD = sig("signal-O")     # the player is dead (on the turn counter's network: no turning)
BUS = STATE + [WEAPON, ANIM, QUEUE, FIRE, NEXT, USE, TURNC, RESTART, DEAD] + tc.GONE + cb.SIGNALS + mo.SIGNALS
TOUCH_SIGS = {"SX": SX, "SY": SY, "health": HEALTH, "armor": ARMOR, **AMMO}
SPAWN = (int(8.5 * FP), int(12.5 * FP))
assert SPAWN[0] % 2 == 0 and SPAWN[1] % 2 == 0, "camera_from folds SPAWN in after the | 1"
A0 = 192           # spawn facing north (angle 0 is east, angles grow clockwise on screen)
SPEED = 10         # 1/FP tiles per tick while walking: 2.3 tiles a second
ANGLE_SUB = 2      # R counts 2^ANGLE_SUB steps per view angle: 1024 per revolution
# Turning (R steps per tick): slow for the first RAMP ticks a turn button is held, so a tap (the
# pad holds a button 14 ticks) turns 14 steps, about 5 degrees, then fast: a turn in 4.3 s. User
# requests: 8 a tick was too stiff (2026-11-01), then 3 still too coarse to aim (2026-11-02). Doom
# also turns slowly for its first few tics.
DEFAULT_TURN = (1, 4, 18)   # (slow, fast, ramp ticks)
AMASK = (256 << ANGLE_SUB) - 1
TURN = AMASK + 1   # R per revolution


def angle_range(turn=DEFAULT_TURN):
    """R stays within a turn up to two ticks of the fastest turn either side (angle_wrap): the
    keys the angle lookups cover."""
    m = max(turn[0], turn[1])
    return (-2 * m - 2, TURN + 2 * m + 2)
NEAR_TIE = 0.02    # tiles: aim decisions this close to a boundary may round either way (verify)
MARGIN = 48        # player box half-size (1/FP tiles), before the latency allowance
MBOX = 25          # a monster's box in the player's corner tests: the player's box plus this (DX is a
                   # tick older than REG: the player's step, and the monster's steps meanwhile)
SPEND_ROWS = 24    # REG-input deciders (ammo spent, pickups, damage) per column


def walls():
    return [(x, y) for y, row in enumerate(MAP) for x, c in enumerate(row) if raycast.is_wall(c)]


def dead_terms(first=True):
    """Condition: the player is dead (health 0, as an offset from a new game's)."""
    return [sem.cond(HEALTH, "<=", -hd.START["health"], RED, None if first else "and")]


def gated(spec):
    """The spec with RESTART = 0 added to every clause: nothing comes out on a restart tick, so a
    value the decider holds (by copying it from the bus) drops to 0."""
    term = sem.cond(RESTART, "=", 0, RED, "and")
    conds = []
    for i, c in enumerate(spec["conditions"]):
        if i and c.get("compare_type") == "or":
            conds.append(dict(term))
        conds.append(c)
    return {**spec, "conditions": conds + [dict(term)]}


def velocity(a, fw=1):
    """Per-tick step (vx, vy) for lookup key a = R & AMASK, walking forward (fw 1) or back (-1)."""
    t = 2 * math.pi * (((a >> ANGLE_SUB) + A0) % 256) / 256
    return round(SPEED * math.cos(t)) * fw, round(SPEED * math.sin(t)) * fw


def wall_runs():
    """The walls as horizontal runs (x0, x1, y), for range conditions."""
    runs = []
    for x, y in sorted(walls(), key=lambda c: (c[1], c[0])):
        if runs and runs[-1][2] == y and runs[-1][1] == x - 1:
            runs[-1][1] = x
        else:
            runs.append([x, x, y])
    return runs


def player_graph(reg, reg_pos, held, held_pos, gate_pos, turn_pos, box, turn_rate=DEFAULT_TURN,
                 pressed=None, pressed_pos=None, wm_pos=None, things=(), turnc=None, turnc_pos=None, restart_pos=None,
                 mnet=None, mnet_pos=None):
    """The player logic. Returns (graph, nodes) with nodes S (the bus), H, P (ext), gates, turn and
    spend (fixed sinks feeding REG), bus (fixed sinks writing the bus: relays and weapon state),
    and lat: how many ticks the gates' REG-derived inputs run behind REG.

    Nothing on the input path waits for collision. From REG, every tick and whatever is held:
      V+, V-   the step forward and back for the current angle (signed lookups on R & AMASK);
      corners  for each corner of the player's box (half-size `box`): is it in a wall? Range
               tests on X and Y straight from REG (one decider per corner).
    The gates then pass a step when its button is held and the box's two corners on that side are
    free: per axis and direction, (W held, V+x > 0, both +x corners free) or (W held, V+x < 0,
    both -x corners free) -> copy V+x into REG. Each axis is gated alone, so you slide along walls.
    A press reaches REG in two ticks, like a turn.

    Monsters (touch.py) are solid: their boxes (half-size `box`, so the player's box can't straddle
    one between its corners) join the walls in the corner tests. Thing contact (pickups, pain)
    writes the bus and REG like the weapon logic."""
    g = Graph(reserved=BUS + mo.NET + [sig(s) for s in input_pad.BUTTON_SIGNALS])
    S = g.ext(reg, reg_pos, label="REG")
    # The monsters' network (monsters.py): a pole the caller placed; its writers and readers wire to it.
    M = g.ext(mnet, mnet_pos, size=(1, 1), connectors=(1, 2), label="M") if mnet is not None else None
    H = g.ext(held, held_pos, size=(1, 2), label="HELD")
    # Weapons: the relays put fire (held) and next (pressed) on the bus; the weapon state deciders
    # read the bus and write W and A back onto it (or spend ammo into REG).
    bus, spend, net = [], [], []
    P = None
    if pressed:
        P = g.ext(pressed, pressed_pos, size=(1, 2), label="PRESSED")
        relay = lambda src, s, label: Node(g, "arith", sem.arith_spec(s, "+", 0, s, RED), red=[src], time=1, label=label)  # noqa: E731
        relays = [relay(H, FIRE, "fire held"), relay(P, NEXT, "next pressed"), relay(P, USE, "use pressed")]
        bus += relays
        start = {k: v for k, v in hd.START.items() if k in AMMO}
        specs = wp.logic(WEAPON, ANIM, QUEUE, FIRE, NEXT, AMMO, start, (HEALTH, hd.START["health"]))
        mon = None
        if things:
            mon = mo.Monsters(things, walls(), SPAWN, TOUCH_SIGS, hd.START)
            specs += (tc.Logic(things, TOUCH_SIGS, hd.START, SPAWN).specs() + cb.logic(things, WEAPON, ANIM, AMMO, start)
                      + mon.specs())
        # Every state the bus (or M) holds drops to 0 on a restart (gated); the REG writers need no
        # gate: REG drops its input that tick, and none of them fires for a dead player afterwards.
        # The arithmetic M writers (monsters.py: offsets, random numbers) hold nothing. Deciders
        # read the bus on red and M (if any) on green.
        mg = [M] if M else []
        io = lambda v: [S] if v == "S" else [M] if v == "M" else [g.const(v, "monster const")] if v else []  # noqa: E731
        for item in specs:
            label, spec, target = item[:3]
            if target in ("bus", "net"):
                n = Node(g, "decider", gated(spec), red=[S], green=mg, time=1, label=label)
            elif target == "reg":
                n = Node(g, "decider", spec, red=[S], green=mg, time=1, label=label)
            else:   # netarith
                n = Node(g, "arith", spec, red=io(item[3].get("red")), green=io(item[3].get("green")), time=1, label=label)
            {"reg": spend, "bus": bus}.get(target, net).append(n)
        if M:
            M.green += net   # their outputs: onto M
        if mon:
            # Line of sight (two ticks deep): onto M. A row per monster below the bus writers'
            # columns (wm_pos), a free column left of each node for its constant.
            los_rows = mon.los(g, S)
            los = [lk for lk, _ in los_rows]
            M.green += los
            if wm_pos:
                for k, (lk, prods) in enumerate(los_rows):
                    for j, n in enumerate(prods + [lk]):
                        n.fixed, n.pos = True, (wm_pos[0] + 1 + 3 * j, wm_pos[1] + SPEND_ROWS + 1 + 2 * k)
    if wm_pos:
        # Columns snaking down and up (so each chain stays short): the spend deciders (REG's input,
        # so the chain starts next to REG), then the bus writers.
        c0 = 0
        for lst in (spend, bus, net):
            for i, n in enumerate(lst):
                c, r = divmod(i, SPEND_ROWS)
                c += c0
                n.fixed, n.pos = True, (wm_pos[0] + 2 * c, wm_pos[1] + (r if c % 2 == 0 else SPEND_ROWS - 1 - r))
            c0 += -(-len(lst) // SPEND_ROWS)
    # Death and restart: Use pressed while dead -> RESTART for a tick, onto the bus (the gated
    # deciders above drop their state) and REG's green input (REG drops its values). Fixed next to
    # REG (restart_pos); game.build wires its outputs.
    restart = []
    if pressed:
        restart = [Node(g, "decider", sem.decider_spec(dead_terms() + [sem.cond(USE, ">", 0, RED, "and")],
                                                       [sem.out(RESTART, constant=1)]),
                        red=[S], time=1, label="restart")]
        restart[0].fixed, restart[0].pos = True, restart_pos
    # Turning: straight from HELD into REG, slow until the turn counter (TURNC, the caller's
    # register: turn_counter) reaches the ramp, and not while dead (DEAD on the counter's network).
    slow, fast, ramp = turn_rate
    TC = g.ext(turnc, turnc_pos, label="turn counter")
    turn = [g.decider([sem.cond(sig(b), ">", 0, RED), sem.cond(TURNC, "<" if k == 0 else ">=", ramp, GREEN, "and"),
                       sem.cond(DEAD, "=", 0, GREEN, "and")],
                      [sem.out(SR, constant=d * r)], red=H, green=TC, label=f"turn{'+' if d > 0 else '-'} {r}")
            for b, d in ((input_pad.RIGHT, 1), (input_pad.LEFT, -1)) for k, r in enumerate((slow, fast))]
    for i, n in enumerate(turn):
        n.fixed, n.pos = True, (turn_pos[0], turn_pos[1] + i)
    X, Y, R = g.scalar(S, SX), g.scalar(S, SY), g.scalar(S, SR)
    V = {fw: [g.lookup_scalar(R, {a: velocity(a % TURN, fw)[i] for a in range(*angle_range(turn_rate))}, 32, f"v{fw:+d} {'xy'[i]}", out=s)
              for i, s in enumerate((SX, SY))] for fw in (1, -1)}
    # Corner (sx, sy) of the box is in a wall: X + SPAWN + sx*box in [256 x0, 256 (x1 + 1)) and the
    # same for Y, for some wall run.
    corner = {}
    for sx in (1, -1):
        for sy in (1, -1):
            ox, oy = SPAWN[0] + sx * box, SPAWN[1] + sy * box
            conds = []
            boxes = [(256 * x0, 256 * (x1 + 1), 256 * y, 256 * (y + 1), []) for x0, x1, y in wall_runs()]
            for x0, x1, y0, y1, extra in boxes:
                conds += [sem.cond(SX, ">=", x0 - ox, RED, "or" if conds else None),
                          sem.cond(SX, "<", x1 - ox, RED, "and"),
                          sem.cond(SY, ">=", y0 - oy, RED, "and"),
                          sem.cond(SY, "<", y1 - oy, RED, "and")] + extra
            # A monster's box only while it lives (combat.py: DMG on the bus), where it is: the
            # player's offset from it (monsters.py DX, DY: a tick older than REG, and it moves
            # meanwhile), so the box is bigger by that allowance (MBOX).
            for k, i in enumerate(tc.monsters(things or [])):
                bm = box + MBOX
                conds += [sem.cond(mo.DX[k], ">=", -bm - sx * box, GREEN, "or" if conds else None),
                          sem.cond(mo.DX[k], "<", bm - sx * box, GREEN, "and"),
                          sem.cond(mo.DY[k], ">=", -bm - sy * box, GREEN, "and"),
                          sem.cond(mo.DY[k], "<", bm - sy * box, GREEN, "and")] + \
                    [sem.cond(s_, c, v, RED, "and") for s_, c, v in cb.alive_terms(things, i)]
            if pressed:
                # A dead player can't move: every corner reads as in a wall. This also covers the
                # restart's jump back to the spawn (the corners run lat ticks behind REG).
                conds += [dict(dead_terms()[0], compare_type="or")]
            s = g.new_scalar_signal()
            corner[sx, sy] = (s, g.decider(conds, [sem.out(s, constant=1)], red=S, green=[M] if M else [],
                                           label=f"corner {sx:+d}{sy:+d}"))
    lat = V[1][0].node.time
    cnodes = [g.delay(n, lat - n.time) for _, n in corner.values()]
    gates = []
    for fw, button in ((1, input_pad.FORWARD), (-1, input_pad.BACK)):
        for i, out_sig in enumerate((SX, SY)):
            conds = []
            for d in (1, -1):
                free = [corner[(d, o) if i == 0 else (o, d)][0] for o in (1, -1)]
                conds += [sem.cond(sig(button), ">", 0, RED, "or" if conds else None),
                          sem.cond(out_sig, ">" if d > 0 else "<", 0, GREEN, "and")]
                conds += [sem.cond(c, "=", 0, GREEN, "and") for c in free]
            # Built directly (no latency balancing): HELD is read as it is now, V and the corners
            # lat ticks behind REG.
            n = Node(g, "decider", sem.decider_spec(conds, [sem.out(out_sig, networks=GREEN)]),
                     red=[H], green=V[fw][0].node.nodes + V[fw][1].node.nodes + cnodes, time=lat + 1, label=f"gate {fw:+d} {'xy'[i]}")
            n.fixed, n.pos = True, (gate_pos[0], gate_pos[1] + len(gates))
            gates.append(n)
    return g, {"S": S, "H": H, "P": P, "TC": TC, "gates": gates, "turn": turn, "spend": spend, "bus": bus, "lat": lat,
               "restart": restart, "los": los if pressed and things else [], "mon": mon if pressed and things else None,
               "net": net, "M": M}


def turn_counter(b, x, y):
    """TURNC: ticks a turn button has been held (on HELD, its red input), 0 otherwise. A decider
    copying its own output (green loop) plus 1 while left or right is held."""
    turning = [sem.cond(sig(input_pad.RIGHT), ">", 0, RED), sem.cond(sig(input_pad.LEFT), ">", 0, RED, "or")]
    n = b.decider(x, y, turning, [sem.out(TURNC, networks=GREEN), sem.out(TURNC, constant=1)], direction=4)
    b.wire(n, GREEN_OUT, n, GREEN_IN)
    return n


def dead_flag(b, x, y):
    """DEAD = 1 while the player is dead: reads the bus on red, its green output joins the turn
    counter's network, where the turn deciders read it (the bus is red only)."""
    return b.decider(x, y, dead_terms(), [sem.out(DEAD, constant=1)], direction=4)


def angle_wrap(b, x, y):
    """Two deciders keeping R within a turn: R >= TURN -> -TURN into REG, R < 0 -> +TURN. They read
    the bus (REG) on red and their own outputs on green, so a correction counts the tick it is on
    its way to REG and fires once. R overshoots by at most two ticks' turning (angle_range)."""
    hi = b.decider(x, y, [sem.cond(SR, ">=", TURN)], [sem.out(SR, constant=-TURN)], direction=4)
    lo = b.decider(x, y + 1, [sem.cond(SR, "<", 0)], [sem.out(SR, constant=TURN)], direction=4)
    b.wire(hi, GREEN_OUT, hi, GREEN_IN)
    b.wire(hi, GREEN_IN, lo, GREEN_IN)
    b.wire(lo, GREEN_IN, lo, GREEN_OUT)
    b.wire(lo, RED_IN, hi, RED_IN)
    b.wire(lo, RED_OUT, hi, RED_OUT)
    return hi, lo


def wrap_step(r, w):
    """angle_wrap's output from R and its own previous output (the reference)."""
    v = r + w
    return -TURN if v >= TURN else TURN if v < 0 else 0


def turn_counts(H, ticks):
    """The counter's output per tick from HELD per tick (the network at t holds HELD(t - 1))."""
    out, v = [], 0
    for t in range(ticks):
        held = t >= 1 and (H[t - 1].get(input_pad.LEFT, 0) > 0 or H[t - 1].get(input_pad.RIGHT, 0) > 0)
        v = v + 1 if held else 0
        out.append(v)
    return out


def monster_boxes(things):
    """Monster centres (1/FP tiles) and their alive terms: solid while alive, as boxes of the
    collision box's half-size."""
    return [(int(things[i][0] * FP), int(things[i][1] * FP), cb.alive_terms(things, i)) for i in tc.monsters(things or [])]


def camera_from(port, port_pos, things=(), turn=DEFAULT_TURN):
    """The raycaster's camera from the state port. With things: hides picked-up items, shows each
    monster's frame from its clock and puts the aim (combat.aim_stage) onto the port, i.e. the bus;
    camera.aim holds those nodes once built."""
    hidden = set(tc.items(things or []))
    mons = set(tc.monsters(things or []))

    def hide(g, i, out):
        """Item i's GONE flag times things.HIDE onto its depth signal."""
        if i not in hidden:
            return None
        return g.ss(g.scalar(g_port[0], tc.GONE[i]), "*", raycast.th.HIDE, f"hide {i}", out=out)

    g_port = []

    def frame(g, i, fid):
        """Monster i's frame (as an offset from its standing frame): a lookup on its clock."""
        if i not in mons:
            return None
        return g.lookup_scalar(g.scalar(g_port[0], cb.MS[i]), cb.frame_offsets(things[i][2], fid), 6, f"frame {i}")

    order = {i: k for k, i in enumerate(tc.monsters(things or []))}

    def pos(g, i):
        """Monster i's offset from the camera: (MX - X) + (its spawn - SPAWN) per axis, one op from
        the port like a standing thing's."""
        if i not in order:
            return None
        k, out = order[i], []
        for ax, (M, P) in enumerate(((mo.MX, SX), (mo.MY, SY))):
            s = g.new_scalar_signal()
            n = g.op("arith", sem.arith_spec(M[k], "-", P, s, RED, RED), red=[g_port[0]], label=f"r{'xy'[ax]}{i}")
            c = int(things[i][ax] * FP) - SPAWN[ax]
            out.append(Scalar(Collect([n, g.const([(s, c)], "thing offset")]), s))
        return out

    def aim(g, col, D, lane):
        nodes = cb.aim_stage(g, col, D, lane, things, g_port[0])
        g_port[0].red += nodes   # wired to the port: onto the bus
        camera.aim[:] = nodes

    def camera(g):
        S = g.ext(port, port_pos, size=(1, 1), connectors=(1, 2), label="state port")
        g_port[:] = [S]
        camera.port = S
        A = g.scalar(S, SR)   # wrapped near one turn (angle_wrap); A0, >> ANGLE_SUB and the wrap are in the tables
        X0, Y0 = g.scalar(S, SX), g.scalar(S, SY)
        # (X0 + SPAWN) | 1 = (X0 | 1) + SPAWN for an even SPAWN, which rides on the consumers' wire.
        PX, PY = ((lambda o: Scalar(Collect([o.node, g.const([(o.signal, sp)], "spawn")]), o.signal))(g.ss(p, "OR", 1, n))
                  for p, sp, n in ((X0, SPAWN[0], "px"), (Y0, SPAWN[1], "py")))
        extra = {"port": S, "angle_offset": A0, "angle_sub": ANGLE_SUB, "angle_range": angle_range(turn),
                 "pos0": (X0, Y0, SPAWN), "hide": hide}
        if mons:
            extra.update(frames=cb.frames(things), frame=frame, aim=aim, thing_pos=pos)
        return A, PX, PY, extra
    camera.aim = []
    return camera


def hud_from_port(g, extra):
    """The status bar's values: REG's offsets plus a new game's numbers (on the wire), and the
    current weapon's ammo (-1, nothing shown, for the fist): per ammo type a decider copies the
    count while the weapon uses it, and Each + 0 sums them onto one signal."""
    keys = {"health": HEALTH, "armor": ARMOR, **AMMO}
    base = g.const([(s, hd.START[k]) for k, s in keys.items()], "hud start")
    hs = g.vv(extra["port"], "+", base, "hud state")
    CUR = g.new_scalar_signal()
    sel = []
    for k, s in list(AMMO.items()) + [(None, CUR)]:
        conds = []
        for wi, w in enumerate(wp.WEAPONS):
            if w["ammo"] == k:
                conds.append(sem.cond(WEAPON, "=", wi, RED, "or" if conds else None))
        sel.append(g.decider(conds, [sem.out(s, networks=RED) if k else sem.out(CUR, constant=-1)], red=hs,
                             label=f"current ammo {k or 'none'}"))
    cur = g.op("arith", sem.arith_spec(EACH, "+", 0, CUR, RED), red=sel, label="current ammo")
    out = g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=[hs, cur], label="hud values")
    return out, {**keys, "ammo": CUR}


def weapon_from_port(g, extra):
    """The weapon picture: a lookup on W * KEYW + A."""
    port = extra["port"]
    K = g.ss(g.ss(g.scalar(port, WEAPON), "*", wp.KEYW), "+", g.ss(g.scalar(port, ANIM), "OR", 0, "anim"), "weapon key")
    table = wp.pictures()[1]
    return g.lookup_scalar(K, table, max(table.values()).bit_length(), "weapon picture")


def build(w, h, n=None, hold=input_pad.DEFAULT_HOLD, substation_quality=DEFAULT_SUBSTATION_QUALITY,
          tex=raycast.DEFAULT_TEX, flat=raycast.DEFAULT_FLAT, things=raycast.th.THINGS, turn=DEFAULT_TURN,
          shade=None, hud=True, weapon=True, rows=raycast.DEFAULT_ROWS, view=None):
    n = n or input_pad.button_size(h)
    b = Blueprint(f"Factorio Doom M4 {w}x{h}", substation_quality)
    lattice = b.grid.hole

    # Pad right of the screen (beside it: the player zooms in on the lamps), player logic below it.
    px0 = w + 2
    pad_h = 4 * n + 10
    py0 = max(0, (h - pad_h) // 2 - 10)
    pad = input_pad.build_pad(b, px0, py0, n, hold)
    pad_bottom = max(int(e["position"]["y"]) + 1 for e in b.entities)
    reg_pos = (w + 4, pad_bottom + 4)
    # REG holds its values (its own output on green) plus the increments on red, and drops them all
    # on a restart (RESTART on green).
    reg = b.decider(*reg_pos, [sem.cond(RESTART, "=", 0, GREEN)], [sem.out(netlist.EVERYTHING)], direction=4)
    b.wire(reg, GREEN_OUT, reg, GREEN_IN)
    d1 = b.entities[pad["d1"] - 1]["position"]
    held_pos = (int(d1["x"]), int(d1["y"] - 1))
    d3 = b.entities[pad["d3"] - 1]["position"]
    pressed_pos = (int(d3["x"]), int(d3["y"] - 1))
    gate_pos = (reg_pos[0], reg_pos[1] + 1)
    turn_pos = (reg_pos[0] + 2, reg_pos[1])
    turnc_pos = (reg_pos[0] + 2, reg_pos[1] + 4)
    turnc = turn_counter(b, *turnc_pos)
    wrap = angle_wrap(b, turnc_pos[0], turnc_pos[1] + 1)
    wm_pos = (reg_pos[0] + 4, reg_pos[1])
    restart_pos = (reg_pos[0], reg_pos[1] + 5)
    dead = dead_flag(b, reg_pos[0], reg_pos[1] + 6)

    # The monsters' network M: a pole below the turn deciders, left of the writers' columns.
    mnet_pos = (reg_pos[0] + 3, reg_pos[1] + SPEND_ROWS + 1)
    mnet = b.pole(*mnet_pos) if things else None

    def make(box):
        return player_graph(reg, reg_pos, pad["d1"], held_pos, gate_pos, turn_pos, box, turn,
                            pad["d3"], pressed_pos, wm_pos, things, turnc, turnc_pos, restart_pos, mnet, mnet_pos)

    g, nodes = make(MARGIN)
    lat = nodes["lat"]
    # The box: the margin, plus how far REG moves between the REG value a gate's inputs come
    # from and the REG value its step lands in (lat + 2 steps).
    probe = MARGIN + (lat + 2) * SPEED
    g, nodes = make(probe)
    pre = b.occupied()
    ent_p = g.compile(b, blocked=lambda x, y: (x, y) in pre,
                      region=lambda x, y: x >= w + 4 and y >= pad_bottom + 3, x_right=w + 4 + 3 * lat)
    for n in nodes["gates"] + nodes["turn"]:
        b.wire(ent_p[n.id][1], RED_OUT, reg, RED_IN)
    b.wire(turnc, RED_IN, ent_p[nodes["turn"][0].id][1], RED_IN)   # the counter reads HELD too
    b.wire(wrap[0], RED_IN, reg, RED_OUT)
    b.wire(wrap[0], RED_OUT, reg, RED_IN)
    b.wire(dead, RED_IN, wrap[0], RED_IN)        # reads the bus
    b.wire(dead, GREEN_OUT, turnc, GREEN_OUT)    # onto the turn counter's network
    for n in nodes["restart"]:
        e = ent_p[n.id][1]
        b.wire(e, RED_OUT, e, RED_IN)            # onto the bus (it reads it)
        b.wire(e, GREEN_OUT, reg, GREEN_IN)      # and REG's green input
    # Weapon state onto the bus (REG's red output), ammo spent into REG's input: chained. Most bus
    # writers also read the bus (not the relays: HELD, PRESSED), so their chain joins it through
    # one's own input.
    for lst, conn in ((nodes["bus"], RED_OUT), (nodes["spend"], RED_IN)):
        es = [ent_p[n.id][1] for n in lst]
        b.chain(es, RED_OUT)
        if not es:
            continue
        if conn == RED_OUT:
            n = next(n for n in lst if n.red == [nodes["S"]])
            b.wire(ent_p[n.id][1], RED_OUT, ent_p[n.id][1], RED_IN)
        else:
            b.wire(es[0], RED_OUT, reg, conn)

    # Raycaster. Its camera port is a substation left of the column stage, on the lattice row
    # through the top of the screen; that row of substations carries the state bus from REG
    # (right of the screen) across, so there are no poles of its own.
    cam = camera_from(None, (0, 0), things, turn)
    gr, cols, rows, scal, layout = raycast.build_graph(w, h, cam, Graph(reserved=BUS),
                                                       hole=lattice, tex=tex, flat=flat, things=things, shade=shade,
                                                       hud=hud_from_port if hud else None,
                                                       weapon=weapon_from_port if weapon else None, rows_per_net=rows,
                                                       view=view)
    grid = b.grid
    auto = [n.time for n in gr.nodes if not n.fixed and n.kind != "const" and n.time is not None]
    x_left = layout["x_right"] - 3 * (max(auto) - min(auto)) - 3
    y_s = grid.offset
    xs = [x for x in range(x_left - grid.period, w + grid.period) if (x - grid.offset) % grid.period == 0]
    xs = [x for x in xs if x <= x_left][-1:] + [x for x in xs if x_left < x < w]
    if xs[-1] < w - 1:
        xs.append(w)   # in the gap between the screen and the pad (pad at w + 2)
    subs = [b.substation(x, y_s) for x in xs]
    b.chain(subs, 1)
    S = cam.port
    S.spec["entity"], S.spec["size"], S.pos = subs[0], (2, 2), (xs[0], y_s)
    S.spec["taps"] = [(e, (x, y_s), (2, 2)) for e, x in zip(subs[1:], xs[1:])]
    ent_r = gr.compile(b, blocked=lattice, region=lambda x, y: x <= -2, x_right=layout["x_right"])
    raycast.wire_screen(b, ent_r, cols, layout, w, h, rows)
    nodes["aim"], nodes["aim_graph"], nodes["port"] = cam.aim, gr, getattr(cam, "port", None)
    # REG to the row's last substation (right of the screen), through relay poles if needed.
    reg_fp, sub_fp = (reg_pos[0], reg_pos[1], reg_pos[0] + 2, reg_pos[1] + 1), (xs[-1], y_s, xs[-1] + 2, y_s + 2)
    netlist._relay(b, reg_fp, (reg, RED_OUT), sub_fp, (subs[-1], 1), 1, lambda x, y: not lattice(x, y), b.occupied(),
              c_pole=True)

    raycast._substations(b)
    b.power()
    b.link_power()
    b.check_reach()
    return b, pad, g, nodes, gr, cols, rows, scal, layout, lat, probe


# ---------------------------------------------------------------------------------------------
# Verification

# The fight (monsters.py, combat.py), after the weapon tests and the walk above (the monsters face
# away from the spawn, so they sleep through the weapon tests until the walk passes in front of
# them): (button, from, to) as there. Turn to imp 2, which came at us, and shotgun it mid-attack (it
# dies); to the chaingun, turn to zombieman 3 (asleep) and fire: a flinch, then it dies; four taps to
# the fist while imp 0 walks around the red block (a detour) and throws fireballs; walk up, turn to
# it and punch four times (a flinch each time, and it doesn't attack meanwhile); stop, and it
# scratches us to death. Dead: fire, switch, turn and walk (all ignored), then Use: a new game
# (monsters back where they started, asleep), walk and fire.
FIGHT_SCRIPT = [("ring 1,0", 680, 704), ("fire", 725, 727),
                ("weapon", 800, 801), ("ring -1,0", 800, 836), ("ring -1,0", 848, 848), ("fire", 875, 877),
                ("weapon", 1000, 1001), ("weapon", 1018, 1019), ("weapon", 1036, 1037), ("weapon", 1054, 1055),
                ("ring 1,0", 1100, 1134), ("ring 0,-1", 1160, 1210), ("ring 1,0", 1225, 1245), ("fire", 1300, 1445),
                ("fire", 2140, 2150), ("weapon", 2160, 2161), ("ring 1,0", 2170, 2185), ("ring 0,-1", 2190, 2200),
                ("use", 2215, 2215), ("ring 0,-1", 2240, 2265), ("fire", 2275, 2280)]


def schedule(pad, ticks, seed=2, keys=False):
    """Drops every 8-12 ticks while a button is held. With keys, the same script typed on the
    keyboard instead (mod/doom-input): the keyboard combinators hold a button's signal from the
    entry's start through its end, and a weapon entry is a one-tick press at its start."""
    # Timing and chest choice from separate generators: the pad's chests per button depend on the
    # screen size, and choice() over a different count would shift the timing, so the walk would
    # differ by size.
    rnd, pick = random.Random(seed), random.Random(seed + 1)
    cells = pad["cells"]
    # Weapons: a switch is about 55 ticks (lower, raise); fire during one is ignored.
    script = [("fire", 30, 38),                # pistol: one shot
              ("weapon", 60, 61),              # -> shotgun
              ("fire", 125, 130),
              ("weapon", 180, 181),            # -> chaingun, held: refires
              ("fire", 245, 300),
              ("weapon", 280, 281),            # -> rocket launcher, while firing
              ("fire", 345, 350),
              # Taps further apart than the pad's hold (closer ones are one held press): two
              # while it lowers (queued: plasma, BFG), one while the BFG rises (lowers again: fist).
              ("weapon", 400, 401), ("weapon", 418, 419), ("weapon", 436, 437),
              ("fire", 480, 485),              # during the switch: ignored
              ("fire", 525, 535),              # punch
              ("weapon", 560, 561),            # -> pistol
              ("weapon", 600, 601),            # a press while it rises: lowers again (-> shotgun)
              ("ring 0,-1", 5, 150),           # walk north into the wall ahead: collision
              ("ring 1,0", 165, 230),          # turn right
              ("ring 1,-1", 245, 420),         # forward + right: an arc, sliding on walls
              ("ring 0,1", 430, 520)] + FIGHT_SCRIPT   # back up
    if keys:
        return key_schedule(pad, script, ticks)
    inserts = []
    for cell, start, end in script:
        t = start
        while t <= min(end, ticks - 20):
            inserts.append([t, pick.choice(cells[cell]["chests"]), "iron-plate", 1])
            t += rnd.randint(8, 12)
    return sorted(inserts)


def key_schedule(pad, script, ticks):
    """Keyboard combinator writes for a script of (cell, start, end): [t, entity, signal, 0/1]."""
    held = [set() for _ in range(ticks)]
    pressed = [set() for _ in range(ticks)]
    for cell, start, end in script:
        if start > ticks - 20:
            continue
        if cell == "weapon":
            pressed[start].add(NEXT["name"])
            continue
        for t in range(start, min(end, ticks - 20) + 1):
            held[t] |= set(pad["cells"][cell]["signals"])
    out = []
    for ent, per in ((pad["keys_held"], held), (pad["keys_pressed"], pressed)):
        prev = set()
        for t, now in enumerate(per):
            out += [[t, ent, s, 1] for s in sorted(now - prev)] + [[t, ent, s, 0] for s in sorted(prev - now)]
            prev = now
    return sorted(out)


def reference_states(pad, g, nodes, inserts, ticks, hold, lat):
    """REG's output per tick, the bus's (REG plus the weapon state and relays) and the monsters'
    network M's per tick, from the pad's reference and the graph's own evaluation of its fixed
    nodes. Outputs at t read the networks at t, which hold the outputs at t - 1."""
    H, P, _ = input_pad.reference(pad, inserts, ticks, hold)
    turnc = turn_counts(H, ticks)   # the turn counter's output per tick
    hvec = lambda t: {sem.key(sig(s)): v for s, v in H[t].items() if v} if t >= 0 else {}  # noqa: E731
    pvec = lambda t: {sem.key(sig(s)): v for s, v in P[t].items() if v} if t >= 0 else {}  # noqa: E731
    keys = [sem.key(s) for s in STATE]
    state = [{k: 0 for k in keys} for _ in range(ticks)]
    wm = [{} for _ in range(ticks)]     # weapon state and relay outputs
    spend = [{} for _ in range(ticks)]
    bus = [{} for _ in range(ticks)]    # what the bus network holds at t + 1
    mnet = [{} for _ in range(ticks)]   # what M holds at t + 1
    M = nodes.get("M")
    inc_cache = {}
    # The aim (raycaster nodes wired onto the bus): at t from the port at t - tau.
    aim = nodes.get("aim") or []
    tau = aim[0].time if aim else 0
    akeys = [sem.key(s) for s in (SX, SY, SR)] + [sem.key(s) for s in cb.DMG + mo.MX + mo.MY]
    aim_cache = {}

    def aim_out(t):
        net = bus[t - tau] if t - tau >= 0 else {}
        k = tuple(net.get(a, 0) for a in akeys)
        if k not in aim_cache:
            acc = {}
            for o in nodes["aim_graph"].evaluate(0, aim, {nodes["port"]: {a: v for a, v in zip(akeys, k) if v}}):
                sem.add_into(acc, o)
            aim_cache[k] = acc
        return aim_cache[k]
    dkeys = [sem.key(s) for s in cb.DMG]
    mkeys = [sem.key(s) for s in mo.DX + mo.DY]
    # Line of sight (monsters.py): two ticks deep, from the bus then.
    los = nodes.get("los") or []
    lkeys = [sem.key(s) for s in (SX, SY)] + [sem.key(s) for s in mo.MX + mo.MY]
    los_cache = {}

    def los_out(t):
        net = bus[t - 2] if t >= 2 else {}
        k = tuple(net.get(a, 0) for a in lkeys)
        if k not in los_cache:
            acc = {}
            for o in g.evaluate(0, los, {nodes["S"]: {a: v for a, v in zip(lkeys, k) if v}}):
                sem.add_into(acc, o)
            los_cache[k] = acc
        return los_cache[k]
    RK, HK, ZK = sem.key(SR), sem.key(HEALTH), sem.key(RESTART)
    wrap = [0] * ticks   # angle_wrap's output at t: from REG and itself at t - 1
    for t in range(ticks):
        prev = state[t - 1] if t else {k: 0 for k in keys}
        if t:
            wrap[t] = wrap_step(prev[RK], wrap[t - 1])
        # A gate's output in REG(t) read HELD(t - 2) and V/corners computed from REG(t - 2 - lat).
        src = t - 2 - lat
        if src >= 0:
            # The corners read the bus: REG, and DMG, DX and DY for the monsters' boxes.
            s_in = {k: v for k, v in state[src].items() if v}
            s_in.update({k: bus[src][k] for k in dkeys if bus[src].get(k)})
            m_in = {k: mnet[src][k] for k in mkeys if mnet[src].get(k)}
            ck = (tuple(sorted(s_in.items())), tuple(sorted(m_in.items())), tuple(sorted(hvec(t - 2).items())))
            if ck not in inc_cache:
                inc_cache[ck] = {}
                gins = {nodes["S"]: s_in, nodes["H"]: hvec(t - 2), **({M: m_in} if M else {})}
                for o in g.evaluate(0, nodes["gates"], gins):
                    sem.add_into(inc_cache[ck], o)
            inc = dict(inc_cache[ck])
        else:
            inc = {}
        tcv = {sem.key(TURNC): turnc[t - 2]} if t >= 2 and turnc[t - 2] else {}
        # The dead flag on the counter's network at t - 1: its output at t - 2, from the bus then.
        if t >= 3 and state[t - 3][HK] <= -hd.START["health"]:
            tcv[sem.key(DEAD)] = 1
        for o in g.evaluate(0, nodes["turn"], {nodes["H"]: hvec(t - 2), nodes["TC"]: tcv}):
            sem.add_into(inc, o)
        if t:
            sem.add_into(inc, spend[t - 1])
            sem.add_into(inc, {RK: wrap[t - 1]} if wrap[t - 1] else {})
        state[t] = {k: sem.i32(prev[k] + inc.get(k, 0)) for k in keys}
        if t and wm[t - 1].get(ZK):   # a restart: REG drops everything
            state[t] = {k: 0 for k in keys}
        # The bus network at t holds the outputs at t - 1.
        net = bus[t - 1] if t else {}
        ins = {nodes["S"]: net, nodes["H"]: hvec(t - 1), nodes["P"]: pvec(t - 1)}
        if M:
            ins[M] = mnet[t - 1] if t else {}
        for o in g.evaluate(0, nodes["bus"] + nodes["restart"], ins):
            sem.add_into(wm[t], o)
        if aim:
            sem.add_into(wm[t], aim_out(t))
        for o in g.evaluate(0, nodes["spend"], ins):
            sem.add_into(spend[t], o)
        for o in g.evaluate(0, nodes.get("net") or [], ins):
            sem.add_into(mnet[t], o)
        if los:
            sem.add_into(mnet[t], los_out(t))
        bus[t] = {k: v for k, v in state[t].items() if v}
        sem.add_into(bus[t], wm[t])
    return state, bus, mnet


def verify(path, b, pad, g, nodes, gr, cols, rows, layout, w, h, inserts, ticks, hold, lat, things, turn, probe):
    ins_path = path.with_suffix(".inserts.json")
    ins_path.write_text(json.dumps(inserts))
    reg = nodes["S"].spec["entity"]
    out_dir = ROOT / "out" / f"game_{w}x{h}_ppm"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.ppm"):
        f.unlink()
    M = nodes.get("M")
    mnet_args = ["--net", f"{M.spec['entity']}:2"] if M else []
    res = subprocess.run([str(ROOT / "target" / "release" / "fsim"), "run", str(path), "--ticks", str(ticks),
                          "--input", str(ins_path), "--probe", str(reg), "--net", f"{reg}:{RED_OUT}"] + mnet_args
                         + ["--ppm", str(out_dir)],
                         check=True, capture_output=True, text=True)
    got, got_bus, got_m = {}, {}, {}
    for line in res.stdout.splitlines():
        if not line.startswith("t="):
            continue
        t = int(line.split()[0][2:])
        vals = dict(kv.split("=") for kv in line.split(": ", 1)[1].split()) if ": " in line.strip() else {}
        d = {sem.key(sig(f"signal-{k[-1]}" if not k.startswith("signal-") and len(k) == 1 else k)): int(v)
             for k, v in vals.items()}
        if " net " in line:
            (got_m if M and f" net {M.spec['entity']}:" in line else got_bus)[t] = d
        else:
            got[t] = d
    ref, ref_bus, ref_m = reference_states(pad, g, nodes, inserts, ticks, hold, lat)
    bad = [t for t in range(ticks) if {k: v for k, v in ref[t].items() if v} != got.get(t, {})]
    # A network at t holds the outputs at t - 1.
    bad_bus = [t for t in range(1, ticks) if ref_bus[t - 1] != got_bus.get(t, {})]
    bad_m = [t for t in range(1, ticks) if M and ref_m[t - 1] != got_m.get(t, {})]
    final = {k[1]: v for k, v in ref[-1].items()}
    path_ticks = [t for t in range(1, ticks) if ref[t] != ref[t - 1]]
    print(f"player state: {ticks} ticks vs reference, {len(bad)} differ; latency {lat}; moved on "
          f"{len(path_ticks)} ticks; final {final}")
    for t in bad[:5]:
        print(f"  t={t}: fsim {got.get(t)} ref {ref[t]}")
    W, A = sem.key(WEAPON), sem.key(ANIM)
    shots = sum(1 for t in range(1, ticks) if ref_bus[t].get(A) == 1 and ref_bus[t - 1].get(A) != 1)
    weapons_seen = sorted({ref_bus[t].get(W, 0) for t in range(ticks)})
    print(f"state bus (weapon, animation, relays): {len(bad_bus)} ticks differ; {shots} firing cycles, "
          f"weapons {weapons_seen}")
    for t in bad_bus[:5]:
        print(f"  t={t}: fsim {got_bus.get(t)} ref {ref_bus[t - 1]}")
    # The weapon, contact, combat and monster rules, independently (weapons.step, touch.step,
    # combat.step, monsters.step): each tick's bus state from the last, and REG's stats one tick
    # later (ammo spent, pickups, damage).
    rule_bad = []
    stats = {"health": HEALTH, "armor": ARMOR, **AMMO}
    G, Q = [sem.key(s) for s in tc.GONE], sem.key(QUEUE)
    DK, MK = [sem.key(s) for s in cb.DMG], [sem.key(s) for s in cb.MS]
    mon = nodes.get("mon")
    nm = len(mon.idx) if mon else 0
    mk = {name: [sem.key(s) for s in lst[:nm]] for name, lst in (("MX", mo.MX), ("MY", mo.MY))}
    mn = {name: [sem.key(s) for s in lst[:nm]] for name, lst in
          (("DX", mo.DX), ("DY", mo.DY), ("XQ", mo.XQ), ("YQ", mo.YQ), ("R", mo.RK), ("LD", mo.LD), ("ATT", mo.ATT),
           ("SEE", mo.SEE), ("DT", mo.DT))}
    BK = [[sem.key(s) for s in q] for q in mo.CF[:nm]]
    PDK, RNDK, PHK = sem.key(mo.PD), sem.key(mo.RND), sem.key(mo.PH)
    picked, hits, shot, aimed, woke, attacks = set(), 0, {}, set(), set(), 0
    ZK, restarts, dead_ticks = sem.key(RESTART), [], 0
    for t in range(1, ticks):
        prev, cur = ref_bus[t - 1], ref_bus[t]
        prevm, curm = ref_m[t - 1], ref_m[t]
        ammo = {k: prev.get(sem.key(s), 0) + hd.START[k] for k, s in AMMO.items()}
        alive = prev.get(sem.key(HEALTH), 0) + hd.START["health"] > 0
        Wn, An, Qn, spent = wp.step(prev.get(W, 0), prev.get(A, 0), prev.get(Q, 0), prev.get(sem.key(FIRE), 0),
                                    prev.get(sem.key(NEXT), 0), ammo, alive)
        tb = {k: prev.get(sem.key(s), 0) for k, s in TOUCH_SIGS.items()}
        tb.update(gone=[prev.get(k, 0) for k in G], dmg=[prev.get(k, 0) for k in DK])
        gone, inc = tc.step(things or [], SPAWN, hd.START, tb) if things else ([0] * 8, {})
        dmg, hit = cb.step(things or [], prev.get(W, 0), prev.get(A, 0), ammo, prev.get(sem.key(cb.AIM), 0),
                           prev.get(sem.key(cb.CLOSE), 0), tb["dmg"])
        mwant, mhave = {}, {}
        ms = [prev.get(k, 0) for k in MK]
        if mon:
            mb = {k: tb[k] for k in ("SX", "SY", "health", "armor")}
            mb.update({name: [prev.get(k, 0) for k in keys] for name, keys in mk.items()},
                      **{name: [prevm.get(k, 0) for k in keys] for name, keys in mn.items()},
                      CF=[[prevm.get(k, 0) for k in q] for q in BK], MS=ms, DMG=tb["dmg"], PD=prevm.get(PDK, 0),
                      RND=prevm.get(RNDK, 0), PH=prevm.get(PHK, 0))
            mout = mo.step(mon, mb, mb["SEE"])
            mout_hits = mout.pop("hits")
            for k_, v in mout.pop("inc").items():
                inc[k_] = inc.get(k_, 0) + v
            ms = mout.pop("MS")
            # The sight lines read the bus two ticks back (two ticks deep).
            src = ref_bus[t - 2] if t >= 2 else {}
            mout["SEE"] = [mo.los_count(mon, k, {"SX": src.get(sem.key(SX), 0), "SY": src.get(sem.key(SY), 0),
                                                 "MX": [src.get(q, 0) for q in mk["MX"]],
                                                 "MY": [src.get(q, 0) for q in mk["MY"]]}) for k in range(nm)]
            mwant = mout
            mhave = {name: [cur.get(k, 0) for k in keys] for name, keys in mk.items()}
            mhave.update({name: [curm.get(k, 0) for k in keys] for name, keys in mn.items()},
                         CF=[[curm.get(k, 0) for k in q] for q in BK], PD=curm.get(PDK, 0), RND=curm.get(RNDK, 0),
                         PH=curm.get(PHK, 0))
        exp = {k: inc.get(k, 0) - spent.get(k, 0) for k in stats}
        if t + 1 < ticks and cur.get(ZK):   # a restart: REG starts over
            if any(ref[t + 1].values()):
                rule_bad.append((t, "restart", ref[t + 1]))
            restarts.append(t)
        elif t + 1 < ticks:   # from outputs at t (reading the bus at t), so in REG at t + 1
            got_d = {k: ref[t + 1][sem.key(s)] - ref[t][sem.key(s)] for k, s in stats.items()}
            if got_d != exp:
                rule_bad.append((t, "stats", got_d, exp))
        # A restart (Use pressed while dead, one tick) and everything the bus holds starts over
        # (not the arithmetic: offsets, random numbers; nor the sight lines).
        want_z = int(not alive and prev.get(sem.key(USE), 0) > 0)
        if cur.get(ZK, 0) != want_z:
            rule_bad.append((t, "restart flag", cur.get(ZK, 0), want_z))
        if prev.get(ZK):
            Wn, An, Qn, gone, dmg, ms = 0, 0, 0, [0] * 8, [0] * 8, [0] * 8
            if mon:
                for name in ("MX", "MY", "ATT", "DT"):
                    mwant[name] = [0] * nm
                mwant.update(PD=0, PH=0, CF=[[0] * 8 for _ in range(nm)])
        if not alive:
            dead_ticks += 1
        gone = [g_ if i in tc.items(things or []) else 0 for i, g_ in enumerate(gone)]
        want = (Wn, An, Qn, gone, dmg, ms, mwant)
        have = (cur.get(W, 0), cur.get(A, 0), cur.get(Q, 0), [cur.get(k, 0) for k in G],
                [cur.get(k, 0) for k in DK], [cur.get(k, 0) for k in MK], mhave)
        if have != want:
            diff = {k: (mhave.get(k), mwant.get(k)) for k in mwant if mhave.get(k) != mwant.get(k)}
            rule_bad.append((t, "bus", have[:6], want[:6], diff))
        picked |= {i for i, v in enumerate(have[3]) if v}
        if mon and not prev.get(ZK):
            hits += mout_hits
            attacks += sum(mwant["ATT"])
            woke |= {mon.idx[k] for k in range(nm) if mb["MS"][mon.idx[k]] == 0 and ms[mon.idx[k]] == mo.FRESH}
        for i, (d0, d1) in enumerate(zip(tb["dmg"], dmg)):
            if d1 != d0 and not prev.get(ZK):
                shot.setdefault(i, []).append(t)
        if prev.get(sem.key(cb.AIM)):
            aimed.add(prev[sem.key(cb.AIM)] - 1)
    # Turning: slow until the counter reaches the ramp, then fast.
    slow, fast, ramp = turn
    H_ref, _, _ = input_pad.reference(pad, inserts, ticks, hold)
    tcs = turn_counts(H_ref, ticks)
    turn_bad = []
    for t in range(3, ticks):
        if ref_bus[t - 1].get(ZK):
            continue   # a restart
        rate = slow if tcs[t - 2] < ramp else fast
        if ref[t - 3][sem.key(HEALTH)] <= -hd.START["health"]:
            rate = 0   # dead: no turning
        d = rate * ((H_ref[t - 2].get(input_pad.RIGHT, 0) > 0) - (H_ref[t - 2].get(input_pad.LEFT, 0) > 0))
        if (ref[t][sem.key(SR)] - ref[t - 1][sem.key(SR)] - d) % TURN or not \
                angle_range(turn)[0] <= ref[t][sem.key(SR)] < angle_range(turn)[1]:
            turn_bad.append(t)
    rule_bad += turn_bad
    final_v = {k: ref[-1][sem.key(s)] + hd.START[k] for k, s in stats.items()}
    print(f"weapon, contact and monster rules (weapons.step, touch.step, monsters.step, turn ramp): {len(rule_bad)} "
          f"ticks differ {rule_bad[:3]}; items picked up {sorted(picked)}, final {final_v}")
    last = ref_bus[-1]
    print(f"combat (combat.step): monsters aimed at {sorted(aimed)}, hit at {shot}; final damage "
          f"{[last.get(k, 0) for k in DK]}, clocks {[last.get(k, 0) for k in MK]}")
    if mon:
        print(f"monsters: woke {sorted(woke)}, {attacks} attacks, {hits} hit the player; final positions "
              f"{[(last.get(a, 0), last.get(b_, 0)) for a, b_ in zip(mk['MX'], mk['MY'])]}")
    # Dead: nothing moves (position, turn) until the restart.
    # (From lat + 4 ticks after dying: the gates' corners and the turn deciders' flag run behind.)
    frozen = [t for t in range(lat + 4, ticks)
              if all(ref[t - 1 - j][sem.key(HEALTH)] <= -hd.START["health"] for j in range(lat + 4))
              and t - 1 not in restarts
              and any(ref[t][sem.key(s)] != ref[t - 1][sem.key(s)] for s in (SX, SY, SR))]
    rule_bad += frozen
    print(f"death: dead on {dead_ticks} ticks, moved while dead on {len(frozen)} {frozen[:3]}; restarts at {restarts}")
    # The aim against the float hitscan (combat.ideal_aim) from the bus tau ticks earlier; near-ties
    # (within NEAR_TIE tiles of a boundary) may round either way.
    aim_bad, aim_near = [], 0
    if nodes.get("aim"):
        tau = nodes["aim"][0].time
        for t in range(tau, ticks):
            src = ref_bus[t - tau]
            x = ((src.get(sem.key(SX), 0) + SPAWN[0]) | 1) / FP
            y = ((src.get(sem.key(SY), 0) + SPAWN[1]) | 1) / FP
            ang = 2 * math.pi * (((src.get(sem.key(SR), 0) >> ANGLE_SUB) + A0) % 256) / 256
            xy = {i: ((int(things[i][0] * FP) + src.get(sem.key(mo.MX[k]), 0)) / FP,
                      (int(things[i][1] * FP) + src.get(sem.key(mo.MY[k]), 0)) / FP)
                  for k, i in enumerate(tc.monsters(things or []))}
            fa, fc, margin = cb.ideal_aim(things, x, y, ang, [src.get(k, 0) for k in DK], raycast.is_wall, MAP,
                                            w, layout["view"].plane, xy)
            if (ref_bus[t].get(sem.key(cb.AIM), 0), ref_bus[t].get(sem.key(cb.CLOSE), 0)) != (fa, fc):
                if margin < NEAR_TIE:
                    aim_near += 1
                else:
                    aim_bad.append(t)
        print(f"aim vs float hitscan: {len(aim_bad)} ticks differ {aim_bad[:5]}, {aim_near} near-ties")
    rule_bad += aim_bad
    if M:
        print(f"monster network M: {len(bad_m)} ticks differ")
        for t in bad_m[:3]:
            print(f"  t={t}: fsim {got_m.get(t)} ref {ref_m[t - 1]}")
    bad_bus += rule_bad + bad_m
    # Collision: the player's box (half-size MARGIN around the position) never touches a wall or a
    # live monster's box (half-size probe, where the monster is now).
    mons = monster_boxes(things)

    def touches(t):
        x, y = ref[t][sem.key(SX)] + SPAWN[0], ref[t][sem.key(SY)] + SPAWN[1]
        if any(raycast.is_wall(MAP[(y + dy) >> 8][(x + dx) >> 8]) for dx in (-MARGIN, MARGIN) for dy in (-MARGIN, MARGIN)):
            return True
        live = lambda alive: all(ref_bus[t].get(sem.key(s), 0) < v for s, _, v in alive)  # noqa: E731
        moved = [(mx + ref_bus[t].get(sem.key(mo.MX[k]), 0), my + ref_bus[t].get(sem.key(mo.MY[k]), 0), alive)
                 for k, (mx, my, alive) in enumerate(mons)]
        return any(abs(x - mx) < MARGIN + probe and abs(y - my) < MARGIN + probe for mx, my, alive in moved if live(alive))
    # Monsters never step into a wall (their box, Doom's radius 20 units).
    def in_wall(t):
        for k, (mx, my, _) in enumerate(mons):
            x, y = mx + ref_bus[t].get(sem.key(mo.MX[k]), 0), my + ref_bus[t].get(sem.key(mo.MY[k]), 0)
            if any(raycast.is_wall(MAP[(y + dy) >> 8][(x + dx) >> 8]) for dx in (-40, 40) for dy in (-40, 40)):
                return True
        return False
    stuck = [t for t in range(ticks) if in_wall(t)]
    print(f"monster boxes in a wall on {len(stuck)} ticks {stuck[:3]}")
    bad_bus += stuck
    inside = [t for t in range(ticks) if touches(t)]
    print(f"player box ({MARGIN}/{FP} tile) touching a wall or monster on {len(inside)} ticks")
    # Frames: rows evaluated from the reference state, at the raycaster's latency.
    frames = {int(p.stem): raycast.read_ppm(p) for p in sorted(out_dir.glob("*.ppm"))}
    holes = {(x, y) for x in range(w) for y in range(h) if b.grid.hole(x, y)}
    port = None
    for n in gr.nodes:
        if n.kind == "ext":
            port = n
    lag = rows[0].time + 1

    def ref_img(t):
        return raycast.evaluate(gr, cols, rows, 0, layout, {port: ref_bus[t] if t >= 0 else {}})

    sample = list(range(lag + 2, ticks, max(1, ticks // 40)))
    fbad = []
    for t in sample:
        r = ref_img(t - lag)
        nd = sum(1 for y in range(h) for x in range(w) if (x, y) not in holes and frames[t][y][x] != r[y][x])
        if nd:
            fbad.append((t, nd))
    print(f"frames: {len(sample)} sampled, raycaster latency {lag}; {len(fbad)} differ {fbad[:5]}")
    return not bad and not bad_bus and not inside and not fbad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="160x100")
    ap.add_argument("--button-size", type=int)
    ap.add_argument("--hold", type=int, default=input_pad.DEFAULT_HOLD)
    ap.add_argument("--ticks", type=int, default=2340)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--keys", action="store_true",
                    help="script the input as the keyboard mod (mod/doom-input) instead of pad drops")
    ap.add_argument("--tex", type=int, default=raycast.DEFAULT_TEX, help="texture resolution (texels per tile)")
    ap.add_argument("--flat", type=int, default=raycast.DEFAULT_FLAT,
                    help="floor/ceiling texture resolution (texels per tile: 8, 16, 32; 0 = untextured)")
    ap.add_argument("--no-things", action="store_true", help="leave out the monsters and items")
    ap.add_argument("--no-hud", action="store_true", help="leave out the status bar (the view takes the whole screen)")
    ap.add_argument("--no-weapon", action="store_true", help="leave out the weapon in front of the view")
    ap.add_argument("--turn", type=int, default=DEFAULT_TURN[1],
                    help=f"turn speed once held a while: 1/{1 << ANGLE_SUB} view angles (1/{256 << ANGLE_SUB} turn) per tick")
    ap.add_argument("--turn-slow", type=int, default=DEFAULT_TURN[0], help="turn speed while a button is first held")
    ap.add_argument("--turn-ramp", type=int, default=DEFAULT_TURN[2], help="ticks held before turning speeds up")
    quality_arg(ap)
    raycast.shading_args(ap)
    raycast.view_args(ap)
    a = ap.parse_args()
    w, h = map(int, a.size.lower().split("x"))
    things = None if a.no_things else raycast.th.THINGS
    turn = (a.turn_slow, a.turn, a.turn_ramp)
    b, pad, g, nodes, gr, cols, rows, scal, layout, lat, probe = build(
        w, h, a.button_size, a.hold, a.substation_quality, a.tex, a.flat, things,
        turn, raycast.Shading.from_args(a), not a.no_hud, not a.no_weapon, view=raycast.View.from_args(a))
    inserts = schedule(pad, a.ticks, keys=a.keys)
    data = b.to_json()
    data["fsim"] = {"ticks": a.ticks, "doc": f"M4 {w}x{h}", "inserts": inserts}
    gen = HERE / "gen"
    gen.mkdir(exist_ok=True)
    path = gen / f"game_{w}x{h}.json"
    path.write_text(json.dumps(data, separators=(",", ":")))
    (gen / f"game_{w}x{h}.txt").write_text(encode(b.to_json()) + "\n")
    counts = {}
    for e in b.entities:
        counts[e["name"]] = counts.get(e["name"], 0) + 1
    n_comb = sum(v for k, v in counts.items() if k in ("arithmetic-combinator", "decider-combinator", "selector-combinator"))
    print(f"{path.relative_to(ROOT)}: {len(b.entities)} entities, {n_comb} combinators, "
          f"player logic {len(g.nodes)} nodes, latency {lat}, probe {probe}")
    if a.verify and not verify(path, b, pad, g, nodes, gr, cols, rows, layout, w, h, inserts, a.ticks, a.hold, lat,
                               things, turn, probe):
        sys.exit(1)


if __name__ == "__main__":
    main()
