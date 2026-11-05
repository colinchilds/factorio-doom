"""Things: Freedoom monsters and items standing on the map, drawn by the raycaster with
occlusion (raycast.build_graph(..., things=...)).

Sprites are on the walls' texel grid (STEX = 64 texels per tile, Doom units / 2) in a box of
HP = 32 rows from 64 units above the floor down to it. Texel row te = View.thing_eye() is at eye
level (12 with the eye at 40 units, so monsters reach above it as in Doom; 0 with the eye at 64). Per column only the nearest
thing whose box covers it is drawn (a farther one is hidden in the nearer one's box columns,
transparent texels included). A thing may move (the `pos` hook: a monster's position from the
state bus, monsters.py).

Per thing s at (ex, ey), every frame (scalars, from the camera's angle and position):
    rx, ry      ex - PX, ey - PY (1/256 tiles)
    alpha       rx DX + ry DY: depth (2^18 per tile; DX, DY the view direction at 1024)
    L           ry DX - rx DY: lateral offset (2^18 per tile)
    sigma       alpha / 1551 (= alpha * PLANE / 2^20)
Per column (lanes):
    U = camx * sigma - L + C    the box column u times 2^F (u + 1 in [1, texw + 1))
    Zt_s = U + (alpha >> 10) << KB   on columns the box covers (range decider), else absent
    Dt_s = alpha * 64 + frame        the same columns: precise depth with the frame in the low bits
Two min trees over the things (absent lanes lose) give per column Zt (the key: u, ordered by
depth first) and Dt (depth and frame). With the `back` hook, things that yield (items, dying and
dead monsters) lose to the rest wherever their boxes overlap: their leaves carry 2^30 more on Zt and
2^29 more on Dt (only in front of NEAR, so one behind the camera still fails it), and Dt drops it
after the trees (AND 2^29 - 1; Zt's goes with SK's AND). A live monster behind a corpse is drawn
whole; the corpse is cut where the monster's box covers it. One tick more (game.py: 18).
Then:
    SK = Zt AND (2^KB - 1), FR = Dt AND 63 -> lookups per (frame, u): a mask word (bit 31 - t:
        texel row t is opaque) and HP / 8 texel words (8 texels each, like the wall strips)
    row y (r = 2y + 1 - H), texel row t = te + Q below the eye and te - 1 + Q above it, with
    Q = Dt / (4 cs_y) = 4 STEX z truncated toward 0 (negative above the eye; per chunk of rows in
    raycast.build_graph, cs_y a constant vector over its lanes):
      kill   P = 2^t (0 for t < 0 or t >= 32: the offset te or te - 1 is a constant on P's input),
             OM = mask * P: the sign is row t's opacity.
             OM < 0 and the wall farther than the thing (D - Dt / 512 > 0) -> 2^29 onto the wall's
             V, which then misses every wall mux and the bounded flat select: nothing else shows.
      texel  Vs = Dt / cs_y plus 4 te (+ 3 above the eye: 4 t to 4 t + 3 either way), a mux per
             word, a shift, and one palette decider per color copying that color times the thing's
             brightness on columns where it is in front of the wall.
The kill has to reach V's network with V, so the thing path is timed backwards from there: trees
out 4 ticks before V (t_v; the chunks' quality spread adds one). Up to 8 things fit (3 levels).
"""
import json
import math
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from compiler import semantics as sem  # noqa: E402
from compiler.netlist import EACH, GREEN, LANE_SIGNALS, RED, Collect, Scalar  # noqa: E402
import maps  # noqa: E402
from palette import nearest, pack, quantize  # noqa: E402

ASSETS = ROOT / "assets" / "freedoom" / "sprites.json"
STEX = 64                  # sprite texels per tile (each texel 2x2 Doom units, like walls at tex 64)
HP = STEX // 2             # box rows: eye level down to the floor
F = 12                     # fraction bits of U (the box column)
QD = 10                    # Zt's depth: alpha >> QD (1/256 tile)


def div_sigma(plane):
    """sigma = alpha * plane / 2^20 = alpha / div_sigma(plane)."""
    return round((1 << 20) / plane)

NEAR = 1 << 16             # alpha of 1/4 tile: nearer (or behind) things aren't drawn
KILL = 1 << 29             # added to a killed pixel's V
HIDE = -(1 << 23)          # added to a hidden thing's depth (column_stage hide)
FRAME_BITS = 6
YIELD_D = 1 << 29          # added to a yielding thing's Dt leaf (Dt = alpha * 64 + frame < 2^29)
COLORS = 15                # thing texels 1..15, 0 = transparent

# Things on the current map (maps.select): (x, y) in tiles and the sprite frame, monsters first.
THINGS = list(maps.ARENA["things"])


def load(names, shade):
    """Frames at STEX: each in its type's box (types = name[:4]), centered on its x offset, the
    floor at the bottom. Returns {"frames": [{name, type, texels[t][u]}], "types": {type: texw},
    "colors": c4 per texel value 1..15}. A texel is opaque when at least 2 of its 4 pixels are."""
    data = json.loads(ASSETS.read_text())
    pal = data["palette"]
    spr = {n: data["sprites"][n] for n in names}
    half = {}
    for n, s in spr.items():
        t = n[:4]
        half[t] = max(half.get(t, 0), s["left"], s["width"] - s["left"])
        if s["top"] > 2 * HP - 2:
            raise ValueError(f"{n}: {s['top']} units tall, the box holds {2 * HP - 2}")
    types = {t: 2 * -(-hv // 2) for t, hv in half.items()}   # texw (texels), even
    raw = []
    for n, s in spr.items():
        texw = types[n[:4]]
        px = [[None] * texw for _ in range(HP)]
        for t in range(HP):
            for u in range(texw):
                acc, k = [0, 0, 0], 0
                for yy in (s["top"] - 2 * (HP - t), s["top"] - 2 * (HP - t) + 1):
                    for xx in (s["left"] + 2 * u - texw, s["left"] + 2 * u - texw + 1):
                        if 0 <= yy < s["height"] and 0 <= xx < s["width"] and s["rows"][yy][xx] >= 0:
                            i = s["rows"][yy][xx]
                            for c in range(3):
                                acc[c] += pal[i][c]
                            k += 1
                if k >= 2:
                    px[t][u] = tuple(round(a / k) for a in acc)
        raw.append((n, px))
    centers = quantize([c for _, px in raw for r in px for c in r if c is not None], COLORS)
    frames = [{"name": n, "type": n[:4], "texels": [[0 if c is None else 1 + nearest(centers, c) for c in r] for r in px]}
              for n, px in raw]
    for f in frames:
        assert not any(f["texels"][0]), "row 0 must stay transparent (Q = 0 has no lane)"
    return {"frames": frames, "types": types, "colors": [shade.c4(c) for c in centers]}


def mask_word(texels, u):
    return sem.i32(sum(1 << (31 - t) for t in range(HP) if texels[t][u]))


def texel_word(texels, u, j):
    return sem.i32(sum(texels[8 * j + n][u] << (28 - 4 * n) for n in range(8)))


def color(c4, br):
    return (pack(c4) if any(c4) else 1) * br


def key_bits(types):
    return max(((max(types.values()) + 1) << F).bit_length(), 1)


def row_divisor(y, h, proj=None):
    """cs_y with Vs = Dt / cs_y = 4 STEX z (z below eye level, in tiles), for r = 2y + 1 - h > 0
    (proj: the projection height in rows, h unless the status bar crops the view)."""
    return round((proj or h) * (1 << 23) / (STEX * (2 * y + 1 - h)))


def first_row(h):
    return h // 2 + (h % 2)   # r = 2y + 1 - h > 0


def mul(g, a, table, out, label):
    """a (red) * table (green): every thing's products share the table's green network (on one
    wire with a, each thing would need its own copy of the lookup)."""
    return g.op("arith", sem.arith_spec(a.signal, "*", table.signal, out, RED, GREEN), red=[a.node],
                green=[table.node], label=label)


def tree(g, level, name, levels=None):
    """Lane-wise min over the leaves; absent lanes (0) lose. levels: a list to record each level's
    nodes in (for the layout)."""
    depth = 0
    while len(level) > 1:
        nxt = []
        if levels is not None:
            levels.append(nxt)
        for i, (a, b) in enumerate(zip(level[::2], level[1::2])):
            conds = [sem.cond(EACH, "<", EACH, RED, None, GREEN), sem.cond(EACH, "!=", 0, RED, "and"),
                     sem.cond(EACH, "=", 0, GREEN, "or")]
            nxt.append(g.decider(conds, [sem.out(EACH, networks=RED)], [sem.out(EACH, networks=GREEN)],
                                 red=a, green=b, label=f"{name}.{depth}.{i}"))
        level = nxt + level[len(level) - len(level) % 2:]
        depth += 1
    return level[0]


def column_stage(g, w, vec, camx, look, tabs, pos0, things, sprites, t_v, shade, hide=None, frame=None, plane=676,
                 pos=None, light=None, back=None):
    """The per-thing scalars, the per-column key/depth vectors and the lookup blocks. Returns a dict
    of nodes for the row stage (all timed against t_v, the wall V's time), and each thing's depth
    and lateral offset ("alpha", "lat": Scalars) for the aim (combat.py).

    frame(g, i, fid): a Scalar added to thing i's frame id (fid: name -> id), or None for its
    THINGS frame throughout (a monster's pain and death frames, combat.py).

    hide(g, i, out): a Scalar adding HIDE (or 0) to thing i's depth signal `out`, or None. A hidden
    thing's depth is behind the camera, so neither leaf passes and it isn't drawn (a picked-up
    item). |alpha| < 2^23 on a 16-tile map, so alpha - 2^23 stays negative, and its Zt base
    (alpha >> qd) << KB stays above -2^31 with qd = QD for KB <= 17; wider boxes (a dying imp's)
    take one bit of Zt's depth per extra key bit (qd = QD + KB - 17).

    plane: the camera plane (raycast.View). pos(g, i, out_x, out_y): thing i's offset from the
    camera (rx, ry) as Scalars on those signals, ready with the camera position X0 (a moving
    monster's position from the state bus), or None for its fixed place.

    light: the muzzle light's LS = 512 / LK (raycast.LIGHT_K), or None: Dt / LS (about LK times the
    thing's D) joins the brightness numerator, so things brighten with the walls.

    back(g, i): None (thing i never yields), True (always: an item) or a Scalar, 1 while it yields
    (a dying or dead monster), or no hook at all. Yielding things lose the trees to the rest (see
    the module docstring); the trees then need one more tick (t_v - 5)."""
    if len(things) > 8:
        raise ValueError(f"{len(things)} things: at most 8 fit in the latency budget")
    X0, Y0, (ox, oy) = pos0
    DX = look("DX", {a: tabs[a]["DX"] for a in range(256)})
    DY = look("DY", {a: tabs[a]["DY"] for a in range(256)})
    NDY = look("-DY", {a: -tabs[a]["DY"] for a in range(256)})
    fid = {f["name"]: i + 1 for i, f in enumerate(sprites["frames"])}
    types = sprites["types"]
    KB = key_bits(types)
    qd = QD + max(0, KB - 17) if hide else QD
    yz = (1 << 30) >> KB   # in dc units: 2^30 on Zt (dc << KB stays below 2^30)
    zleaves, dleaves, alphas, lats, rows = [], [], [], [], []
    for i, (tx, ty, name) in enumerate(things):
        ex, ey = int(tx * 256), int(ty * 256)
        moved = pos(g, i) if pos else None
        if moved:
            rx, ry = moved
        else:
            rx = g.ss(ex - ox, "-", X0, f"rx{i}")
            ry = g.ss(ey - oy, "-", Y0, f"ry{i}")
        sa, sl = g.new_scalar_signal(), g.new_scalar_signal()
        parts = [mul(g, rx, DX, sa, f"a{i}x"), mul(g, ry, DY, sa, f"a{i}y")]
        hid = hide(g, i, sa) if hide else None
        if hid is not None:
            if hid.node.time > parts[0].time:
                raise ValueError(f"thing {i}: hide flag at {hid.node.time}, depth at {parts[0].time}")
            parts.append(g.delay(hid.node, parts[0].time - hid.node.time))
        alpha = Scalar(Collect(parts), sa)
        lat = Scalar(Collect([mul(g, ry, DX, sl, f"l{i}x"), mul(g, rx, NDY, sl, f"l{i}y")]), sl)
        alphas.append(alpha)
        lats.append(lat)
        sigma = g.ss(alpha, "/", div_sigma(plane), f"sigma{i}")
        dc = g.ss(alpha, ">>", qd, f"dc{i}")
        yl = back(g, i) if back else None
        ybias = []
        if yl is not None and yl is not True:
            # Yielding, and in front of NEAR (so the bias can't lift one behind the camera past it):
            # yz onto dc's wire, YIELD_D onto every column of the Dt leaf's green input.
            yc = [sem.cond(alpha.signal, ">=", NEAR, RED), sem.cond(yl.signal, ">", 0, RED, "and")]
            yzn = g.decider(yc, [sem.out(dc.signal, constant=yz)], red=[alpha.node, yl.node], label=f"yield z{i}")
            ybias = [g.decider(yc, [sem.out(s_, constant=YIELD_D) for s_ in LANE_SIGNALS[:w]], red=[alpha.node, yl.node],
                               label=f"yield d{i}")]
            dc = Scalar(Collect([dc.node, yzn]), dc.signal)
        U = [g.vs(vec(camx, "camx"), "*", sigma, f"u{i}"), g.vs(vec([-1] * w, "-1"), "*", lat, f"lat{i}")]
        ZB = g.vs(vec([1 << KB] * w, "2^KB"), "*", dc, f"zb{i}")
        DB = g.vs(vec([1 << FRAME_BITS] * w, "64"), "*", alpha, f"db{i}")
        texw = types[name[:4]]
        C = (texw // 2 + 1) << F
        static = yl is True   # an item: the bias rides on the leaves' constant outputs
        rng = [sem.cond(EACH, ">=", -(texw // 2) << F, RED), sem.cond(EACH, "<", (texw // 2) << F, RED, "and")]
        zleaves.append(g.decider(rng + [sem.cond(EACH, ">=", (NEAR >> qd) << KB, GREEN, "and")],
                                 [sem.out(EACH, constant=C + ((yz << KB) if static else 0)), sem.out(EACH, networks=RED),
                                  sem.out(EACH, networks=GREEN)],
                                 red=U, green=ZB, label=f"zt{i}"))
        fo = frame(g, i, fid) if frame else None
        DF = [DB, g.broadcast(vec([1] * w, "ones"), fo, f"frame{i}")] if fo else DB
        if ybias:
            DF = (DF if isinstance(DF, list) else [DF]) + ybias
        dleaves.append(g.decider(rng + [sem.cond(EACH, ">=", NEAR << FRAME_BITS, GREEN, "and")],
                                 [sem.out(EACH, constant=fid[name] + (YIELD_D if static else 0)), sem.out(EACH, networks=GREEN)],
                                 red=U, green=DF, label=f"dt{i}"))
        rows.append([rx.node, ry.node] + parts[:2] + lat.node.nodes + ([hid.node] if hid is not None else [])
                    + [sigma.node] + (dc.node.nodes if isinstance(dc.node, Collect) else [dc.node]) + U + [ZB, DB]
                    + (DF[1:] if isinstance(DF, list) else []) + [zleaves[-1], dleaves[-1]])
    tz, td = [], []
    Zt, Dt = tree(g, zleaves, "zmin", tz), tree(g, dleaves, "dmin", td)
    at = t_v - 4
    strip = []
    if back:
        Dt = g.vc(Dt, "AND", YIELD_D - 1, "thing depth strip")
        strip = [Dt]
    if Zt.time > at or Dt.time > at:
        raise ValueError(f"thing path too long: trees at {max(Zt.time, Dt.time)}, needed by {at}")
    Zt, Dt = g.delay(Zt, at - Zt.time), g.delay(Dt, at - Dt.time)
    SK = g.vc(Zt, "AND", (1 << KB) - 1, "thing key")
    FR = g.vc(Dt, "AND", (1 << FRAME_BITS) - 1, "thing frame")
    NSD = g.vc(Dt, "/", -512, "-thing depth")
    SD = g.vc(Dt, "/", 512, "thing depth")
    # Lookups: per (frame, u) a mask word and HP / 8 texel words (zero words left out).
    masks, words = [], [[] for _ in range(HP // 8)]
    for f in sprites["frames"]:
        for u in range(types[f["type"]]):
            conds = [sem.cond(EACH, ">=", (u + 1) << F, RED), sem.cond(EACH, "<", (u + 2) << F, RED, "and"),
                     sem.cond(EACH, "=", fid[f["name"]], GREEN, "and")]
            m = mask_word(f["texels"], u)
            if m:
                masks.append(g.decider(conds, [sem.out(EACH, constant=m)], red=SK, green=FR, label=f"mask.{f['name']}.{u}"))
            for j in range(HP // 8):
                wd = texel_word(f["texels"], u, j)
                if wd:
                    words[j].append(g.decider(conds, [sem.out(EACH, constant=wd)], red=SK, green=FR,
                                              label=f"sw.{f['name']}.{u}.{j}"))
    # Word buses: summed once (a bus of many deciders can't take per-source delays), then delayed
    # to meet the texel muxes (V + 1).
    wsum = [g.op("arith", sem.arith_spec(EACH, "+", 0, EACH, RED), red=ws, label=f"thing word{j}") for j, ws in enumerate(words)]
    # Brightness where the thing is in front of the wall: VB at V's time, colors one tick later.
    # The muzzle light (raycast.LIGHT_K): light is LS = 512 / LK, so Dt / LS ~ LK * SD rides on the
    # numerator, computed beside SD.
    lit = [g.vs(Dt, "/", light, "lit thing depth")] if light else []
    BRS = g.vv([vec([shade.bright] * w, "bright")] + lit, "/", [SD, vec([shade.fog] * w, "fog")], "thing brightness")
    return {"Zt": Zt, "Dt": Dt, "SK": SK, "FR": FR, "NSD": NSD, "BRS": BRS, "lit": lit, "masks": masks, "words": words,
            "wsum": wsum, "lookups": masks + [n for ws in words for n in ws], "KB": KB, "alpha": alphas, "lat": lats,
            "rows": rows, "trees": (tz, td), "tail": strip + [Zt, Dt, SK, FR, NSD, SD], "tables": [DX, DY, NDY]}


# ---------------------------------------------------------------------------------------------
# Float reference

def ideal(img, vals, things, sprites, wall_d, w, h, shade, proj=None, view=None, xy=None):
    """Draw the things into img (float math): per column the nearest covering thing, per row its
    texel where opaque and nearer than the wall (wall_d[c]: perpendicular distance, tiles).
    view: raycast.View; xy: {i: (x, y)} tiles for things that moved."""
    from raycast import DS, View  # noqa: E402
    view = view or View()
    plane, te = view.plane, view.thing_eye()
    px, py = vals["PX"] / 256, vals["PY"] / 256
    dx, dy = vals["DX"] / 1024, vals["DY"] / 1024
    fr = {f["name"]: f for f in sprites["frames"]}
    for c in range(w):
        camx = ((2 * c + 1 - w) * 1024 // w) / 1024
        best = None
        for i, (tx, ty, name) in enumerate(things):
            tx, ty = (xy or {}).get(i, (tx, ty))
            rx, ry = tx - px, ty - py
            a = rx * dx + ry * dy
            if a < NEAR / (1 << 18):
                continue
            lat = ry * dx - rx * dy
            texw = sprites["types"][name[:4]]
            u = math.floor((a * camx * plane / 1024 - lat) * STEX + texw / 2)
            if 0 <= u < texw and (best is None or a < best[0]):
                best = (a, u, fr[name])
        if best is None or best[0] >= wall_d[c]:
            continue
        a, u, f = best
        br = int(shade.bright / (a * DS + shade.fog))
        for y in range(h):
            t = te + math.floor((2 * y + 1 - h) * a / (2 * (proj or h)) * STEX)
            if 0 <= t < HP and f["texels"][t][u]:
                img[y][c] = color(sprites["colors"][f["texels"][t][u] - 1], br)
