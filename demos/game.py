"""M2: the raycaster driven by the input pad. Walk with the D-pad, turn, slide along walls.

    screen (W x H lamps) | pad (demos/input_pad.py) | player logic below the pad

Player state is one register, REG: an arithmetic combinator `Each + 0` that adds its own output
(green loop) to the increments on its red input. It holds X and Y (displacement from SPAWN, 1/FP
tiles) and R (turn, 256 per revolution, unbounded; the view angle is (R + A0) & 255).

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

The raycaster (demos/raycast.py build_graph) takes its camera from REG through a relay line over
the top of the screen to a pole left of it (its ext input).

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
from compiler.netlist import EACH, GREEN, RED, Graph, Node  # noqa: E402
import input_pad  # noqa: E402
import raycast  # noqa: E402
from raycast import FP, MAP, trig  # noqa: E402

RED_IN, GREEN_IN, RED_OUT, GREEN_OUT = 1, 2, 3, 4
SX, SY, SR = sig("signal-X"), sig("signal-Y"), sig("signal-R")
SPAWN = (int(8.5 * FP), int(12.5 * FP))
A0 = 192           # spawn facing north (angle 0 is east, angles grow clockwise on screen)
SPEED = 10         # 1/FP tiles per tick while walking: 2.3 tiles a second
TURN = 2           # angle units per tick: a turn in 2.1 seconds
MARGIN = 48        # player box half-size (1/FP tiles), before the latency allowance


def walls():
    return [(x, y) for y, row in enumerate(MAP) for x, c in enumerate(row) if c == "#"]


def velocity(a, fw=1):
    """Per-tick step (vx, vy) for lookup key a = R & 255, walking forward (fw 1) or back (-1)."""
    t = 2 * math.pi * ((a + A0) % 256) / 256
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


def player_graph(reg, reg_pos, held, held_pos, gate_pos, turn_pos, box):
    """The player logic. Returns (graph, nodes) with nodes S, H (ext), gates and turn (fixed
    sinks feeding REG), and lat: how many ticks the gates' REG-derived inputs run behind REG.

    Nothing on the input path waits for collision. From REG, every tick and whatever is held:
      V+, V-   the step forward and back for the current angle (signed lookups on R & 255);
      corners  for each corner of the player's box (half-size `box`): is it in a wall? Range
               tests on X and Y straight from REG (one decider per corner).
    The gates then pass a step when its button is held and the box's two corners on that side are
    free: per axis and direction, (W held, V+x > 0, both +x corners free) or (W held, V+x < 0,
    both -x corners free) -> copy V+x into REG. Each axis is gated alone, so you slide along walls.
    A press reaches REG in two ticks, like a turn."""
    g = Graph(reserved=[SX, SY, SR] + [sig(s) for s in input_pad.BUTTON_SIGNALS])
    S = g.ext(reg, reg_pos, label="REG")
    H = g.ext(held, held_pos, size=(1, 2), label="HELD")
    # Turning: straight from HELD into REG.
    turn = [g.decider([sem.cond(sig(input_pad.RIGHT), ">", 0, RED)], [sem.out(SR, constant=TURN)], red=H, label="turn+"),
            g.decider([sem.cond(sig(input_pad.LEFT), ">", 0, RED)], [sem.out(SR, constant=-TURN)], red=H, label="turn-")]
    for i, n in enumerate(turn):
        n.fixed, n.pos = True, (turn_pos[0], turn_pos[1] + i)
    X, Y, R = g.scalar(S, SX), g.scalar(S, SY), g.scalar(S, SR)
    A = g.ss(R, "AND", 255, "angle")
    V = {fw: [g.lookup_scalar(A, {a: velocity(a, fw)[i] for a in range(256)}, 32, f"v{fw:+d} {'xy'[i]}", out=s)
              for i, s in enumerate((SX, SY))] for fw in (1, -1)}
    # Corner (sx, sy) of the box is in a wall: X + SPAWN + sx*box in [256 x0, 256 (x1 + 1)) and the
    # same for Y, for some wall run.
    corner = {}
    for sx in (1, -1):
        for sy in (1, -1):
            ox, oy = SPAWN[0] + sx * box, SPAWN[1] + sy * box
            conds = []
            for x0, x1, y in wall_runs():
                conds += [sem.cond(SX, ">=", 256 * x0 - ox, RED, "or" if conds else None),
                          sem.cond(SX, "<", 256 * (x1 + 1) - ox, RED, "and"),
                          sem.cond(SY, ">=", 256 * y - oy, RED, "and"),
                          sem.cond(SY, "<", 256 * (y + 1) - oy, RED, "and")]
            s = g.new_scalar_signal()
            corner[sx, sy] = (s, g.decider(conds, [sem.out(s, constant=1)], red=S, label=f"corner {sx:+d}{sy:+d}"))
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
    return g, {"S": S, "H": H, "gates": gates, "turn": turn, "lat": lat}


def camera_from(port, port_pos):
    def camera(g):
        S = g.ext(port, port_pos, size=(1, 1), connectors=(1, 2), label="state port")
        A = g.ss(g.scalar(S, SR), "AND", 255, "view angle")   # A0 is folded into the tables
        PX = g.ss(g.ss(g.scalar(S, SX), "+", SPAWN[0]), "OR", 1, "px")
        PY = g.ss(g.ss(g.scalar(S, SY), "+", SPAWN[1]), "OR", 1, "py")
        return A, PX, PY, {"port": S, "angle_offset": A0}
    return camera


def big_pole_line(b, points):
    """Big poles (2x2, circuit reach 32) at the free spots nearest each point; returns them."""
    taken = b.occupied()
    poles = []
    for px, py in points:
        for dx, dy in sorted(((dx, dy) for dx in range(-4, 5) for dy in range(-4, 5)), key=lambda d: abs(d[0]) + abs(d[1])):
            x, y = px + dx, py + dy
            if not {(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)} & taken:
                poles.append(b._add("big-electric-pole", x + 1, y + 1))
                taken |= {(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)}
                break
        else:
            raise ValueError(f"no room for a big pole near {px},{py}")
    return poles


def build(w, h, n=None, hold=input_pad.DEFAULT_HOLD, substation_quality=DEFAULT_SUBSTATION_QUALITY):
    n = n or input_pad.button_size(h)
    b = Blueprint(f"Factorio Doom M2 {w}x{h}", substation_quality)
    lattice = b.grid.hole

    # Pad right of the screen, player logic below it.
    px0 = w + 8
    pad_h = 4 * n + 10
    py0 = max(0, (h - pad_h) // 2 - 10)
    pad = input_pad.build_pad(b, px0, py0, n, hold)
    pad_bottom = max(int(e["position"]["y"]) + 1 for e in b.entities)
    reg_pos = (w + 4, pad_bottom + 4)
    reg = b.arithmetic(*reg_pos, EACH, "+", 0, EACH, direction=4)
    b.wire(reg, GREEN_OUT, reg, GREEN_IN)
    d1 = b.entities[pad["d1"] - 1]["position"]
    held_pos = (int(d1["x"]), int(d1["y"] - 1))
    gate_pos = (reg_pos[0], reg_pos[1] + 1)
    turn_pos = (reg_pos[0] + 2, reg_pos[1])

    def make(box):
        return player_graph(reg, reg_pos, pad["d1"], held_pos, gate_pos, turn_pos, box)

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

    # Raycaster, camera from a pole left of the screen that the line from REG feeds.
    port_pos = (-4, -6)
    port = b.pole(*port_pos)
    gr, cols, rows, scal = raycast.build_graph(w, h, camera_from(port, port_pos), Graph(reserved=[SX, SY, SR]))
    ent_r = gr.compile(b, blocked=lattice, region=lambda x, y: x <= -3)
    for y, node in enumerate(rows):
        lamps = [b.lamp(x, y, {"use_colors": True, "color_mode": 2, "rgb_signal": cols[x]}, always_on=True)
                 for x in range(w) if not lattice(x, y)]
        b.wire(ent_r[node.id][1], RED_OUT, lamps[0], RED_IN)
        b.chain(lamps, RED_IN)

    # REG -> up the gap right of the screen -> along the top -> the port.
    top = -10
    xs = list(range(w + 2, -3, -24))   # 24 apart, so nudged poles stay within reach (32)
    ys = list(range(reg_pos[1] - 2, top, -24))
    line = big_pole_line(b, [(w + 2, y) for y in ys] + [(x, top) for x in xs] + [(-4, top)])
    b.wire(reg, RED_OUT, line[0], 1)
    b.chain(line, 1)
    b.wire(line[-1], 1, port, 1)

    raycast._substations(b)
    b.power()
    b.link_power()
    b.check_reach()
    return b, pad, g, nodes, gr, cols, rows, scal, lat, probe


# ---------------------------------------------------------------------------------------------
# Verification

def schedule(pad, ticks, seed=2):
    """Drops every 8-12 ticks while a button is held."""
    rnd = random.Random(seed)
    cells = pad["cells"]
    script = [("ring 0,-1", 5, 150),           # walk north into the wall ahead: collision
              ("ring 1,0", 165, 230),          # turn right
              ("ring 1,-1", 245, 420),         # forward + right: an arc, sliding on walls
              ("ring 0,1", 430, 520),          # back up
              ("ring -1,-1", 530, ticks - 30)]
    inserts = []
    for cell, start, end in script:
        t = start
        while t <= min(end, ticks - 20):
            inserts.append([t, rnd.choice(cells[cell]["chests"]), "iron-plate", 1])
            t += rnd.randint(8, 12)
    return sorted(inserts)


def reference_states(pad, g, nodes, inserts, ticks, hold, lat):
    H, _, _ = input_pad.reference(pad, inserts, ticks, hold)
    hvec = lambda t: {sem.key(sig(s)): v for s, v in H[t].items() if v} if t >= 0 else {}  # noqa: E731
    keys = [sem.key(s) for s in (SX, SY, SR)]
    state = [{k: 0 for k in keys} for _ in range(ticks)]
    inc_cache = {}
    for t in range(ticks):
        prev = state[t - 1] if t else {k: 0 for k in keys}
        # A gate's output in REG(t) read HELD(t - 2) and V/corners computed from REG(t - 2 - lat).
        src = t - 2 - lat
        if src >= 0:
            s_in = {k: v for k, v in state[src].items() if v}
            ck = (tuple(sorted(s_in.items())), tuple(sorted(hvec(t - 2).items())))
            if ck not in inc_cache:
                inc_cache[ck] = {}
                for o in g.evaluate(0, nodes["gates"], {nodes["S"]: s_in, nodes["H"]: hvec(t - 2)}):
                    sem.add_into(inc_cache[ck], o)
            inc = inc_cache[ck]
        else:
            inc = {}
        turn = {}
        for o in g.evaluate(0, nodes["turn"], {nodes["H"]: hvec(t - 2)}):
            sem.add_into(turn, o)
        state[t] = {k: sem.i32(prev[k] + inc.get(k, 0) + turn.get(k, 0)) for k in keys}
    return state


def verify(path, b, pad, g, nodes, gr, cols, rows, w, h, inserts, ticks, hold, lat):
    ins_path = path.with_suffix(".inserts.json")
    ins_path.write_text(json.dumps(inserts))
    reg = nodes["S"].spec["entity"]
    out_dir = ROOT / "out" / f"game_{w}x{h}_ppm"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.ppm"):
        f.unlink()
    res = subprocess.run([str(ROOT / "target" / "release" / "fsim"), "run", str(path), "--ticks", str(ticks),
                          "--input", str(ins_path), "--probe", str(reg), "--ppm", str(out_dir)],
                         check=True, capture_output=True, text=True)
    got = {}
    for line in res.stdout.splitlines():
        t = int(line.split()[0][2:])
        vals = dict(kv.split("=") for kv in line.split(": ", 1)[1].split()) if ": " in line else {}
        got[t] = {sem.key(sig(f"signal-{k[-1]}" if not k.startswith("signal-") else k)): int(v) for k, v in vals.items()}
    ref = reference_states(pad, g, nodes, inserts, ticks, hold, lat)
    bad = [t for t in range(ticks) if {k: v for k, v in ref[t].items() if v} != got.get(t, {})]
    final = {k[1]: v for k, v in ref[-1].items()}
    path_ticks = [t for t in range(1, ticks) if ref[t] != ref[t - 1]]
    print(f"player state: {ticks} ticks vs reference, {len(bad)} differ; latency {lat}; moved on "
          f"{len(path_ticks)} ticks; final {final}")
    for t in bad[:5]:
        print(f"  t={t}: fsim {got.get(t)} ref {ref[t]}")
    # Collision: the player's box (half-size MARGIN around the position) never touches a wall.
    def touches(t):
        x, y = ref[t][sem.key(SX)] + SPAWN[0], ref[t][sem.key(SY)] + SPAWN[1]
        return any(MAP[(y + dy) >> 8][(x + dx) >> 8] == "#" for dx in (-MARGIN, MARGIN) for dy in (-MARGIN, MARGIN))
    inside = [t for t in range(ticks) if touches(t)]
    print(f"player box ({MARGIN}/{FP} tile) touching a wall on {len(inside)} ticks")
    # Frames: rows evaluated from the reference state, at the raycaster's latency.
    frames = {int(p.stem): raycast.read_ppm(p) for p in sorted(out_dir.glob("*.ppm"))}
    holes = {(x, y) for x in range(w) for y in range(h) if b.grid.hole(x, y)}
    port = None
    for n in gr.nodes:
        if n.kind == "ext":
            port = n
    lag = rows[0].time + 1

    def ref_img(t):
        st = {k: v for k, v in ref[t].items() if v} if t >= 0 else {}
        outs = gr.evaluate(0, rows, {port: st})
        return [[o.get(sem.key(c), 0) for c in cols] for o in outs]

    sample = list(range(lag + 2, ticks, max(1, ticks // 40)))
    fbad = []
    for t in sample:
        r = ref_img(t - lag)
        nd = sum(1 for y in range(h) for x in range(w) if (x, y) not in holes and frames[t][y][x] != r[y][x])
        if nd:
            fbad.append((t, nd))
    print(f"frames: {len(sample)} sampled, raycaster latency {lag}; {len(fbad)} differ {fbad[:5]}")
    return not bad and not inside and not fbad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="160x100")
    ap.add_argument("--button-size", type=int)
    ap.add_argument("--hold", type=int, default=input_pad.DEFAULT_HOLD)
    ap.add_argument("--ticks", type=int, default=700)
    ap.add_argument("--verify", action="store_true")
    quality_arg(ap)
    a = ap.parse_args()
    w, h = map(int, a.size.lower().split("x"))
    b, pad, g, nodes, gr, cols, rows, scal, lat, probe = build(w, h, a.button_size, a.hold, a.substation_quality)
    inserts = schedule(pad, a.ticks)
    data = b.to_json()
    data["fsim"] = {"ticks": a.ticks, "doc": f"M2 {w}x{h}", "inserts": inserts}
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
    if a.verify and not verify(path, b, pad, g, nodes, gr, cols, rows, w, h, inserts, a.ticks, a.hold, lat):
        sys.exit(1)


if __name__ == "__main__":
    main()
