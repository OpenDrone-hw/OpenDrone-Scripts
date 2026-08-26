#!/usr/bin/env python3
"""Headless JLCPCB fab export via the Fabrication Toolkit plugin.

Run with KiCad's bundled python (it imports pcbnew and wx):

    $KPY fab_export.py <board.kicad_pcb> [--name ARCHIVE_NAME]

Reads ARCHIVE_NAME and the other options from fabrication-toolkit-options.json
beside the board unless --name overrides it. Output lands in
<board dir>/production/ exactly as the GUI plugin writes it.

The plugin's own cli.py cannot be used directly: its package directory name
contains hyphens (not importable) and it calls pcbnew.GetBoard(), which is
None outside the editor. Both are worked around here: the package is copied
to a legal module name in a cache dir, and GetBoard is pointed at the loaded
board.
"""
import argparse, json, os, shutil, sys, tempfile

PLUGIN_DIR = os.path.expanduser(
    "~/Documents/KiCad/10.0/3rdparty/plugins/com_github_bennymeg_JLC-Plugin-for-KiCad")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--name")
    a = ap.parse_args()
    board_path = os.path.abspath(a.board)
    bdir = os.path.dirname(board_path)

    opts_path = os.path.join(bdir, "fabrication-toolkit-options.json")
    stored = json.load(open(opts_path)) if os.path.exists(opts_path) else {}

    cache = os.path.join(tempfile.gettempdir(), "jlc_plugin_headless")
    legal = os.path.join(cache, "jlc_plugin")
    src_files = {f for f in os.listdir(PLUGIN_DIR)
                 if os.path.isfile(os.path.join(PLUGIN_DIR, f))}
    have = {f for f in os.listdir(legal)} if os.path.isdir(legal) else set()
    if not src_files <= have:
        shutil.rmtree(legal, ignore_errors=True)
        os.makedirs(cache, exist_ok=True)
        shutil.copytree(PLUGIN_DIR, legal)
    sys.path.insert(0, cache)

    import wx
    wx.App(False)
    import pcbnew
    from jlc_plugin.thread import ProcessThread
    import jlc_plugin.options as O

    board = pcbnew.LoadBoard(board_path)
    pcbnew.GetBoard = lambda *args, **kw: board
    opts = {
        O.ARCHIVE_NAME: a.name or stored.get("ARCHIVE_NAME", ""),
        O.EXTRA_LAYERS: stored.get("EXTRA_LAYERS", ""),
        O.ALL_ACTIVE_LAYERS_OPT: stored.get("ALL_ACTIVE_LAYERS", False),
        O.EXTEND_EDGE_CUT_OPT: stored.get("EXTEND_EDGE_CUT", False),
        O.ALTERNATIVE_EDGE_CUT_OPT: stored.get("ALTERNATIVE_EDGE_CUT", False),
        O.AUTO_TRANSLATE_OPT: stored.get("AUTO TRANSLATE", True),
        O.AUTO_FILL_OPT: stored.get("AUTO FILL", True),
        O.EXCLUDE_DNP_OPT: stored.get("EXCLUDE DNP", False),
        O.OPEN_BROWSER_OPT: False,
        O.NO_BACKUP_OPT: True,
    }
    t = ProcessThread(wx=None, cli=board_path, nonInteractive=True,
                      openBrowser=False, options=opts)
    t.join()
    name = opts[O.ARCHIVE_NAME]
    out = os.path.join(bdir, "production", f"{name}.zip")
    if not os.path.exists(out):
        sys.exit(f"export did not produce {out}")
    print(f"exported {out}")


if __name__ == "__main__":
    main()
