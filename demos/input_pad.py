"""Input pad: buttons the player presses in god mode by dropping items (Z) onto chests.

Vanilla has no keyboard -> circuit path, and in god mode the keyboard pans the camera, so a
button is an n x n block of chests: hover it and press or hold Z. Every chest of a button sits on
one red network, and dropping an item anywhere on it counts as a press. Holding Z drops about 6
items a second, so a button counts as held while drops keep coming.

Layout (n = button size, 9 at 320x200; see button_size()):

    ring: a 3n x 3n D-pad. The edges are forward/back/turn left/turn right, and each corner is a
          combo (forward+left, ...): one cursor can't hold two buttons, so the combos are buttons
          of their own. Fire is the middle, a crosshair. Steel chests with arrows in wooden chests.
          Indicator lamps in the fire button (n >= 5, and substations that reach): its corners
          for fire, the middle of each edge for that direction (a combo lights two).
    row:  extra buttons below the ring (use, next weapon, enter), letters in wooden chests,
          a display panel and an indicator lamp above each.

Decoder, all buttons at once as lanes (one virtual signal per button, BUTTON_SIGNALS):

    SUM_j  per button and cell: Each + 0 -> j       X(t)  total items in the button's chests
    NEG    Each * -1, reading X one tick late       B(t)  = X(t-1) - X(t-2), drops this tick
    D1     B > 0          -> Each = HOLD            H(t)  = HOLD on a drop,
    D2     B <= 0 and H' > 0 -> copy H'                     else max(H(t-1) - 1, 0)
           (H' = H(t-1) - 1: D1/D2 fed back on green, plus a constant -1 on every button lane)
    D3     B > 0 and H' < 0 -> Each = 1             P(t)  = 1 on the first drop after a release

The pad's outputs are two red networks: HELD (H > 0 while held; H counts down from HOLD after
the last drop) and PRESSED (one-tick pulses, for buttons that act once per press). Removing items
from a chest never counts as a press.

Keyboard (optional, mod/doom-input): two empty constant combinators described KEYS_HELD and
KEYS_PRESSED sit on HELD and PRESSED. The mod writes 1 on a button's lane while its key is held
(or for one tick on a press); without the mod they stay empty. Readers only test > 0, so the two
sources simply add. In schedules, an entry on one of them sets that signal ([t, entity, signal,
value], fsim's and the harness's constant-combinator form of an insert).

    python3 demos/input_pad.py --size 320x200 --verify   # fsim vs the reference model
    python3 harness/run.py demos/gen/input_pad_n9.json   # same schedule in the game, then
    fsim conform <dir with that json> --dump harness/out/dump.json
"""
import argparse
import json
import pathlib
import random
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "conformance"))
from bp import DEFAULT_SUBSTATION_QUALITY, Blueprint, encode, quality_arg, sig  # noqa: E402
from compiler import netlist  # noqa: E402
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import EACH, GREEN, RED  # noqa: E402

RED_IN, GREEN_IN, RED_OUT, GREEN_OUT = 1, 2, 3, 4
DEFAULT_HOLD = 14  # ticks a button stays held after a drop (Z repeats about every 10)
GAP_CAP = 60       # the gap meter ignores gaps this long (separate presses)

FORWARD, BACK, LEFT, RIGHT, FIRE = "signal-W", "signal-S", "signal-A", "signal-D", "signal-F"
# Ring cells by direction (dx, dy): the button signals the cell presses.
RING = {
    (0, -1): [FORWARD], (0, 1): [BACK], (-1, 0): [LEFT], (1, 0): [RIGHT],
    (-1, -1): [FORWARD, LEFT], (1, -1): [FORWARD, RIGHT],
    (-1, 1): [BACK, LEFT], (1, 1): [BACK, RIGHT],
}
# Extra buttons: (name, signal, letter).
EXTRAS = [("use", "signal-U", "U"), ("weapon", "signal-N", "N"), ("enter", "signal-E", "E")]
BUTTON_SIGNALS = [FORWARD, BACK, LEFT, RIGHT, FIRE] + [s for _, s, _ in EXTRAS]
KEYS_HELD, KEYS_PRESSED = "doom-input:held", "doom-input:pressed"   # the mod finds them by these


MAX_BUTTON = 9  # the fire button's chests reach a sum outside the ring across one cell (reach 9)


def button_size(h):
    """Chest blocks big enough to hit from a zoom that shows the whole screen: 9 at 200 rows."""
    return min(MAX_BUTTON, max(3, (9 * h + 100) // 200))


# ---------------------------------------------------------------------------------------------
# Glyphs (tiles inside an n x n button that get wooden chests)

def arrow(n, dx, dy):
    """An arrow pointing (dx, dy): shaft plus chevron head, or a diagonal one for corners."""
    t = n // 6
    tiles = set()
    if dx and dy:  # north-east, then mirrored
        for x in range(n):
            for y in range(n):
                shaft = 0 <= x + y - (n - 1) <= t
                head = (y <= t and x >= n // 2) or (x >= n - 1 - t and y <= n // 2)
                if shaft or head:
                    tiles.add((x, y))
        return {(x if dx > 0 else n - 1 - x, y if dy < 0 else n - 1 - y) for x, y in tiles}
    c = (n - 1) / 2
    for y in range(n):
        d = abs(y - c)
        if d <= t + 0.5:
            tiles |= {(x, y) for x in range(n)}
        tiles |= {(x, y) for x in (int(n - 1 - d), int(n - d)) if 0 <= x < n}
    if dx > 0:
        return tiles
    if dx < 0:
        return {(n - 1 - x, y) for x, y in tiles}
    return {(y, n - 1 - x) for x, y in tiles} if dy < 0 else {(y, x) for x, y in tiles}


def crosshair(n):
    """A ring with a cross through it, gaps where the indicator lamps go."""
    c, r = n // 2, (n - 1) / 2 - 1
    tiles = set()
    for x in range(n):
        for y in range(n):
            if x == c or y == c or abs(((x - c) ** 2 + (y - c) ** 2) ** 0.5 - r) < 0.5:
                tiles.add((x, y))
    return tiles


FONT = {
    5: {"F": "##### #.... #.... ####. #.... #.... #....", "U": "#...# #...# #...# #...# #...# #...# .###.",
        "N": "#...# ##..# #.#.# #..## #...# #...# #...#", "E": "##### #.... #.... ####. #.... #.... #####"},
    3: {"F": "### #.. ##. #.. #..", "U": "#.# #.# #.# #.# ###", "N": "##. #.# #.# #.# #.#",
        "E": "### #.. ##. #.. ###"},
}


def letter(n, ch):
    """The letter centered in the button (5x7 from n = 9, 3x5 from n = 5, none below)."""
    fw = 5 if n >= 9 else 3 if n >= 5 else 0
    if not fw:
        return set()
    rows = FONT[fw][ch].split()
    ox, oy = (n - fw) // 2, (n - len(rows)) // 2
    return {(ox + x, oy + y) for y, r in enumerate(rows) for x, c in enumerate(r) if c == "#"}


# ---------------------------------------------------------------------------------------------
# Wiring with relays

def footprint(e):
    w, h = Blueprint.SIZES.get(e["name"], (1, 1))
    if e.get("direction", 0) in (4, 12):
        w, h = h, w
    x0, y0 = e["position"]["x"] - w / 2, e["position"]["y"] - h / 2
    return (x0, y0, x0 + w, y0 + h)


def wire_net(b, members, taken, usable):
    """Connect (entity, connector) members as a minimum spanning tree over footprint gaps,
    relaying long edges through medium poles (netlist._relay)."""
    color = 1 if members[0][1] in (RED_IN, RED_OUT) else 2
    fps = [footprint(b.entities[e - 1]) for e, _ in members]
    best = {i: (netlist.gap(fps[0], fps[i]), 0) for i in range(1, len(members))}
    while best:
        i = min(best, key=lambda i: best[i][0])
        dist, j = best.pop(i)
        if dist > netlist.REACH:
            netlist._relay(b, fps[j], members[j], fps[i], members[i], color, usable, taken)
        else:
            b.wire(*members[j], *members[i])
        for k in best:
            d2 = netlist.gap(fps[i], fps[k])
            if d2 < best[k][0]:
                best[k] = (d2, i)


# ---------------------------------------------------------------------------------------------
# The pad

def build_pad(b, x0, y0, n, hold=DEFAULT_HOLD, meters=False):
    """Adds the pad with its top-left ring tile at (x0, y0). Returns a dict with the cells
    ({name: [chest entity numbers]}, and the signals each cell presses), the decoder entities
    and `held` / `pressed`: (entity, connector) ends of the two output networks."""
    nets = {}  # name -> list of (entity, connector)
    cells = {}

    def net(name, *members):
        nets.setdefault(name, []).extend(members)

    def block(cx, cy, wood, name, signals, skip=()):
        chests = []
        for y in range(n):
            xs = range(n) if y % 2 == 0 else range(n - 1, -1, -1)
            for x in xs:
                if (x, y) not in skip:
                    chests.append(b.chest(cx + x, cy + y, "wooden-chest" if (x, y) in wood else "steel-chest"))
        # Serpentine chain; around a skipped tile the next chest is still within reach.
        b.chain(chests, 1)
        cells[name] = {"chests": chests, "signals": signals}
        return chests

    def sum_comb(tx, ty, signal, chests):
        e = b.arithmetic(tx, ty, EACH, "+", 0, sig(signal), direction=4)
        # Wire from the nearest chest of the cell.
        near = min(chests, key=lambda c: netlist.gap(footprint(b.entities[c - 1]), footprint(b.entities[e - 1])))
        net(f"cell {near}", (near, 1), (e, RED_IN))
        net("B", (e, RED_OUT))
        net("Bg", (e, GREEN_OUT))
        return e

    def lamp(x, y, signal):
        return b.lamp(x, y, {"circuit_enabled": True, "circuit_condition": sem.cond(sig(signal), ">", 0)},
                      always_on=True)

    sums, lamps = [], []
    for (dx, dy), signals in RING.items():
        cx, cy = x0 + n * (dx + 1), y0 + n * (dy + 1)
        chests = block(cx, cy, arrow(n, dx, dy), f"ring {dx},{dy}", signals)
        mid = cx + (n - 2) // 2
        for i, s in enumerate(signals):
            if dy < 0:
                pos = (mid, y0 - 1 - i)
            elif dy > 0:
                pos = (mid, y0 + 3 * n + i)
            else:
                pos = (x0 - 2 if dx < 0 else x0 + 3 * n, cy + n // 2)
            sums.append(sum_comb(*pos, s, chests))
    # Fire in the middle, a crosshair. Its indicator lamps: one per direction at the middle of
    # each edge (a combo lights two), fire at the four corners. Substations can only sit outside
    # the ring, so this needs n <= d - 2 (d: substation reach; n = 9 needs better than normal).
    # None below n = 5, where they'd take too much of the button.
    c, last = n // 2, n - 1
    spots = {}
    if 5 <= n <= b.grid.d - 2:
        spots = {(c, 0): FORWARD, (c, last): BACK, (0, c): LEFT, (last, c): RIGHT,
                 (0, 0): FIRE, (last, 0): FIRE, (0, last): FIRE, (last, last): FIRE}
    for (x, y), s in spots.items():
        lamps.append(lamp(x0 + n + x, y0 + n + y, s))
    chests = block(x0 + n, y0 + n, crosshair(n), "fire", [FIRE], skip=spots)
    # Its chests are surrounded by the ring: the sum sits left of the ring, a ring cell away.
    sums.append(sum_comb(x0 - 2, y0 + n, FIRE, chests))
    ye = y0 + 3 * n + 3
    for k, (name, s, ch) in enumerate(EXTRAS):
        cx = x0 + k * (n + 1)
        chests = block(cx, ye, letter(n, ch), name, [s])
        b.display(cx + n // 2, ye - 1, s)
        lamps.append(lamp(cx + n // 2 + 1, ye - 1, s))
        sums.append(sum_comb(cx + (n - 2) // 2, ye + n, s, chests))

    # Decoder core, below the extra buttons' sums.
    yc = ye + n + 2
    neg = b.arithmetic(x0, yc, EACH, "*", -1, EACH, GREEN)
    d1 = b.decider(x0 + 1, yc, [sem.cond(EACH, ">", 0, RED)], [sem.out(EACH, constant=hold)])
    d2 = b.decider(x0 + 2, yc, [sem.cond(EACH, "<=", 0, RED), sem.cond(EACH, ">", 0, GREEN, compare_type="and")],
                   [sem.out(EACH, networks=GREEN)])
    d3 = b.decider(x0 + 3, yc, [sem.cond(EACH, ">", 0, RED), sem.cond(EACH, "<", 0, GREEN, compare_type="and")],
                   [sem.out(EACH, constant=1)])
    minus = b.constant(x0 + 2, yc + 2, [(sig(s), -1) for s in BUTTON_SIGNALS])
    net("Bg", (neg, GREEN_IN))
    net("B", (neg, RED_OUT), (d1, RED_IN), (d2, RED_IN), (d3, RED_IN))
    net("H'", (d1, GREEN_OUT), (d2, GREEN_OUT), (d2, GREEN_IN), (d3, GREEN_IN), (minus, 2))
    keys_held = b.constant(x0 + 4, yc, {}, description=KEYS_HELD)
    keys_pressed = b.constant(x0 + 4, yc + 1, {}, description=KEYS_PRESSED)
    net("H", (d1, RED_OUT), (d2, RED_OUT), (keys_held, 1), *[(lamp_, RED_IN) for lamp_ in lamps])
    net("P", (d3, RED_OUT), (keys_pressed, 1))
    out = {"cells": cells, "neg": neg, "d1": d1, "d2": d2, "d3": d3, "sums": sums,
           "held": (d1, RED_OUT), "pressed": (d3, RED_OUT), "lamps": lamps,
           "keys_held": keys_held, "keys_pressed": keys_pressed}
    if meters:
        # Presses per button so far: one hold should be one press.
        acc = b.arithmetic(x0 + 5, yc, EACH, "*", 1, EACH)
        net("P", (acc, RED_IN))
        b.wire(acc, GREEN_OUT, acc, GREEN_IN)
        # Longest gap between drops, per button, counting gaps under GAP_CAP ticks: the shortest
        # --hold that never drops a held button is this + 1. G' = ticks since the last drop + 1
        # (G1/G2 count, frozen at GAP_CAP; a constant +1 per lane), C = G' at a drop, M = max(C).
        g1 = b.decider(x0 + 6, yc, [sem.cond(EACH, "<=", 0, RED), sem.cond(EACH, "<", GAP_CAP, GREEN, compare_type="and")],
                       [sem.out(EACH, networks=GREEN)])
        g2 = b.decider(x0 + 7, yc, [sem.cond(EACH, "<=", 0, RED), sem.cond(EACH, ">=", GAP_CAP, GREEN, compare_type="and")],
                       [sem.out(EACH, constant=GAP_CAP - 1)])
        ones = b.constant(x0 + 7, yc + 2, [(sig(s), 1) for s in BUTTON_SIGNALS])
        dc = b.decider(x0 + 8, yc, [sem.cond(EACH, ">", 0, RED), sem.cond(EACH, "<", GAP_CAP, GREEN, compare_type="and")],
                       [sem.out(EACH, networks=GREEN)])
        dm = b.decider(x0 + 9, yc, [sem.cond(EACH, ">", EACH, RED, second_nets=GREEN)],
                       [sem.out(EACH, networks=RED)], [sem.out(EACH, networks=GREEN)])
        net("B", (g1, RED_IN), (g2, RED_IN), (dc, RED_IN))
        net("G'", (g1, GREEN_OUT), (g2, GREEN_OUT), (g1, GREEN_IN), (g2, GREEN_IN), (dc, GREEN_IN), (ones, 2))
        net("C", (dc, RED_OUT), (dm, RED_IN))
        b.wire(dm, GREEN_OUT, dm, GREEN_IN)
        out.update(counter=acc, gap_max=dm, gap_capture=dc)

    taken = b.occupied()
    usable = lambda x, y: (x, y) not in taken  # noqa: E731
    for name, members in nets.items():
        if len(members) > 1:
            wire_net(b, members, taken, usable)
    return out


# ---------------------------------------------------------------------------------------------
# Test schedule and reference model

def schedule(pad, hold, seed=1):
    """Scripted Z presses: drops every 8-12 ticks while a button is held (measured: about 6 a
    second), one item into a random chest of the cell. Returns (inserts, ticks)."""
    rnd = random.Random(seed)
    cells = pad["cells"]
    script = [  # (cell, start, end); end = start for a single tap
        ("ring 0,-1", 10, 130),                                    # hold forward
        ("fire", 170, 170),                                        # tap fire
        ("ring -1,-1", 210, 270),                                  # hold forward+left
        ("ring 0,-1", 280, 310), ("ring 1,-1", 318, 350),          # sweep along the top
        ("weapon", 400, 400), ("weapon", 400 + hold // 2, 400 + hold // 2),  # double tap, inside hold
        ("weapon", 470, 470), ("weapon", 470 + hold + 15, 470 + hold + 15),  # two taps, apart
        ("ring 1,1", 560, 620), ("enter", 660, 660), ("use", 690, 720),
    ]
    inserts = []
    for cell, start, end in script:
        t = start
        while t <= end:
            inserts.append([t, rnd.choice(cells[cell]["chests"]), "iron-plate", 1])
            t += rnd.randint(8, 12)
    fire = next(e for _, e, _, _ in inserts if e in cells["fire"]["chests"])
    inserts.append([740, fire, "iron-plate", -1])  # taking an item out is not a press
    return sorted(inserts), 780


def reference(pad, inserts, ticks, hold):
    """H(t) and P(t) per button signal, from the formulas in the module docstring: what the
    HELD and PRESSED networks hold at t + 1 (the decoder's output at t plus the keyboard
    combinators' contents at t)."""
    owner = {}
    for c in pad["cells"].values():
        for e in c["chests"]:
            owner[e] = c["signals"]
    keyed = {pad.get("keys_held"): "held", pad.get("keys_pressed"): "pressed"}
    X = [{s: 0 for s in BUTTON_SIGNALS} for _ in range(ticks)]
    K = {"held": [], "pressed": []}
    total = {s: 0 for s in BUTTON_SIGNALS}
    keys = {"held": {s: 0 for s in BUTTON_SIGNALS}, "pressed": {s: 0 for s in BUTTON_SIGNALS}}
    by_tick = {}
    for t, e, name, k in inserts:
        if e in keyed:
            by_tick.setdefault(t, []).append((keyed[e], name, k))
        else:
            by_tick.setdefault(t, []).append((e, None, k))
    for t in range(ticks):
        for e, name, k in by_tick.get(t, []):
            if name is None:
                for s in owner[e]:
                    total[s] += k
            else:
                keys[e][name] = k       # a constant combinator entry sets the signal
        X[t] = dict(total)
        K["held"].append(dict(keys["held"]))
        K["pressed"].append(dict(keys["pressed"]))
    x = lambda t, s: X[t][s] if t >= 0 else 0  # noqa: E731
    H = [{s: 0 for s in BUTTON_SIGNALS} for _ in range(ticks)]
    P = [{s: 0 for s in BUTTON_SIGNALS} for _ in range(ticks)]
    for t in range(ticks):
        for s in BUTTON_SIGNALS:
            delta = x(t - 1, s) - x(t - 2, s)
            prev = H[t - 1][s] if t else 0
            H[t][s] = hold if delta > 0 else max(prev - 1, 0)
            P[t][s] = 1 if delta > 0 and prev == 0 else 0
    # The decoder's recurrences use its own outputs; the keyboard adds onto the networks after.
    # A write scheduled for tick k is on the network during tick k (like a chest's contents, which
    # the sums read), so the network at t + 1 holds the keyboard's state as of t + 1.
    for t in range(ticks):
        k = min(t + 1, ticks - 1)
        for s in BUTTON_SIGNALS:
            H[t][s] += K["held"][k][s]
            P[t][s] += K["pressed"][k][s]
    # Gap meter: G'(t) = O(t-1) + 1; O = 0 on a drop, else G' frozen below GAP_CAP;
    # C = G' at a drop (under the cap); M(t) = max(C(t-1), M(t-1)).
    M = [{s: 0 for s in BUTTON_SIGNALS} for _ in range(ticks)]
    for s in BUTTON_SIGNALS:
        o = c = m = 0
        for t in range(ticks):
            delta = x(t - 1, s) - x(t - 2, s)
            g = o + 1
            m = max(c, m)
            M[t][s] = m
            o = 0 if delta > 0 else min(g, GAP_CAP - 1)
            c = g if delta > 0 and g < GAP_CAP else 0
    return H, P, M


def probe(path, ticks, entities, inserts_path):
    res = subprocess.run([str(ROOT / "target" / "release" / "fsim"), "run", str(path), "--ticks", str(ticks),
                          "--input", str(inserts_path)] + [a for e in entities for a in ("--probe", str(e))],
                         check=True, capture_output=True, text=True)
    out = {}
    for line in res.stdout.splitlines():
        m = re.match(r"t=(\d+) #(\d+) [\w-]+: ?(.*)", line)
        t, e = int(m.group(1)), int(m.group(2))
        vals = {f"signal-{k}" if not k.startswith("signal-") else k: int(v)
                for k, v in (kv.split("=") for kv in m.group(3).split())}
        out[(t, e)] = vals
    return out


def verify(path, pad, inserts, ticks, hold):
    ins_path = path.with_suffix(".inserts.json")
    ins_path.write_text(json.dumps(inserts))
    got = probe(path, ticks, [pad["d1"], pad["d2"], pad["d3"], pad["gap_max"]], ins_path)
    H, P, M = reference(pad, inserts, ticks, hold)
    bad = []
    for t in range(ticks):
        h = {s: got[(t, pad["d1"])].get(s, 0) + got[(t, pad["d2"])].get(s, 0) for s in BUTTON_SIGNALS}
        p = {s: got[(t, pad["d3"])].get(s, 0) for s in BUTTON_SIGNALS}
        m = {s: got[(t, pad["gap_max"])].get(s, 0) for s in BUTTON_SIGNALS}
        if h != H[t] or p != P[t] or m != M[t]:
            bad.append((t, h, H[t], p, P[t], m, M[t]))
    presses = {s: sum(P[t][s] for t in range(ticks)) for s in BUTTON_SIGNALS}
    print(f"{ticks} ticks x {len(BUTTON_SIGNALS)} buttons checked against the reference; {len(bad)} ticks differ")
    print("presses per button:", {s.removeprefix("signal-"): v for s, v in presses.items()})
    print("longest gap per button:", {s.removeprefix("signal-"): v for s, v in M[-1].items()})
    for row in bad[:5]:
        print("  ", row)
    return not bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="320x200", help="screen size the buttons are sized for")
    ap.add_argument("--button-size", type=int, help="chests per button side (default from --size)")
    ap.add_argument("--hold", type=int, default=DEFAULT_HOLD, help="ticks a button stays held after a drop")
    ap.add_argument("--verify", action="store_true")
    quality_arg(ap)
    a = ap.parse_args()
    _, h = map(int, a.size.lower().split("x"))
    n = a.button_size or button_size(h)
    if not 3 <= n <= MAX_BUTTON:
        raise SystemExit(f"--button-size must be 3..{MAX_BUTTON}")
    b = Blueprint(f"Input pad {n}x{n}", a.substation_quality)
    pad = build_pad(b, 0, 0, n, a.hold, meters=True)
    b.power()
    b.link_power()
    b.check_reach()
    inserts, ticks = schedule(pad, a.hold)
    data = b.to_json()
    data["fsim"] = {"ticks": ticks, "doc": f"input pad n={n} hold={a.hold}", "inserts": inserts}
    gen = HERE / "gen"
    gen.mkdir(exist_ok=True)
    path = gen / f"input_pad_n{n}.json"
    path.write_text(json.dumps(data, separators=(",", ":")))
    (gen / f"input_pad_n{n}.txt").write_text(encode(b.to_json()) + "\n")
    counts = {}
    for e in b.entities:
        counts[e["name"]] = counts.get(e["name"], 0) + 1
    print(f"{path.relative_to(ROOT)}: {len(b.entities)} entities {counts}")
    if a.verify and not verify(path, pad, inserts, ticks, a.hold):
        sys.exit(1)


if __name__ == "__main__":
    main()
