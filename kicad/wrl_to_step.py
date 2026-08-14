#!/usr/bin/env python3
"""Rebuild a .step from the .wrl KiCad's 3D viewer renders.

For the E6 case in check_models.py: the .step sitting beside a .wrl is a
different shape, and the upstream (LCSC/EasyEDA) .step is the same wrong
file, so a correct one has to be made. The .wrl is the model the viewer
shows and the one a human has actually looked at, so it is the trusted
geometry. This converts that mesh to a tessellated STEP solid: exact
dimensions, faceted surfaces, no colors. Good for fit-check, not render.

Needs the cadquery-ocp wheel (pip install cadquery-ocp), system python.

Usage:
    python3 wrl_to_step.py model.wrl [-o model.step]
    python3 wrl_to_step.py --check model.wrl     # parse and report only

KiCad-lineage .wrl files are authored in 0.1 inch units; output is mm.
"""
import argparse, os, re, sys

SCALE = 2.54
POINT_BLOCK = re.compile(r"point\s*\[(.*?)\]", re.S)
INDEX_BLOCK = re.compile(r"coordIndex\s*\[(.*?)\]", re.S)
TRIPLE = re.compile(r"(-?[\d.eE+]+)\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)")


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


def build_shape(meshes):
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakePolygon,
                                    BRepBuilderAPI_MakeFace,
                                    BRepBuilderAPI_Sewing)
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound
    sew = BRepBuilderAPI_Sewing(1e-4)
    n_faces = 0
    for pts, faces in meshes:
        for face in faces:
            # fan-triangulate anything beyond a triangle
            for a, b, c in [(face[0], face[i], face[i + 1])
                            for i in range(1, len(face) - 1)]:
                if len({a, b, c}) < 3:
                    continue
                poly = BRepBuilderAPI_MakePolygon()
                for i in (a, b, c):
                    poly.Add(gp_Pnt(*pts[i]))
                poly.Close()
                if not poly.IsDone():
                    continue
                mk = BRepBuilderAPI_MakeFace(poly.Wire())
                if mk.IsDone():
                    sew.Add(mk.Face())
                    n_faces += 1
    sew.Perform()
    shape = sew.SewedShape()
    builder, comp = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(comp)
    builder.Add(comp, shape)
    return comp, n_faces


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
    shape, n = build_shape(meshes)
    export_step(shape, out)
    print(f"wrote {out} ({n} faces)")


if __name__ == "__main__":
    main()
