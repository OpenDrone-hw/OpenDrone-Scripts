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

THE STANDARD EXPORT IS: board body + components + pads + silkscreen, plus
copper that a mask opening leaves bare. Each decision below is measured:

  Soldermask is EXCLUDED. kicad-cli models no mask apertures: exporting F.Mask +
  B.Mask for a whole board adds only 2-3 ADVANCED_FACEs over a body-only export,
  i.e. flat sheets with no openings cut. Including it buries every pad under an
  unbroken slab. It also carries a 17% transparency factor (silkscreen 10%),
  which made earlier exports look like frosted glass.

  Mask GRAPHICS are recovered instead. Logos and lettering drawn on F.Mask or
  B.Mask are openings that expose bare copper on the real board. Since kicad-cli
  emits no apertures, the exposed regions are synthesised: a mask layer rendered
  to polygons IS the openings, a copper layer rendered to polygons is the copper,
  and the intersection is the bare metal. See add_mask_exposed_copper.

  Tracks and zones are EXCLUDED, for FILE SIZE, not visibility. Including them
  measures 1.53x on OpenRX-Lite and 1.79x on OpenFC-Lite. Do not repeat the older
  claim that they are hidden under the mask: the mask is excluded here, so that
  copper would sit about 35 um proud of the board and would be plainly visible,
  exactly as the pads are.

  Everything past Edge.Cuts is CUT (--clip, on by default), in two passes:
    1. A drill that straddles the outline is a castellation. The hole is
       subtracted FROM the outline so the board body carries the notch, as the
       KiCad 3D viewer draws it, and only then is the drill zeroed so no barrel
       is left floating. Zeroing the drill without notching loses the notch;
       setting the pad to SMD as well collapses it to one copper layer and loses
       all the back-side copper.
    2. Any pad with copper outside is REBUILT, not reshaped: intersect with the
       outline, delete, and add fresh SMD pads, one per copper side. Reshaping in
       place fails silently when the pad is already PAD_SHAPE_CUSTOM or its
       padstack is front/inner/back.

  Scope of the cut, honestly: gross overhang is gone on every board (OpenRX-Lite
  went from 648 pad points up to 2.25 mm outside, to none). The 30x30 panel still
  retains about 120 pad points up to 4.6 um outside, from MIN_OUTSIDE_MM2 and
  polygonisation error. Silkscreen is NOT clipped and can overhang. Component 3D
  models are NOT clipped: that needs a CAD kernel, which is not available here.

Transparency is zeroed in the written STEP, the export timestamp is normalised,
and product names are rewritten to the product. The file is still not
byte-reproducible: kicad-cli emits STYLED_ITEM records in a nondeterministic
order, and rebuilt pads carry random UUIDs that perturb coordinates by ~0.3 um.

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

These files are RELEASE ASSETS, not tracked source: `export/` is gitignored in
every board repo and the set is attached to the repo's rev release with
`gh release upload`. --products drops fab panels and bench fixtures, which
export fine but are not something anyone fits into their own design.

Usage:
  export_step.py --all                       # every board discovered under hardware/
  export_step.py --all --repo OpenRX         # one repo
  export_step.py --all --products            # publishable boards only
  export_step.py <board.kicad_pcb> -o out.step
  export_step.py --all --dry-run
"""
import argparse
import collections
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time

# One definition of what a named STEP colour means, shared with the tool that
# writes the library models. wrl_to_step imports OCP inside its functions
# only, so this costs nothing under KiCad's Python, which has no OCP.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wrl_to_step import expand_predefined_colours  # noqa: E402

DEFAULT_KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
ROOT = os.path.expanduser("~/OpenDrone/hardware")

# --subst-models pulls the STEP model where a footprint ships both STEP and
# VRML; --no-dnp keeps unpopulated parts out so the model matches a shipped board.
# --fill-all-vias: do NOT cut via holes in the conductor layers. A via-in-pad
# array otherwise punches a grid of circles through every pad, which is what the
# Onshape import kept showing. render_board.py has always stripped vias for the
# README images, so the 2D renders never revealed it. Measured on OpenESC-20x20:
# 16.16 MB -> 14.47 MB, and byte-identical to deleting all 1050 vias with pcbnew,
# so the flag alone is enough and the board is never mutated for it.
COMMON = ["--subst-models", "--no-dnp", "--fill-all-vias"]

STANDARD = ["--include-pads", "--include-silkscreen"]

PRESETS = {
    "standard": STANDARD,
    "body": [],
    # Outline only: board body, no components, no copper. One solid, one
    # product, a few hundred kB. This is the Onshape placement set -- Onshape
    # only has to answer "where does this board sit", and a single solid with
    # real cylindrical mounting holes is the easiest thing to mate against.
    "outline": ["--board-only"],
    "full": STANDARD + ["--include-soldermask", "--include-tracks", "--include-zones"],
    "inner": STANDARD + ["--include-soldermask", "--include-tracks", "--include-zones",
                         "--include-inner-copper"],
}

STRIP_FP_SILK = True
GOLD_PADS = True

# ENIG gold, the same value export-boards.mjs paints the web GLB pads, so the
# STEP and the GLB agree. kicad-cli writes exposed copper as a neutral 0.735
# grey, which reads as bare tin next to the green mask.
GOLD_RGB = (0.90, 0.72, 0.36)

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
#      Every dot directory is skipped, not just the ones named here. A
#      `.history_trim/` left beside a board held a stale copy of it, complete
#      with its .kicad_pro, so discovery found two boards of the same name and
#      the stale one, exported second, overwrote the real OpenFC-Lite-Mini with
#      a 5.7 MB file missing 18 components.
SKIP_DIRS = {"backups", "archive", "libs", "__pycache__", "node_modules",
             "export", "templates"}

# A discovered board is not automatically a PRODUCT. Fab panels and bench
# fixtures are real projects that export fine, but nobody fits one into their
# own design, so they have no place in a published fit-check set. Product
# identity is not derivable from the files, so it is spelled out here, in the
# one shared place, rather than as a marker file in every board repo.
#
#   -panel      step-and-repeat fab panel, not a shipped board
#   -all        multi-variant fab panel (OpenRX-all)
#   -QC         bench QC fixture
#   -Flashing   bench flashing jig
#   -MotorTest  bench motor test rig
NON_PRODUCT_SUFFIXES = ("-panel", "-all", "-qc", "-flashing", "-motortest")


def is_product(stem):
    return not stem.lower().endswith(NON_PRODUCT_SUFFIXES)


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


KICAD_SHARE = "/Applications/KiCad/KiCad.app/Contents/SharedSupport"
KICAD_COMMON = os.path.expanduser("~/Library/Preferences/kicad/10.0/kicad_common.json")


def kicad_path_vars(project_dir=None):
    """Every path variable KiCad itself would substitute, one dict.

    Not just the built-in ones. `OPENDRONE_LIB` is defined in KiCad's own
    settings on this machine and in some repos' .kicad_pro instead, and a
    tool that only knows the built-ins reads a board full of
    ${OPENDRONE_LIB} paths as a board with no 3D models at all. That is
    what had check_models reporting OpenAIO as missing 10 models and 20
    footprints while kicad-cli, which does read those settings, exported
    it complete.

    Precedence follows KiCad: the project's own text_variables win over
    the global settings, and the process environment is the last word.
    """
    out = {"KICAD10_3DMODEL_DIR": f"{KICAD_SHARE}/3dmodels",
           "KICAD9_3DMODEL_DIR": f"{KICAD_SHARE}/3dmodels",
           "KICAD8_3DMODEL_DIR": f"{KICAD_SHARE}/3dmodels",
           "KISYS3DMOD": f"{KICAD_SHARE}/3dmodels",
           "KICAD10_FOOTPRINT_DIR": f"{KICAD_SHARE}/footprints",
           "KICAD9_FOOTPRINT_DIR": f"{KICAD_SHARE}/footprints"}
    try:
        with open(KICAD_COMMON, encoding="utf8") as fh:
            out.update(json.load(fh).get("environment", {}).get("vars", {}) or {})
    except (OSError, ValueError):
        pass
    if project_dir:
        out["KIPRJMOD"] = project_dir
        for pro in sorted(glob.glob(os.path.join(project_dir, "*.kicad_pro"))):
            try:
                with open(pro, encoding="utf8") as fh:
                    out.update(json.load(fh).get("text_variables", {}) or {})
            except (OSError, ValueError):
                pass
            break
        out["KIPRJMOD"] = project_dir
    for k in list(out):
        if k in os.environ:
            out[k] = os.environ[k]
    return out


def expand_path(path, project_dir=None, variables=None):
    """Resolve ${VAR} and $(VAR) in a library uri or model filename."""
    text = str(path)
    variables = variables if variables is not None else kicad_path_vars(project_dir)
    for _ in range(4):     # a variable may expand into another one
        before = text
        for var, value in variables.items():
            text = text.replace("${%s}" % var, value).replace("$(%s)" % var, value)
        if text == before:
            break
    return os.path.normpath(os.path.expanduser(text))


def discover(root, only_repo=None, products_only=False):
    """Yield (repo, board_path, product_name) for every project board found."""
    found = []
    wanted = {r.strip() for r in only_repo.split(",")} if only_repo else None
    for repo in sorted(os.listdir(root)):
        repo_dir = os.path.join(root, repo)
        if not os.path.isdir(repo_dir) or repo.startswith("."):
            continue
        if wanted and repo not in wanted:
            continue
        for dirpath, dirnames, filenames in os.walk(repo_dir):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                           and not d.startswith(".") and not d.endswith(".pretty")]
            for fn in sorted(filenames):
                if not fn.endswith(".kicad_pcb"):
                    continue
                stem = fn[: -len(".kicad_pcb")]
                if stem.startswith(TEMP_PREFIX):
                    continue
                if not os.path.exists(os.path.join(dirpath, stem + ".kicad_pro")):
                    continue
                if products_only and not is_product(stem):
                    continue
                found.append((repo, os.path.join(dirpath, fn), product_name(repo, stem)))
    return found


def find_kicad_cli(explicit):
    """Locate kicad-cli. Validates an explicit path instead of trusting it, and
    derives the path from the running interpreter so a non-default KiCad install
    works. The two earlier copies each got one half of this right."""
    if explicit:
        if os.path.exists(explicit):
            return explicit
        sys.exit(f"--kicad-cli path does not exist: {explicit}")
    here = sys.executable
    if "KiCad.app" in here:
        base = here.split("KiCad.app")[0] + "KiCad.app/Contents/MacOS/kicad-cli"
        if os.path.exists(base):
            return base
    for c in (DEFAULT_KICAD_CLI, shutil.which("kicad-cli")):
        if c and os.path.exists(c):
            return c
    sys.exit("kicad-cli not found - pass --kicad-cli PATH")


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
        return board_path, None, None, None, None

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
        return board_path, "no-outline", None, None, None, 0

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
            # Zero the drill only. Setting PAD_ATTRIB_SMD here also collapses the
            # pad's layer set to a single copper layer, and pass 2 then rebuilds
            # only that side, silently losing all the back-side castellation
            # copper. The drill size alone is what suppresses the barrel.
            pad.SetDrillSize(pcbnew.VECTOR2I(0, 0))
            notched += 1

        # Rewrite Edge.Cuts to the notched outline. The old edge graphics, both
        # board-level and inside footprints, have to stop defining the outline or
        # the original straight edge survives alongside the notched one.
        #
        # They are MOVED to Cmts.User, not removed. board.Remove() hands
        # ownership to Python without taking it, which corrupts the SWIG proxies:
        # every later GetFootprints() then yields raw SwigPyObject and the next
        # attribute access dies with "'SwigPyObject' object is not iterable".
        # That crashed the two boards with the most edge graphics, OpenESC-30x30
        # 4in1 (141 items) and OpenRX-all (54), and took the rest of the --all
        # batch down with them. Cmts.User is not in any export preset, so the
        # moved graphics are inert.
        for d in list(board.GetDrawings()):
            if d.GetLayer() == pcbnew.Edge_Cuts:
                d.SetLayer(pcbnew.Cmts_User)
        for fp in board.GetFootprints():
            for g in list(fp.GraphicalItems()):
                if g.GetLayer() == pcbnew.Edge_Cuts:
                    g.SetLayer(pcbnew.Cmts_User)
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

            # An SMD pad is normalised to ONE copper layer, so a rebuilt
            # through-hole pad that lived on F.Cu and B.Cu silently loses a side.
            # Emit one pad per side instead. Copy the layer set first: it is a
            # reference into the pad and fp.Delete frees it.
            layerset = pcbnew.LSET(pad.GetLayerSet())
            sides = [l for l in (pcbnew.F_Cu, pcbnew.B_Cu) if layerset.Contains(l)]
            noncopper = [l for l in layerset.Seq()
                         if l not in (pcbnew.F_Cu, pcbnew.B_Cu) and not pcbnew.IsCopperLayer(l)]
            net = pad.GetNetCode()
            pos, number = pcbnew.VECTOR2I(pad.GetPosition()), str(pad.GetNumber())
            fp.Delete(pad)

            if keep.OutlineCount() == 0:
                deleted += 1
                continue

            # An outline with cutouts yields an intersection with holes, and a
            # pad primitive cannot carry holes. Fracture folds them into one.
            keep.Fracture()
            local = pcbnew.SHAPE_POLY_SET(keep)
            local.Move(pcbnew.VECTOR2I(-pos.x, -pos.y))

            for side in sides or [pcbnew.F_Cu]:
                new_pad = pcbnew.PAD(fp)
                new_pad.SetNumber(number)
                new_pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                ls = pcbnew.LSET()
                ls.AddLayer(side)
                for l in noncopper:
                    ls.AddLayer(l)
                new_pad.SetLayerSet(ls)
                new_pad.SetPosition(pos)
                new_pad.SetDrillSize(pcbnew.VECTOR2I(0, 0))
                new_pad.SetShape(L, pcbnew.PAD_SHAPE_CUSTOM)
                new_pad.SetAnchorPadShape(L, pcbnew.PAD_SHAPE_CIRCLE)
                new_pad.SetSize(L, pcbnew.VECTOR2I(10000, 10000))
                for i in range(local.OutlineCount()):
                    one = pcbnew.SHAPE_POLY_SET()
                    one.AddOutline(local.Outline(i))
                    new_pad.AddPrimitivePoly(L, one, 0, True)
                new_pad.SetNetCode(net)
                fp.Add(new_pad)
            clipped += 1

    exposed = add_mask_exposed_copper(board, outline, pcbnew)

    # Footprint-level silkscreen (component outlines, polarity ticks) is clutter
    # in a 3D export: it is mostly hidden under the part that owns it, and what
    # a reader looks for is the BOARD legend, which lives in board drawings and
    # is untouched here. RemoveNative, not Remove: the SWIG binding hands back
    # copies, so Remove silently does nothing and the strip reports success
    # while changing nothing.
    silk = 0
    if STRIP_FP_SILK:
        for fp in board.GetFootprints():
            for it in list(fp.GraphicalItems()):
                if it.GetLayer() in (pcbnew.F_SilkS, pcbnew.B_SilkS):
                    fp.RemoveNative(it)
                    silk += 1

    # Save returns False on failure rather than raising. Ignoring it would export
    # the UNCLIPPED board while printing clipped-pad counts.
    if not board.Save(tmp):
        sys.exit(f"pcbnew could not write {tmp}")
    return tmp, clipped, deleted, notched, exposed, silk


def add_mask_exposed_copper(board, outline, pcbnew):
    """Draw copper that a soldermask opening leaves bare, as gold pads.

    Graphics on F.Mask/B.Mask (logos, lettering) are mask openings: on the real
    board they expose bare copper, which is how the RX in "OpenRX" reads. There
    is no flag for this. kicad-cli models no mask apertures at all, so including
    the mask exports two unbroken sheets and shows nothing.

    So synthesise it. A mask layer rendered to polygons IS the set of openings,
    and a copper layer rendered to polygons is all the copper, so the
    intersection of the two is exactly the bare metal. Pad areas are subtracted
    because pads are already exported. What remains is added as flat SMD pads,
    which the STEP export colours like any other exposed copper.
    """
    made = 0
    holder = None
    for cu, mask in ((pcbnew.F_Cu, pcbnew.F_Mask), (pcbnew.B_Cu, pcbnew.B_Mask)):
        openings = pcbnew.SHAPE_POLY_SET()
        copper = pcbnew.SHAPE_POLY_SET()
        board.ConvertBrdLayerToPolygonalContours(mask, openings)
        board.ConvertBrdLayerToPolygonalContours(cu, copper)
        if openings.OutlineCount() == 0 or copper.OutlineCount() == 0:
            continue

        bare = pcbnew.SHAPE_POLY_SET(openings)
        bare.BooleanIntersection(copper)
        bare.BooleanIntersection(outline)
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if pad.IsOnLayer(cu):
                    bare.BooleanSubtract(pad.GetEffectivePolygon(cu, pcbnew.ERROR_INSIDE))
        if bare.OutlineCount() == 0:
            continue
        bare.Fracture()

        if holder is None:
            holder = pcbnew.FOOTPRINT(board)
            holder.SetReference("MASKART")
            holder.Reference().SetVisible(False)
            board.Add(holder)

        layers = pcbnew.LSET()
        layers.AddLayer(cu)
        for i in range(bare.OutlineCount()):
            contour = bare.Outline(i)
            if abs(contour.Area()) / 1e12 < 0.002:
                continue  # tessellation slivers, not artwork
            one = pcbnew.SHAPE_POLY_SET()
            one.AddOutline(contour)
            centre = one.BBox().Centre()
            one.Move(pcbnew.VECTOR2I(-centre.x, -centre.y))

            pad = pcbnew.PAD(holder)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(layers)
            pad.SetPosition(centre)
            pad.SetDrillSize(pcbnew.VECTOR2I(0, 0))
            pad.SetShape(pcbnew.UNDEFINED_LAYER, pcbnew.PAD_SHAPE_CUSTOM)
            pad.SetAnchorPadShape(pcbnew.UNDEFINED_LAYER, pcbnew.PAD_SHAPE_CIRCLE)
            pad.SetSize(pcbnew.UNDEFINED_LAYER, pcbnew.VECTOR2I(10000, 10000))
            pad.AddPrimitivePoly(pcbnew.UNDEFINED_LAYER, one, 0, True)
            holder.Add(pad)
            made += 1
    return made


def parse_entities(text):
    """{id: (type, body)} for every STEP entity, and the flattened text."""
    flat = re.sub(r"\s*\n\s*", " ", text)
    return {m.group(1): (m.group(2), m.group(3)) for m in
            re.finditer(r"#(\d+)\s*=\s*([A-Z_0-9]+)\s*\((.*?)\)\s*;", flat)}


def mark_outer_bounds(text):
    """Name the outer loop of every face that has more than one bound.

    kicad-cli writes both the outer loop and the holes as FACE_BOUND and
    never FACE_OUTER_BOUND, which is legal but leaves the importer to work
    out which is which. Onshape does not: it fills them, so every letter
    with a closed counter (the O of OPEN, the 8 of a rev number, the ring
    a stroke-font glyph makes) comes in as a solid blob, and the same
    happens to any component face with a hole in it.

    The outer loop is the one whose points enclose the others, taken here
    as the largest bounding box: a hole is inside its face by definition,
    so it cannot be the biggest. Only multi-bound faces are touched, and
    only the entity keyword changes.
    """
    ent = parse_entities(text)
    point = {}
    for i, (t, b) in ent.items():
        if t == "CARTESIAN_POINT":
            v = re.findall(r"([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)", b)
            if v:
                point[i] = tuple(float(x) for x in v[0])

    def loop_extent(bound_id, depth=0):
        """Bounding-box diagonal of the loop under a FACE_BOUND."""
        seen, stack, pts = set(), list(re.findall(r"#(\d+)", ent[bound_id][1])), []
        while stack:
            j = stack.pop()
            if j in seen or len(seen) > 4000:
                continue
            seen.add(j)
            if j in point:
                pts.append(point[j])
                continue
            if j in ent:
                stack.extend(re.findall(r"#(\d+)", ent[j][1]))
        if not pts:
            return 0.0
        return sum((max(p[k] for p in pts) - min(p[k] for p in pts)) ** 2
                   for k in range(3)) ** 0.5

    rename = set()
    for i, (t, b) in ent.items():
        if t != "ADVANCED_FACE":
            continue
        bounds = [j for j in re.findall(r"#(\d+)", b)
                  if ent.get(j, ("", ""))[0] in ("FACE_BOUND", "FACE_OUTER_BOUND")]
        if len(bounds) < 2:
            continue
        outer = max(bounds, key=loop_extent)
        if ent[outer][0] == "FACE_BOUND":
            rename.add(outer)
    if not rename:
        return text, 0

    def sub(m):
        return (f"#{m.group(1)} = FACE_OUTER_BOUND" if m.group(1) in rename
                else m.group(0))
    out = re.sub(r"#(\d+)\s*=\s*FACE_BOUND", sub, text)
    return out, len(rename)


def recolour_pads_gold(text):
    """Repaint exposed copper from kicad-cli's neutral grey to ENIG gold.

    The pad colour is found, not assumed: STYLED_ITEMs are grouped by the
    COLOUR_RGB they resolve to, and the pad colour is the neutral grey whose
    styled items are all MANIFOLD_SOLID_BREPs. That is exactly the pad solids
    (758 of them on OpenESC-20x20) and never a component, whose models are
    styled per ADVANCED_FACE. Matching on the literal 0.735 would break the
    first time KiCad changes its default copper colour.
    """
    flat = re.sub(r"\s*\n\s*", " ", text)
    ent = {m.group(1): (m.group(2), m.group(3)) for m in
           re.finditer(r"#(\d+)\s*=\s*([A-Z_0-9]+)\s*\((.*?)\)\s*;", flat)}
    colour = {}
    for i, (t, b) in ent.items():
        if t != "COLOUR_RGB":
            continue
        v = re.findall(r"'[^']*'\s*,\s*([\d.eE+-]+)\s*,\s*([\d.eE+-]+)\s*,\s*([\d.eE+-]+)", b)
        if v:
            colour[i] = tuple(float(x) for x in v[0])

    def resolve(i, depth=0):
        if depth > 8 or i not in ent:
            return None
        if ent[i][0] == "COLOUR_RGB":
            return i
        for j in re.findall(r"#(\d+)", ent[i][1]):
            r = resolve(j, depth + 1)
            if r:
                return r
        return None

    solids = collections.Counter()
    faces = collections.Counter()
    for i, (t, b) in ent.items():
        if t != "STYLED_ITEM":
            continue
        cid = resolve(i)
        if not cid:
            continue
        ids = re.findall(r"#(\d+)", b)
        tt = ent.get(ids[-1], ("?", ""))[0] if ids else "?"
        (solids if tt == "MANIFOLD_SOLID_BREP" else faces)[cid] += 1

    best, best_n = None, 0
    for cid, n in solids.items():
        r, g, b_ = colour.get(cid, (0, 0, 0))
        neutral = max(r, g, b_) - min(r, g, b_) < 0.02
        if neutral and not faces[cid] and n > best_n:
            best, best_n = cid, n
    if not best:
        return text

    def repaint(m):
        if m.group(1) != best:
            return m.group(0)
        return (f"#{best} = COLOUR_RGB ( '{m.group(2)}', "
                f"{GOLD_RGB[0]:.10f}, {GOLD_RGB[1]:.10f}, {GOLD_RGB[2]:.10f} )")

    out, n = re.subn(r"#(\d+)\s*=\s*COLOUR_RGB\s*\(\s*'([^']*)'\s*,"
                     r"\s*[\d.eE+-]+\s*,\s*[\d.eE+-]+\s*,\s*[\d.eE+-]+\s*\)",
                     repaint, text)
    if n:
        print(f"  pads       {best_n} exposed-copper solid(s) repainted ENIG gold")
    return out


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
    # A named colour is a colour Onshape ignores, and KiCad's own library
    # models use one for every black IC body. Numbers instead.
    text, n_named = expand_predefined_colours(text)
    if n_named:
        print(f"  colours    {n_named} named colour(s) written out as RGB")
    text, n_outer = mark_outer_bounds(text)
    if n_outer:
        print(f"  faces      {n_outer} outer loop(s) named, so holes stay holes")
    if GOLD_PADS:
        text = recolour_pads_gold(text)
    # kicad-cli stamps the export time into FILE_NAME, so two exports of an
    # unchanged board never compare equal. Both sibling scripts strip their
    # equivalent. Note this does NOT make the file fully reproducible: kicad-cli
    # also emits STYLED_ITEM records in a nondeterministic order, which is not
    # fixable from here.
    text = re.sub(r"(FILE_NAME\('[^']*',')[^']*(')", r"\g<1>1970-01-01T00:00:00\g<2>",
                  text, count=1)
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
            src, nclip, ndel, nhole, nmask, nsilk = clip_board_to_outline(board, scratch)
            if scratch:
                temp_stem = os.path.basename(scratch[0])
            if nclip == "no-outline":
                note = "  WARNING: board has no closed Edge.Cuts outline, nothing clipped"
            elif nclip is None:
                note = "  (clip skipped: pcbnew unavailable — run with KiCad's Python)"
            elif nclip or ndel or nhole or nmask:
                note = (f"  (clipped {nclip} pad(s), removed {ndel} outside, "
                        f"notched {nhole} castellation(s), "
                        f"{nmask} mask-exposed copper shape(s)"
                        + (f", stripped {nsilk} footprint silk" if nsilk else "") + ")")
            cmd[-1] = src
        result = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        # KiCad writes a lock file as ~<basename>.lck, i.e. a TILDE-PREFIXED
        # sibling that the stem glob never matches. Sweep both patterns or the
        # locks accumulate inside the repo.
        for stem_path in scratch:
            d, base = os.path.dirname(stem_path), os.path.basename(stem_path)
            for pattern in (glob.escape(stem_path) + ".*",
                            os.path.join(d, "~" + glob.escape(base) + ".*")):
                for f in glob.glob(pattern):
                    try:
                        os.remove(f)
                    except OSError:
                        pass

    dt = time.time() - t0
    if not os.path.exists(out):
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        print(f"  FAIL rc={result.returncode}  {tail}")
        return False
    # kicad-cli exits 2 when a footprint references a .wrl and nothing else
    # ("Cannot use VRML models when exporting to non-mesh formats") but still
    # writes a complete STEP for everything it could resolve. Treating that as a
    # hard failure skipped post_process, which left the file on disk with its
    # products still named after the clipped temp board -- worse than either
    # succeeding or failing outright. A written file is a result; report the
    # exit code as a warning and finish processing it.
    if result.returncode != 0:
        reasons = sorted({ln.strip() for ln in (result.stderr + result.stdout).splitlines()
                          if "Cannot use VRML models" in ln})
        print(f"  WARNING: kicad-cli exited {result.returncode} but wrote the file"
              + (f": {reasons[0]}" if reasons else ""))

    post_process(out, temp_stem or os.path.splitext(os.path.basename(board))[0],
                 os.path.splitext(os.path.basename(out))[0])
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
    p.add_argument("--repo", help="with --all, limit to these repos (comma separated)")
    p.add_argument("--products", action="store_true",
                   help="with --all, skip fab panels and bench fixtures "
                        "(the published fit-check set)")
    p.add_argument("--preset", default="standard", choices=sorted(PRESETS),
                   help="escape hatch only; the repo standard is 'standard'")
    p.add_argument("--no-clip", dest="clip", action="store_false",
                   help="keep copper that hangs past Edge.Cuts")
    p.add_argument("--root", default=ROOT, help=f"hardware dir (default {ROOT})")
    p.add_argument("--kicad-cli", help="path to kicad-cli")
    p.add_argument("--outdir",
                   help="with --all, write every STEP into this one directory instead of "
                        "each repo's export/. This is the Onshape import set: one folder "
                        "to drag in, named by product.")
    p.add_argument("--grey-pads", action="store_true",
                   help="leave exposed copper at kicad-cli's grey instead of ENIG gold")
    p.add_argument("--keep-fp-silk", action="store_true",
                   help="keep footprint-level silkscreen (component outlines and "
                        "polarity ticks); the board legend is always kept")
    p.add_argument("--dry-run", action="store_true", help="print commands only")
    p.add_argument("extra", nargs="*", help="extra kicad-cli flags")
    a = p.parse_args()

    global STRIP_FP_SILK, GOLD_PADS
    STRIP_FP_SILK = not a.keep_fp_silk
    GOLD_PADS = not a.grey_pads

    cli = find_kicad_cli(a.kicad_cli)

    if a.all:
        jobs = [(board,
                 os.path.join(a.outdir, name + ".step") if a.outdir
                 else os.path.join(a.root, repo, "export", name + ".step"))
                for repo, board, name in discover(a.root, a.repo, a.products)]
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
