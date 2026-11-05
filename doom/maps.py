"""Maps: our own layouts, Freedoom's textures (BSD). Pure data, plus select(), which makes one the
current map for every module (raycast, things, monsters; game.py reads spawn and facing from it).

A map is 16 x 16 cells (raycast.K grid lines per axis; the flats' tile lookup keeps 4 bits per
axis). A cell is a wall (a key of `walls`: its texture) or open (a key of `flats`: its floor and
ceiling flats). Open cells must be closed in by walls.

  arena   the default: pillars and a central block, monsters 5 to 9 tiles from the spawn.
  test    the arena with two monsters a few tiles from the spawn: game.py's scripted walk and fight
          are tuned on it, so verify with --map test.
  acid    a start room, a wooden tunnel north into a hall over acid, and a brown walkway zigzagging
          across it to an exit corridor.
"""
import sys

ARENA = {
    "rows": """
#######BB#######
#..............#
#..MM......MM..#
#..MM......MM..#
#..............#
#......RR......#
B...B......B...B
B...B......B...B
B...B......B...B
#......RR......#
#..............#
#..MM......MM..#
#..MM......MM..#
#..............#
#..............#
#######BB#######
""".split(),
    "walls": {"#": "STARTAN3", "B": "BRICK7", "M": "MARBLE1", "R": "SP_HOT1"},
    "flats": {".": ("FLOOR4_8", "TLITE6_4")},
    # (x, y) in tiles and the sprite frame: monsters first, then items (things.py). The side
    # monsters stand behind the brick pillars.
    "things": [
        (5.5, 4.5, "TROOA1"),
        (10.5, 7.5, "POSSA1"),
        (13.5, 7.5, "TROOA1"),
        (2.5, 7.5, "POSSA1"),
        (5.6, 13.5, "AMMOA0"),
        (4.45, 14.65, "MEDIA0"),
        (7.4, 13.35, "ARM1A0"),
        (13.5, 13.5, "SBOXA0"),
    ],
    # Per monster the way it faces (monsters.py: it wakes on seeing the player in front of it).
    "facing": {0: (0, 1), 1: (0, -1), 2: (1, 0), 3: (-1, 0)},
    "spawn": (8.5, 12.5),   # tiles; times FP it must be even (game.camera_from)
    "a0": 192,              # the spawn's view angle: north (0 east, growing clockwise)
    "center": (8, 7.5),     # raycast.py's scripted camera circles this
}

# Walls: # STARTAN3 (start room), W WOOD1 (tunnel), G TEKGREN2 (north wall, exit
# corridor), N BROWNGRN (the hall's sides). Open: . grey hex floor (start room, tunnel), b the brown
# walkway, ~ acid; one dark ceiling over all.
ACID = {
    "rows": """
GGGGGGGGGGGGGGGG
GGGGGGGGGGGbbbbG
GGGGGGGGGGGbbGGG
N~~~~~~~~~~bb~~N
N~~~bbbbbbbbb~~N
N~~~bbbbbbbbb~~N
N~~~bb~~~~~~~~~N
N~~~bbbbb~~~~~~N
N~~~bbbbb~~~~~~N
N~~~~~~bb~~~~~~N
NNNNNNW..WNNNNNN
######W..W######
#..............#
#..............#
#..............#
################
""".split(),
    "walls": {"#": "STARTAN3", "W": "WOOD1", "G": "TEKGREN2", "N": "BROWNGRN"},
    "flats": {".": ("FLOOR4_8", "CEIL5_1"), "b": ("FLOOR5_2", "CEIL5_1"), "~": ("NUKAGE1", "CEIL5_1")},
    # The monsters wait where the start room and the tunnel can't see them: either side of the
    # tunnel's mouth (an ambush as you come out), the far walkway, and around the exit's corner.
    "things": [
        (2.0, 8.5, "POSSA1"),
        (14.0, 8.5, "TROOA1"),
        (13.5, 1.5, "TROOA1"),
        (2.0, 4.0, "POSSA1"),
        (3.5, 13.0, "AMMOA0"),
        (12.5, 13.0, "SBOXA0"),
        (8.0, 8.0, "MEDIA0"),
        (2.5, 12.5, "ARM1A0"),
    ],
    "facing": {0: (1, 0), 1: (-1, 0), 2: (-1, 0), 3: (1, 0)},   # toward the walkway
    "spawn": (8.0, 13.5),
    "a0": 192,
    "center": (8, 7),
}

TEST = dict(ARENA, things=[(5.5, 4.5, "TROOA1"), (10.5, 7.5, "POSSA1"), (10.1, 10.9, "TROOA1"), (5.2, 12.7, "POSSA1")]
            + ARENA["things"][4:])

MAPS = {"arena": ARENA, "test": TEST, "acid": ACID}
DEFAULT = "arena"
current = ARENA


def select(name):
    """Make map `name` current in raycast, things and monsters (game.py reads spawn, a0)."""
    global current
    import monsters
    import raycast  # noqa: F401
    import things
    m = current = MAPS[name]
    rows = m["rows"]
    assert len(rows) == 16 and all(len(r) == 16 for r in rows), "maps are 16 x 16"
    assert all(c in m["walls"] or c in m["flats"] for r in rows for c in r)
    # raycast.py may also be running as __main__: patch every loaded copy.
    rcs = {id(m_): m_ for m_ in list(sys.modules.values())
           if getattr(m_, "__file__", None) and m_.__file__.endswith("raycast.py")}
    for rc in rcs.values():
        rc.MAP, rc.TEXTURE_OF, rc.FLATS = rows, m["walls"], m["flats"]
        rc.NEG = 4 * len(rows)
        rc.CENTER = (m["center"][0] * rc.FP, m["center"][1] * rc.FP)
    things.THINGS = list(m["things"])
    monsters.FACING = dict(m["facing"])
    return m


def suffix():
    """'' for the default map, else '_' + its name (generated file names)."""
    name = next(k for k, v in MAPS.items() if v is current)
    return "" if name == DEFAULT else "_" + name


def map_arg(ap):
    ap.add_argument("--map", default=DEFAULT, choices=sorted(MAPS), help="the map (maps.py)")
