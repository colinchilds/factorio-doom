"""Thing contact: items are picked up when you walk over them. Live monsters are solid
(game.player_graph adds their boxes to the wall corners); dead ones (combat.py) aren't.

Everything here reads the state bus (REG's red output, game.py) and is one tick deep, like the
weapon logic:

    GONE_i   item i picked up (bus, held by one decider copying them all): the raycaster hides a
             gone thing by pushing its depth behind the camera (things.column_stage `hide`).
    take_i   item i under the player (|X - x_i|, |Y - y_i| < PICKUP), not gone, the player alive
             and the stat below its cap -> GONE_i = 1 (bus). Doom leaves an item when you're full.
    effect   the same condition and the stat's value -> the increment into REG: min(amount,
             cap - value), as bit-sliced constant tables over value ranges (a decider can only copy
             or output constants, and the clamp needs the value).

`step` is the same rules in plain Python, for game.py's check.
"""
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from compiler.bp import sig  # noqa: E402
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import RED  # noqa: E402

FP = 256
GONE = [sig(f"signal-{i}") for i in range(8)]   # per thing index (only items use theirs)
PICKUP = 80          # 1/FP tiles per axis, centre to centre: Doom's 16 + 20 units, a bit more
MONSTERS = {"TROO", "POSS"}
# Sprite type -> (stat, amount, cap). Doom's numbers; armor "amount" sets it to the cap.
ITEMS = {
    "MEDI": ("health", 25, 100), "STIM": ("health", 10, 100),
    "ARM1": ("armor", 100, 100), "ARM2": ("armor", 200, 200),
    "CLIP": ("bullets", 10, 200), "AMMO": ("bullets", 50, 200),
    "SHEL": ("shells", 4, 50), "SBOX": ("shells", 20, 50),
    "ROCK": ("rockets", 1, 50), "CELL": ("cells", 20, 300),
}


def kind(name):
    t = name[:4]
    return "item" if t in ITEMS else "monster" if t in MONSTERS else "decoration"


def items(things):
    return [i for i, (_, _, n) in enumerate(things) if kind(n) == "item"]


def monsters(things):
    return [i for i, (_, _, n) in enumerate(things) if kind(n) == "monster"]


def _box(x, y, spawn, r):
    """(X, Y) within r of the thing at tile (x, y), on REG's offsets from spawn."""
    cx, cy = int(x * FP) - spawn[0], int(y * FP) - spawn[1]
    return [("SX", ">", cx - r), ("SX", "<", cx + r), ("SY", ">", cy - r), ("SY", "<", cy + r)]


def pickup_table(amount, cap):
    """[(lo, hi, increment)] over the stat's value (None = unbounded), below the cap."""
    return [(None, cap - amount, amount)] + [(v, v, cap - v) for v in range(max(0, cap - amount + 1), cap)]


class Logic:
    """The deciders. sigs: {"SX", "SY", "health", "armor", ammo names...: signal}; start: the
    stats' new-game values (REG holds offsets from them)."""

    def __init__(self, things, sigs, start, spawn):
        self.things, self.sigs, self.start, self.spawn = things, sigs, start, spawn

    def _c(self, s, comp, v, ct):
        return sem.cond(self.sigs[s] if isinstance(s, str) else s, comp, v, RED, ct)

    def _clause(self, terms, first):
        return [self._c(s, comp, v, (None if first else "or") if j == 0 else "and") for j, (s, comp, v) in enumerate(terms)]

    def _rng(self, stat, lo, hi):
        """Range terms for the stat's value in [lo, hi] (None = open), on its REG offset."""
        o = self.start[stat]
        if lo is not None and lo == hi:
            return [(stat, "=", lo - o)]
        return ([(stat, ">=", lo - o)] if lo is not None else []) + ([(stat, "<=", hi - o)] if hi is not None else [])

    def alive(self):
        return [("health", ">", -self.start["health"])]

    def _bits(self, cases, out, sign, label):
        """cases: [(terms, stat, lo, hi, magnitude)]: terms and the stat's value in [lo, hi]. One
        decider per group of bits with the same cases (like lookup_scalar), each an OR of those
        cases (adjacent value ranges with the same terms merged), outputting sign * the weights."""
        groups = {}
        for b in range(31):
            ks = tuple(i for i, c in enumerate(cases) if (c[4] >> b) & 1)
            if ks:
                groups.setdefault(ks, []).append(b)
        res = []
        for ks, bs in groups.items():
            merged = []
            for i in ks:
                terms, stat, lo, hi, _ = cases[i]
                last = merged[-1] if merged else None
                if last and repr(last[0]) == repr(terms) and last[1] == stat and last[3] is not None \
                        and lo is not None and last[3] + 1 == lo:
                    last[3] = hi
                else:
                    merged.append([terms, stat, lo, hi])
            conds = []
            for terms, stat, lo, hi in merged:
                conds += self._clause(terms + self._rng(stat, lo, hi), not conds)
            spec = sem.decider_spec(conds, [sem.out(out, constant=sign * sum(1 << b for b in bs))])
            res.append((f"{label} bit{bs[0]}", spec, "reg"))
        return res

    def specs(self):
        """[(label, spec, "bus" | "reg")], like weapons.logic: "bus" ones write the bus, "reg" ones
        REG's input."""
        res = []
        its = items(self.things)
        if its:
            res.append(("gone hold", sem.decider_spec([sem.cond(GONE[its[0]], ">=", 0, RED)],
                                                      [sem.out(GONE[i], networks=RED) for i in its]), "bus"))
        for i in its:
            x, y, name = self.things[i]
            stat, amount, cap = ITEMS[name[:4]]
            near = _box(x, y, self.spawn, PICKUP) + [(GONE[i], "=", 0)] + self.alive()
            res.append((f"take {i} {name}", sem.decider_spec(self._clause(near + self._rng(stat, None, cap - 1), True),
                                                             [sem.out(GONE[i], constant=1)]), "bus"))
            cases = [(near, stat, lo, hi, m) for lo, hi, m in pickup_table(amount, cap)]
            res += self._bits(cases, self.sigs[stat], 1, f"pickup {i} {name}")
        return res


def step(things, spawn, start, bus):
    """The rules in plain Python. bus: {"SX", "SY", "health", "armor", ammo..., "gone": [..]} (REG's
    offsets, as on the bus). Returns (gone, increments {stat: delta})."""
    val = {k: bus[k] + start[k] for k in start}
    alive = val["health"] > 0
    gone = list(bus["gone"])
    inc = {}
    for i in items(things):
        x, y, name = things[i]
        stat, amount, cap = ITEMS[name[:4]]
        dx, dy = bus["SX"] + spawn[0] - int(x * FP), bus["SY"] + spawn[1] - int(y * FP)
        if abs(dx) < PICKUP and abs(dy) < PICKUP and not bus["gone"][i] and alive and val[stat] < cap:
            gone[i] = 1
            inc[stat] = inc.get(stat, 0) + min(amount, cap - val[stat])
    return gone, {k: v for k, v in inc.items() if v}
