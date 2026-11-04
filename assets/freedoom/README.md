# Freedoom sprites and textures

`sprites.json` holds sprites as palette indexes (-1 = transparent) with their offsets, plus
PLAYPAL palette 0: two Freedoom monsters, `TROO` and `POSS`, with their front-view walk (A–D),
attack and pain frames and their death frames (rotation 0), and the items and decorations
`BAR1A0`, `BAR1B0`, `MEDIA0`, `STIMA0`, `CLIPA0`, `ARM1A0` and `COLUA0`, and the pickups
`SHELA0`, `SBOXA0`, `AMMOA0`, `ROCKA0`, `CELLA0` and `ARM2A0`.

`textures.json` holds six wall textures composed from their patches (TEXTURE1 + PNAMES):
STARTAN3, BRICK7, MARBLE1, SP_HOT1, STONE2 and WOOD1, as palette indexes, plus the same palette.
Its `flats` holds six 64×64 floor/ceiling flats (rows of palette indexes): FLOOR4_8, CEIL3_5,
FLOOR5_1, FLOOR7_1, TLITE6_4 and FLAT5_4.

`hud.json` holds the status bar lumps in the same format: STBAR, STARMS, STTPRCNT, the big
digits STTNUM0–9, the small digits STYSNUM0–9 and STGNUM0–9, and every face: STFST00–42 (pain
level, then straight 0–2), STFTR00–40 and STFTL00–40 (turned), STFOUCH0–4, STFEVL0–4, STFKILL0–4,
STFGOD0 and STFDEAD0.

`weapons.json` holds the player weapon sprites (Doom's psprites) in the same format: PUNGA0–D0,
PISGA0–C0 and PISFA0, SHTGA0–D0 and SHTFA0–B0, CHGGA0–B0 and CHGFA0–B0, MISGA0–B0 and MISFA0–D0,
PLSGA0–B0 and PLSFA0, BFGGA0–B0 and BFGFA0–B0.

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
- **Regenerate the status bar art:**
  `python3 tools/wad.py freedoom2.wad --export assets/freedoom/hud.json STBAR STARMS STTPRCNT STTNUM0 ...`
  (every lump listed above).
- **Regenerate the weapons:**
  `python3 tools/wad.py freedoom2.wad --export assets/freedoom/weapons.json PUNGA0 PUNGB0 ...`
  (every lump listed above).
- **Regenerate or add textures:**
  `python3 tools/wad.py freedoom2.wad --export-textures assets/freedoom/textures.json STARTAN3 ...`
  (`--textures PREFIX` lists them, `--texture-png DIR NAME...` dumps previews).
- **Regenerate or add flats:**
  `python3 tools/wad.py freedoom2.wad --export-flats assets/freedoom/textures.json FLOOR4_8 ...`
  (`--flats PREFIX` lists them, `--flat-png DIR NAME...` dumps previews).
