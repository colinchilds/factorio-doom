# Freedoom sprites

`sprites.json` holds walk frames A–D (front view, rotation 1) of two Freedoom monsters, `TROO`
and `POSS`, as palette indexes (-1 = transparent) with their offsets, plus PLAYPAL palette 0.

- **Source:** Freedoom 0.13.0, `freedoom2.wad`, from the official release
  <https://github.com/freedoom/freedoom/releases/tag/v0.13.0>. The zip matched the SHA-256 in
  the release's signed `freedoom-0.13.0-CHECKSUM`
  (`3f9b264f3e3ce503b4fb7f6bdcb1f419d93c7b546f4df3e874dd878db9688f59`).
- **License:** BSD 3-clause, see `COPYING.txt` (it must stay next to anything derived from these).
  Freedoom is original art made as a free replacement for Doom's; none of it is id Software's.
- **Regenerate or add sprites:**
  `python3 tools/wad.py freedoom2.wad --export assets/freedoom/sprites.json TROOA1 TROOB1 ...`
  (`--list PREFIX` shows what's in the WAD, `--png DIR NAME...` dumps previews).
