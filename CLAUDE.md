# factorio-doom: agent brief

A Doom-style shooter built from Factorio 2.1 combinators (no mods; Space Age for signal qualities),
shown on a lamp screen. Python generators emit blueprints; a Rust simulator (fsim) and a game
harness check them.

- [README.md](README.md): usage, options, layout.
- [docs/design.md](docs/design.md): how the renderer and game logic work. Read the relevant
  section before changing a subsystem; module docstrings in `doom/` have the exact formulas.
- [docs/semantics.md](docs/semantics.md): measured game behavior that everything relies on.
- [docs/roadmap.md](docs/roadmap.md): what's next.

## Constraints

- **Original or freely licensed content only.** Art comes from Freedoom (BSD,
  `assets/freedoom/`); maps are our own. Never add id Software's levels or art. The default
  song (`music/level1.txt`) is a fan-made MIDI recreation, converted with MIDItorio.
- **Screen size is a build parameter.** Never hard-code a resolution: everything derives from W and
  H. Check changes at several sizes (at least 64×40, 160×100 and 320×200).
- **Python is standard library only.** Rust is only for fsim.
- **Lane signals come from `conformance/lane_signals.json`**, never straight from the signal order:
  signals that exist and are visible in both vanilla and Space Age, so blueprints import in either
  and never become parametrised. Named scalars use virtual signals.
- **Blueprints for players power themselves:** `b.power()` places substations, `b.link_power()`
  copper-wires them. Player builds default to legendary substations (`--substation-quality`).
- **The input pad sits right beside the screen**, since players zoom in on the lamps.

## Workflow

1. Generate: `python3 doom/game.py --size 64x40` (or `raycast.py` for the renderer alone). Output
   goes to `doom/gen/` (`.json` for tools, `.txt` to paste).
2. Verify in fsim: `--verify` compares the simulated blueprint with the netlist evaluation, a float
   raycaster and the plain-Python rules (`step` functions). Use `--map test` for the game: the
   scripted walk and fight are tuned on that map. Everything must report 0 differences.
3. Check in the game: `python3 harness/run.py --dlc --bench 30 FILE.json` must import with every
   entity powered. For semantics, record with `--dlc --warmup 60` (`--every 2` for long runs) and
   compare with `fsim conform`.
4. Refactors that shouldn't change the circuit: compare the generated `.json` byte for byte before
   and after.

Build fsim first: `cargo build --release && cargo test --release`. The harness starts its own
isolated Factorio instance (`harness/instance/`); it never touches other saves or mods.

## Rules

- **Never rely on unverified game behavior.** Add a conformance case to `conformance/gen.py`, run
  it in the game, make fsim match, and refresh the golden dump. Obvious guesses have been wrong
  before: the min tie order, lanes when red + green sum to 0, wire reach rules, and lamp color
  timing.
- **Treat fsim's dropped-wire warnings as errors.** The game silently drops a wire whose footprint
  gap exceeds the shorter reach of its ends (9 for combinators). Between two poles, reach is
  measured center to center. `Blueprint.check_reach()` checks both; call it on every generated
  blueprint.
- **When a game result looks wrong, check power and wiring first.** Unpowered entities and
  over-long wires have masqueraded as semantics puzzles.
- **Zero lanes vanish.** A signal at 0 doesn't exist, so keep vectors nonzero on every lane (or
  read them with a decider that also tests a full vector). Keep a decider's outputs on one signal
  from summing to 0: count down while > 1, not > 0.
- **The game's warm-up runs a tick longer than fsim's.** Free-running state (toggles, counters
  from power-on) comes out out of phase; start such state from a signal instead.
- **Additions ride on wires for free.** Fold constant and vector additions into a consumer's
  input rather than spending a tick. The raycaster is 18 ticks deep; don't add latency without
  reason.
- **`lookup_scalar` must be sized for its values;** it refuses values that don't fit its bits.
- **Scripted inputs:** add new entries to the end of game.py's script lists. Drop timing is drawn
  in order, so an entry in the middle changes the whole walk.
- **Screen lamps need `always_on`**, or they stay dark in daylight (the harness runs at night, so
  it can't catch this).
- **Quality above normal needs Space Age.** Check player builds with `harness/run.py --dlc`;
  comparisons without the DLC need `--substation-quality normal`.
- Two verifies of the same size and map at once share their frame folder under `out/`; run them
  one at a time.
