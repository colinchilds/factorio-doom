#!/usr/bin/env python3
"""Run blueprint cases in a real (windowless) Factorio and collect the dump.

  python3 harness/run.py conformance/cases            # all *.json in a directory
  python3 harness/run.py a.json b.json -o out.json
  python3 harness/run.py --discover                    # export the field-discovery blueprint
  python3 harness/run.py --bench 300 a.json b.json     # UPS: median ms/tick per blueprint
  python3 harness/run.py --dlc --bench 30 a.json       # same, in a Space Age game (import check)
  python3 harness/run.py --lanes                       # signals safe to use as lanes, with/without DLC
  python3 harness/run.py --poles                       # pole supply area and reach per quality (DLC)

Uses an isolated config and mod directory under harness/, so the user's saves and mods are
never touched. Set FACTORIO to override the binary path.
"""

import argparse
import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "conformance"))
from bp import encode  # noqa: E402

DEFAULT_BIN = pathlib.Path.home() / "Library/Application Support/Steam/steamapps/common/Factorio/factorio.app/Contents/MacOS/factorio"
INSTANCE = HERE / "instance"
DATA = INSTANCE / "data"
MODS = HERE / "mods"
OUTPUT = DATA / "script-output" / "fsim"


def lua_str(s):
    return "[==[" + s + "]==]"


def write_cases(cases, warmup, mode="run", every=1):
    lines = [f"return {{ mode = {lua_str(mode)}, warmup = {warmup}, every = {every}, cases = {{"]
    for c in cases:
        ins = ", ".join(f"{{ {k}, {e}, {lua_str(item)}, {n} }}" for k, e, item, n in c.get("inserts", []))
        lines.append(f"  {{ name = {lua_str(c['name'])}, ticks = {c['ticks']}, inserts = {{ {ins} }}, "
                     f"bp = {lua_str(c['bp'])} }},")
    lines.append("} }")
    (MODS / "fsim-harness" / "cases.lua").write_text("\n".join(lines) + "\n")


def factorio(binary, *args):
    cmd = [str(binary), "-c", str(INSTANCE / "config.ini"), "--mod-directory", str(MODS), "--disable-audio", *args]
    res = subprocess.run(cmd, capture_output=True, text=True)
    log = res.stdout + res.stderr
    if res.returncode != 0 or "Error" in log:
        sys.stderr.write(log)
        raise SystemExit(f"factorio failed: {' '.join(args)}")
    return log


@contextlib.contextmanager
def dlc_mods(enabled):
    """Temporarily enable every mod in the harness mod list (space-age, quality, elevated-rails)."""
    mod_list = MODS / "mod-list.json"
    original = mod_list.read_text()
    try:
        if enabled:
            ml = json.loads(original)
            for m in ml["mods"]:
                m["enabled"] = True
            mod_list.write_text(json.dumps(ml, indent=2))
        yield
    finally:
        mod_list.write_text(original)


def survey(binary, dlc):
    """Run lanes.lua with the DLC mods on or off; returns {(type, name): flags}."""
    with dlc_mods(dlc):
        write_cases([], 0, mode="lanes")
        shutil.rmtree(OUTPUT, ignore_errors=True)
        save = DATA / "fsim-lanes.zip"
        factorio(binary, "--create", str(save))
        factorio(binary, "--benchmark", str(save), "--benchmark-ticks", "3")
    return {(x["type"], x["name"]): x for x in json.loads((OUTPUT / "lanes.json").read_text())}


def lanes(binary):
    """Lane signals for generated blueprints: not virtual (those are kept for named scalars), not
    hidden or parameter signals in either game, and present in both vanilla and Space Age. Kept
    in vanilla signal order."""
    vanilla, dlc = survey(binary, False), survey(binary, True)
    order = json.loads((ROOT / "conformance" / "signal_order.json").read_text())
    bad = lambda f: f["hidden"] or f["parameter"] or f["special"]
    out, dropped = [], []
    for t, name, q in order:
        if q != "normal" or t == "virtual" or (t, name) not in vanilla:
            continue
        if (t, name) in dlc and not bad(vanilla[(t, name)]) and not bad(dlc[(t, name)]):
            out.append([t, name])
        else:
            dropped.append(name)
    dest = ROOT / "conformance" / "lane_signals.json"
    dest.write_text("[\n" + ",\n".join(json.dumps(x) for x in out) + "\n]\n")
    print(f"{len(out)} lane signals -> {dest}; dropped {len(dropped)}: {' '.join(dropped[:60])} ...")


def poles(binary):
    """Supply area and wire reach of each electric pole at every quality, from the prototypes of a
    Space Age game (quality levels above normal need the quality mod)."""
    with dlc_mods(True):
        write_cases([], 0, mode="poles")
        shutil.rmtree(OUTPUT, ignore_errors=True)
        save = DATA / "fsim-poles.zip"
        factorio(binary, "--create", str(save))
        factorio(binary, "--benchmark", str(save), "--benchmark-ticks", "3")
    data = json.loads((OUTPUT / "poles.json").read_text())
    dest = ROOT / "conformance" / "pole_quality.json"
    dest.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    for pole, qs in data.items():
        print(pole, {q: (v["supply_area_distance"], v["max_wire_distance"], v["max_circuit_wire_distance"])
                     for q, v in sorted(qs.items(), key=lambda kv: kv[1]["level"])})
    print(f"-> {dest}")


def load_cases(paths):
    files = []
    for p in map(pathlib.Path, paths):
        files += sorted(p.glob("*.json")) if p.is_dir() else [p]
    cases = []
    for f in files:
        data = json.loads(f.read_text())
        ticks = data.get("fsim", {}).get("ticks", 10)
        bp = {k: v for k, v in data.items() if k != "fsim"}
        cases.append({"name": f.stem, "ticks": ticks, "bp": encode(bp),
                      "inserts": data.get("fsim", {}).get("inserts", [])})
    return cases


BENCH_COLUMNS = ["wholeUpdate", "controlBehaviorUpdate", "electricHeatFluidCircuitUpdate", "entityUpdate", "scriptUpdate"]


def bench(binary, cases, warmup, measure):
    """Run each case alone with --benchmark-verbose; median ms per tick after power-up."""
    print(f"{'case':<28} {'entities':>8} " + " ".join(f"{c[:12]:>12}" for c in BENCH_COLUMNS) + "   (median ms/tick)")
    results = {}
    for c in cases:
        write_cases([c], warmup, mode="bench")
        shutil.rmtree(OUTPUT, ignore_errors=True)
        save = DATA / "fsim-base.zip"
        factorio(binary, "--create", str(save))
        settle = 10
        ticks = 1 + warmup + settle + measure
        log = factorio(binary, "--benchmark", str(save), "--benchmark-ticks", str(ticks), "--benchmark-verbose", "all")
        info = json.loads((OUTPUT / "bench.json").read_text())
        t0 = info["t0"]
        case_info = info["cases"][c["name"]]
        if case_info["unpowered"]:
            raise SystemExit(f"{c['name']}: {case_info['unpowered']} entities unpowered; timings meaningless")
        header, rows = None, []
        for line in log.splitlines():
            if line.startswith("tick,"):
                header = line.split(",")
            elif header and line.startswith("t") and line[1:2].isdigit():
                f = line.split(",")
                if int(f[0][1:]) >= t0 + settle:
                    rows.append(f)
        idx = [header.index(col) for col in BENCH_COLUMNS]
        med = []
        for i in idx:
            vals = sorted(int(r[i]) for r in rows)
            med.append(vals[len(vals) // 2] / 1e6)
        results[c["name"]] = {"entities": case_info["entities"], "ticks": len(rows),
                              **{col: m for col, m in zip(BENCH_COLUMNS, med)}}
        print(f"{c['name']:<28} {case_info['entities']:>8} " + " ".join(f"{m:>12.3f}" for m in med), flush=True)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("-o", "--out", default=str(HERE / "out" / "dump.json"))
    ap.add_argument("--warmup", type=int, default=30, help="ticks between build and t=0 (power up)")
    ap.add_argument("--every", type=int, default=1,
                    help="record every n-th tick only (the others stay empty rows, which fsim conform skips): "
                         "big, long runs otherwise run out of memory")
    ap.add_argument("--discover", action="store_true", help="export a blueprint with every option set")
    ap.add_argument("--signals", action="store_true", help="write the game's signal order to conformance/")
    ap.add_argument("--dlc", action="store_true",
                    help="run with Space Age, quality and elevated rails enabled (fsim models vanilla only)")
    ap.add_argument("--lanes", action="store_true",
                    help="write conformance/lane_signals.json: signals usable in vanilla and in Space Age")
    ap.add_argument("--poles", action="store_true",
                    help="write conformance/pole_quality.json: pole supply area and reach per quality")
    ap.add_argument("--bench", type=int, metavar="TICKS", help="time each blueprint alone for TICKS ticks")
    args = ap.parse_args()
    binary = pathlib.Path(os.environ.get("FACTORIO", DEFAULT_BIN))
    DATA.mkdir(parents=True, exist_ok=True)

    with dlc_mods(args.dlc):
        run(args, binary, ap)


def run(args, binary, ap):
    if args.bench:
        results = bench(binary, load_cases(args.paths), args.warmup, args.bench)
        if args.out != ap.get_default("out"):
            pathlib.Path(args.out).write_text(json.dumps(results, indent=1))
        return

    if args.lanes:
        return lanes(binary)
    if args.poles:
        return poles(binary)

    if args.discover or args.signals:
        write_cases([], 0, mode="discover" if args.discover else "signals")
        ticks = 6
    else:
        cases = load_cases(args.paths)
        if not cases:
            raise SystemExit("no cases")
        write_cases(cases, args.warmup, every=args.every)
        ticks = 1 + args.warmup + max(c["ticks"] for c in cases) + 3

    DATA.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(OUTPUT, ignore_errors=True)
    save = DATA / "fsim-base.zip"
    start = time.time()
    factorio(binary, "--create", str(save))
    log = factorio(binary, "--benchmark", str(save), "--benchmark-ticks", str(ticks))
    elapsed = time.time() - start

    if args.discover:
        print((OUTPUT / "discover.txt").read_text())
        return
    if args.signals:
        sigs = json.loads((OUTPUT / "signals.json").read_text())
        dest = ROOT / "conformance" / ("signal_order_dlc.json" if args.dlc else "signal_order.json")
        dest.write_text("[\n" + ",\n".join(json.dumps(x) for x in sigs) + "\n]\n")
        print(f"{len(sigs)} signals -> {dest}")
        return
    dump = OUTPUT / "dump.json"
    if not dump.exists():
        sys.stderr.write(log)
        raise SystemExit("no dump written")
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(dump, out)
    print(f"{len(cases)} cases, {ticks} ticks in {elapsed:.1f}s -> {out}")


if __name__ == "__main__":
    main()
