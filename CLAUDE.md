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

## Renderer architecture

M1 (flat-shaded walls) is built and verified; see "Raycaster M1" below. The original plan had
one column unit (~20 combinators) per screen column. The built design is far smaller: **the
whole ray march is vector ops with lanes = screen columns**, about 640 combinators at any width.

```
Game state (scalars) ─► ray vectors ─► march: K steps per axis ─► shade ─► one decider per row ─► lamps
 x, y, angle, sin/cos    RDX, RDY        each step one W-lane        wall height,
 (bit-sliced lookups)    per column      network; running per-lane   brightness, color
                                         min keeps the nearest hit
```

1. **Game state:** position in 1/256 tiles, angle 0–255.
   - Bit-sliced lookups on the angle give the camera plane and the scaled direction directly.
     There's one decider per output bit, each an OR over the angles with that bit set.
   - Tables are signed (`lookup_scalar(..., bits=32)`): bits with the same key set share one
     decider, so the sign bits cost one. A constant offset (like the spawn facing A0) goes into
     the table, not an add.
2. **Rays:** `RAW = camX * plane + dir << 10 + e` per lane, where camX is a constant vector.
   - The sum is a plain wire join (no combinator). Its sign is the ray's sign.
   - `RD = RAW >> 10`. The rounding term e is picked per angle (`ray_base`) so no column's RD
     is 0 (a 0 lane vanishes); this replaced forcing RD odd, one tick.
   - **Latency rule:** additions of constants or of other vectors can ride on a consumer's
     input wire for free, so fold them in instead of spending a tick on a `+` op.
3. **March (lanes = columns), once for vertical grid lines and once for horizontal ones:**
   - Step k is one network holding every column's k-th crossing. All K steps are computed at
     once from k. Crossing k is at signed distance `along_k = sel(256(k+1), -256k) - FR` along
     the axis (sel per lane by the ray's sign, FR the position within the tile). Then each is one
     division:
     - distance `D_k = along_k * 8192 / RD` (2^11 per tile of ray length);
     - map key `Q_k = along_k * 16 * RO / RD + 16 PO + (P >> 8 << 18) + line_k`: the key of the
       cell entered, << 12. Vertical lines use `64*(x+1) + y`; horizontal lines use
       `64*y + x + 1`, with their own wall table.
   - A lookup decider per step tests key *ranges* (`lo <= Each < hi`, consecutive wall keys
     merged), so there's no `>> 12`. It outputs `-2^30` on wall lanes; added to `D_k` on the
     same wire, that makes hits negative.
   - Vectors shared by all steps (FR terms, the broadcast position) ride on the per-step sign
     selectors: a decider outputs `Each = constant` plus `Each` copied from a shared green wire.
     That avoids a copy of the shared vector per step.
   - A min tree per side over its K leaves (`Each(red) < Each(green)` → copy red, else copy
     green, depth 4) keeps the nearest hit. The last level is split: MV keeps the lanes where
     the vertical side is nearer, MH the rest (ties).
   - Before 2026-11-01 the steps were recurrences with a running-min chain (18 more ticks), and
     then a sign/abs/divide/multiply/add/shift chain (6 more ticks than now).
   - Out-of-map keys can alias real wall keys, but only for crossings beyond the boundary wall,
     which is always nearer. So the map must be closed.
4. **Shade:** DS = M + 2^30 is never formed (the constant rides on each consumer's wire).
   `LH + 1 = (H*2048 + DS) / DS`. Brightness per side is `16*8192 / (DS + 8192)`, which is 0
   on the other side's lanes (where DS is just 2^30) and ≥ 1 within the march. Times the side's
   base color (channels ≤ 15, so no carry between channels), the two sides just add up.
5. **Rows:** one decider per screen row: `LH > |2y+1-H|` → copy the color vector, else the
   row's floor/ceiling color (a decider else-output).

Still to come (M3+): textures via the packed-strip path the sprite demo proved, sprites and
the rest below.

Earlier notes that still hold:

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
6. **Input:** the chest pad, `demos/input_pad.py`; see "Input pad" below.
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
  - It's pipelined, so a new frame can start every tick, with latency around 16 ticks.
- **UPS is not the blocker (M0, measured).** A full 320×200 pixel stage with 63k lamps costs
  about 1 ms per tick, against a 16.7 ms budget. See "M0 results" below.
- **Power means dead pixels.** Substations have to sit inside the lamp screen, as 2×2 holes on
  a grid. **Substation quality is a build parameter (`--substation-quality`, default
  `legendary`, user request 2026-11-01).**
  - Legendary substations power 28×28 and reach 28, so the holes cost about 0.5% of pixels.
  - Normal substations (18×18, as in giftorio) cost about 1.2%.
  - Values come from `conformance/pole_quality.json` (`harness/run.py --poles`); `bp.SubstationGrid`
    derives the lattice from them.

  Plan the HUD and the layout around
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

   `run` prints a warning for every wire the game would drop (before 2026-11-01 these were
   computed but never printed). `FSIM_SHOW=N fsim conform` shows more than 12 mismatches.

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
   - Scripted input: a case's `fsim.inserts` (`[k, entity_number, item, count]`, negative
     removes) puts items into a chest in on_tick at t0 + k, after row k is recorded. fsim applies
     the same list before simulation tick k (`fsim run --input FILE` takes one too). A chest's
     contents are on its wires like a constant combinator's signals, from the next tick
     (`chest_read` case).
3. **Conformance** (`conformance/`):
   - `bp.py` is a stdlib-only blueprint builder. `power()` places substations until every
     entity tile is covered, counting substations already placed. A solid lamp screen needs
     its own substation grid placed first (`b.grid.screen(w, h)`, or the `b.grid.hole` lattice),
     and solid combinator blocks need a gap column for substations every 18 tiles or so.
   - `Blueprint(label, substation_quality)` defaults to `normal` so the conformance cases stay
     vanilla. The generators for players (`demos/*.py`) default to legendary.
   - `gen.py` defines the cases. Each packs many edge values into separate lanes.
   - Golden game recordings: `conformance/game_dump.json`, and
     `demos/golden/gradient_dump.json` for the demo. `cargo test` checks fsim against both.
4. **Demo** (`demos/gradient.py`): a 16×16 screen in the row-stage layout. It matches the game
   on all 24,705 checks.
5. **Netlist compiler** (`compiler/netlist.py`, `compiler/semantics.py`): write the circuit as
   a dataflow graph (vector ops `vv`/`vc`/`vs`, scalar ops `ss`, bit-sliced `lookup_scalar`,
   raw deciders). It:
   - balances latency (inserts shared `Each + 0` delays so every input carries the same frame);
   - gives each consumer its own constant combinator, and duplicates a node whose output must
     join two different networks of one color;
   - places one tile column per time step (right to left from the sinks), each node at the mean
     row of its consumers, leaving the column between steps for constants and relay poles;
   - wires each network as a minimum spanning tree, bridging long edges with medium poles;
   - evaluates the graph without timing (`Graph.evaluate`), using the same specs it emits. That
     evaluation is the reference: the simulated blueprint must match it frame for frame.
6. **Benchmarks** (`bench/m0.py`): synthetic row-stage blueprints at any W×H, written to
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

### Raycaster M1 (`demos/raycast.py`, 2026-11-01)

Flat-shaded walls, floor and ceiling, 16×16 original map, the camera circling and turning on
a script. Built with the netlist compiler at any `--size`.
- **Size:** 464 / 530 / 632 combinators at 64×40 / 160×100 / 320×200, plus lamps. A new frame
  every tick. Latency 16 ticks from the camera port with the game's camera (23 before the
  setup rework, 42 before the parallel march); M1's scripted camera adds its own path.
- **Checks:**
  - `--verify` compares every fsim frame with the netlist evaluation (exact), and samples the
    float raycaster (`ideal()`, within about 2% of pixels: rounding at wall edges).
  - The 64×40 version matches the game on 451,792 checks. Deep pipelines need the harness
    run with `--warmup 60`, so the warm-up state settles before t0 in both the game and fsim.

### Input pad (`demos/input_pad.py`, 2026-11-01)

Decided with the user:
- **Played in god mode.** A character's camera is centered on it and can't zoom out far enough.
  In god mode WASD pans the camera, so every input is "hover the mouse over a button, press Z"
  (drop one item). The user measured holding Z: about 6 drops a second.
- **A button is an n×n block of chests.** n is 9 at 320×200 and scales with H
  (`button_size()`, `--button-size`). At the zoom that shows the whole screen, smaller blocks
  are too hard to hit.
- **Layout (the user's design):**
  - A 3n×3n D-pad: forward/back/turn on the edges, combos (forward+turn, ...) on the corners.
    One cursor can't hold two buttons, so the combos have to be buttons of their own.
  - Fire is in the middle, drawn as a crosshair (user request).
  - Steel chests, with arrows in wooden chests.
  - Use, next weapon and enter sit in a row below, with letters in wood, a display panel icon
    and an indicator lamp. No strafe for now.
  - **Limits:**
    - n ≤ 9: the fire button's chests wire across one ring cell to their sum.
    - The fire button's indicator lamps (corners = fire, edge middles = directions) only get
      power if n ≤ substation d − 2, because substations can only sit outside the solid ring.
      At n = 9 that means legendary; the center tile is out of reach even then.
- **Held vs pressed** (all buttons at once, lanes = `BUTTON_SIGNALS`):
  - The chests' total per button goes through a one-tick difference to get drops.
  - A countdown (two deciders with feedback) holds HELD for `--hold` ticks after each drop.
    The default was 20; the user found the release too slow, so it is now 14.
  - PRESSED pulses on the first drop after a release.
  - Taking items out never counts.
  - The module docstring has the formulas and timing.
- **Checks:**
  - `--verify` compares fsim with the Python reference: 780 scripted ticks with jittered drops,
    taps and sweeps, 0 differences at n = 3, 5 and 9.
  - The legendary n=9 pad matches a Space Age game on 28,116 checks (`harness/run.py --dlc`
    with scripted inserts), and it is fully powered.
- **Test-pad meters** (`meters=True`), next to the decoder:
  - a press counter per button;
  - the longest gap between drops per button (gaps under 60 ticks). The shortest safe
    `--hold` is that + 1.
- **Waiting on the user:** the gap meter's reading while holding Z. Set `DEFAULT_HOLD` to it + 1.

### M2: movement (`demos/game.py`, 2026-11-01)

The raycaster driven by the pad: walk, turn, slide along walls. Screen, then the pad on its
right, then the player logic below the pad.
- **State:** one register REG (`Each + 0`, green self-loop) holds X, Y (displacement from
  SPAWN) and R (unbounded turn; the view angle is (R + A0) & 255).
- **Player logic:** a netlist graph with **ext inputs**: `Graph.ext(entity, pos, ...)` is an
  entity placed by the caller, feeding the graph at time 0. Here that's REG and the pad's HELD
  network.
  - **Nothing on the input path waits for collision** (user idea, 2026-11-01). From REG, every
    tick, whatever is held, lat = 2 ticks behind REG: the step forward and back for the current
    angle (signed lookups on R & 255), and for each corner of the player's box whether it's in
    a wall (range tests on X and Y straight from REG, wall runs merged).
  - Four gate deciders (forward/back × x/y), fixed next to REG, built with `Node(...)` directly
    so the compiler doesn't balance their inputs: button held, the step component points that
    way, both box corners on that side free → copy the component into REG.
  - So REG(t) = REG(t-1) + gates(HELD(t-2), REG(t-2-lat)) + turn(HELD(t-2)). The reference loop
    evaluates the gates with `Graph.evaluate(inputs=...)` on those two inputs.
- **Collision:**
  - The player is a box of half-size MARGIN + (lat+2)·SPEED = 88. That covers how far REG
    moves between the value the corners came from and the value a step lands in.
  - Each axis is gated alone on both corners of its leading side, so you slide along walls and
    can't cut corners (checking only the center line once let diagonal moves cut in).
- **Startup:** a gate needs a held button, so a pipeline that's still filling adds nothing.
  (The previous design needed offset values and `Each - C1` for this.)
- **Camera path:** REG reaches the raycaster's camera port (a pole left of the screen, the
  raycaster graph's ext input) over a big-pole line above the screen.
  `raycast.build_graph(w, h, camera)` takes the camera from the caller; M1's script is
  `scripted_camera`.
- **Checks:**
  - `--verify`: REG matches the reference on every tick, the player box never touches a wall,
    and sampled frames match the raycaster evaluation, at 64×40, 160×100 and 320×200.
  - The 64×40 build matches the game on 1,209,416 checks (`harness/run.py --warmup 60`,
    scripted inserts, 400 ticks).
  - 160×100 and 320×200 import in Space Age, fully powered, at 0.40 and 0.88 ms per tick.
- **Input-to-screen latency:** about 21 ticks (0.35 s) for both turning and walking: pad 3,
  gate or turn decider into REG 2, raycaster 16 with the lamps.
  - **History:** about 56 ticks at first (the user felt "almost a full second"), then 27 for
    turning and 35 for walking, then this.
  - **The raycaster's 16:** angle 1, camera lookups 1, ray products 1, ray shift / sign
    selectors 1, key product 1, key division 1, hit lookup 1, min tree 5 (4 per side + the
    split root), shading 2, rows 1, lamps 1.
- **Further cuts if we want them (not done; option B, discussed 2026-11-01):**
  - **Angle ROM:** everything that depends only on view angle and column (ray components, the
    per-step slopes and distance steps, the sign selections) precomputed for all 256 angles in
    constant combinators, with one decider per angle selecting its set (condition on the angle
    on green, `Everything` from the constant on red). Each frame then only multiplies by the
    position within the tile (`Each * FR` per step, plus per-step copies of the angle-only
    vectors), then the hit lookup. Hits at about 4 ticks from the port instead of 7, so the
    raycaster would be about 13.
    - Per side it needs about 6 vectors (D: the FR coefficient, the k step, the constant part;
      key: the same three), so 12 × 256 constants of W values: about 0.5M values at 160 wide,
      1M at 320. The 320×200 blueprint string would grow from about 1.5 MB to an estimated
      8–10 MB, and the 3,000 or so selector deciders cost about 0.3 ms/tick.
    - Splitting the per-step terms by sign (`Each * (256 - FR)` on lanes going + and `Each *
      FR` on lanes going −) removes the sign-dependent constant vectors.
    - Do it after M3: textures make the min tree carry more per column, which changes what the
      march must produce.
  - **Bounded angle:** a wrap decider keeping R in about [-4, 259] (reading REG plus its own
    output, so it fires once) would let lookups key on R directly with the `& 255` folded into
    the tables: 1 tick off the raycaster and off the step lookups (box 78).

## Status (2026-11-01)

Done:
- The design above, and checks of the 2.1 features it depends on.
- fsim, the harness, the conformance suite (15 cases, including chests and range lookups) and the gradient demo.
  All match the real game exactly.
- The input pad (above): decided with the user, matches the game.
- M2 movement (above): matches the game.
- Measured semantics: see the README table, and `sim.rs` / `conform.rs` doc comments for the
  timing model.
- Screen size decided (160×100, aiming for 320×200), and M0 UPS measurements: 320×200 fits.
- Freedoom sprite assets and the sprite demo (above): the texture path works end to end in fsim
  at three sizes and in the game.
- The netlist compiler and the M1 raycaster (above).

Commit only when the user asks.

## Next steps (in order)

1. **M3:** textures (the sprite demo's strip path, keyed by wall type and texture u from the
   march) and distance shading per texture.
2. **M4:** sprites, enemies, shooting, HUD, doors. M5 (stretch): angled walls, varying heights.
3. **Tooling as needed:** fsim speed for 320×200 work (lookups are about 100× slower than the
   game); the Python netlist evaluation takes about 3 s per frame at 320×200.

## Rules for working here

- **Never rely on unverified game behavior.** Add a conformance case to `gen.py`, run it in the
  game, make fsim match, and refresh the golden dump. Several obvious guesses turned out wrong:
  the min tie order, lanes when red + green sums to 0, wire reach being measured footprint to
  footprint, and lamp color lagging its condition by one tick less.
- **Pole-to-pole circuit wires are measured center to center** (≤ 9 for medium poles), not
  footprint to footprint (`pole_reach` case). A relay chain spaced by footprint gap silently
  lost its wires in the game. `Blueprint.check_reach()` checks both rules; call it on every
  generated blueprint.
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
- **Substation quality:** player blueprints use legendary substations by default. Above-normal
  quality only exists with the quality mod, so:
  - Check them with `harness/run.py --dlc`.
  - Game comparisons without the DLC (`harness/run.py` then `fsim conform`) need
    `--substation-quality normal` when generating.

  fsim ignores entity quality (its pole reach is normal-quality). That's harmless while
  substations carry no circuit wires.
- **Screen lamps need `always_on`** (`b.lamp(..., always_on=True)`), or they stay dark in
  daylight. The harness runs at night, so it can't catch this.
- Python is stdlib only. Game paths come from `FACTORIO` and the Steam default in
  `harness/run.py`. Harness runs take about 2 s.
- The harness mod list disables space-age/quality/elevated-rails, so signals and the signal
  order are vanilla.
- The sandbox once lost all access to `~/Documents` mid-session (EPERM on everything). If that
  happens, stop and ask the user (a restart fixed it) rather than piling up workarounds.
