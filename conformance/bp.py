"""Tiny blueprint builder for conformance cases (Factorio 2.1 blueprint JSON).

Wire connector ids: 1/2 = red/green input (or the only connector on lamps, poles and constant
combinators), 3/4 = red/green combinator output, 5 = copper.
"""

import base64
import json
import zlib

RED_IN, GREEN_IN, RED_OUT, GREEN_OUT, COPPER = 1, 2, 3, 4, 5
INT_MAX, INT_MIN = 2**31 - 1, -(2**31)

LETTERS = [f"signal-{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"] + [f"signal-{d}" for d in range(10)]


def sig(name, typ="virtual"):
    if typ == "item":
        return {"name": name}
    return {"type": typ, "name": name}


EACH = sig("signal-each")
EVERYTHING = sig("signal-everything")
ANYTHING = sig("signal-anything")


def nets(red=True, green=True):
    return {"red": red, "green": green}


RED, GREEN = nets(True, False), nets(False, True)


class Blueprint:
    def __init__(self, label):
        self.label = label
        self.entities = []
        self.wires = []

    def _add(self, name, x, y, cb=None, **extra):
        n = len(self.entities) + 1
        e = {"entity_number": n, "name": name, "position": {"x": x, "y": y}}
        if cb is not None:
            e["control_behavior"] = cb
        e.update(extra)
        self.entities.append(e)
        return n

    # Placement uses integer tile coordinates (tx, ty) of the top-left tile.
    def constant(self, tx, ty, signals, enabled=True):
        """signals: list of (SignalID dict, count) or dict name->count (virtual signals)."""
        if isinstance(signals, dict):
            signals = [(sig(k), v) for k, v in signals.items()]
        filters = []
        for i, (s, count) in enumerate(signals, 1):
            f = {"index": i, **s, "quality": "normal", "comparator": "=", "count": count}
            filters.append(f)
        cb = {"sections": {"sections": [{"index": 1, "filters": filters}]}}
        if not enabled:
            cb["is_on"] = False
        return self._add("constant-combinator", tx + 0.5, ty + 0.5, cb)

    def _comb_pos(self, tx, ty, direction):
        """1x2 combinator footprint at tile (tx, ty); facing east/west makes it 2x1."""
        if direction in (4, 12):
            return {"x": tx + 1, "y": ty + 0.5}, {"direction": direction}
        return {"x": tx + 0.5, "y": ty + 1}, ({"direction": direction} if direction else {})

    def arithmetic(self, tx, ty, first, op, second, out, first_nets=None, second_nets=None, direction=0):
        """first/second: SignalID dict or int constant."""
        a = {"operation": op, "output_signal": out}
        if isinstance(first, int):
            a["first_constant"] = first
        else:
            a["first_signal"] = first
            if first_nets:
                a["first_signal_networks"] = first_nets
        if isinstance(second, int):
            a["second_constant"] = second
        else:
            a["second_signal"] = second
            if second_nets:
                a["second_signal_networks"] = second_nets
        pos, extra = self._comb_pos(tx, ty, direction)
        return self._add("arithmetic-combinator", pos["x"], pos["y"], {"arithmetic_conditions": a}, **extra)

    def decider(self, tx, ty, conditions, outputs, else_outputs=None, direction=0):
        d = {"conditions": conditions, "outputs": outputs}
        if else_outputs:
            d["else_outputs"] = else_outputs
        pos, extra = self._comb_pos(tx, ty, direction)
        return self._add("decider-combinator", pos["x"], pos["y"], {"decider_conditions": d}, **extra)

    def selector(self, tx, ty, **params):
        return self._add("selector-combinator", tx + 0.5, ty + 1, dict(params))

    def lamp(self, tx, ty, cb=None, always_on=False):
        """always_on: lit in daylight too (otherwise lamps only light up at night)."""
        return self._add("small-lamp", tx + 0.5, ty + 0.5, cb, **({"always_on": True} if always_on else {}))

    def pole(self, tx, ty):
        return self._add("medium-electric-pole", tx + 0.5, ty + 0.5)

    def substation(self, tx, ty):
        return self._add("substation", tx + 1, ty + 1)

    def wire(self, a, ca, b, cb):
        self.wires.append([a, ca, b, cb])

    def chain(self, ids, connector):
        for a, b in zip(ids, ids[1:]):
            self.wire(a, connector, b, connector)

    SIZES = {"arithmetic-combinator": (1, 2), "decider-combinator": (1, 2), "selector-combinator": (1, 2),
             "big-electric-pole": (2, 2), "substation": (2, 2)}

    def occupied(self):
        tiles = set()
        for e in self.entities:
            w, h = self.SIZES.get(e["name"], (1, 1))
            if e.get("direction", 0) in (4, 12):
                w, h = h, w
            x0 = int(e["position"]["x"] - w / 2)
            y0 = int(e["position"]["y"] - h / 2)
            tiles |= {(x0 + i, y0 + j) for i in range(w) for j in range(h)}
        return tiles

    def power(self):
        """Substations until every entity tile is inside some substation's 18x18 supply area.

        Greedy: take the first uncovered tile, put a substation at the free spot nearest to it,
        repeat. The harness powers each substation on its own, so no copper links are needed.
        """
        occ = self.occupied()
        targets = set(occ)
        for e in self.entities:  # substations already placed (e.g. a grid inside a lamp screen)
            if e["name"] == "substation":
                x, y = int(e["position"]["x"]) - 1, int(e["position"]["y"]) - 1
                targets = {(a, b) for a, b in targets if not (x - 8 <= a <= x + 9 and y - 8 <= b <= y + 9)}
        free = lambda x, y: not {(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)} & occ
        spiral = sorted(((dx, dy) for dx in range(-8, 9) for dy in range(-8, 9)), key=lambda d: abs(d[0]) + abs(d[1]))
        while targets:
            tx, ty = min(targets, key=lambda t: (t[1], t[0]))
            for dx, dy in spiral:
                x, y = tx + dx, ty + dy
                if free(x, y):
                    break
            else:
                raise ValueError(f"no room for a substation within reach of tile {tx},{ty}")
            self.substation(x, y)
            occ |= {(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)}
            # Supply area: 18x18 centered on (x + 1, y + 1), i.e. tiles x-8 .. x+9.
            targets = {(a, b) for a, b in targets if not (x - 8 <= a <= x + 9 and y - 8 <= b <= y + 9)}

    def link_power(self, reach=18):
        """Copper-wire the substations into one electric network (a minimum spanning tree over
        center distances). Blueprints only carry the wires they list, so without this every
        substation is its own island when the user pastes the blueprint."""
        subs = [e for e in self.entities if e["name"] == "substation"]
        if not subs:
            return
        pos = lambda e: (e["position"]["x"], e["position"]["y"])
        d2 = lambda a, b: (pos(a)[0] - pos(b)[0]) ** 2 + (pos(a)[1] - pos(b)[1]) ** 2
        best = {id(e): (d2(subs[0], e), subs[0]) for e in subs[1:]}
        left = {id(e): e for e in subs[1:]}
        while left:
            k = min(left, key=lambda k: best[k][0])
            e = left.pop(k)
            dist, near = best[k]
            if dist > reach * reach:
                raise ValueError(f"substation at {pos(e)} is {dist ** 0.5:.1f} tiles from the nearest one (max {reach})")
            self.wire(near["entity_number"], COPPER, e["entity_number"], COPPER)
            for k2, e2 in left.items():
                if d2(e, e2) < best[k2][0]:
                    best[k2] = (d2(e, e2), e)

    def to_json(self):
        return {"blueprint": {"item": "blueprint", "label": self.label, "entities": self.entities,
                              "wires": self.wires, "version": 562954249895938}}


def cond(first, comparator, second, first_nets=None, second_nets=None, compare_type=None):
    c = {"comparator": comparator}
    if first is not None:
        c["first_signal"] = first
    if first_nets:
        c["first_signal_networks"] = first_nets
    if isinstance(second, int):
        c["constant"] = second
    elif second is not None:
        c["second_signal"] = second
        if second_nets:
            c["second_signal_networks"] = second_nets
    if compare_type:
        c["compare_type"] = compare_type
    return c


def out(signal, copy=True, constant=None, networks=None):
    o = {"signal": signal}
    if not copy:
        o["copy_count_from_input"] = False
        if constant is not None:
            o["constant"] = constant
    if networks:
        o["networks"] = networks
    return o


def encode(bp_json):
    raw = json.dumps(bp_json, separators=(",", ":")).encode()
    return "0" + base64.b64encode(zlib.compress(raw, 9)).decode()


def decode(s):
    return json.loads(zlib.decompress(base64.b64decode(s.strip()[1:])))
