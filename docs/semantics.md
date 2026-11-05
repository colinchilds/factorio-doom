# Measured game semantics (Factorio 2.1.23)

Each of these was measured in the game and is pinned by a conformance case (`conformance/gen.py`);
fsim implements them and `cargo test` checks it against the game's recordings.

| Topic | Behavior |
|---|---|
| Timing | Every network is the sum of what feeds it; every combinator outputs one tick after it reads: `N(t) = constants + O(t-1)`, `O(t) = f(N(t))`. Lamp *color* follows its input one tick sooner than its *on/off condition* does. |
| `Each(red) op Each(green)` | Works lane by lane. The lanes are the union of signals on both wires; a side that's missing counts as 0. |
| Lanes | Lanes come only from the wires the `Each` operands read. A signal at +5 red / −5 green is **not** a lane for `Each(both)`. |
| Arithmetic | int32 that wraps. `/` and `%` truncate toward zero; x/0 → 0, `INT_MIN / -1` → 0. Shift amounts are masked to 5 bits, and `>>` keeps the sign. `^` wraps; a negative exponent gives 0 unless the base is ±1. |
| Named output from `Each` | Arithmetic sums the lanes, wrapping. A decider outputs constant × the matching lane count, or the sum of the matching lanes' values when copying from input. |
| Decider else-output | Per lane: lanes that fail the condition get the else outputs. With no lanes at all, neither branch outputs. |
| Decider `Each` ranges and outputs | Several `Each` conditions in one decider are evaluated per lane together, so `lo <= Each AND Each < hi OR ...` is a per-lane range lookup. Several outputs on the same lane add up: `Each = constant` plus `Each` copied from a wire gives constant + value, in either branch. |
| Decider `Each` lanes | An `Each` decider visits the union of the lanes on every wire read by any `Each` operand of any condition. A condition on green (e.g. `Each(green) != 0`, ANDed in) brings in lanes that are absent (0) on red, so a range test on red can match 0 there. |
| Decider logic | AND binds tighter than OR. `Everything` is vacuously true and `Anything` is false when there are no inputs. `Everything` output under `Each` conditions gives nothing. |
| Zero sums | A decider whose outputs on one signal sum to 0 (copy X plus a constant −X) shows that signal at 0 in the game's `signals_last_tick`; fsim leaves it out. Networks behave the same, but designs keep such sums from reaching 0. |
| Selector | Ties keep the game's internal signal order when selecting the max; the min list is the max list reversed. `index_comparator` supports `<`, `<=` and so on (N-highest / N-lowest). The index can come from a signal on either wire; a negative index gives nothing. Count and time modes are also covered. |
| Quality (Space Age) | `Each` arithmetic and deciders keep qualities apart; lamps read a signal at a quality. A selector in quality-transfer mode (static quality, destination `Each`) moves every input signal to one quality in one tick, summing signals that differ only in quality. Quality filter keeps one quality (quality-unknown ranks above legendary). |
| Signal order | The game's internal order (items, fluids, virtual, recipes, entities, …), saved in `conformance/signal_order.json` (vanilla: 1,439 signals × normal / quality-unknown = 2,878 lanes) and `signal_order_dlc.json` (Space Age: six qualities). |
| Lamps | Packed RGB = `(v>>16)&255, (v>>8)&255, v&255`. Components are clamped to 0–255. Color mapping uses the first positive color signal in prototype order; with none, the lamp shows its default color. A lamp with no wires ignores its circuit settings. Screen lamps need `always_on`, or they stay dark in daylight. |
| Chests | A chest's contents are on its wires like a constant combinator's signals, from the next tick. |
| Wire reach | A wire connects when the straight-line **gap between the two tile footprints** is ≤ the *shorter* reach of its two ends; **between two poles it's the distance between their centers** instead. Combinators, lamps and constants reach 9, small poles 7.5, medium 9, big 32, substations 18. Longer wires are silently dropped when a blueprint is built; fsim warns about them. Quality raises reach and supply area (`conformance/pole_quality.json`): a legendary substation supplies 28×28 and reaches 28, a legendary medium pole reaches 19. |
| Per-entity wire selection | `input_networks` / `output_networks` don't exist for combinators (they always choose wires per signal); only lamps and similar entities have them. |
| Constant combinators | A section holds at most 1,000 filters. |

Blueprint wire connector ids: 1/2 = red/green input (or the only connector on lamps, poles and
constant combinators), 3/4 = red/green combinator output, 5 = copper.

## Not simulated yet

- Selector modes: random, stack size, rocket capacity, quality transfer from a signal, and time
  mode's day signals.
- Power: fsim assumes everything is powered. The harness reports any unpowered entity as an error.
- Lamps' `input_networks` setting, and display panels.
