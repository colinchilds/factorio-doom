"""Generate conformance case blueprints into conformance/cases/*.json.

Each case is small, runs from a clean start (all constant combinators switched on at once at t=0
by the harness), and records every combinator's output and every lamp's color per tick. The same
blueprint is simulated by fsim and the two traces are compared.

Run: python3 conformance/gen.py
"""

import json
import pathlib

from bp import (ANYTHING, EACH, EVERYTHING, GREEN, GREEN_IN, GREEN_OUT, INT_MAX, INT_MIN, LETTERS,
                RED, RED_IN, RED_OUT, Blueprint, cond, nets, out, sig)

CASES = {}


def case(ticks):
    def register(fn):
        CASES[fn.__name__] = (fn, ticks)
        return fn
    return register


# (red, green) value pairs, one lane each. A zero means the signal is absent on that wire.
PAIRS = [
    (7, 3), (-7, 3), (7, -3), (-7, -3), (5, 0), (0, 5), (INT_MAX, 1), (INT_MIN, -1), (INT_MIN, 1),
    (1, 31), (1, 32), (1, 33), (1, -1), (-1, 1), (-8, 1), (-8, 33), (2, 30), (3, 20), (-2, -1),
    (65536, 65536), (INT_MAX, INT_MAX), (100, 7), (-3, 3), (2, -2), (-1, 31), (INT_MIN, 31),
]
OPS = ["*", "/", "+", "-", "%", "^", "<<", ">>", "AND", "OR", "XOR"]


def pair_inputs(b, pairs=PAIRS):
    red = [(sig(LETTERS[i]), r) for i, (r, _) in enumerate(pairs) if r != 0]
    green = [(sig(LETTERS[i]), g) for i, (_, g) in enumerate(pairs) if g != 0]
    return b.constant(0, 0, red), b.constant(1, 0, green)


def wire_inputs(b, combs, red_src, green_src):
    b.wire(red_src, 1, combs[0], RED_IN)
    b.wire(green_src, 2, combs[0], GREEN_IN)
    b.chain(combs, RED_IN)
    b.chain(combs, GREEN_IN)


@case(ticks=6)
def arith_each_each(b):
    """Each(red) op Each(green) -> Each, for every operator, over edge-case lanes."""
    r, g = pair_inputs(b)
    combs = [b.arithmetic(i, 2, EACH, op, EACH, EACH, RED, GREEN) for i, op in enumerate(OPS)]
    wire_inputs(b, combs, r, g)


@case(ticks=6)
def arith_scalar(b):
    """Each op constant, Each op named signal, Each -> named sum (wrap), named op named."""
    r, g = pair_inputs(b)
    combs = [
        b.arithmetic(0, 2, EACH, "/", 2, EACH, RED),
        b.arithmetic(1, 2, EACH, "*", sig("signal-Z"), EACH, GREEN, RED),   # Z on red is absent -> 0
        b.arithmetic(2, 2, EACH, "*", sig("signal-A"), EACH, RED, GREEN),   # A(green)=3
        b.arithmetic(3, 2, EACH, "*", 1, sig("signal-dot"), RED),           # sum of all red lanes
        b.arithmetic(4, 2, sig("signal-A"), "*", sig("signal-B"), sig("signal-C"), RED, RED),
        b.arithmetic(5, 2, EACH, "+", EACH, EACH),                          # both wires, both sides
        b.arithmetic(6, 2, EACH, "*", 1, EACH),                             # red+green summed per lane
        b.arithmetic(7, 2, 100, "-", sig("signal-A"), sig("signal-check"), None, RED),
        b.arithmetic(8, 2, EACH, "-", EACH, sig("signal-dot"), RED, GREEN),  # each-each into a named sum
        b.arithmetic(9, 2, sig("signal-A"), "/", 0, sig("signal-check")),
    ]
    wire_inputs(b, combs, r, g)


@case(ticks=6)
def arith_const_each(b):
    """constant op Each (constant first operand): powers with big and negative exponents (2^n wraps
    to 0 from n = 32), shifts and divisions with the lane value as the second operand."""
    vals = [1, 5, 30, 31, 32, 33, 63, 64, 100, 640, 1000, 65536, -1, -32, INT_MIN, INT_MAX]
    r = b.constant(0, 0, [(sig(LETTERS[i]), v) for i, v in enumerate(vals)])
    g = b.constant(1, 0, [(sig(LETTERS[0]), 4)])
    combs = [b.arithmetic(i, 2, c, op, EACH, EACH, None, RED)
             for i, (c, op) in enumerate([(2, "^"), (3, "^"), (-2, "^"), (100, "-"), (1, "<<"), (-7, "/"), (7, "%")])]
    combs.append(b.arithmetic(len(combs), 2, 2, "^", EACH, EACH))   # both wires (A: 1 + 4)
    wire_inputs(b, combs, r, g)


@case(ticks=6)
def decider_each(b):
    """Per-lane conditions, per-lane else-outputs, named outputs under Each conditions."""
    r, g = pair_inputs(b)
    gt = [cond(EACH, ">", EACH, RED, GREEN)]
    combs = [
        b.decider(0, 2, gt, [out(EACH, networks=RED)], [out(EACH, copy=False, constant=7)]),
        b.decider(1, 2, gt, [out(sig("signal-check"), copy=False, constant=1)],
                  [out(sig("signal-dot"), copy=False, constant=1)]),
        b.decider(2, 2, gt, [out(sig("signal-A"), networks=RED)], [out(sig("signal-B"), networks=GREEN)]),
        b.decider(3, 2, [cond(EACH, ">", 0)], [out(EACH, copy=False, constant=1)]),
        b.decider(4, 2, [cond(EACH, "!=", 0, RED), cond(sig("signal-A"), ">", 0, GREEN, compare_type="and")],
                  [out(EACH, networks=GREEN)]),
        b.decider(5, 2, [cond(EACH, "<", sig("signal-A"), RED, GREEN)], [out(EACH)],
                  [out(EACH, copy=False, constant=-1)]),
        b.decider(6, 2, gt, [out(EACH, networks=RED), out(sig("signal-check"), copy=False, constant=2)]),
        b.decider(7, 2, gt, [out(EVERYTHING, networks=GREEN)]),
    ]
    wire_inputs(b, combs, r, g)


@case(ticks=6)
def decider_select(b):
    """Per-lane select: copy one wire or the other per lane (lane-wise min), and a condition on
    one wire that copies the other wire."""
    r, g = pair_inputs(b)
    combs = [
        b.decider(0, 2, [cond(EACH, "<", EACH, RED, GREEN)], [out(EACH, networks=RED)], [out(EACH, networks=GREEN)]),
        b.decider(1, 2, [cond(EACH, ">", 3, GREEN)], [out(EACH, networks=RED)], [out(EACH, copy=False, constant=-9)]),
        b.decider(2, 2, [cond(EACH, "=", sig("signal-A"), RED, GREEN)], [out(EACH, networks=GREEN)]),
    ]
    wire_inputs(b, combs, r, g)


@case(ticks=6)
def decider_ranges(b):
    """Several Each conditions in one decider (range tests per lane: lo <= Each <= hi, OR'd), and
    named conditions on chosen wires gating a named output copied from one wire."""
    r, g = pair_inputs(b)
    A, B, C = sig("signal-A"), sig("signal-B"), sig("signal-C")
    rng = [cond(EACH, ">=", 1, RED), cond(EACH, "<=", 7, RED, compare_type="and"),
           cond(EACH, ">=", 30, RED, compare_type="or"), cond(EACH, "<", 33, RED, compare_type="and"),
           cond(EACH, "=", -8, RED, compare_type="or")]
    combs = [
        b.decider(0, 2, rng, [out(EACH, copy=False, constant=-(1 << 30))]),
        b.decider(1, 2, rng, [out(EACH, networks=GREEN)], [out(EACH, copy=False, constant=5)]),
        # (A(red) > 0 AND B(green) > 0 AND C(green) = 0) OR (A(red) < 0 AND B(green) < 0): copy B
        b.decider(2, 2, [cond(A, ">", 0, RED), cond(B, ">", 0, GREEN, compare_type="and"),
                         cond(C, "=", 0, GREEN, compare_type="and"),
                         cond(A, "<", 0, RED, compare_type="or"), cond(B, "<", 0, GREEN, compare_type="and")],
                  [out(B, networks=GREEN)]),
        b.decider(3, 2, [cond(B, ">", 0, GREEN), cond(C, "!=", 0, GREEN, compare_type="and")],
                  [out(B, networks=GREEN)]),
        # Per lane: a constant plus a copy of the green wire, in both branches.
        b.decider(4, 2, [cond(EACH, ">", 0, RED)],
                  [out(EACH, copy=False, constant=1000), out(EACH, networks=GREEN)],
                  [out(EACH, copy=False, constant=-50), out(EACH, networks=GREEN)]),
        b.decider(5, 2, [cond(EACH, ">", 0, RED)], [out(EACH, copy=False, constant=9)], [out(EACH, networks=GREEN)]),
    ]
    wire_inputs(b, combs, r, g)


@case(ticks=6)
def decider_lanes(b):
    """Which lanes an Each decider visits: the union over every Each operand of every condition,
    so a condition on green brings in lanes that are absent (0) on red. Also: a per-lane key
    match gated by the other wire, range tests on large signed values, division by a negative
    constant."""
    extra = [(3 << 28, 4), (-(1 << 28), 6), (1 << 28, 9), (-(5 << 28), 1), (0, -2), (-4, 0),
             (0x70000000, 5), (-(1 << 28) - 1, 2), (7, 0), (0, 8)]
    r, g = pair_inputs(b, PAIRS + extra)
    combs = [
        # Texture mux: red in [-5, 6) on lanes present on green; red-absent lanes count as 0.
        b.decider(0, 2, [cond(EACH, "!=", 0, GREEN), cond(EACH, ">=", -5, RED, compare_type="and"),
                         cond(EACH, "<", 6, RED, compare_type="and")], [out(EACH, networks=GREEN)]),
        # The same without the green condition: only red's lanes are visited.
        b.decider(1, 2, [cond(EACH, ">=", -5, RED), cond(EACH, "<", 6, RED, compare_type="and")],
                  [out(EACH, networks=GREEN)]),
        # Masked lookup: red = key and green > 0.
        b.decider(2, 2, [cond(EACH, "=", 7, RED), cond(EACH, ">", 0, GREEN, compare_type="and")],
                  [out(EACH, copy=False, constant=99)]),
        # Top-nibble range tests on large signed values.
        b.decider(3, 2, [cond(EACH, ">=", -(1 << 28), RED), cond(EACH, "<", 0, RED, compare_type="and")],
                  [out(EACH, copy=False, constant=15)], [out(EACH, copy=False, constant=1)]),
        b.decider(4, 2, [cond(EACH, ">=", 1 << 28, RED), cond(EACH, "<", 2 << 28, RED, compare_type="and"),
                         cond(EACH, "<", -(1 << 30), RED, compare_type="or")],
                  [out(EACH, networks=GREEN)]),
        b.arithmetic(5, 2, EACH, "/", -3, EACH, RED),
        b.arithmetic(6, 2, EACH, "<<", EACH, EACH, GREEN, RED),
    ]
    wire_inputs(b, combs, r, g)


@case(ticks=6)
def decider_logic(b):
    """AND/OR precedence, everything/anything, outputs of everything, empty inputs."""
    inp = b.constant(0, 0, {"signal-A": 5, "signal-D": -4, "signal-E": 9})
    A, B, C = sig("signal-A"), sig("signal-B"), sig("signal-C")
    X, Y = sig("signal-X"), sig("signal-Y")
    one = lambda s=X: out(s, copy=False, constant=1)
    combs = [
        # A>0 OR B>0 AND C>0 : true with AND-binds-tighter, false if left-to-right
        b.decider(0, 2, [cond(A, ">", 0), cond(B, ">", 0, compare_type="or"), cond(C, ">", 0, compare_type="and")],
                  [one()], [one(Y)]),
        b.decider(1, 2, [cond(EVERYTHING, ">", 0)], [one()], [one(Y)]),
        b.decider(2, 2, [cond(EVERYTHING, ">", -5)], [out(EVERYTHING)], [one(Y)]),
        b.decider(3, 2, [cond(ANYTHING, "<", 0)], [out(EVERYTHING, copy=False, constant=3)]),
        b.decider(4, 2, [cond(ANYTHING, "=", 9)], [out(ANYTHING)]),
        b.decider(5, 2, [cond(A, ">", sig("signal-E"))], [one()], [out(EVERYTHING)]),
        b.decider(6, 2, [cond(A, "=", 5), cond(EVERYTHING, ">", 0, compare_type="and")], [one()], [one(Y)]),
    ]
    b.wire(inp, 1, combs[0], RED_IN)
    b.chain(combs, RED_IN)
    # Deciders with no input at all.
    b.decider(8, 2, [cond(EVERYTHING, ">", 0)], [one()], [one(Y)])
    b.decider(9, 2, [cond(ANYTHING, ">", 0)], [one()], [one(Y)])
    b.decider(10, 2, [cond(EACH, ">", 0)], [one()], [one(Y)])
    b.decider(11, 2, [cond(A, "=", 0)], [one()], [one(Y)])


@case(ticks=6)
def selector(b):
    """Sorting with ties, index comparators, index from a signal, count, game tick."""
    vals = {"signal-A": 5, "signal-B": 5, "signal-C": 9, "signal-D": -2, "signal-E": 9, "signal-F": 1}
    items = [(sig(k), v) for k, v in vals.items()] + [(sig("iron-plate", "item"), 5), (sig("copper-plate", "item"), 9)]
    inp = b.constant(0, 0, items)
    idx = b.constant(1, 0, {"signal-I": 2})
    combs = []
    for i, (mx, ix, cmp) in enumerate([(True, 0, "="), (True, 1, "="), (True, 2, "="), (True, 3, "="),
                                        (False, 0, "="), (False, 1, "="), (True, 3, "<"), (False, 2, "<"),
                                        (True, 2, ">"), (True, 7, "="), (True, 1, "<=")]):
        combs.append(b.selector(i, 2, operation="select", select_max=mx, index_constant=ix, index_comparator=cmp))
    combs.append(b.selector(11, 2, operation="count", count_signal=sig("signal-N")))
    b.wire(inp, 1, combs[0], RED_IN)
    b.chain(combs, RED_IN)
    s_idx = b.selector(12, 2, operation="select", select_max=True, index_signal=sig("signal-I"))
    b.wire(inp, 1, s_idx, RED_IN)
    b.wire(idx, 2, s_idx, GREEN_IN)
    b.selector(13, 2, operation="time", game_tick_signal=sig("signal-T"))


@case(ticks=5)
def selector_more(b):
    """Index taken from a signal (several placements), tie order when selecting the minimum."""
    data = [(sig("signal-A"), 5), (sig("signal-B"), 5), (sig("signal-C"), 9), (sig("iron-plate", "item"), 5),
            (sig("signal-D"), -2)]
    src = b.constant(0, 0, data)
    mins = [b.selector(i, 2, operation="select", select_max=False, index_constant=i) for i in range(4)]
    b.wire(src, 1, mins[0], RED_IN)
    b.chain(mins, RED_IN)
    # index_signal variants: index on the other wire, on the same wire, and a data signal as index.
    for j, (where, val) in enumerate([("green", 1), ("red", 1), ("green", 0), ("red", 0)]):
        x = 5 + j * 2
        s = b.selector(x, 2, operation="select", select_max=True, index_signal=sig("signal-I"))
        d = b.constant(x, 0, data)
        b.wire(d, 1, s, RED_IN)
        if val:
            i = b.constant(x + 1, 0, {"signal-I": val})
            b.wire(i, 1 if where == "red" else 2, s, RED_IN if where == "red" else GREEN_IN)
    s = b.selector(13, 2, operation="select", select_max=True, index_signal=sig("signal-D"))
    b.wire(src, 1, s, RED_IN)
    s2 = b.selector(14, 2, operation="select", select_max=True, index_signal=sig("signal-I"), index_constant=1)
    b.wire(src, 1, s2, RED_IN)


@case(ticks=5)
def lanes_zero_sum(b):
    """A signal at +5 on red and -5 on green: is it a lane for Each(both)?"""
    r = b.constant(0, 0, {"signal-A": 5, "signal-B": 2})
    g = b.constant(1, 0, {"signal-A": -5, "signal-C": 3})
    combs = [
        b.decider(0, 2, [cond(EACH, "=", 0)], [out(EACH, copy=False, constant=1)], [out(EACH, copy=False, constant=2)]),
        b.arithmetic(1, 2, EACH, "+", 1, EACH),
        b.decider(2, 2, [cond(EACH, "=", 0, RED)], [out(EACH, copy=False, constant=1)]),
        b.decider(3, 2, [cond(EVERYTHING, ">", 0)], [out(sig("signal-X"), copy=False, constant=1)]),
        b.selector(4, 2, operation="count", count_signal=sig("signal-N")),
        b.arithmetic(5, 2, EACH, "+", EACH, EACH, RED, GREEN),
    ]
    wire_inputs(b, combs, r, g)


@case(ticks=4)
def lamps(b):
    """Lamp color modes and enable conditions."""
    src = b.constant(0, 0, {"signal-R": 0x123456, "signal-G": 300, "signal-B": -5, "signal-A": 128,
                            "signal-red": 1, "signal-blue": 1, "signal-P": 0xFF00FF})
    ls = [
        b.lamp(1, 0, {"use_colors": True, "color_mode": 2, "rgb_signal": sig("signal-R")}),
        b.lamp(2, 0, {"use_colors": True, "color_mode": 2, "rgb_signal": sig("signal-Z")}),
        b.lamp(3, 0, {"use_colors": True, "color_mode": 1, "red_signal": sig("signal-G"),
                      "green_signal": sig("signal-B"), "blue_signal": sig("signal-A")}),
        b.lamp(4, 0, {"use_colors": True, "color_mode": 0}),
        b.lamp(5, 0, {"circuit_enabled": True, "circuit_condition": cond(sig("signal-A"), ">", 200)}),
        b.lamp(6, 0, {"circuit_enabled": True, "circuit_condition": cond(sig("signal-A"), ">", 100)}),
        b.lamp(7, 0, {"use_colors": True, "color_mode": 2, "rgb_signal": sig("signal-P"),
                      "circuit_enabled": True, "circuit_condition": cond(sig("signal-A"), "<", 0)}),
        b.lamp(8, 0),
    ]
    b.wire(src, 1, ls[0], 1)
    b.chain(ls, 1)
    neg = b.constant(0, 2, {"signal-red": -1, "signal-green": 2})
    b.wire(neg, 1, b.lamp(1, 2, {"use_colors": True, "color_mode": 0}), 1)
    # Unwired lamps in each color mode.
    b.lamp(2, 2, {"use_colors": True, "color_mode": 0})
    b.lamp(3, 2, {"use_colors": True, "color_mode": 2, "rgb_signal": sig("signal-R")})
    b.lamp(4, 2, {"use_colors": True, "color_mode": 1, "red_signal": sig("signal-R")})


@case(ticks=14)
def timing(b):
    """Pipeline latency, feedback counter (integrator), and a resetting clock."""
    a = b.constant(0, 0, {"signal-A": 1})
    c1 = b.arithmetic(1, 2, EACH, "*", 2, EACH)
    c2 = b.arithmetic(2, 2, EACH, "+", 1, EACH)
    c3 = b.arithmetic(3, 2, EACH, "*", 10, EACH)
    b.wire(a, 1, c1, RED_IN)
    b.wire(c1, RED_OUT, c2, RED_IN)
    b.wire(c2, RED_OUT, c3, RED_IN)
    # Integrator: output loops back into its own input on green, plus a constant 1.
    one = b.constant(5, 0, {"signal-T": 1})
    acc = b.arithmetic(5, 2, EACH, "*", 1, EACH)
    b.wire(one, 2, acc, GREEN_IN)
    b.wire(acc, GREEN_OUT, acc, GREEN_IN)
    # Clock: T < 5 passes T through; constant adds 1 each tick; resets to 0 at 5.
    one2 = b.constant(7, 0, {"signal-T": 1})
    clk = b.decider(7, 2, [cond(sig("signal-T"), "<", 5)], [out(sig("signal-T"))])
    b.wire(one2, 1, clk, RED_IN)
    b.wire(clk, RED_OUT, clk, RED_IN)


@case(ticks=5)
def networks(b):
    """Two sources summed on one network through a pole; output to red only vs both wires."""
    a = b.constant(0, 0, {"signal-A": 3, "signal-B": 1})
    a2 = b.constant(0, 1, {"signal-A": 4, "signal-B": -1})
    p = b.pole(2, 0)
    b.wire(a, 1, p, 1)
    b.wire(a2, 1, p, 1)
    c1 = b.arithmetic(3, 2, EACH, "*", 1, EACH)
    b.wire(p, 1, c1, RED_IN)
    # c1's output goes red to c2 and green to c3; c4 hears c2 and c3 combined on both wires.
    c2 = b.arithmetic(4, 2, EACH, "+", 100, EACH)
    c3 = b.arithmetic(5, 2, EACH, "*", -1, EACH)
    b.wire(c1, RED_OUT, c2, RED_IN)
    b.wire(c1, GREEN_OUT, c3, GREEN_IN)
    c4 = b.arithmetic(6, 2, EACH, "*", 1, EACH)
    b.wire(c2, RED_OUT, c4, RED_IN)
    b.wire(c3, GREEN_OUT, c4, GREEN_IN)
    # Two combinators driving one network: values add.
    c5 = b.arithmetic(7, 2, EACH, "+", 0, EACH)
    b.wire(c2, RED_OUT, c5, RED_IN)
    b.wire(c4, RED_OUT, c2, RED_OUT)


@case(ticks=4)
def wire_reach(b):
    """How wire reach is measured: lamps at known offsets from one constant, plus pole reach."""
    src = b.constant(0, 0, {"signal-R": 0x00FF00})
    packed = {"use_colors": True, "color_mode": 2, "rgb_signal": sig("signal-R")}
    # Tile offsets from the source. Footprint gaps (dx, dy) in whole tiles are offset - 1.
    for tx, ty in [(10, 0), (11, 0), (12, 0), (0, 10), (0, 11), (7, 7), (8, 7), (8, 8), (9, 6), (10, 3), (10, 4)]:
        b.wire(src, 1, b.lamp(tx, ty, packed), 1)
    # 1x2 combinators: footprint gap 10 tiles to the left; 9 tiles up.
    b.wire(src, 1, b.arithmetic(-11, 0, EACH, "*", 1, EACH), RED_IN)
    b.wire(src, 1, b.arithmetic(0, -11, EACH, "*", 1, EACH), RED_IN)
    b.wire(src, 1, b.arithmetic(-10, -1, EACH, "*", 1, EACH), RED_IN)
    # Poles: a medium pole (reach 9) and a big pole (reach 32), each with a near and a far constant.
    for px, kind, with_far in [(-20, "medium-electric-pole", True), (-40, "big-electric-pole", True),
                               (-60, "medium-electric-pole", False), (-80, "big-electric-pole", False)]:
        pole = b._add(kind, px + 0.5, 20.5) if kind == "medium-electric-pole" else b._add(kind, px + 1, 21)
        near = b.constant(px, 23, {"signal-B": 2})
        b.wire(near, 1, pole, 1)
        if with_far:
            far = b.constant(px - 12, 20, {"signal-A": 1})
            b.wire(far, 1, pole, 1)
        b.wire(pole, 1, b.arithmetic(px + 3, 20, EACH, "*", 1, EACH), RED_IN)


@case(ticks=4)
def pole_reach(b):
    """Circuit wires between two poles: measured center to center or footprint to footprint?
    Each pair: constant -> pole A -> pole B -> combinator, with B at a given offset from A."""
    for i, (dx, dy) in enumerate([(9, 0), (10, 0), (6, 6), (7, 7), (1, 9), (1, 10)]):
        x = 30 * i
        src = b.constant(x, 0, {"signal-A": i + 1})
        a = b.pole(x + 1, 0)
        c = b.pole(x + 1 + dx, dy)
        b.wire(src, 1, a, 1)
        b.wire(a, 1, c, 1)
        b.wire(c, 1, b.arithmetic(x + 2 + dx, dy, EACH, "*", 1, EACH), RED_IN)
    # A medium pole to a big pole (2x2): footprint gap 9 but center distance 10.5.
    src = b.constant(200, 0, {"signal-A": 9})
    a = b.pole(201, 0)
    big = b._add("big-electric-pole", 211 + 1, 0 + 1)
    b.wire(src, 1, a, 1)
    b.wire(a, 1, big, 1)
    b.wire(big, 1, b.arithmetic(213, 0, EACH, "*", 1, EACH), RED_IN)


@case(ticks=14)
def chest_read(b):
    """When a script insert into a chest shows up on its wire, removals, two chests summed, and a
    chest on the green wire. The harness inserts after recording row k (fsim.inserts)."""
    c1 = b.chest(0, 0)
    c2 = b.chest(1, 0, "wooden-chest")
    c3 = b.chest(2, 0, "iron-chest")
    c4 = b.chest(4, 0)
    copy = b.arithmetic(0, 2, EACH, "+", 0, EACH)               # c1 + c2 + c3 on red
    total = b.arithmetic(1, 2, EACH, "+", 0, sig("signal-S"))  # their sum as one signal
    green = b.arithmetic(4, 2, EACH, "*", 1, EACH)             # c4 on green
    b.wire(c1, 1, c2, 1)
    b.wire(c2, 1, c3, 1)
    b.wire(c1, 1, copy, RED_IN)
    b.wire(copy, RED_IN, total, RED_IN)
    b.wire(c4, 2, green, GREEN_IN)
    return {"inserts": [
        [2, c1, "iron-plate", 1],
        [5, c2, "iron-plate", 3], [5, c3, "copper-plate", 2], [5, c4, "coal", 7],
        [6, c1, "iron-plate", 1],
        [8, c2, "iron-plate", -2],
        [9, c4, "coal", -7],
    ]}


def main():
    root = pathlib.Path(__file__).parent / "cases"
    root.mkdir(exist_ok=True)
    for old in root.glob("*.json"):
        old.unlink()
    for name, (fn, ticks) in CASES.items():
        b = Blueprint(name)
        extra = fn(b) or {}
        b.power()
        data = b.to_json()
        data["fsim"] = {"ticks": ticks, "doc": (fn.__doc__ or "").strip(), **extra}
        (root / f"{name}.json").write_text(json.dumps(data, indent=1))
        print(f"{name}: {len(b.entities)} entities, {ticks} ticks")


if __name__ == "__main__":
    main()
