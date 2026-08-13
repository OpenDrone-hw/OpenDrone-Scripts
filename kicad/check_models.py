#!/usr/bin/env python3
"""
check_models.py — verify every board's 3D models before an export or a fab release.

kicad-cli treats an unresolvable 3D model as a warning and still exits 0, so a
board that has lost half its components exports "successfully" as a bare slab.
This turns that into a hard failure, and it runs BEFORE the export rather than
reporting it afterwards.

Five checks per footprint, per board:

  E1  library nickname resolves in neither the project nor the global
      fp-lib-table, so the footprint cannot be loaded at all
  E2  nickname resolves but the footprint is not in that library any more
  E3  a referenced model file does not exist on disk
  E4  the library footprint has models but the board instance has none
  E5  board and library disagree about the model list (path, offset, scale,
      rotation or visibility)

E5 is the one that matters for propagation: those are exactly the footprints a
library-only fix cannot reach, because a .kicad_pcb embeds its own copy of every
footprint including the model paths.

Only the FIRST applicable error is reported per footprint, so a footprint that is
missing from its library (E2) is not also counted as drift (E5). That makes the
E5 column smaller than a full drift audit would show, and more actionable: fix
E1/E2 first, then re-run and the remaining drift appears.

Needs pcbnew, so run with KiCad's bundled Python:

  KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
  $KPY check_models.py --all
  $KPY check_models.py <board.kicad_pcb> -v

Exits non-zero if anything is found. Read-only: no board or library is written.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from export_step import ROOT, discover  # noqa: E402

GLOBAL_FP_TABLE = os.path.expanduser("~/Library/Preferences/kicad/10.0/fp-lib-table")


def parse_fp_lib_table(path):
    """Map library nickname -> uri, from an fp-lib-table s-expression."""
    if not os.path.exists(path):
        return {}
    text = open(path, encoding="utf8", errors="replace").read()
    table = {}
    for entry in re.findall(r"\(lib\s+(.*?)\)\s*(?=\(lib|\s*\)\s*$)", text, re.S):
        name = re.search(r'\(name\s+"?([^")\s]+)"?\)', entry)
        uri = re.search(r'\(uri\s+"?([^")]+)"?\)', entry)
        if name and uri:
            table[name.group(1)] = uri.group(1).strip()
    return table


def expand(path, project_dir):
    """Resolve KiCad path variables in a library uri or model filename."""
    kicad_3d = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels"
    kicad_fp = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"
    for var, value in (("KIPRJMOD", project_dir),
                       ("KICAD10_3DMODEL_DIR", kicad_3d),
                       ("KICAD9_3DMODEL_DIR", kicad_3d),
                       ("KICAD10_FOOTPRINT_DIR", kicad_fp),
                       ("KICAD9_FOOTPRINT_DIR", kicad_fp)):
        path = path.replace("${%s}" % var, value).replace("$(%s)" % var, value)
    return os.path.normpath(os.path.expanduser(path))


def model_tuple(model):
    """Comparable identity of one 3D model entry, rounded so float noise is ignored."""
    r = lambda v: round(v, 4)  # noqa: E731
    return (os.path.basename(model.m_Filename).lower(),
            (r(model.m_Offset.x), r(model.m_Offset.y), r(model.m_Offset.z)),
            (r(model.m_Scale.x), r(model.m_Scale.y), r(model.m_Scale.z)),
            (r(model.m_Rotation.x), r(model.m_Rotation.y), r(model.m_Rotation.z)),
            bool(model.m_Show))


def check_board(board_path, pcbnew, verbose=False):
    project_dir = os.path.dirname(os.path.abspath(board_path))
    libs = dict(parse_fp_lib_table(GLOBAL_FP_TABLE))
    libs.update(parse_fp_lib_table(os.path.join(project_dir, "fp-lib-table")))

    board = pcbnew.LoadBoard(board_path)
    counts = {k: 0 for k in ("E1", "E2", "E3", "E4", "E5")}
    detail = []

    for fp in board.GetFootprints():
        fpid = fp.GetFPIDAsString()
        nick, _, name = fpid.partition(":")
        ref = fp.GetReference()

        board_models = list(fp.Models())
        for model in board_models:
            resolved = expand(model.m_Filename, project_dir)
            stem = os.path.splitext(resolved)[0]
            if not any(os.path.exists(stem + ext)
                       for ext in (".step", ".stp", ".wrl", ".wrz", ".STEP", ".Step", "")):
                counts["E3"] += 1
                detail.append(f"E3 {ref:8s} model not on disk: {model.m_Filename}")

        uri = libs.get(nick)
        if uri is None:
            counts["E1"] += 1
            detail.append(f"E1 {ref:8s} library nickname not in any fp-lib-table: {nick}")
            continue

        try:
            lib_fp = pcbnew.FootprintLoad(expand(uri, project_dir), name)
        except Exception:
            lib_fp = None
        if lib_fp is None:
            counts["E2"] += 1
            detail.append(f"E2 {ref:8s} footprint missing from library: {fpid}")
            continue

        lib_models = list(lib_fp.Models())
        if lib_models and not board_models:
            counts["E4"] += 1
            detail.append(f"E4 {ref:8s} board instance has no model, library has {len(lib_models)}")
            continue

        if [model_tuple(m) for m in board_models] != [model_tuple(m) for m in lib_models]:
            counts["E5"] += 1
            if verbose:
                detail.append(f"E5 {ref:8s} {fpid}")
                for m in board_models:
                    detail.append(f"      board: {m.m_Filename}")
                for m in lib_models:
                    detail.append(f"      lib:   {m.m_Filename}")
            else:
                detail.append(f"E5 {ref:8s} board/library model mismatch: {fpid}")

    return counts, detail


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("board", nargs="?", help="path to a .kicad_pcb")
    p.add_argument("--all", action="store_true", help="check every discovered board")
    p.add_argument("--repo", help="with --all, limit to one repo")
    p.add_argument("--root", default=ROOT)
    p.add_argument("-v", "--verbose", action="store_true", help="list every finding")
    a = p.parse_args()

    try:
        import pcbnew
    except ImportError:
        sys.exit("needs pcbnew: run with KiCad's bundled Python")

    if a.all:
        jobs = [(n, b) for _, b, n in discover(a.root, a.repo)]
    elif a.board:
        jobs = [(os.path.basename(a.board), a.board)]
    else:
        p.error("pass a board or --all")

    total = {k: 0 for k in ("E1", "E2", "E3", "E4", "E5")}
    failing = 0
    print(f"{'BOARD':<34} {'E1':>4} {'E2':>4} {'E3':>4} {'E4':>4} {'E5':>4}")
    print("-" * 60)
    for name, board_path in jobs:
        counts, detail = check_board(board_path, pcbnew, a.verbose)
        for k in total:
            total[k] += counts[k]
        bad = sum(counts.values())
        failing += bool(bad)
        print(f"{name:<34} {counts['E1']:>4} {counts['E2']:>4} {counts['E3']:>4} "
              f"{counts['E4']:>4} {counts['E5']:>4}" + ("" if bad else "   clean"))
        if a.verbose:
            for line in detail:
                print("    " + line)
    print("-" * 60)
    print(f"{'TOTAL':<34} {total['E1']:>4} {total['E2']:>4} {total['E3']:>4} "
          f"{total['E4']:>4} {total['E5']:>4}   {failing}/{len(jobs)} boards failing")
    print("\nE1 nickname unresolvable · E2 footprint gone from library · "
          "E3 model file missing\nE4 board lost a model · E5 board/library drift "
          "(a library-only fix cannot reach these)")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
