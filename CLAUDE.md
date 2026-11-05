# factorio-doom: agent brief

Read this first. [README.md](README.md) has tool usage and the table of measured game semantics.

## Goal

A playable Doom-style first-person shooter built entirely from **Factorio 2.1 combinators** (no
mods), shown on a lamp screen. Factorio 2.1 added decider else-outputs, which is what makes this
look feasible now. **Space Age is required (user decision, 2026-11-02):** the screen packs six rows
per network on the six signal qualities (architecture item 12).

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
  in `conformance/signal_order.json`). Space Age has six qualities per signal (normal, uncommon,
  rare, epic, legendary, quality-unknown; 13,446 signals in `conformance/signal_order_dlc.json`,
  `harness/run.py --signals --dlc`). fsim uses that order for any blueprint with a Space Age quality.
- **Quality is a lane axis (Space Age, measured: `conformance/cases_dlc`):** Each arithmetic and
  Each deciders keep qualities apart, lamps read a signal at a quality, and a selector in quality
  transfer mode (static quality, destination Each) moves *every* input signal to one quality in one
  tick (`Graph.requality` / `Graph.spread`), summing signals that differ only in quality. That is
  the one cheap permutation: a vector copied onto other lanes. Quality filter keeps one quality
  (quality-unknown ranks above legendary in comparisons).

## Renderer architecture

M3 (Freedoom-textured walls, floors and ceilings) is built and verified; see "Raycaster" below.
The original plan had one column unit (~20 combinators) per screen column. The built design is
far smaller: **the whole ray march is vector ops with lanes = screen columns** (about 700
combinators at any width), plus a fixed texture lookup block (2 × types × tex × tex/8 deciders,
4,096 for 4 textures at 64) and **one pipeline per chunk of six screen rows** (item 12), about 85
combinators each (walls, flats, things) plus the weapon's.

```
Game state (scalars) ─► ray vectors ─► march: K steps per axis ─► split ─► texture ─► row pipelines ─► lamps
 x, y, angle, sin/cos    RDX, RDY        each step one W-lane      by side   strip words,  v, mux, shift,
 (bit-sliced lookups)    per column      network; min trees keep             palettes ×    palette
                                         the nearest hit                     brightness
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
     - distance `D_k = along_k * 2^17 / RD` (DS = 2^15 per tile of ray length; the numerator
       stays under 2^30);
     - map key `key_k = along_k * 16 * RO / RD + 16 PO + line_k`: the key of the cell entered,
       << 12, with the position across the line (the texture column) in the low 12 bits.
       Vertical lines use `64*a + y`, horizontal lines `64*a + x + 1`, where the line index a is
       `x + 1` (or `y`) for rays going + and `NEG - x` (`NEG - y`, NEG = 64) for rays going −.
       So **keys grow with distance either way** (`cell_keys`), and line_k is `(C + k + 2) << 18`
       or `(NEG + k + 1 - C) << 18` from the cell C: one decider per sign, each adding its
       broadcast (16 PO ± C << 18) from green.
   - A lookup decider per step tests key *ranges* (`lo <= Each < hi`, consecutive wall keys of
     both formats merged), so there's no `>> 12`. It outputs `HIT = -2^30` on wall lanes.
   - Vectors shared by all steps (FR terms, the broadcast position) ride on the per-step sign
     selectors: a decider outputs `Each = constant` plus `Each` copied from a shared green wire.
     That avoids a copy of the shared vector per step.
   - Two min trees per side (`Each(red) < Each(green)` → copy red, else copy green, depth 4):
     over `HIT + D_k` (the distance) and over `HIT + key_k` (the same nearest hit, since keys grow
     with distance: its wall type and texture column). The key tree pairs its leaves the other
     way round, so each hit decider feeds one red and one green network (no duplicate). The
     key leaf's `key_k` is delayed one tick (`Each + 0`) to meet the hit decider's output.
   - Before 2026-11-01 the steps were recurrences with a running-min chain (18 more ticks), and
     then a sign/abs/divide/multiply/add/shift chain (6 more ticks than now).
   - Out-of-map keys can alias real wall keys, but only for crossings beyond the boundary wall,
     which is always nearer. So the map must be closed.
4. **Split and texture** (`build_graph`):
   - The last D level splits by side: MV = `Each(red) < Each(green)` → copy red plus a constant
     2^30, which turns `HIT + D` into D (MH: `>=`, copy green, the ties). The two never share a
     lane, so `[MV, MH]` on one wire is D.
   - Per side, the texture key `TK = (type + 1) << 12 | u`: u from `key AND (4096 - 4096/tex)`,
     the type from range deciders on `HIT + key` (one per wall type).
   - **Strip lookups:** one decider per (side, type, texture column, word): `Each(red TK) = key
     AND Each(green MV) > 0` → `Each = word`. Gating by the side being nearer gets the words out
     one tick after the split (masking TK first would cost a tick). Each word holds 8 texels,
     texel n at bits 28 − 4n. Fixed block below the row stage, filled column by column.
   - **Palettes:** textures are quantized to 14 colors each, stored at 4 bits per channel (the
     H side ¾ as bright). Per (side, type, texel value) a decider gated the same way outputs that
     color: one bus per texel value. Times the brightness `17*FOG / (D + FOG)` (≤ 17, so ≤ 255 per
     channel): `C_t`, one product per texel value, fixed above its palette column.
   - **Color grade (user request, 2026-11-02):** in game the screen looked dark and washed out
     (lamps draw a dark frame around each lit square). Every palette (walls, flats, things) goes
     through `sprite.grade` before the 4-bit store: a gamma lift on the brightest channel,
     scaling all three so hue holds and nothing clips (a per-channel clamp flattened SP_HOT1 to
     solid red), and a saturation boost. Fog (tiles to half brightness) went from 4 to 8. Mean
     luma of sample frames 28 → 61. Constants only: no combinators, no latency.
   - **Build parameters (user request):** `raycast.Shading`, flags `--gamma` (default 0.5),
     `--saturation` (1.25) and `--fog` (8) on `raycast.py` and `game.py`. `--gamma 1
     --saturation 1 --fog 4` gives the old colors.
5. **Rows** (now per chunk of six rows, item 12; per row as written here before 2026-11-02):
   - `V = D / c_y` with `c_y = round(H * 2^14 / ((2y + 1 - H) * tex))`: 4 × (texel row − tex/2).
     The wall covers `|V| < 2 tex`, so there's no wall-height vector.
   - Mux: per word j, `Each(green) != 0 AND lo_j <= Each(red V) < hi_j` → copy the word bus. The
     green condition makes the decider visit lanes where V is 0 (absent). Plus `bg`: V out of the
     wall → `Each = -1`.
   - `T = word << (V AND 28)`: the texel is in the top nibble (all 1s = floor/ceiling).
   - Palette: one decider per texel value, a range test on the top nibble (signed) → copy `C_t`;
     nibble 15 → the floor/ceiling (below, or the row's flat color with `--flat 0`).
6. **Flats (floor and ceiling)**, in the same row pipeline (`--flat`, texels per tile: 8, 16 or
   32; default 16; 0 = a flat color per row). Freedoom flats FLOOR4_8 and TLITE6_4, one per tile,
   quantized to 16 colors.
   - Row y shows the floor (r = 2y + 1 − H > 0) or ceiling at distance H / |r| tiles, the same
     for every column. So per row, per axis: `F = RAW * K_y + (P << 24)` with `K_y = round(H *
     2^12 / |r|)` is the pixel's **position within its tile times 2^32**. The product wraps
     (int32), which drops the whole tiles for free; it means one flat for the whole map.
     `P << 24` is added with `Each + scalar` (a broadcast would need a W-lane constant per row).
   - Its top fb bits are the texel: one decider per word (flat row v from FY, u >> 3 from FX;
     32 at flat 16, `top_range`) outputs 8 packed texels. `Sf = (FX >> (30 − fb)) AND 28` picks
     the texel (two ops: the shift amount's 5 bits include 2 sub-texel bits), so the words wait
     one tick in a single `Each + 0` (they sum on one wire).
   - Palette: one decider per flat color, `wall texel = 15 AND flat texel = c` → that color times
     the row's brightness (a constant, since the row's distance is). An absent flat lane reads 0,
     which is color 0, so zero words need no decider.
   - It starts from RAW delayed to land with the wall texel, so it adds no latency.
   - **Per chunk now (item 12):** the flat's color comes from one decider per flat color (the
     texel, gated by a vector of ones so an absent texel reads as color 0) times a brightness
     vector, and a flat select (`V` out of the wall, bounded so killed pixels miss it) copies it,
     two delays before the palette tick. The wall's texel path no longer needs the floor marker.

7. **Things (M4 sprites, `demos/things.py`, 2026-11-01):** monsters and items at map positions
   (`THINGS`, up to 8), Freedoom frames on the walls' texel grid (STEX = 64 per tile) in a box of
   32 rows from 64 units up to the floor (eye level at its top until item 14 lowered the eye, so
   only the lower half of the screen had thing rows). The module docstring has the formulas.
   - **Per thing (scalars):** depth `alpha = rx DX + ry DY` and lateral `L = ry DX - rx DY` (DX,
     DY, -DY lookups on green, shared by every thing's products; on one wire with rx the compiler
     copied the table per thing). Per column `U = camx * (alpha / 1551) - L + C` is the box column
     times 2^12.
   - **Nearest per column:** two min trees over the things (absent lanes lose: `(red < green AND
     red != 0) OR green = 0`). `Zt = (alpha >> 10) << KB + U` gives the box column ordered by
     depth; `Dt = alpha * 64 + frame` gives the precise depth and the frame. They can disagree
     only when two things are within 1/256 tile of each other. One thing per column: a farther
     thing is hidden by the nearer one's box columns, even where it is transparent.
   - **Live monsters first (2026-11-04, user report: monsters behind a corpse were invisible):**
     with the `back` hook (game.py: items always, a monster while dying or dead) a yielding
     thing's leaves carry 2^30 more on Zt and 2^29 more on Dt (`YIELD_D`), so it loses the trees
     wherever its box overlaps a live monster's; Dt drops it after the trees (an AND), Zt's goes
     with SK's AND. Only in front of NEAR (a decider per monster on its alpha and flag, the bias on
     dc's wire and on every column of the Dt leaf's green input; items as constant outputs), so
     one behind the camera still fails NEAR. The live monster is drawn whole; the corpse is cut
     where the live box covers it (the full fix, two layers per column, would double the thing
     path and its lookups). The AND costs the thing path a tick, so the walls wait a tick after
     their min trees (four delays: waiting after the strips took one per strip, 4,096, and broke
     the layout): the game's raycaster is 18 ticks. raycast.py alone (no hook) is unchanged.
   - **Lookups per (frame, u):** a mask word (bit 31 − t: row t opaque) and 4 texel words, keyed
     by `SK = Zt AND (2^KB - 1)` (ranges) and `FR = Dt AND 63`. They sit in the strip block after
     the wall strips.
   - **Kill, no extra latency:** per thing row, `Q = Dt / 4cs_y`, `P = 2 ^ Q` (constant-first
     Each power: 0 once Q ≥ 32, `arith_const_each` case), `OM = mask * P`, then `OM < 0` and
     `D - Dt/512 > 0` → 2^29 onto the wall's V network. The wall's mux ranges and the bounded bg
     (|V| < 2^28) miss it, so the wall and floor output nothing there. It has to arrive with V, so
     the thing path is timed back from V: trees out 4 ticks before it, which fits 8 things.
   - **Texel path:** `Vs = Dt / cs_y`, mux per word, shift, and 15 palette deciders copying
     `color * brightness` gated by the thing being in front (one bus per color, like the walls).
   - **Layout:** the kill path (`sq sp som kill`) sits before `v` in the interleave. The thing
     palettes and texel nodes are woven between the wall palettes (`palette_run`), so rows
     without things still chain their palettes within reach.
   - **Cost at 160×100:** about 1,900 combinators (8 things, 6 frames; 14,912 in all), and no
     latency.

8. **HUD (status bar, `demos/hud.py`, 2026-11-02):** Freedoom's STBAR in Doom's layout across the
   bottom `bar_height(H) = round(32 H / 200)` rows (6 / 16 / 32 at 64×40 / 160×100 / 320×200):
   ammo (the current weapon's; blank for the fist), health %, armor %, the face (5 pain levels and
   dead, Doom's formula), the ARMS panel (every weapon owned) and the ammo list (all four counts
   live). Art in `assets/freedoom/hud.json`.
   - **View crop:** `build_graph(..., hud=...)` renders only `hv = H - bar` view rows (r = 2y + 1
     - hv) but keeps the full screen's projection (`proj = H` in c_y, `row_scale`, the thing
     rows): walls keep their size and the view is cropped, as in Doom. Without a HUD hv = proj = H
     and the blueprint is byte-identical to before.
   - **Circuit:** per bar row a constant combinator (right of the screen) holds the static row.
     Per (slot, glyph, bar row) one decider outputs, on the slot's columns, that glyph's row
     minus the static row: exact 8-bit colors (box-filtered from 320×32 at build time, then
     graded with `Shading.rgb`), so no palette or texel path. A slot shows at most one glyph.
     Conditions on three networks one tick from the values HS: hundreds `100g <= N < 100(g+1)` on
     N, tens `10g <= N%100 < 10g+10` (tens 0 also needs N >= 100 on green), ones `N%10 = g`, face
     a health range. Leading zeros blank, negative values show nothing; glyph sets come from
     `MAXIMA` (Doom's limits with a backpack). The game's current-ammo value: per ammo type a
     decider copies the count while the weapon uses it (the fist: -1), `Each + 0 → CUR` sums them.
   - **Timing:** HS is delayed to land the deciders on the view rows' tick, so bar and view always
     show the same frame (raycaster latency unchanged).
   - **Shared pixels:** Freedoom's digits are 13 wide, so at odd scales two slots can share a
     screen pixel. It belongs to the slot covering more of it (`Hud._own`); the other slot's glyph
     doesn't change it, so every pixel is always one valid composite (summed deltas could carry
     between packed channels). None at 320×200, 27 at 160×100.
   - **Cost:** 356 / 783 / 1,391 deciders (busiest row 98, left of the screen like the row
     pipelines), but the bar replaces that many view-row pipelines, so the total drops: 160×100
     raycaster 14,165 combinators (14,912 before).
   - **Checks:** `--verify` adds "status bar vs the composed bar": the reference (`Hud.image`)
     picks glyphs the way Doom's STlib_drawNum does, independently of the deciders.

9. **Weapons (`demos/weapons.py`, 2026-11-02, user request; overlay reworked by item 12):** seven Freedoom weapons (pistol,
   shotgun, chaingun, rocket launcher, plasma rifle, BFG, fist; art in
   `assets/freedoom/weapons.json`), the number buttons select them (item 18; "next weapon"
   cycled them before), and fire plays Doom's animation
   with its muzzle flash.
   - **Pictures:** Doom's weapon states (info.c) converted from tics to ticks; the flash layer is
     composed over the gun at build time, giving 32 composites ("pictures"). Placed like
     R_DrawPSprite (x = 1 - left offset, top = viewheight / 2 - 68 - top offset in a 320 × 168
     view, or 200 without the bar), box-filtered to the view (opaque where at least half the
     footprint is), graded with `Shading.rgb`. No bob.
   - **Raise/lower on a switch (user request, 2026-11-02):** Doom lowers the gun 6 px a tic to
     96 below (16 tics, 27 ticks), switches, raises the new one. The ready frames are drawn at
     `DROPS` = 8 steps of 12 px: 56 more pictures (88 in all), only +22% deciders since a lowered
     gun covers few rows. (Measured: 4 steps +7%, 16 steps +51% with the busiest row 77.)
   - **Overlay (`build_graph(..., weapon=...)`, since item 12):** per (picture, chunk) two
     deciders (condition `F = id`): a kill, adding KILL to the chunk's V on red (no wall mux or
     flat select fires) and to the things' Vs on green (no thing texel), and the colors, onto the
     chunk's output at the palette tick. No compositor, so no extra tick. (Before: per (picture,
     row) one decider and a compositor per row, one tick more.) The kills also nominally read the
     flat select's output on green (`Node.nominal`: wired, not evaluated), so that network runs
     through the weapon block without relay poles.
   - **Cost:** before item 12: 469 / 1,184 / 2,347 overlay deciders plus one compositor per view
     row at 64×40 / 160×100 / 320×200.
   - **State (game):** W (weapon index, 0 = pistol), A (animation tick: 0 ready, 1..L firing,
     128.. lowering, 155 down, 192.. raising) and P (the pending weapon, item 18; D, switches
     queued, before) live on the state bus, REG's
     red output network, held by deciders that read the bus and write back onto it
     (`weapons.logic`): pending set/hold, weapon hold/set, anim advance (copy A
     + 1), start (A = 1), lower, down (hold), raise. Fire held from ready starts the cycle if
     there's ammo; at A = R (Doom's A_ReFire) it restarts while fire is held, else runs on to L
     and back to 0. Per weapon a spend decider at its shots' A values (with an ammo check each,
     so the chaingun's second shot) writes `ammo -cost` into REG's input. Relays put fire held
     and the selected weapon (SEL) onto the bus, so every weapon decider reads one network. The raycaster's picture F is a lookup on `W * 256 + A`, its bits sized from the table: the
     88 pictures need 7, and with 6 (sized for the first 32) the BFG and fist drew the pistol's
     and shotgun's pictures, which the checks missed (they evaluate the same lookup). The user
     found it in game; `lookup_scalar` now refuses values that don't fit its bits.
   - **Switching (item 18 since 2026-11-03):** selecting another weapon sets P; while P > 0 a
     ready or firing gun starts lowering (cancelling a shot); at the bottom W becomes P - 1, then
     the gun rises (and lowers again at once if a selection came while it rose). A selection
     while it lowers replaces P, so one lower and one raise, about 0.9 s. Fire is ignored while
     switching.
   - **Checks:** game `--verify` also compares the bus (fsim `--net`) with the reference every
     tick, and the reference with `weapons.step`, the same rules written as plain Python: 0
     differ at 64×40, 160×100 and 320×200, frames exact. The real game matches the 64×40 build
     on 8,545,891 checks (switches, queued taps, fire during a switch); 320×200 runs at 2.09
     ms/tick in Space Age (1.72 before weapons), 25,747 combinators, blueprint string 3.7 MB. Without a HUD
     and weapon the raycaster's blueprint is byte-identical to before.

10. **Thing contact (`demos/touch.py`, 2026-11-02, user request):** walk over an item to pick it
    up, stand near a monster and it hurts; monsters are solid.
   - **Pickups:** Doom's amounts and caps (`ITEMS`: medikit +25 to 100, green armor sets 100,
     ammo box +50 bullets to 200, shell box +20 to 50, ...); a full stat leaves the item, as in
     Doom. Per item a bus flag GONE_i (`signal-0`..`7`, held by one decider copying them all) is
     set by a decider: player within PICKUP (80/256 tile per axis) of it, not gone, alive, the stat
     below its cap. The increment `min(amount, cap - value)` needs the value, and deciders only
     copy or output constants, so it's a bit-sliced constant table over value ranges (like
     `lookup_scalar`, adjacent ranges merged) gated by the same condition, into REG's input.
   - **Hidden when gone:** the raycaster reads GONE_i on its port and adds `things.HIDE` (-2^23)
     to the item's depth (one more term on alpha's wire, delayed to meet the products), so both
     leaves fail NEAR and it isn't drawn. No latency.
   - **Pain (until 2026-11-03, replaced by monster attacks, item 15):** a bus clock COOL: a
     monster within a tile, COOL = 0 and alive -> COOL = 40, counting down; at its start tables over
     (armor, health) took 9.
   - **Solid monsters:** their boxes join the walls in the corner deciders (`player_graph`), at
     the collision box's half-size (88, so the player's box can't straddle one between its
     corners). You slide along them like walls.
   - **Starting ammo** is now below the caps (`hud.START`: 50 bullets, 20 shells, 10 rockets,
     100 cells) so ammo pickups have room.
   - **Cost:** 36 deciders for the default 8 things (4 items, 4 monsters), about 1,600 conditions
     (the armor table is most of it); no latency.
   - **Checks:** `touch.step` is the same rules in plain Python. game `--verify` compares it with
     the evaluated bus and REG every tick; the scripted walk takes an unarmored hit, picks up the
     armor, takes an armored hit, then the ammo box and the medikit.

11. **Shooting (`demos/combat.py`, 2026-11-02, user request):** hitscan along the middle of the
    view kills monsters, with Freedoom's pain and death frames on Doom's timing.
   - **Aim (raycaster side, `combat.aim_stage`, the `aim` hook of `build_graph`):** it already has
     every thing's depth alpha and lateral offset L (`column_stage` returns them) and the wall
     distance per column. `DC` = the middle column's D × 8 (alpha's scale); per live monster
     A_i = alpha_i if |L_i| < HIT_R (24 Doom units), 0 < alpha_i < DC, else BIG (an else-output);
     then B_i: A_i the smallest (ties to the lower index) → `AIM = i + 1`, and with alpha_i < MELEE
     also `CLOSE = 1`. Corpses don't block shots, items never do.
   - **B's outputs are wired to the camera port** (the port ext node lists them as red inputs, so
     the compiler wires them to it): the port is the state bus, so AIM and CLOSE land on the bus,
     τ = 14 ticks behind REG (newer than the frame on screen, 18). No raycaster latency.
   - **Monster state (bus, per thing index):** `DMG_i` (damage taken, held) and `MS_i` (animation
     clock: 0 standing, 1..PL pain, DEATH = 64.. dying, CORPSE held). At a weapon's shot ticks
     (the spend deciders' A values and ammo checks) with AIM = i + 1 (and CLOSE for the fist) and
     alive → DMG_i += Doom's mean damage (pistol 10, shotgun 70 (all pellets), chaingun 10,
     rocket 100, plasma 22, BFG 450, fist 11; no randomness). Pain starts only from standing on a
     hit that doesn't kill; death starts the tick after DMG reaches the health (imp 60, zombieman
     20). Signals: DMG = `signal-red`..`grey`, MS = punctuation signals, AIM `signal-I`, CLOSE
     `signal-M`.
   - **Frames:** the raycaster adds a lookup on MS_i (from the port) to the standing frame's id,
     broadcast onto the dleaf's green input (`column_stage` `frame` hook); `combat.frames` adds the
     pain/death frames to `things.load`. The dying imp's box is wider (32 texels), so Zt drops one
     depth bit (`qd`) to keep a hidden item's key in range.
   - **Dead monsters** stop attacking (item 15) and stop being solid (the corner deciders
     test DMG < health; the reference feeds them the bus, not just REG).
   - **Fist reach is a tile:** Doom's is 84 units to the centre, but our monster boxes carry the
     latency allowance and stop the player 0.53–0.69 tile away per axis.
   - **Cost:** about 25 aim combinators in the raycaster, 37 bus deciders for 4 monsters, ~250
     more thing lookups (14 more frames). The bus writers now snake in columns after the spend
     deciders; their chain joins the bus through one writer's own input (it reads the bus).
   - **Checks:** `combat.step` (plain Python) against the bus every tick; the aim against a float
     hitscan (`combat.ideal_aim`: DDA along the middle column's ray, as the circuit) from the bus τ earlier, skipping near-ties (corner grazes,
     within 0.02 tile). The scripted fight (`game.COMBAT_SCRIPT`, ticks 700–1340; `FIGHT_SCRIPT` since item 15): shotgun the
     zombieman (dies), chaingun the imp (four bullets, two pain flinches), switch to the fist while
     walking up to it, two punches (dies).

12. **Chunks, layout and latency (2026-11-02, user request: "the layout is abysmal", fewer
    combinators, keep latency):**
   - **Six screen rows per network.** Row q of a chunk is quality q of its column's lane signal
     (`QUALITY_LANES`, `q_signal`, `DEFAULT_ROWS` 6; one row per network no longer lays out: its
     blocks are one row tall, with no room for relay poles). Chunks never straddle
     the horizon (floor and ceiling flats differ) or the status bar. Per-row constants (c_y, the
     flat's K_y and brightness, the things' cs_y, the background) become constant vectors over the
     chunk's lanes, and every row op a vector op (`vv` with the constant on green).
   - **Spreads:** per-column vectors the chunks read (D, the 8 word buses, the 14 color products,
     RAW, the things' Dt, mask and word buses, thing visibility) are copied onto every quality by
     six quality transfers each (`bus()` in `build_graph`; about 190 selectors), one tick. The
     weapon's kill (item 9) saves the compositor's tick, so the raycaster stays at 18, and the
     angle wrap (below) makes it 17.
   - **Count:** 160×100 game 8,724 combinators (16,764 before), raycaster 7,624 (14,165);
     64×40 7,648. Relay poles at 160×100: game about 520 (2,694 before), raycaster 403 (about
     1,000). Blueprint strings grow (every lane signal names its quality, and each chunk has a few
     constant vectors): 160×100 1.7 MB (1.4), 320×200 5.9 MB (3.7).
   - **Pad** right beside the screen (x = w + 2; the user zooms in on the lamps); the bus row ends
     in a substation in the 2-tile gap. `link_power` bridges a copper link longer than the reach
     with a substation in between (the pad can take a lattice spot).
   - **Layout by concept** (`place_chunks`): chunk blocks left of the screen, each k rows tall at
     y = k m, the same (column, row) per role in every chunk (`ROLE_GROUPS`: flats, weapon kills,
     wall and thing paths, palettes, weapon colors; a free column between groups for relay poles),
     so each bus runs straight down one column; status bar chunks below; the spreads and per-color
     products just above (`area`, the free spot nearest their consumers, empty role slots
     included); the texture and sprite lookup block above them; the march as a grid left of it
     (`place_march`: per side a row per step, its ops in `MARCH_COLS`, then the min trees between
     their inputs, the split and the texture keys); the rest (camera lookups, things, aim, status
     values) placed by the compiler left of that, its stacks now centered on their consumers
     (isotonic regression) instead of piling downward. Compiler copies (`dup`) go next to their
     original. Row blocks for things and the camera were tried and cost more poles (fan-out).
   - **The state bus rides a substation row:** substations on the lattice row through the top of
     the screen (y = offset), chained by red wire from right of the screen (REG, through relay
     poles) to the camera port left of the column stage; the port ext lists the rest as `taps`, so
     every bus reader wires to its nearest one (`_mst(joined=...)`). No big-pole line.
   - **Angle wrap (latency, the "bounded angle" option below):** two deciders next to REG
     (`angle_wrap`) add ∓1024 to R when R + their own output (green loop) leaves [0, 1024), so a
     correction fires once; R stays in `angle_range(turn)` (two ticks of the fastest turn either
     side) and the camera, things and step lookups key on R directly (the `& 1023` and `>> 2` in
     the tables). The position's `+ SPAWN` rides on its consumers' wire after the `| 1` (SPAWN is
     even). Raycaster 17 ticks from the port, the movement path 1 tick behind REG (box 78).
   - **Checks:** raycast and game `--verify` at 64×40, 160×100 and 320×200: every frame matches
     the evaluation; the scripted fight got a turn tap before the punches (the smaller box changed
     the walk). The 64×40 game matches a Space Age game on 13,682,223 checks (1,340 ticks,
     `harness/run.py --dlc --warmup 60`, about 13 minutes, a 5 GB dump). 320×200: 10,736
     combinators (26,819 before), 1.14 ms per tick in Space Age (1.97 before); 160×100 0.58 ms
     (300-tick medians, `harness/run.py --dlc --bench 300`). A constant combinator section holds at most 1,000 filters: a 320-wide
     chunk's constants (1,920 lanes) failed to import until `Blueprint.constant` split them into
     sections.

13. **Death and restart (`demos/game.py`, 2026-11-02, user request):** at health 0 the player is
    dead: frozen, the gun lowered, the face dead. Use starts a new game.
   - **Dead** = `HEALTH <= -hud.START["health"]` on the bus.
     - Walking stops because every corner decider gets one more OR clause, "dead", so all four
       corners read as walls. The corners run lat ticks behind REG, which also covers the
       restart's jump back to SPAWN.
     - Turning stops through a dead-flag decider (`dead_flag`, next to REG). It reads the bus on
       red, and its green output joins the turn counter's network, where the turn deciders require
       `DEAD` (`signal-O`) = 0. The bus is red-only, so the flag needs a network of its own.
     - The gun lowers and stays down, as in Doom's A_Lower, and fire and weapon selections are ignored
       (`weapons.logic(..., health)`, `weapons.step(..., alive)`).
   - **Restart:**
     - A relay puts Use (`signal-U`, pad or keyboard) on the bus. While dead, a decider next to REG
       outputs `RESTART` (`signal-Z`) for a tick. Its red output is on the bus and its green output
       on REG's green input.
     - **REG is now a decider,** `RESTART = 0 -> Everything`: its own output on green, the
       increments on red. It drops everything on that tick, so REG is exactly 0 the next tick
       (negating REG into its input would lag and race the increments).
     - Every bus writer gets `RESTART = 0` ANDed into each clause (`gated`), so everything the bus
       holds drops to 0 together: weapon, animation, pending weapon, GONE, COOL, DMG, MS. Items
       come back and monsters come back alive.
     - The REG writers needed no gate while only a dead player could restart (REG drops its input
       that tick, and none of them fires for a dead player); the reset button (item 18) gated them
       too.
   - **Cost:** two deciders and a relay. No latency.
   - **Checks:** game `--verify` ran `DEATH_SCRIPT` after the fight (default `--ticks` 2280; part of `FIGHT_SCRIPT` since item 15):
     - Walk up to the zombieman and die there.
     - While dead, press fire, switch, turn and walk; all are ignored.
     - Press Use, then walk and fire.

     It checks the restart flag against its rule, that REG and every bus state are 0 after a
     restart, and that nothing moves while dead (once the pipeline has caught up).
   - **Results:**
     - 0 differ at 64×40, 160×100 and 320×200.
     - The 64×40 game matches a Space Age game on 23,282,167 checks (2,280 ticks,
       `--dlc --warmup 60`, about 28 minutes and a 9 GB dump).
     - 320×200: 10,740 combinators, 1.02 ms per tick in Space Age (300-tick median), fully
       powered.

14. **View geometry (`raycast.View`, 2026-11-03, user request: "they look kind of silly as short
    as they are"):** the monsters were Doom's size against the walls, but looked squat and short
    for two reasons. The eye was half way up the 128-unit wall (Doom's is at 41 units, below an
    imp's 56, so monsters stand above the eye line). And pixels were 0.83 as tall as wide at 16:10
    with the 67° view (proj = H), where Doom's 320×200 on a 4:3 screen shows them 1.2 times taller.
    - **Build parameters:** `--fov` (degrees, default the old 66.85: plane 676), `--aspect` (default
      1.2; 0 = the old proj = H), `--eye` (Doom units, default 40; even, eye × tex a multiple of 32).
      proj = aspect × W × 512 / plane rows per tile: 145 at 160 wide (100 before), so monsters and
      walls are about 1.45 times taller on screen, same width. `--aspect 0 --eye 64` gives a
      byte-identical blueprint to before. Previews of 90°/1.2/40 (Doom's own) and 75° looked
      narrower; the user can pick.
    - **Walls:** the wall covers -OV <= V < 4 tex - OV with OV = 4 tex (1 - e) (mux and bounds are
      constants); the shift's (V + OV) AND 28 takes OV mod 32 as a constant on its green input.
    - **Flats:** the floor's distance is 2e proj / |r|, the ceiling's 2(1 - e) proj / |r|
      (`View.flat_factor` in K_y and the brightness): constants.
    - **Things above the eye:** the 32-row box is unchanged (64 units up to the floor); its row at
      eye level is te = (64 - eye) / 2 = 12. Upper chunks now get thing rows too (as far up as a
      thing's top reaches at NEAR): t = te - 1 + Q with Q truncated toward 0 (te + Q below), the
      offsets as constants on P's input (`2 ^ Each`) and on Vs's network, so the kill and the texel
      always pick the same row.
    - **Cost:** about 70 more combinators at 64×40 (the upper chunks' thing rows); no latency.
    - **Checks:** raycast `--verify` 0 differ at 64×40; the game checks below.

15. **Monsters (`demos/monsters.py`, 2026-11-03, user request: they stood still and hurt on contact
    every 40 ticks, so close enough to punch was close enough to die):** Doom's behaviour,
    simplified. The module docstring has the details; `monsters.step` is the same rules in Python.
    - **Two networks:** positions MX, MY (offsets from the spawn) and the clock MS stay on the bus
      (the raycaster and the player's corners read them). Everything else is on M, a network of its
      own around a pole below the turn deciders (green): the player's offset DX, DY (one arith
      each, absolute: the spawn difference rides on its green input), DX², DY², 8 corner flags, the
      sight line, random numbers, the attack flag, the last DMG (DMG > LD is a hit), the detour,
      the damage queue PD and its phase. The bus's names were running out: the raycaster needs
      about 77 of the 146 virtual signals for its scalars, and reserves every bus signal.
    - **Sight:** per monster, 63 points between it and the player as lanes (two products of the
      bus's positions by constant vectors, plus a constant), a 2D range lookup over the wall
      rectangles (x on red, y on green) whose named else-output counts the clear points (SEE =
      63 is in sight). Two ticks deep, outputs onto M. The compiler's evaluator learned named
      outputs under Each conditions (`decider_each` case: constant × matching lanes).
    - **Waking:** in sight and in front (`FACING`, a half plane; ours face away from the spawn), or
      within a tile and in sight, or shot.
    - **Chase:** the walk clock (Doom's frames) moves the monster 3 (imp) or 2 (zombieman) 1/FP
      tiles a tick toward the player: along the major axis (DX² vs DY²) and the minor one, stopping
      at STOP (176) on both axes. Each step tests its two leading corners (one flag set per step
      axis, BOX 56 ahead and SIDE 46 across: one tick of stale flags and a simultaneous step on the
      other axis, and room to slide along a wall). A wall corner blocking the major axis on one
      side: step sideways off it first. Both corners blocked and the minor axis no help: a detour
      DT along the wall (side by random number, turning back at a dead end) until the way opens
      (Doom keeps a chase direction for a while). No monster-monster collision yet.
    - **Attacks:** after a full walk cycle (after waking, an attack or a flinch), an attempt at each
      of Doom's A_Chase ticks while in sight, with Doom's chance by distance band
      (P_CheckMissileRange), and none beyond 5 tiles (the last band; user request 2026-11-04:
      monsters hit from across the acid map's hall the moment the game started). The attack frames, then at the shot frame, if still in sight: the
      zombieman hits by Doom's spread (64% within a tile down to 10% far off) for 9, the imp
      scratches within a tile (always) or throws a fireball (half hit: no projectiles yet) for 12.
      Random numbers: an LCG on M (the arith reads its constant on red, M on green) times a
      constant per monster.
    - **Pain:** any hit that doesn't kill restarts the pain frames from any live state (Doom's pain
      chance at 100%), then a fresh walk cycle: punching an imp every ~30 ticks keeps it from ever
      attacking (the scripted fight shows it). Pain, death and the corpse moved here from
      combat.py.
    - **Damage:** each hit adds to PD; every other tick (PH, since REG's health on the bus is a
      tick stale; PH toggles only while PD > 0: a free-running toggle came out a tick out of phase
      in the game, whose warm-up runs a tick longer than fsim's) 3 of it is applied: green armor takes 1 while it lasts, health stops at 0.
      Simultaneous hits queue. touch.py's contact damage (COOL) is gone.
    - **Raycaster:** a monster's rx = (MX - X) + (spawn - SPAWN), one op from the port like a
      standing thing's (`things.column_stage` `pos` hook); walk and attack frames go through the
      same MS lookup as pain and death (11 more frames). The player's corner deciders test monster
      boxes on DX, DY (green, M), the box grown by MBOX 25 for their age.
    - **Cost (64×40):** 8,823 combinators (7,652 before), most of it the new sprite frames' lookups;
      about 200 monster deciders. No latency.
    - **Checks:** game `--verify` checks M against fsim every tick, the rules (`monsters.step`)
      against the bus and M (sight lines from `los_count`), that no monster box enters a wall, and
      that the player never touches a live monster where it is now. The scripted fight
      (`FIGHT_SCRIPT`, `--ticks` 2340) is described above it.
    - **Results:**
      - fsim: 0 differ at 64×40, 160×100 and 320×200 (REG, bus, M, the rules, aim, frames); no
        monster box in a wall, the player never touching a live monster.
      - The 64×40 game matches a Space Age game on 13,323,638 checks (2,340 ticks recorded every
        second tick, `harness/run.py --dlc --warmup 60 --every 2`, about 14 minutes and a 5 GB dump).
      - Combinators: 8,823 / 10,003 / 12,197 at 64×40 / 160×100 / 320×200 (7,652 / 8,728 / 10,740
        before). 320×200: 78,155 entities, 1.14 ms per tick in Space Age (1.02 before), fully
        powered; blueprint string 7.3 MB.

16. **Muzzle light (2026-11-03, user request):** Doom's flash states call A_Light1 / A_Light2, which
    raise `extralight` by 1 or 2 light levels until S_LIGHTDONE: the whole view (not the status
    bar) brightens while a weapon flashes.
    - **Levels:** each flash frame carries its level (`WEAPONS[...]["flashes"]`: (frame, tics,
      light), from info.c: pistol 1, shotgun 1 then 2, chaingun 1 / 2, rocket 1, 1, 2, 2, plasma 1,
      BFG 1 then 2); `weapons.light_table()` maps `W * KEYW + A` to it.
    - **Brightness:** `Shading.level(D, k)` = (bright + k D) / (D + fog) with k = `--muzzle-light`
      (default 6) times the level: the dark part of the light curve shrinks by k / 17, never above
      17, so no channel overflows. Doom's own strength is about 4 (1/8 of full per level at the
      half-bright distance), which hardly showed in previews; 8 at level 2 is nearly full bright.
    - **Circuit:** the raycaster's `light` hook (game.py: the weapon key, shared with the picture
      lookup) gives two lookups, LK = k × level and LS = ceil(512 / LK). Walls: LK × D joins the
      brightness numerator (one product; the palette deciders wait a tick on their inputs, four
      delays, using the tick of slack before the spread). Things: Dt / LS (about LK × their D)
      beside SD. Flats: per chunk and level one decider adding that row's brightness gain onto the
      constant brightness vector (role `lb`). No latency.
    - **Cost:** 8,848 / 10,044 / 12,266 combinators at 64×40 / 160×100 / 320×200 (25 / 41 / 69
      more). Without a weapon the raycaster's blueprint is byte-identical.
    - **Checks:** game `--verify` adds frames lit at each level to the samples: 0 differ at 64×40,
      160×100 and 320×200 (latency 17). The 64×40 game matches a Space Age game on 13,352,913
      checks (`--dlc --warmup 60 --every 2`). 320×200: 78,260 entities, 1.13 ms per tick in Space
      Age, fully powered.

17. **Face expressions (`demos/face.py`, 2026-11-03, user request):** Doom's ST_updateFaceWidget.
    The status bar face glances left and right at random, turns toward a monster that hurts you (or
    grits its teeth if it's in front), and grits its teeth after 2 s of firing. The module
    docstring has the rules; `face.step` is the same in plain Python.
    - **Priorities (Doom's):** dead (hud.py, by health), hurt (damagecount > 0: turn toward the
      attacker, left or right outside 45 degrees, else the rampage face; shown 1 s after the
      damage ends), rampage (Doom's lastattackdown: fire held 70 tics, i.e. A in 1 .. LOWER - 1),
      idle (a random straight face, half a second each). Tics become ticks (`weapons.ticks`).
    - **Left out:** the evil grin (weapon pickups, none yet), god mode, and the ouch face: Doom
      tests `health - oldhealth > 20`, true only when you *gain* 20 health while hurt (the famous
      bug); the intended "lost more than 20 at once" can't happen with our hits (9 and 12).
    - **State:** FE (bus, `signal-skull`): the expression, 0..2 straight, 3 right, 4 left, 5 hurt
      head-on, 6 rampage (5 and 6 show the same face; 3..5 have the hurt priority). On M: FC (the
      face count), RC (lastattackdown), DC (damagecount in 1/12 tics, +12 per health point lost,
      -7 a tick, at most 100 tics; for the red flash later), ATK (the attacker: a shot writes 16 << k,
      the next tick ranges make it k + 1). DC's increments mirror monsters.py's health deciders
      (`Monsters.damage_bits`), ATK's the shot clauses (`Monsters.shot_clauses`).
    - **Bearing (raycaster):** FB_k (bus, four signals): monster k right of the view (1), left (2)
      or within 45 degrees (0), from the aim stage's alpha and L (`face.bearing_stage`: L > 0 and
      alpha <= L, or L < 0 and alpha <= -L from a negated copy), on the bus like the aim. So the
      direction is from the camera a few ticks back; who hit you is current.
    - **Status bar:** 31 faces (5 pain levels × 6 pictures, plus dead), each a (health range, FE)
      condition on the same per-chunk delta deciders as before (`hud.face_ranges`).
    - **Game harness lessons:** the idle count first ran from power-on, so its phase depended on
      the warm-up (a tick longer in the game: 6,942 checks differed); it now starts once RND runs
      (RND != 0, from its constant). And "face count" counted down while FC > 0, so at 1 its
      output summed to 0 (the 0-signal rule below: 30 checks); now while > 1.
    - **Cost:** 8,908 / 10,154 / 12,451 combinators at 64×40 / 160×100 / 320×200 (60 / 110 / 185
      more: the logic, and 25 more face pictures in the bar). No latency.
    - **Checks:** game `--verify` checks `face.step` against the bus and M every tick, the face's
      deciders against `face.step` on a grid of 54,432 states (the script never gets hit from the
      left), the bearings against floats, and samples frames showing each expression. The script now
      ends with fire held 2.7 s after the restart (rampage), `--ticks` 2480. raycast `--verify`
      cycles the expression and checks the bar against `Hud.image` on every checked frame (26 of
      the 31 faces at 64×40 over 460 ticks, 0 pixels differ).
    - **Results:** game `--verify` 0 differ at 64×40, 160×100 and 320×200 (expressions seen: all
      but turned left, which the grid covers); raycast `--verify` 0 differ at 64×40 and 320×200 (bar
      0 pixels on 109 frames, 26 faces). The 64×40 game matches a Space Age game on 14,225,583
      checks (2,480 ticks, `--dlc --warmup 60 --every 2`). 320×200: 78,442 entities, 1.17 ms per
      tick in Space Age, fully powered; blueprint string 8.2 MB.

18. **Weapon keys and reset (2026-11-03, user request):** number buttons pick a weapon (Doom's
    slots), and a reset button starts a new game at any time.
    - **Pad (`input_pad.py`):** the next-weapon button is gone. Below the ring, like a numpad: 1 2 3 /
      4 5 6 / 7 U E (`SLOTS`, `ROW_BUTTONS`; lanes `signal-1`..`7`), and reset (`signal-R`, iron
      chests) right of the ring with a button's gap (`RESET_GAP`). `pad_height(n)` sizes the pad
      for game.py.
    - **Selection:** per slot a decider reads PRESSED and puts SEL (`signal-N`, the weapon's index
      + 1; slot k is `weapons.slot_index(k)`) on the bus for a tick; the highest slot wins if two
      are pressed at once. Doom's pendingweapon replaces the switch queue: P (`signal-Q`) is set by
      a selection of another weapon (one decider per weapon, since a decider can't copy SEL onto
      P), held until the bottom, where W becomes P - 1 (one decider per weapon but the pistol).
      A later selection replaces P; selecting the current weapon changes nothing.
    - **Reset:** a decider reading PRESSED (fixed below the dead flag) outputs RESTART onto the
      restart decider's two networks (the bus and REG's green input). The REG writers (ammo spent,
      pickups, damage) are now gated by RESTART = 0 too: reading the bus on the restart tick, the
      old game's state would land in the new one. Movement after a reset uses corners from the old
      position for lat + 2 ticks: at most 3 steps of 10 from the spawn, at a tile's centre, so the
      box (48 + 30 < 128) stays inside the spawn tile.
    - **Keyboard mod 0.3.0:** keys 1-7, use (F) and reset (Ctrl+Shift+R) are one-tick presses; the
      next-weapon link is gone. The number keys still select quickbar slots.
    - **Script:** every switch is a selection now (an override while lowering, a selection while
      rising, the current weapon again), and the fight ends with a reset mid-walk and mid-switch,
      then fire (`--ticks` 2560). New entries go at the end of the list: drop timing is drawn in
      order, and an entry in the middle changed the walk (the player didn't die).
    - **Cost:** 24 more combinators: 8,932 / 10,178 / 12,475 at 64×40 / 160×100 / 320×200. No
      latency.
    - **Checks:** input_pad `--verify` 0 differ at n = 3, 5 and 9; game `--verify` 0 differ at all
      three sizes, restarts at 2218 (Use, dead) and 2464 (reset, alive), and `--keys` at 64×40;
      the mod loads in a headless game and writes one-tick presses.
      The 64×40 game matches a Space Age game on 14,723,814 checks (2,560 ticks, `--dlc --warmup 60
      --every 2`). 320×200: 79,060 entities, 1.07 ms per tick in Space Age, fully powered.

19. **Music (`demos/music.py`, 2026-11-04, user request):** a song converted by
    [MIDItorio](https://miditorio.com) (version 2.2.1 tested) loops on programmable speakers and
    starts over on a new game. `game.py --music FILE` takes the blueprint string from a file of the
    player's; it is optional, and without it the blueprint is unchanged.
    - **What MIDItorio emits:** the song in two constant combinators (per lane, at six qualities: a
      time, and up to two events of an 8-bit instrument address and a 6-bit note), a clock (a red
      self-loop plus 1 from a constant, its on switch: it counts forever, so the song plays once),
      a decider passing the lanes whose time is now (`Each(red) = signal-green`), four arithmetic
      combinators unpacking the events, and per channel a decider (`Each(red) = address` -> the
      speaker signal, copying the matching lane's note from green: a named output under an Each
      condition, `decider_each`) driving a speaker (pitch from the value, global playback). The
      user's first song: 1,544 lanes, 19 speakers, 96 s, 15×6 tiles.
    - **The clock, rewired (`Song.place`):** a counter on a green loop (its own count, plus 1 from
      the constant) while the count is at most the song's length (the last event's time: it then
      drops to 0, so the song loops), `RESTART = 0` on its red input (the state bus: a new game,
      either way, starts it over) and `signal-check` > 0 from the constant (the on switch: off stops
      the music, on starts it from the top). Everything else is the song's own. Nothing on the bus
      changes; the clock only reads it.
    - **Placement (`game.place_music`):** the free spot nearest the dead flag (a bus reader), right
      of the screen, clear by a tile; its clock's red input is relayed to the dead flag's.
    - **Content:** songs stay out of the repository (`music/` is gitignored). The user's first song
      is id Software's E1M1 ("At Doom's Gate"), which the project's freely-licensed rule keeps out.
    - **Checks:** `music.py FILE --verify` builds the song with a stand-in bus (a scripted constant:
      two restarts, the switch off and on) and checks the clock and every speaker decider, every
      tick, against the song's events (`Song.expected`, decoding the lanes through the song's own
      arithmetic): 0 differ over 11,920 ticks (4,374 notes). With `--loop 300` (3 loops) it matches
      a Space Age game on 42,525 checks (`harness/run.py --dlc --warmup 60`). game `--verify` checks
      the clock against RESTART on the bus: 0 differ at 64×40 (started over at 2219 and 2465).
      320×200 with the song: 12,500 combinators (+25), 79,108 entities, 1.07 ms per tick in Space
      Age, fully powered.
    - **Not checked:** the sound itself (the harness records combinators, not speakers).

20. **Maps and floors per tile (`demos/maps.py`, 2026-11-04, user request: the launch video's
    showcase, see "Next steps"):**
    - **Maps:** `--map` on raycast.py and game.py picks one: `arena` (the default; its monsters
      moved 5-9 tiles from the spawn on 2026-11-04, user request), `test` (the arena with the
      monsters where game.py's scripted walk and fight were tuned: verify game.py with `--map
      test` for the full fight, death and restarts) or `acid` (a start room, a wooden
      tunnel north into a hall over acid with a brown walkway zigzagging across it to an exit
      corridor; our layout, Freedoom's textures). A map holds its cells (wall characters to wall
      textures, open characters to a floor and a ceiling flat), things, monster facings, spawn,
      spawn angle and the scripted camera's center. `maps.select` makes one current in every
      module (patching raycast also when it runs as `__main__`). Maps are 16 x 16.
    - **Floors per tile:** with more than one flat on the map, the flat position keeps its tile:
      `(RAW >> 4) * K_y + ((P << 20) - 2^31)` is the world position times 2^28, offset by -8 tiles
      so it is signed and increasing over the map, and its top 4 bits are the tile. Per chunk:
      `FX = that << 4` (the old position within the tile, times 2^32), `FY = that AND (2^28 - 1)`,
      and per flat class but the first a decider over the class's tile rectangles (`flat_rects`; x
      ranges on red, y on green: `tile_range`) outputting `class << 28` onto FY's wire. The word
      deciders then test one range on FY: class, then the flat row. Each class has its own 16
      colors: its palette deciders test the class on green (`1 + class << 28`, the ones vector plus
      the class sum delayed). One tick more on the flat path, which starts a tick earlier: no
      latency. Up to 8 flats per part.
    - **The acid map's monsters (2026-11-04, user request: they saw the player from the start and
      hurt from across the hall):** out of sight of the start room and the tunnel: either side of
      the tunnel's mouth facing the walkway (an ambush), the far walkway (it sees the mouth from 7
      tiles, beyond the new 5-tile attack range) and around the exit corridor's corner.
    - **The acid map's flats:** FLOOR4_8 (grey hex: start room, tunnel), FLOOR5_2 (the brown
      walkway), NUKAGE1 (acid; not animated, no damage: user decision), one ceiling (CEIL5_1) for
      now: the tunnel's low ceiling comes with ceiling heights. Walls STARTAN3, WOOD1, TEKGREN2,
      BROWNGRN (exported into `assets/freedoom/textures.json` with the flats).
    - **Cost (acid vs arena):** 9,243 / 10,909 / 13,941 combinators at 64×40 / 160×100 / 320×200
      (+311 / +731 / +1,466; a second ceiling flat added about 730 more at 320×200). 320×200: 80,760
      entities, 1.14 ms per tick in Space Age (1.07 for the arena), fully powered.
    - **Checks:** the arena's blueprints are byte-identical (raycast 64×40, game 64×40, raycast
      160×100 with `--flat 0`). On the acid map: raycast `--verify` 0 differ at 64×40, 160×100
      and 320×200 (the float reference differs on 8.5 / 9.2 / 11.1% of view pixels, the arena's
      7-9%: flat texels and tiles at boundaries round either way); game `--verify` 0 differ at
      64×40 and 160×100 (the arena's script on the acid map: every monster wakes, chases and kills
      the player, both restarts).
    - **After the monster and corpse fixes (2026-11-04):** game `--verify` 0 differ on both maps at
      64×40 and 160×100 (raycaster latency 18; on the acid map the walk still wakes all four and
      dies, 19 attacks). Combinators: arena 8,976 / 10,222, acid 9,287 / 10,953 at 64×40 /
      160×100; acid 320×200 with the music 14,010, 1.20 ms per tick in Space Age, fully powered.
      A staged scene (an imp behind a zombieman's corpse) shows the imp whole. Each map now writes
      its frames to its own folder (`maps.suffix`): two maps verified at once shared one, and the
      arena's frames came out wrong.

Still to come (M4): doors; see "Next steps".

Earlier notes that still hold:

3. **Row or pixel stage.** Two layouts:
   - One network per row, with W lanes.
   - A whole-frame vector with one lane per pixel, filled by the broadcast trick. Same total
     work, far fewer combinators.

   At 160×100 and above, use per-row networks. The whole-frame vector only fits up to 2,878
   pixels.

   Texturing, about 10 lane-ops per pixel and no per-pixel lookup tables (built in M3 as
   above, with a mux per row instead of one shift, and palette deciders instead of a multiply):
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
   computed but never printed), including red-to-green wires. `FSIM_SHOW=N fsim conform` shows
   more than 12 mismatches. Space Age (2026-11-02): selector quality transfer (static, to Each)
   and quality filter, pole wire reach by entity quality (`pole_quality.json`), and the Space Age
   signal order for blueprints using its qualities; `conformance/cases_dlc` (`quality_ops`,
   `substation_reach`) with `conformance/game_dump_dlc.json` is a cargo test.

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
     - `--every N` (2026-11-03): record only every N-th tick (the rest are empty rows, which
       `fsim conform` skips). The 64×40 game over 2,340 ticks with monsters (about 11 GB of rows,
       held in Lua and serialized at once) got Factorio killed after 24 minutes; `--every 2` fits.
     - `--discover`: export a blueprint with every option set, to learn the JSON field names.
   - Scripted input: a case's `fsim.inserts` (`[k, entity_number, item, count]`, negative
     removes) puts items into a chest in on_tick at t0 + k, after row k is recorded. fsim applies
     the same list before simulation tick k (`fsim run --input FILE` takes one too). A chest's
     contents are on its wires like a constant combinator's signals, from the next tick
     (`chest_read` case). On a constant combinator an entry `[k, entity, signal, value]` sets
     that signal in its first section instead (0 clears it; names starting `signal-` are virtual):
     the keyboard mod's path, on the wire from the next tick (`constant_script` case).
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
     row of its consumers, leaving the column between steps for constants and relay poles.
     Regular blocks (the raycaster's row pipelines, its strip lookups) are **fixed nodes** the
     caller positions (`n.fixed, n.pos`); the placer only fills in the rest, left of
     `x_right`;
   - wires each network as a minimum spanning tree (Kruskal over grid neighbors for big nets),
     bridging long edges with medium poles: greedy toward the target, A* around obstacles when
     that gets stuck. A long edge whose end is boxed in (no free tile within reach, e.g. deep in
     the packed thing rows) can't take a pole, so such edges are avoided and the tree is rebuilt
     (`_mst(fps, boxed)`); the 320×200 thing kill column needed it;
   - evaluates the graph without timing (`Graph.evaluate`), using the same specs it emits. That
     evaluation is the reference: the simulated blueprint must match it frame for frame. It
     shares input sums between nodes reading the same sources and indexes key lookups, so a
     160×100 textured frame takes about 0.4 s.
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

### Raycaster (`demos/raycast.py`): M1 2026-11-01, M3 textures 2026-11-01

Freedoom-textured walls (M1: flat-shaded), textured floor and ceiling (`--flat 0`: a gradient),
16×16 original map, the camera circling and turning on a script. Built with the netlist compiler at any `--size`.
- **Map and textures:** wall characters pick textures (`TEXTURE_OF`): `#` STARTAN3, `B` BRICK7,
  `M` MARBLE1, `R` SP_HOT1, from `assets/freedoom/textures.json` (`tools/wad.py
  --export-textures`; it also has STONE2 and WOOD1). A tile is 128×128 texture units,
  box-filtered to `--tex` (default 64) texels each way. Flats (`FLAT_OF`): FLOOR4_8 floor,
  TLITE6_4 ceiling (a light panel per tile; CEIL3_5 was too dark to read), `--flat` (default 16)
  texels per tile.
- **Size:** about 8,100 / 13,000 / 21,200 combinators at 64×40 / 160×100 / 320×200 (flat 0:
  5,847 / 7,473 / 10,175; M1: 464 / 530 / 632), plus lamps and about 900–1,000 relay poles.
  Of those, 4,096 are strip lookups (4 textures at tex 64), 27 per row for walls and about 55
  per row for flats (32 word deciders, 16 palette deciders). A new frame every tick.
- **Latency:** 18 ticks from the camera port with the game's camera, 17 without the weapon's
  compositor (`--no-weapon`; M1 16; 23 before the setup
  rework, 42 before the parallel march); the scripted camera adds its own path.
- **Checks:**
  - `--verify` compares sampled fsim frames (`--every`) with the netlist evaluation (exact; 0
    differ at 64×40 and 160×100), and samples the float raycaster (`ideal()`, textured: about
    7–8% of pixels differ, from texel and brightness rounding at boundaries).
  - The M1 64×40 version matched the game on 451,792 checks; M3 is checked through the game
    build (below). Deep pipelines need the harness run with `--warmup 60`, so the warm-up state
    settles before t0 in both the game and fsim.
- **Known limitation:** u is the position along the wall's axis, so textures are mirrored on
  faces seen from the other side (side 0 rays going −, side 1 rays going +). Fixing it costs a
  tick or doubles the strip block; text-free textures hide it.

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
  - Below the ring, like a numpad: the weapon slots 1 2 3 / 4 5 6 / 7, then use and enter, with
    glyphs in wood, a display panel icon and an indicator lamp (item 18; use, next weapon and
    enter before). No strafe for now.
  - Reset (item 18): right of the ring with a button's gap, in iron chests with an R, so a stray
    drop can't hit it. Its lamp sits level with the ring's top: a lamp above the screen's top row
    moved the screen's bounds in fsim's frames (every frame differed).
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

### Keyboard mod (`mod/doom-input`, optional, 2026-11-02, user request)

The blueprint stays vanilla; the mod is an optional input path ("the input is so dang clunky").
- **Blueprint side:** two empty constant combinators in the pad's decoder, described
  `doom-input:held` (on HELD) and `doom-input:pressed` (on PRESSED). Every reader tests `> 0`,
  so they add onto the pad's outputs; without the mod nothing changes.
- **What the game tells a mod (probed in the user's game, editor/god mode):** custom inputs fire
  on press only (no release event; holding fires once). `walking_state` gives the held movement
  keys every tick (8 directions; it follows the press one tick later and clears the tick after a
  release); `shooting_state` gives Space held. In the editor WASD moves the player (the camera),
  so the mod teleports it back. The game moves the player after on_tick, so a teleport there
  left each held tick's move on screen (the user saw bad jitter); the teleport now runs in
  on_player_changed_position, right after the move and before the frame is drawn (on_tick still
  catches moves within a tile). The user confirmed it works well. Tab opens the map.
- **Mapping:** walking direction -> signal-W/S/A/D (diagonals give the combos), shooting ->
  signal-F, all 1 while held; its own inputs 1-7 -> signal-1..7 (weapon slots), use (F) ->
  signal-U and reset (Ctrl+Shift+R) -> signal-R, one tick each (since 0.3.0, item 18; before,
  the game's next-weapon control -> signal-N). The number keys keep selecting quickbar slots
  (consuming "none": consuming them would take them while the mod is off too). Linked press events fill the tick before `walking_state` follows. The arrow keys work
  once bound as alternative movement keys in Settings > Controls.
- **UI:** a toggleable shortcut-bar button (a joystick icon, `tools/mod_icon.py`) and
  Ctrl+Shift+J. Toggling on scans for the tagged combinators and anchors the camera.
- **Latency:** a press is on the HELD network the tick after the key event, against about 3
  ticks for a pad drop, and release ends it at once instead of the pad's 14-tick hold.
- **Checks:** `game.py --keys` scripts the walk as keyboard writes; `--verify` checks it like the
  pad (the pad reference adds the keyboard's contents). The mod itself was driven in a headless
  game through its test interface (`remote.call("doom-input", "hold" | "press" | "scan")`): writes
  reach the wire the next tick, a press lasts one tick.

### M2: movement (`demos/game.py`, 2026-11-01)

The raycaster driven by the pad: walk, turn, slide along walls. Screen, then the pad on its
right, then the player logic below the pad.
- **State:** one register REG (green self-loop; a decider `RESTART = 0 -> Everything` since
  the restart, item 13, before that `Each + 0`) holds X, Y (displacement from
  SPAWN) and R (unbounded turn, in quarter view angles: the view angle is ((R >> 2) + A0) & 255).
  - **Status bar values (2026-11-02):** REG also holds HEALTH (`signal-H`), ARMOR (`signal-V`)
    and the ammo counts (bullets `signal-B`, shells `J`, rockets `K`, cells `C`) as offsets from
    `hud.START` (every weapon, ammo below Doom's caps), which `hud_from_port` adds on the wire
    (`Each + Each` with a constant).
  - **Contact state (2026-11-02):** the items' GONE flags (and until item 15 the pain clock COOL) on the bus
    (architecture item 10); their deciders join the weapon columns (bus writers, then the REG
    writers in columns of `SPEND_ROWS`, snaking so the chain stays short).
  - **Weapon state (2026-11-02):** W (`signal-G`), A (`signal-T`) and the pending weapon P
    (`signal-Q`) on the state bus, plus the relayed FIRE and SEL (`signal-N`, the selected
    weapon's index + 1); see architecture items 9 and 18. The weapon deciders sit two
    columns right of the turn deciders (`wm_pos`): bus writers chained to REG's red output,
    spend deciders chained to its red input.
  - **Finer turning (user request, 2026-11-01):** 2 view angles per tick (a turn in 2.1 s,
    about 40° per tap) felt stiff. R counts 1,024 per revolution (`ANGLE_SUB`) and turns
    `--turn` (default 3) per tick: 3/8 of the old rate, a turn in 5.7 s. The lookups key on
    `R & 1023` with the `>> 2` folded into the tables (camera `angle_sub`), so no extra tick;
    `lookup_scalar` merges runs of consecutive keys into ranges, so the 4× keys cost few
    extra conditions.
  - **Turn ramp (user request, 2026-11-02):** 3 a tick was still too coarse to aim (a tap, held
    14 ticks by the pad, turned 15°). Now `DEFAULT_TURN = (1, 4, 18)`: 1 a tick for the first 18
    ticks a turn button is held (a tap turns about 5°), then 4 (a turn in 4.3 s); flags
    `--turn-slow`, `--turn`, `--turn-ramp`. A turn counter TURNC (`signal-L`, `turn_counter`)
    right below the turn deciders: a decider reading HELD on red, copying its own output on a
    green loop plus 1 while left or right is held. The four turn deciders read HELD on red and
    the counter on green (the bus is red-only: REG's green output is its self-loop, and HELD's
    green output is the pad's internal feedback), so no extra tick.
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
- **Camera path:** REG reaches the raycaster's camera port over the substation row through the
  top of the screen (item 12; a big-pole line above the screen before 2026-11-02).
  `raycast.build_graph(w, h, camera)` takes the camera from the caller; M1's script is
  `scripted_camera`.
- **Checks (with M3 textures):**
  - `--verify`: REG matches the reference on every tick, the player box never touches a wall or a monster's box,
    and sampled frames match the raycaster evaluation, at 64×40, 160×100 and 320×200.
  - The 64×40 build (normal substations) matches the game on 17,255,163 checks with shooting
    (1,300 ticks: the zombieman shotgunned, the imp chaingunned with two pain flinches and punched
    to death, both corpses; about 14 minutes and a 4 GB dump; 2026-11-02). Before that it matched
    on 8,569,024 checks with thing contact
    (two monster hits, armor, ammo and medikit pickups, the turn ramp; 2026-11-02) and the weapons
    (every weapon switched to, raise/lower, queued taps, most fired, `harness/run.py --warmup 60`, scripted inserts, 700
    ticks; about 7 minutes and a 2 GB dump; almost all of it is the mod recording every entity,
    not the game). Before thing contact it matched on 8,545,891. With the status bar alone it matched on 4,627,941
    (400 ticks). Before the HUD it matched on 4,706,537 with things. With flats only it matched on 4,263,031, without flats on 3,367,999, and M2 on 1,209,416.
  - With shooting, 320×200 imports in Space Age fully powered (95,460 entities, 26,819
    combinators) at 1.97 ms per tick; 160×100 is 16,764 combinators.
  - With thing contact, 320×200 imported in Space Age fully powered (94,343 entities, 25,780
    combinators) at 2.53 and 3.23 ms per tick in two runs, noisy (the machine had just run heavy
    jobs; contact adds about 40 one-lane deciders). With the weapons alone 2.09 ms (94,278
    entities, 25,747 combinators; network update 1.03 ms of it). With the status bar alone 1.72 ms
    (22,783 combinators, 24,414 before: the bar replaces 32 view rows).
  - 160×100 imports in Space Age, fully powered, at 0.87 ms per tick with things (0.78 without;
    without flats 0.59; M2 0.40). 320×200: 1.75 ms with things (1.40 without, 1.16 without
    flats, M2 0.88). Blueprint strings 1.0 and 2.6 MB.
- **Input-to-screen latency:** about 23 ticks (0.38 s) for both turning and walking: pad 3,
  gate or turn decider into REG 2, raycaster 18 with the lamps (17 before live monsters drew in
  front of corpses, item 7; 18 before the angle wrap, item 12). Firing: pad 3, relay 1, the animation's start 1, then the raycaster.
  - **History:** about 56 ticks at first (the user felt "almost a full second"), then 27 for
    turning and 35 for walking, then 21 (M2, flat walls), then this.
  - **The raycaster's 18:** camera lookups 1, ray products 1, ray shift / sign selectors 1, key
    product 1, key division 1, hit lookup 1, min trees 4, the things' tick (item 7) 1, side split 1, strip lookups 1, quality
    spread (and V's spread) 1, V 1, mux 1, shift 1, palette 1, lamps 1.
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
    - With M3 the march also feeds the key tree: its leaves (the per-step key) would come from
      the ROM the same way.
  - **Bounded angle:** done (item 12, `angle_wrap`).

## Status (2026-11-01)

Done:
- The design above, and checks of the 2.1 features it depends on.
- fsim, the harness, the conformance suite (17 cases, including chests, range lookups,
  which lanes an Each decider visits and constant-first powers) and the gradient demo.
  All match the real game exactly.
- The input pad (above): decided with the user, matches the game.
- M2 movement (above): matches the game.
- Measured semantics: see the README table, and `sim.rs` / `conform.rs` doc comments for the
  timing model.
- Screen size decided (160×100, aiming for 320×200), and M0 UPS measurements: 320×200 fits.
- Freedoom sprite assets and the sprite demo (above): the texture path works end to end in fsim
  at three sizes and in the game.
- The netlist compiler and the raycaster (above): M1 flat walls, then M3 Freedoom textures (walls, floors, ceilings),
  driven by the pad in the game build.
- M4 step 1, things (architecture item 7): static Freedoom monsters and items on the map,
  occluded by walls, in the raycaster and the game at no extra latency.
- M4 HUD (architecture item 8): the Freedoom status bar with live ammo, health, armor and face
  (expressions: item 17).
- M4 weapons (architecture item 9): seven weapons in front of the view, switching, Doom's firing
  animations and ammo use.
- M4 thing contact (architecture item 10): pickups, monster pain, solid monsters; the turn ramp.
- M4 shooting (architecture item 11): hitscan, monster health, pain and death animations, corpses.
- Death and restart (architecture item 13): frozen at health 0, Use starts a new game.
- View geometry (architecture item 14): Doom's pixel aspect and eye height, so monsters stand tall.
- Monsters (architecture item 15): wake, chase around walls, attack on Doom's timing, flinch.
- Muzzle light (architecture item 16): the view brightens while a weapon flashes.
- Face expressions (architecture item 17): glances, turning toward an attacker, rampage.
- Weapon keys and reset (architecture item 18): number buttons and keys pick weapons; reset any time.
- Music (architecture item 19): a MIDItorio song on a loop, starting over on a new game.
- Maps and floors per tile (architecture item 20): the acid showcase map, step 1.
- Chunks, layout and latency (architecture item 12): six rows per network on signal qualities
  (Space Age), about half the combinators, layout by concept, the bus on a substation row, 17-tick
  raycaster.

Commit only when the user asks.

## Next steps (in order)

0. **The launch video's showcase (user request, 2026-11-04):** the feel of a classic first level's
   acid room, not a copy of it: a low tunnel opening into a tall hall, a brown walkway zigzagging
   over acid, shotgun guys. Scope the user set: the floor stays at one height (the acid is a
   different flat at the same level, no damage), no up and down, no secret area.
   1. Floors per tile and the map: done (architecture item 20).
   2. Two ceiling heights, the eye fixed: tall walls in the hall, the low tunnel (and start room).
      Design: with a flat floor and a fixed eye only entering a low region matters, and a ray
      enters a rectangle once. So per low region (a rectangle) a min tree gives the column's entry
      and exit distances E, X (lookups on the march's crossings like the wall hits); per pixel in
      the upper half a few comparisons with the row's constants decide: the hall's ceiling, the
      wall above the opening (at E, its own texture lookups), the tunnel's ceiling (its own flat
      distance) or the walls behind, cut by the tunnel's ceiling edge. Tall walls repeat their
      texture (more mux ranges). Estimate 2-3k combinators at 320×200, 1-2 ticks; prototype in
      raycast.py first.
   3. Shotgun guys: Freedoom's shotgun zombie (SPOS) frames, the zombieman's logic with 3 pellets,
      about 30 health, a shotgun drop. Maybe 6 monsters and 2 items (8 things).
   4. Tune the map and a camera path for the video.
1. **M4, the rest:** things are drawn (item 7), can be shot (item 11) and move and attack (item
   15); next are doors.
   - Monster follow-ups: imp fireballs as projectiles (things that move; two more things need a
     fourth tree level, a tick, or fewer items), monsters blocking each other, waking on the
     player's shots (Doom's noise), Doom's pain chance and damage rolls (the LCG is there), sprite
     rotations (they always face the camera), more monster types.
   - Shooting follow-ups: Doom's randomness (damage rolls, pain chance), shotgun spread, rocket
     splash, bullet puffs, more than 8 things. M5 (stretch): angled walls, varying heights.
   - HUD follow-ups: keys, owned weapons in the ARMS panel once there are pickups (more slots of
     the same kind); the face's evil grin on a weapon pickup.
   - Weapon follow-ups:
     pickups (weapons owned, ammo); the walking bob (it moves sideways too: about 10× the
     pictures, or a different way to draw the gun); the chainsaw and super shotgun.
   - Contact follow-ups: Doom's blue armor class (absorbs a half; one class now). Death follow-ups (item 13): the view dropping to the floor (Doom lowers the
     eye), the music restarting on a restart (M6).
   - Screen flashes (Doom's ST_doPaletteStuff, asked about 2026-11-03): a hit adds the damage to
     `damagecount` (at most 100, minus 1 a tic) and the screen takes red palette (count + 7) >> 3
     (1..8); a pickup adds 6 to `bonuscount` (gold palettes 1..4). The whole screen tints,
     status bar included: color × (1 - f) + tint × f. damagecount is on M already (DC, item 17). Walls, flats and things could scale through
     the brightness (as the muzzle light) plus a red broadcast per chunk; the weapon's and the
     bar's colors are exact constants, so they need another way.
   - Thing follow-ups: more than 8 things (a fourth tree level costs a tick, or sort by depth on
     scalars); a second nearest layer so overlapping things don't cut each other (live monsters
     already win over corpses and items, item 7; two monsters still cut each other).
2. **M6: audio (planned 2026-11-02, user request).** Music on a loop and sound effects, with
   vanilla programmable speakers driven by the circuit.
   - **Music:** done (architecture item 19), for any MIDItorio song the player supplies. A song
     for the repository would have to be freely licensed: Freedoom's own soundtrack (BSD) is in
     its WAD as MUS lumps, which convert to MIDI, then MIDItorio.
   - **Sound effects:** short note patterns (the drum kit and similar instruments), each triggered
     by an event on the bus:
     - a shot: the spend deciders' A values;
     - a pickup: GONE rising;
     - pain: PD rising (monsters.py's damage queue);
     - monster pain and death: MS starting;
     - the weapon switch, and the player dying.

     Freedoom's sound effects are samples, which speakers can't play, so these are our own MIDI
     patterns or speaker notes.
   - **To measure first** (conformance cases, as for everything else):
     - what the speaker does with a signal held for several ticks (one note or a repeat);
     - polyphony and the global-playback option, so the player hears it while zoomed in on the
       screen;
     - whether the instrument can come from the circuit, or needs one speaker per instrument;
     - UPS for a few hundred speakers.
3. **Possible M3 follow-ups:** fix the mirrored faces; fewer strip deciders (4,096 of the
   ~13,000 combinators at 160×100); different flats per room (done: item 20); a lower-cost flat path (the 32 word deciders per row are
   most of it).
4. **Tooling as needed:** fsim speed for 320×200 work (about 70 ms per tick for the textured
   game without flats; lookups are about 100× slower than the game).

## Rules for working here

- **Never rely on unverified game behavior.** Add a conformance case to `gen.py`, run it in the
  game, make fsim match, and refresh the golden dump. Several obvious guesses turned out wrong:
  the min tie order, lanes when red + green sums to 0, wire reach being measured footprint to
  footprint, and lamp color lagging its condition by one tick less.
- **A decider whose outputs on one signal sum to 0** (copy X plus a constant -X) shows that signal
  at 0 in the game's `signals_last_tick`; fsim leaves it out (seen on the pain clock, 2026-11-02:
  2 of 8.5M checks). Networks behaved the same, but keep such sums from reaching 0 (count down
  while > 1, not > 0) rather than rely on it.
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
