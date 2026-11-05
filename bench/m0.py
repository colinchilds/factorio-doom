"""UPS benchmarks: what does a lane-op cost in the real game? Generates synthetic row-stage
blueprints.

Every variant uses a row layout: one wire network per screen row whose lanes are the
columns (W signals), plus a shared column "bus" (green) and a free-running clock (red) so values
change every tick. Rows are independent copies, so cost should scale with W x H.

  baseline       one constant combinator (fixed per-tick cost of the game)
  lamps          per row: s1 = bus + T, then W lamps (packed RGB)
  pixel          per row: 9 combinators shaped like a texture path, then W lamps
  pixel_nolamps  the same 9 combinators, no lamps
  lookupN        per row: s1, then one decider with N OR'd `Each = k` conditions (no lamps)

Screens with lamps get a substation grid inside them (2x2 dead pixels every 18 tiles), the way
giftorio powers its displays.

The pixel pipeline (about 9 lane-ops per pixel):
  s1 Each(bus) + T     s2 Each - row      s3 Each * Each(bus)   s4 Each >> 3   s5 Each AND 28
  s6 Each(bus) >> Each s7 Each AND 15      s8 Each * Each(bus)   s9 decider: 0 < Each < 2^24
                                                                   then copy, else floor color

  python3 bench/m0.py 160x100 320x200          # writes bench/gen/<variant>_<W>x<H>.json
  python3 harness/run.py --bench 300 bench/gen/*_160x100.json
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from compiler.bp import EACH, GREEN, GREEN_IN, RED, RED_IN, RED_OUT, Blueprint, cond, out, sig  # noqa: E402

# Column signals: safe in vanilla and Space Age (harness/run.py --lanes). No virtual signals, so
# the special ones (each/everything/anything) and the clock T can't collide with a column.
COLUMN_SIGNALS = [sig(n, t) for t, n in json.loads((HERE.parent / "conformance" / "lane_signals.json").read_text())]
T = sig("signal-T")
STAGES = 9
GAP_AT = 5


def column_signals(w):
    if w > len(COLUMN_SIGNALS):
        raise SystemExit(f"only {len(COLUMN_SIGNALS)} column signals available")
    return COLUMN_SIGNALS[:w]


def build(variant, w, h):
    b = Blueprint(f"m0 {variant} {w}x{h}")
    cols = column_signals(w)
    if variant == "baseline":
        b.constant(0, 0, {"signal-A": 1})
        b.power()
        return b

    lamps = variant in ("lamps", "pixel")
    nstages = {"lamps": 1, "pixel": STAGES, "pixel_nolamps": STAGES}.get(variant, 2)
    # Stage j sits at tiles pos(j) .. pos(j) + 1 (facing east); lamps start at x = 0. A 2-tile gap
    # after stage GAP_AT leaves room for substations inside the combinator block.
    gap = 2 if nstages > GAP_AT else 0
    x0 = -2 * nstages - gap

    def pos(j):
        return x0 + 2 * j + (gap if j >= GAP_AT else 0)

    # Clock (red) and column bus (green), shared by every row.
    clock_c = b.constant(x0 - 4, -3, {"signal-T": 1})
    clock = b.decider(x0 - 3, -3, [cond(T, "<", 1 << 20, RED)], [out(T, networks=RED)], direction=4)
    b.wire(clock_c, 1, clock, RED_IN)
    b.wire(clock, RED_OUT, clock, RED_IN)
    bus = b.constant(x0 - 4, -2, [(c, 1000 + 37 * i) for i, c in enumerate(cols)])

    # Substations inside the screen on an 18-tile grid, as giftorio does: 2x2 dead pixels each.
    holes = set()
    if lamps:
        for sy in sorted({min(8 + 18 * j, h - 2) for j in range((h + 17) // 18)}):
            for sx in sorted({min(8 + 18 * i, w - 2) for i in range((w + 17) // 18)}):
                b.substation(sx, sy)
                holes |= {(sx, sy), (sx + 1, sy), (sx, sy + 1), (sx + 1, sy + 1)}

    prev_clock_reader, prev_bus_reader = clock, bus
    prev_bus_conn = 2  # green on the constant combinator
    for y in range(h):
        st = []

        def arith(first, op, second, **kw):
            st.append(b.arithmetic(pos(len(st)), y, first, op, second, EACH, direction=4, **kw))
            return st[-1]

        s1 = arith(EACH, "+", T, first_nets=GREEN, second_nets=RED)
        b.wire(prev_clock_reader, RED_OUT if prev_clock_reader == clock else RED_IN, s1, RED_IN)
        prev_clock_reader = s1
        bus_readers = [s1]
        if variant.startswith("pixel"):
            arith(EACH, "-", 7 * y)
            bus_readers.append(arith(EACH, "*", EACH, first_nets=RED, second_nets=GREEN))
            arith(EACH, ">>", 3)
            arith(EACH, "AND", 28)
            bus_readers.append(arith(EACH, ">>", EACH, first_nets=GREEN, second_nets=RED))
            arith(EACH, "AND", 15)
            bus_readers.append(arith(EACH, "*", EACH, first_nets=RED, second_nets=GREEN))
            st.append(b.decider(pos(len(st)), y,
                                [cond(EACH, ">", 0, RED), cond(EACH, "<", 1 << 24, RED, compare_type="and")],
                                [out(EACH, networks=RED)],
                                else_outputs=[out(EACH, copy=False, constant=0x203040)], direction=4))
        elif variant.startswith("lookup"):
            n = int(variant[len("lookup"):])
            conds = [cond(EACH, "=", 5000 + 97 * k, RED) for k in range(n)]
            st.append(b.decider(pos(1), y, conds, [out(EACH, networks=RED)], direction=4))
        for a, c in zip(st, st[1:]):
            b.wire(a, RED_OUT, c, RED_IN)
        # Bus: chain this row's bus readers' green inputs, and link to the row above.
        b.wire(prev_bus_reader, prev_bus_conn, bus_readers[0], GREEN_IN)
        for a, c in zip(bus_readers, bus_readers[1:]):
            b.wire(a, GREEN_IN, c, GREEN_IN)
        prev_bus_reader, prev_bus_conn = bus_readers[0], GREEN_IN
        if lamps:
            row = [b.lamp(x, y, {"use_colors": True, "color_mode": 2, "rgb_signal": cols[x]}, always_on=True) for x in range(w) if (x, y) not in holes]
            b.wire(st[-1], RED_OUT, row[0], 1)
            b.chain(row, 1)
    b.power()
    b.link_power()
    return b


VARIANTS = ["baseline", "lamps", "pixel", "pixel_nolamps", "lookup16", "lookup64", "lookup256"]


def main():
    sizes = sys.argv[1:] or ["160x100"]
    gen = HERE / "gen"
    gen.mkdir(exist_ok=True)
    for size in sizes:
        w, h = map(int, size.split("x"))
        for v in VARIANTS:
            if v == "baseline" and size != sizes[0]:
                continue
            b = build(v, w, h)
            data = b.to_json()
            data["fsim"] = {"ticks": 60, "doc": f"bench {v} {w}x{h}"}
            name = "baseline" if v == "baseline" else f"{v}_{w}x{h}"
            path = gen / f"{name}.json"
            path.write_text(json.dumps(data, separators=(",", ":")))
            print(f"{path.relative_to(HERE.parent)}: {len(b.entities)} entities")


if __name__ == "__main__":
    main()
