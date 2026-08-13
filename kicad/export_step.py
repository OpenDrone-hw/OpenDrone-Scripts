#!/usr/bin/env python3
"""
export_step.py — standardized STEP exports for the OpenDrone boards.

kicad-cli's default STEP export writes only the board body and the component 3D
models: no copper, no pads, no silkscreen. Imported into Fusion that is a bare
green slab with parts on it. This script drives kicad-cli with a fixed flag set
so every OpenDrone STEP looks the same and lands in one predictable place, the
same in every repo:

    <repo root>/export/<ProductName>.step

Repo root, not next to the .kicad_pcb — board files live at different depths per
repo (OpenRX/OpenRX-Lite/, OpenFC-Lite/hardware/), so anchoring to the board
would scatter the output. One directory per repo, always at the top.

THE STANDARD EXPORT IS: board body + components + pads + silkscreen.
Each decision below was measured, not assumed:

  Soldermask is EXCLUDED. kicad-cli does NOT model mask apertures: exporting
  F.Mask + B.Mask for a whole board yields 3 ADVANCED_FACEs, i.e. two flat
  sheets with no openings cut in them. Including it therefore buries every pad
  under an unbroken green slab. It is also stamped with a 17% transparency
  factor (silkscreen gets 10%), which is what made earlier exports look like
  frosted glass. The board body is already opaque green, so leaving the mask
  out is both more correct and smaller.

  KNOWN LIMITATION from the same cause: graphics drawn on F.Mask/B.Mask (logos,
  lettering) are mask openings that expose bare copper on the real board. Since
  kicad-cli emits no apertures, that copper cannot be shown by any combination
  of flags. Rendering it would mean synthesising the exposed-copper regions
  (mask graphics INTERSECT copper) as geometry, which this script does not yet do.

  Tracks and zones are EXCLUDED. Copper spans z 0.91-0.945 mm on a 1.0 mm
  board, under the mask on a real board, so it is invisible from outside.
  Including it roughly doubles the file for geometry nobody can see.

  Everything past Edge.Cuts is CUT (--clip, on by default). Any pad with copper
  outside the outline is REBUILT, not edited: intersect its copper with the
  outline, delete the pad, and add a fresh SMD pad carrying the result. Editing
  in place looks like it works and does not. A pad that is already
  PAD_SHAPE_CUSTOM, or whose padstack is front/inner/back, keeps its original
  primitives and stays full size, with no error raised. A rebuilt pad is
  uniform, unrotated, drill-free and empty, so none of those rules apply.
  Dropping the drill matters on its own: plated holes export as barrels at the
  drill's position and size regardless of pad shape, so a castellated hole on
  the outline used to leave a tube hanging in space.

  Verified across all 16 boards that have a closed outline: zero pads and zero
  holes with geometry past Edge.Cuts. Component 3D models are NOT clipped, that
  needs a CAD kernel which is not available here, so a connector body that
  genuinely overhangs will still overhang.

Any leftover transparency is zeroed in the written STEP so nothing renders
see-through, and product names are rewritten from the temp filename back to the
product name.

--preset exists only as an escape hatch for copper inspection work, and its
output does not belong in a repo:
  full   standard + tracks + zones (outer layers)
  inner  full + inner copper layers
  body   board body + components only (kicad-cli's bare default)

Clipping needs pcbnew, so run with KiCad's bundled Python, same as
render_board.py. Without it the export still runs, unclipped, with a warning:

  KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
  $KPY software/OpenDrone-Scripts/kicad/export_step.py --all

KiCad may stay open: every board edit happens on a temp copy.

Usage:
  export_step.py --all                       # every board discovered under hardware/
  export_step.py --all --repo OpenRX         # one repo
  export_step.py <board.kicad_pcb> -o out.step
  export_step.py --all --dry-run
"""
import argparse
import glob
import math
import os
import re
import shutil
import subprocess
import sys
import time

DEFAULT_KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
ROOT = os.path.expanduser("~/OpenDrone/hardware")

# --subst-models pulls the STEP model where a footprint ships both STEP and
# VRML; --no-dnp keeps unpopulated parts out so the model matches a shipped board.
COMMON = ["--subst-models", "--no-dnp"]

STANDARD = ["--include-pads", "--include-silkscreen"]

PRESETS = {
    "standard": STANDARD,
    "body": [],
    "full": STANDARD + ["--include-soldermask", "--include-tracks", "--include-zones"],
    "inner": STANDARD + ["--include-soldermask", "--include-tracks", "--include-zones",
                         "--include-inner-copper"],
}

MIN_OUTSIDE_MM2 = 0.001  # ignore rounding-level slivers of copper past the edge

# The clipped temp board has to be written beside the source (see
# clip_board_to_outline), so it lands inside a repo. The pid keeps concurrent
# runs from colliding on one filename, and discover() skips the prefix so a
# run in flight is never mistaken for a real board.
TEMP_PREFIX = ".export_step_tmp_"

# Boards are DISCOVERED, never listed. A new repo or a new variant is picked up
# with no edit to this file. Two rules do the whole job:
#
#   1. A board counts if a .kicad_pro of the same stem sits next to it. That is
#      what makes it a real KiCad project rather than a stray or a backup, and
#      it is also required for export: ${KIPRJMOD} only resolves when the
#      project file is there, and without it every project-relative 3D model
#      silently vanishes from the output.
#   2. These directories are skipped wherever they appear.
SKIP_DIRS = {".history", ".git", "backups", "archive", "libs", ".pio", "__pycache__",
             "node_modules", ".venv", "export"}


def product_name(repo, stem):
    """Name the STEP after the product, derived from repo + board stem.

    Board stems are not unique on their own (OpenFC-Lite and OpenFC-Lite-Mini
    both use OpenFC.kicad_pcb) and are sometimes internal (4in1). Where repo and
    stem overlap, the longer one is the real product name; where they do not,
    both are needed to stay unambiguous.
    """
    r, s = repo.lower(), stem.lower()
    if s in r or r in s:
        return repo if len(repo) >= len(stem) else stem
    return f"{repo}-{stem}"


def discover(root, only_repo=None):
    """Yield (repo, board_path, product_name) for every project board found."""
    found = []
    for repo in sorted(os.listdir(root)):
        repo_dir = os.path.join(root, repo)
        if not os.path.isdir(repo_dir) or repo.startswith("."):
            continue
        if only_repo and repo != only_repo:
            continue
        for dirpath, dirnames, filenames in os.walk(repo_dir):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".pretty")]
            for fn in sorted(filenames):
                if not fn.endswith(".kicad_pcb"):
                    continue
                stem = fn[: -len(".kicad_pcb")]
                if stem.startswith(TEMP_PREFIX):
                    continue
                if not os.path.exists(os.path.join(dirpath, stem + ".kicad_pro")):
                    continue
                found.append((repo, os.path.join(dirpath, fn), product_name(repo, stem)))
    return found


def find_kicad_cli(explicit):
    for c in (explicit, DEFAULT_KICAD_CLI, shutil.which("kicad-cli")):
        if c and os.path.exists(c):
            return c
    sys.exit("kicad-cli not found — pass --kicad-cli PATH")


def human(n):
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} kB"


def clip_board_to_outline(board_path, scratch):
    """Copy the board and remove copper that falls outside Edge.Cuts.

    Returns (path_to_use, clipped_count, deleted_count). The copy is what gets
    exported; board_path itself is only ever read.

    The copy has to live in the SOURCE directory, not a temp dir: footprints
    reference 3D models through ${KIPRJMOD}, which resolves to the project
    directory, and KiCad only sets it when a .kicad_pro of the same stem sits
    next to the board. Exporting from /tmp silently drops every project-relative
    model. Both temp files are registered with the caller for cleanup.
    """
    try:
        import pcbnew
    except ImportError:
        return board_path, None, None, None

    stem = os.path.splitext(os.path.basename(board_path))[0]
    srcdir = os.path.dirname(os.path.abspath(board_path))
    tmp_stem = os.path.join(srcdir, f"{TEMP_PREFIX}{os.getpid()}_{stem}")
    tmp = tmp_stem + ".kicad_pcb"
    # Register the STEM, not the files: saving a board also makes a .kicad_prl
    # (and sometimes a .kicad_pro) that we never asked for. Cleanup globs.
    scratch.append(tmp_stem)

    src_pro = os.path.join(srcdir, stem + ".kicad_pro")
    if os.path.exists(src_pro):
        shutil.copy(src_pro, tmp_stem + ".kicad_pro")

    shutil.copy(board_path, tmp)
    board = pcbnew.LoadBoard(tmp)

    outline = pcbnew.SHAPE_POLY_SET()
    if not board.GetBoardPolygonOutlines(outline, False) or outline.OutlineCount() == 0:
        # No closed Edge.Cuts to clip against. That is a board defect, not an
        # export problem, and it must not be reported as "pcbnew missing".
        return board_path, "no-outline", None, None

    L = pcbnew.UNDEFINED_LAYER  # padstack "all layers"
    clipped = deleted = notched = 0

    def outside_area(poly, region):
        t = pcbnew.SHAPE_POLY_SET(poly)
        t.BooleanSubtract(region)
        return sum(abs(t.Outline(i).Area()) for i in range(t.OutlineCount())) / 1e12

    def circle(cx, cy, dx, dy, segs=48):
        c = pcbnew.SHAPE_POLY_SET()
        c.NewOutline()
        rx, ry = dx / 2, (dy if dy > 0 else dx) / 2
        for k in range(segs):
            a = 2 * math.pi * k / segs
            c.Append(int(cx + rx * math.cos(a)), int(cy + ry * math.sin(a)))
        return c

    # PASS 1 - castellations. A drill that straddles Edge.Cuts is a castellation:
    # on the finished board the router cuts through it, leaving a plated notch in
    # the edge, which is exactly what the KiCad 3D viewer draws. Cut those holes
    # OUT OF the outline so the board body carries the notch, then drop the drill
    # so no floating barrel is exported. Deleting the drill without notching the
    # outline (an earlier version of this) loses the notch entirely.
    straddling = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            dx, dy = pad.GetDrillSizeX(), pad.GetDrillSizeY()
            if dx <= 0 and dy <= 0:
                continue
            pos = pad.GetPosition()
            hole = circle(pos.x, pos.y, dx, dy)
            if outside_area(hole, outline) > MIN_OUTSIDE_MM2:
                straddling.append((pad, hole))

    if straddling:
        for _, hole in straddling:
            outline.BooleanSubtract(hole)
        for pad, _ in straddling:
            pad.SetDrillSize(pcbnew.VECTOR2I(0, 0))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            notched += 1

        # Rewrite Edge.Cuts to the notched outline. Board-level and
        # footprint-level edge graphics both have to go, or the old straight
        # edge survives alongside the new one.
        for d in list(board.GetDrawings()):
            if d.GetLayer() == pcbnew.Edge_Cuts:
                board.Remove(d)
        for fp in board.GetFootprints():
            for g in list(fp.GraphicalItems()):
                if g.GetLayer() == pcbnew.Edge_Cuts:
                    fp.Remove(g)
        for i in range(outline.OutlineCount()):
            contours = [outline.Outline(i)]
            contours += [outline.Hole(i, h) for h in range(outline.HoleCount(i))]
            for contour in contours:
                one = pcbnew.SHAPE_POLY_SET()
                one.AddOutline(contour)
                shape = pcbnew.PCB_SHAPE(board)
                shape.SetShape(pcbnew.SHAPE_T_POLY)
                shape.SetLayer(pcbnew.Edge_Cuts)
                shape.SetPolyShape(one)
                shape.SetFilled(False)
                shape.SetWidth(pcbnew.FromMM(0.05))
                board.Add(shape)

    # PASS 2 - copper. Anything past the (now notched) outline is cut away.
    CU = (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.B_Cu)

    for fp in board.GetFootprints():
        for pad in list(fp.Pads()):
            if max(outside_area(pad.GetEffectivePolygon(l, pcbnew.ERROR_INSIDE), outline)
                   for l in CU) <= MIN_OUTSIDE_MM2:
                continue
            # Do NOT edit the pad in place. Reshaping a pad that is already
            # PAD_SHAPE_CUSTOM, or whose padstack is front/inner/back, SILENTLY
            # leaves the original primitives behind and the pad keeps its full
            # size. Rebuilding sidesteps every padstack rule: a fresh pad is
            # uniform, unrotated and empty.
            shape = pcbnew.SHAPE_POLY_SET()
            for l in CU:
                shape.BooleanAdd(pad.GetEffectivePolygon(l, pcbnew.ERROR_INSIDE))
            keep = pcbnew.SHAPE_POLY_SET(shape)
            keep.BooleanIntersection(outline)

            layerset, net = pad.GetLayerSet(), pad.GetNetCode()
            pos, number = pad.GetPosition(), pad.GetNumber()
            fp.Delete(pad)

            if keep.OutlineCount() == 0:
                deleted += 1
                continue

            # An outline with cutouts yields an intersection with holes, and a
            # pad primitive cannot carry holes. Fracture folds them into one.
            keep.Fracture()
            new_pad = pcbnew.PAD(fp)
            new_pad.SetNumber(number)
            new_pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            new_pad.SetLayerSet(layerset)
            new_pad.SetPosition(pos)
            new_pad.SetDrillSize(pcbnew.VECTOR2I(0, 0))
            new_pad.SetShape(L, pcbnew.PAD_SHAPE_CUSTOM)
            new_pad.SetAnchorPadShape(L, pcbnew.PAD_SHAPE_CIRCLE)
            new_pad.SetSize(L, pcbnew.VECTOR2I(10000, 10000))
            local = pcbnew.SHAPE_POLY_SET(keep)
            local.Move(pcbnew.VECTOR2I(-pos.x, -pos.y))
            for i in range(local.OutlineCount()):
                one = pcbnew.SHAPE_POLY_SET()
                one.AddOutline(local.Outline(i))
                new_pad.AddPrimitivePoly(L, one, 0, True)
            new_pad.SetNetCode(net)
            fp.Add(new_pad)
            clipped += 1

    board.Save(tmp)
    return tmp, clipped, deleted, notched


def post_process(step_path, temp_stem, product):
    """Zero transparency, and name the assembly after the product.

    kicad-cli names every STEP product after the input filename, so exporting
    from the clipped temp copy would ship products called
    '.export_step_tmp_OpenFC_pad'. Rename them back.
    """
    with open(step_path, encoding="utf8", errors="surrogateescape") as fh:
        text = fh.read()
    text = re.sub(r"SURFACE_STYLE_TRANSPARENT\([^)]*\)",
                  "SURFACE_STYLE_TRANSPARENT(0.)", text)
    if temp_stem:
        text = text.replace(temp_stem, product)
    with open(step_path, "w", encoding="utf8", errors="surrogateescape") as fh:
        fh.write(text)


def export(cli, board, out, preset, extra, dry_run, clip):
    cmd = [cli, "pcb", "export", "step", "-f", "-o", out]
    cmd += COMMON + PRESETS[preset] + extra + [board]
    if dry_run:
        print(" ".join(cmd))
        return True

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    t0 = time.time()
    scratch = []
    note = ""
    temp_stem = ""
    try:
        if clip:
            src, nclip, ndel, nhole = clip_board_to_outline(board, scratch)
            if scratch:
                temp_stem = os.path.basename(scratch[0])
            if nclip == "no-outline":
                note = "  WARNING: board has no closed Edge.Cuts outline, nothing clipped"
            elif nclip is None:
                note = "  (clip skipped: pcbnew unavailable — run with KiCad's Python)"
            elif nclip or ndel or nhole:
                note = (f"  (clipped {nclip} pad(s), removed {ndel} outside, "
                        f"notched {nhole} castellation(s) into the edge)")
            cmd[-1] = src
        result = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        for stem_path in scratch:
            for f in glob.glob(glob.escape(stem_path) + ".*"):
                os.remove(f)

    dt = time.time() - t0
    if result.returncode != 0 or not os.path.exists(out):
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        print(f"  FAIL rc={result.returncode}  {tail}")
        return False

    post_process(out, temp_stem, os.path.splitext(os.path.basename(out))[0])
    print(f"  {human(os.path.getsize(out)):>9}  {dt:5.1f}s  {out}{note}")

    # kicad-cli reports an unresolvable 3D model as a warning and still exits 0,
    # so a board with a broken model path exports "successfully" as a bare slab.
    # Surface it: the fix belongs in the footprint library, not here.
    missing = sorted({ln.split("File not found: ", 1)[1].strip()
                      for ln in (result.stderr + result.stdout).splitlines()
                      if "File not found: " in ln})
    if missing:
        print(f"  WARNING: {len(missing)} unresolved 3D model path(s) — components are missing:")
        for m in missing[:10]:
            print(f"    {m}")
        if len(missing) > 10:
            print(f"    ... and {len(missing) - 10} more")
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("board", nargs="?", help="path to a .kicad_pcb")
    p.add_argument("-o", "--output", help="output .step (single-board mode)")
    p.add_argument("--all", action="store_true", help="export every board in the manifest")
    p.add_argument("--repo", help="with --all, limit to one repo")
    p.add_argument("--preset", default="standard", choices=sorted(PRESETS),
                   help="escape hatch only; the repo standard is 'standard'")
    p.add_argument("--no-clip", dest="clip", action="store_false",
                   help="keep copper that hangs past Edge.Cuts")
    p.add_argument("--root", default=ROOT, help=f"hardware dir (default {ROOT})")
    p.add_argument("--kicad-cli", help="path to kicad-cli")
    p.add_argument("--dry-run", action="store_true", help="print commands only")
    p.add_argument("extra", nargs="*", help="extra kicad-cli flags")
    a = p.parse_args()

    cli = find_kicad_cli(a.kicad_cli)

    if a.all:
        jobs = [(board, os.path.join(a.root, repo, "export", name + ".step"))
                for repo, board, name in discover(a.root, a.repo)]
        if not jobs:
            sys.exit("no boards discovered")
    elif a.board:
        out = a.output or os.path.join(os.path.dirname(os.path.abspath(a.board)), "export",
                                       os.path.splitext(os.path.basename(a.board))[0] + ".step")
        jobs = [(a.board, out)]
    else:
        p.error("pass a board or --all")

    print(f"preset: {a.preset}   {len(jobs)} board(s)")
    ok = 0
    for board, out in jobs:
        print(os.path.relpath(board, a.root) if a.all else board)
        ok += export(cli, board, out, a.preset, a.extra, a.dry_run, a.clip)
    print(f"{ok}/{len(jobs)} exported")
    return 0 if ok == len(jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
