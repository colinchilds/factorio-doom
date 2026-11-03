"""M4 things: Freedoom monsters and items standing on the map, drawn by the raycaster with
occlusion (raycast.build_graph(..., things=...)).

Sprites are on the walls' texel grid (STEX = 64 texels per tile, Doom units / 2) in a box of
HP = 32 rows from eye level (z = 0) down to the floor (z = 0.5 tile), so they only show on the
lower half of the screen. Per column only the nearest thing whose box covers it is drawn (a
farther one is hidden in the nearer one's box columns, transparent texels included).

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
depth first) and Dt (depth and frame). Then:
    SK = Zt AND (2^KB - 1), FR = Dt AND 63 -> lookups per (frame, u): a mask word (bit 31 - t:
        texel row t is opaque) and HP / 8 texel words (8 texels each, like the wall strips)
    row y (r = 2y + 1 - H > 0), texel row t = Vs >> 2 with Vs = Dt / cs_y = 4 STEX z (per chunk of
    rows in raycast.build_graph, cs_y a constant vector over its lanes):
      kill   Q = Dt / (4 cs_y), P = 2^Q (0 once Q >= 32), OM = mask * P: the sign is row t's opacity.
             OM < 0 and the wall farther than the thing (D - Dt / 512 > 0) -> 2^29 onto the wall's
             V, which then misses every wall mux and the bounded flat select: nothing else shows.
      texel  Vs, a mux per word, a shift, and one palette decider per color copying that color
             times the thing's brightness on columns where it is in front of the wall.
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
from compiler.netlist import EACH, GREEN, RED, Collect, Scalar  # noqa: E402
from sprite import nearest, pack, quantize  # noqa: E402

ASSETS = ROOT / "assets" / "freedoom" / "sprites.json"
STEX = 64                  # sprite texels per tile (each texel 2x2 Doom units, like walls at tex 64)
HP = STEX // 2             # box rows: eye level down to the floor
F = 12                     # fraction bits of U (the box column)
QD = 10                    # Zt's depth: alpha >> QD (1/256 tile)
DIV_SIGMA = round((1 << 20) / 676)   # sigma = alpha * PLANE / 2^20
NEAR = 1 << 16             # alpha of 1/4 tile: nearer (or behind) things aren't drawn
KILL = 1 << 29             # added to a killed pixel's V
HIDE = -(1 << 23)          # added to a hidden thing's depth (column_stage hide)
FRAME_BITS = 6
COLORS = 15                # thing texels 1..15, 0 = transparent

# Things on the map: (x, y) in tiles and the sprite frame. Our placement, Freedoom's art.
# The game (touch.py): monsters are solid and hurt up close, items are picked up. An imp near the
# start, armor, ammo and a medikit along the south wall (game.py's scripted walk meets them all).
# The barrel and column gave way to items, 2026-11-02: 8 things fit.
THINGS = [
    (5.5, 4.5, "TROOA1"),
    (10.5, 7.5, "POSSA1"),
    (10.1, 10.9, "TROOA1"),
    (5.2, 12.7, "POSSA1"),
    (5.6, 13.5, "AMMOA0"),
    (4.45, 14.65, "MEDIA0"),
    (7.4, 13.35, "ARM1A0"),
    (13.5, 13.5, "SBOXA0"),
]


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


def column_stage(g, w, vec, camx, look, tabs, pos0, things, sprites, t_v, shade, hide=None, frame=None):
    """The per-thing scalars, the per-column key/depth vectors and the lookup blocks. Returns a dict
    of nodes for the row stage (all timed against t_v, the wall V's time), and each thing's depth
    and lateral offset ("alpha", "lat": Scalars) for the aim (combat.py).

    frame(g, i, fid): a Scalar added to thing i's frame id (fid: name -> id), or None for its
    THINGS frame throughout (a monster's pain and death frames, combat.py).

    hide(g, i, out): a Scalar adding HIDE (or 0) to thing i's depth signal `out`, or None. A hidden
    thing's depth is behind the camera, so neither leaf passes and it isn't drawn (a picked-up
    item). |alpha| < 2^23 on a 16-tile map, so alpha - 2^23 stays negative, and its Zt base
    (alpha >> qd) << KB stays above -2^31 with qd = QD for KB <= 17; wider boxes (a dying imp's)
    take one bit of Zt's depth per extra key bit (qd = QD + KB - 17)."""
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
    zleaves, dleaves, alphas, lats, rows = [], [], [], [], []
    for i, (tx, ty, name) in enumerate(things):
        ex, ey = int(tx * 256), int(ty * 256)
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
        sigma = g.ss(alpha, "/", DIV_SIGMA, f"sigma{i}")
        dc = g.ss(alpha, ">>", qd, f"dc{i}")
        U = [g.vs(vec(camx, "camx"), "*", sigma, f"u{i}"), g.vs(vec([-1] * w, "-1"), "*", lat, f"lat{i}")]
        ZB = g.vs(vec([1 << KB] * w, "2^KB"), "*", dc, f"zb{i}")
        DB = g.vs(vec([1 << FRAME_BITS] * w, "64"), "*", alpha, f"db{i}")
        texw = types[name[:4]]
        C = (texw // 2 + 1) << F
        rng = [sem.cond(EACH, ">=", -(texw // 2) << F, RED), sem.cond(EACH, "<", (texw // 2) << F, RED, "and")]
        zleaves.append(g.decider(rng + [sem.cond(EACH, ">=", (NEAR >> qd) << KB, GREEN, "and")],
                                 [sem.out(EACH, constant=C), sem.out(EACH, networks=RED), sem.out(EACH, networks=GREEN)],
                                 red=U, green=ZB, label=f"zt{i}"))
        fo = frame(g, i, fid) if frame else None
        DF = [DB, g.broadcast(vec([1] * w, "ones"), fo, f"frame{i}")] if fo else DB
        dleaves.append(g.decider(rng + [sem.cond(EACH, ">=", NEAR << FRAME_BITS, GREEN, "and")],
                                 [sem.out(EACH, constant=fid[name]), sem.out(EACH, networks=GREEN)],
                                 red=U, green=DF, label=f"dt{i}"))
        rows.append([rx.node, ry.node] + parts[:2] + lat.node.nodes + ([hid.node] if hid is not None else [])
                    + [sigma.node, dc.node] + U + [ZB, DB] + (DF[1:] if fo else []) + [zleaves[-1], dleaves[-1]])
    tz, td = [], []
    Zt, Dt = tree(g, zleaves, "zmin", tz), tree(g, dleaves, "dmin", td)
    at = t_v - 4
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
    BRS = g.vv(vec([shade.bright] * w, "bright"), "/", [SD, vec([shade.fog] * w, "fog")], "thing brightness")
    return {"Zt": Zt, "Dt": Dt, "SK": SK, "FR": FR, "NSD": NSD, "BRS": BRS, "masks": masks, "words": words,
            "wsum": wsum, "lookups": masks + [n for ws in words for n in ws], "KB": KB, "alpha": alphas, "lat": lats,
            "rows": rows, "trees": (tz, td), "tail": [Zt, Dt, SK, FR, NSD, SD], "tables": [DX, DY, NDY]}


# ---------------------------------------------------------------------------------------------
# Float reference

def ideal(img, vals, things, sprites, wall_d, w, h, shade, proj=None):
    """Draw the things into img (float math): per column the nearest covering thing, per lower row
    its texel where opaque and nearer than the wall (wall_d[c]: perpendicular distance, tiles)."""
    from raycast import DS, PLANE  # noqa: E402
    px, py = vals["PX"] / 256, vals["PY"] / 256
    dx, dy = vals["DX"] / 1024, vals["DY"] / 1024
    fr = {f["name"]: f for f in sprites["frames"]}
    for c in range(w):
        camx = ((2 * c + 1 - w) * 1024 // w) / 1024
        best = None
        for tx, ty, name in things:
            rx, ry = tx - px, ty - py
            a = rx * dx + ry * dy
            if a < NEAR / (1 << 18):
                continue
            lat = ry * dx - rx * dy
            texw = sprites["types"][name[:4]]
            u = math.floor((a * camx * PLANE / 1024 - lat) * STEX + texw / 2)
            if 0 <= u < texw and (best is None or a < best[0]):
                best = (a, u, fr[name])
        if best is None or best[0] >= wall_d[c]:
            continue
        a, u, f = best
        br = int(shade.bright / (a * DS + shade.fog))
        for y in range(first_row(h), h):
            t = math.floor((2 * y + 1 - h) * a / (2 * (proj or h)) * STEX)
            if 0 <= t < HP and f["texels"][t][u]:
                img[y][c] = color(sprites["colors"][f["texels"][t][u] - 1], br)
