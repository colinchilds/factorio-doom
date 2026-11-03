"""Netlist compiler: a dataflow graph of combinator ops, compiled to a blueprint.

A Graph holds nodes. Each node is one combinator (or a constant, or the clock) whose inputs are a
list of source nodes on the red wire and a list on the green wire (sources on one wire add up).
The compiler:

  * balances latency: every combinator outputs one tick after it reads, so all sources feeding a
    node must carry the same frame. Node.time is that frame's offset; sources that are earlier
    get delay combinators (`Each + 0`), shared through a memo.
  * resolves wiring conflicts: a node's red output connector can sit on one network only, so a
    node needed in two different red networks gets a duplicate (same op, same inputs).
  * places nodes greedily within wire reach (footprint gap <= 9) of their networks, starting
    from fixed sinks and walking back in time, then wires each network as a minimum spanning tree.
  * evaluates the graph without timing (Graph.evaluate), using the same specs the blueprint gets.
    That's the reference the simulated blueprint must match frame for frame.

Vectors are networks whose lanes are "lane signals" (conformance/lane_signals.json). Scalars
are named virtual signals, allocated so no two different scalars share a name.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "conformance"))
sys.path.insert(0, str(ROOT))
from bp import Blueprint, sig  # noqa: E402

from compiler import semantics as sem  # noqa: E402

EACH = sig("signal-each")
EVERYTHING = sig("signal-everything")
RED, GREEN = {"red": True, "green": False}, {"red": False, "green": True}
LANE_SIGNALS = [sig(n, t) for t, n in json.loads((ROOT / "conformance" / "lane_signals.json").read_text())]
SCALAR_NAMES = ([f"signal-{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"] + [f"signal-{d}" for d in range(10)]
                + [f"signal-{c}" for c in ("red", "green", "blue", "yellow", "pink", "cyan", "white", "grey",
                                           "black", "check", "dot", "info", "deny")])
# Every other virtual signal, after the original list (so earlier allocations don't change).
SCALAR_NAMES += [n for t, n, q in json.loads((ROOT / "conformance" / "signal_order.json").read_text())
                 if t == "virtual" and q == "normal" and n not in SCALAR_NAMES]
REACH = 9
RED_IN, GREEN_IN, RED_OUT, GREEN_OUT = 1, 2, 3, 4


class Node:
    def __init__(self, g, kind, spec, red=(), green=(), time=None, label=""):
        self.g, self.kind, self.spec = g, kind, spec
        self.red, self.green = list(red), list(green)
        self.time = time
        self.label = label
        self.id = g._next_id  # not len(g.nodes): passes drop nodes, and ids must stay unique
        g._next_id += 1
        self.pos = None
        self.fixed = False
        g.nodes.append(self)

    def __repr__(self):
        return f"<{self.kind} {self.id} {self.label} t={self.time}>"


class Scalar:
    """A named scalar: the node producing it and its signal."""

    def __init__(self, node, signal):
        self.node, self.signal = node, signal


class Graph:
    def __init__(self, reserved=()):
        """reserved: signal names the graph must not allocate (those of external inputs)."""
        self.nodes = []
        self._next_id = 0
        self._delays = {}
        reserved = {r if isinstance(r, str) else r["name"] for r in reserved}
        self._names = iter([n for n in SCALAR_NAMES if n not in reserved])

    # -- construction --------------------------------------------------------------------------

    def new_scalar_signal(self):
        try:
            return sig(next(self._names))
        except StopIteration:
            raise SystemExit("out of scalar signal names") from None

    def const(self, signals, label="const"):
        """signals: list of (signal, value). Constants have no time; each use gets its own copy
        when wiring (see _split_constants)."""
        return Node(self, "const", [(s, v) for s, v in signals if v], time=None, label=label)

    def ext(self, entity, pos, size=(2, 1), connectors=(RED_OUT, GREEN_OUT), label="ext"):
        """An existing entity (placed by the caller, footprint at pos with size) whose output feeds
        the graph at frame time 0: a register, or a relay pole carrying one. connectors are the
        (red, green) connector ids to wire to. Its value comes from evaluate(inputs=...)."""
        n = Node(self, "ext", {"entity": entity, "size": size, "connectors": connectors}, time=0, label=label)
        n.fixed, n.pos = True, pos
        return n

    def scalar(self, node, signal):
        """A named signal on a node's output (e.g. one value of an ext register)."""
        return Scalar(node, signal if isinstance(signal, dict) else sig(signal))

    def clock(self):
        """A free-running counter; its output is T (frame time 0)."""
        s = self.new_scalar_signal()
        n = Node(self, "clock", {"signal": s}, time=0, label="clock")
        return Scalar(n, s)

    def delay(self, n, k):
        """n delayed by k ticks (shared chains)."""
        for i in range(k):
            key = (n.id, 1)
            if key not in self._delays:
                base = n.label if not n.label.startswith("delay") else n.label.split(" ", 1)[1]
                d = Node(self, "arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=[n],
                         time=n.time + 1, label=f"delay {base}")
                self._delays[key] = d
            n = self._delays[key]
        return n

    def _align(self, red, green):
        srcs = [s for s in red + green if s.time is not None]
        t = max((s.time for s in srcs), default=None)
        fix = lambda lst: [s if s.time is None else self.delay(s, t - s.time) for s in lst]
        return fix(red), fix(green), (None if t is None else t + 1)

    def op(self, kind, spec, red=(), green=(), label=""):
        red, green, t = self._align(list(red), list(green))
        return Node(self, kind, spec, red, green, time=t, label=label)

    # Vector ops (lanes = lane signals). Inputs are nodes (or lists of nodes, which add up).
    def vv(self, a, op, b, label=""):
        """Each(red a) op Each(green b), lane by lane over the union of lanes."""
        return self.op("arith", sem.arith_spec(EACH, op, EACH, EACH, RED, GREEN), _l(a), _l(b), label)

    def vc(self, a, op, c, label=""):
        """Each(a) op constant."""
        return self.op("arith", sem.arith_spec(EACH, op, c, EACH, RED), _l(a), [], label)

    def vs(self, a, op, s, label=""):
        """Each(red a) op scalar(green)."""
        return self.op("arith", sem.arith_spec(EACH, op, s.signal, EACH, RED, GREEN), _l(a), [s.node], label)

    def broadcast(self, ones, s, label=""):
        """ones (a constant vector of 1s) * scalar: the scalar on every lane."""
        return self.vs(ones, "*", s, label or "bcast")

    # Scalar ops
    def ss(self, a, op, b, label="", out=None):
        """a op b for scalars/ints; result is a new scalar (or the named signal out)."""
        out = out or self.new_scalar_signal()
        first = a.signal if isinstance(a, Scalar) else a
        second = b.signal if isinstance(b, Scalar) else b
        srcs = [x.node for x in (a, b) if isinstance(x, Scalar)]
        if len(srcs) == 2 and srcs[0] is not srcs[1] and a.signal == b.signal:
            raise ValueError("two scalars with one name")
        spec = sem.arith_spec(first, op, second, out, RED if isinstance(a, Scalar) else None,
                              RED if isinstance(b, Scalar) else None)
        n = self.op("arith", spec, _dedup(srcs), [], label)
        return Scalar(n, out)

    def lookup_scalar(self, a, table, bits, label="", out=None):
        """table[a] for a scalar a, as bit-sliced deciders: one per output bit, each an OR of the
        keys with that bit set, outputting the bit's weight. All bits land on one signal.

        bits=32 takes negative values as they are (two's complement: bit 31 weighs -2^31). Bits
        with the same key set share one decider outputting their summed weight, so the sign bits
        of a signed table cost one decider."""
        if bits < 32:
            bad = [v for v in table.values() if not 0 <= v < 1 << bits]
            assert not bad, f"lookup {label!r}: values {sorted(set(bad))[:5]} don't fit in {bits} bits"
        out = out or self.new_scalar_signal()
        groups = {}
        for b in range(bits):
            keys = tuple(k for k, v in table.items() if (v >> b) & 1)
            if keys:
                groups.setdefault(keys, []).append(b)
        nodes = []
        for keys, bs in groups.items():
            # Runs of 3+ consecutive keys become one range (two conditions).
            runs = []
            for k in keys:
                if runs and runs[-1][1] == k - 1:
                    runs[-1][1] = k
                else:
                    runs.append([k, k])
            conds = []
            for lo, hi in runs:
                if hi - lo >= 2:
                    conds += [sem.cond(a.signal, ">=", lo, RED, "or" if conds else None),
                              sem.cond(a.signal, "<=", hi, RED, "and")]
                else:
                    conds += [sem.cond(a.signal, "=", k, RED, "or" if conds else None) for k in range(lo, hi + 1)]
            spec = sem.decider_spec(conds, [sem.out(out, constant=sem.i32(sum(1 << b for b in bs)))])
            nodes.append(self.op("decider", spec, [a.node], [], f"{label} bit{bs[0]}"))
        return Scalar(Collect(nodes), out)

    def decider(self, conds, outs, else_outs=None, red=(), green=(), label=""):
        return self.op("decider", sem.decider_spec(conds, outs, else_outs), _l(red), _l(green), label)

    def requality(self, a, quality, label=""):
        """Every lane of a (red) moved to `quality` (a selector's quality transfer, Space Age)."""
        return self.op("selector", sem.quality_transfer(quality), _l(a), [], label or f"to {quality}")

    def spread(self, a, qualities, label=""):
        """a's lanes copied onto every quality in `qualities` (normal first), one tick later: one
        quality transfer per quality (normal included: a delay alone would do, but the selector
        keeps every copy alike). The copies share a network (a Collect)."""
        return Collect([self.requality(a, q, f"{label or a.label} @{q}") for q in qualities])

    # -- evaluation ----------------------------------------------------------------------------

    def evaluate(self, T, want, inputs=None):
        """Outputs of the nodes in `want` for clock value T and ext values inputs ({node: signal
        dict}), ignoring timing."""
        cache, sums, scratch = {}, {}, {}
        inputs = {k.id: v for k, v in (inputs or {}).items()}

        def total(srcs):
            """The sum of several outputs, shared by every node reading the same set (and never
            mutated, so semantics can cache by id)."""
            k = tuple(sorted(m.id for m in srcs))
            if k not in sums:
                acc = {}
                for m in srcs:
                    sem.add_into(acc, val(m))
                sums[k] = acc
            return sums[k]

        def val(n):
            if isinstance(n, Collect):
                acc = {}
                for m in n.nodes:
                    sem.add_into(acc, val(m))
                return acc
            if n.id in cache:
                return cache[n.id]
            if n.kind == "const":
                r = {sem.key(s): v for s, v in n.spec}
            elif n.kind == "clock":
                r = {sem.key(n.spec["signal"]): T} if T else {}
            elif n.kind == "ext":
                r = dict(inputs.get(n.id, {}))
            else:
                # A node may read a network on a wire its spec ignores (n.nominal: "green"), only so
                # the network runs through it; that input isn't evaluated (it may even be later).
                green = [] if getattr(n, "nominal", None) == "green" else _flat(n.green)
                r = sem.evaluate(n.kind, n.spec, total(_flat(n.red)), total(green), scratch)
            cache[n.id] = r
            return r

        sys.setrecursionlimit(100000)
        return [val(n) for n in want]

    # -- compilation ---------------------------------------------------------------------------

    def compile(self, b, blocked, region, x_right=-6):
        """Place and wire every node into Blueprint b. blocked(x, y) says a tile can't be used;
        region(x, y) says it may be. Nodes with .fixed already have .pos."""
        self._expand_collects()
        self._split_constants()
        self._dedup_outputs()
        nets = self._nets()
        _place(self, nets, blocked, region, x_right)
        ent = _emit(self, b)
        _wire(self, nets, b, ent, lambda x, y: region(x, y) and not blocked(x, y))
        return ent

    def _expand_collects(self):
        for n in self.nodes:
            n.red = [m for x in n.red for m in (x.nodes if isinstance(x, Collect) else [x])]
            n.green = [m for x in n.green for m in (x.nodes if isinstance(x, Collect) else [x])]

    def _split_constants(self):
        """Every input network gets its own constant combinator, placed next to a consumer:
        consumers reading the same sources on the same color (one network) share it. A lone
        constant the caller placed (fixed) is kept as it is."""
        made = {}
        for n in list(self.nodes):
            for attr in ("red", "green"):
                lst = getattr(n, attr)
                consts = [s for s in lst if s.kind == "const"]
                if not consts:
                    continue
                if len(consts) == 1 and consts[0].fixed:
                    continue
                k = (attr, frozenset(s.id for s in lst))
                if k not in made:
                    merged = {}
                    for c in consts:
                        for s, v in c.spec:
                            kk = sem.key(s)
                            merged[kk] = (s, sem.i32(merged.get(kk, (s, 0))[1] + v))
                    made[k] = Node(self, "const", [sv for sv in merged.values() if sv[1]], label="const")
                setattr(n, attr, [s for s in lst if s.kind != "const"] + [made[k]])
        # Drop the original (now unused) constants.
        used = {s.id for n in self.nodes for s in n.red + n.green}
        self.nodes = [n for n in self.nodes if n.kind != "const" or n.id in used]

    def _dedup_outputs(self):
        """A node's output connector of one color can join only one network."""
        while True:
            seen = {}  # (source id, color) -> net key
            clash = None
            for n in self.nodes:
                for color in ("red", "green"):
                    srcs = getattr(n, color)
                    if not srcs:
                        continue
                    k = frozenset(s.id for s in srcs)
                    for s in srcs:
                        prev = seen.setdefault((s.id, color), k)
                        if prev != k:
                            clash = (n, color, s)
                            break
                    if clash:
                        break
                if clash:
                    break
            if not clash:
                return
            n, color, s = clash
            if s.kind in ("clock", "ext"):
                raise ValueError(f"{s.kind} {s.label} can feed only one network per color")
            dup = Node(self, s.kind, s.spec, s.red, s.green, time=s.time, label=s.label + " dup")
            dup.orig = s
            lst = getattr(n, color)
            setattr(n, color, [dup if x is s else x for x in lst])
            # Every consumer that reads exactly this source set on this color shares the dup.
            k = frozenset(x.id for x in lst)
            for m in self.nodes:
                ml = getattr(m, color)
                if ml and frozenset(x.id for x in ml) == k and m is not n:
                    setattr(m, color, [dup if x is s else x for x in ml])

    def _nets(self):
        """Networks: (color, frozenset of source ids) -> {'src': [nodes], 'dst': [nodes]}."""
        nets = {}
        for n in self.nodes:
            for color in ("red", "green"):
                srcs = getattr(n, color)
                if srcs:
                    k = (color, frozenset(s.id for s in srcs))
                    net = nets.setdefault(k, {"color": color, "src": srcs, "dst": []})
                    net["dst"].append(n)
        return nets


class Collect:
    """Several nodes whose outputs are always used together (e.g. bit-slice deciders)."""

    _ids = iter(range(-1, -10**9, -1))

    def __init__(self, nodes):
        self.nodes = nodes
        self.id = next(Collect._ids)
        ts = {n.time for n in nodes if n.time is not None}   # constants (no time) may join a wire
        assert len(ts) == 1
        self.time = ts.pop()
        self.kind = "collect"
        self.label = nodes[0].label


def _flat(lst):
    return [m for x in lst for m in (x.nodes if isinstance(x, Collect) else [x])]


def _l(x):
    return list(x) if isinstance(x, (list, tuple)) else [x]


def _dedup(lst):
    out = []
    for x in lst:
        if x not in out:
            out.append(x)
    return out


# ---------------------------------------------------------------------------------------------
# Placement

def footprint(n, pos=None):
    x, y = pos or n.pos
    if n.kind == "ext":
        w, h = n.spec["size"]
        return (x, y, x + w, y + h)
    w = 1 if n.kind == "const" else 2
    return (x, y, x + w, y + 1)


def tiles(n, pos=None):
    """Tiles a node occupies: its footprint, plus the clock's constant to its right."""
    x0, y0, x1, y1 = footprint(n, pos)
    t = {(x, y) for x in range(x0, x1) for y in range(y0, y1)}
    if n.kind == "clock":
        t.add((x1, y0))
    return t


def gap(a, b):
    gx = max(0, b[0] - a[2], a[0] - b[2])
    gy = max(0, b[1] - a[3], a[1] - b[3])
    return (gx * gx + gy * gy) ** 0.5


def _place(g, nets, blocked, region, x_right):
    """One tile column per time step, right to left from x_right (latest step) so every network
    joins neighboring columns; the free column between two steps holds constants and relay
    poles. Within a step, nodes sit at the mean row of their consumers, pushed apart as needed."""
    occupied = set()

    def take(n):
        occupied.update(tiles(n))

    def free(n, pos):
        return all(region(x, y) and not blocked(x, y) and (x, y) not in occupied for x, y in tiles(n, pos))

    for n in g.nodes:
        if n.fixed:
            take(n)
    # Copies of a placed node go right next to it.
    for n in g.nodes:
        o = getattr(n, "orig", None)
        if not n.fixed and o is not None and o.fixed:
            x0, y0 = o.pos
            spot = next((x0 + dx, y0 + dy) for r in range(1, 60) for dx in range(-2 * r, 2 * r + 1) for dy in range(-r, r + 1)
                        if max(abs(dx) // 2, abs(dy)) == r and free(n, (x0 + dx, y0 + dy)))
            n.fixed, n.pos = True, spot
            take(n)
    consumers = {n.id: [] for n in g.nodes}
    for net in nets.values():
        for s in net["src"]:
            consumers[s.id] += net["dst"]
    timed = [n for n in g.nodes if not n.fixed and n.kind != "const"]
    tmax = max(n.time for n in timed if n.time is not None)
    for n in timed:
        if n.time is None:
            raise ValueError(f"{n} has no time (built only from constants)")
    for t in range(tmax, -1, -1):
        col = [n for n in timed if n.time == t]
        x = x_right - 3 * (tmax - t)
        want = {}
        for n in col:
            ys = [c.pos[1] for c in consumers[n.id] if c.pos is not None]
            if not ys:
                raise ValueError(f"{n} has no placed consumer")
            want[n.id] = sum(ys) / len(ys)
        # One row per node, in the order of their wanted rows, as close to them as possible: the
        # stack centers on a cluster instead of piling up below it (isotonic regression of
        # want_i - i, pool adjacent violators), then each node takes the nearest free tile.
        order = sorted(col, key=lambda n: want[n.id])
        blocks = []   # [mean, count]
        for i, n in enumerate(order):
            blocks.append([want[n.id] - i, 1])
            while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
                m2, c2 = blocks.pop()
                m1, c1 = blocks.pop()
                blocks.append([(m1 * c1 + m2 * c2) / (c1 + c2), c1 + c2])
        ys = [m + i for i, m in enumerate(m for m, c in blocks for _ in range(c))]
        for n, yw in zip(order, ys):
            y0 = round(yw)
            y = next(y0 + d for a in range(0, 1000) for d in ((a, -a) if a else (0,)) if free(n, (x, y0 + d)))
            n.pos = (x, y)
            take(n)
    for n in g.nodes:
        if n.kind == "const" and not n.fixed:
            c = consumers[n.id][0]
            cx, cy = c.pos
            for dy in sorted(range(-8, 9), key=abs):
                for x in (cx - 1, cx - 2, cx + 2):
                    if free(n, (x, cy + dy)):
                        n.pos = (x, cy + dy)
                        break
                if n.pos:
                    break
            if n.pos is None:
                raise ValueError(f"no room for a constant next to {c}")
            take(n)


# ---------------------------------------------------------------------------------------------
# Emission

RED_IN, GREEN_IN, RED_OUT, GREEN_OUT = 1, 2, 3, 4


def _emit(g, b):
    """Returns {node id: (entity number for inputs, entity number for outputs)}."""
    ent = {}
    for n in g.nodes:
        x, y = n.pos
        if n.kind == "const":
            e = b.constant(x, y, n.spec)
            ent[n.id] = (e, e)
        elif n.kind == "ext":
            ent[n.id] = (n.spec["entity"], n.spec["entity"])
        elif n.kind == "clock":
            s = n.spec["signal"]
            c = b.constant(x + 2, y, [(s, 1)])
            d = b.decider(x, y, [sem.cond(s, "<", 1 << 30, RED)], [sem.out(s, networks=RED)], direction=4)
            b.wire(c, 1, d, RED_IN)
            b.wire(d, RED_OUT, d, RED_IN)
            ent[n.id] = (d, d)
        elif n.kind == "arith":
            e = b._add("arithmetic-combinator", x + 1, y + 0.5, {"arithmetic_conditions": n.spec}, direction=4)
            ent[n.id] = (e, e)
        elif n.kind == "decider":
            e = b._add("decider-combinator", x + 1, y + 0.5, {"decider_conditions": n.spec}, direction=4)
            ent[n.id] = (e, e)
        elif n.kind == "selector":
            e = b._add("selector-combinator", x + 1, y + 0.5, dict(n.spec), direction=4)
            ent[n.id] = (e, e)
        else:
            raise ValueError(n.kind)
    return ent


def _center_dist(a, b):
    return (((a[0] + a[2]) - (b[0] + b[2])) ** 2 + ((a[1] + a[3]) - (b[1] + b[3])) ** 2) ** 0.5 / 2


def _relay(b, a_fp, a_end, c_fp, c_end, conn, usable, taken, a_pole=False, c_pole=False):
    """Wire a to c, adding medium poles when they're out of reach. Pole to entity is measured
    footprint to footprint, pole to pole center to center (both reach 9). a_pole / c_pole: the
    ends are poles themselves. Greedy (each pole as close to c as it can get) when that works,
    else an A* search around whatever is in the way."""
    def reaches(fp1, pole1, fp2, pole2):
        return (_center_dist(fp1, fp2) if pole1 and pole2 else gap(fp1, fp2)) <= REACH

    tx, ty = (c_fp[0] + c_fp[2]) / 2, (c_fp[1] + c_fp[3]) / 2
    path, prev_fp, prev_pole = [], a_fp, a_pole
    while not reaches(prev_fp, prev_pole, c_fp, c_pole):
        here = ((prev_fp[0] + prev_fp[2]) / 2 - tx) ** 2 + ((prev_fp[1] + prev_fp[3]) / 2 - ty) ** 2
        best = None
        x0, y0 = int(prev_fp[0]), int(prev_fp[1])
        for x in range(x0 - 10, x0 + 12):
            for y in range(y0 - 10, y0 + 12):
                if (x, y) in taken or not usable(x, y):
                    continue
                fp = (x, y, x + 1, y + 1)
                if not reaches(prev_fp, prev_pole, fp, True):
                    continue
                d = (x + 0.5 - tx) ** 2 + (y + 0.5 - ty) ** 2
                if best is None or d < best[0]:
                    best = (d, x, y)
        if best is None or best[0] >= here:
            path = _relay_search(a_fp, a_pole, c_fp, c_pole, usable, taken, reaches)
            break
        _, x, y = best
        path.append((x, y))
        prev_fp, prev_pole = (x, y, x + 1, y + 1), True
    prev_end = a_end
    for x, y in path:
        pole = b.pole(x, y)
        taken.add((x, y))
        b.wire(prev_end[0], prev_end[1], pole, conn)
        prev_end = (pole, conn)
    b.wire(prev_end[0], prev_end[1], c_end[0], c_end[1])


def _relay_search(a_fp, a_pole, c_fp, c_pole, usable, taken, reaches):
    """A* over pole positions (hops on a 3-tile lattice plus free tiles next to the ends)."""
    import heapq
    tx, ty = (c_fp[0] + c_fp[2]) / 2, (c_fp[1] + c_fp[3]) / 2
    free = lambda x, y: (x, y) not in taken and usable(x, y)  # noqa: E731
    hops = [(dx, dy) for dx in range(-9, 10) for dy in range(-9, 10) if dx * dx + dy * dy <= 81 and (dx or dy)]
    h = lambda x, y: ((x + 0.5 - tx) ** 2 + (y + 0.5 - ty) ** 2) ** 0.5 / 9  # noqa: E731
    start = []
    x0, y0 = int(a_fp[0]), int(a_fp[1])
    for x in range(x0 - 10, x0 + 12):
        for y in range(y0 - 10, y0 + 12):
            if free(x, y) and reaches(a_fp, a_pole, (x, y, x + 1, y + 1), True):
                start.append((x, y))
    prev = {p: None for p in start}
    cost = {p: 1 for p in start}
    heap = [(1 + h(*p), p) for p in start]
    heapq.heapify(heap)
    seen = 0
    while heap:
        _, p = heapq.heappop(heap)
        seen += 1
        if seen > 200000:
            break
        if reaches((p[0], p[1], p[0] + 1, p[1] + 1), True, c_fp, c_pole):
            path = []
            while p:
                path.append(p)
                p = prev[p]
            return path[::-1]
        for dx, dy in hops:
            q = (p[0] + dx, p[1] + dy)
            if q in cost or not free(*q):
                continue
            cost[q] = cost[p] + 1
            prev[q] = p
            heapq.heappush(heap, (cost[q] + h(*q), q))
    raise ValueError(f"can't route a wire from {a_fp} to {c_fp}")


def _mst(fps, boxed=(), joined=()):
    """Minimum spanning tree over footprint gaps: [(gap, i, j)], each edge joining node i to the
    tree through j. Prim's for small nets; for big ones Kruskal over the pairs in neighboring grid
    cells, with coarser cells until everything is joined. Edges longer than REACH touching a boxed
    member (no room for a relay pole next to it) cost extra, so they're used only as a last resort."""
    n = len(fps)
    joined = set(joined)   # members already connected to each other (free edges, not wired)

    def cost(i, j):
        if i in joined and j in joined:
            return 0
        d = gap(fps[i], fps[j])
        return d + 1e6 if d > REACH and (i in boxed or j in boxed) else d
    if n <= 120:
        edges = []
        best = {i: (cost(0, i), 0) for i in range(1, n)}
        while best:
            i = min(best, key=lambda i: best[i][0])
            _, j = best.pop(i)
            edges.append((gap(fps[j], fps[i]), j, i))
            for k in best:
                d2 = cost(i, k)
                if d2 < best[k][0]:
                    best[k] = (d2, i)
        return edges
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    edges, comps = [], n
    for size in (3, 10, 30, 100, 300, 1000, 10 ** 6):
        cells = {}
        for i, f in enumerate(fps):
            cells.setdefault((int(f[0] // size), int(f[1] // size)), []).append(i)
        cand = []
        if joined and size == 3:   # join them first
            js = sorted(joined)
            for a in js[1:]:
                ra, rb = find(a), find(js[0])
                if ra != rb:
                    parent[ra] = rb
                    edges.append((0, js[0], a))
                    comps -= 1
        for (cx, cy), idx in cells.items():
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    other = cells.get((cx + dx, cy + dy))
                    if not other:
                        continue
                    for i in idx:
                        ri = find(i)
                        for j in other:
                            if i < j and find(j) != ri:
                                cand.append((cost(i, j), i, j))
        if size < 10 ** 6:   # boxed long edges only once nothing else is left
            cand = [c for c in cand if c[0] < 1e6]
        cand.sort()
        for d, i, j in cand:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj
                edges.append((gap(fps[i], fps[j]), i, j))
                comps -= 1
        if comps == 1:
            break
    return edges


def _wire(g, nets, b, ent, usable):
    g.relay_stats = {}
    taken = b.occupied()
    relays = 0
    for net in nets.values():
        color = net["color"]
        members = []  # (node, connector)
        for s in net["src"]:
            if s.kind == "const":
                members.append((s, 1 if color == "red" else 2))
            elif s.kind == "ext":
                members.append((s, s.spec["connectors"][0 if color == "red" else 1]))
            else:
                members.append((s, RED_OUT if color == "red" else GREEN_OUT))
        for d in net["dst"]:
            members.append((d, RED_IN if color == "red" else GREEN_IN))
        fps = [footprint(n) for n, _ in members]
        # An ext source with taps (other entities on its network, e.g. a row of substations
        # chained to it): any of them may join the network.
        joined = []
        for s in net["src"]:
            if s.kind == "ext" and s.spec.get("taps"):
                joined.append(next(i for i, (m, _) in enumerate(members) if m is s))
                for ent_id, pos, size in s.spec["taps"]:
                    t = Node.__new__(Node)
                    t.kind, t.spec, t.pos, t.id, t.label = "ext", {"entity": ent_id, "size": size, "connectors": s.spec["connectors"]}, pos, None, "tap"
                    joined.append(len(members))
                    members.append((t, s.spec["connectors"][0 if color == "red" else 1]))
                    fps.append(footprint(t))
        edges = [e for e in _mst(fps, joined=joined) if not (e[1] in joined and e[2] in joined)]
        # A long edge needs a relay pole within reach of each end. Members boxed in (inside a
        # packed block) can't take one: if the tree uses such an edge, rebuild it with those
        # members joined only within reach, so the net enters the block where there is room.
        if any(d > REACH for d, _, _ in edges):
            free = lambda fp: any((x, y) not in taken and usable(x, y) and gap(fp, (x, y, x + 1, y + 1)) <= REACH  # noqa: E731
                                  for x in range(fp[0] - 10, fp[2] + 10) for y in range(fp[1] - 10, fp[3] + 10))
            boxed, cache = set(), {}
            while True:
                new = {k for d, j, i in edges if d > REACH for k in (j, i)
                       if k not in boxed and not cache.setdefault(k, free(fps[k]))}
                if not new:
                    break
                boxed |= new
                edges = [e for e in _mst(fps, boxed, joined) if not (e[1] in joined and e[2] in joined)]
        for dist, j, i in edges:
            (a, ca), (c, cc) = members[j], members[i]
            ea = ent[a.id] if a.id is not None else (a.spec["entity"],) * 2
            ec = ent[c.id] if c.id is not None else (c.spec["entity"],) * 2
            a_end = (ea[1 if ca in (RED_OUT, GREEN_OUT) else 0], ca)
            c_end = (ec[1 if cc in (RED_OUT, GREEN_OUT) else 0], cc)
            if dist > REACH:
                n0 = len(b.entities)
                is_pole = lambda n: n.kind == "ext" and b.is_pole(n.spec["entity"])  # noqa: E731
                _relay(b, fps[j], a_end, fps[i], c_end, 1 if color == "red" else 2, usable, taken,
                       is_pole(a), is_pole(c))
                relays += len(b.entities) - n0
                k = f"{net['color']} {net['src'][0].label}"
                g.relay_stats[k] = g.relay_stats.get(k, 0) + len(b.entities) - n0
            else:
                b.wire(*a_end, *c_end)
    g.relays = relays
