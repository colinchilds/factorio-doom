"""A 16x16 lamp screen showing an animated diagonal gradient, laid out like an early row
stage: one wire network per row whose lanes are the screen columns, a shared clock broadcast on
green, three east-facing arithmetic combinators per row, then the lamps.

  row y lanes:  v_x = x + y            (constant combinator, one signal per column)
  stage 1:      Each(red) + T(green)   (T is only on green, so it isn't a lane)
  stage 2:      Each % 16
  stage 3:      Each * 17 * 0x010101   (gray level -> packed RGB)
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from compiler.bp import EACH, GREEN, GREEN_IN, GREEN_OUT, LETTERS, RED, RED_IN, RED_OUT, Blueprint, cond, out, sig  # noqa: E402

W = H = 16
b = Blueprint("gradient demo")
cols = [sig(LETTERS[x]) for x in range(W)]
clock_in = b.constant(-8, -3, {"signal-T": 1})
clock = b.decider(-7, -3, [cond(sig("signal-T"), "<", 64, RED)], [out(sig("signal-T"), networks=RED)], direction=4)
b.wire(clock_in, 1, clock, RED_IN)
b.wire(clock, RED_OUT, clock, RED_IN)
prev_bus = None
for y in range(H):
    values = [(cols[x], x + y) for x in range(W) if x + y]
    c = b.constant(-8, y, values)
    a1 = b.arithmetic(-7, y, EACH, "+", sig("signal-T"), EACH, RED, GREEN, direction=4)
    a2 = b.arithmetic(-5, y, EACH, "%", 16, EACH, direction=4)
    a3 = b.arithmetic(-3, y, EACH, "*", 17 * 0x010101, EACH, direction=4)
    b.wire(c, 1, a1, RED_IN)
    b.wire(a1, RED_OUT, a2, RED_IN)
    b.wire(a2, RED_OUT, a3, RED_IN)
    lamps = [b.lamp(x, y, {"use_colors": True, "color_mode": 2, "rgb_signal": cols[x]}) for x in range(W)]
    b.wire(a3, RED_OUT, lamps[0], 1)
    b.chain(lamps, 1)
    if prev_bus is None:
        b.wire(clock, GREEN_OUT, a1, GREEN_IN)
    else:
        b.wire(prev_bus, GREEN_IN, a1, GREEN_IN)
    prev_bus = a1
b.power()
data = b.to_json()
data["fsim"] = {"ticks": 80, "doc": __doc__.strip()}
path = pathlib.Path(__file__).with_suffix(".json")
path.write_text(json.dumps(data, indent=1))
print(f"{path}: {len(b.entities)} entities")
