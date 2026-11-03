"""Combinator semantics in Python, for the timing-free reference evaluation of a netlist.

Specs are the blueprint control-behavior dicts themselves (arithmetic_conditions /
decider_conditions), so what gets evaluated is exactly what gets built. The rules follow the
verified table in README.md; anything not covered there raises instead of guessing.
Signal values are dicts {(type, name, quality): int}; absent means 0.
"""

EACH, EVERYTHING, ANYTHING = "signal-each", "signal-everything", "signal-anything"


def i32(x):
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x & 0x80000000 else x


def key(s):
    return (s.get("type", "item"), s["name"], s.get("quality", "normal"))


QUALITY_LEVEL = {"normal": 0, "uncommon": 1, "rare": 2, "epic": 3, "legendary": 5, "quality-unknown": 6}


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


def quality_transfer(quality):
    """Selector spec: every input signal re-tagged at `quality` (Space Age; conformance quality_ops)."""
    return {"operation": "quality-transfer", "quality_source_static": {"name": quality},
            "quality_destination_signal": {"type": "virtual", "name": EACH}}


def _selector(spec, red, green):
    net = _net(red, green, None)
    op = spec["operation"]
    if op == "quality-transfer" and spec["quality_destination_signal"]["name"] == EACH \
            and not spec.get("select_quality_from_signal"):
        q = spec.get("quality_source_static", {}).get("name", "normal")
        res = {}
        for (t, n, _), v in net.items():
            add_into(res, {(t, n, q): v})
        return res
    if op == "quality-filter":
        f = spec["quality_filter"]
        cmp, lq = CMP[f.get("comparator", "=")], QUALITY_LEVEL[f["quality"]]
        return {k: v for k, v in net.items() if cmp(QUALITY_LEVEL[k[2]], lq)}
    raise NotImplementedError(op)


def evaluate(kind, spec, red, green, cache=None):
    """cache: a dict shared by one evaluation pass in which input dicts are never mutated (keyed
    by their ids), so merged networks and value indexes are computed once."""
    if kind == "selector":
        return _selector(spec, red, green)
    return _arith(spec, red, green) if kind == "arith" else _decider(spec, red, green, cache)


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


def _net_cached(red, green, nets, cache):
    if cache is None:
        return _net(red, green, nets)
    nets = nets or {"red": True, "green": True}
    k = ("net", id(red), id(green), nets.get("red", True), nets.get("green", True))
    if k not in cache:
        cache[k] = (_net(red, green, nets), red, green)   # keep the inputs alive: ids stay unique
    return cache[k][0]


def _index(d, cache):
    """{value: [lanes]} for a lane dict."""
    k = ("idx", id(d))
    if k not in cache:
        idx = {}
        for lane, v in d.items():
            idx.setdefault(v, []).append(lane)
        cache[k] = (idx, d)
    return cache[k][0]


def _decider(spec, red, green, cache=None):
    conds = spec["conditions"]

    def operand(c, side):
        s = c.get(f"{side}_signal")
        if s is None:
            return False, c.get("constant", 0)
        net = _net_cached(red, green, c.get(f"{side}_signal_networks"), cache)
        if s["name"] == EACH:
            return True, net
        if s["name"] in (EVERYTHING, ANYTHING):
            raise NotImplementedError(s["name"])
        return False, net.get(key(s), 0)

    groups = [[(CMP[c["comparator"]], operand(c, "first"), operand(c, "second")) for c in g] for g in _groups(conds)]

    def holds(lane):
        for g in groups:
            for cmp, (ea, a), (eb, b) in g:
                va = a.get(lane, 0) if ea else a
                vb = b.get(lane, 0) if eb else b
                if not cmp(va, vb):
                    break
            else:
                return True
        return False

    each_nets = [x for g in groups for _, a, b in g for e, x in (a, b) if e]
    res = {}
    if each_nets:
        outs, else_outs = spec["outputs"], spec.get("else_outputs", [])
        named = any(o["signal"]["name"] != EACH for o in outs + else_outs)
        for o in outs + else_outs:
            if o["signal"]["name"] in (EVERYTHING, ANYTHING):
                raise NotImplementedError(f"{o['signal']['name']} output under Each conditions")
        first = groups[0][0]
        if (cache is not None and len(groups) == 1 and not else_outs and not named and first[0] is CMP["="]
                and first[1][0] and not first[2][0] and first[2][1] != 0):
            # A key lookup: only lanes whose value equals the key can pass.
            cand = _index(first[1][1], cache).get(first[2][1], [])
            passing, failing = [l for l in cand if holds(l)], []
        else:
            lanes = set()
            for x in each_nets:
                lanes |= x.keys()
            passing, failing = [], []
            for lane in lanes:
                (passing if holds(lane) else failing).append(lane)
        for lst, os_ in ((passing, outs), (failing, else_outs)):
            for o in os_:
                if o["signal"]["name"] != EACH:
                    # A named output under Each conditions (decider_each case): the constant times
                    # the matching lanes, or the sum of their values when copying.
                    if o.get("copy_count_from_input", True):
                        src = _net_cached(red, green, o.get("networks"), cache)
                        v = i32(sum(src.get(lane, 0) for lane in lst))
                    else:
                        v = i32(o.get("constant", 1) * len(lst))
                    if lst:
                        add_into(res, {key(o["signal"]): v})
                elif o.get("copy_count_from_input", True):
                    src = _net_cached(red, green, o.get("networks"), cache)
                    for lane in lst:
                        add_into(res, {lane: src.get(lane, 0)})
                else:
                    v = o.get("constant", 1)
                    for lane in lst:
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
