#!/usr/bin/env python3
"""OCC passes over an exported board STEP.

export_step.py runs under KiCad's bundled Python, which has pcbnew and no OCC.
Everything here needs OCC, so it runs under the system interpreter and
export_step.py shells out to it once per board. Import OCP lazily so that
importing this module under KiCad's Python (for the constants) is harmless.

Two passes, both driven from the board file alone:

  solidify_silkscreen
      kicad-cli writes silkscreen as "a set of flat faces": one
      SHELL_BASED_SURFACE_MODEL per glyph, zero thickness. A letter with a
      closed counter (B, O, D, R, 0) is one face with an inner bound, which is
      legal and which Onshape draws filled in: every such letter arrives as a
      solid blob. Giving each glyph real thickness removes the question -- an
      importer never has to decide what an inner bound on a sheet body means,
      because there are no sheet bodies left.

  strip_markings
      LCSC/EasyEDA package models carry the vendor's own silkscreen: the
      "LCEDA EasyEDA" wordmark, a cloud logo and the pin-1 dot, embossed a few
      microns proud of the package top and fused into the body solid. It is
      noise at any zoom a person actually uses. The faces are found by measure,
      not by name: a marking face lies in the emboss slab above the dominant
      top plane of its own solid, so it is removed with BRepAlgoAPI_Defeaturing
      and the top plane closes over it.

Both passes verify before they commit. Silkscreen is kept only if the prism is
a valid solid; a defeatured body is kept only if it is valid and its volume
moved by less than the emboss could account for. Anything that fails is left
exactly as kicad-cli wrote it, and the run says so.
"""

import argparse
import os
import sys
import time

SILK_THICKNESS_MM = 0.02

# An emboss on an LCSC model is 0.003 of a VRML unit, 0.0076 mm. Nothing
# structural on a package is that thin, so 0.05 mm is a wide margin that still
# cannot reach a lead, a shoulder or a chamfer.
MARKING_SLAB_MM = 0.05

# The wordmark is fine detail: many faces over very little area. A lead or a
# chamfer is the opposite. Faces per mm2 of the plane they sit on separates
# the two by two orders of magnitude, so the threshold is not delicate.
MARKING_MIN_FACES = 12


def _occ():
    """Import OCC once, and only when a pass actually runs."""
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.STEPCAFControl import STEPCAFControl_Reader, STEPCAFControl_Writer
    from OCP.XCAFDoc import (XCAFDoc_DocumentTool, XCAFDoc_ColorSurf,
                             XCAFDoc_ColorGen)
    from OCP.TDF import TDF_LabelSequence
    from OCP.TDataStd import TDataStd_Name
    from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID, TopAbs_SHELL
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.gp import gp_Vec, gp_Dir
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
    from OCP.Interface import Interface_Static
    return locals()


def _bbox(o, shape):
    b = o["Bnd_Box"]()
    o["BRepBndLib"].Add_s(shape, b)
    return b.Get()


def _faces(o, shape):
    out = []
    e = o["TopExp_Explorer"](shape, o["TopAbs_FACE"])
    while e.More():
        out.append(o["TopoDS"].Face_s(e.Current()))
        e.Next()
    return out


def _solids(o, shape):
    out = []
    e = o["TopExp_Explorer"](shape, o["TopAbs_SOLID"])
    while e.More():
        out.append(o["TopoDS"].Solid_s(e.Current()))
        e.Next()
    return out


def _label_name(o, lab):
    n = o["TDataStd_Name"]()
    if lab.FindAttribute(o["TDataStd_Name"].GetID_s(), n):
        return str(n.Get().ToExtString())
    return ""


def _colour_of(o, ct, shape):
    c = o["Quantity_Color"]()
    for kind in (o["XCAFDoc_ColorSurf"], o["XCAFDoc_ColorGen"]):
        if ct.GetColor(shape, kind, c):
            return c
    return None


def open_doc(o, path):
    app = o["XCAFApp_Application"].GetApplication_s()
    doc = o["TDocStd_Document"](o["TCollection_ExtendedString"]("MDTV-XCAF"))
    app.NewDocument(o["TCollection_ExtendedString"]("MDTV-XCAF"), doc)
    r = o["STEPCAFControl_Reader"]()
    r.SetColorMode(True)
    r.SetNameMode(True)
    if not r.ReadFile(path):
        sys.exit(f"step_post: cannot read {path}")
    r.Transfer(doc)
    return doc


def write_doc(o, doc, path):
    # Parametric surface curves double the file for nothing an importer needs;
    # off, the rewrite comes out the same size as what kicad-cli wrote.
    o["Interface_Static"].SetIVal_s("write.surfacecurve.mode", 0)
    o["Interface_Static"].SetCVal_s("write.step.unit", "MM")
    w = o["STEPCAFControl_Writer"]()
    w.SetColorMode(True)
    w.SetNameMode(True)
    w.Transfer(doc)
    if not w.Write(path):
        sys.exit(f"step_post: cannot write {path}")


def solidify_silkscreen(o, doc, thickness=SILK_THICKNESS_MM):
    """Extrude every silkscreen face into a thin solid, in place.

    Direction is decided per face from where it sits relative to the middle of
    the board, not from the face normal: kicad-cli orients both sides the same
    way, so following the normal buries the bottom legend inside the substrate.
    """
    st = o["XCAFDoc_DocumentTool"].ShapeTool_s(doc.Main())
    ct = o["XCAFDoc_DocumentTool"].ColorTool_s(doc.Main())
    seq = o["TDF_LabelSequence"]()
    st.GetShapes(seq)

    labels = []
    zmin = zmax = None
    for i in range(1, seq.Length() + 1):
        lab = seq.Value(i)
        if not st.IsSimpleShape_s(lab):
            continue
        shape = st.GetShape_s(lab)
        if _label_name(o, lab).endswith("_silkscreen"):
            # Already thickened by an earlier run. Extruding a solid again
            # would double its thickness every time, so a second pass over the
            # same file has to be a no-op.
            if not _solids(o, shape):
                labels.append((lab, shape))
            continue
        if _label_name(o, lab).endswith("_PCB"):
            _, _, a, _, _, b = _bbox(o, shape)
            zmin = a if zmin is None else min(zmin, a)
            zmax = b if zmax is None else max(zmax, b)

    if not labels:
        return 0, 0, 0
    if zmin is None:
        # No board body in this preset. Fall back to the span of the silk
        # itself, which still separates top from bottom.
        zs = [z for _, s in labels for z in (_bbox(o, s)[2], _bbox(o, s)[5])]
        zmin, zmax = min(zs), max(zs)
    middle = (zmin + zmax) / 2.0

    done = skipped = 0
    replaced = False
    for lab, shape in labels:
        colour = _colour_of(o, ct, shape)
        if colour is None:
            for f in _faces(o, shape)[:1]:
                colour = _colour_of(o, ct, f)
        builder = o["BRep_Builder"]()
        comp = o["TopoDS_Compound"]()
        builder.MakeCompound(comp)
        made = 0
        for f in _faces(o, shape):
            _, _, fz, _, _, _ = _bbox(o, f)
            sign = 1.0 if fz >= middle else -1.0
            vec = o["gp_Vec"](0.0, 0.0, sign * thickness)
            try:
                prism = o["BRepPrimAPI_MakePrism"](f, vec, True).Shape()
            except Exception:
                skipped += 1
                continue
            if not o["BRepCheck_Analyzer"](prism).IsValid():
                skipped += 1
                continue
            builder.Add(comp, prism)
            made += 1
        if not made:
            continue
        st.SetShape(lab, comp)
        replaced = True
        if colour is not None:
            ct.SetColor(lab, colour, o["XCAFDoc_ColorSurf"])
            ct.SetColor(lab, colour, o["XCAFDoc_ColorGen"])
        done += made
    if replaced:
        # Without this the writer emits what it read: SetShape updates the
        # label, UpdateAssemblies is what pushes it up into the compound the
        # STEP writer actually transfers.
        st.UpdateAssemblies()
    return len(labels), done, skipped


def close_shells(o, doc):
    """Sew every open shell into a solid, in place.

    A handful of library models arrive as SHELL_BASED_SURFACE_MODEL rather than
    a solid: the mesh they were rebuilt from did not close, so wrl_to_step could
    only write a shell. They are exactly the parts every colour complaint has
    been about -- the boot button, the two 0900 filters on Gemini and Mono, the
    TLV7031 in its X2SON-4, the USB-C shell -- because a surface body is not a
    part, so Onshape paints it out of its own palette instead of taking the
    colour the file gives, and two of the same part land in two different
    colours.

    Sewing at a loose tolerance closes almost all of them: the mesh really is a
    closed surface, its triangles just did not share vertices exactly. A shell
    that stays open after sewing is left as it is, because a sheet with a hole
    in it is not a solid and pretending otherwise would invent volume.
    """
    st = o["XCAFDoc_DocumentTool"].ShapeTool_s(doc.Main())
    ct = o["XCAFDoc_DocumentTool"].ColorTool_s(doc.Main())
    seq = o["TDF_LabelSequence"]()
    st.GetShapes(seq)
    closed = left = 0
    for i in range(1, seq.Length() + 1):
        lab = seq.Value(i)
        if not st.IsSimpleShape_s(lab):
            continue
        shape = st.GetShape_s(lab)
        shells = []
        e = o["TopExp_Explorer"](shape, o["TopAbs_SHELL"])
        while e.More():
            shells.append(o["TopoDS"].Shell_s(e.Current()))
            e.Next()
        open_shells = [s for s in shells if not s.Closed()]
        if not open_shells:
            continue
        colour = _colour_of(o, ct, shape)
        builder = o["BRep_Builder"]()
        comp = o["TopoDS_Compound"]()
        builder.MakeCompound(comp)
        touched = False
        for s in shells:
            made = None
            if not s.Closed():
                made = _sew_to_solid(o, s)
            if made is not None:
                touched = True
                closed += 1
                builder.Add(comp, made)
            else:
                if not s.Closed():
                    left += 1
                builder.Add(comp, s)
        # Anything in the label that was not a shell has to come across too.
        for solid in _solids(o, shape):
            builder.Add(comp, solid)
        if not touched:
            continue
        st.SetShape(lab, comp)
        if colour is not None:
            ct.SetColor(lab, colour, o["XCAFDoc_ColorSurf"])
            ct.SetColor(lab, colour, o["XCAFDoc_ColorGen"])
    if closed:
        st.UpdateAssemblies()
    return closed, left


def _sew_to_solid(o, shell):
    """Sew a shell shut and make it a solid, or None if it will not close."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing, BRepBuilderAPI_MakeSolid
    for tol in (1e-4, 1e-3, 1e-2):
        try:
            sew = BRepBuilderAPI_Sewing(tol, True, True, True, False)
            sew.Add(shell)
            sew.Perform()
            sewn = sew.SewedShape()
        except Exception:
            continue
        got = []
        e = o["TopExp_Explorer"](sewn, o["TopAbs_SHELL"])
        while e.More():
            got.append(o["TopoDS"].Shell_s(e.Current()))
            e.Next()
        if len(got) != 1 or not got[0].Closed():
            continue
        try:
            solid = BRepBuilderAPI_MakeSolid(got[0]).Solid()
        except Exception:
            continue
        if not o["BRepCheck_Analyzer"](solid).IsValid():
            continue
        g = o["GProp_GProps"]()
        o["BRepGProp"].VolumeProperties_s(solid, g)
        if g.Mass() <= 0:
            continue
        return solid
    return None


def _marking_faces(o, solid):
    """Faces of `solid` that sit in the emboss slab above its own top plane.

    The top plane is the horizontal plane carrying the most face area, which on
    a package body is the lid. Anything planar and horizontal above it, plus
    the walls that reach it, is the vendor's marking.
    """
    from collections import defaultdict
    areas = defaultdict(float)
    info = []
    for f in _faces(o, solid):
        ad = o["BRepAdaptor_Surface"](f)
        planar = ad.GetType() == o["GeomAbs_Plane"]
        x0, y0, z0, x1, y1, z1 = _bbox(o, f)
        g = o["GProp_GProps"]()
        o["BRepGProp"].SurfaceProperties_s(f, g)
        horizontal = planar and (z1 - z0) < 1e-6
        info.append((f, horizontal, z0, z1, g.Mass()))
        if horizontal:
            areas[round(z0, 4)] += g.Mass()
    if not areas:
        return []
    top = max(areas.items(), key=lambda kv: kv[1])[0]
    top_area = areas[top]
    # A glyph stroke is a sliver next to the lid it sits on. The metal end cap
    # of an 0201 is not: it covers a third of the top, and an earlier version
    # of this took four faces off every 0201 on the board because it only
    # looked at height. Area is what tells a marking from a part of the part.
    def small(area):
        return area <= 0.05 * top_area
    marks = [f for f, horiz, z0, z1, area in info
             if z0 > top + 1e-6 and z1 <= top + MARKING_SLAB_MM + 1e-6 and small(area)]
    walls = [f for f, horiz, z0, z1, area in info
             if not horiz and z0 >= top - 1e-6
             and z1 <= top + MARKING_SLAB_MM + 1e-6 and small(area)]
    out = marks + walls
    if len(out) < MARKING_MIN_FACES:
        return []
    return out


def strip_markings(o, doc):
    """Defeature the vendor wordmark off every component body that carries one."""
    st = o["XCAFDoc_DocumentTool"].ShapeTool_s(doc.Main())
    seq = o["TDF_LabelSequence"]()
    st.GetShapes(seq)
    cleaned = failed = 0
    for i in range(1, seq.Length() + 1):
        lab = seq.Value(i)
        if not st.IsSimpleShape_s(lab):
            continue
        name = _label_name(o, lab)
        if name.endswith("_silkscreen") or name.endswith("_PCB") or name.endswith("_pad"):
            continue
        shape = st.GetShape_s(lab)
        solids = _solids(o, shape)
        if len(solids) != 1:
            continue
        solid = solids[0]
        marks = _marking_faces(o, solid)
        if len(marks) < MARKING_MIN_FACES:
            continue
        before = o["GProp_GProps"]()
        o["BRepGProp"].VolumeProperties_s(solid, before)
        try:
            df = o["BRepAlgoAPI_Defeaturing"]()
            df.SetShape(solid)
            for f in marks:
                df.AddFaceToRemove(f)
            df.Build()
            if not df.IsDone():
                failed += 1
                continue
            out = df.Shape()
        except Exception:
            failed += 1
            continue
        if not o["BRepCheck_Analyzer"](out).IsValid():
            failed += 1
            continue
        after = o["GProp_GProps"]()
        o["BRepGProp"].VolumeProperties_s(out, after)
        # The emboss is microns. A defeaturing that moved real volume did not
        # remove a wordmark, it removed a feature, so throw it away.
        x0, y0, z0, x1, y1, z1 = _bbox(o, solid)
        budget = (x1 - x0) * (y1 - y0) * MARKING_SLAB_MM * 2
        if abs(after.Mass() - before.Mass()) > budget:
            failed += 1
            continue
        st.SetShape(lab, out)
        cleaned += 1
    if cleaned:
        st.UpdateAssemblies()
    return cleaned, failed


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", help="board STEP to rewrite in place")
    p.add_argument("--out", help="write here instead of in place")
    p.add_argument("--no-silk", action="store_true",
                   help="leave silkscreen as flat faces")
    p.add_argument("--no-close", action="store_true",
                   help="leave open shells as surface bodies")
    p.add_argument("--markings", action="store_true",
                   help="also defeature vendor wordmarks off package bodies")
    p.add_argument("--thickness", type=float, default=SILK_THICKNESS_MM,
                   help=f"silkscreen thickness in mm (default {SILK_THICKNESS_MM})")
    a = p.parse_args()

    o = _occ()
    t0 = time.time()
    doc = open_doc(o, a.step)
    notes = []
    if not a.no_silk:
        n_lab, n_made, n_skip = solidify_silkscreen(o, doc, a.thickness)
        notes.append(f"silkscreen {n_made} glyphs solid on {n_lab} layers"
                     + (f", {n_skip} left flat" if n_skip else ""))
    if not a.no_close:
        n_closed, n_left = close_shells(o, doc)
        if n_closed or n_left:
            notes.append(f"{n_closed} open shell(s) sewn into solids"
                         + (f", {n_left} would not close" if n_left else ""))
    if a.markings:
        n_ok, n_bad = strip_markings(o, doc)
        notes.append(f"markings stripped from {n_ok} bodies"
                     + (f", {n_bad} refused" if n_bad else ""))
    write_doc(o, doc, a.out or a.step)
    print(f"  post: {'; '.join(notes)} ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
