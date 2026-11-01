# factorio-doom

Groundwork for a Doom-style first-person shooter built from vanilla Factorio 2.1 combinators and
shown on a lamp screen. See [CLAUDE.md](CLAUDE.md) for the goal, the planned design, and status.

The tools so far:

- **`fsim`** (`crates/fsim`): a tick-accurate circuit-network simulator. It reads a blueprint
  (string or JSON), simulates every wire network and combinator, and renders the lamps as
  PNG/GIF frames. It can also print any combinator output or network value each tick.
- **Harness** (`harness/`): runs blueprints in the real game, windowless (`--benchmark`), with an
  isolated config and mod directory. It records every combinator output and lamp color per tick.
- **Conformance suite** (`conformance/`): small blueprints that pin down the game's semantics.
  The harness runs them in Factorio, `fsim conform` runs them in the simulator, and the two
  traces must match exactly. The game's recordings are kept as golden files, so the comparison
  also runs offline (`cargo test`).
- **Benchmarks** (`bench/`): M0 UPS measurements. A 320×200 lamp screen running a 9-op pixel
  pipeline costs about 1 ms per tick in the game. CLAUDE.md has the table.
- **Demos** (`demos/`): `gradient.py` builds a 16×16 lamp screen laid out like the planned
  renderer's row stage. fsim and the game agree on it lamp for lamp.

## Usage

```sh
cargo build --release
cargo test --release                              # fsim vs. the golden game recordings

# Simulate and render lamps (one PNG per captured tick, and/or a GIF)
./target/release/fsim run demos/gradient.json --ticks 80 --every 4 --scale 12 \
    --frames out/frames --gif out/run.gif
# Debug: print an entity's output, or a network (ENTITY:CONNECTOR), every tick
./target/release/fsim run some.bp --ticks 10 --probe 12 --net 12:1
./target/release/fsim decode some.bp              # blueprint string -> JSON

# Conformance against the real game (needs Factorio 2.1 installed)
python3 conformance/gen.py                        # regenerate conformance/cases/*.json
python3 harness/run.py conformance/cases -o conformance/game_dump.json   # ~2 s
./target/release/fsim conform                     # every case must PASS

# Any blueprint can be checked the same way
python3 harness/run.py demos/gradient.json -o demos/golden/gradient_dump.json
./target/release/fsim conform demos --dump demos/golden/gradient_dump.json

# UPS benchmarks: synthetic row-stage screens, timed in the game with recording off
python3 bench/m0.py 160x100 320x200               # -> bench/gen/*.json
python3 harness/run.py --bench 300 bench/gen/*_160x100.json -o bench/m0_160x100.json

python3 harness/run.py --signals                  # refresh conformance/signal_order.json
python3 harness/run.py --discover                 # export a blueprint with every option set
```

Set `FACTORIO` to use a different game binary. The default is the Steam install on macOS.
Wire connector ids in blueprint `wires` are 1/2 = red/green input (or the only connector, on
lamps, poles and constant combinators), 3/4 = red/green combinator output, 5 = copper.

## Verified semantics (Factorio 2.1.23)

Each of these was measured in the game and is checked by a conformance case.

| Topic | Behavior |
|---|---|
| Timing | Every network is the sum of what feeds it; every combinator outputs one tick after it reads: `N(t) = constants + O(t-1)`, `O(t) = f(N(t))`. Lamp *color* follows its input one tick sooner than its *on/off condition* does. |
| `Each(red) op Each(green)` | Works lane by lane. The lanes are the union of signals on both wires; a side that's missing counts as 0. |
| Lanes | Lanes come only from the wires the `Each` operands read. A signal at +5 red / −5 green is **not** a lane for `Each(both)`. |
| Arithmetic | int32 that wraps. `/` and `%` truncate toward zero; x/0 → 0, `INT_MIN / -1` → 0. Shift amounts are masked to 5 bits, and `>>` keeps the sign. `^` wraps; a negative exponent gives 0 unless the base is ±1. |
| Named output from `Each` | Arithmetic sums the lanes, wrapping. A decider outputs constant × the matching lane count, or the sum of the matching lanes' values when copying from input. |
| Decider else-output | Per lane: lanes that fail the condition get the else outputs. With no lanes at all, neither branch outputs. |
| Decider logic | AND binds tighter than OR. `Everything` is vacuously true and `Anything` is false when there are no inputs. `Everything` output under `Each` conditions gives nothing. |
| Selector | Ties keep the game's internal signal order when selecting the max; the min list is the max list reversed. `index_comparator` supports `<`, `<=` and so on (N-highest / N-lowest). The index can come from a signal on either wire; a negative index gives nothing. Count and time modes are also covered. |
| Signal order | The game's internal order (items, fluids, virtual, recipes, entities, …), saved in `conformance/signal_order.json`. Vanilla has **2,878 signal lanes** (1,439 signals × normal / quality-unknown). |
| Lamps | Packed RGB = `(v>>16)&255, (v>>8)&255, v&255`. Components are clamped to 0–255. Color mapping uses the first positive color signal in prototype order; with none, the lamp shows its default color. A lamp with no wires ignores its circuit settings. |
| Wire reach | A wire connects when the straight-line **gap between the two tile footprints** is ≤ the *shorter* reach of its two ends. Combinators, lamps and constants reach 9, small poles 7.5, medium 9, big 32, substations 18. Longer wires are silently dropped when a blueprint is built; fsim warns about them. |
| Per-entity wire selection (2.1.7) | `input_networks` / `output_networks` don't exist for combinators (they always choose wires per signal); only lamps and similar entities have them. |

## Not simulated yet

- Selector modes: random, stack size, rocket capacity, the quality modes, and time mode's day signals.
- Power: fsim assumes everything is powered. The harness reports any unpowered entity as a harness error.
- Lamps' 2.1.7 `input_networks` setting, quality-scaled wire reach, and display panels.
