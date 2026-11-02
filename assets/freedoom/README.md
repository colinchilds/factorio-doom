# Freedoom sprites and textures

`sprites.json` holds sprites as palette indexes (-1 = transparent) with their offsets, plus
PLAYPAL palette 0: two Freedoom monsters, `TROO` and `POSS`, with their front-view walk (A–D),
attack and pain frames and their death frames (rotation 0), and the items and decorations
`BAR1A0`, `BAR1B0`, `MEDIA0`, `STIMA0`, `CLIPA0`, `ARM1A0` and `COLUA0`.

`textures.json` holds six wall textures composed from their patches (TEXTURE1 + PNAMES):
STARTAN3, BRICK7, MARBLE1, SP_HOT1, STONE2 and WOOD1, as palette indexes, plus the same palette.
Its `flats` holds six 64×64 floor/ceiling flats (rows of palette indexes): FLOOR4_8, CEIL3_5,
FLOOR5_1, FLOOR7_1, TLITE6_4 and FLAT5_4.

- **Source:** Freedoom 0.13.0, `freedoom2.wad`, from the official release
  <https://github.com/freedoom/freedoom/releases/tag/v0.13.0>. The zip matched the SHA-256 in
  the release's signed `freedoom-0.13.0-CHECKSUM`
  (`3f9b264f3e3ce503b4fb7f6bdcb1f419d93c7b546f4df3e874dd878db9688f59`).
- **License:** BSD 3-clause, see `COPYING.txt` (it must stay next to anything derived from these).
  Freedoom is original art made as a free replacement for Doom's; none of it is id Software's.
- **Regenerate or add sprites:**
  `python3 tools/wad.py freedoom2.wad --export assets/freedoom/sprites.json TROOA1 TROOB1 ...`
  (it rewrites the file, so list every sprite to keep)
  (`--list PREFIX` shows what's in the WAD, `--png DIR NAME...` dumps previews).
- **Regenerate or add textures:**
  `python3 tools/wad.py freedoom2.wad --export-textures assets/freedoom/textures.json STARTAN3 ...`
  (`--textures PREFIX` lists them, `--texture-png DIR NAME...` dumps previews).
- **Regenerate or add flats:**
  `python3 tools/wad.py freedoom2.wad --export-flats assets/freedoom/textures.json FLOOR4_8 ...`
  (`--flats PREFIX` lists them, `--flat-png DIR NAME...` dumps previews).
