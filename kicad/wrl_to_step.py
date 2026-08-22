#!/usr/bin/env python3
"""Rebuild a .step from the .wrl KiCad's 3D viewer renders.

For the E6 case in check_models.py: the .step sitting beside a .wrl is a
different shape, and the upstream (LCSC/EasyEDA) .step is the same wrong
file, so a correct one has to be made. The .wrl is the model the viewer
shows and the one a human has actually looked at, so it is the trusted
geometry. This converts that mesh to tessellated STEP solids: exact
dimensions, faceted surfaces, no colors. Good for fit-check, not render.

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

KiCad-lineage .wrl files are authored in 0.1 inch units; output is mm.
"""
import argparse, collections, os, re, sys

SCALE = 2.54
POINT_BLOCK = re.compile(r"point\s*\[(.*?)\]", re.S)
INDEX_BLOCK = re.compile(r"coordIndex\s*\[(.*?)\]", re.S)
# a VRML coordinate can carry a negative exponent (-9.9999994e-05); a
# character class without the "-" truncates it and float() then raises
_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
TRIPLE = re.compile(rf"({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})")


def parse_wrl(path):
    """Yield (points, faces) per IndexedFaceSet, points in mm."""
    text = open(path, errors="replace").read()
    points = [(m.start(), m.group(1)) for m in POINT_BLOCK.finditer(text)]
    indexes = [(m.start(), m.group(1)) for m in INDEX_BLOCK.finditer(text)]
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
            yield pts, faces


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


def _tri_face(pts, tri):
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakePolygon,
                                    BRepBuilderAPI_MakeFace)
    from OCP.gp import gp_Pnt
    if len(set(tri)) < 3:
        return None
    poly = BRepBuilderAPI_MakePolygon()
    for i in tri:
        poly.Add(gp_Pnt(*pts[i]))
    poly.Close()
    if not poly.IsDone():
        return None
    mk = BRepBuilderAPI_MakeFace(poly.Wire())
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


def _cap_faces(pts, loop):
    """Close one boundary loop. A planar loop becomes a single face; a
    non-planar one is ear-clipped into triangles. Returns a list of faces."""
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakePolygon,
                                    BRepBuilderAPI_MakeFace)
    from OCP.gp import gp_Pnt
    poly = BRepBuilderAPI_MakePolygon()
    for i in loop:
        poly.Add(gp_Pnt(*pts[i]))
    poly.Close()
    if poly.IsDone():
        mk = BRepBuilderAPI_MakeFace(poly.Wire())
        if mk.IsDone():
            return [mk.Face()]
    ring = [pts[i] for i in loop]
    out = []
    for a, b, c in _ear_clip(ring):
        f = _tri_face(pts, (loop[a], loop[b], loop[c]))
        if f is not None:
            out.append(f)
    return out


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
        # SolidFromShell also flips an inward-facing shell, so the material
        # lands on the right side; a reversed solid reads as a void in CAD.
        solids.append(ShapeFix_Solid().SolidFromShell(shell))
    return solids, opened


def build_shape(meshes, cap=True, tol=1e-4):
    """Sew each IndexedFaceSet on its own into a closed solid.

    Sewing all meshes as one soup hands back whatever shells fall out and
    STEPControl_AsIs then writes those shells verbatim, which is what made
    every importer show loose surfaces. Per mesh, cap, solid.
    """
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopoDS import TopoDS_Compound

    builder, comp = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(comp)
    n_faces = n_caps = n_solids = n_open = 0

    for pts, faces in meshes:
        sew = BRepBuilderAPI_Sewing(tol)
        added = 0
        for face in faces:
            for tri in triangulate(face):
                f = _tri_face(pts, tri)
                if f is not None:
                    sew.Add(f)
                    added += 1
        if cap:
            for loop in boundary_loops(faces):
                for f in _cap_faces(pts, loop):
                    sew.Add(f)
                    n_caps += 1
        if not added:
            continue
        n_faces += added
        sew.Perform()
        solids, opened = _solidify(sew.SewedShape())
        for s in solids:
            builder.Add(comp, s)
        for s in opened:
            builder.Add(comp, s)
        n_solids += len(solids)
        n_open += len(opened)
    return comp, n_faces, n_caps, n_solids, n_open


def export_step(shape, out):
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    if w.Write(out) != IFSelect_RetDone:
        raise RuntimeError(f"STEP write failed: {out}")


def bbox(path_or_meshes):
    pts = [p for pts, _ in path_or_meshes for p in pts]
    cols = list(zip(*pts))
    return tuple(max(c) - min(c) for c in cols)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wrl")
    ap.add_argument("-o", "--out")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-cap", action="store_true",
                    help="do not close boundary loops (leaves open shells)")
    a = ap.parse_args()
    meshes = list(parse_wrl(a.wrl))
    if not meshes:
        sys.exit(f"no IndexedFaceSet geometry parsed from {a.wrl}")
    dims = bbox(meshes)
    n_pts = sum(len(p) for p, _ in meshes)
    print(f"{os.path.basename(a.wrl)}: {len(meshes)} meshes, {n_pts} points, "
          f"bbox {'x'.join(f'{d:.2f}' for d in dims)} mm")
    if a.check:
        return
    out = a.out or os.path.splitext(a.wrl)[0] + ".step"
    shape, n, caps, solids, opened = build_shape(meshes, cap=not a.no_cap)
    export_step(shape, out)
    print(f"wrote {out} ({n} faces + {caps} caps, "
          f"{solids} solids, {opened} open shells)")
    if opened:
        print("  warning: open shells left, those import as surfaces",
              file=sys.stderr)


if __name__ == "__main__":
    main()
