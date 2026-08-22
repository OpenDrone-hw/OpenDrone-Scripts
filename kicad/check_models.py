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

Exits non-zero if anything is found, or with --blocking-only, only when E3/E4 is
found. That distinction matters because E1, E2 and E5 do not affect the exported
STEP at all: a .kicad_pcb carries its own copy of every footprint, models
included, and that copy is what kicad-cli exports. Gating a release build on the
full set would block on cosmetic library drift.

Read-only: no board or library is written.
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


FIELDS = ("file", "offset", "scale", "rotation", "visible")

# --- E6: what --subst-models actually swaps in -------------------------------
# export_step.py passes --subst-models because KiCad's STEP exporter cannot read
# VRML: without it every .wrl-referenced part is silently DROPPED (OpenRX-Lite
# goes 9.1 MB -> 3.1 MB). The flag makes kicad-cli use a same-named .step
# instead. That is only safe while the two files are the same geometry. Where
# they are not, the 3D viewer stays right (it renders the .wrl) and the STEP
# export is wrong, which is exactly the failure that is hard to catch by eye.
WRL_POINTS = re.compile(r"point\s*\[(.*?)\]", re.S)
# a VRML coordinate can carry a negative exponent (-9.9999994e-05); a
# character class without the "-" truncates it and float() then raises
_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
TRIPLE = re.compile(rf"({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})")
STEP_POINT = re.compile(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*"
                        r"([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)")
SUBST_TOL_MM = 0.3     # below this is tessellation and rounding, not a different part
_bbox_cache = {}


def _bbox(points, scale=1.0):
    if not points:
        return None
    cols = list(zip(*points))
    return tuple((max(c) - min(c)) * scale for c in cols)


def wrl_bbox(path):
    """Envelope of a VRML model in mm. KiCad authors .wrl in 0.1 inch units."""
    try:
        text = open(path, errors="replace").read()
    except OSError:
        return None
    pts = []
    for block in WRL_POINTS.finditer(text):
        pts += [tuple(float(v) for v in t.groups()) for t in TRIPLE.finditer(block.group(1))]
    return _bbox(pts, 2.54)


def step_bbox(path):
    """Envelope of a STEP model in mm. Entities may wrap lines, so read whole."""
    try:
        text = open(path, errors="replace").read().replace("\r", "").replace("\n", "")
    except OSError:
        return None
    return _bbox([tuple(float(v) for v in m.groups()) for m in STEP_POINT.finditer(text)])


def substitution_gap(model_path):
    """mm difference between a .wrl and the .step --subst-models would use.

    Returns (gap, step_path, wrl_size, step_size), or None when the model is not
    a VRML reference or has no STEP sibling to be substituted by.
    """
    if not model_path.lower().endswith((".wrl", ".wrz")):
        return None
    stem = os.path.splitext(model_path)[0]
    step = next((stem + e for e in (".step", ".stp", ".STEP", ".Step")
                 if os.path.exists(stem + e)), None)
    if not step or not os.path.exists(model_path):
        return None
    if stem not in _bbox_cache:
        _bbox_cache[stem] = (wrl_bbox(model_path), step_bbox(step))
    wb, sb = _bbox_cache[stem]
    if not wb or not sb:
        return None
    return (max(abs(s - w) for s, w in zip(sb, wb)), step, wb, sb)


def drift_fields(bt, lt):
    """Which parts of the model entry differ, so E5 says what actually moved.

    Reporting only the filename (what this used to do) makes E5 unreadable: the
    common case on these boards is a board instance whose filename matches the
    library exactly and whose OFFSET does not, so both lines printed identically
    and the finding looked like a bug in the checker.
    """
    if len(bt) != len(lt):
        return ["model count"]
    seen = []
    for b, l in zip(bt, lt):
        for i, field in enumerate(FIELDS):
            if b[i] != l[i] and field not in seen:
                seen.append(field)
    return seen


def describe_drift(bt, lt):
    """Board-vs-library detail lines for one footprint, differing entries only."""
    out = []
    for i in range(max(len(bt), len(lt))):
        b = bt[i] if i < len(bt) else None
        l = lt[i] if i < len(lt) else None
        if b == l:
            continue
        for label, v in (("board", b), ("lib  ", l)):
            if v is None:
                out.append(f"      {label}: (no model at this index)")
                continue
            out.append(f"      {label}: {v[0]}  off={v[1]} scale={v[2]} "
                       f"rot={v[3]} show={v[4]}")
    return out


def check_board(board_path, pcbnew, verbose=False):
    project_dir = os.path.dirname(os.path.abspath(board_path))
    libs = dict(parse_fp_lib_table(GLOBAL_FP_TABLE))
    libs.update(parse_fp_lib_table(os.path.join(project_dir, "fp-lib-table")))

    board = pcbnew.LoadBoard(board_path)
    counts = {k: 0 for k in ("E1", "E2", "E3", "E4", "E5", "E6")}
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
                continue

            subst = substitution_gap(resolved)
            if subst and subst[0] > SUBST_TOL_MM:
                gap, step_path, wb, sb = subst
                counts["E6"] += 1
                detail.append(
                    f"E6 {ref:8s} substituted STEP is {gap:.2f} mm off the VRML: "
                    f"{os.path.basename(step_path)}")
                detail.append(f"      viewer (.wrl): {wb[0]:6.2f} x {wb[1]:5.2f} x {wb[2]:5.2f} mm")
                detail.append(f"      export (.step):{sb[0]:6.2f} x {sb[1]:5.2f} x {sb[2]:5.2f} mm")

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

        bt = [model_tuple(m) for m in board_models]
        lt = [model_tuple(m) for m in lib_models]
        if bt != lt:
            counts["E5"] += 1
            if verbose:
                detail.append(f"E5 {ref:8s} {fpid}")
                detail += describe_drift(bt, lt)
            else:
                detail.append(f"E5 {ref:8s} board/library model mismatch: {fpid} "
                              f"({', '.join(drift_fields(bt, lt)) or 'model count'})")

    return counts, detail


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("board", nargs="?", help="path to a .kicad_pcb")
    p.add_argument("--all", action="store_true", help="check every discovered board")
    p.add_argument("--repo", help="with --all, limit to these repos (comma separated)")
    p.add_argument("--products", action="store_true",
                   help="with --all, skip fab panels and bench fixtures "
                        "(matches export_step.py --products)")
    p.add_argument("--root", default=ROOT)
    p.add_argument("-v", "--verbose", action="store_true", help="list every finding")
    p.add_argument("--blocking-only", action="store_true",
                   help="exit non-zero only for E3/E4, the errors that actually "
                        "empty the export; use this to gate a release build")
    a = p.parse_args()

    try:
        import pcbnew
    except ImportError:
        sys.exit("needs pcbnew: run with KiCad's bundled Python")

    if a.all:
        jobs = [(n, b) for _, b, n in discover(a.root, a.repo, a.products)]
    elif a.board:
        jobs = [(os.path.basename(a.board), a.board)]
    else:
        p.error("pass a board or --all")

    codes = ("E1", "E2", "E3", "E4", "E5", "E6")
    total = {k: 0 for k in codes}
    failing = 0
    blocking = 0
    print(f"{'BOARD':<34}" + "".join(f"{c:>4}" for c in codes))
    print("-" * 64)
    for name, board_path in jobs:
        counts, detail = check_board(board_path, pcbnew, a.verbose)
        for k in total:
            total[k] += counts[k]
        bad = sum(counts.values())
        failing += bool(bad)
        blocks = counts["E3"] + counts["E4"] + counts["E6"]
        blocking += bool(blocks)
        note = "   clean" if not bad else ("   BLOCKS EXPORT" if blocks else "")
        print(f"{name:<34}" + "".join(f"{counts[c]:>4}" for c in codes) + note)
        if a.verbose:
            for line in detail:
                print("    " + line)
    print("-" * 64)
    print(f"{'TOTAL':<34}" + "".join(f"{total[c]:>4}" for c in codes) +
          f"   {failing}/{len(jobs)} with findings, {blocking}/{len(jobs)} blocking")
    print("\nE1 nickname unresolvable · E2 footprint gone from library · "
          "E3 model file missing\nE4 board lost a model · E5 board/library drift "
          "(a library-only fix cannot reach these)\nE6 the .step that "
          "--subst-models swaps in is not the same shape as the .wrl")
    print("\nE3, E4 and E6 are blocking. E3/E4 leave a component out; E6 ships a "
          "component\nat the wrong size or place, and the 3D viewer will NOT show "
          "it because the\nviewer renders the .wrl while the STEP export renders "
          "the substitute.\nE1, E2 and E5 do not affect the export: the board "
          "carries its own footprint\ncopies, and those are what kicad-cli exports.")
    if a.blocking_only:
        return 1 if blocking else 0
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
