# Ready-made blueprints

Blueprint strings for the game with default settings (the arena map, music on), ready to paste.
Each needs Factorio 2.1 with Space Age.

| File | Screen | Entities | ms/tick |
|---|---|---|---|
| [doom_64x40.txt](doom_64x40.txt) | 64×40 | 12,677 | 0.44 |
| [doom_160x100.txt](doom_160x100.txt) | 160×100 | 27,763 | 0.61 |
| [doom_320x200.txt](doom_320x200.txt) | 320×200 (Doom's resolution) | 79,242 | 1.12 |

Times are the game's headless update time on an Apple Silicon Mac; a tick has 16.7 ms.

1. Open the file and copy its whole contents (the 320×200 string is about 8 MB).
2. In a Space Age game (the map editor, `/editor`, is easiest), import it from the blueprint
   library and place it.
3. Connect one power source to any of its substations.
4. Play from the input pad right of the screen; see the [README](../README.md#quick-start) for the
   controls, or install the optional [keyboard mod](../README.md#keyboard-mod-optional).

For another map, size or option, generate your own with `python3 doom/game.py --help`.
