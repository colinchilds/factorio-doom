"""Read sprites out of a Doom-format WAD (stdlib only). Used with Freedoom's BSD-licensed IWADs.

  python3 tools/wad.py FREEDOOM2.WAD --list TROO          # sprite lumps with a prefix
  python3 tools/wad.py FREEDOOM2.WAD --png out/ TROOA1 ... # dump lumps as PNGs (palette 0)
  python3 tools/wad.py FREEDOOM2.WAD --export assets/freedoom/sprites.json TROOA1 ...
      # palette-indexed JSON (-1 = transparent) plus PLAYPAL 0; what the generators read
  python3 tools/wad.py FREEDOOM2.WAD --textures [PREFIX]           # wall textures (TEXTURE1/2)
  python3 tools/wad.py FREEDOOM2.WAD --texture-png out/ BROWN1 ... # composed textures as PNGs
  python3 tools/wad.py FREEDOOM2.WAD --export-textures assets/freedoom/textures.json BROWN1 ...

As a module: Wad(path).sprite("TROOA1") -> (width, height, leftoffset, topoffset, rows), where
rows[y][x] is a palette index or None (transparent). Wad.palette is PLAYPAL 0 as RGB tuples.
"""
import json
import pathlib
import struct
import sys
import zlib


class Wad:
    def __init__(self, path):
        self.data = pathlib.Path(path).read_bytes()
        ident, n, off = struct.unpack_from("<4sii", self.data, 0)
        if ident not in (b"IWAD", b"PWAD"):
            raise ValueError(f"{path}: not a WAD")
        self.lumps = []
        for i in range(n):
            pos, size, name = struct.unpack_from("<ii8s", self.data, off + 16 * i)
            self.lumps.append((name.rstrip(b"\0").decode("ascii"), pos, size))
        pal = self.lump("PLAYPAL")
        self.palette = [tuple(pal[3 * i:3 * i + 3]) for i in range(256)]

    def lump(self, name):
        for n, pos, size in reversed(self.lumps):  # later lumps override earlier ones
            if n == name:
                return self.data[pos:pos + size]
        raise KeyError(name)

    def sprite_names(self):
        names = [n for n, _, _ in self.lumps]
        start = max(i for i, n in enumerate(names) if n in ("S_START", "SS_START"))
        end = names.index("S_END", start)
        return names[start + 1:end]

    def texture_defs(self):
        """TEXTURE1/TEXTURE2 (Doom format): {name: (width, height, [(x, y, patch name), ...])}."""
        pd = self.lump("PNAMES")
        (n,) = struct.unpack_from("<i", pd, 0)
        pnames = [pd[4 + 8 * i:12 + 8 * i].rstrip(b"\0").decode("ascii").upper() for i in range(n)]
        out = {}
        for lump in ("TEXTURE1", "TEXTURE2"):
            try:
                d = self.lump(lump)
            except KeyError:
                continue
            (count,) = struct.unpack_from("<i", d, 0)
            for i in range(count):
                (off,) = struct.unpack_from("<i", d, 4 + 4 * i)
                name = d[off:off + 8].rstrip(b"\0").decode("ascii").upper()
                w, h = struct.unpack_from("<hh", d, off + 12)
                (np_,) = struct.unpack_from("<h", d, off + 20)
                patches = []
                for p in range(np_):
                    x, y, idx = struct.unpack_from("<hhh", d, off + 22 + 10 * p)
                    patches.append((x, y, pnames[idx]))
                out[name] = (w, h, patches)
        return out

    def texture(self, name):
        """A wall texture composed from its patches: (width, height, rows), rows[y][x] a palette
        index or None where no patch covers it."""
        w, h, patches = self.texture_defs()[name]
        rows = [[None] * w for _ in range(h)]
        for px, py, pname in patches:
            pw, ph, _, _, prow = self.sprite(pname)
            for y in range(ph):
                for x in range(pw):
                    i = prow[y][x]
                    if i is not None and 0 <= px + x < w and 0 <= py + y < h:
                        rows[py + y][px + x] = i
        return w, h, rows

    def sprite(self, name):
        """Doom picture format: header, column offsets, then per column a list of posts."""
        d = self.lump(name)
        w, h, left, top = struct.unpack_from("<hhhh", d, 0)
        rows = [[None] * w for _ in range(h)]
        for x in range(w):
            (p,) = struct.unpack_from("<I", d, 8 + 4 * x)
            while d[p] != 0xFF:
                y0, length = d[p], d[p + 1]
                for k in range(length):
                    if y0 + k < h:
                        rows[y0 + k][x] = d[p + 3 + k]
                p += length + 4
        return w, h, left, top, rows


def write_png(path, rgba_rows):
    """rgba_rows: list of rows of (r, g, b, a)."""
    h, w = len(rgba_rows), len(rgba_rows[0])
    raw = b"".join(b"\0" + bytes(c for px in row for c in px) for row in rgba_rows)
    chunk = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    pathlib.Path(path).write_bytes(png)


def main():
    wad = Wad(sys.argv[1])
    if sys.argv[2] == "--list":
        for n in wad.sprite_names():
            if n.startswith(sys.argv[3]):
                w, h, l, t, _ = wad.sprite(n)
                print(f"{n:9} {w}x{h} offset {l},{t}")
    elif sys.argv[2] == "--textures":
        for n, (w, h, p) in sorted(wad.texture_defs().items()):
            if n.startswith(sys.argv[3] if len(sys.argv) > 3 else ""):
                print(f"{n:9} {w}x{h} {len(p)} patches")
    elif sys.argv[2] == "--texture-png":
        out = pathlib.Path(sys.argv[3])
        out.mkdir(parents=True, exist_ok=True)
        for n in sys.argv[4:]:
            w, h, rows = wad.texture(n)
            write_png(out / f"{n}.png", [[(0, 0, 0, 255) if i is None else (*wad.palette[i], 255) for i in r] for r in rows])
    elif sys.argv[2] == "--export-textures":
        path = pathlib.Path(sys.argv[3])
        out = json.loads(path.read_text()) if path.exists() else {"palette": [list(c) for c in wad.palette]}
        out.setdefault("textures", {})
        for n in sys.argv[4:]:
            w, h, rows = wad.texture(n)
            out["textures"][n] = {"width": w, "height": h, "rows": [[-1 if i is None else i for i in r] for r in rows]}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    elif sys.argv[2] == "--png":
        out = pathlib.Path(sys.argv[3])
        out.mkdir(parents=True, exist_ok=True)
        for n in sys.argv[4:]:
            w, h, _, _, rows = wad.sprite(n)
            write_png(out / f"{n}.png", [[(0, 0, 0, 0) if i is None else (*wad.palette[i], 255) for i in r] for r in rows])
    elif sys.argv[2] == "--export":
        out = {"palette": [list(c) for c in wad.palette], "sprites": {}}
        for n in sys.argv[4:]:
            w, h, left, top, rows = wad.sprite(n)
            out["sprites"][n] = {"width": w, "height": h, "left": left, "top": top,
                                 "rows": [[-1 if i is None else i for i in r] for r in rows]}
        path = pathlib.Path(sys.argv[3])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
