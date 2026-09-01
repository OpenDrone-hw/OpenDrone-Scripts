#!/usr/bin/env python3
"""Rebuild a .step from the .wrl KiCad's 3D viewer renders.

For the E6 case in check_models.py: the .step sitting beside a .wrl is a
different shape, and the upstream (LCSC/EasyEDA) .step is the same wrong
file, so a correct one has to be made. The .wrl is the model the viewer
shows and the one a human has actually looked at, so it is the trusted
geometry. This converts that mesh to tessellated STEP solids: exact
dimensions, faceted surfaces. Good for fit-check, not render.

Each IndexedFaceSet carries its own Material, so the solid built from it
is written with that diffuseColor as a STEP surface colour. Without it
every solid arrives unstyled and the importer picks its own: Onshape
gives two instances of one part two different colours, which is what an
uncoloured export looked like on the board. The float values are written
through unchanged, so the STEP shows what the KiCad 3D viewer shows.

Coplanar facets are merged into one face before writing
(ShapeUpgrade_UnifySameDomain). A tessellated box side is dozens of
triangles otherwise, which is most of the file size and reads as a mesh
of tiny squares in CAD. Merging is skipped for a solid it would
invalidate or whose volume it changes.

Output is solids, not shells. Sewing alone yields shells and
STEPControl_AsIs writes those verbatim, so every importer showed loose
surfaces rather than a part. Meshes EasyEDA authored open (the underside
is left off shielding cans and card cages) also shaded with visible
backfaces, so boundary loops are capped before the shell is closed. A
loop that is not a simple polygon cannot be capped and its shell stays
open; the run prints how many, and those still import as surfaces.

Needs the cadquery-ocp wheel (pip install cadquery-ocp), system python.

Usage:
    python3 wrl_to_step.py model.wrl [-o model.step]
    python3 wrl_to_step.py --check model.wrl     # parse and report only
    python3 wrl_to_step.py model.wrl --no-unify  # keep every facet a face
    python3 wrl_to_step.py --rebuild path/to/hardware
    python3 wrl_to_step.py --fill-missing path/to/hardware

--rebuild walks a tree and regenerates every .step THIS SCRIPT wrote that
is missing colour, next to its own .wrl. It matches on the OCC header, so
a vendor B-rep is never replaced by a mesh: the only files it touches are
ones an earlier run of this script produced.

KiCad-lineage .wrl files are authored in 0.1 inch units; output is mm.
"""
import argparse, collections, os, re, shutil, sys

SCALE = 2.54
POINT_BLOCK = re.compile(r"point\s*\[(.*?)\]", re.S)
INDEX_BLOCK = re.compile(r"coordIndex\s*\[(.*?)\]", re.S)
# a VRML coordinate can carry a negative exponent (-9.9999994e-05); a
# character class without the "-" truncates it and float() then raises
_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
TRIPLE = re.compile(rf"({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})")
DIFFUSE = re.compile(rf"diffuseColor\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})")
# Anything without a Material of its own: mid grey, so an unmatched mesh is
# still styled rather than left for the importer to colour at random.
FALLBACK_RGB = (0.6, 0.6, 0.6)


# STEP lets a colour be named instead of given as numbers, and both OCC and
# KiCad's own library models use that for the common ones. Onshape draws a
# named colour as untinted default, so a black IC body arrives the same grey
# as everything else. ISO 10303-46 fixes the meaning of each name, so the
# substitution is exact, not a guess.
PRE_DEFINED_RGB = {
    "black": (0.0, 0.0, 0.0),
    "white": (1.0, 1.0, 1.0),
    "red": (1.0, 0.0, 0.0),
    "green": (0.0, 1.0, 0.0),
    "blue": (0.0, 0.0, 1.0),
    "yellow": (1.0, 1.0, 0.0),
    "magenta": (1.0, 0.0, 1.0),
    "cyan": (0.0, 1.0, 1.0),
}
_PRE_DEFINED = re.compile(
    r"#(\d+)\s*=\s*DRAUGHTING_PRE_DEFINED_COLOUR\s*\(\s*'([^']*)'\s*\)\s*;")


def expand_predefined_colours(text):
    """Rewrite DRAUGHTING_PRE_DEFINED_COLOUR entities as COLOUR_RGB.

    Same entity id, so every STYLED_ITEM that points at it keeps working;
    only the record itself changes. Returns (text, n_rewritten).
    """
    def sub(m):
        rgb = PRE_DEFINED_RGB.get(m.group(2).strip().lower())
        if rgb is None:
            return m.group(0)
        return (f"#{m.group(1)} = COLOUR_RGB ( '{m.group(2)}', "
                f"{rgb[0]:.10f}, {rgb[1]:.10f}, {rgb[2]:.10f} ) ;")
    out, n = _PRE_DEFINED.subn(sub, text)
    return out, n


def parse_wrl(path):
    """Yield (points, faces, rgb) per IndexedFaceSet, points in mm.

    The Material sits in the same Shape node as the geometry and always
    ahead of it, so the colour of a mesh is the last diffuseColor before
    its point block.
    """
    text = open(path, errors="replace").read()
    points = [(m.start(), m.group(1)) for m in POINT_BLOCK.finditer(text)]
    indexes = [(m.start(), m.group(1)) for m in INDEX_BLOCK.finditer(text)]
    colours = [(m.start(), tuple(float(v) for v in m.groups()))
               for m in DIFFUSE.finditer(text)]
    for ppos, pblock in points:
        follow = [(ipos, iblock) for ipos, iblock in indexes if ipos > ppos]
        if not follow:
            continue
        ipos, iblock = min(follow)
        nxt = min((q for q, _ in points if q > ppos), default=None)
        if nxt is not None and ipos > nxt:
            continue  # this point block has no own coordIndex
        pts = [tuple(float(v) * SCALE for v in m.groups())
               for m in TRIPLE.finditer(pblock)]
        idx, face, faces = [int(v) for v in re.findall(r"-?\d+", iblock)], [], []
        for i in idx:
            if i == -1:
                if len(face) >= 3:
                    faces.append(tuple(face))
                face = []
            else:
                face.append(i)
        if len(face) >= 3:
            faces.append(tuple(face))
        if pts and faces:
            before = [rgb for cpos, rgb in colours if cpos < ppos]
            yield pts, faces, (before[-1] if before else FALLBACK_RGB)


def triangulate(face):
    """Fan-triangulate a polygon face index tuple."""
    return [(face[0], face[i], face[i + 1]) for i in range(1, len(face) - 1)]


def boundary_loops(faces):
    """Ordered index loops around the open boundary of a triangle mesh."""
    used = collections.Counter()
    for face in faces:
        for tri in triangulate(face):
            for a, b in zip(tri, tri[1:] + tri[:1]):
                if a != b:
                    used[frozenset((a, b))] += 1
    edges = {e for e, n in used.items() if n == 1}
    adj = collections.defaultdict(set)
    for e in edges:
        a, b = tuple(e)
        adj[a].add(b)
        adj[b].add(a)
    loops = []
    while edges:
        a, b = tuple(edges.pop())
        loop = [a, b]
        while True:
            nxt = [n for n in adj[loop[-1]]
                   if frozenset((loop[-1], n)) in edges]
            if not nxt:
                break
            edges.discard(frozenset((loop[-1], nxt[0])))
            if nxt[0] == loop[0]:
                break
            loop.append(nxt[0])
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def orient(pts, tris):
    """Consistent winding per connected component, outward.

    The .wrl files declare `solid FALSE`, so KiCad draws both sides of every
    triangle and inconsistent winding is invisible in the 3D viewer. Sewing
    that into a solid is where it starts to matter: a shell built from
    mixed-winding triangles comes out partly inside-out, and an inside-out
    solid reads as a void in CAD. Walk each component over shared edges
    flipping neighbours into agreement, then flip any component whose signed
    volume came out negative.
    """
    adj = collections.defaultdict(list)
    for i, (a, b, c) in enumerate(tris):
        for u, v in ((a, b), (b, c), (c, a)):
            adj[frozenset((u, v))].append(i)
    out = list(tris)
    seen = [False] * len(out)
    for start in range(len(out)):
        if seen[start]:
            continue
        seen[start] = True
        stack, comp = [start], [start]
        while stack:
            i = stack.pop()
            a, b, c = out[i]
            for u, v in ((a, b), (b, c), (c, a)):
                for j in adj[frozenset((u, v))]:
                    if seen[j]:
                        continue
                    x, y, z = out[j]
                    # a correctly wound neighbour crosses the shared edge the
                    # other way round; same direction means it is flipped
                    if (u, v) in ((x, y), (y, z), (z, x)):
                        out[j] = (x, z, y)
                    seen[j] = True
                    stack.append(j)
                    comp.append(j)
        vol = 0.0
        for i in comp:
            a, b, c = out[i]
            pa, pb, pc = pts[a], pts[b], pts[c]
            vol += (pa[0] * (pb[1] * pc[2] - pb[2] * pc[1])
                    - pa[1] * (pb[0] * pc[2] - pb[2] * pc[0])
                    + pa[2] * (pb[0] * pc[1] - pb[1] * pc[0])) / 6.0
        if vol < 0:
            for i in comp:
                a, b, c = out[i]
                out[i] = (a, c, b)
    return out


def _tri_face(pts, tri):
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakePolygon,
                                    BRepBuilderAPI_MakeFace)
    from OCP.gp import gp_Pnt
    if len(set(tri)) < 3:
        return None
    poly = BRepBuilderAPI_MakePolygon()
    try:
        for i in tri:
            poly.Add(gp_Pnt(*pts[i]))
        poly.Close()
        if not poly.IsDone():
            return None
        mk = BRepBuilderAPI_MakeFace(poly.Wire())
    except Exception:
        # A triangle with two coincident points below OCC's tolerance raises
        # rather than reporting not-done. Dropping it loses nothing: it has
        # no area. Some EasyEDA meshes carry hundreds.
        return None
    return mk.Face() if mk.IsDone() else None


def _newell(ring):
    """Best-fit plane normal of a 3D ring (Newell's method), normalised."""
    n = [0.0, 0.0, 0.0]
    for (x1, y1, z1), (x2, y2, z2) in zip(ring, ring[1:] + ring[:1]):
        n[0] += (y1 - y2) * (z1 + z2)
        n[1] += (z1 - z2) * (x1 + x2)
        n[2] += (x1 - x2) * (y1 + y2)
    mag = sum(c * c for c in n) ** 0.5
    return None if mag < 1e-12 else [c / mag for c in n]


def _ear_clip(ring):
    """Triangulate a simple polygon, given as 3D points, on its best-fit
    plane. Returns index triples into ring. Ear clipping keeps the cap
    inside the loop, which a centroid fan does not do on a concave one:
    that produced self-intersecting caps and invalid solids."""
    n = _newell(ring)
    if n is None or len(ring) < 3:
        return []
    # any two axes orthogonal to the normal form the projection basis
    up = [0.0, 0.0, 1.0] if abs(n[2]) < 0.9 else [1.0, 0.0, 0.0]
    u = [n[1] * up[2] - n[2] * up[1],
         n[2] * up[0] - n[0] * up[2],
         n[0] * up[1] - n[1] * up[0]]
    mag = sum(c * c for c in u) ** 0.5
    u = [c / mag for c in u]
    v = [n[1] * u[2] - n[2] * u[1],
         n[2] * u[0] - n[0] * u[2],
         n[0] * u[1] - n[1] * u[0]]
    flat = [(sum(a * b for a, b in zip(p, u)),
             sum(a * b for a, b in zip(p, v))) for p in ring]

    def area2(a, b, c):
        return ((flat[b][0] - flat[a][0]) * (flat[c][1] - flat[a][1]) -
                (flat[b][1] - flat[a][1]) * (flat[c][0] - flat[a][0]))

    total = sum(flat[i][0] * flat[(i + 1) % len(flat)][1] -
                flat[(i + 1) % len(flat)][0] * flat[i][1]
                for i in range(len(flat)))
    idx = list(range(len(flat)))
    if total < 0:
        idx.reverse()

    def inside(a, b, c, p):
        d1, d2, d3 = area2(p, a, b), area2(p, b, c), area2(p, c, a)
        return not ((d1 < 0 or d2 < 0 or d3 < 0) and
                    (d1 > 0 or d2 > 0 or d3 > 0))

    tris, guard = [], 0
    while len(idx) > 3 and guard < len(idx) * len(idx) + 16:
        guard += 1
        for k in range(len(idx)):
            a, b, c = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            if area2(a, b, c) <= 1e-12:
                continue
            if any(inside(a, b, c, q) for q in idx if q not in (a, b, c)):
                continue
            tris.append((a, b, c))
            idx.pop(k)
            guard = 0
            break
        else:
            break  # no ear found, polygon is not simple; bail out
    if len(idx) == 3:
        tris.append(tuple(idx))
    return tris


def _solidify(shape):
    """Closed shells -> oriented solids. Returns (solids, open_shells)."""
    from OCP.BRep import BRep_Tool
    from OCP.ShapeFix import ShapeFix_Solid
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    solids, opened = [], []
    exp = TopExp_Explorer(shape, TopAbs_SHELL)
    while exp.More():
        shell = TopoDS.Shell_s(exp.Current())
        exp.Next()
        if not BRep_Tool.IsClosed_s(shell):
            opened.append(shell)
            continue
        # SolidFromShell is meant to orient the shell outward. On a
        # tessellated shell it does not always manage it: 67 of the 405
        # solids here still read inside-out, which shows as a void in CAD.
        # Reversing them does not survive the STEP round trip (it measures
        # worse, 83), so this is left as the source .wrl's problem.
        solids.append(ShapeFix_Solid().SolidFromShell(shell))
    return solids, opened


def unify(shape):
    """Merge coplanar faces. Returns the original if that goes wrong.

    Every facet of the mesh arrives as its own planar face, so a flat box
    side is dozens of triangles and a STEP importer draws every seam.
    UnifySameDomain fuses faces that share a surface. It is not always
    safe on a tessellated solid, so the result is kept only when it is
    still a valid shape of the same volume.
    """
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    def volume(s):
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(s, props)
        return props.Mass()

    before = volume(shape)
    try:
        up = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
        up.Build()
        out = up.Shape()
    except Exception:
        return shape, False
    if not BRepCheck_Analyzer(out).IsValid():
        return shape, False
    after = volume(out)
    if abs(after - before) > max(1e-9, abs(before) * 1e-6):
        return shape, False
    return out, True


def build_shape(meshes, cap=True, tol=1e-4, merge=True):
    """Sew each IndexedFaceSet on its own into a closed solid.

    Sewing all meshes as one soup hands back whatever shells fall out and
    STEPControl_AsIs then writes those shells verbatim, which is what made
    every importer show loose surfaces. Per mesh, cap, solid.

    Returns [(shape, rgb)] rather than one compound: the colour is per
    mesh, and the writer needs each shape separately to label it.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing

    items = []
    n_faces = n_caps = n_solids = n_open = n_merged = 0

    for pts, faces, rgb in meshes:
        tris = []
        for face in faces:
            tris.extend(triangulate(face))
        n_solid_tris = len(tris)
        if cap:
            # Cap as triangles, not as one planar face per loop, so the
            # orientation pass below sees the caps too.
            for loop in boundary_loops(faces):
                ring = [pts[i] for i in loop]
                for a, b, c in _ear_clip(ring):
                    tris.append((loop[a], loop[b], loop[c]))
        n_caps += len(tris) - n_solid_tris
        tris = orient(pts, tris)

        sew = BRepBuilderAPI_Sewing(tol)
        added = 0
        for tri in tris:
            f = _tri_face(pts, tri)
            if f is not None:
                sew.Add(f)
                added += 1
        if not added:
            continue
        n_faces += n_solid_tris
        sew.Perform()
        solids, opened = _solidify(sew.SewedShape())
        for s_ in solids:
            if merge:
                s_, done = unify(s_)
                n_merged += done
            items.append((s_, rgb))
        for s_ in opened:
            items.append((s_, rgb))
        n_solids += len(solids)
        n_open += len(opened)
    return items, n_faces, n_caps, n_solids, n_open, n_merged


def export_step(items, out, name=None):
    """Write [(shape, rgb)] as ONE named STEP part, each solid coloured.

    STEPCAFControl_Writer, not STEPControl_Writer: colour rides on an
    XCAF label beside the shape, and only the CAF writer emits the
    STYLED_ITEM records an importer reads.

    The solids go in as one compound under a single named label, and the
    colours onto sub-shape labels beneath it. Adding them as separate top
    labels also works and colours correctly, but OCC then names each
    product after its shape type, so the board arrives in Onshape with a
    tree full of parts called SOLID and SHELL instead of the model name.

    The colour goes in as sRGB and comes out as the same numbers. OCC 7.9
    holds Quantity_Color in LINEAR rgb and converts on write, so handing
    it a .wrl diffuseColor as Quantity_TOC_RGB writes a value gamma
    steps too light: 0.2235 arrived in the file as 0.5101.
    """
    from OCP.BRep import BRep_Builder
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Controller
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopoDS import TopoDS_Compound
    from OCP.XCAFDoc import (XCAFDoc_ColorSurf, XCAFDoc_ColorGen,
                             XCAFDoc_DocumentTool)
    # The write.* statics do not exist until the STEP controller registers
    # them, so setting one before this call is silently a no-op.
    STEPControl_Controller.Init_s()
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    # Drop the parametric (p-)curves. They are optional data an importer
    # recomputes, and on a tessellated solid they are most of the file:
    # OCC writes almost every triangle edge as a B_SPLINE_CURVE_WITH_KNOTS
    # plus two PCURVEs. TF-PUSH goes 4.43 -> 1.72 MB with the same 21
    # solids, same volume and same bounding box. It also stops
    # model_audit.py counting those splines as modelled LCEDA artwork.
    Interface_Static.SetIVal_s("write.surfacecurve.mode", 0)
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colours = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    builder, comp = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(comp)
    for shape, _ in items:
        builder.Add(comp, shape)
    top = shapes.AddShape(comp, False)
    TDataStd_Name.Set_s(top, TCollection_ExtendedString(
        name or os.path.splitext(os.path.basename(out))[0]))
    for shape, rgb in items:
        label = shapes.AddSubShape(top, shape)
        colour = Quantity_Color(*rgb, Quantity_TOC_sRGB)
        # Surf styles the faces, Gen is the fallback an importer reads when
        # it does not look at surface styles. Both, or the part is grey in
        # one viewer and coloured in the next.
        colours.SetColor(label, colour, XCAFDoc_ColorSurf)
        colours.SetColor(label, colour, XCAFDoc_ColorGen)
    w = STEPCAFControl_Writer()
    w.Transfer(doc, STEPControl_AsIs)
    if w.Write(out) != IFSelect_RetDone:
        raise RuntimeError(f"STEP write failed: {out}")
    # OCC writes pure black and pure white as named colours, so a model
    # whose body is 0 0 0 would still land unstyled in Onshape.
    with open(out, encoding="utf8", errors="surrogateescape") as fh:
        text = fh.read()
    text, n = expand_predefined_colours(text)
    if n:
        with open(out, "w", encoding="utf8", errors="surrogateescape") as fh:
            fh.write(text)


def bbox(meshes):
    pts = [p for pts, _, _ in meshes for p in pts]
    cols = list(zip(*pts))
    return tuple(max(c) - min(c) for c in cols)


OUR_HEADER = "Open CASCADE STEP processor"
CURVED = re.compile(r"CYLINDRICAL_SURFACE|CONICAL_SURFACE|SPHERICAL_SURFACE|"
                    r"TOROIDAL_SURFACE|B_SPLINE_SURFACE")


def colour_from_wrl(step, wrl, meshes, near_mm=0.05):
    """Add the .wrl colours to an existing STEP without touching geometry.

    For a real B-rep the mesh rebuild is a downgrade: it trades cylinders
    for hundreds of flat facets and a much larger file. So the solids are
    read back, matched to the .wrl shape that covers them, and written out
    again with that shape's colour.

    Matching is per VERTEX, not per bounding box. A .wrl carries one mesh
    per material, so the sixteen contacts of a USB-C are a single mesh
    whose box spans the whole connector and matches no one solid; but
    every vertex of a solid sits on the mesh that drew it. Each solid
    takes the colour of the mesh holding the most vertices within near_mm
    of its own. The two are the same geometry, so that is a near-exact
    vote rather than a guess.
    """
    import numpy as np
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_SOLID, TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.BRep import BRep_Tool

    reader = STEPControl_Reader()
    if reader.ReadFile(step) != IFSelect_RetDone:
        raise RuntimeError(f"cannot read {step}")
    reader.TransferRoots()
    shape = reader.OneShape()

    clouds = [(np.array(pts, dtype=float), rgb) for pts, _, rgb in meshes]
    biggest = max(clouds, key=lambda c: len(c[0]))[1]
    items, matched = [], 0
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        solid = TopoDS.Solid_s(exp.Current())
        exp.Next()
        vexp, verts = TopExp_Explorer(solid, TopAbs_VERTEX), []
        while vexp.More():
            p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vexp.Current()))
            verts.append((p.X(), p.Y(), p.Z()))
            vexp.Next()
        if not verts:
            items.append((solid, biggest))
            continue
        v = np.array(verts, dtype=float)
        best, votes = None, -1
        for cloud, rgb in clouds:
            d = np.abs(v[:, None, :] - cloud[None, :, :]).sum(axis=2)
            hit = int((d.min(axis=1) <= near_mm).sum())
            if hit > votes:
                best, votes = rgb, hit
        if votes > 0:
            matched += 1
        items.append((solid, best if votes > 0 else biggest))
    export_step(items, step)
    return len(items), matched


def rebuildable(root, force=False):
    """Every .step under root that this script wrote and that has no colour.

    Both conditions matter. The header keeps a vendor B-rep from being
    replaced with a mesh; the missing colour is what a rebuild is for, so
    a model already carrying its .wrl colours is left alone and the walk
    is idempotent. force drops the colour test, for when this script
    itself changed and every model it owns has to be written again.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ('.git', '.history', 'backups', 'archive',
                                    'export', 'node_modules', '__pycache__')]
        for name in sorted(filenames):
            if not name.endswith('.step'):
                continue
            step = os.path.join(dirpath, name)
            wrl = step[:-5] + '.wrl'
            if not os.path.exists(wrl):
                continue
            with open(step, errors='replace') as fh:
                head = fh.read(400)
            if OUR_HEADER not in head:
                continue
            if not force:
                with open(step, errors='replace') as fh:
                    if 'COLOUR_RGB' in fh.read():
                        continue
            out.append((wrl, step))
    return out


# How far a catalogue model may differ from the board's own before it counts
# as a different part. This tolerance should remain well below the measured
# difference between distinct packages that happen to share a filename.
CATALOGUE_TOL_MM = 0.1


def prefer_catalogue(root, catalogue, tol=CATALOGUE_TOL_MM, apply=False):
    """Swap a mesh-rebuilt .step for the catalogue's real B-rep of the same part.

    A model this script wrote is a triangle soup: a few hundred flat facets
    where the vendor's own file has a dozen cylinders, and often an open shell
    where the mesh did not close. An open shell is not a solid, so an importer
    may colour it from its own palette.

    KiCad-Library already holds the vendor B-rep for most of them. Where the
    two agree on size they are the same part and the catalogue copy is simply
    better, so it replaces the local one. Where they do not, they are NOT the
    same part and nothing is touched.
    Size is the test because it is the one property a wrong part cannot fake.
    """
    from OCP.STEPControl import STEPControl_Reader
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    def dims(path):
        r = STEPControl_Reader()
        r.ReadFile(path)
        r.TransferRoots()
        box = Bnd_Box()
        BRepBndLib.Add_s(r.OneShape(), box)
        x0, y0, z0, x1, y1, z1 = box.Get()
        return (x1 - x0, y1 - y0, z1 - z0)

    have = {}
    for name in sorted(os.listdir(catalogue)) if os.path.isdir(catalogue) else []:
        if name.endswith(".step"):
            have[name] = os.path.join(catalogue, name)

    swapped = kept = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ('.git', 'export', 'node_modules', '__pycache__')
                       and 'backup' not in d.lower() and 'archive' not in d.lower()
                       and not d.startswith('.')]
        if os.path.abspath(dirpath) == os.path.abspath(catalogue):
            continue
        for name in sorted(filenames):
            if name not in have:
                continue
            step = os.path.join(dirpath, name)
            with open(step, errors='replace') as fh:
                if OUR_HEADER not in fh.read(400):
                    continue
            try:
                mine, theirs = dims(step), dims(have[name])
            except Exception as exc:
                print(f"  ?  {os.path.relpath(step, root)}: {exc}")
                continue
            far = max(abs(mine[i] - theirs[i]) for i in range(3))
            if far > tol:
                kept += 1
                print(f"  keep {os.path.relpath(step, root)}: catalogue is a "
                      f"different part, {far:.3f} mm apart")
                continue
            swapped += 1
            print(f"  swap {os.path.relpath(step, root)} ({far:.3f} mm)")
            if apply:
                shutil.copyfile(have[name], step)
    print(f"{swapped} model(s) {'replaced by' if apply else 'would take'} the "
          f"catalogue B-rep, {kept} left alone as a different part")
    return 0


def missing_step(root):
    """Every .wrl under root with no .step beside it.

    `--subst-models` swaps a same-named .step in for each .wrl, because
    KiCad's STEP exporter cannot read VRML. Where there is nothing to swap
    in, kicad-cli drops the component silently and the board exports one
    part short. check_models does not catch it: E3 wants a missing FILE
    and the .wrl is right there, E6 only compares when both exist.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ('.git', 'export', 'node_modules', '__pycache__')
                       and 'backup' not in d.lower() and 'archive' not in d.lower()
                       and not d.startswith('.')]
        for name in sorted(filenames):
            if not name.endswith('.wrl'):
                continue
            wrl = os.path.join(dirpath, name)
            stem = os.path.splitext(wrl)[0]
            if not os.path.exists(stem + '.step') and not os.path.exists(stem + '.stp'):
                out.append((wrl, stem + '.step'))
    return out


def fill_missing(root, merge=True):
    todo = missing_step(root)
    print(f"{len(todo)} .wrl under {root} with no .step to substitute\n")
    for wrl, step in todo:
        meshes = list(parse_wrl(wrl))
        if not meshes:
            print(f"  SKIP  {os.path.relpath(wrl, root)}: no geometry parsed")
            continue
        items, n, caps, solids, opened, merged = build_shape(meshes, merge=merge)
        export_step(items, step)
        print(f"  {os.path.relpath(step, root)}\n"
              f"      {len({rgb for _, _, rgb in meshes})} colour(s), {solids} solids, "
              f"{merged} merged, {opened} open shell(s), "
              f"{os.path.getsize(step) / 1e6:.2f} MB")


def rebuild(root, merge=True, force=False):
    todo = rebuildable(root, force=force)
    print(f"{len(todo)} model(s) to rebuild under {root}\n")
    for wrl, step in todo:
        was = os.path.getsize(step)
        meshes = list(parse_wrl(wrl))
        if not meshes:
            print(f"  SKIP  {step}: no geometry parsed from the .wrl")
            continue
        n_rgb = len({rgb for _, _, rgb in meshes})
        with open(step, errors='replace') as fh:
            curved = CURVED.search(fh.read()) is not None
        if curved:
            # A real B-rep, whatever wrote it. Colour it, keep the geometry.
            n_solids, matched = colour_from_wrl(step, wrl, meshes)
            note = f"coloured in place, {matched}/{n_solids} solids matched"
        else:
            items, n, caps, solids, opened, merged = build_shape(
                meshes, merge=merge)
            export_step(items, step)
            note = (f"rebuilt, {solids} solids, {merged} merged, "
                    f"{opened} open shell(s)")
        now = os.path.getsize(step)
        print(f"  {os.path.relpath(step, root)}\n"
              f"      {n_rgb} colour(s), {note}, "
              f"{was / 1e6:.2f} -> {now / 1e6:.2f} MB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wrl", nargs='?')
    ap.add_argument("-o", "--out")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-cap", action="store_true",
                    help="do not close boundary loops (leaves open shells)")
    ap.add_argument("--no-unify", action="store_true",
                    help="do not merge coplanar facets into one face")
    ap.add_argument("--rebuild", metavar="ROOT",
                    help="regenerate every uncoloured .step this script wrote")
    ap.add_argument("--force", action="store_true",
                    help="with --rebuild: redo the coloured ones too")
    ap.add_argument("--fill-missing", metavar="ROOT",
                    help="write a .step beside every .wrl that has none")
    ap.add_argument("--prefer-catalogue", metavar="ROOT",
                    help="replace mesh-rebuilt models with catalogue B-reps "
                         "of the same part, where the two agree on size")
    ap.add_argument("--catalogue",
                    help="model catalogue directory (required with --prefer-catalogue)")
    ap.add_argument("--apply", action="store_true",
                    help="with --prefer-catalogue: write, instead of listing")
    a = ap.parse_args()
    if a.prefer_catalogue:
        if not a.catalogue:
            ap.error("--catalogue is required with --prefer-catalogue")
        return prefer_catalogue(os.path.expanduser(a.prefer_catalogue),
                                os.path.expanduser(a.catalogue),
                                apply=a.apply)
    if a.fill_missing:
        return fill_missing(os.path.expanduser(a.fill_missing),
                            merge=not a.no_unify)
    if a.rebuild:
        return rebuild(os.path.expanduser(a.rebuild), merge=not a.no_unify,
                       force=a.force)
    if not a.wrl:
        ap.error("a .wrl is required unless --rebuild is given")
    meshes = list(parse_wrl(a.wrl))
    if not meshes:
        sys.exit(f"no IndexedFaceSet geometry parsed from {a.wrl}")
    dims = bbox(meshes)
    n_pts = sum(len(p) for p, _, _ in meshes)
    n_rgb = len({rgb for _, _, rgb in meshes})
    print(f"{os.path.basename(a.wrl)}: {len(meshes)} meshes, {n_pts} points, "
          f"{n_rgb} colour(s), bbox {'x'.join(f'{d:.2f}' for d in dims)} mm")
    if a.check:
        return
    out = a.out or os.path.splitext(a.wrl)[0] + ".step"
    items, n, caps, solids, opened, merged = build_shape(
        meshes, cap=not a.no_cap, merge=not a.no_unify)
    export_step(items, out)
    print(f"wrote {out} ({n} faces + {caps} caps, "
          f"{solids} solids, {merged} merged, {opened} open shells)")
    if opened:
        print("  warning: open shells left, those import as surfaces",
              file=sys.stderr)


if __name__ == "__main__":
    main()
