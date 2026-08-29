#!/usr/bin/env python3
"""Assembly drawings for fab reviewers: one image per side with every pad,
pin 1 in red on every part where orientation matters (more than two pads, or
a D/Q/U/X reference), reference designators, fab/silk outlines and the board
edge (board drawings and footprint edge items, the ESCs keep theirs in a
footprint). Made after the first external turnkey RFQs, when every fab asked
how to verify polarity and rotation from a positions file alone.

Reads the board with the pcbnew API (read only), writes SVG, then PNG via
rsvg-convert or ImageMagick if either is on PATH.

Usage (KiCad's bundled Python, see README "Interpreter"):
    $KPY assembly_drawing.py <board.kicad_pcb> [--out DIR] [--stem NAME]
                             [--dpi 600] [--no-png]

Output: <DIR>/<stem>_assembly_top.svg|png and <stem>_assembly_bottom.svg|png.
The bottom view is MIRRORED (viewed from below, as the assembler sees it) and
says so in its title. Pin 1 is the pad numbered "1" or "A1". Plain 2-pad
passives (R, C, L) get no red; 2-pad D/Q/U/X parts do (pad 1 = cathode for
diodes in KiCad libraries). Parts with exclude_from_bom or DNP are drawn
hatched grey and listed in the title.
"""
import argparse, math, os, shutil, subprocess, sys

try:
    import pcbnew
except ImportError:
    sys.exit("needs KiCad's bundled Python (pcbnew); see OpenDrone-Scripts README")

MM = 1e6  # pcbnew internal units per mm


def mm(v):
    return v / MM


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def pad_polygon(pad, layer):
    """Pad outline as list of (x, y) in mm, from the pad's effective polygon."""
    poly = pad.GetEffectivePolygon(layer)
    pts = []
    if poly.OutlineCount() == 0:
        return pts
    o = poly.Outline(0)
    for i in range(o.PointCount()):
        p = o.CPoint(i)
        pts.append((mm(p.x), mm(p.y)))
    return pts


def shape_segments(item):
    """Line segments (mm) approximating a PCB_SHAPE / FP_SHAPE."""
    segs = []
    st = item.GetShape()
    if st == pcbnew.SHAPE_T_SEGMENT:
        a, b = item.GetStart(), item.GetEnd()
        segs.append(((mm(a.x), mm(a.y)), (mm(b.x), mm(b.y))))
    elif st in (pcbnew.SHAPE_T_RECT,):
        pts = [item.GetCorners()[i] for i in range(4)]
        for i in range(4):
            a, b = pts[i], pts[(i + 1) % 4]
            segs.append(((mm(a.x), mm(a.y)), (mm(b.x), mm(b.y))))
    elif st in (pcbnew.SHAPE_T_ARC, pcbnew.SHAPE_T_CIRCLE, pcbnew.SHAPE_T_POLY,
                pcbnew.SHAPE_T_BEZIER):
        try:
            poly = pcbnew.SHAPE_POLY_SET()
            item.TransformShapeToPolygon(poly, item.GetLayer(), 0, 5000, pcbnew.ERROR_INSIDE)
            for k in range(poly.OutlineCount()):
                o = poly.Outline(k)
                n = o.PointCount()
                for i in range(n):
                    a, b = o.CPoint(i), o.CPoint((i + 1) % n)
                    segs.append(((mm(a.x), mm(a.y)), (mm(b.x), mm(b.y))))
        except Exception:
            pass
    return segs


def draw_side(board, side, stem, out_dir, dpi, png):
    top = side == "top"
    cu = pcbnew.F_Cu if top else pcbnew.B_Cu
    fab = pcbnew.F_Fab if top else pcbnew.B_Fab
    silk = pcbnew.F_SilkS if top else pcbnew.B_SilkS
    crt = pcbnew.F_CrtYd if top else pcbnew.B_CrtYd

    bb = board.GetBoardEdgesBoundingBox()
    x0, y0 = mm(bb.GetX()), mm(bb.GetY())
    w, h = mm(bb.GetWidth()), mm(bb.GetHeight())
    margin = 3.0
    # bottom view: mirror X about the board centre
    cx = x0 + w / 2

    def X(x):
        return x if top else 2 * cx - x

    edge, fabs, silks, pads, pin1, refs, dnp = [], [], [], [], [], [], []
    for d in board.GetDrawings():
        if d.GetLayer() == pcbnew.Edge_Cuts and isinstance(d, pcbnew.PCB_SHAPE):
            edge += shape_segments(d)
    excluded = []
    for fp in board.GetFootprints():
        for it in fp.GraphicalItems():
            if isinstance(it, pcbnew.PCB_SHAPE) and it.GetLayer() == pcbnew.Edge_Cuts:
                edge += shape_segments(it)
        if fp.GetLayer() != cu:
            continue
        ref = fp.GetReference()
        attrs = fp.GetAttributes()
        is_dnp = bool(attrs & pcbnew.FP_EXCLUDE_FROM_BOM) or fp.IsDNP()
        if is_dnp:
            excluded.append(ref)
        for it in fp.GraphicalItems():
            if not isinstance(it, pcbnew.PCB_SHAPE):
                continue
            if it.GetLayer() == fab:
                fabs += shape_segments(it)
            elif it.GetLayer() == silk:
                silks += shape_segments(it)
        npads = sum(1 for p in fp.Pads() if p.IsOnLayer(cu))
        polar = npads > 2 or ref.rstrip("0123456789_").upper() in ("D", "LED", "Q", "U", "X", "Y", "IC", "CN", "J", "P", "USB")
        for pad in fp.Pads():
            if not pad.IsOnLayer(cu):
                continue
            poly = pad_polygon(pad, cu)
            if not poly:
                continue
            num = pad.GetNumber()
            if num in ("1", "A1") and polar and not is_dnp:
                pin1.append(poly)
            else:
                pads.append((poly, is_dnp))
        pos = fp.GetPosition()
        refs.append((mm(pos.x), mm(pos.y), ref, is_dnp))

    title = "%s   %s SIDE%s" % (stem, side.upper(), "   (MIRRORED: viewed from below)" if not top else "")
    ex = sorted(excluded)
    exs = "none" if not ex else (", ".join(ex) if len(ex) <= 8 else "%d pads/test points (%s ...)" % (len(ex), ", ".join(ex[:4])))
    sub = "RED = pin 1 / pad 1 / cathode.  Grey hatch = not placed: %s.  Outline approx. %.1f x %.1f mm" % (exs, w, h)
    scale = 40  # px per mm in SVG user units
    W = max((w + 2 * margin) * scale, 30 * scale)
    # shrink header fonts until both lines fit the canvas width
    f_title = min(1.15 * scale, (W - 24) / (0.6 * len(title)))
    f_sub = min(0.8 * scale, (W - 24) / (0.55 * len(sub)))
    H = (h + 2 * margin + 8) * scale  # room for title

    def P(x, y):
        return ((X(x) - x0 + margin) * scale if top else (X(x) - x0 + margin) * scale,
                (y - y0 + margin + 8) * scale)

    def poly_svg(pts, fill, stroke, sw=0.6, extra=""):
        d = " ".join("%.2f,%.2f" % P(x, y) for x, y in pts)
        return '<polygon points="%s" fill="%s" stroke="%s" stroke-width="%.2f"%s/>' % (d, fill, stroke, sw, extra)

    def segs_svg(segs, stroke, sw):
        return "".join('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="%s" stroke-width="%.2f"/>'
                       % (P(*a) + P(*b) + (stroke, sw)) for a, b in segs)

    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (W, H, W, H),
             '<rect width="100%" height="100%" fill="white"/>',
             '<defs><pattern id="hatch" patternUnits="userSpaceOnUse" width="4" height="4">'
             '<path d="M0,4 l4,-4" stroke="#999" stroke-width="1"/></pattern></defs>',
             '<text x="12" y="%.0f" font-family="Helvetica,Arial" font-size="%.1f" font-weight="bold">%s</text>' % (3.2 * scale, f_title, esc(title)),
             '<text x="12" y="%.0f" font-family="Helvetica,Arial" font-size="%.1f">%s</text>' % (5.6 * scale, f_sub, esc(sub)),
             segs_svg(edge, "#000", 1.2)]
    for poly, is_dnp in pads:
        parts.append(poly_svg(poly, "url(#hatch)" if is_dnp else "#c9c9c9", "#777", 0.4))
    for poly in pin1:
        parts.append(poly_svg(poly, "#e60000", "#900", 0.6))
    parts.append(segs_svg(fabs, "#2266cc", 0.8))
    parts.append(segs_svg(silks, "#22aa44", 0.6))
    for x, y, ref, is_dnp in refs:
        px, py = P(x, y)
        parts.append('<text x="%.2f" y="%.2f" font-family="Helvetica,Arial" font-size="%.0f" text-anchor="middle" '
                     'fill="%s" stroke="white" stroke-width="0.35" paint-order="stroke">%s</text>'
                     % (px, py + 0.25 * scale, 0.75 * scale, "#888" if is_dnp else "#000", esc(ref)))
    parts.append("</svg>")
    svg = os.path.join(out_dir, "%s_assembly_%s.svg" % (stem, side))
    open(svg, "w").write("\n".join(parts))
    out = [svg]
    if png:
        pngp = svg[:-4] + ".png"
        # 40 px/mm at 96 dpi nominal; scale to requested dpi
        zoom = dpi / 96.0
        if shutil.which("rsvg-convert"):
            subprocess.run(["rsvg-convert", "-z", "%.3f" % zoom, "-o", pngp, svg], check=True)
        elif shutil.which("magick"):
            subprocess.run(["magick", "-density", str(dpi), svg, pngp], check=True)
        else:
            print("no rsvg-convert or magick on PATH, SVG only", file=sys.stderr)
            return out
        out.append(pngp)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("--out", default=None, help="output dir (default: next to the board, production/)")
    ap.add_argument("--stem", default=None, help="file stem (default: board file name)")
    ap.add_argument("--dpi", type=int, default=600)
    ap.add_argument("--no-png", action="store_true")
    a = ap.parse_args()
    board = pcbnew.LoadBoard(a.board)
    stem = a.stem or os.path.splitext(os.path.basename(a.board))[0]
    out_dir = a.out or os.path.join(os.path.dirname(os.path.abspath(a.board)), "production")
    os.makedirs(out_dir, exist_ok=True)
    for side in ("top", "bottom"):
        for f in draw_side(board, side, stem, out_dir, a.dpi, not a.no_png):
            print(f)


if __name__ == "__main__":
    main()
