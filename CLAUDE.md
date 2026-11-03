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
   32 rows from eye level to the floor, so only the lower half of the screen has thing rows. The
   module docstring has the formulas.
   - **Per thing (scalars):** depth `alpha = rx DX + ry DY` and lateral `L = ry DX - rx DY` (DX,
     DY, -DY lookups on green, shared by every thing's products; on one wire with rx the compiler
     copied the table per thing). Per column `U = camx * (alpha / 1551) - L + C` is the box column
     times 2^12.
   - **Nearest per column:** two min trees over the things (absent lanes lose: `(red < green AND
     red != 0) OR green = 0`). `Zt = (alpha >> 10) << KB + U` gives the box column ordered by
     depth; `Dt = alpha * 64 + frame` gives the precise depth and the frame. They can disagree
     only when two things are within 1/256 tile of each other. One thing per column: a farther
     thing is hidden by the nearer one's box columns, even where it is transparent.
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
   `assets/freedoom/weapons.json`), "next weapon" cycles them, and fire plays Doom's animation
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
     128.. lowering, 155 down, 192.. raising) and D (switches queued) live on the state bus, REG's
     red output network, held by deciders that read the bus and write back onto it
     (`weapons.logic`, 10 deciders): queue hold/add/take, weapon hold/next, anim advance (copy A
     + 1), start (A = 1), lower, down (hold), raise. Fire held from ready starts the cycle if
     there's ammo; at A = R (Doom's A_ReFire) it restarts while fire is held, else runs on to L
     and back to 0. Per weapon a spend decider at its shots' A values (with an ammo check each,
     so the chaingun's second shot) writes `ammo -cost` into REG's input. Relays (`F + 0`,
     `N + 0`) put fire held and next pressed onto the bus, so every weapon decider reads one
     network. The raycaster's picture F is a lookup on `W * 256 + A`, its bits sized from the table: the
     88 pictures need 7, and with 6 (sized for the first 32) the BFG and fist drew the pistol's
     and shotgun's pictures, which the checks missed (they evaluate the same lookup). The user
     found it in game; `lookup_scalar` now refuses values that don't fit its bits.
   - **Switching:** a "next" press adds 1 to D. While D > 0 a ready or firing gun starts lowering
     (cancelling a shot); down, each tick with D > 0 steps W and takes 1 from D, then the gun
     rises (and lowers again at once if presses came while it rose). So several quick taps cost
     one lower and one raise, about 0.9 s. Fire is ignored while switching. Taps closer than
     the pad's hold (14 ticks) are one press to the pad.
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
   - **Pain:** a bus clock COOL (`signal-P`): a monster within REACH (1 tile per axis), COOL = 0
     and alive -> COOL = COOLDOWN (40); it counts down (copy + -1, while > 1: see the rules below). The tick after it starts
     (COOL = COOLDOWN) tables over (armor, health) take DAMAGE 9: green armor takes SAVE 3 while
     it has that much, and health stops at 0 (the face shows dead; nothing else happens yet).
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
   - **Dead monsters** stop hurting (touch's pain start) and stop being solid (the corner deciders
     test DMG < health; the reference feeds them the bus, not just REG).
   - **Fist reach is a tile:** Doom's is 84 units to the centre, but our monster boxes carry the
     latency allowance and stop the player 0.53–0.69 tile away per axis.
   - **Cost:** about 25 aim combinators in the raycaster, 37 bus deciders for 4 monsters, ~250
     more thing lookups (14 more frames). The bus writers now snake in columns after the spend
     deciders; their chain joins the bus through one writer's own input (it reads the bus).
   - **Checks:** `combat.step` (plain Python) against the bus every tick; the aim against a float
     hitscan (`combat.ideal_aim`: DDA along the middle column's ray, as the circuit) from the bus τ earlier, skipping near-ties (corner grazes,
     within 0.02 tile). The scripted fight (`game.COMBAT_SCRIPT`, ticks 700–1340): shotgun the
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

Still to come (M4): enemies, doors; see "Next steps".

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
  catches moves within a tile). The user confirmed it works well. Tab opens the map; next-weapon (C for the user) arrives as a linked control.
- **Mapping:** walking direction -> signal-W/S/A/D (diagonals give the combos), shooting ->
  signal-F, all 1 while held; next-weapon -> signal-N and its own "use" input (F) -> signal-U, one
  tick each. Linked press events fill the tick before `walking_state` follows. The arrow keys work
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
- **State:** one register REG (`Each + 0`, green self-loop) holds X, Y (displacement from
  SPAWN) and R (unbounded turn, in quarter view angles: the view angle is ((R >> 2) + A0) & 255).
  - **Status bar values (2026-11-02):** REG also holds HEALTH (`signal-H`), ARMOR (`signal-V`)
    and the ammo counts (bullets `signal-B`, shells `J`, rockets `K`, cells `C`) as offsets from
    `hud.START` (every weapon, ammo below Doom's caps), which `hud_from_port` adds on the wire
    (`Each + Each` with a constant).
  - **Contact state (2026-11-02):** the items' GONE flags and the pain clock COOL on the bus
    (architecture item 10); their deciders join the weapon columns (bus writers, then the REG
    writers in columns of `SPEND_ROWS`, snaking so the chain stays short).
  - **Weapon state (2026-11-02):** W (`signal-G`) and A (`signal-T`) on the state bus, plus the
    relayed FIRE and NEXT (`signal-N`); see architecture item 9. The weapon deciders sit two
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
- **Input-to-screen latency:** about 22 ticks (0.37 s) for both turning and walking: pad 3,
  gate or turn decider into REG 2, raycaster 17 with the lamps (18 before the angle wrap, item
  12). Firing: pad 3, relay 1, the animation's start 1, then the raycaster.
  - **History:** about 56 ticks at first (the user felt "almost a full second"), then 27 for
    turning and 35 for walking, then 21 (M2, flat walls), then this.
  - **The raycaster's 17:** camera lookups 1, ray products 1, ray shift / sign selectors 1, key
    product 1, key division 1, hit lookup 1, min trees 4, side split 1, strip lookups 1, quality
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
- M4 HUD (architecture item 8): the Freedoom status bar with live ammo, health, armor and face.
- M4 weapons (architecture item 9): seven weapons in front of the view, switching, Doom's firing
  animations and ammo use.
- M4 thing contact (architecture item 10): pickups, monster pain, solid monsters; the turn ramp.
- M4 shooting (architecture item 11): hitscan, monster health, pain and death animations, corpses.
- Chunks, layout and latency (architecture item 12): six rows per network on signal qualities
  (Space Age), about half the combinators, layout by concept, the bus on a substation row, 17-tick
  raycaster.

Commit only when the user asks.

## Next steps (in order)

1. **M4, the rest:** things are drawn (item 7) and can be shot (item 11); next are enemies
   (positions from a state register instead of constants: alpha/L from scalars per thing, walk and
   attack frames through the same frame lookup as pain and death), doors.
   - Shooting follow-ups: Doom's randomness (damage rolls, pain chance), shotgun spread, rocket
     splash, bullet puffs, more than 8 things. M5 (stretch): angled walls, varying heights.
   - HUD follow-ups: the face looking left/right and its pain/ouch frames, keys, owned weapons
     in the ARMS panel once there are pickups; all are more slots of the same kind.
   - Weapon follow-ups:
     pickups (weapons owned, ammo); the walking bob (it moves sideways too: about 10× the
     pictures, or a different way to draw the gun); Doom's muzzle light (A_Light1 brightens the view: a brightness term on
     the bus); the chainsaw and super shotgun.
   - Contact follow-ups: death (freeze the controls at health 0, and a restart: REG can't be
     cleared by itself, it needs every value negated into its input), Doom's blue armor class
     (absorbs a half; one class now), the face's ouch frame on a hit.
   - Thing follow-ups: more than 8 things (a fourth tree level costs a tick, or sort by depth on
     scalars); a second nearest layer so overlapping things don't cut each other; things taller
     than eye level (they'd need upper-half rows).
2. **Possible M3 follow-ups:** fix the mirrored faces; fewer strip deciders (4,096 of the
   ~13,000 combinators at 160×100); different flats per room (needs the map cell per pixel, which
   the wrapping product throws away); a lower-cost flat path (the 32 word deciders per row are
   most of it).
3. **Tooling as needed:** fsim speed for 320×200 work (about 70 ms per tick for the textured
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
