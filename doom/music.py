#!/usr/bin/env python3
"""Music: a MIDItorio song (https://miditorio.com) on a loop, starting over on a restart.

MIDItorio turns a MIDI file into a blueprint:
  - Two constant combinators hold the song. On each lane (a signal at a quality), one holds a time
    in ticks, the other the events at that time: up to two (instrument address, note) pairs packed
    into the value.
  - A clock counts ticks: a red self-loop, plus 1 from a constant combinator (its on switch).
  - A decider passes the lanes whose time is now (`Each(red) = signal-green`).
  - Arithmetic combinators unpack the events.
  - Per instrument channel, a decider picks that channel's note (`Each(red) = address` -> the
    speaker signal, the matching lane's note from green). Its programmable speaker plays it
    (global playback, so it is heard anywhere).

The song plays once: its clock counts on forever. Here the clock decider is rewired as a counter
on a green loop (its own count, plus 1 from the constant), which counts while
  - the count is at most the song's length (its last event's time). Past it, it drops to 0, so the
    song loops;
  - RESTART = 0 on the state bus (red). A new game, or the reset button, starts the song over;
  - the constant is on (ON, `signal-check`, beside the 1). Switched off, the music stops; on again,
    it starts from the top.
Everything else in the song's blueprint is kept as it is.

The song comes from a file of the player's (`game.py --music FILE`: any MIDItorio blueprint string),
not from the repository: only original or freely licensed content goes there.

`python3 doom/music.py FILE --verify` builds the song with a stand-in bus (RESTART on a constant
combinator, scripted) and checks every speaker decider, every tick, against the song's events.
`--loop TICKS` shortens the loop, so a game run (harness/run.py --dlc) can cover it.
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from compiler.bp import GREEN_IN, GREEN_OUT, RED_IN, Blueprint, decode, encode, quality_arg, sig  # noqa: E402
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import GREEN, RED  # noqa: E402

TIME = sig("signal-green")    # MIDItorio's clock signal: ticks since the song started
ON = sig("signal-check")      # on the clock's constant: the music's on switch
COMBINATORS = ("decider-combinator", "arithmetic-combinator", "selector-combinator")


def size(e):
    """(width, height) in tiles."""
    if e["name"] in COMBINATORS:
        return (2, 1) if e.get("direction") in (4, 12) else (1, 2)
    return (1, 1)


def _filters(e):
    for s in e.get("control_behavior", {}).get("sections", {}).get("sections", []):
        yield from s.get("filters", [])


def _lane(f):
    return (f.get("type", "item"), f["name"], f.get("quality", "normal"))


def _arith(v, a):
    """An arithmetic combinator's Each op on a lane value (the ops MIDItorio uses; int32)."""
    k, op = a["second_constant"], a["operation"]
    if op == "AND":
        return v & k
    if op == "/":
        return int(v / k)
    if op == "%":
        return v - k * int(v / k)
    if op == ">>":
        return v >> k
    raise ValueError(f"unexpected arithmetic op {op}")


class Song:
    def __init__(self, text):
        bp = decode(text)["blueprint"]
        self.label = bp.get("label", "song")
        self.entities = {e["entity_number"]: e for e in bp["entities"]}
        self.wires = [list(w) for w in bp.get("wires", [])]
        E = self.entities

        def kind(n):
            return E[n]["name"]

        def dc(n):
            return E[n].get("control_behavior", {}).get("decider_conditions", {})

        clocks = [n for n in E if kind(n) == "decider-combinator"
                  and any(o["signal"].get("name") == "signal-everything" for o in dc(n).get("outputs", []))
                  and any(c.get("first_signal", {}).get("name") == TIME["name"] for c in dc(n)["conditions"])]
        if len(clocks) != 1:
            raise SystemExit(f"not a MIDItorio song: {len(clocks)} clocks")
        self.clock = clocks[0]
        starts = {a for w in self.wires for a, b in ((w[0], w[2]), (w[2], w[0]))
                  if b == self.clock and kind(a) == "constant-combinator"
                  and [f["name"] for f in _filters(E[a])] == [TIME["name"]]}
        if len(starts) != 1:
            raise SystemExit("not a MIDItorio song: no start constant on the clock")
        (self.start,) = starts

        # Networks: (entity, connector) joined by wires (1/3 red, 2/4 green; 1/2 on a constant).
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, ca, b, cb in self.wires:
            parent[find((a, ca))] = find((b, cb))
        self.net = find
        members = {}
        for x in list(parent):
            members.setdefault(find(x), []).append(x)
        self.members = lambda n, c: members.get(find((n, c)), [(n, c)])  # noqa: E731

        # The time match deciders, and the song's lanes: the time on the constants on their red input,
        # the events on those on their green input.
        self.matches = [n for n in E if kind(n) == "decider-combinator"
                        and any(c.get("second_signal", {}).get("name") == TIME["name"] for c in dc(n)["conditions"])]
        self.lanes = []   # (time, events value)
        for m in self.matches:
            consts = lambda c: [n for n, _ in self.members(m, c) if kind(n) == "constant-combinator"  # noqa: E731
                                and n != self.start]
            times = {_lane(f): f["count"] for n in consts(RED_IN) for f in _filters(E[n])}
            events = {_lane(f): f["count"] for n in consts(GREEN_IN) for f in _filters(E[n])}
            self.lanes += [(t, events.get(k, 0)) for k, t in times.items()]
        self.length = max(t for t, _ in self.lanes)

        # Per speaker its decider (address on red, note on green), and the arithmetic combinators
        # unpacking the events onto those two networks.
        self.channels = []   # (decider, address, red arith, green arith)
        for a, _, b, _ in self.wires:
            for d, s in ((a, b), (b, a)):
                if kind(d) == "decider-combinator" and kind(s) == "programmable-speaker":
                    feed = [[E[n]["control_behavior"]["arithmetic_conditions"] for n, c in self.members(d, cin)
                             if kind(n) == "arithmetic-combinator" and c in (3, 4)] for cin in (RED_IN, GREEN_IN)]
                    if len(feed[0]) != 1 or len(feed[1]) != 1:
                        raise SystemExit(f"unexpected wiring at speaker decider {d}")
                    self.channels.append((d, dc(d)["conditions"][0]["constant"], feed[0][0], feed[1][0]))
        self.channels.sort()
        self.speakers = sum(1 for n in E if kind(n) == "programmable-speaker")

        tiles = {}
        for n, e in E.items():
            w, h = size(e)
            tx, ty = int(e["position"]["x"] - w / 2 + 0.5 + 1e9) - 10 ** 9, int(e["position"]["y"] - h / 2 + 0.5 + 1e9) - 10 ** 9
            tiles[n] = (tx, ty)
        x0, y0 = min(t[0] for t in tiles.values()), min(t[1] for t in tiles.values())
        self.tiles = {n: (tx - x0, ty - y0) for n, (tx, ty) in tiles.items()}
        self.w = max(tx + size(E[n])[0] for n, (tx, ty) in self.tiles.items())
        self.h = max(ty + size(E[n])[1] for n, (tx, ty) in self.tiles.items())

    def place(self, b, x, y, restart, loop=None):
        """Add the song to b with its top-left tile at (x, y). Its clock loops after `loop` ticks
        (default: the last event's time) and starts over while `restart` (a signal on its red input)
        is set. Returns {song entity number: b's}, the clock's red input to wire to the bus."""
        loop = loop or self.length
        ids = {}
        for n, e in sorted(self.entities.items()):
            e = json.loads(json.dumps(e))
            w, h = size(e)
            tx, ty = self.tiles[n]
            ids[n] = b._add(e.pop("name"), x + tx + w / 2, y + ty + h / 2,
                            **{k: v for k, v in e.items() if k not in ("entity_number", "position")})
        clock, start = b.entities[ids[self.clock] - 1], b.entities[ids[self.start] - 1]
        # The count, plus 1 from the constant (both on green), while it's within the loop, the bus
        # has no RESTART and the constant is on.
        clock["control_behavior"] = {"decider_conditions": {
            "conditions": [sem.cond(TIME, "<=", loop, GREEN), sem.cond(ON, ">", 0, GREEN, compare_type="and"),
                           sem.cond(restart, "=", 0, RED, compare_type="and")],
            "outputs": [sem.out(TIME, networks=GREEN)]}}
        clock["player_description"] = (
            f"Music clock: {TIME['name']} counts the ticks since the song started (its own count on the green "
            f"loop, plus 1 from the constant). It drops to 0 after {loop} ticks (the song loops) and on "
            f"{restart['name']} on the state bus (red): a new game starts the song over.")
        start["control_behavior"] = {"sections": {"sections": [{"index": 1, "filters": [
            {"index": 1, **TIME, "quality": "normal", "comparator": "=", "count": 1},
            {"index": 2, **ON, "quality": "normal", "comparator": "=", "count": 1}]}]}}
        start["player_description"] = (
            f"[item=programmable-speaker] [font=heading-1]{self.label}[/font]\n\nMusic on/off: switched off, "
            f"the music stops; on again, it starts from the top.\n\nConverted using MIDItorio (miditorio.com).")
        for a, ca, bb, cb in self.wires:
            if a == self.clock and bb == self.clock:
                continue   # the red self-loop: the count is on green now
            b.wire(ids[a], ca, ids[bb], cb)
        b.wire(ids[self.clock], GREEN_OUT, ids[self.clock], GREEN_IN)
        return ids

    def expected(self, tau):
        """{speaker decider (song number): its output} when the clock network shows time tau."""
        on = [v for t, v in self.lanes if t == tau]
        out = {}
        for d, addr, ra, ga in self.channels:
            v = sum(_arith(x, ga) for x in on if _arith(x, ra) == addr)
            if v:
                out[d] = v
        return out


def clock_rule(prev, restart, on, loop):
    """The clock's next count from its previous one (the network shows prev + 1)."""
    return prev + 1 if on and prev + 1 <= loop and not restart else 0


# The stand-in bus's script: (tick, RESTART); the on switch's: (tick, ON).
RESTARTS = [(400, 1), (401, 0), (1500, 1), (1501, 0)]
SWITCH = [(900, 0), (1000, 1)]


def main():
    ap = argparse.ArgumentParser(description="Build a MIDItorio song with the game's looping clock.")
    ap.add_argument("song", help="a MIDItorio blueprint string (file)")
    ap.add_argument("--loop", type=int, help="loop after this many ticks (default: the song's length)")
    ap.add_argument("--ticks", type=int, help="ticks to check (default: two loops and a bit)")
    ap.add_argument("--verify", action="store_true", help="simulate in fsim and check every speaker decider")
    quality_arg(ap)
    a = ap.parse_args()
    song = Song(pathlib.Path(a.song).read_text())
    loop = a.loop or song.length
    print(f"{song.label}: {len(song.lanes)} lanes, {song.speakers} speakers, {len(song.channels)} channels, "
          f"last event at {song.length} ticks ({song.length / 60:.1f} s), {song.w}x{song.h} tiles")
    b = Blueprint(f"Music {song.label}", a.substation_quality)
    restart = sig("signal-Z")
    bus = b.constant(0, 0, [(restart, 0)], description="stand-in state bus")
    b.entities[bus - 1]["control_behavior"]["sections"]["sections"][0]["filters"] = []
    ids = song.place(b, 2, 0, restart, loop)
    b.wire(bus, 1, ids[song.clock], RED_IN)
    b.power()
    b.link_power()
    b.check_reach()
    ticks = a.ticks or max(2 * loop + 400, 1700)
    inserts = [[k, bus, "signal-Z", v] for k, v in RESTARTS] + [[k, ids[song.start], ON["name"], v] for k, v in SWITCH]
    data = b.to_json()
    data["fsim"] = {"ticks": ticks, "doc": f"music {song.label} loop {loop}", "inserts": inserts}
    gen = HERE / "gen"
    gen.mkdir(exist_ok=True)
    path = gen / f"music_loop{loop}.json"
    path.write_text(json.dumps(data, separators=(",", ":")))
    (gen / f"music_loop{loop}.txt").write_text(encode(b.to_json()) + "\n")
    print(f"{path.relative_to(ROOT)}: {len(b.entities)} entities")
    if not a.verify:
        return
    ins_path = path.with_suffix(".inserts.json")
    ins_path.write_text(json.dumps(inserts))
    probes = [ids[song.clock]] + [ids[d] for d, *_ in song.channels]
    res = subprocess.run([str(ROOT / "target" / "release" / "fsim"), "run", str(path), "--ticks", str(ticks),
                          "--input", str(ins_path)] + [x for p in probes for x in ("--probe", str(p))],
                         check=True, capture_output=True, text=True)
    got = {}
    for line in res.stdout.splitlines():
        m = re.match(r"t=(\d+) #(\d+) [\w-]+: ?(.*)", line)
        if m:
            got[(int(m.group(1)), int(m.group(2)))] = sum(int(kv.split("=")[1]) for kv in m.group(3).split())
    # Reference. A script entry at k is on the clock's network at k (fsim's tick numbering: a
    # network at t shows the outputs at t - 1, and a combinator's output at t comes from it).
    restart_at = {k for k, v in RESTARTS if v}
    on, sw, prev = 1, dict(SWITCH), 0
    clock = [0] * ticks
    for t in range(ticks):   # a combinator's output at t comes from its networks at t (outputs at t - 1)
        if t in sw:
            on = sw[t]
        clock[t] = prev = clock_rule(prev, t in restart_at, on, loop)
    bad_clock = [t for t in range(ticks) if got.get((t, ids[song.clock]), 0) != clock[t]]
    # The speakers' deciders: the clock network shows count + 1 (the constant's 1); the match
    # decider, the unpacking and the channel decider take a tick each.
    bad, notes = [], 0
    for t in range(3, ticks):
        tau = clock[t - 3] + 1
        want = song.expected(tau)
        notes += len(want)
        have = {d: got.get((t, ids[d]), 0) for d, *_ in song.channels}
        if have != {d: want.get(d, 0) for d, *_ in song.channels}:
            bad.append((t, tau, have, want))
    loops = sum(1 for t in range(1, ticks) if clock[t] == 0 and clock[t - 1] == loop)
    print(f"clock: {ticks} ticks, {len(bad_clock)} differ {bad_clock[:5]}; {loops} loops, restarts at "
          f"{sorted(restart_at)}, off {SWITCH[0][0]}..{SWITCH[1][0]}")
    print(f"speaker deciders: {len(bad)} ticks differ; {notes} notes played")
    for x in bad[:5]:
        print("  ", x)
    if bad or bad_clock:
        sys.exit(1)


if __name__ == "__main__":
    main()
