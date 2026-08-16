#!/usr/bin/env python3
"""Headless 'Update' of the multiboard plugin: pull the schematic into every
sub-board (or the named ones) without opening KiCad. Same code path as the
plugin's Update button, so the GUI and this script cannot disagree.

  KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
  $KPY kicad/multiboard/update.py path/to/hardware              # all boards
  $KPY kicad/multiboard/update.py path/to/hardware OpenAIO-Core  # one board

Ownership: a footprint belongs to the board it is already on. A symbol that is
on no board yet goes to the board you name (one-board call, same as the Update
button in KiCad). In an all-boards call the boards are only refreshed and the
leftovers go to `default_board` from .kicad_multiboard.json, so the order of
the boards never decides where a new part lands.

Needs KiCad's Python (pcbnew) and the boards closed in KiCad (the plugin
refuses to write a board that has a lock file).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from opendrone_multiboard.manager import MultiBoardManager  # noqa: E402


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    m = MultiBoardManager(Path(sys.argv[1]).resolve())
    names = sys.argv[2:]
    if names:
        plan = [(n, True) for n in names]
    else:
        default = m.config.default_board
        others = [n for n in m.config.boards if n != default]
        plan = [(n, False) for n in others] + ([(default, True)] if default in m.config.boards else [])
        if not plan:
            sys.exit(f"no boards in {m.config_path}")
        if default not in m.config.boards:
            print("note: no default_board in the config, unplaced parts stay unplaced")
    rc = 0
    for name, claim in plan:
        ok, msg = m.update_board(name, progress_callback=lambda p, s: None, claim_new=claim)
        print(f"{name}: {'ok' if ok else 'FAILED'}  {msg.replace(chr(10), ', ')}")
        rc |= 0 if ok else 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
