"""Monsters that wake, chase and attack: Doom's behaviour, simplified.

    asleep   until the player is in sight in front of it (or within a tile), or it gets shot
    chase    walk toward the player, stopping a little way off: along the axis it is further off on
             (the major axis) and the other one, sliding along walls; when one corner of a wall blocks
             the major axis, it steps sideways off that corner first (Doom tries other directions);
             one full walk cycle after waking, an attack or a flinch, then an attack attempt at
             each of Doom's A_Chase calls, with Doom's chance by distance (P_CheckMissileRange), and
             none beyond 5 tiles (Doom attacks from anywhere in sight)
    attack   Doom's attack frames; at the shot frame, if the player is still in sight: the imp
             scratches within a tile (always hits) or throws a fireball (hits half the time: there
             are no projectiles yet), the zombieman fires (hit chance from Doom's spread)
    pain     any hit that doesn't kill interrupts whatever the monster was doing (Doom's pain state,
             with its pain chance at 100%), then it walks a full cycle before it can attack again, so
             punching an imp every 38 ticks keeps it from ever scratching
    death    as before (combat.py)

Everything is one tick deep, like the weapons: deciders read the state bus (REG's red output,
game.py) on red and the monsters' own network M (green, around a pole: game.build) on green, and
write one or the other. The bus carries only what others read (positions, clocks, damage taken);
M carries the rest, so the bus's signal names stay few (the raycaster reads the bus, and needs most
of the virtual signals for its own scalars). Per monster k (k-th in touch.monsters, at thing index
i, spawned at E):

    MX, MY   (bus) position, as an offset from E (1/FP tiles)
    MS       (bus) its clock (combat.MS): 0 asleep, 1.. pain, DEATH.. dying, FRESH.. the first walk
             cycle, LOOP.. walking (wraps), ATTACK.. attacking
    DX, DY   the player's offset from it (an arith each, SX + (SPAWN - E) - MX: the constant rides
             on its green input; one tick behind)
    XQ, YQ   DX * DX, DY * DY (two ticks behind): XQ >= YQ when x is the major axis
    DT       a detour: when the way to the player is blocked by a wall on the major axis and the
             other axis gives nothing, walk along that wall (DT = +-1: along x, +-2: along y) until
             the way opens, turning back at a dead end (Doom keeps a chase direction for a while)
    CF       per step axis and corner (+x+y, +x-y, -x+y, -x-y): in a wall. An x step tests the
             corners at (+-BOX, +-SIDE), a y step those at (+-SIDE, +-BOX): narrower across the
             step, so a monster that walked up to a wall (its box up to two steps into it) can
             still slide along it
    SEE      line of sight: the number of the N - 1 sample points between it and the player that
             aren't in a wall (two ticks behind: products, then a 2D range lookup on the samples'
             x (red) and y (green), counting the misses with a named else-output)
    R        a random number: RND (M's LCG) times a per-monster odd constant
    ATT      one tick: it decided to attack (MS jumps to ATTACK the next tick)
    LD       its DMG a tick ago (an arith): DMG > LD is a hit (it flinches)
PD (M) is the damage the player has coming: each attack that hits adds its damage, and each tick
3 of it is applied (green armor takes 1 while it lasts, health stops at 0), so simultaneous hits
just queue.

`step` is the same rules in plain Python, for game.py's check.
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from compiler.bp import sig  # noqa: E402
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import EACH, GREEN, LANE_SIGNALS, RED  # noqa: E402
import combat as cb  # noqa: E402
import maps  # noqa: E402
import touch as tc  # noqa: E402
import weapons as wp  # noqa: E402

FP = 256
MAXMON = 4
_names = lambda *ns: [sig(n) for n in ns]  # noqa: E731
MX = _names("signal-plus", "signal-minus", "signal-multiplication", "signal-division")
MY = _names("signal-equal", "signal-not-equal", "signal-less-than", "signal-greater-than")
DX = _names("signal-less-than-or-equal-to", "signal-greater-than-or-equal-to", "signal-left-parenthesis",
            "signal-right-parenthesis")
DY = _names("signal-left-square-bracket", "signal-right-square-bracket", "signal-ampersand", "signal-circumflex-accent")
SEE = _names("signal-number-sign", "signal-percent", "signal-8", "signal-9")
RK = _names("signal-check", "signal-deny", "signal-no-entry", "signal-heart")
LD = _names("signal-alert", "signal-star", "signal-info", "signal-dot")
ATT = _names("up-arrow", "right-arrow", "down-arrow", "left-arrow")
CF = [_names(*q) for q in (("shape-vertical", "shape-horizontal", "shape-curve", "shape-curve-2",
                            "up-right-arrow", "down-right-arrow", "down-left-arrow", "up-left-arrow"),
                           ("shape-corner", "shape-corner-2", "shape-t", "shape-t-2",
                            "signal-rightwards-leftwards-arrow", "signal-upwards-downwards-arrow", "signal-left-right-arrow",
                            "signal-up-down-arrow"),
                           ("shape-cross", "shape-diagonal-cross", "shape-diagonal", "shape-diagonal-2",
                            "signal-clockwise-circle-arrow", "signal-anticlockwise-circle-arrow", "signal-input", "signal-output"),
                           ("shape-curve-3", "shape-curve-4", "shape-corner-4", "shape-corner-3",
                            "signal-snowflake", "signal-explosion", "signal-liquid", "signal-stack-size"))]
CORNERS = [(1, 1), (1, -1), (-1, 1), (-1, -1)]   # CF[k][4 * axis + c]
XQ = _names("signal-fuel", "signal-lightning", "signal-battery-low", "signal-battery-mid-level")
YQ = _names("signal-battery-full", "signal-radioactivity", "signal-thermometer-blue", "signal-thermometer-red")
RND = sig("signal-shuffle")
PD = sig("signal-damage")
PH = sig("signal-hourglass")   # 1 every other tick while PD > 0: PD drains then (REG's health is a tick stale)
DT = _names("signal-recycle", "signal-trash-bin", "signal-science-pack", "signal-map-marker")
SIGNALS = MX + MY                                                                  # on the bus
NET = DX + DY + XQ + YQ + SEE + RK + LD + ATT + DT + [s for q in CF for s in q] + [RND, PD, PH]      # on M
_NETNAMES = {s["name"] for s in NET}

# The LCG: RND' = (RND + C) * A (the arith reads C on its red input, M on green), and R_k = M_k * RND.
LCG_A, LCG_C = 1103515245, 12345
MULT = [0x2545F491, 0x6C8E9CF5, 0x9E3779B1 - (1 << 32), 0x85EBCA77 - (1 << 32)]

NS = 64                   # line of sight: points k / NS of the way, k = 1 .. NS - 1
BOX = 56                  # the box tested ahead of a step: Doom's radius 20 units (40), plus the corner flags'
                          # age (two steps) and room to stop short of a wall by more than SIDE - 40
SIDE = 46                 # ... and across it: the radius plus two steps (the other axis moves too)
DZ = 20                   # dead zone: no step on an axis the player is within this of (Doom: 10 units)
STOP = 176                # a monster doesn't step to within this of the player on both axes
REACH = FP                # melee: within a tile on both axes (the imp's scratch, and a sleeper's notice)
BANDS = [FP, 2 * FP, 3 * FP, 5 * FP]   # distance bands (both axes within), the last one open
FRESH, LOOP, ATTACK = 128, 192, 256   # MS ranges (combat.DEATH = 64 .. corpse < 128)
TILE_DMG = 3              # applied per tick from PD
# Doom (info.c, p_enemy.c): walk frames (A_Chase every half frame), attack frames, the tic of the
# attack, damage (the mean of Doom's roll, a multiple of 3: green armor takes a third), speed
# (1/FP tiles per tick: Doom's 8 units per A_Chase), and per distance band the chance of an
# attack per A_Chase (P_CheckMissileRange, 1 - min(dist - 64 (- 128 without melee), 200) / 256) and
# of a hit (the zombieman's spread, +-22 degrees, against the player's 16 unit radius; the imp's
# fireball without projectiles: half). No attacks from the last band (beyond 5 tiles): Doom's 22%.
TYPES = {
    "TROO": {"walk": [("TROOA1", 6), ("TROOB1", 6), ("TROOC1", 6), ("TROOD1", 6)], "chase": 3,
             "attack": [("TROOE1", 8), ("TROOF1", 8), ("TROOG1", 6)], "fire": 16, "damage": 12, "speed": 3,
             "melee": True, "chance": [1.0, 0.5, 0.22, 0.22, 0.0], "hit": [1.0, 0.5, 0.5, 0.5, 0.5]},
    "POSS": {"walk": [("POSSA1", 8), ("POSSB1", 8), ("POSSC1", 8), ("POSSD1", 8)], "chase": 4,
             "attack": [("POSSE1", 10), ("POSSF1", 8), ("POSSE1", 8)], "fire": 10, "damage": 9, "speed": 2,
             "melee": False, "chance": [1.0, 1.0, 0.5, 0.3, 0.0], "hit": [0.64, 0.38, 0.24, 0.15, 0.1]},
}
# Which way each monster on the current map faces (thing index -> unit vector, maps.select): it
# notices the player in that half plane (Doom: 180 degrees), or within a tile.
FACING = dict(maps.ARENA["facing"])


def walk_len(t):
    return wp.ticks(sum(n for _, n in TYPES[t]["walk"]))


def attack_len(t):
    return wp.ticks(sum(n for _, n in TYPES[t]["attack"]))


def fire_tick(t):
    return wp.ticks(TYPES[t]["fire"])


def chase_ticks(t):
    """The walk cycle's A_Chase ticks (offsets): attack attempts."""
    return sorted({wp.ticks(TYPES[t]["chase"] * j) for j in range(8)})   # Doom: 8 run states a cycle


def threshold(p):
    """R < threshold(p) with probability p (R uniform over int32); None: always."""
    return None if p >= 1 else round(p * (1 << 32)) - (1 << 31)


def frame(name, ms):
    """The frame at clock ms in the chase or attack ranges, or None."""
    t = TYPES[name[:4]]
    for base, n, seq in ((FRESH, walk_len(name[:4]), t["walk"]), (LOOP, walk_len(name[:4]), t["walk"]),
                         (ATTACK, attack_len(name[:4]), t["attack"])):
        if base <= ms < base + n:
            return cb._seq(seq, ms - base)
    return None


def clock_values(name):
    t = name[:4]
    return (list(range(FRESH, FRESH + walk_len(t))) + list(range(LOOP, LOOP + walk_len(t)))
            + list(range(ATTACK, ATTACK + attack_len(t))))


def rects(walls):
    """Wall cells as rectangles (x0, x1, y0, y1), inclusive: runs per row, merged down."""
    runs = []
    for x, y in sorted(walls, key=lambda c: (c[1], c[0])):
        if runs and runs[-1][3] == y and runs[-1][1] == x - 1:
            runs[-1][1] = x
        else:
            runs.append([x, x, y, y])
    out = []
    for r in runs:
        for o in out:
            if o[0] == r[0] and o[1] == r[1] and o[3] == r[2] - 1:
                o[3] = r[3]
                break
        else:
            out.append(list(r))
    return [tuple(o) for o in out]


class Monsters:
    """The bus logic. things: the THINGS list; walls: wall cells; spawn: the player's spawn (REG's
    X, Y are offsets from it); sigs: {"SX", "SY", "health", "armor"} bus signals; start: hud.START."""

    def __init__(self, things, walls, spawn, sigs, start):
        self.things, self.spawn, self.sigs, self.start = things, spawn, sigs, start
        self.idx = tc.monsters(things)
        if len(self.idx) > MAXMON:
            raise ValueError(f"{len(self.idx)} monsters: at most {MAXMON}")
        self.rects = rects(walls)
        self.E = [(int(things[i][0] * FP), int(things[i][1] * FP)) for i in self.idx]
        self.off = [(spawn[0] - ex, spawn[1] - ey) for ex, ey in self.E]   # DX = SX - MX + off

    def type(self, k):
        return self.things[self.idx[k]][2][:4]

    # -- condition helpers: a term is (signal, comparator, constant or signal); signals of M on green
    @staticmethod
    def wire(s):
        return GREEN if s["name"] in _NETNAMES else RED

    def _c(self, s, comp, v, ct="and"):
        if isinstance(v, dict):
            return sem.cond(s, comp, v, self.wire(s), ct, self.wire(v))
        return sem.cond(s, comp, v, self.wire(s), ct)

    def clause(self, terms, first):
        """terms [(signal, comparator, value)] as one AND clause (OR'd onto what precedes)."""
        return [self._c(s, comp, v, (None if first else "or") if j == 0 else "and") for j, (s, comp, v) in enumerate(terms)]

    def dnf(self, clauses):
        out = []
        for terms in clauses:
            out += self.clause(terms, not out)
        return out

    def player_alive(self):
        return [(self.sigs["health"], ">", -self.start["health"])]

    def alive(self, k):
        i = self.idx[k]
        return [(cb.DMG[i], "<", cb.hp(self.things[i][2]))]

    def move_clauses(self, k, ax, u, s):
        """Clauses for a step of u along axis ax (see move_rules): a major-axis move, a minor-axis
        move (not while the major axis is nudging), or a nudge off a corner blocking the major axis."""
        b = 1 - ax
        major = lambda a: [(XQ[k], ">=" if a == 0 else "<", YQ[k])]  # noqa: E731

        def corner(axis, w, c, v):
            """The corner steps along `axis` test, at w along that axis and c across it."""
            return self.cf(k, axis, *((w, c) if axis == 0 else (c, w)), v)
        free = [corner(ax, u, 1, 0), corner(ax, u, -1, 0)]   # both leading corners for this step
        far = lambda axis, w, d: self.dx(k, ">=" if w > 0 else "<=", w * d, axis)  # noqa: E731
        # Detouring along ax in direction u (away from the player at least STOP on some axis), or
        # along the other axis while ax was blocked: a step toward the player on ax, if it's free.
        det = [[(DT[k], "=", u * (1 + ax))] + free + [far(a, w, STOP)] for a in (0, 1) for w in (1, -1)]
        det += [[(DT[k], "=", e * (1 + b)), self.dx(k, ">" if u > 0 else "<", u * DZ, ax)] + free + r
                for e in (1, -1) for r in ([far(ax, u, STOP + s)], [far(b, 1, STOP)], [far(b, -1, STOP)])]
        none = [(DT[k], "=", 0)]
        out = [none + major(ax) + [far(ax, u, STOP + s)] + free]
        # minor: the major axis b at least STOP away, and not nudging (b wanted with its leading
        # corner on the -u side blocked and the u side free).
        for w in (1, -1):
            not_nudging = [[far(b, w, STOP), self.dx(k, "<" if w > 0 else ">", w * (STOP + s), b)],
                           [far(b, w, STOP), corner(b, w, -u, 0)],
                           [far(b, w, STOP), corner(b, w, u, 1)]]
            for nn in not_nudging:
                out.append(none + major(b) + [self.dx(k, ">" if u > 0 else "<", u * DZ, ax)] + free + nn)
        # nudge: b wanted (far enough), its leading corner on the -u side blocked, the u side free.
        for w in (1, -1):
            out.append(none + major(b) + [far(b, w, STOP + s), corner(b, w, -u, 1), corner(b, w, u, 0)] + free)
        return out + det

    def detour_specs(self, k):
        """DT: set when chasing, the major axis a blocked toward the player on both corners and the
        other axis b no way toward the player (aligned within DZ, or blocked): along b, the side R
        picks (or the free one); held while a stays blocked toward the player and b is free ahead;
        turned around when b is blocked ahead. Dropped otherwise (not chasing, the way open)."""
        alive, pa = self.alive(k), self.player_alive()
        i = self.idx[k]
        calm = alive + [(cb.DMG[i], "<=", LD[k])]
        base = self.chasing(k)[0] + calm + pa
        major = lambda a: [(XQ[k], ">=" if a == 0 else "<", YQ[k])]  # noqa: E731

        def corner(axis, w, c, v):
            return self.cf(k, axis, *((w, c) if axis == 0 else (c, w)), v)
        free = lambda axis, w: [corner(axis, w, 1, 0), corner(axis, w, -1, 0)]  # noqa: E731
        blocked = lambda axis, w: [[corner(axis, w, 1, 1)], [corner(axis, w, -1, 1)]]  # noqa: E731
        toward = lambda axis, w: self.dx(k, ">" if w > 0 else "<", w * DZ, axis)  # noqa: E731
        spec = lambda clauses, outs: sem.decider_spec(self.dnf(clauses), outs)  # noqa: E731
        out = []
        # set
        for a in (0, 1):
            b = 1 - a
            for e in (1, -1):
                no_way = [[self.dx(k, "<=", DZ, b), self.dx(k, ">=", -DZ, b)]] + \
                         [[toward(b, w)] + bl for w in (1, -1) for bl in blocked(b, w)]
                pick = [[(RK[k], ">=" if e > 0 else "<", 0)]] + [bl for bl in blocked(b, -e)]
                cl = [[(DT[k], "=", 0)] + base + major(a) + [toward(a, w), corner(a, w, 1, 1), corner(a, w, -1, 1)]
                      + nw + pk + free(b, e) for w in (1, -1) for nw in no_way for pk in pick]
                out.append((f"detour {k} {'xy'[b]}{'+-'[e < 0]}", spec(cl, [sem.out(DT[k], constant=e * (1 + b))]), "net"))
        # hold, turn around
        hold, turn = [], {}
        for b in (0, 1):
            a = 1 - b
            for e in (1, -1):
                c = e * (1 + b)
                stuck = [[toward(a, w)] + bl for w in (1, -1) for bl in blocked(a, w)]
                hold += [[(DT[k], "=", c)] + base + st + free(b, e) for st in stuck]
                turn[c] = [[(DT[k], "=", c)] + base + st + bl for st in stuck for bl in blocked(b, e)]
        out.append((f"detour hold {k}", spec(hold, [sem.out(DT[k], networks=GREEN)]), "net"))
        for c, cl in turn.items():
            out.append((f"detour turn {k} {c:+d}", spec(cl, [sem.out(DT[k], constant=-c)]), "net"))
        return out

    def ms_range(self, k, lo, hi):
        """lo <= MS <= hi"""
        s = cb.MS[self.idx[k]]
        return [(s, "=", lo)] if lo == hi else [(s, ">=", lo), (s, "<=", hi)]

    def dx(self, k, comp, v, axis=0):
        """The player's offset from monster k (stale by a tick) compared with v."""
        return ((DX, DY)[axis][k], comp, v)

    def chasing(self, k):
        """FRESH .. LOOP + n - 1: both walk ranges (nothing lies between them)."""
        assert FRESH + walk_len(self.type(k)) <= LOOP
        return [self.ms_range(k, FRESH, LOOP + walk_len(self.type(k)) - 1)]

    def cf(self, k, axis, sx, sy, v):
        """Corner (sx, sy) of the box tested by steps along axis is (v = 1) or isn't in a wall."""
        return (CF[k][4 * axis + CORNERS.index((sx, sy))], "=", v)

    def band(self, k, b):
        """The player within band b on both axes (b = len(BANDS): anywhere)."""
        if b >= len(BANDS):
            return []
        r = BANDS[b]
        return [self.dx(k, ">", -r, 0), self.dx(k, "<", r, 0), self.dx(k, ">", -r, 1), self.dx(k, "<", r, 1)]

    def bands(self, k, probs, extra):
        """Clauses: extra, the player in band b and R < threshold(probs[b]), for every b. The bands
        nest and the chances fall with distance, so this is the chance of the nearest band holding
        the player."""
        return [extra + self.band(k, b) + ([(RK[k], "<", threshold(p))] if threshold(p) is not None else [])
                for b, p in enumerate(probs) if p > 0]

    def specs(self):
        """[(label, spec, target[, inputs])]: deciders reading the bus (red) and M (green) and
        writing the bus ("bus") or M ("net"), both gated by the restart, or REG's input ("reg");
        "netarith": an arithmetic writing M (not gated: it holds nothing), inputs {"red", "green"}:
        "S" (the bus), "M", or a list of (signal, value) for a constant. The line-of-sight nodes
        are built by los()."""
        c = sem.cond
        res = []
        ks = range(len(self.idx))
        if not self.idx:
            return res
        SX, SY = self.sigs["SX"], self.sigs["SY"]
        # Held positions (one decider copying them all) and the damage queue.
        res.append(("monster hold", sem.decider_spec([c(MX[0], ">=", 0, RED), c(MX[0], "<", 0, RED, "or")],
                                                     [sem.out(s, networks=RED) for k in ks for s in (MX[k], MY[k])]),
                    "bus"))
        # The player's offsets, the damage a tick ago, and the random numbers.
        for k in ks:
            ox, oy = self.off[k]
            res.append((f"dx {k}", sem.arith_spec(SX, "-", MX[k], DX[k], None, RED), "netarith", {"red": "S", "green": [(SX, ox)]}))
            res.append((f"dy {k}", sem.arith_spec(SY, "-", MY[k], DY[k], None, RED), "netarith", {"red": "S", "green": [(SY, oy)]}))
            res.append((f"xq {k}", sem.arith_spec(DX[k], "*", DX[k], XQ[k], GREEN, GREEN), "netarith", {"green": "M"}))
            res.append((f"yq {k}", sem.arith_spec(DY[k], "*", DY[k], YQ[k], GREEN, GREEN), "netarith", {"green": "M"}))
            res.append((f"last damage {k}", sem.arith_spec(cb.DMG[self.idx[k]], "+", 0, LD[k], RED), "netarith", {"red": "S"}))
        res.append(("rng", sem.arith_spec(RND, "*", LCG_A, RND), "netarith", {"red": [(RND, LCG_C)], "green": "M"}))
        res.append(("rng split", sem.arith_spec(EACH, "*", RND, EACH, RED, GREEN), "netarith",
                    {"red": list(zip(RK, MULT[:len(self.idx)])), "green": "M"}))
        # Walls: per step axis and corner, in a wall.
        for k in ks:
            ex, ey = self.E[k]
            for axis in (0, 1):
                for c_, (sx, sy) in enumerate(CORNERS):
                    ox, oy = (ex + sx * BOX, ey + sy * SIDE) if axis == 0 else (ex + sx * SIDE, ey + sy * BOX)
                    cl = [[(MX[k], ">=", x0 * FP - ox), (MX[k], "<", (x1 + 1) * FP - ox),
                           (MY[k], ">=", y0 * FP - oy), (MY[k], "<", (y1 + 1) * FP - oy)] for x0, x1, y0, y1 in self.rects]
                    res.append((f"wall {k} {'xy'[axis]} {sx:+d}{sy:+d}",
                                sem.decider_spec(self.dnf(cl), [sem.out(CF[k][4 * axis + c_], constant=1)]), "net"))
        for k in ks:
            res += self._monster(k) + self.detour_specs(k)
        res += self._damage()
        return res

    def _monster(self, k):
        t, i = self.type(k), self.idx[k]
        T = TYPES[t]
        M, n_w, n_a = cb.MS[i], walk_len(t), attack_len(t)
        alive, pa = self.alive(k), self.player_alive()
        calm = alive + [(cb.DMG[i], "<=", LD[k])]   # not hit this tick
        out = []
        copy1 = lambda: [sem.out(M, networks=RED), sem.out(M, constant=1)]  # noqa: E731
        spec = lambda clauses, outs: sem.decider_spec(self.dnf(clauses), outs)  # noqa: E731
        # Moves (chasing, the player alive, no flinch): see move_rules; one decider per direction.
        s = T["speed"]
        for ax in (0, 1):
            for u in (1, -1):
                cl = [ch + calm + pa + c for ch in self.chasing(k) for c in self.move_clauses(k, ax, u, s)]
                out.append((f"move {k} {'+-'[u < 0]}{'xy'[ax]}", spec(cl, [sem.out((MX, MY)[ax][k], constant=u * s)]), "bus"))
        # Waking: in sight and in front (or within a tile), the player alive.
        fx, fy = FACING.get(i, (0, 0))
        front = ([[self.dx(k, ">=" if fx > 0 else "<=", 0, 0)]] if fx else []) + \
                ([[self.dx(k, ">=" if fy > 0 else "<=", 0, 1)]] if fy else [])
        if not front:
            front = [[]]
        seen = [(SEE[k], ">=", NS - 1)]
        cl = [self.ms_range(k, 0, 0) + calm + pa + seen + f for f in front] + \
             [self.ms_range(k, 0, 0) + calm + pa + seen + self.band(k, 0)]
        out.append((f"wake {k}", spec(cl, [sem.out(M, constant=FRESH)]), "bus"))
        # Flinch on a hit that doesn't kill (from any live state: dying is DMG >= hp).
        out.append((f"pain {k}", spec([alive + [(cb.DMG[i], ">", LD[k])]], [sem.out(M, constant=1)]), "bus"))
        pl = cb.pain_len(t)
        out.append((f"pain on {k}", spec([calm + self.ms_range(k, 1, pl - 1)], copy1()), "bus"))
        # The pain, the first walk cycle and an attack end in a fresh walk cycle... or the loop.
        out.append((f"fresh {k}", spec([calm + self.ms_range(k, pl, pl), calm + self.ms_range(k, ATTACK + n_a - 1, ATTACK + n_a - 1)],
                                       [sem.out(M, constant=FRESH)]), "bus"))
        no_att = [(ATT[k], "=", 0)]
        out.append((f"walk {k}", spec([calm + self.ms_range(k, FRESH, FRESH + n_w - 2),
                                       calm + no_att + self.ms_range(k, LOOP, LOOP + n_w - 2),
                                       calm + self.ms_range(k, ATTACK, ATTACK + n_a - 2)], copy1()), "bus"))
        out.append((f"loop {k}", spec([calm + self.ms_range(k, FRESH + n_w - 1, FRESH + n_w - 1),
                                       calm + no_att + self.ms_range(k, LOOP + n_w - 1, LOOP + n_w - 1)],
                                      [sem.out(M, constant=LOOP)]), "bus"))
        out.append((f"attack {k}", spec([calm + [(ATT[k], ">", 0)]], [sem.out(M, constant=ATTACK)]), "bus"))
        # Attack attempts at the loop's A_Chase ticks, in sight, the player alive.
        base = []
        for p in chase_ticks(t):
            base.append(calm + pa + seen + self.ms_range(k, LOOP + p, LOOP + p))
        cl = [b + extra for b in base for extra in self.bands(k, T["chance"], [])]
        out.append((f"decide {k}", spec(cl, [sem.out(ATT[k], constant=1)]), "net"))
        out.append((f"shoot {k}", spec(self.shot_clauses(k), [sem.out(PD, constant=T["damage"])]), "net"))
        # Dying and the corpse (combat.py's states).
        H = cb.hp(self.things[i][2])
        dead = [(cb.DMG[i], ">=", H)]
        out.append((f"die {k}", spec([dead + [(M, "<", cb.DEATH)], dead + [(M, ">=", FRESH)]],
                                     [sem.out(M, constant=cb.DEATH)]), "bus"))
        out.append((f"dying {k}", spec([self.ms_range(k, cb.DEATH, cb.corpse(t) - 1)], copy1()), "bus"))
        out.append((f"corpse {k}", spec([self.ms_range(k, cb.corpse(t), cb.corpse(t))], [sem.out(M, networks=RED)]), "bus"))
        return out

    def shot_clauses(self, k):
        """Monster k's shot hits the player: at its attack's fire tick, in sight (and within a
        tile for a scratch), the hit roll by distance band."""
        t, i = self.type(k), self.idx[k]
        calm = self.alive(k) + [(cb.DMG[i], "<=", LD[k])]
        at = calm + self.player_alive() + [(SEE[k], ">=", NS - 1)] + \
            self.ms_range(k, ATTACK + fire_tick(t), ATTACK + fire_tick(t))
        return self.bands(k, TYPES[t]["hit"], at)

    def damage_bits(self):
        """Clauses for the health the queue takes this tick: (bit 0, bit 1) of the decrement
        (armor saves 1: min(2, health); bare: min(3, health))."""
        h0, a0 = self.start["health"], self.start["armor"]
        H, A = self.sigs["health"], self.sigs["armor"]
        arm, bare = [(A, ">=", 1 - a0)], [(A, "<", 1 - a0)]
        due = self.player_alive() + [(PD, ">", 0), (PH, ">", 0)]
        hv = lambda comp, v: (H, comp, v - h0)  # noqa: E731
        bit0 = [due + arm + [hv("=", 1)], due + bare + [hv(">=", 3)], due + bare + [hv("=", 1)]]
        bit1 = [due + arm + [hv(">=", 2)], due + bare + [hv(">=", 2)]]
        return bit0, bit1

    def _damage(self):
        """The queue PD: drains TILE_DMG a tick while the player lives (held while dead), and the
        REG writers applying it: armor -1 while it lasts, health -(3 - save) down to 0."""
        h0, a0 = self.start["health"], self.start["armor"]
        H, A = self.sigs["health"], self.sigs["armor"]
        pa = self.player_alive()
        spec = lambda clauses, outs: sem.decider_spec(self.dnf(clauses), outs)  # noqa: E731
        # The phase toggles only while damage is queued (a free-running toggle would depend on how
        # long the game ran before: the harness's warm-up differs from fsim's by a tick).
        out = [("damage phase", spec([[(PH, "=", 0), (PD, ">", 0)]], [sem.out(PH, constant=1)]), "net"),
               ("damage queue", spec([pa + [(PD, ">", TILE_DMG), (PH, ">", 0)]], [sem.out(PD, networks=GREEN),
                                                                               sem.out(PD, constant=-TILE_DMG)]), "net"),
               ("damage held", spec([[(H, "<=", -h0), (PD, ">", 0)], pa + [(PD, ">", 0), (PH, "=", 0)]],
                                    [sem.out(PD, networks=GREEN)]), "net")]
        arm = [(A, ">=", 1 - a0)]
        due = pa + [(PD, ">", 0), (PH, ">", 0)]
        bit0, bit1 = self.damage_bits()
        out.append(("damage health 1", spec(bit0, [sem.out(H, constant=-1)]), "reg"))
        out.append(("damage health 2", spec(bit1, [sem.out(H, constant=-2)]), "reg"))
        out.append(("damage armor", spec([due + arm], [sem.out(A, constant=-1)]), "reg"))
        return out

    # -- line of sight (built into the graph: two ticks deep)
    def los(self, g, S):
        """Per monster: the sample points' x (red) and y (green) times NS * FP, a 2D range lookup
        over the walls whose else-output counts the free points onto the bus as SEE. Returns
        [(lookup, [its four products])] per monster (the lookups' outputs belong on the bus)."""
        lanes = LANE_SIGNALS[:NS - 1]
        SX, SY = self.sigs["SX"], self.sigs["SY"]
        kv = lambda f: g.const([(lanes[j - 1], f(j)) for j in range(1, NS)], "los vector")  # noqa: E731
        out = []
        for k in range(len(self.idx)):
            ex, ey = self.E[k]
            parts, prods = [], []
            for ax, (Ms, Ps, e0, sp) in enumerate(((MX[k], SX, ex, self.spawn[0]), (MY[k], SY, ey, self.spawn[1]))):
                pm = g.op("arith", sem.arith_spec(EACH, "*", Ms, EACH, GREEN, RED), red=[S], green=[kv(lambda j: NS - j)],
                          label=f"los m{'xy'[ax]}{k}")
                pp = g.op("arith", sem.arith_spec(EACH, "*", Ps, EACH, GREEN, RED), red=[S], green=[kv(lambda j: j)],
                          label=f"los p{'xy'[ax]}{k}")
                base = kv(lambda j: e0 * (NS - j) + sp * j)
                parts.append([pm, pp, base])
                prods += [pm, pp]
            conds = []
            q = NS * FP
            for x0, x1, y0, y1 in self.rects:
                conds += [sem.cond(EACH, ">=", x0 * q, RED, "or" if conds else None), sem.cond(EACH, "<", (x1 + 1) * q, RED, "and"),
                          sem.cond(EACH, ">=", y0 * q, GREEN, "and"), sem.cond(EACH, "<", (y1 + 1) * q, GREEN, "and")]
            out.append((g.decider(conds, [], [sem.out(SEE[k], constant=1)], red=parts[0], green=parts[1], label=f"los {k}"),
                        prods))
        return out


# ---------------------------------------------------------------------------------------------
# The rules in plain Python

def los_count(mon, k, bus):
    """SEE from a bus snapshot: sample points not in a wall."""
    ex, ey = mon.E[k]
    mx, my = ex + bus.get("MX", [0] * MAXMON)[k], ey + bus.get("MY", [0] * MAXMON)[k]
    px, py = bus["SX"] + mon.spawn[0], bus["SY"] + mon.spawn[1]
    q = NS * FP
    n = 0
    for j in range(1, NS):
        x, y = sem.i32(mx * (NS - j) + px * j), sem.i32(my * (NS - j) + py * j)
        if not any(x0 * q <= x < (x1 + 1) * q and y0 * q <= y < (y1 + 1) * q for x0, x1, y0, y1 in mon.rects):
            n += 1
    return n


def step(mon, bus, see):
    """One tick of the rules: from the bus at t - 1 (a dict: "SX", "SY", "health", "armor" (REG's
    offsets), "MX", "MY", "DX", "DY", "XQ", "YQ", "R", "LD", "ATT", "CF" (per monster lists), "MS", "DMG" (per
    thing index), "PD", "RND") and SEE (computed two ticks back), the values written at t: a dict
    of the same keys (positions, clocks, flags, PD, RND) and "inc": REG increments."""
    things = mon.things
    n = len(mon.idx)
    h0, a0 = mon.start["health"], mon.start["armor"]
    health, armor = bus["health"] + h0, bus["armor"] + a0
    pa = health > 0
    out = {"MX": list(bus["MX"]), "MY": list(bus["MY"]), "MS": list(bus["MS"]), "ATT": [0] * n, "PD": 0,
           "DX": [sem.i32(bus["SX"] + mon.off[k][0] - bus["MX"][k]) for k in range(n)],
           "DY": [sem.i32(bus["SY"] + mon.off[k][1] - bus["MY"][k]) for k in range(n)],
           "XQ": [sem.i32(v * v) for v in bus["DX"]], "YQ": [sem.i32(v * v) for v in bus["DY"]],
           "R": [sem.i32(m * bus["RND"]) for m in MULT[:n]], "LD": [bus["DMG"][i] for i in mon.idx],
           "RND": sem.i32((bus["RND"] + LCG_C) * LCG_A), "CF": [], "DT": [0] * n, "PH": int(bus["PH"] == 0 and bus["PD"] > 0),
           "inc": {}, "hits": 0, "shots": [0] * n}
    for k in range(n):
        ex, ey = mon.E[k]
        cf = []
        for axis in (0, 1):
            hx, hy = (BOX, SIDE) if axis == 0 else (SIDE, BOX)
            for sx, sy in CORNERS:
                x, y = ex + bus["MX"][k] + sx * hx, ey + bus["MY"][k] + sy * hy
                cf.append(int(any(x0 * FP <= x < (x1 + 1) * FP and y0 * FP <= y < (y1 + 1) * FP
                                  for x0, x1, y0, y1 in mon.rects)))
        out["CF"].append(cf)
    for k in range(n):
        i, t = mon.idx[k], mon.type(k)
        T = TYPES[t]
        ms, hp = bus["MS"][i], cb.hp(things[i][2])
        alive = bus["DMG"][i] < hp
        hit = bus["DMG"][i] > bus["LD"][k]
        calm = alive and not hit
        dx, dy = bus["DX"][k], bus["DY"][k]
        seen = see[k] >= NS - 1
        R = bus["R"][k]
        n_w, n_a, pl = walk_len(t), attack_len(t), cb.pain_len(t)
        chasing = FRESH <= ms < FRESH + n_w or LOOP <= ms < LOOP + n_w

        def in_band(b):
            return b >= len(BANDS) or (-BANDS[b] < dx < BANDS[b] and -BANDS[b] < dy < BANDS[b])

        def roll(probs):
            return any(in_band(b) and (threshold(p) is None or R < threshold(p)) for b, p in enumerate(probs))
        # moves (Monsters.move_clauses) and the detour (Monsters.detour_specs)
        if chasing and calm and pa:
            d, s, dt = (dx, dy), T["speed"], bus["DT"][k]
            xmajor = bus["XQ"][k] >= bus["YQ"][k]

            def corner(axis, w, c):
                return bus["CF"][k][4 * axis + CORNERS.index((w, c) if axis == 0 else (c, w))]

            def free(axis, w):
                return not corner(axis, w, 1) and not corner(axis, w, -1)
            far_any = abs(dx) >= STOP or abs(dy) >= STOP
            for ax in (0, 1):
                b = 1 - ax
                for u in (1, -1):
                    if not free(ax, u):
                        continue
                    step = False
                    if dt == u * (1 + ax):
                        step = far_any
                    elif dt in (1 + b, -(1 + b)):
                        step = u * d[ax] > DZ and (u * d[ax] >= STOP + s or abs(d[b]) >= STOP)
                    elif dt == 0 and xmajor == (ax == 0):
                        step = u * d[ax] >= STOP + s
                    elif dt == 0:
                        for w in (1, -1):
                            nudging = w * d[b] >= STOP + s and corner(b, w, -u) and not corner(b, w, u)
                            if w * d[b] >= STOP and u * d[ax] > DZ and not nudging:
                                step = True
                            if nudging:
                                step = True
                    if step:
                        out[("MX", "MY")[ax]][k] += u * s
            if dt == 0:
                a = 0 if xmajor else 1
                b = 1 - a
                wa = 1 if d[a] > DZ else -1 if d[a] < -DZ else 0
                no_way = abs(d[b]) <= DZ or (d[b] > DZ and not free(b, 1)) or (d[b] < -DZ and not free(b, -1))
                if wa and corner(a, wa, 1) and corner(a, wa, -1) and no_way:
                    for e in (1, -1):
                        if free(b, e) and ((R >= 0) == (e > 0) or not free(b, -e)):
                            out["DT"][k] = e * (1 + b)
            else:
                b = 0 if abs(dt) == 1 else 1
                a, e = 1 - b, (1 if dt > 0 else -1)
                stuck = any(w * d[a] > DZ and not free(a, w) for w in (1, -1))
                if stuck:
                    out["DT"][k] = dt if free(b, e) else -dt
        # the clock
        fx, fy = FACING.get(i, (0, 0))
        front = (not fx and not fy) or (fx and fx * dx >= 0) or (fy and fy * dy >= 0)
        if not alive:
            if ms < cb.DEATH or ms >= FRESH:
                nm = cb.DEATH
            elif ms < cb.corpse(t):
                nm = ms + 1
            else:
                nm = ms
        elif hit:
            nm = 1
        elif ms == 0:
            nm = FRESH if pa and seen and (front or in_band(0)) else 0
        elif 1 <= ms < pl:
            nm = ms + 1
        elif ms == pl or ms == ATTACK + n_a - 1:
            nm = FRESH
        elif FRESH <= ms < FRESH + n_w - 1 or ATTACK <= ms < ATTACK + n_a - 1:
            nm = ms + 1
        elif ms == FRESH + n_w - 1:
            nm = LOOP
        elif bus["ATT"][k] > 0:   # (only ever set from the loop)
            nm = ATTACK
        elif LOOP <= ms < LOOP + n_w - 1:
            nm = ms + 1
        elif ms == LOOP + n_w - 1:
            nm = LOOP
        else:
            nm = ms   # not reached
        out["MS"][i] = nm
        if calm and pa and seen and ms - LOOP in chase_ticks(t) and LOOP <= ms < LOOP + n_w and roll(T["chance"]):
            out["ATT"][k] = 1
        if calm and pa and seen and ms == ATTACK + fire_tick(t) and roll(T["hit"]):
            out["PD"] += T["damage"]
            out["hits"] += 1
            out["shots"][k] = 1
    # the damage queue: drains every other tick (PH)
    pd, ph = bus["PD"], bus["PH"] > 0
    if pa and pd > TILE_DMG and ph:
        out["PD"] += pd - TILE_DMG
    elif pd > 0 and (not pa or not ph):
        out["PD"] += pd
    if pa and pd > 0 and ph:
        save = 1 if armor >= 1 else 0
        out["inc"] = {"health": -min(TILE_DMG - save, health), **({"armor": -1} if save else {})}
    return out
