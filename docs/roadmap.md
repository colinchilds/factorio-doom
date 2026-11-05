# Roadmap

## In progress: the acid hall

A showcase level on the `acid` map: a low tunnel opening into a tall hall, a brown walkway
zigzagging over acid, shotgun guys. The floor stays at one height (the acid is a different flat
at the same level, without damage).

1. **Floors per tile and the map:** done.
2. **Two ceiling heights**, with a fixed eye: tall walls in the hall, a low tunnel and start room.
   With a flat floor and a fixed eye, only entering a low region matters, and a ray enters a
   rectangle once. So per low region (a rectangle), a min tree gives each column's entry and exit
   distances (lookups on the march's crossings, like the wall hits). Per pixel in the upper half, a
   few comparisons with the row's constants pick the surface: the hall's ceiling, the wall above
   the opening, the tunnel's ceiling, or the walls behind cut by the tunnel's ceiling edge. Tall
   walls repeat their texture (more mux ranges). Estimate: 2–3k combinators at 320×200 and 1–2
   ticks. Prototype in `raycast.py` first.
3. **Shotgun guys:** Freedoom's shotgun zombie (SPOS), the zombieman's logic with 3 pellets, about
   30 health. Perhaps 6 monsters and 2 items (8 things).
4. **Tune the map** and a camera path.

## Gameplay

- **Doors.**
- **Monsters:** imp fireballs as projectiles (things that move), monsters blocking each other, waking
  on the player's shots (Doom's noise), Doom's pain chance and damage rolls (the LCG is there),
  sprite rotations, more monster types.
- **Shooting:** shotgun spread, rocket splash, bullet puffs.
- **Things:** more than 8 (a fourth tree level costs a tick, or sort by depth on scalars); a second
  nearest layer so two overlapping monsters don't cut each other.
- **Weapons:** pickups (weapons owned, ammo), the walking bob, the chainsaw and super shotgun.
- **Status bar:** keys; the ARMS panel showing owned weapons; the face's evil grin on a weapon
  pickup.
- **Armor:** Doom's blue armor class (absorbs half).
- **Death:** the view dropping to the floor.
- **Screen flashes** (Doom's ST_doPaletteStuff): red on damage, gold on pickups. The damage count is
  already on M. Walls, flats and things could scale through the brightness (like the muzzle light)
  plus a red broadcast per chunk; the weapon's and the bar's colors are exact constants, so they
  need another way.

## Sound effects

Short note patterns on programmable speakers, each triggered by an event on the bus: shots, pickups,
pain, monster pain and death, weapon switches, the player dying. Freedoom's effects are samples,
which speakers can't play, so these would be our own patterns. To measure first, as conformance
cases: what a speaker does with a signal held for several ticks, polyphony and global playback,
whether the instrument can come from the circuit, and UPS for a few hundred speakers.

## Renderer

- Fix the mirrored wall faces (a tick, or doubling the strip block).
- Fewer strip deciders (4,096 of the total).
- A cheaper flat path (the word deciders per chunk are most of it).
- **Angle ROM:** precompute everything that depends only on the view angle and column for all 256
  angles in constant combinators, selected by one decider per angle. Hits would arrive about 3
  ticks sooner, at the cost of a blueprint string several times larger.
- Stretch: angled walls (column lanes become map segments) and varying floor heights.

## Tooling

- A faster fsim for 320×200 work: its lookup deciders are about 100× slower than the game's.
