# factorio-doom

A Doom-style first-person shooter built entirely from Factorio 2.1 combinators and shown on a lamp
screen. No mods, no CPU: a pipelined raycaster made of combinators draws a new frame every tick.

- Freedoom-textured walls, floors and ceilings, with distance fog
- Monsters that wake, chase you around walls, attack on Doom's timing, flinch and die
- Seven weapons with Doom's animations and muzzle flashes, pickups, a full status bar with the face
- Death, restart, and looping music
- Any screen size: 64×40 up to Doom's own 320×200

Art comes from [Freedoom](https://freedoom.github.io/) (BSD). The maps are original.

## Requirements

- **Factorio 2.1 with Space Age.** The blueprint is vanilla, but the screen stores six rows per
  wire network on Space Age's signal qualities.
- **Python 3.10+** (standard library only) to generate blueprints.
- **Rust** (optional) to build `fsim`, the simulator used by the `--verify` checks.

## Quick start

Ready-made blueprint strings are in [`blueprints/`](blueprints/) (64×40, 160×100 and 320×200). To
build your own:

```sh
python3 doom/game.py --size 160x100     # writes doom/gen/game_160x100.txt
```

1. Copy the blueprint string and import it as a blueprint in a Space Age game (the map editor,
   `/editor`, is easiest).
2. Place it and connect one power source to any of its substations; the blueprint wires its own
   power network.
3. Play from the input pad right of the screen: with an item in hand, hover over a button and drop
   one item (Z). Holding Z repeats.

| Pad | |
|---|---|
| D-pad | Forward, back, turn left and right on the edges; combinations on the corners |
| Center (crosshair) | Fire |
| 1–7 | Weapons, in Doom's slots (1 fist … 7 BFG) |
| U | Use: start a new game after dying |
| R (iron chests, right) | Reset: a new game at any time |

`--size 320x200` (Doom's resolution) makes about 80,000 entities and an 8 MB blueprint string, and runs
at about 1.2 ms per tick of the game's 16.7 ms budget.

## Options

```sh
python3 doom/game.py --help
```

| Option | Default | |
|---|---|---|
| `--size WxH` | 160x100 | Screen size in lamps |
| `--map NAME` | arena | `arena`, `acid` (a tunnel into an acid hall) or `test` |
| `--music FILE` | music/level1.txt | A [MIDItorio](https://miditorio.com) blueprint string: loops, restarts with each new game (`--no-music`: none) |
| `--fov`, `--aspect`, `--eye` | 67, 1.2, 40 | Field of view (degrees), pixel aspect, eye height (Doom units) |
| `--gamma`, `--saturation`, `--fog` | 0.5, 1.25, 8 | Color grade and fog distance (`1 1 4`: Freedoom's own colors) |
| `--muzzle-light` | 6 | Brightening while a weapon flashes (0 = none) |
| `--tex`, `--flat` | 64, 16 | Wall and floor texture resolution (`--flat 0`: untextured, cheaper) |
| `--no-things`, `--no-hud`, `--no-weapon` | | Leave out monsters and items, the status bar, the gun |
| `--turn-slow`, `--turn`, `--turn-ramp` | 1, 4, 18 | Turn speed while a button is first held, after the ramp, and the ramp's length in ticks |
| `--substation-quality` | legendary | Fewer dead pixels; `normal` for game checks without Space Age |

**Music:** the default song is a fan-made MIDI recreation of Doom's first-level theme. For another,
convert a MIDI file with MIDItorio, save the blueprint string to a file and pass
`--music music/song.txt`. The song's constant combinator switches it off and on in the game.

## Keyboard mod (optional)

The pad works without mods, but `mod/doom-input` lets you play from the keyboard: the movement keys
walk and turn, Space fires, 1–7 pick a weapon, F is use and Ctrl+Shift+R resets. Toggle it with the
joystick button on the shortcut bar (or Ctrl+Shift+J); while it's on, the camera stays put.

```sh
ln -s "$PWD/mod/doom-input" ~/Library/Application\ Support/factorio/mods/doom-input   # macOS
```

The arrow keys work once bound as alternative movement keys (Settings > Controls). The number keys
still select quickbar slots; rebind any key under Settings > Controls > Mods.

## How it works

The circuit network is used as a SIMD machine: each signal is a lane, a wire network is a vector
register and a combinator is a one-tick vector op. The renderer's lanes are screen columns. For each
column it marches the ray across every grid line at once (one division each), keeps the nearest wall
hit with min trees, looks up packed texture strips, and runs one small pipeline per six screen rows
to pick each pixel's texel and color. Monsters, items, the gun and the status bar are composited in
the same pipelines, so the whole thing stays about 18 ticks deep.

The game logic (movement and collision, weapons, monsters, pickups, the face) is state held in
registers on a shared bus. The rules are also written in plain Python, and every build is
checked against them.

See [docs/design.md](docs/design.md) for the full design and [docs/roadmap.md](docs/roadmap.md) for
what's next.

## Repository layout

| Path | |
|---|---|
| `blueprints/` | Ready-made blueprint strings at three sizes |
| `doom/` | The game's generators: `game.py` (entry point), `raycast.py` (renderer), `things.py`, `monsters.py`, `combat.py`, `weapons.py`, `touch.py`, `hud.py`, `face.py`, `input_pad.py`, `music.py`, `maps.py` |
| `compiler/` | Netlist compiler (`netlist.py`: latency balancing, placement, wiring, evaluation), combinator semantics, blueprint builder (`bp.py`) |
| `crates/fsim/` | Tick-accurate circuit simulator (Rust) |
| `harness/` | Runs blueprints in a windowless Factorio and records every combinator per tick |
| `conformance/` | Small blueprints pinning down the game's semantics, golden recordings, measured signal and pole data |
| `mod/doom-input/` | The optional keyboard mod |
| `demos/` | Early standalone demos: a gradient screen and a walking sprite |
| `bench/` | UPS benchmarks |
| `assets/freedoom/` | Freedoom sprites, textures, status bar and weapons as JSON |
| `tools/` | WAD extraction, the mod icon |

## Development

```sh
cargo build --release && cargo test --release   # fsim, checked against golden game recordings

python3 doom/game.py --size 64x40 --map test --verify   # scripted session: every rule and sampled frames
python3 doom/raycast.py --size 160x100 --verify         # the renderer alone, scripted camera
python3 doom/input_pad.py --verify                      # the pad's decoder
python3 doom/music.py music/level1.txt --verify         # a song's clock and speakers

./target/release/fsim run doom/gen/raycast_160x100.json --ticks 200 --every 4 --gif out/run.gif
./target/release/fsim run some.json --probe 12 --net 12:1   # print an entity or network per tick
```

`--verify` simulates the blueprint in fsim and compares it with independent references: the netlist
evaluation (frame for frame), a float raycaster, and the Python rules.

**Against the real game** (`harness/`; set `FACTORIO` to the game binary, the default is Steam on
macOS):

```sh
python3 harness/run.py --dlc --bench 30 doom/gen/game_160x100.json   # imports, fully powered? ms per tick
python3 harness/run.py --dlc --warmup 60 --every 2 DIR -o dump.json  # record a run (deep pipelines need the warm-up)
./target/release/fsim conform DIR --dump dump.json                   # compare with fsim
```

**Conformance:** `python3 conformance/gen.py` regenerates the cases;
`python3 harness/run.py conformance/cases -o conformance/game_dump.json` records them in the game
(`--dlc` with `conformance/cases_dlc`); `fsim conform` compares. The measured rules are listed in
[docs/semantics.md](docs/semantics.md).

**Assets:** `tools/wad.py` extracts sprites, textures and flats from `freedoom2.wad`; see
[assets/freedoom/README.md](assets/freedoom/README.md).

## Credits

- **[Freedoom](https://freedoom.github.io/)** 0.13.0: every sprite, texture, flat, status bar
  graphic and weapon, under its BSD 3-clause license
  ([assets/freedoom/COPYING.txt](assets/freedoom/COPYING.txt)).
- **[decider-compiler](https://github.com/Globexix/decider-compiler)** by Globexix, and its
  [one-combinator Flappy Bird](https://factorioprints.com/view/-P5CDU0KirKtOhXeCKSd): the
  inspiration for this project, and the idea of a decider with a long OR'd condition list as a
  one-tick lookup table.
- **[giftorio](https://github.com/colinchilds/giftorio)**: the lamp screen this builds on (packed
  RGB lamps, substations on a lattice inside the screen).
- **[MIDItorio](https://miditorio.com)**: converts MIDI files into the speaker blueprints that
  `--music` takes.
- **The default song** (`music/level1.txt`): a freely licensed, fan-made MIDI recreation of
  "At Doom's Gate" (Bobby Prince, Doom's first level), converted with MIDItorio.
- **Doom** by id Software: the game behavior (state timings, monster attack chances, the status
  bar face's rules, weapon placement) follows the released Doom source. Its numbers are
  reimplemented as combinators; no code or art of id Software's is included.

## License

[MIT](LICENSE). The Freedoom-derived files in `assets/freedoom/` keep Freedoom's BSD license.
