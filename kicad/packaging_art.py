#!/usr/bin/env python3
"""
packaging_art.py — flat gold/white vector board art for OpenDrone packaging.

Exports front/back of a .kicad_pcb as single-color vector SVG in the black/gold
packaging theme:
  - pads, silkscreen, board outline in gold; tracks/zones/vias hidden
  - artwork clipped to the board outline (pads never stick out past the edge)
  - every component drawn as a simple knocked-out body with a gold outline
    (closed Fab shapes where the library has them, otherwise a body synthesized
    from the Fab drawings / courtyard bounding box)
  - designer text on the mask layers (copper-pour cutout logos like "openESC")
    moved to silk so it renders like silkscreen text
  - fab text (values/designators) stripped

The source .kicad_pcb is NEVER touched — all edits happen on throwaway temp
copies. KiCad may stay open.

MUST be run with KiCad's bundled Python (it imports pcbnew):

  KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
  $KPY software/OpenDrone-Scripts/kicad/packaging_art.py hardware/4in1.kicad_pcb --outdir packaging/ --png

Examples:
  # gold-on-white theme (default), SVG + PNG previews
  packaging_art.py board.kicad_pcb --outdir packaging/ --png

  # art for a BLACK box: holes/bodies knocked out in black
  packaging_art.py board.kicad_pcb --outdir packaging/ --holes '#0a0a0a' --body '#0a0a0a' --png --png-bg '#0a0a0a'

  # keep copper traces / skip component bodies
  packaging_art.py board.kicad_pcb --keep-traces --no-components

Dependencies: KiCad 9/10 (pcbnew + kicad-cli); rsvg-convert or ImageMagick
(`magick`) only needed for --png.
"""
import argparse
import glob
import gzip
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

DEFAULT_KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
GOLD = "#C9A227"  # packaging gold — matches the black/gold box theme

BASE_LAYERS = {"top": "Edge.Cuts,F.Cu,F.SilkS", "bottom": "Edge.Cuts,B.Cu,B.SilkS"}
FAB_LAYERS = {"top": "F.Fab", "bottom": "B.Fab"}


def find_kicad_cli(explicit):
    if explicit:
        return explicit
    here = sys.executable
    if "KiCad.app" in here:
        c = here.split("KiCad.app")[0] + "KiCad.app/Contents/MacOS/kicad-cli"
        if os.path.exists(c):
            return c
    for c in (DEFAULT_KICAD_CLI, shutil.which("kicad-cli")):
        if c and os.path.exists(c):
            return c
    sys.exit("kicad-cli not found — pass --kicad-cli PATH")


def import_pcbnew():
    try:
        import pcbnew
        return pcbnew
    except ImportError:
        sys.exit("pcbnew not importable — run with KiCad's bundled python3 "
                 "(/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3)")


def fab_shapes(pcbnew, board):
    """Closed PCB_SHAPEs on F.Fab/B.Fab, from footprints and board drawings."""
    fab = {pcbnew.F_Fab, pcbnew.B_Fab}
    closed = {pcbnew.SHAPE_T_RECT, pcbnew.SHAPE_T_CIRCLE, pcbnew.SHAPE_T_POLY}
    items = []
    for fp in board.GetFootprints():
        items.extend(fp.GraphicalItems())
    items.extend(board.GetDrawings())
    for it in items:
        if isinstance(it, pcbnew.PCB_SHAPE) and it.GetLayer() in fab and it.GetShape() in closed:
            yield it


def synthesize_bodies(pcbnew, board, exclude):
    """Fab-body fallback for footprints WITHOUT a usable 3D model (exclude =
    refs that already got a real 3D silhouette). Uses the library's closed fab
    shape, else the ACTUAL courtyard polygon (slightly deflated), else a
    pad-extent rect as a last resort."""
    closed = {pcbnew.SHAPE_T_RECT, pcbnew.SHAPE_T_CIRCLE, pcbnew.SHAPE_T_POLY}
    min_dim = pcbnew.FromMM(0.25)
    # bare-pad hardware: a body outline around a lone pad is just noise
    skip_pat = re.compile(r"solderpad|testpoint|test_point|fiducial|mountinghole|castellat", re.I)
    eb = board.GetBoardEdgesBoundingBox()
    max_w, max_h = int(eb.GetWidth() * 0.6), int(eb.GetHeight() * 0.6)
    polys = []  # (fab_layer, [(x, y), ...]) — plain ints; BOX2I here segfaults
    rects = []  # (fab_layer, l, t, r, b)
    for fp in board.GetFootprints():
        if fp.GetReference() in exclude:
            continue
        if skip_pat.search(str(fp.GetFPID().GetLibItemName())) or \
                re.match(r"TP\d|FID\d|H\d", fp.GetReference()):
            continue
        front = fp.GetLayer() == pcbnew.F_Cu
        fab = pcbnew.F_Fab if front else pcbnew.B_Fab
        crt = pcbnew.F_CrtYd if front else pcbnew.B_CrtYd
        has_body = False
        ext = None  # (l, t, r, b) of open fab drawings
        for it in list(fp.GraphicalItems()):
            if not isinstance(it, pcbnew.PCB_SHAPE) or it.GetLayer() != fab:
                continue
            b = it.GetBoundingBox()
            if it.GetShape() in closed and min(b.GetWidth(), b.GetHeight()) > pcbnew.FromMM(0.8):
                has_body = True
                break
            l, t, r, bt = b.GetLeft(), b.GetTop(), b.GetRight(), b.GetBottom()
            ext = (l, t, r, bt) if ext is None else \
                (min(ext[0], l), min(ext[1], t), max(ext[2], r), max(ext[3], bt))
        if has_body:
            continue
        court = fp.GetCourtyard(crt)
        if court.OutlineCount() > 0:
            c = pcbnew.SHAPE_POLY_SET(court)
            try:
                c.Inflate(-pcbnew.FromMM(0.15), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
                          pcbnew.FromMM(0.01))
            except Exception:
                pass
            for i in range(c.OutlineCount()):
                ch = c.Outline(i)
                pts = [(ch.CPoint(j).x, ch.CPoint(j).y) for j in range(ch.PointCount())]
                if len(pts) >= 3:
                    polys.append((fab, pts))
            continue
        if ext is None or min(ext[2] - ext[0], ext[3] - ext[1]) < min_dim:
            # no courtyard, no usable fab drawing — pad extent, pulled in so the
            # perimeter pads peek out; never for single-pad or board-sized parts
            pads = list(fp.Pads())
            if len(pads) < 2:
                continue
            xs, ys = [], []
            for p in pads:
                pb = p.GetBoundingBox()
                xs += [pb.GetLeft(), pb.GetRight()]
                ys += [pb.GetTop(), pb.GetBottom()]
            ext = (min(xs), min(ys), max(xs), max(ys))
            if ext[2] - ext[0] > max_w and ext[3] - ext[1] > max_h:
                continue  # spans the board (pad-array footprint) — not a body
            deflate = min(pcbnew.FromMM(0.35),
                          (min(ext[2] - ext[0], ext[3] - ext[1]) - min_dim) // 2)
            ext = (ext[0] + deflate, ext[1] + deflate, ext[2] - deflate, ext[3] - deflate)
        if min(ext[2] - ext[0], ext[3] - ext[1]) < min_dim:
            continue
        rects.append((fab, *ext))
    for fab, pts in polys:
        poly = pcbnew.PCB_SHAPE(board)
        poly.SetShape(pcbnew.SHAPE_T_POLY)
        poly.SetPolyPoints([pcbnew.VECTOR2I(x, y) for x, y in pts])
        poly.SetLayer(fab)
        poly.SetWidth(pcbnew.FromMM(0.12))
        board.Add(poly)
    for fab, l, t, r, b in rects:
        rect = pcbnew.PCB_SHAPE(board)
        rect.SetShape(pcbnew.SHAPE_T_RECT)
        rect.SetStart(pcbnew.VECTOR2I(l, t))
        rect.SetEnd(pcbnew.VECTOR2I(r, b))
        rect.SetLayer(fab)
        rect.SetWidth(pcbnew.FromMM(0.12))
        board.Add(rect)
    return len(polys) + len(rects)


def normalize_edge(pcbnew, board, width_mm):
    """Force every Edge.Cuts graphic to one stroke width so the board outline
    renders with a consistent thickness."""
    w = pcbnew.FromMM(width_mm)
    items = list(board.GetDrawings())
    for fp in board.GetFootprints():
        items.extend(fp.GraphicalItems())
    for it in items:
        if isinstance(it, pcbnew.PCB_SHAPE) and it.GetLayer() == pcbnew.Edge_Cuts:
            it.SetWidth(w)


KICAD_3DMODELS = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels"
VRML_UNIT_MM = 2.54  # VRML unit in KiCad models is 0.1 inch


def resolve_model_path(filename, proj_dir):
    fn = re.sub(r"\$\{KIPRJMOD\}", proj_dir, filename)
    fn = re.sub(r"\$\{KICAD\d*_3DMODEL_DIR\}", KICAD_3DMODELS, fn)
    fn = re.sub(r"\$\{KISYS3DMOD\}", KICAD_3DMODELS, fn)
    stem = os.path.splitext(fn)[0]
    # prefer the tessellated mesh (.wrl) even when the footprint references the
    # .step — STEP files carry construction geometry that pollutes silhouettes
    for cand in (stem + ".wrl", stem + ".wrz", fn, stem + ".step", stem + ".stp", stem + ".STEP"):
        if os.path.exists(cand):
            return cand
    return None


def model_points(path, cache):
    """All 3D vertices of a model file, in mm, model space. Text-parsed:
    VRML 'point [...]' blocks, STEP CARTESIAN_POINTs. Cached per file."""
    if path in cache:
        return cache[path]
    pts = []
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".wrl", ".wrz"):
            raw = gzip.open(path, "rt", errors="ignore").read() if ext == ".wrz" \
                else open(path, errors="ignore").read()
            for block in re.findall(r"point\s*\[(.*?)\]", raw, re.S):
                nums = [float(v) for v in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", block)]
                pts += [(nums[i] * VRML_UNIT_MM, nums[i + 1] * VRML_UNIT_MM,
                         nums[i + 2] * VRML_UNIT_MM) for i in range(0, len(nums) - 2, 3)]
        else:  # STEP is mm
            raw = open(path, errors="ignore").read()
            for triple in re.findall(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)", raw):
                nums = [float(v) for v in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", triple)]
                if len(nums) == 3:
                    pts.append(tuple(nums))
    except Exception:
        pts = []
    cache[path] = pts
    return pts


def parse_wrl_mesh(path):
    """Triangles from a VRML file's IndexedFaceSets, in mm, model space.
    Returns a list of ((x,y,z), (x,y,z), (x,y,z)) or None."""
    try:
        raw = gzip.open(path, "rt", errors="ignore").read() if path.endswith(".wrz") \
            else open(path, errors="ignore").read()
    except Exception:
        return None
    point_blocks = [(m.start(), m.group(1))
                    for m in re.finditer(r"point\s*\[(.*?)\]", raw, re.S)]
    idx_blocks = [(m.start(), m.group(1))
                  for m in re.finditer(r"coordIndex\s*\[(.*?)\]", raw, re.S)]
    if not point_blocks or not idx_blocks:
        return None
    tris = []
    for ipos, iblock in idx_blocks:
        pts_src = None
        for ppos, pblock in point_blocks:
            if ppos < ipos:
                pts_src = pblock
            else:
                break
        if pts_src is None:
            continue
        nums = [float(v) for v in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", pts_src)]
        verts = [(nums[i] * VRML_UNIT_MM, nums[i + 1] * VRML_UNIT_MM, nums[i + 2] * VRML_UNIT_MM)
                 for i in range(0, len(nums) - 2, 3)]
        idx = [int(v) for v in re.findall(r"-?\d+", iblock)]
        face = []
        for k in idx:
            if k == -1:
                for j in range(1, len(face) - 1):  # fan-triangulate
                    tris.append((face[0], face[j], face[j + 1]))
                face = []
            elif k < len(verts):
                face.append(verts[k])
        for j in range(1, len(face) - 1):
            tris.append((face[0], face[j], face[j + 1]))
    return tris or None


def mesh_outlines(pcbnew, path, scale, rot, cache):
    """EXACT 2D silhouette of a mesh model: project every triangle to the board
    plane and union them with pcbnew's polygon engine. Returns a list of outline
    point lists (mm, model space, y-up) or None if the file has no mesh."""
    key = (path, scale, rot)
    if key in cache:
        return cache[key]
    result = None
    tris = parse_wrl_mesh(path) if path.lower().endswith((".wrl", ".wrz")) else None
    if tris:
        sx, sy, sz = scale
        poly = pcbnew.SHAPE_POLY_SET()
        n = 0
        for tri in tris:
            p3 = [(x * sx, y * sy, z * sz) for x, y, z in tri]
            p3 = _rot3(p3, -rot[0], "x")
            p3 = _rot3(p3, -rot[1], "y")
            p3 = _rot3(p3, -rot[2], "z")
            (x1, y1, _), (x2, y2, _), (x3, y3, _) = p3
            area2 = (x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)
            if abs(area2) < 1e-4:
                # edge-on face (vertical wall). Its extent still bounds the
                # silhouette — models without a bottom face would otherwise
                # collapse to their smaller top face. Emit it as a thin strip.
                pts = sorted(((x1, y1), (x2, y2), (x3, y3)))
                (ax, ay), (bx, by) = pts[0], pts[-1]
                dx, dy = bx - ax, by - ay
                ln = math.hypot(dx, dy)
                if ln < 1e-6:
                    continue
                px_, py_ = -dy / ln * 0.01, dx / ln * 0.01
                quad = ((ax + px_, ay + py_), (bx + px_, by + py_),
                        (bx - px_, by - py_), (ax - px_, ay - py_))
                ch = pcbnew.SHAPE_LINE_CHAIN()
                for x, y in quad:
                    ch.Append(int(x * 1e6), int(y * 1e6))
                ch.SetClosed(True)
                poly.AddOutline(ch)
                n += 1
                continue
            a, b, c = ((x1, y1), (x2, y2), (x3, y3)) if area2 > 0 else \
                ((x1, y1), (x3, y3), (x2, y2))
            ch = pcbnew.SHAPE_LINE_CHAIN()
            for x, y in (a, b, c):
                ch.Append(int(x * 1e6), int(y * 1e6))
            ch.SetClosed(True)
            poly.AddOutline(ch)
            n += 1
        if n:
            poly.Simplify()
            outlines = []
            for i in range(poly.OutlineCount()):
                ch = poly.Outline(i)
                pts = [(ch.CPoint(j).x / 1e6, ch.CPoint(j).y / 1e6)
                       for j in range(ch.PointCount())]
                if len(pts) >= 3:
                    # drop dust (loose mesh fragments under 0.02 mm²)
                    area = abs(sum(pts[k][0] * pts[k - 1][1] - pts[k - 1][0] * pts[k][1]
                                   for k in range(len(pts)))) / 2
                    if area > 0.02:
                        outlines.append(pts)
            result = outlines or None
    cache[key] = result
    return result


def convex_hull(points):
    """Monotone chain, 2D."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return None
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _rot3(pts, deg, axis):
    if not deg:
        return pts
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    if axis == "x":
        return [(x, y * c - z * s, y * s + z * c) for x, y, z in pts]
    if axis == "y":
        return [(x * c + z * s, y, -x * s + z * c) for x, y, z in pts]
    return [(x * c - y * s, x * s + y * c, z) for x, y, z in pts]


def collect_hulls(pcbnew, board, proj_dir):
    """Real component silhouettes: each footprint's 3D model vertices projected
    to the board plane, convex-hulled, placed at the footprint position.
    proj_dir = the ORIGINAL project dir (${KIPRJMOD}) — the board argument is a
    temp copy, so its own path must not be used to resolve models.
    Returns ({'top': [poly_mm...], 'bottom': [...]}, set_of_refs_with_hulls)."""
    eb = board.GetBoardEdgesBoundingBox()
    max_w = pcbnew.ToMM(eb.GetWidth()) * 0.6
    max_h = pcbnew.ToMM(eb.GetHeight()) * 0.6
    cache = {}
    hulls = {"top": [], "bottom": []}
    have = set()
    for fp in board.GetFootprints():
        if getattr(fp, "IsDNP", lambda: False)():
            continue
        back = fp.GetLayer() != pcbnew.F_Cu
        side = "bottom" if back else "top"
        pts2d = []
        exact = []  # exact mesh silhouettes (model space, offset applied)
        for m in fp.Models():
            if not m.m_Show:
                continue
            path = resolve_model_path(m.m_Filename, proj_dir)
            if not path:
                continue
            scale = (m.m_Scale.x, m.m_Scale.y, m.m_Scale.z)
            rot = (m.m_Rotation.x, m.m_Rotation.y, m.m_Rotation.z)
            ox, oy = m.m_Offset.x, m.m_Offset.y  # offset lives in model space (y-up)
            outlines = mesh_outlines(pcbnew, path, scale, rot, cache)
            if outlines:
                exact += [[(x + ox, y + oy) for x, y in o] for o in outlines]
                continue
            pts = model_points(path, cache)
            if not pts:
                continue
            sx, sy, sz = scale
            p3 = [(x * sx, y * sy, z * sz) for x, y, z in pts]
            p3 = _rot3(p3, -rot[0], "x")
            p3 = _rot3(p3, -rot[1], "y")
            p3 = _rot3(p3, -rot[2], "z")
            pts2d += [(x + ox, y + oy) for x, y, _ in p3]
        if not pts2d and not exact:
            continue
        cand = list(exact)
        if pts2d:
            if len(pts2d) > 100:
                # STEP files carry a few construction/reference points outside
                # the real body (axis markers, land-pattern rings) — trim the
                # sparse extremes before hulling. Real edges are dense.
                sx_ = sorted(p[0] for p in pts2d)
                sy_ = sorted(p[1] for p in pts2d)
                lo = max(1, int(len(sx_) * 0.02))
                x0, x1 = sx_[lo], sx_[-1 - lo]
                y0, y1 = sy_[lo], sy_[-1 - lo]
                mx = (x1 - x0) * 0.05 + 0.05
                my = (y1 - y0) * 0.05 + 0.05
                kept = [(x, y) for x, y in pts2d
                        if x0 - mx <= x <= x1 + mx and y0 - my <= y <= y1 + my]
                if len(kept) >= 3:
                    pts2d = kept
            hull = convex_hull(pts2d)
            if hull:
                cand.append(hull)
        if not cand:
            continue
        ang = math.radians(fp.GetOrientationDegrees())
        c, s = math.cos(ang), math.sin(ang)
        px, py = pcbnew.ToMM(fp.GetPosition().x), pcbnew.ToMM(fp.GetPosition().y)
        world_polys = []
        xs, ys = [], []
        for poly in cand:
            # model space is y-up; board space is y-down: front = (x, -y).
            # A flipped footprint rotates the model 180° about the board X axis,
            # which lands at (x, +y) in board coords — NOT a plain x-mirror.
            local = [(x, y) for x, y in poly] if back else [(x, -y) for x, y in poly]
            # rotation is CCW-positive in board coords (y down)
            world = [(px + x * c + y * s, py - x * s + y * c) for x, y in local]
            world_polys.append(world)
            xs += [p[0] for p in world]
            ys += [p[1] for p in world]
        if max(xs) - min(xs) > max_w and max(ys) - min(ys) > max_h:
            continue  # broken/oversized model
        # sanity vs the courtyard: a silhouette that spills far outside it or is
        # tiny inside it comes from a polluted/mis-scaled model — fall back
        crt = pcbnew.B_CrtYd if back else pcbnew.F_CrtYd
        court = fp.GetCourtyard(crt)
        if court.OutlineCount() > 0:
            cb = court.BBox()
            cl, ct = pcbnew.ToMM(cb.GetLeft()), pcbnew.ToMM(cb.GetTop())
            cr, cbt = pcbnew.ToMM(cb.GetRight()), pcbnew.ToMM(cb.GetBottom())
            slack = 2.0
            if min(xs) < cl - slack or max(xs) > cr + slack or \
                    min(ys) < ct - slack or max(ys) > cbt + slack:
                print(f"    !! {fp.GetReference()}: 3D silhouette spills outside its "
                      f"courtyard — dropped, courtyard fallback used")
                continue
            if (max(xs) - min(xs)) < 0.3 * (cr - cl) and \
                    (max(ys) - min(ys)) < 0.3 * (cbt - ct):
                print(f"    !! {fp.GetReference()}: 3D silhouette implausibly small — "
                      f"dropped, courtyard fallback used")
                continue
        hulls[side].extend(world_polys)
        have.add(fp.GetReference())
    return hulls, have


MARKER_MARGIN_MM = 5.0  # calibration dots sit this far outside the board bbox


def add_markers(pcbnew, board):
    """Plant two tiny dots outside the board on every plotted layer. They pin
    the content bbox of each SVG export to known mm positions, giving an exact
    mm -> SVG mapping (the plot origin is otherwise content-dependent). They
    fall outside the board clip, so they never appear in the final art.
    Returns (xA, yA, xB, yB) in mm."""
    bb = board.ComputeBoundingBox(False)  # ALL content — stray notes outside the
    # board must not out-extreme the markers, or the calibration breaks
    m = pcbnew.FromMM(MARKER_MARGIN_MM)
    ax, ay = bb.GetLeft() - m, bb.GetTop() - m
    bx, by = bb.GetRight() + m, bb.GetBottom() + m
    r = pcbnew.FromMM(0.005)
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu, pcbnew.F_Fab, pcbnew.B_Fab):
        for x, y in ((ax, ay), (bx, by)):
            dot = pcbnew.PCB_SHAPE(board)
            dot.SetShape(pcbnew.SHAPE_T_CIRCLE)
            dot.SetCenter(pcbnew.VECTOR2I(x, y))
            dot.SetEnd(pcbnew.VECTOR2I(x + r, y))
            dot.SetFilled(True)
            dot.SetWidth(0)
            dot.SetLayer(layer)
            board.Add(dot)
    return (pcbnew.ToMM(ax), pcbnew.ToMM(ay), pcbnew.ToMM(bx), pcbnew.ToMM(by))


def prepare_art_copy(path, keep_traces, edge_width, proj_dir):
    """Strip zones/vias (and tracks unless kept), strip fab text, move
    mask-layer designer text to silk, force fab shapes unfilled (outline pass;
    small circles stay filled so pin-1 dots read as solid dots).
    Returns (markers_mm, contours_mm, hulls) for SVG clipping/calibration and
    the real 3D component silhouettes."""
    pcbnew = import_pcbnew()
    board = pcbnew.LoadBoard(path)
    for z in list(board.Zones()):
        board.RemoveNative(z)
    for t in list(board.GetTracks()):
        if isinstance(t, pcbnew.PCB_VIA) or not keep_traces:
            board.RemoveNative(t)
    fab = {pcbnew.F_Fab, pcbnew.B_Fab}
    mask2silk = {pcbnew.F_Mask: pcbnew.F_SilkS, pcbnew.B_Mask: pcbnew.B_SilkS}
    for fp in board.GetFootprints():
        for it in list(fp.GraphicalItems()):
            if hasattr(it, "GetText") and it.GetLayer() in fab:
                fp.RemoveNative(it)
            elif it.GetLayer() in mask2silk:
                # designer text AND graphics on the mask layers (copper-pour
                # cutout logos, art) render like silkscreen
                it.SetLayer(mask2silk[it.GetLayer()])
        for f in fp.GetFields():
            if f.GetLayer() in fab:
                f.SetVisible(False)
    for d in list(board.GetDrawings()):
        if hasattr(d, "GetText") and d.GetLayer() in fab:
            board.RemoveNative(d)
        elif d.GetLayer() in mask2silk:
            d.SetLayer(mask2silk[d.GetLayer()])
    hulls, have3d = collect_hulls(pcbnew, board, proj_dir)
    print(f"    3D silhouettes: {len(have3d)} footprints "
          f"({len(hulls['top'])} top, {len(hulls['bottom'])} bottom)")
    for fp in board.GetFootprints():
        # parts with a real silhouette drop ALL their fab drawings — the 3D
        # hull replaces the body, and leftover open segments/arrows would draw
        # mismatched fragments over it. Only pin-1 dots (small circles) stay.
        if fp.GetReference() not in have3d:
            continue
        f = pcbnew.F_Fab if fp.GetLayer() == pcbnew.F_Cu else pcbnew.B_Fab
        for it in list(fp.GraphicalItems()):
            if not isinstance(it, pcbnew.PCB_SHAPE) or it.GetLayer() != f:
                continue
            pin1_dot = (it.GetShape() == pcbnew.SHAPE_T_CIRCLE
                        and it.GetRadius() <= pcbnew.FromMM(0.6))
            if not pin1_dot:
                fp.RemoveNative(it)
    n = synthesize_bodies(pcbnew, board, have3d)
    if n:
        print(f"    synthesized {n} fallback bodies (no 3D model)")
    closed = {pcbnew.SHAPE_T_RECT, pcbnew.SHAPE_T_CIRCLE, pcbnew.SHAPE_T_POLY}
    for fp in board.GetFootprints():
        # open fab drawings (polarity arrows, outline fragments) just add noise
        # next to the bodies — drop them from every footprint
        for it in list(fp.GraphicalItems()):
            if isinstance(it, pcbnew.PCB_SHAPE) and it.GetLayer() in fab \
                    and it.GetShape() not in closed:
                fp.RemoveNative(it)
    for d in list(board.GetDrawings()):
        # board-level fab graphics are documentation (motor-direction arrows,
        # assembly notes) — never part of the art
        if isinstance(d, pcbnew.PCB_SHAPE) and d.GetLayer() in fab:
            board.RemoveNative(d)
    normalize_edge(pcbnew, board, edge_width)
    for it in fab_shapes(pcbnew, board):
        pin1_dot = (it.GetShape() == pcbnew.SHAPE_T_CIRCLE
                    and it.GetRadius() <= pcbnew.FromMM(0.6))
        it.SetFilled(pin1_dot)
    markers = add_markers(pcbnew, board)
    pcbnew.SaveBoard(path, board)

    has_edge = any(d.GetLayer() == pcbnew.Edge_Cuts for d in list(board.GetDrawings())) or \
        any(it.GetLayer() == pcbnew.Edge_Cuts
            for fp in board.GetFootprints() for it in list(fp.GraphicalItems()))
    if not has_edge:
        print("    !! no Edge.Cuts on this board — outline clip skipped, art is unclipped")
        return markers, None, hulls

    poly = pcbnew.SHAPE_POLY_SET()
    board.GetBoardPolygonOutlines(poly, True)
    try:  # grow slightly so the outline stroke itself survives the clip
        poly.Inflate(pcbnew.FromMM(0.15), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
                     pcbnew.FromMM(0.01))
    except Exception:
        pass
    contours = []
    for i in range(poly.OutlineCount()):
        chain = poly.Outline(i)  # outer contours only; hole cutouts keep their edge art
        pts = [(pcbnew.ToMM(chain.CPoint(j).x), pcbnew.ToMM(chain.CPoint(j).y))
               for j in range(chain.PointCount())]
        if len(pts) >= 3:
            contours.append(pts)
    return markers, contours, hulls


def prepare_bodies_copy(path):
    """Force all closed fab shapes filled (component-body knockout pass)."""
    pcbnew = import_pcbnew()
    board = pcbnew.LoadBoard(path)
    for it in fab_shapes(pcbnew, board):
        it.SetFilled(True)
    pcbnew.SaveBoard(path, board)


def export_svg(cli, pcb, side, layers, out, drill):
    cmd = [cli, "pcb", "export", "svg", "--mode-single", "-o", out,
           "--layers", layers, "--black-and-white", "--exclude-drawing-sheet",
           "--page-size-mode", "2", "--drill-shape-opt", str(drill), pcb]
    if side == "bottom":
        cmd.insert(-1, "--mirror")  # back view as seen when flipping the board over
    subprocess.run(cmd, check=True, capture_output=True)


def svg_inner(path):
    return re.search(r"<svg\b[^>]*>(.*)</svg>", open(path).read(), re.S).group(1)


def recolor(s, black_to, white_to=None):
    s = re.sub(r"(fill|stroke):#000000", r"\1:" + black_to, s, flags=re.I)
    if white_to:
        s = re.sub(r"(fill|stroke):#FFFFFF", r"\1:" + white_to, s, flags=re.I)
    return s


def find_marker_circles(svg_text):
    """The two calibration dots, located by their unique tiny radius."""
    circles = [(float(cx), float(cy), float(r)) for cx, cy, r in
               re.findall(r'<circle cx="([-0-9.]+)" cy="([-0-9.]+)" r="([-0-9.]+)"', svg_text)]
    if not circles:
        return None
    rmin = min(r for _, _, r in circles)
    pts = sorted({(cx, cy) for cx, cy, r in circles if r <= rmin * 1.5})
    if len(pts) != 2:
        return None
    return pts


def mm_mapping(svg_text, markers_mm):
    """mm -> SVG user-unit mapping calibrated on the marker dots. Handles the
    mirrored bottom plot transparently (sx comes out negative)."""
    xa, ya, xb, yb = markers_mm
    pts = find_marker_circles(svg_text)
    if pts is None:
        return None
    # marker A is the top one; y is never mirrored
    (ax, ay), (bx, by) = sorted(pts, key=lambda p: p[1])
    sx = (bx - ax) / (xb - xa)
    sy = (by - ay) / (yb - ya)
    fx = lambda x: ax + (x - xa) * sx
    fy = lambda y: ay + (y - ya) * sy
    return fx, fy, abs(sx)


def clip_path_svg(base_text, side, markers_mm, contours_mm):
    mapping = mm_mapping(base_text, markers_mm)
    if mapping is None:
        return "", "", None, None
    fx, fy, sx = mapping
    d = []
    ext = None
    for pts in contours_mm:
        cmds = []
        for k, (x, y) in enumerate(pts):
            u, v = fx(x), fy(y)
            ext = (u, v, u, v) if ext is None else \
                (min(ext[0], u), min(ext[1], v), max(ext[2], u), max(ext[3], v))
            cmds.append(f"{'M' if k == 0 else 'L'}{u:.3f} {v:.3f}")
        d.append("".join(cmds) + "Z")
    cp = (f'<defs><clipPath id="boardclip" clip-rule="evenodd">'
          f'<path fill-rule="evenodd" d="{" ".join(d)}"/></clipPath></defs>')
    return cp, ' clip-path="url(#boardclip)"', ext, sx


def tighten_head(head, ext, sx, pad_mm=0.25):
    """Rewrite width/height/viewBox so the page hugs the clipped board."""
    p = pad_mm * sx
    x, y = ext[0] - p, ext[1] - p
    w, h = ext[2] - ext[0] + 2 * p, ext[3] - ext[1] + 2 * p
    head = re.sub(r'width="[0-9.]+mm"', f'width="{w / sx:.4f}mm"', head)
    head = re.sub(r'height="[0-9.]+mm"', f'height="{h / sx:.4f}mm"', head)
    head = re.sub(r'viewBox="[^"]*"', f'viewBox="{x:.3f} {y:.3f} {w:.3f} {h:.3f}"', head)
    return head


def hulls_svg(base_text, markers_mm, hulls_mm, color, body):
    """Component 3D silhouettes as white bodies with a gold outline, drawn over
    the copper like the parts sit on the assembled board."""
    mapping = mm_mapping(base_text, markers_mm)
    if mapping is None or not hulls_mm:
        return ""
    fx, fy, sx = mapping
    paths = []
    for hull in hulls_mm:
        d = "".join(f"{'M' if i == 0 else 'L'}{fx(x):.3f} {fy(y):.3f}"
                    for i, (x, y) in enumerate(hull)) + "Z"
        paths.append(f'<path d="{d}" style="fill:{body}; stroke:{color}; '
                     f'stroke-width:{0.12 * sx:.3f}; stroke-linejoin:round;"/>')
    return "".join(paths)


def composite(base_svg, fill_svg, outline_svg, out, color, holes, body, side,
              markers, contours, hulls_mm):
    base_text = open(base_svg).read()
    head = re.match(r"(.*?<svg\b[^>]*>)", base_text, re.S).group(1)
    cp, attr = "", ""
    if contours:
        cp, attr, ext, sx = clip_path_svg(base_text, side, markers, contours)
        if ext is not None:
            head = tighten_head(head, ext, sx)
    # fallback bodies UNDER the copper (parts without 3D models), then copper,
    # then the real 3D silhouettes ON TOP (they physically cover their pads),
    # then fab outlines/pin-1 dots
    parts = [head, cp, f"<g{attr}>"]
    if fill_svg:
        parts.append(recolor(svg_inner(fill_svg), body))
    parts.append(recolor(svg_inner(base_svg), color, holes))
    parts.append(hulls_svg(base_text, markers, hulls_mm, color, body))
    if fill_svg:
        parts.append(recolor(svg_inner(outline_svg), color))
    parts.append("</g></svg>")
    open(out, "w").write("".join(parts))


def rasterize(svg_path, size, bg):
    png = os.path.splitext(svg_path)[0] + ".png"
    rsvg = shutil.which("rsvg-convert") or "/opt/homebrew/bin/rsvg-convert"
    if os.path.exists(rsvg):
        cmd = [rsvg, "-w", str(size), svg_path, "-o", png]
        if bg != "transparent":
            cmd[1:1] = ["-b", bg]
        subprocess.run(cmd, check=True)
        return png
    magick = shutil.which("magick")
    if magick:
        subprocess.run([magick, "-background", bg if bg != "transparent" else "none",
                        "-density", "300", svg_path, "-resize", str(size), png], check=True)
        return png
    print("    (no rsvg-convert or magick — PNG preview skipped)")
    return None


def main():
    ap = argparse.ArgumentParser(description="Flat single-color vector board art (SVG) for packaging.")
    ap.add_argument("pcb", help="path to the .kicad_pcb (never modified)")
    ap.add_argument("--top", help="output SVG path for the top/front art")
    ap.add_argument("--bottom", help="output SVG path for the bottom/back art")
    ap.add_argument("--outdir", help="dir for default-named outputs (<stem>-art-front.svg / -back.svg)")
    ap.add_argument("--sides", default="top,bottom", help="comma list: top,bottom (default both)")
    ap.add_argument("--color", default=GOLD, help=f"artwork color (default packaging gold {GOLD})")
    ap.add_argument("--holes", default="#FFFFFF",
                    help="drill-hole knockout color (default #FFFFFF; use the box background color)")
    ap.add_argument("--body", default="#F2E7C9",
                    help="component body fill (default a light gold tint so bodies read as "
                         "solid shapes; on a dark box use a dark tint of the background)")
    ap.add_argument("--keep-traces", action="store_true", help="keep copper tracks (hidden by default)")
    ap.add_argument("--no-components", action="store_true",
                    help="skip the Fab component bodies, pads/silk art only")
    ap.add_argument("--no-clip", action="store_true", help="do not clip artwork to the board outline")
    ap.add_argument("--edge-width", type=float, default=0.3,
                    help="board outline stroke width in mm, applied uniformly (default 0.3)")
    ap.add_argument("--png", action="store_true", help="also rasterize a PNG preview next to each SVG")
    ap.add_argument("--png-size", type=int, default=1600, help="PNG width in px (default 1600)")
    ap.add_argument("--png-bg", default="transparent", help="PNG background (default transparent)")
    ap.add_argument("--kicad-cli", help="path to kicad-cli (auto-detected by default)")
    args = ap.parse_args()

    pcb = os.path.abspath(args.pcb)
    if not pcb.endswith(".kicad_pcb") or not os.path.exists(pcb):
        sys.exit(f"not a .kicad_pcb: {pcb}")

    sides = [s.strip() for s in args.sides.split(",") if s.strip()]
    bad = [s for s in sides if s not in BASE_LAYERS]
    if bad:
        sys.exit(f"unknown side(s): {','.join(bad)} (use top,bottom)")

    stem = os.path.splitext(os.path.basename(pcb))[0]
    outdir = args.outdir or os.path.dirname(pcb)
    name = {"top": "front", "bottom": "back"}
    outputs = {}
    for side in sides:
        explicit = getattr(args, side)
        outputs[side] = os.path.abspath(explicit) if explicit else \
            os.path.abspath(os.path.join(outdir, f"{stem}-art-{name[side]}.svg"))

    cli = find_kicad_cli(args.kicad_cli)

    print(f">>> {pcb}")
    with tempfile.TemporaryDirectory(prefix="packaging_art_") as tmp:
        art = os.path.join(tmp, "art.kicad_pcb")
        shutil.copy2(pcb, art)
        markers, contours, hulls = prepare_art_copy(art, args.keep_traces, args.edge_width,
                                            os.path.dirname(pcb))
        if args.no_clip:
            contours = None
        bodies = None
        if not args.no_components:
            bodies = os.path.join(tmp, "bodies.kicad_pcb")
            shutil.copy2(art, bodies)
            prepare_bodies_copy(bodies)
        for side, out in outputs.items():
            os.makedirs(os.path.dirname(out), exist_ok=True)
            base = os.path.join(tmp, f"base-{side}.svg")
            export_svg(cli, art, side, BASE_LAYERS[side], base, drill=2)
            fill = outline = None
            if bodies:
                fill = os.path.join(tmp, f"fill-{side}.svg")
                outline = os.path.join(tmp, f"outline-{side}.svg")
                # no drill marks on fab passes — they'd plot as solid discs
                export_svg(cli, bodies, side, FAB_LAYERS[side], fill, drill=0)
                export_svg(cli, art, side, FAB_LAYERS[side], outline, drill=0)
            composite(base, fill, outline, out, args.color, args.holes, args.body,
                      side, markers, contours, hulls[side])
            print(f"    {side} -> {out}")
            if args.png:
                png = rasterize(out, args.png_size, args.png_bg)
                if png:
                    print(f"           {png}")


if __name__ == "__main__":
    main()
