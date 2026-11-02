"""Tiny blueprint builder for conformance cases (Factorio 2.1 blueprint JSON).

Wire connector ids: 1/2 = red/green input (or the only connector on lamps, poles and constant
combinators), 3/4 = red/green combinator output, 5 = copper.
"""

import base64
import json
import pathlib
import zlib

RED_IN, GREEN_IN, RED_OUT, GREEN_OUT, COPPER = 1, 2, 3, 4, 5
INT_MAX, INT_MIN = 2**31 - 1, -(2**31)

# Supply area and wire reach of each pole at every quality, measured in a Space Age game
# (harness/run.py --poles). Quality above normal needs the quality mod (Space Age).
POLES = json.loads((pathlib.Path(__file__).resolve().parent / "pole_quality.json").read_text())
QUALITIES = sorted(POLES["substation"], key=lambda q: POLES["substation"][q]["level"])
# Generated blueprints for players use legendary substations: a 28x28 supply area instead of
# 18x18, so the lamp screen loses 0.5% of its pixels to substations instead of 1.2%. Game checks
# without the DLC (conformance, golden dumps) must pass "normal".
DEFAULT_SUBSTATION_QUALITY = "legendary"


class SubstationGrid:
    """Substation layout for one quality. The supply area of a substation whose top-left tile is
    (x, y) covers tiles x - (d - 1) .. x + d (d = supply area distance; 2d tiles square), so a
    lattice with period 2d and offset d - 1 covers the plane with no overlap, starting at tile 0."""

    def __init__(self, quality="normal"):
        if quality not in POLES["substation"]:
            raise ValueError(f"unknown quality {quality!r}; one of {', '.join(QUALITIES)}")
        p = POLES["substation"][quality]
        self.quality = quality
        self.d = int(p["supply_area_distance"])
        assert self.d == p["supply_area_distance"], "substation supply area is a whole number of tiles"
        self.period, self.offset = 2 * self.d, self.d - 1
        self.reach = p["max_wire_distance"]

    def covers(self, sx, sy, x, y):
        """Whether the substation with top-left tile (sx, sy) powers tile (x, y)."""
        return sx - self.d < x <= sx + self.d and sy - self.d < y <= sy + self.d

    def hole(self, x, y):
        """Whether tile (x, y) is under a substation on the infinite lattice."""
        return (x - self.offset) % self.period in (0, 1) and (y - self.offset) % self.period in (0, 1)

    def screen(self, w, h):
        """Top-left tiles of substations inside a w x h screen at (0, 0): the lattice, with the last
        row and column pulled in so they stay inside the screen."""
        xs = sorted({min(self.offset + self.period * i, w - 2) for i in range((w + self.period - 1) // self.period)})
        ys = sorted({min(self.offset + self.period * j, h - 2) for j in range((h + self.period - 1) // self.period)})
        return [(x, y) for y in ys for x in xs]


def quality_arg(ap):
    """Add --substation-quality to an argparse parser."""
    ap.add_argument("--substation-quality", default=DEFAULT_SUBSTATION_QUALITY, choices=QUALITIES,
                    help=f"quality of the substations (default {DEFAULT_SUBSTATION_QUALITY}; normal for "
                         "game checks without the DLC)")


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
    def __init__(self, label, substation_quality="normal"):
        self.label = label
        self.grid = SubstationGrid(substation_quality)
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

    def chest(self, tx, ty, kind="steel-chest"):
        """A container. Its contents are on any circuit wire attached to connector 1 (red) or 2."""
        return self._add(kind, tx + 0.5, ty + 0.5)

    def display(self, tx, ty, icon, always_show=True):
        """A display panel showing a virtual signal's icon (no circuit behavior, no power)."""
        return self._add("display-panel", tx + 0.5, ty + 0.5, icon=sig(icon), always_show=always_show)

    def substation(self, tx, ty):
        """A substation of the blueprint's substation quality, top-left tile (tx, ty)."""
        q = self.grid.quality
        return self._add("substation", tx + 1, ty + 1, **({"quality": q} if q != "normal" else {}))

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

    UNPOWERED = ("wooden-chest", "iron-chest", "steel-chest", "display-panel")

    def unpowered_tiles(self):
        """Tiles of entities that need no electricity (containers, display panels)."""
        return {(int(e["position"]["x"]), int(e["position"]["y"])) for e in self.entities
                if e["name"] in self.UNPOWERED}

    def power(self):
        """Substations until every entity tile is inside some substation's supply area (18x18 at
        normal quality, 28x28 at legendary; see SubstationGrid).

        Greedy: take the first uncovered tile, put a substation at the free spot nearest to it,
        repeat. Call link_power() afterwards so the substations form one network.
        """
        grid = self.grid
        occ = self.occupied()
        targets = occ - self.unpowered_tiles()
        for e in self.entities:  # substations already placed (e.g. a grid inside a lamp screen)
            if e["name"] == "substation":
                x, y = int(e["position"]["x"]) - 1, int(e["position"]["y"]) - 1
                g = SubstationGrid(e.get("quality", "normal"))
                targets = {(a, b) for a, b in targets if not g.covers(x, y, a, b)}
        free = lambda x, y: not {(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)} & occ
        r = range(-(grid.d - 1), grid.d)
        spiral = sorted(((dx, dy) for dx in r for dy in r), key=lambda d: abs(d[0]) + abs(d[1]))
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
            targets = {(a, b) for a, b in targets if not grid.covers(x, y, a, b)}

    @staticmethod
    def reach(e):
        """Circuit wire reach of an entity (9 for anything that isn't a pole), by its quality."""
        if e["name"] in POLES:
            return POLES[e["name"]][e.get("quality", "normal")]["max_circuit_wire_distance"]
        return 9

    def check_reach(self):
        """Raise if any circuit wire is longer than the game allows (it silently drops those):
        the footprint gap (center distance between two poles) must be <= the shorter reach of
        the two ends (9 for combinators and lamps)."""
        rects = {}
        for e in self.entities:
            w, h = self.SIZES.get(e["name"], (1, 1))
            if e.get("direction", 0) in (4, 12):
                w, h = h, w
            x0, y0 = e["position"]["x"] - w / 2, e["position"]["y"] - h / 2
            rects[e["entity_number"]] = (x0, y0, x0 + w, y0 + h, self.reach(e))
        for a, ca, c, cc in self.wires:
            if ca == COPPER:
                continue
            ra, rc = rects[a], rects[c]
            if self.is_pole(a) and self.is_pole(c):   # pole to pole: center to center
                gx = (ra[0] + ra[2] - rc[0] - rc[2]) / 2
                gy = (ra[1] + ra[3] - rc[1] - rc[3]) / 2
            else:                                      # otherwise: footprint gap
                gx = max(0, rc[0] - ra[2], ra[0] - rc[2])
                gy = max(0, rc[1] - ra[3], ra[1] - rc[3])
            if (gx * gx + gy * gy) ** 0.5 > min(ra[4], rc[4]) + 1e-9:
                raise ValueError(f"wire #{a} -> #{c} is {(gx * gx + gy * gy) ** 0.5:.2f} tiles, over reach")

    def is_pole(self, n):
        return self.entities[n - 1]["name"] in POLES

    def link_power(self):
        """Copper-wire the substations into one electric network (a minimum spanning tree over
        center distances; reach is the shorter copper reach of the two ends). Blueprints only
        carry the wires they list, so without this every substation is its own island when the
        user pastes the blueprint."""
        subs = [e for e in self.entities if e["name"] == "substation"]
        if not subs:
            return
        cu = {q: v["max_wire_distance"] for q, v in POLES["substation"].items()}
        reach = min(cu[e.get("quality", "normal")] for e in subs)
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
