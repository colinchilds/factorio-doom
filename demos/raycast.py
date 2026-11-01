"""M1 raycaster: flat-shaded walls, floor and ceiling, a scripted camera, at any --size.

Everything runs as vectors whose lanes are screen columns (no per-column units):

  scalars   clock -> view angle and path angle -> cos/sin (bit-sliced lookups) -> position,
            direction, camera plane
  rays      RDX, RDY per column = dir + plane * camX (camX is a constant vector)
  march     for vertical grid lines and for horizontal ones separately: K steps, each one network
            of W lanes. Step k holds every column's k-th crossing: its distance D_k and the map
            cell it enters (as a key 64*(x+1) + y). A lookup decider marks walls with -2^30, so a
            running per-lane min over the steps (one decider each) keeps the nearest wall.
            Distances are doubled and the horizontal march adds 1, so the min carries the side.
  shade     wall height LH = H*2048 / DS, brightness 15*8192 / (DS + 8192) (+1), side tint
  rows      one decider per screen row: LH > |2y+1-H| ? wall color : the row's floor/ceiling

Lanes vanish when a value is 0, which would silently drop columns. Every vector that might hit 0
is combined lane-wise with a constant vector that is nonzero on every lane (ONES and friends).

  python3 demos/raycast.py --size 160x100 --verify   # fsim vs the netlist evaluation
"""
import argparse
import json
import math
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "conformance"))
from bp import Blueprint, encode  # noqa: E402
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import EACH, GREEN, LANE_SIGNALS, RED, Graph  # noqa: E402

MAP = """
################
#..............#
#..##......##..#
#..##......##..#
#..............#
#......##......#
#...#......#...#
#...#......#...#
#...#......#...#
#......##......#
#..............#
#..##......##..#
#..##......##..#
#..............#
#..............#
################
""".split()
K = 16                       # grid lines marched per axis (>= map size)
FP = 256                     # position units per tile
CENTER = (8 * FP, 7.5 * FP)  # camera path: circle around this point
RADIUS = int(1.25 * FP)
PLANE = 676                  # tan(fov/2) * 1024: about 66 degrees
HIT = -(1 << 30)
BASE_V, BASE_H = (15, 11, 8), (11, 8, 6)   # wall color per brightness step, by side
FOG = 8192                   # DS at half brightness (4 tiles)


def pack(rgb):
    return (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]


def background(y, h):
    half = h // 2
    if y < half:
        t = (half - y) * 255 // half
        return (14 + 34 * t // 255, 14 + 34 * t // 255, 20 + 40 * t // 255)
    t = (y - half + 1) * 255 // (h - half)
    return (20 + 70 * t // 255, 18 + 50 * t // 255, 14 + 30 * t // 255)


def wall_keys():
    return [64 * (x + 1) + y for y, row in enumerate(MAP) for x, c in enumerate(row) if c == "#"]


def trig(fn):
    return {a: round(1024 * fn(2 * math.pi * a / 256)) + 1024 for a in range(256)}


def build_graph(w, h):
    g = Graph()
    cols = LANE_SIGNALS[:w]
    vec = lambda vals, label="vec": g.const([(cols[c], v) for c, v in enumerate(vals)], label)
    ones = lambda: vec([1] * w, "ones")
    camx = [(2 * c + 1 - w) * 1024 // w for c in range(w)]
    assert all(camx)

    # -- scalars
    T = g.clock()
    A = g.ss(g.ss(T, ">>", 1), "AND", 255, "view angle")
    B = g.ss(g.ss(T, ">>", 2), "AND", 255, "path angle")
    cos_t, sin_t = trig(math.cos), trig(math.sin)
    DX = g.ss(g.lookup_scalar(A, cos_t, 12, "cosA"), "-", 1024)
    DY = g.ss(g.lookup_scalar(A, sin_t, 12, "sinA"), "-", 1024)
    OX = g.ss(g.lookup_scalar(B, cos_t, 12, "cosB"), "-", 1024)
    OY = g.ss(g.lookup_scalar(B, sin_t, 12, "sinB"), "-", 1024)
    PX = g.ss(g.ss(g.ss(g.ss(OX, "*", RADIUS), ">>", 10), "+", int(CENTER[0])), "OR", 1, "px")
    PY = g.ss(g.ss(g.ss(g.ss(OY, "*", RADIUS), ">>", 10), "+", int(CENTER[1])), "OR", 1, "py")
    PLX = g.ss(g.ss(DY, "*", -PLANE), ">>", 10)
    PLY = g.ss(g.ss(DX, "*", PLANE), ">>", 10)
    DX10 = g.ss(g.ss(DX, "<<", 10), "OR", 512)
    DY10 = g.ss(g.ss(DY, "<<", 10), "OR", 512)

    # -- ray directions per column (scale 1024), forced odd so never 0
    def ray(PL, D10):
        r = g.vc(g.vv(g.vs(vec(camx, "camx"), "*", PL), "+", g.broadcast(ones(), D10)), ">>", 10)
        return g.vv(r, "OR", ones())
    RDX, RDY = ray(PLX, DX10), ray(PLY, DY10)

    def march(RD, RO, P, PO, side):
        """Crossings of the grid lines perpendicular to this axis. RD: ray component along the
        axis, RO: the other one; P/PO: position along / across (scale FP)."""
        FR = g.ss(P, "AND", FP - 1)
        FI = g.ss(P, ">>", 8)
        pos = lambda c: g.decider([sem.cond(EACH, ">", 0, RED)], [sem.out(EACH, constant=c)], red=RD)
        S = g.decider([sem.cond(EACH, ">", 0, RED)], [sem.out(EACH, constant=1)], [sem.out(EACH, constant=-1)], red=RD)
        AR = g.vv(RD, "*", S)
        NUM = g.vv(pos(FP), "+", g.broadcast(ones(), g.ss(0, "-", FR)))         # (line - p), scale FP
        T1 = g.vv(g.vc(NUM, "*", 4096), "/", RD)                                  # distance, 2^10 per tile
        DT = g.vv(vec([1 << 20] * w, "c20"), "/", AR)
        Q = g.vv(g.vv(g.vc(g.vv(NUM, "*", RO), "*", 256), "/", RD), "+", g.broadcast(ones(), g.ss(PO, "<<", 8)))
        DQ = g.vv(g.vc(RO, "*", 65536), "/", AR)                                  # across, 2^16 per tile
        if side == 0:   # vertical lines: integer part is x -> 64*(x+1); across part is y
            IK = g.vv(pos(128), "+", g.broadcast(ones(), g.ss(FI, "<<", 6)))
            STEP = g.vc(S, "*", 64)
            cell = lambda q: g.vc(q, ">>", 16)
        else:           # horizontal lines: integer part is y (+64 offset); across part is 64*x
            IK = g.vv(pos(2), "+", g.broadcast(ones(), g.ss(FI, "+", 63)))
            STEP = S
            cell = lambda q: g.vc(g.vc(q, ">>", 16), "<<", 6)
        D = g.vc(T1, "*", 2) if side == 0 else g.vc(g.vc(T1, "*", 2), "+", 1)
        DD = g.vc(DT, "*", 2)
        # Start each chain at the time its consumers need it, so only the increments need delays.
        t0 = Q.time
        lat_cell = 1 if side == 0 else 2   # ops in cell()
        IK = g.delay(IK, t0 + lat_cell - IK.time) if IK.time < t0 + lat_cell else IK
        D = g.delay(D, t0 + lat_cell + 2 - D.time) if D.time < t0 + lat_cell + 2 else D
        best = None
        walls = wall_keys()
        hit_conds = [sem.cond(EACH, "=", k, RED, "or" if i else None) for i, k in enumerate(walls)]
        for k in range(K):
            key = g.vv(IK, "+", cell(Q), f"key{side}.{k}")
            hit = g.decider(hit_conds, [sem.out(EACH, constant=HIT)], red=key, label=f"hit{side}.{k}")
            v = [hit, D]
            if best is None:
                best = v
            else:
                best = [g.decider([sem.cond(EACH, "<", EACH, RED, None, GREEN)], [sem.out(EACH, networks=RED)],
                                  [sem.out(EACH, networks=GREEN)], red=best, green=v, label=f"min{side}.{k}")]
            if k + 1 < K:
                Q, IK, D = g.vv(Q, "+", DQ), g.vv(IK, "+", STEP), g.vv(D, "+", DD)
        return best

    MX = march(RDX, RDY, PX, PY, 0)
    MY = march(RDY, RDX, PY, PX, 1)
    M = g.decider([sem.cond(EACH, "<", EACH, RED, None, GREEN)], [sem.out(EACH, networks=RED)],
                  [sem.out(EACH, networks=GREEN)], red=MX, green=MY, label="min")
    DS = g.vc(M, "+", 1 << 30, "DS")
    LHB = g.vv(g.vv(vec([h << 11] * w, "h"), "/", DS), "+", ones(), "LH+1")
    BR = g.vv(g.vv(vec([15 * FOG] * w, "fog"), "/", g.vc(DS, "+", FOG)), "+", ones(), "brightness")
    BASE = g.vv(g.vc(g.vc(DS, "AND", 1), "*", pack(BASE_H) - pack(BASE_V)), "+", vec([pack(BASE_V)] * w, "base"))
    COLOR = g.vv(BR, "*", BASE, "color")

    rows = []
    for y in range(h):
        n = g.decider([sem.cond(EACH, ">", abs(2 * y + 1 - h) + 1, GREEN)], [sem.out(EACH, networks=RED)],
                      [sem.out(EACH, constant=pack(background(y, h)))], red=COLOR, green=LHB, label=f"row{y}")
        n.fixed, n.pos = True, (-2, y)
        rows.append(n)
    return g, cols, rows, {"T": T, "PX": PX, "PY": PY, "DX": DX, "DY": DY, "PLX": PLX, "PLY": PLY}


def lattice(x, y):
    return (x - 8) % 18 in (0, 1) and (y - 8) % 18 in (0, 1)


def build(w, h):
    if w > len(LANE_SIGNALS):
        raise SystemExit(f"width {w} needs {w} lane signals, only {len(LANE_SIGNALS)}")
    g, cols, rows, scal = build_graph(w, h)
    b = Blueprint(f"Raycaster M1 {w}x{h}")
    ent = g.compile(b, blocked=lattice, region=lambda x, y: x <= -3)
    for y, n in enumerate(rows):
        lamps = [b.lamp(x, y, {"use_colors": True, "color_mode": 2, "rgb_signal": cols[x]}, always_on=True)
                 for x in range(w) if not lattice(x, y)]
        b.wire(ent[n.id][1], 3, lamps[0], 1)
        b.chain(lamps, 1)
    _substations(b)
    b.power()
    b.link_power()
    b.check_reach()
    return g, b, cols, rows, scal


def _substations(b):
    """Substations on the 18-tile lattice wherever they power something."""
    tiles = b.occupied()
    xs = [x for x, _ in tiles]
    ys = [y for _, y in tiles]
    for sx in range(min(xs) - 18, max(xs) + 19):
        for sy in range(min(ys) - 18, max(ys) + 19):
            if (sx - 8) % 18 or (sy - 8) % 18:
                continue
            if any((x, y) in tiles for x in range(sx - 8, sx + 10) for y in range(sy - 8, sy + 10)):
                if not {(sx, sy), (sx + 1, sy), (sx, sy + 1), (sx + 1, sy + 1)} & tiles:
                    b.substation(sx, sy)


# ---------------------------------------------------------------------------------------------
# Reference images

def evaluate(g, cols, rows, T):
    outs = g.evaluate(T, rows)
    return [[o.get(sem.key(c), 0) for c in cols] for o in outs]


def ideal(g, scal, w, h, T):
    """Float raycaster from the same camera scalars, shaded the same way: a sanity check for the
    fixed-point math (not expected to match every pixel)."""
    vals = {k: g.evaluate(T, [s.node])[0].get(sem.key(s.signal), 0) for k, s in scal.items()}
    px, py = vals["PX"] / FP, vals["PY"] / FP
    img = [[pack(background(y, h))] * w for y in range(h)]
    for c in range(w):
        camx = ((2 * c + 1 - w) * 1024 // w) / 1024
        rdx = (vals["DX"] + vals["PLX"] * camx) / 1024
        rdy = (vals["DY"] + vals["PLY"] * camx) / 1024
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
            if MAP[my][mx] == "#":
                break
        ds = 2 * d * 1024 + side
        lh = int(h * 2048 / ds)
        br = int(15 * FOG / (ds + FOG)) + 1
        color = br * pack(BASE_H if side else BASE_V)
        for y in range(h):
            if lh > abs(2 * y + 1 - h):
                img[y][c] = color
    return img


def read_ppm(path):
    data = path.read_bytes()
    parts = data.split(b"\n", 3)
    w, h = map(int, parts[1].split())
    px = parts[3]
    return [[(px[3 * (y * w + x)] << 16) | (px[3 * (y * w + x) + 1] << 8) | px[3 * (y * w + x) + 2]
             for x in range(w)] for y in range(h)]


def verify(path, g, cols, rows, scal, w, h, ticks):
    out_dir = ROOT / "out" / f"raycast_{w}x{h}_ppm"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.ppm"):
        f.unlink()
    subprocess.run([str(ROOT / "target" / "release" / "fsim"), "run", str(path), "--ticks", str(ticks),
                    "--ppm", str(out_dir)], check=True)
    frames = {int(p.stem): read_ppm(p) for p in sorted(out_dir.glob("*.ppm"))}
    holes = {(x, y) for x in range(w) for y in range(h) if lattice(x, y)}
    cache = {}

    def ref(T):
        if T not in cache:
            cache[T] = evaluate(g, cols, rows, T)
        return cache[T]

    def diff(img, ref_img):
        return sum(1 for y in range(h) for x in range(w) if (x, y) not in holes and img[y][x] != ref_img[y][x])

    last = sorted(frames)[-6:]
    lags = [k for k in range(max(0, rows[0].time - 5), rows[0].time + 15) if all(diff(frames[t], ref(t - k)) == 0 for t in last[:2])]
    lags = [k for k in lags if all(diff(frames[t], ref(t - k)) == 0 for t in last)]
    if not lags:
        print("no latency makes the last frames match the evaluation")
        return False
    lag = lags[0]
    checked = [t for t in sorted(frames) if t - lag >= 1]
    bad = [(t, n) for t in checked[2:] for n in [diff(frames[t], ref(t - lag))] if n]
    print(f"latency {lag} ticks; {len(checked) - 2} frames x {w * h - len(holes)} lamps checked against the "
          f"netlist evaluation; {len(bad)} differ")
    for t, n in bad[:10]:
        print(f"  tick {t}: {n} lamps differ")
    # Fixed point vs float.
    worst = 0
    for t in checked[2::25]:
        n = diff(ref(t - lag), ideal(g, scal, w, h, t - lag))
        worst = max(worst, n)
    print(f"fixed point vs float raycaster: at most {worst} of {w * h} pixels differ "
          f"({100 * worst / (w * h):.2f}%) on sampled frames")
    return not bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="160x100")
    ap.add_argument("--ticks", type=int, default=400)
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    w, h = map(int, a.size.lower().split("x"))
    g, b, cols, rows, scal = build(w, h)
    data = b.to_json()
    data["fsim"] = {"ticks": a.ticks, "doc": f"raycaster M1 {w}x{h}"}
    gen = HERE / "gen"
    gen.mkdir(exist_ok=True)
    path = gen / f"raycast_{w}x{h}.json"
    path.write_text(json.dumps(data, separators=(",", ":")))
    (gen / f"raycast_{w}x{h}.txt").write_text(encode(b.to_json()) + "\n")
    n_comb = sum(1 for e in b.entities if "combinator" in e["name"] and e["name"] != "constant-combinator")
    n_lamp = sum(1 for e in b.entities if e["name"] == "small-lamp")
    print(f"{path.relative_to(ROOT)}: {len(b.entities)} entities ({n_comb} combinators, {n_lamp} lamps, {g.relays} relay poles)")
    if a.verify and not verify(path, g, cols, rows, scal, w, h, a.ticks):
        sys.exit(1)


if __name__ == "__main__":
    main()
