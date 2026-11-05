# Design

How factorio-doom draws a first-person shooter with nothing but Factorio 2.1 combinators and lamps.
The module docstrings in `doom/` have the exact formulas; this document explains the shape of the
machine and why it is built that way.

## Contents

- [Approach](#approach)
- [The circuit network as a SIMD machine](#the-circuit-network-as-a-simd-machine)
- [Toolchain](#toolchain)
- [Renderer](#renderer)
- [Status bar](#status-bar)
- [Game logic](#game-logic)
- [Input](#input)
- [Maps](#maps)
- [Screen size and power](#screen-size-and-power)
- [Performance](#performance)
- [Verification](#verification)
- [Known limitations](#known-limitations)

## Approach

A combinator CPU running Doom's code would be millions of times too slow. Instead, the project
builds dedicated rendering hardware out of combinators: a Wolfenstein-style raycaster with Doom's
look (textured walls, floors and ceilings, sprite monsters, hitscan weapons, a status bar). It is
fully pipelined, so a new frame starts every tick, about 18 ticks deep.

- **Factorio 2.1 and Space Age.** 2.1's decider else-outputs make per-lane selects cheap, and Space
  Age's signal qualities give six lanes per signal (the screen packs six rows per network).
- **Screen size is a build parameter.** Everything derives from W and H (`--size`), so players pick
  a resolution. Development targets 160×100; 320×200 (Doom's own) fits comfortably.
- **Original or freely licensed content only.** Maps are our own; sprites, textures and the status
  bar come from [Freedoom](https://freedoom.github.io/) (BSD). Nothing of id Software's.
- **Prior art.** [decider-compiler](https://github.com/Globexix/decider-compiler) runs Flappy Bird
  in a single decider by one-hot encoding every state value. That doesn't scale to Doom's
  fixed-point positions and angles, but it showed that a decider with a long OR'd condition list
  is a one-tick lookup table, which is used throughout. [giftorio](https://github.com/colinchilds/giftorio)
  turns GIFs into lamp screens (one pixel per signal, packed RGB, substations inside the screen);
  the screen here builds on it.

```
Game state (scalars) ─► ray vectors ─► march: K steps per axis ─► split ─► texture ─► row pipelines ─► lamps
 x, y, angle, sin/cos    RDX, RDY        each step one W-lane      by side   strip words,  v, mux, shift,
 (bit-sliced lookups)    per column      network; min trees keep             palettes ×    palette
                                         the nearest hit                     brightness
```

## The circuit network as a SIMD machine

- **Signal = lane, wire network = vector register, combinator = vector op** with one tick of
  latency.
- **Free operations:** joining wires adds vectors lane by lane; `Each * S` broadcasts a scalar to
  every lane; `Each(red) op Each(green)` is a lane-wise op between two vectors.
- **The expensive operation is a permutation** (moving a value from one signal to another), so the
  design avoids them. The one cheap exception: a selector in quality-transfer mode copies a whole
  vector onto another quality in one tick.
- **Broadcast:** a constant holding 1 on a set of lanes, fed into `Each * R → Each`, copies scalar
  R onto all of them.
- **Gather (per-lane lookup):** bit-sliced deciders, one per output bit, each an OR of the keys with
  that bit set, outputting `Each = 2^b`; or one decider per output value. Runs of consecutive keys
  merge into range conditions (`lo <= Each < hi`).
- **Zero lanes vanish.** A signal whose value is 0 doesn't exist, so a column whose value happens
  to be 0 would silently drop out. Every vector that might hit 0 is combined with one that is
  nonzero on every lane, or read by a decider that also tests a full vector (an `Each` decider
  visits the union of lanes over all its `Each` operands).
- **Lane signals** come from `conformance/lane_signals.json` (694 signals): items, fluids, recipes
  and entities that exist and are visible in both vanilla and Space Age, so blueprints import in
  either and never become parametrised. Virtual signals stay free for named scalars.
- **Quality is a lane axis.** `Each` arithmetic and deciders keep qualities apart, lamps read a
  signal at a quality, and a quality-transfer selector moves every input signal to one quality.
  Space Age has six qualities per signal (normal, uncommon, rare, epic, legendary,
  quality-unknown).

## Toolchain

The game is the referee; the simulator is the fast development loop.

- **Netlist compiler** (`compiler/netlist.py`, `compiler/semantics.py`). The circuit is written as a
  dataflow graph: vector ops, scalar ops, bit-sliced lookups and raw deciders. The compiler
  - balances latency, inserting shared `Each + 0` delays so every input carries the same frame;
  - gives each consumer its own constant combinator and duplicates a node whose output must join
    two different networks of one color;
  - places nodes one tile column per time step, at the mean row of their consumers, leaving
    columns free for constants and relay poles. Regular blocks (row pipelines, lookup tables) are
    fixed nodes the caller positions;
  - wires each network as a minimum spanning tree, bridging long edges with medium poles (A*
    around obstacles), and avoids edges whose end is boxed in;
  - evaluates the graph without timing, using the same specs it emits. That evaluation is the
    reference: the simulated blueprint must match it frame for frame.
- **Blueprint builder** (`compiler/bp.py`): entities, wires, substation grids (`power()`,
  `link_power()`) and wire-reach checks (`check_reach()`).
- **fsim** (`crates/fsim`, Rust): a tick-accurate simulator. Networks are union-find over
  connectors; it renders lamps to PNG/GIF/PPM, probes entities and networks, warns about every
  wire the game would drop, and compares itself with game recordings (`fsim conform`).
- **Harness** (`harness/run.py` and the `fsim-harness` mod): runs blueprints in a windowless
  Factorio with an isolated config, records every combinator output and lamp color per tick,
  benchmarks UPS, scripts inputs (chest inserts, constant-combinator writes) and reports any
  unpowered entity.
- **Conformance suite** (`conformance/`): small blueprints that pin down one behavior each, run in
  both the game and fsim. The game's recordings are golden files, so `cargo test` checks fsim
  offline. The measured rules are in [semantics.md](semantics.md).

## Renderer

The renderer is `doom/raycast.py` (`build_graph`). It runs standalone with a scripted camera, or
inside the game with the camera from the player's state.

### Camera and rays

- **State:** position in 1/256 tiles, angle in 1/1024 turns. Bit-sliced lookups on the angle give
  the camera plane and direction directly. Tables are signed; bits that share a key set share one
  decider. Constant offsets (the spawn facing) go into the tables rather than adding a tick.
- **Rays:** `RAW = camX * plane + dir << 10 + e` per column, where camX is a constant vector. The sum
  is a plain wire join. `RD = RAW >> 10`; the rounding term e is chosen per angle so no column's RD
  is 0.
- **Latency rule:** additions of constants or other vectors ride on a consumer's input wire for
  free, so they're folded in rather than spending a tick on a `+`.

### The march

Lanes are screen columns. The march runs once for vertical grid lines and once for horizontal ones,
K steps each, all computed at once from the step index k rather than as a recurrence:

- Crossing k is at signed distance `along_k = sel(256(k+1), -256k) - FR` along the axis (selected
  per lane by the ray's sign; FR is the position within the tile).
- **Distance:** `D_k = along_k * 2^17 / RD` (2^15 per tile of ray length).
- **Map key:** `key_k = along_k * 16 * RO / RD + 16 PO + line_k`: the key of the cell entered, shifted
  left 12, with the position across the line (the texture column) in the low 12 bits. Line indices
  count outward from the camera in both directions, so **keys grow with distance** either way.
- A lookup decider per step tests key ranges (consecutive wall keys merged) and outputs
  `HIT = -2^30` on wall lanes.
- **Two min trees per side** (depth 4): over `HIT + D_k` (the distance) and over `HIT + key_k`, which
  picks the same nearest hit because keys grow with distance. The key gives the wall type and
  texture column.
- Out-of-map keys can alias real wall keys, but only beyond the boundary wall, which is always
  nearer. So maps must be closed.

### Split and textures

- **Split by side:** the last min level splits into MV (vertical lines nearer: D plus a constant
  that cancels HIT) and MH (the rest). They never share a lane, so `[MV, MH]` on one wire is D.
- **Texture key** per side: `TK = (type + 1) << 12 | u`, u from the key's low bits, the type from
  range deciders.
- **Strip lookups:** one decider per (side, type, texture column, word): `TK = key AND MV > 0` →
  `Each = word`. Each word packs 8 texels at 4 bits. Gating by the side being nearer gets the words
  out one tick after the split. This is the biggest fixed block (2 × types × tex × tex/8 deciders).
- **Palettes:** each texture is quantized to 14 colors, stored at 4 bits per channel (the H side ¾
  as bright). Per (side, type, texel value) a gated decider outputs that color; one bus per texel
  value, times the brightness `17 * FOG / (D + FOG)`.

### Rows and chunks

Each screen row needs only a few vector ops, and **six rows share one network**: row q of a chunk
is quality q of its column's lane signal. Chunks never straddle the horizon or the status bar.
Per-row constants become constant vectors over the chunk's lanes. The per-column vectors the chunks
read (D, the word buses, the color products, RAW, the things' vectors) are copied onto all six
qualities by quality-transfer selectors, one tick.

Per chunk:

- `V = D / c_y` with a per-row constant c_y: 4 × (texel row − tex/2). The wall covers a fixed range
  of V, so there is no wall-height vector.
- **Mux:** per word j, a decider copies word bus j where `lo_j <= V < hi_j`. A green condition makes
  it visit lanes where V is 0.
- **Shift:** `T = word << (V AND 28)` puts the texel in the top nibble.
- **Palette:** one decider per texel value, a range test on the top nibble → copy that color bus.

### Floors and ceilings

Row y shows the floor (or ceiling) at a distance that depends only on the row, so the flat point
under each column is a per-row multiple of the ray: `F = RAW * K_y + (P << 24)` per axis is the
pixel's position within its tile times 2^32. The int32 product wraps, which drops the whole tiles
for free. Its top bits pick a word of 8 packed flat texels (one decider per word), a shift picks
the texel, and one decider per flat color outputs that color at the row's (constant) brightness. A
flat select copies it where V is outside the wall. It runs beside the march, so it adds no latency.

**Different flats per tile:** with more than one flat on the map, the position keeps its tile:
`(RAW >> 4) * K_y + ((P << 20) - 2^31)` is the world position times 2^28, offset by −8 tiles so it is
signed and increasing over the map, and its top 4 bits are the tile. Per chunk,
`FX = that << 4`, `FY = that AND (2^28 - 1)`, and per flat class a decider over the class's tile
rectangles adds `class << 28` onto FY. The word deciders then test one range on FY (class, then
flat row), and each class has its own 16-color palette. Up to 8 flats per part (floor, ceiling).

### Things

Monsters and items (`doom/things.py`), up to 8, drawn on the walls' texel grid in a box 32 rows tall.

- **Per thing (scalars):** depth `alpha = rx DX + ry DY` and lateral offset `L = ry DX - rx DY`. Per
  column, `U = camx * (alpha / 1551) - L + C` is the box column.
- **Nearest per column:** two min trees over the things. `Zt` (depth then box column) keys the
  lookups; `Dt = alpha * 64 + frame` gives the precise depth and frame. One thing per column: a
  farther thing is hidden by the nearer one's box columns, even where it's transparent.
- **Live monsters first:** items and dying or dead monsters carry a large bias in the trees
  (`YIELD_D`), so a live monster is drawn whole in front of a corpse or item it overlaps. The bias
  applies only in front of the near plane and is stripped after the trees.
- **Lookups per (frame, column):** a mask word (which rows are opaque) and 4 texel words.
- **Kill without latency:** per row, the mask bit for that row, tested with a constant-first
  `Each` power (`2 ^ Q`), adds a large value to the wall's V where an opaque thing pixel is in
  front, so the wall and floor output nothing there. The thing path is timed back from V.
- **Texel path:** the same mux, shift and per-color palette deciders as the walls.

### View geometry

`raycast.View`: the field of view (`--fov`, default 67°), pixel aspect (`--aspect`, default 1.2,
as Doom's 320×200 on a 4:3 screen) and eye height (`--eye`, default 40 Doom units, below an imp's
height, so monsters stand above the eye line). All are constants in the blueprint: the projection
height `proj = aspect × W × 512 / plane` rows per tile, the wall's V range, the flats' distance
factors and the things' row offsets. They cost no combinators or ticks.

### Shading

- **Color grade** (`doom/palette.py`): lamps draw a dark frame around each lit square, so
  Freedoom's dark palette looks murky. Every palette is graded before the 4-bit store: a gamma
  lift on the brightest channel (scaling all three, so hue holds and nothing clips) and a
  saturation boost. `--gamma 1 --saturation 1 --fog 4` gives Freedoom's own colors.
- **Fog:** brightness is `17 * FOG / (D + FOG)` (≤ 17, so ≤ 255 per channel), `--fog` tiles to half
  brightness.
- **Muzzle light:** Doom's flash frames raise the light level by 1 or 2. Here the brightness
  becomes `(bright + k D) / (D + fog)` with `k = --muzzle-light × level`. Walls add one product,
  things a division, and flats a per-chunk decider per level. No latency.

### Layout

- Chunk blocks sit left of the screen, each k rows tall, with the same (column, row) per role in
  every chunk, so each bus runs straight down one column.
- Status bar chunks go below. The spreads and color products sit just above, then the texture and
  sprite lookup block, then the march as a grid to its left. The compiler places the rest (camera
  lookups, things, aim, status values) left of that.
- **Power and buses:** substations sit on a lattice inside the screen (see below). The state bus
  rides the substation row through the top of the screen, from the player's register on the
  right to the raycaster's camera port on the left.
- A constant combinator section holds at most 1,000 filters, so large constant vectors are split
  into sections.

### Latency

In the game, 18 ticks from the camera port to the lamps: camera lookups 1, ray products 1, ray
shift and sign selectors 1, key product 1, key division 1, hit lookup 1, min trees 4, thing
priority 1, side split 1, strip lookups 1, quality spread 1, V 1, mux 1, shift 1, palette 1,
lamps 1. Input to screen is about 23 ticks (0.38 s): pad 3, into the register 2, raycaster 18.

## Status bar

`doom/hud.py`: Freedoom's STBAR in Doom's layout across the bottom `round(32 H / 200)` rows. It
shows the current weapon's ammo, health %, armor %, the face, the ARMS panel and all four ammo
counts.

- **View crop:** the view renders only the rows above the bar but keeps the full screen's
  projection, so walls keep their size and the view is cropped, as in Doom.
- **Circuit:** per bar row, a constant holds the static row. Per (slot, glyph, bar row), one decider
  outputs that glyph's row minus the static row, in exact 8-bit colors (box-filtered and graded at
  build time). There is no palette or texel path. Digits are selected by range conditions on the
  value (hundreds, tens, ones), with leading zeros blank.
- **Timing:** the values are delayed to land with the view rows, so the bar and view always show the
  same frame.
- **Shared pixels:** at odd scales two slots can share a screen pixel. It belongs to the slot
  covering more of it, so every pixel is always one valid composite.

## Game logic

`doom/game.py` builds the screen, the pad on its right and the player logic below the pad. The
rules for each system are also written as plain Python (`step` functions), which `--verify`
checks against the simulated circuit every tick.

### Player state and movement

- **REG** is one register: a decider `RESTART = 0 → Everything` holding its own output (green loop)
  plus the increments on red. It holds X and Y (offsets from the spawn), R (turn, 1024 per
  revolution), health, armor and the four ammo counts (offsets from a new game's values).
- **Nothing on the input path waits for collision.** Every tick, whatever is held, the player logic
  computes the step forward and back for the current angle and, for each corner of the player's
  box, whether it's in a wall or a live monster (range tests on X and Y). Four gate deciders
  (forward/back × x/y) pass a step component into REG when its button is held, it points that way
  and both box corners on that side are free. Each axis is gated alone, so you slide along walls
  and can't cut corners.
- The corners run a couple of ticks behind REG, so the collision box is grown by how far REG can
  move meanwhile.
- **Turning ramps:** 1/1024 turn per tick for the first 18 ticks a turn button is held (a tap turns
  about 5°), then 4 (`--turn-slow`, `--turn`, `--turn-ramp`).
- **Bounded angle:** two deciders next to REG wrap R back into range when it leaves [0, 1024), so
  the lookups key on R directly with no mask.

### The state bus

REG's red output network carries the game state. Deciders that hold state read the bus and write
back onto it. Monsters keep their positions and animation clocks on the bus, and their working
signals (offsets, sight lines, random numbers, flags, the damage queue) on a network of their own,
M. The raycaster's aim and bearing outputs are wired onto the bus too.

### Weapons

`doom/weapons.py`: seven Freedoom weapons (fist, pistol, shotgun, chaingun, rocket launcher, plasma
rifle, BFG). The number buttons select them by Doom's slots; fire plays Doom's animation with its
muzzle flash and spends ammo.

- **Pictures:** Doom's weapon states, converted from tics to ticks, with the flash layer composed
  over the gun at build time. The ready frames are also drawn at 8 lowered positions for the
  switch animation (88 pictures in all).
- **Overlay:** per (picture, chunk), two deciders on the picture id: a kill (adds a large value to the
  chunk's V and the things' V, so nothing else draws there) and the colors. No extra tick.
- **State:** the weapon W, its animation tick A and the pending weapon P. Fire from ready starts the
  cycle if there's ammo; Doom's A_ReFire restarts it while fire is held. Selecting another weapon
  sets P; the gun lowers, switches at the bottom and rises (about 0.9 s). A selection while
  lowering replaces P.

### Pickups and solid monsters

`doom/touch.py`: walking over an item picks it up with Doom's amounts and caps (a full stat leaves
the item). Per item, a held flag hides it (its depth is pushed behind the camera). The increment
`min(amount, cap - value)` comes from a bit-sliced table over value ranges. Live monsters' boxes
join the walls in the player's corner tests.

### Shooting

`doom/combat.py`: hitscan along the middle of the view.

- **Aim (in the raycaster):** it already has every thing's depth and lateral offset and the middle
  column's wall distance. The nearest live monster within 24 Doom units of the center line and in
  front of the wall gives `AIM = i + 1`, and `CLOSE` within fist reach. These land on the bus
  without adding raycaster latency.
- **Damage:** at a weapon's shot ticks, the aimed monster's damage counter grows by the weapon's
  mean damage (no randomness yet). Corpses don't block shots.
- **Frames:** a lookup on each monster's clock picks its walk, attack, pain or death frame.

### Monsters

`doom/monsters.py`: Doom's behavior, simplified.

- **Sight:** per monster, 63 points between it and the player as lanes, tested against the wall
  rectangles by a 2D range lookup whose else-output counts the clear points.
- **Waking:** in sight and in front of it (its facing), or close and in sight, or shot.
- **Chase:** move toward the player along the major and minor axes, testing two leading corners per
  step and sliding along walls. When blocked, it steps sideways or follows the wall for a while.
- **Attacks:** after a full walk cycle, an attempt at each of Doom's A_Chase ticks with Doom's chance
  by distance band, and none beyond 5 tiles. The zombieman hits by Doom's spread; the imp
  scratches up close or throws a fireball (half of them hit: there are no projectiles yet).
- **Pain:** any hit that doesn't kill restarts the pain frames and the walk cycle, so steady punching
  keeps an imp from attacking.
- **Damage to the player:** queued and applied a few points every other tick. Green armor absorbs a
  third.
- **Random numbers:** an LCG on M, times a constant per monster.

### Status bar face

`doom/face.py`: Doom's ST_updateFaceWidget. The face glances left and right at random, turns toward
a monster that hurts you (or grits its teeth if it's in front), and grits its teeth after 2 s of
firing. The bearing to each monster (left, right or ahead) comes from the raycaster's aim stage.

### Death and restart

At health 0 the player is dead: every corner reads as a wall, turning stops, the gun lowers and fire
and selections are ignored. Use (or the reset button, at any time) outputs `RESTART` for one tick.
REG drops everything that tick, and every bus writer has `RESTART = 0` ANDed into its conditions,
so all state returns to a new game together.

### Music

`doom/music.py` (`--music FILE`, `--no-music`): a song converted by [MIDItorio](https://miditorio.com)
plays on programmable speakers. MIDItorio emits the song in constant combinators, a clock, a
decider matching the current time, unpacking arithmetic and one decider per speaker. Its clock is
rewired to loop at the song's last event and to reset on `RESTART` from the bus. The song's
on/off constant still switches the music.

## Input

### Chest pad

`doom/input_pad.py`. The game is played in god mode (a character's camera can't zoom out far enough)
with the cursor over a button, dropping items with Z (about 6 drops a second when held).

- A button is an n×n block of chests (n = 9 at 320×200, scaling with H) so it's easy to hit at the
  zoom that shows the whole screen.
- Layout: a D-pad with combos on the corners (one cursor can't hold two buttons), fire in the
  middle, weapon slots 1–7 with Use and Enter below, and reset set apart on the right. The pad
  sits right beside the screen, for playing zoomed in on the lamps.
- **Held vs pressed:** each button's chest total goes through a one-tick difference to count
  drops; a countdown holds HELD for 14 ticks after each drop (`--hold`); PRESSED pulses on the first
  drop after a release. Taking items out never counts.

### Keyboard mod

`mod/doom-input`, optional. The blueprint has two empty constant combinators tagged
`doom-input:held` and `doom-input:pressed` that add onto the pad's outputs. The mod writes the held
movement keys (from `walking_state`) and fire to the first, and one-tick presses (weapons 1–7, use,
reset) to the second. In the editor, WASD moves the player, so the mod teleports it back right
after each move, before the frame is drawn. A key press reaches the HELD network the next tick,
against about 3 ticks for a pad drop, and release is immediate.

## Maps

`doom/maps.py`. A map is 16 × 16 cells: wall characters map to wall textures, open characters to a
floor and ceiling flat. It also holds the things (monsters first, then items), each monster's
facing, the spawn, the spawn angle and the scripted camera's center. `--map` picks one (`arena`,
`test`, `acid`), and `maps.select` makes it current in every module.

## Screen size and power

- **Everything derives from W and H:** camera-plane steps, projection, row constants, texture
  sampling steps (textures are stored at a fixed source resolution), the status bar's scale and
  the pad's button size.
- **Power means dead pixels.** Substations sit inside the lamp screen as 2×2 holes on a lattice
  (`bp.SubstationGrid`). Legendary substations (28×28 supply, the default
  `--substation-quality`) cost about 0.5% of pixels; normal ones about 1.2%.
- **Blueprints power themselves:** `b.power()` places substations, `b.link_power()` copper-wires them
  (a pasted blueprint only carries the wires it lists). The player connects one power source.

## Performance

UPS is not the bottleneck. Measured in Space Age on an Apple Silicon Mac (16 cores), headless
update time, 300-tick medians (`harness/run.py --dlc --bench 300`):

| Build | Combinators | ms/tick |
|---|---|---|
| Game 320×200, arena map, with music | 12,544 | 1.16 |
| Game 320×200, acid map, with music | 14,010 | 1.20 |
| Game 160×100, arena map | 10,222 | – |
| Game 64×40, arena map | 8,976 | – |

A game tick has 16.7 ms. About 4,096 of the combinators are the wall strip lookups (4 textures at
64 texels).

**Cost model** (synthetic benchmarks, `bench/m0.py`, raw numbers in `bench/m0_*.json`):

| Blueprint | Entities | ms/tick |
|---|---|---|
| empty baseline | 2 | 0.18 |
| 160×100 pixel stage (9 ops/pixel) + lamps | 16,765 | 0.44 |
| 320×200 pixel stage (9 ops/pixel) + lamps | 65,199 | 0.98 |
| 320×200 lamps only | 63,576 | 0.85 |
| 320×200 lookup decider, 256 OR conditions on every pixel | 424 | 1.81 |

- Arithmetic: about 0.3 ns per lane-op, plus about 90 ns per combinator.
- Lookup deciders: about 0.1 ns per lane × condition.
- Lamps: about 10 ns each; at 320×200 the lamps cost more than the logic.

These exclude rendering, and slower machines may take 2–3× as long.

## Verification

Every generator has a `--verify` mode that simulates the blueprint in fsim and checks it against an
independent reference:

- **Raycaster:** sampled frames against the netlist evaluation (exact), and against a float
  raycaster (`ideal()`; about 7–11% of pixels differ by texel and brightness rounding at
  boundaries).
- **Game:** a scripted session (walk, every weapon, pickups, a fight, death, Use, reset) checked
  every tick: REG against a reference loop, the state bus and M against fsim, the weapon, contact,
  combat, monster and face rules (`step` functions) against the bus, the aim against a float
  hitscan, no box in a wall, and sampled frames. `--map test` is the map the script is tuned for.
- **Pad, music, sprite demo:** fsim against their reference models.

fsim itself is checked against the game: the conformance suite and the gradient demo through
`cargo test`, and full game builds through the harness. The 64×40 game has matched a real Space
Age game on over 14 million recorded values (2,560 ticks of the scripted session).

## Known limitations

- Textures are mirrored on wall faces seen from the far side of one axis (u is the position along
  the wall, not along the viewer's right).
- One thing per column: two overlapping monsters cut each other (live monsters do win over corpses
  and items).
- At most 8 things; no projectiles, doors, varying heights or sprite rotations yet. See
  [roadmap.md](roadmap.md).
