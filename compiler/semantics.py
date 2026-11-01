"""Combinator semantics in Python, for the timing-free reference evaluation of a netlist.

Specs are the blueprint control-behavior dicts themselves (arithmetic_conditions /
decider_conditions), so what gets evaluated is exactly what gets built. The rules follow the
verified table in README.md; anything not covered there raises instead of guessing.
Signal values are dicts {(type, name): int}; absent means 0.
"""

EACH, EVERYTHING, ANYTHING = "signal-each", "signal-everything", "signal-anything"


def i32(x):
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x & 0x80000000 else x


def key(s):
    return (s.get("type", "item"), s["name"])


def add_into(acc, d):
    for k, v in d.items():
        r = i32(acc.get(k, 0) + v)
        if r:
            acc[k] = r
        else:
            acc.pop(k, None)


# -- spec builders (same JSON as conformance/bp.py) ----------------------------------------------

def arith_spec(first, op, second, out, first_nets=None, second_nets=None):
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
    return a


def cond(first, comparator, second, first_nets=None, compare_type=None, second_nets=None):
    c = {"comparator": comparator, "first_signal": first}
    if first_nets:
        c["first_signal_networks"] = first_nets
    if isinstance(second, int):
        c["constant"] = second
    else:
        c["second_signal"] = second
        if second_nets:
            c["second_signal_networks"] = second_nets
    if compare_type:
        c["compare_type"] = compare_type
    return c


def out(signal, constant=None, networks=None):
    """Copy from input (networks) when constant is None, else output the constant."""
    o = {"signal": signal}
    if constant is not None:
        o["copy_count_from_input"] = False
        o["constant"] = constant
    if networks:
        o["networks"] = networks
    return o


def decider_spec(conds, outs, else_outs=None):
    d = {"conditions": conds, "outputs": outs}
    if else_outs:
        d["else_outputs"] = else_outs
    return d


# -- evaluation ------------------------------------------------------------------------------------

def _net(red, green, nets):
    nets = nets or {"red": True, "green": True}
    if nets.get("red", True) and nets.get("green", True):
        acc = dict(red)
        add_into(acc, green)
        return acc
    return dict(red) if nets.get("red", True) else dict(green)


def _tdiv(a, b):
    if b == 0 or (a == -(1 << 31) and b == -1):
        return 0
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _pow(a, b):
    if b < 0:
        return a if a in (1, -1) and b % 2 else (1 if a in (1, -1) else 0)
    return pow(a, b, 1 << 32)


OPS = {
    "+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b,
    "/": _tdiv, "%": lambda a, b: 0 if b == 0 else a - _tdiv(a, b) * b,
    "^": _pow, "<<": lambda a, b: a << (b & 31), ">>": lambda a, b: a >> (b & 31),
    "AND": lambda a, b: a & b, "OR": lambda a, b: a | b, "XOR": lambda a, b: a ^ b,
}

CMP = {"<": lambda a, b: a < b, ">": lambda a, b: a > b, "=": lambda a, b: a == b,
       "!=": lambda a, b: a != b, "≠": lambda a, b: a != b, "<=": lambda a, b: a <= b,
       "≤": lambda a, b: a <= b, ">=": lambda a, b: a >= b, "≥": lambda a, b: a >= b}


def evaluate(kind, spec, red, green):
    return _arith(spec, red, green) if kind == "arith" else _decider(spec, red, green)


def _operand(spec, side, red, green):
    """('each', net) | ('sig', value) | ('const', value)"""
    s = spec.get(f"{side}_signal")
    if s is None:
        return ("const", spec.get(f"{side}_constant", 0))
    net = _net(red, green, spec.get(f"{side}_signal_networks"))
    if s["name"] == EACH:
        return ("each", net)
    if s["name"] in (EVERYTHING, ANYTHING):
        raise NotImplementedError(s["name"])
    return ("sig", net.get(key(s), 0))


def _arith(spec, red, green):
    op = OPS[spec["operation"]]
    a = _operand(spec, "first", red, green)
    b = _operand(spec, "second", red, green)
    outsig = spec["output_signal"]
    if a[0] != "each" and b[0] != "each":
        r = i32(op(a[1], b[1]))
        return {key(outsig): r} if r else {}
    lanes = set()
    for x in (a, b):
        if x[0] == "each":
            lanes |= set(x[1])
    res = {}
    for l in lanes:
        va = a[1].get(l, 0) if a[0] == "each" else a[1]
        vb = b[1].get(l, 0) if b[0] == "each" else b[1]
        r = i32(op(va, vb))
        if r:
            res[l] = r
    if outsig["name"] == EACH:
        return res
    total = 0
    for v in res.values():
        total = i32(total + v)
    return {key(outsig): total} if total else {}


def _groups(conds):
    groups, cur = [], []
    for i, c in enumerate(conds):
        if i and c.get("compare_type", "or") == "or":
            groups.append(cur)
            cur = []
        cur.append(c)
    groups.append(cur)
    return groups


def _cond_value(c, side, red, green, lane):
    s = c.get(f"{side}_signal")
    if s is None:
        return c.get("constant", 0)
    net = _net(red, green, c.get(f"{side}_signal_networks"))
    if s["name"] == EACH:
        return net.get(lane, 0)
    if s["name"] in (EVERYTHING, ANYTHING):
        raise NotImplementedError(s["name"])
    return net.get(key(s), 0)


def _decider(spec, red, green):
    conds = spec["conditions"]
    groups = _groups(conds)

    def holds(lane):
        return any(all(CMP[c["comparator"]](_cond_value(c, "first", red, green, lane),
                                            _cond_value(c, "second", red, green, lane)) for c in g) for g in groups)

    uses_each = any((c.get(f"{s}_signal") or {}).get("name") == EACH for c in conds for s in ("first", "second"))
    res = {}
    if uses_each:
        lanes = set()
        for c in conds:
            for side in ("first", "second"):
                s = c.get(f"{side}_signal")
                if s and s["name"] == EACH:
                    lanes |= set(_net(red, green, c.get(f"{side}_signal_networks")))
        for lane in lanes:
            outs = spec["outputs"] if holds(lane) else spec.get("else_outputs", [])
            for o in outs:
                if o["signal"]["name"] != EACH:
                    raise NotImplementedError("named output under Each conditions")
                if o.get("copy_count_from_input", True):
                    v = _net(red, green, o.get("networks")).get(lane, 0)
                else:
                    v = o.get("constant", 1)
                add_into(res, {lane: v})
        return res
    outs = spec["outputs"] if holds(None) else spec.get("else_outputs", [])
    for o in outs:
        name = o["signal"]["name"]
        if o.get("copy_count_from_input", True):
            net = _net(red, green, o.get("networks"))
            if name == EVERYTHING:
                add_into(res, net)
            elif name == EACH:
                raise NotImplementedError("Each output without Each conditions")
            else:
                add_into(res, {key(o["signal"]): net.get(key(o["signal"]), 0)})
        else:
            if name in (EVERYTHING, EACH):
                raise NotImplementedError(name)
            add_into(res, {key(o["signal"]): o.get("constant", 1)})
    return res
