# factorio-doom: agent brief

Read this first. [README.md](README.md) has tool usage and the table of measured game semantics.

## Goal

A playable Doom-style first-person shooter built entirely from **vanilla Factorio 2.1
combinators** (no mods), shown on a lamp screen. Factorio 2.1 added decider else-outputs, which
is what makes this look feasible now.

- **Not a port of real Doom.** A combinator CPU running Doom's code would be millions of times
  too slow. We design dedicated rendering hardware out of combinators instead: a Wolfenstein-
  style raycaster with Doom aesthetics (textured walls, sprite enemies, hitscan weapons, doors,
  HUD).
- **Screen size is a build parameter.** The generator takes the size (e.g. `--size 160x100`)
  and produces a working blueprint for it, so players pick their resolution. Everything has to
  derive from W and H; see "Screen size" below.
- **Original or freely licensed content only**: our own maps and name. Sprites and textures come
  from [Freedoom](https://freedoom.github.io/) (BSD, original art; see `assets/freedoom/`). Don't
  use or recreate id Software's levels or art; the user asked for "real Doom sprites" and agreed
  to Freedoom's instead.
- **Related project:** the user's `../giftorio` (Rust/WASM) turns GIFs into lamp-display
  blueprints. Each pixel is one signal and each lamp reads packed RGB. Reuse its blueprint and
  lamp ideas.
- **Inspiration:** [Globexix/decider-compiler](https://github.com/Globexix/decider-compiler) runs
  Flappy Bird in a single decider by one-hot encoding every state value. That doesn't scale to
  Doom (positions and angles are fixed-point numbers with too many values), but two of its ideas
  matter here:
  1. A decider with a long OR'd condition list acts as a 1-tick lookup table.
  2. Its calibrated UPS cost model (`lib/cost.ml`).

## Mental model: the circuit network is a SIMD machine

- **Basics:** signal = lane, wire network = vector register, combinator = vector op with 1 tick
  of latency.
- **Free operations:**
  - Joining wires adds vectors lane by lane.
  - `Each * S` broadcasts a scalar to every lane.
  - `Each(red) op Each(green)` is a lane-wise op between two vectors (verified in game).
- **The expensive operation** is moving a value from one signal to another (a permutation), so
  design around it.
- **Two tricks to use:**
  - *Broadcast:* a constant combinator holding 1 on a set of lanes, fed into `Each * R → Each`,
    copies scalar R onto all of them.
  - *Per-lane lookup ("gather"):* bit-sliced lookup deciders, one per output bit, each an OR of
    the keys with that bit set and outputting `Each = 2^b`. Or one decider per output value.
- **Lane signals for generated blueprints come from `conformance/lane_signals.json`** (694 of
  them), never straight from the signal order. That list is item, fluid, recipe and entity
  signals that exist and aren't hidden or parameter signals in *both* vanilla and Space Age
  (`harness/run.py --lanes`). The user plays with Space Age. The first pasted demo failed to
  import because `satellite` doesn't exist there, and `parameter-N` signals would make a
  blueprint parametrised.
- **Lane budget:** 2,878 lanes in vanilla (1,439 signals × 2 qualities, measured in game; order
  in `conformance/signal_order.json`). Space Age would add more, but the signal order file would
  have to be regenerated with DLC enabled.

## Planned renderer architecture (not built yet)

```
Game state (scalars) ─► Column units (×W) ─► column bus ─► Row/pixel stage ─► lamps
 x, y, angle, sin/cos     per-column ray      lanes =      textures, shading,
 enemies, doors           lanes = DDA steps   columns      sprites, floor/ceiling
```

1. **Game state:** player position in fixed point, angle 0–255 with a sin/cos lookup, enemies,
   doors.
2. **Column units, one per screen column.**
   - The ray is a few scalars (`dir + plane*camX`).
   - The lanes are all candidate grid-line crossings at once (k = 0..15 for vertical lines and
     0..15 for horizontal), with `t_k = t0 + k*Δ`.
   - A map-lookup decider marks which lanes hit a wall, and a selector (ascending, index 0)
     picks the nearest hit.
   - Pack `dist | wall type | side | tex u` into one int, so picking the nearest hit carries the
     other fields along.
   - Each column outputs its result on its own column signal. All columns wire onto one bus, so
     the transpose to "lanes = columns" costs nothing, and the bus doubles as the depth buffer.
3. **Row or pixel stage.** Two layouts:
   - One network per row, with W lanes.
   - A whole-frame vector with one lane per pixel, filled by the broadcast trick. Same total
     work, far fewer combinators.

   At 160×100 and above, use per-row networks. The whole-frame vector only fits up to 2,878
   pixels.

   Texturing, about 10 lane-ops per pixel and no per-pixel lookup tables:
   - The column stage fetches its wall's texture column as a packed strip (e.g. 4-bit texels,
     8 per int).
   - Each pixel computes texel `v` from a precomputed `1/h`, then `(strip >> 4v) & 15` with
     lane-wise `Each(red) >> Each(green)`.
   - Palette: `texel * base_color` with one multiply. Pick per-wall base colors whose channels
     can't overflow, and shade by distance in the column stage.
   - Decider **else-outputs (per lane, verified)** fill floor/ceiling and composite sprites over
     walls.
   - Per-pixel lookup deciders (OR lists over texel keys) are affordable after all: 256
     conditions on every pixel at 320×200 measured 1.6 ms. The packed strip is still about
     10× cheaper, so prefer it and keep lookups for the column bus and special cases.
4. **Sprites:** camera transform on scalars, then a per-lane test against the column depth
   buffer.
5. **Game logic:**
   - Movement and collision reuse the map lookup on scalars.
   - Hitscan uses the center column.
   - Enemies are small state machines.
   - The HUD uses display panels.
6. **Input (unsolved):** vanilla has no keyboard → circuit path. The top idea is a "gate pen":
   the player stands surrounded by gates, and walls next to them report when a gate opens.
   Firing needs a separate trigger. Nothing here is prototyped yet.
7. **Stretch goal:**
   - Angled walls: column lanes become map segments, using lane-wise division for the
     ray/segment intersection.
   - Varying floor and ceiling heights: the selector's 2.1 "N lowest" comparator returns the N
     nearest hits per column.

## Screen size (decided 2026-11-01)

- **Develop against 160×100.** At 64×40 the HUD (health, ammo) would be unreadable.
- **The long-term goal is 320×200**, Doom's own resolution. The user knows from giftorio that a
  lamp screen this size still fits at max zoom.
- **Selecting the size in code is a project goal (user requirement, 2026-11-01).** Never
  hard-code a resolution. Everything derives from W and H:
  - **Raycaster:** one column unit per screen column; camera-plane steps, projection distance
    and the field of view come from W and H. Wall height in rows scales with H.
  - **Row stage:** one row pipeline per screen row, including each row's constants
    (row − horizon, the floor/ceiling distance tables).
  - **Textures and sprites:** stored at a fixed source resolution and sampled with
    fixed-point steps computed from W and H, never pre-scaled to one screen size. Sprite
    screen size and position come from the same projection as walls.
  - **HUD:** laid out from W and H. Glyphs and the status bar pick a pixel scale for the size,
    so they stay readable from 160×100 up.
  - **Power:** the dead-pixel substation grid depends on the size, so derive the layout (HUD
    placement, edges) from it.
  - **Testing:** the reference raycaster and the stage checks run at several sizes (at least
    64×40, 160×100 and 320×200), so nothing silently assumes one.
- **Rough size at 160×100:**
  - About 20 combinators per column (around 3,200), about 12 per row (around 1,200), a few
    hundred for logic, and 16,000 lamps.
  - It's pipelined, so a new frame can start every tick, with latency around 25 ticks.
- **UPS is not the blocker (M0, measured).** A full 320×200 pixel stage with 63k lamps costs
  about 1 ms per tick, against a 16.7 ms budget. See "M0 results" below.
- **Power means dead pixels.** Substations have to sit inside the lamp screen: 2×2 holes on an
  18-tile grid, about 1.3% of pixels, the same as giftorio. Plan the HUD and the layout around
  them.

### M0 results (2026-11-01, Factorio 2.1.23, the user's Mac: arm64, 16 cores)

Median `--benchmark-verbose` timings over 300 ticks, from `bench/m0.py` and
`harness/run.py --bench`. Raw numbers are in `bench/m0_*.json`.

| Blueprint | Entities | ms/tick (whole update) |
|---|---|---|
| empty baseline | 2 | 0.18 |
| 160×100 pixel stage (9 ops/pixel) + lamps | 16,765 | 0.44 |
| 320×200 pixel stage (9 ops/pixel) + lamps | 65,199 | 0.98 |
| 320×200 lamps only | 63,576 | 0.85 |
| 320×200 lookup decider, 256 OR conditions on every pixel | 424 | 1.81 |

Derived costs (slopes between sizes and variants):
- Arithmetic: about **0.3 ns per lane-op**, plus about 90 ns per combinator.
- Lookup deciders: about **0.1 ns per lane × condition**, linear from 16 to 256 conditions.
- Lamps: about **10 ns per lamp**. At 320×200 the lamps cost more than the logic.
- Circuit network summing (`electricHeatFluidCircuitUpdate`) barely moves (0.08 to 0.13 ms).

These are update times only, from a headless benchmark. They exclude rendering 64k lamps in a
real client; giftorio experience says that's fine. They also come from a fast machine, so allow
2–3× on slower ones. The game recomputes every tick: the M0 blueprints match fsim tick for tick
(9,918 checks), and their values change every tick.

## Tooling: simulator first, game as ground truth

The game is the referee, and fsim is the fast development loop:

1. **`fsim`** (`crates/fsim`, Rust):
   - `run`: simulate a blueprint and write lamp frames as PNG/GIF. `--probe N` prints an
     entity's output each tick; `--net E:C` prints a network.
   - `conform`: compare against a game dump.
   - `decode`: print a blueprint string as JSON.

   Source files:
   - `model.rs`: parses blueprint JSON.
   - `sim.rs`: networks via union-find over connectors, the tick loop, evaluation, wire reach.
   - `signals.rs`: signal ids in game order; sparse sorted vectors.
   - `render.rs`: lamp grid to images.
   - `conform.rs`: maps game dump rows onto simulation ticks.

   Speed: about 16 µs per tick for 328 entities, 2.7 ms at 160×100 and 11 ms at 320×200 for the
   M0 pixel stage. Lookup deciders are about 100× slower than in the game (10 ns per lane ×
   condition). Nothing is optimized yet; it will need to be for 320×200 development.
2. **Harness** (`harness/run.py` and the `harness/mods/fsim-harness` mod).
   - Launches the user's Factorio (Steam, macOS) with `--create`, then `--benchmark`, using an
     isolated config and mods under `harness/`.
   - Lays the cases out side by side by their size, copper-links substations in reach, and
     gives each substation that has room an electric-energy-interface.
   - Runs a 30-tick warm-up, switches all constant combinators on at t0, then records every
     combinator's `signals_last_tick` and every lamp's color each tick to
     `script-output/fsim/dump.json`.
   - Reports any unpowered combinator or lamp as a harness error.
   - Modes:
     - `--bench TICKS`: time each blueprint alone, with no recording; prints median ms per tick.
     - `--signals`: dump the signal order.
     - `--discover`: export a blueprint with every option set, to learn the JSON field names.
3. **Conformance** (`conformance/`):
   - `bp.py` is a stdlib-only blueprint builder. `power()` places substations until every
     entity tile is covered, counting substations already placed. A solid lamp screen needs
     its own substation grid placed first (see `bench/m0.py`), and solid combinator blocks need
     a gap column for substations every 18 tiles or so.
   - `gen.py` defines the cases. Each packs many edge values into separate lanes.
   - Golden game recordings: `conformance/game_dump.json`, and
     `demos/golden/gradient_dump.json` for the demo. `cargo test` checks fsim against both.
4. **Demo** (`demos/gradient.py`): a 16×16 screen in the row-stage layout. It matches the game
   on all 24,705 checks.
5. **Benchmarks** (`bench/m0.py`): synthetic row-stage blueprints at any W×H, written to
   `bench/gen/` (gitignored, up to 13 MB each).

### Sprite demo (`demos/sprite.py`, 2026-11-01)

The first real renderer piece: a Freedoom monster walking toward and away from the camera, at
any `--size`. It proves the texture path planned above:
- **Scalar pipeline:** each stage reads the previous network and passes all of it through
  (`Each + 0`), so every scalar on a network comes from the same clock tick. This removes the
  need for hand-placed delays.
- **Column stage** (lanes = columns): `u = ((c+1)*step + O) >> 16`, where the offset `O` adds
  2^30 + frame·2^28. That keeps every lane nonzero (a zero lane vanishes) and makes the lookup key
  `16384 + 4096·frame + u` in one shift.
- **Lookup:** bit-sliced deciders, one per (strip word, bit), each an OR over the keys with that
  bit set, outputting `Each = 2^b`. Bit 31 uses the constant -2^31; this works in the game.
- **Row stage:** sprite rows share one `v` per screen row. So a row-lane vector computes
  `J = (v >> 3) + 2048` and `S = (v & 7) * 4`, and each row picks its word with deciders
  `row_y = 2048 + j → Everything(green bus j)`. It then shifts by `row_y` from the red wire,
  masks 15, and maps the palette with 16 deciders on `Each(both)`. A green constant of 16 on every
  column lane makes transparent texels exist as key 16, which maps to the row's floor/ceiling
  color.
- **Size:** about 26 combinators per row, plus a lookup block of up to 8×32 deciders. 160×100 is
  18.8k entities.
- **Checks:**
  - `--verify` compares every fsim frame with a Python reference using the same integer math. It
    has 0 differing frames at 64×40, 160×100 and 320×200 (latency 21 ticks).
  - The 64×40 version matches the game on 233,935 checks.
  - 160×100 and 320×200 import and run in a Space Age game at 0.43 and 1.23 ms/tick.
  - Generated files go to `demos/gen/` (gitignored). The `.txt` file is the blueprint string to
    paste.

## Status (2026-11-01)

Done:
- The design above, and checks of the 2.1 features it depends on.
- fsim, the harness, the conformance suite (11 cases, 806 checks) and the gradient demo. All
  match the real game exactly.
- Measured semantics: see the README table, and `sim.rs` / `conform.rs` doc comments for the
  timing model.
- Screen size decided (160×100, aiming for 320×200), and M0 UPS measurements: 320×200 fits.
- Freedoom sprite assets and the sprite demo (above): the texture path works end to end in fsim
  at three sizes and in the game.

Commit only when the user asks.

## Next steps (in order)

1. **Bit-exact reference raycaster** (Rust, int32 fixed point): the same math the combinators
   will do, including rounding and overflow. It's the oracle for every renderer stage.
2. **Netlist compiler:** a small dataflow description of vector ops on named wires, compiled to
   a blueprint.
   - Insert delays so every path to a stage has the same latency.
   - Place entities and wires within reach (footprint gap ≤ 9) and add power.
   - Check each stage against the reference in fsim, then once in the game via the harness.
3. **Input prototype** (gate pen) in the game.
4. **Milestones:**
   - M1: fixed camera, flat-shaded walls.
   - M2: input, movement, collision.
   - M3: textures and distance shading.
   - M4: sprites, enemies, shooting, HUD, doors.
   - M5 (stretch): angled walls and varying heights.

## Rules for working here

- **Never rely on unverified game behavior.** Add a conformance case to `gen.py`, run it in the
  game, make fsim match, and refresh the golden dump. Several obvious guesses turned out wrong:
  the min tie order, lanes when red + green sums to 0, wire reach being measured footprint to
  footprint, and lamp color lagging its condition by one tick less.
- **Generated layouts must keep every wire's footprint gap ≤ 9** (the shorter reach of its two
  ends). The game silently drops longer wires. fsim warns about them, so treat that warning as
  an error.
- When a game result looks wrong, check power and wiring before concluding anything about
  semantics. Two "semantics" puzzles so far were really an unpowered entity and an over-long
  wire.
- **Blueprints for the user must power themselves:** `b.power()` places the substations, then
  `b.link_power()` copper-wires them (pasted blueprints only carry the wires they list). The
  harness then adds a single power source, so a missing link shows up as unpowered entities.
  Before handing a blueprint over, run `harness/run.py --dlc --bench 30 file.json`: it builds it
  in a Space Age game and fails on import errors or unpowered entities.
- **Screen lamps need `always_on`** (`b.lamp(..., always_on=True)`), or they stay dark in
  daylight. The harness runs at night, so it can't catch this.
- Python is stdlib only. Game paths come from `FACTORIO` and the Steam default in
  `harness/run.py`. Harness runs take about 2 s.
- The harness mod list disables space-age/quality/elevated-rails, so signals and the signal
  order are vanilla.
- The sandbox once lost all access to `~/Documents` mid-session (EPERM on everything). If that
  happens, stop and ask the user (a restart fixed it) rather than piling up workarounds.
