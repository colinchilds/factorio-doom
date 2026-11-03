# factorio-doom

A Doom-style first-person shooter built from Factorio 2.1 combinators (no mods; Space Age for
signal quality, which the screen uses: six rows per network) and shown on a lamp screen. See [CLAUDE.md](CLAUDE.md) for the goal, the planned design, and status.

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
- **Sprite demo** (`demos/sprite.py`): a Freedoom monster (BSD-licensed art, `assets/freedoom/`)
  walking toward the camera, drawn by combinators at any `--size`. The `.txt` file it writes is a
  blueprint string to paste into the game.
- **Raycaster** (`demos/raycast.py`): Freedoom-textured walls, floors and ceilings rendered
  entirely by combinators at any `--size` (about 7,600 at 160×100), six screen rows per network
  (one per signal quality, so it needs Space Age), built with the netlist compiler
  (`compiler/`), which balances latency, places and wires the circuit, and evaluates it as the
  reference.
- **The game** (`demos/game.py`): the raycaster driven by a chest input pad (god mode, drop
  items with Z): walk, turn (slowly at first, for aiming), slide along walls. Freedoom monsters
  and items stand on the map (`demos/things.py`), hidden behind nearer walls, and items are picked
  up (`demos/touch.py`). Monsters (`demos/monsters.py`) sleep until they see you in front of them
  or get shot, then walk at you around walls, and attack on Doom's timing: the zombieman shoots
  (often missing at range), the imp scratches up close and throws fireballs (no projectiles yet:
  half of them hit). Any hit makes a monster flinch, which stops its attack, so punching works. A Freedoom status bar (`demos/hud.py`) shows
  ammo, health, armor and the face. Seven Freedoom weapons (`demos/weapons.py`): the gun in front
  of the view, "next weapon" switches, fire plays Doom's animation and uses ammo. Shots hit the
  nearest live monster in the middle of the view (`demos/combat.py`), which flinches with its
  pain frame and dies with Freedoom's death animation, leaving a corpse. At 0 health you're dead
  (frozen, gun lowered); press Use to start a new game.
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
python3 conformance/gen.py                        # regenerate conformance/cases/*.json (and cases_dlc/)
python3 harness/run.py conformance/cases -o conformance/game_dump.json   # ~2 s
./target/release/fsim conform                     # every case must PASS
# Space Age cases (quality selectors, quality pole reach): run with the DLC
python3 harness/run.py --dlc conformance/cases_dlc -o conformance/game_dump_dlc.json
./target/release/fsim conform conformance/cases_dlc --dump conformance/game_dump_dlc.json

# Any blueprint can be checked the same way
python3 harness/run.py demos/gradient.json -o demos/golden/gradient_dump.json
./target/release/fsim conform demos --dump demos/golden/gradient_dump.json

# UPS benchmarks: synthetic row-stage screens, timed in the game with recording off
python3 bench/m0.py 160x100 320x200               # -> bench/gen/*.json
python3 harness/run.py --bench 300 bench/gen/*_160x100.json -o bench/m0_160x100.json

# Raycaster (M3: Freedoom-textured walls): generate, check sampled frames against the evaluation
python3 demos/raycast.py --size 160x100 --verify   # -> demos/gen/raycast_160x100.{json,txt}
python3 demos/raycast.py --size 64x40 --tex 32 --verify --every 1   # smaller textures, every frame
python3 demos/raycast.py --size 160x100 --flat 0   # untextured floor/ceiling (cheaper); --flat 8/16/32
python3 demos/game.py --size 320x200 --gamma 0.4 --fog 12   # brighter screen (--gamma 1 --saturation 1 --fog 4: Freedoom's colors)
python3 demos/raycast.py --size 160x100 --no-things   # without the monsters and items (also game.py)
python3 demos/game.py --size 160x100 --no-hud      # without the status bar: the view takes the whole screen
python3 demos/game.py --size 160x100 --no-weapon   # without the weapon in front of the view
python3 demos/weapons.py                           # weapon timelines and the overlay's cost per size
python3 demos/game.py --size 160x100 --turn-slow 1 --turn 4 --turn-ramp 18   # turning: per tick, slow then fast
python3 demos/game.py --size 160x100 --fov 90 --aspect 1.2 --eye 40   # Doom's view (default: 67 degrees, 1.2, 40)
python3 demos/game.py --size 160x100 --aspect 0 --eye 64   # the view before 2026-11-03 (squat, eye half way up)
python3 harness/run.py --warmup 60 out/dir -o dump.json   # deep pipelines: settle before t0
python3 harness/run.py --dlc --warmup 60 --every 2 out/dir -o dump.json   # long runs: record every 2nd tick (memory)
# Substations default to legendary (28x28 supply, fewer dead pixels). The screen needs Space Age
# (quality lanes), so game checks run with --dlc: harness/run.py --dlc --bench 30 demos/gen/raycast_160x100.json

# Input pad (god mode, drop items with Z): check the decoder against its reference model
python3 demos/input_pad.py --size 320x200 --verify   # -> demos/gen/input_pad_n9.{json,txt}
./target/release/fsim run demos/gen/input_pad_n9.json --input inserts.json --probe 12  # [[tick, entity, item, count]]

# The game (M2 movement + M3 textured walls, floors, ceilings): the raycaster driven by the pad (walk, turn, collide);
# player state vs reference, frames
python3 demos/game.py --size 160x100 --verify      # -> demos/gen/game_160x100.{json,txt}

# Sprite demo: generate, check every frame against the reference, preview as a GIF
python3 demos/sprite.py --size 160x100 --verify    # -> demos/gen/sprite_160x100.{json,txt}
./target/release/fsim run demos/gen/sprite_160x100.json --every 2 --scale 4 --gif out/sprite.gif
./target/release/fsim run demos/gen/sprite_160x100.json --ppm out/ppm   # raw frames for scripts
python3 tools/wad.py freedoom2.wad --list TROO     # sprites in a WAD (see assets/freedoom/)
python3 tools/wad.py freedoom2.wad --textures BRICK  # wall textures; --texture-png DIR NAME... previews
python3 tools/wad.py freedoom2.wad --flats FLOOR     # floor/ceiling flats; --flat-png DIR NAME... previews

python3 harness/run.py --dlc --bench 30 demos/gen/sprite_160x100.json   # imports in Space Age?
python3 harness/run.py --lanes                    # refresh conformance/lane_signals.json
python3 harness/run.py --poles                    # refresh conformance/pole_quality.json (DLC)
python3 harness/run.py --signals                  # refresh conformance/signal_order.json
python3 harness/run.py --discover                 # export a blueprint with every option set
```

### Keyboard input (optional mod)

`mod/doom-input` drives the blueprint from the keyboard: movement keys (WASD, or the arrows once
bound as alternative movement keys in Settings > Controls) walk and turn, Space fires, the game's
next-weapon key switches, F is use. Toggle it with the joystick button on the shortcut bar (or
Ctrl+Shift+J); while on, the camera stays put. Without the mod the blueprint uses its chest pad.

```sh
ln -s "$PWD/mod/doom-input" ~/Library/Application\ Support/factorio/mods/doom-input   # macOS
python3 tools/mod_icon.py                         # redraw the shortcut icon
python3 demos/game.py --size 64x40 --keys --verify   # the scripted walk typed on the keyboard
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
| Decider `Each` ranges and outputs | Several `Each` conditions in one decider are evaluated per lane together, so `lo <= Each AND Each < hi OR ...` is a per-lane range lookup. Several outputs on the same lane add up: `Each = constant` plus `Each` copied from a wire gives constant + value, in either branch (`decider_ranges` case). |
| Decider `Each` lanes | An `Each` decider visits the union of the lanes on every wire read by any `Each` operand of any condition. A condition on green (e.g. `Each(green) != 0`, ANDed in) brings in lanes that are absent (0) on red, so a range test on red can match 0 there (`decider_lanes` case). |
| Decider logic | AND binds tighter than OR. `Everything` is vacuously true and `Anything` is false when there are no inputs. `Everything` output under `Each` conditions gives nothing. |
| Selector | Ties keep the game's internal signal order when selecting the max; the min list is the max list reversed. `index_comparator` supports `<`, `<=` and so on (N-highest / N-lowest). The index can come from a signal on either wire; a negative index gives nothing. Count and time modes are also covered. |
| Signal order | The game's internal order (items, fluids, virtual, recipes, entities, …), saved in `conformance/signal_order.json`. Vanilla has **2,878 signal lanes** (1,439 signals × normal / quality-unknown). |
| Lamps | Packed RGB = `(v>>16)&255, (v>>8)&255, v&255`. Components are clamped to 0–255. Color mapping uses the first positive color signal in prototype order; with none, the lamp shows its default color. A lamp with no wires ignores its circuit settings. |
| Wire reach | A wire connects when the straight-line **gap between the two tile footprints** is ≤ the *shorter* reach of its two ends; **between two poles it's the distance between their centers** instead. Combinators, lamps and constants reach 9, small poles 7.5, medium 9, big 32, substations 18. Longer wires are silently dropped when a blueprint is built; fsim warns about them. Quality raises reach and supply area (`conformance/pole_quality.json`): a legendary substation supplies 28×28 and reaches 28, a legendary medium pole reaches 19. |
| Per-entity wire selection (2.1.7) | `input_networks` / `output_networks` don't exist for combinators (they always choose wires per signal); only lamps and similar entities have them. |

## Not simulated yet

- Selector modes: random, stack size, rocket capacity, the quality modes, and time mode's day signals.
- Power: fsim assumes everything is powered. The harness reports any unpowered entity as a harness error.
- Lamps' 2.1.7 `input_networks` setting, quality-scaled wire reach, and display panels.
