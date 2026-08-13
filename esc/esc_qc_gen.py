#!/usr/bin/env python3
"""Build an ESC-QC fixture board for another OpenESC, copying the 20x20 layout.

The 20x20-ESC-QC board is the reference. It is one negative footprint holding
the ESC contact pads plus the whole board geometry (100 x 100 outline, four M3
corner holes, and an ESC-shaped pocket cut into Edge.Cuts), with 44 edge test
pads around it that the fan-out copper runs to.

This script reproduces that for any OpenESC:

  <lib>.pretty/<DUT>-negative.kicad_mod   contact pads + outline + pocket
  <out>.kicad_pcb                         negative placed, 44 edge pads placed

Nothing is routed. Copper is drawn by hand afterwards.

Run with KiCad's bundled Python (needs pcbnew).
"""

import argparse
import math
import os
import re
import sys

import pcbnew

MM = pcbnew.FromMM
SIGNAL_NAMES = ["VBAT", "GND_SIG", "CURR", "TX", "M1", "M2", "M3", "M4"]

# Edge test pad positions, measured off 20x20-ESC-QC, relative to board centre.
# The fixture board is 100 x 100 for every ESC, so these do not change.
EDGE_PADS = {
    "phase_left":  [(-47.05, -12.37 + 5.0 * i) for i in range(6)],
    "phase_right": [(47.12, -12.63 + 5.0 * i) for i in range(6)],
    "signal":      [(-10.52 + 3.0 * i, -47.45) for i in range(8)],
    "battery":     [(-3.18, 47.05), (2.89, 47.04)],
}
PAD_SIZE = {"phase_left": 3.0, "phase_right": 3.0, "signal": 2.0, "battery": 4.0}

BOARD = 100.0          # fixture outline, mm
MOUNT_XY = 45.0        # M3 hole centres from board centre
MOUNT_R = 1.62         # M3 clearance radius
POCKET_R = 3.8         # pocket corner radius


def to_mm(v):
    return v / 1e6


def find_dut(board, hint):
    cands = [fp for fp in board.GetFootprints() if hint in fp.GetFPIDAsString()]
    if not cands:
        sys.exit("no footprint matching %r in %s" % (hint, board.GetFileName()))
    cands.sort(key=lambda fp: -len(list(fp.Pads())))
    return cands[0]


def collect(dut):
    """Contact pads of the ESC, ESC-local mm, keyed by group."""
    ox, oy = dut.GetPosition().x, dut.GetPosition().y
    out = []
    for pad in dut.Pads():
        num = pad.GetNumber()
        rec = dict(num=num,
                   x=to_mm(pad.GetPosition().x - ox),
                   y=to_mm(pad.GetPosition().y - oy),
                   w=to_mm(pad.GetSize().x), h=to_mm(pad.GetSize().y),
                   angle=pad.GetOrientationDegrees(), net=pad.GetNetname(),
                   shape=pad.GetShape())
        if num in ("1", "2"):
            rec["group"] = "battery"
        elif num in [str(n) for n in range(3, 11)]:
            rec["group"] = "signal"
        elif num == "11":
            rec["group"] = "mount"
        else:
            rec["group"] = "phase"
        out.append(rec)
    return out


def esc_outline_bbox(dut):
    ox, oy = dut.GetPosition().x, dut.GetPosition().y
    xs, ys = [], []
    for it in dut.GraphicalItems():
        if not isinstance(it, pcbnew.PCB_SHAPE) or it.GetLayer() != pcbnew.Edge_Cuts:
            continue
        for p in (it.GetStart(), it.GetEnd()):
            xs.append(to_mm(p.x - ox))
            ys.append(to_mm(p.y - oy))
    return min(xs), max(xs), min(ys), max(ys)


def pocket_rect(contacts, dut, inset_top=1.35, inset_bottom=2.5, gap=0.15):
    """The ESC-shaped hole the board drops into.

    Sized off the ESC's own pads so the protruding edge pads land on the rim
    while the body sits in the hole. Reproduces the 20x20 pocket to ~0.1 mm.
    """
    ph = [c for c in contacts if c["group"] == "phase"]
    sig = [c for c in contacts if c["group"] == "signal"]
    half = min(abs(c["x"]) for c in ph) - max(c["w"] for c in ph) / 2.0 - gap
    top = max(c["y"] for c in sig) + max(c["h"] for c in sig) / 2.0 + inset_top
    _, _, _, ymax = esc_outline_bbox(dut)
    bottom = ymax - inset_bottom
    return -half, half, top, bottom


def add_seg(fp, layer, x1, y1, x2, y2, w=0.1):
    s = pcbnew.PCB_SHAPE(fp)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetLayer(layer)
    s.SetWidth(MM(w))
    fp.Add(s)
    s.SetStart(pcbnew.VECTOR2I(MM(x1), MM(y1)))
    s.SetEnd(pcbnew.VECTOR2I(MM(x2), MM(y2)))


def add_arc(fp, layer, cx, cy, r, a0, a1, w=0.1):
    s = pcbnew.PCB_SHAPE(fp)
    s.SetShape(pcbnew.SHAPE_T_ARC)
    s.SetLayer(layer)
    s.SetWidth(MM(w))
    fp.Add(s)
    p = lambda a: pcbnew.VECTOR2I(MM(cx + r * math.cos(math.radians(a))),
                                  MM(cy + r * math.sin(math.radians(a))))
    s.SetArcGeometry(p(a0), p((a0 + a1) / 2.0), p(a1))


def add_circle(fp, layer, cx, cy, r, w=0.1):
    s = pcbnew.PCB_SHAPE(fp)
    s.SetShape(pcbnew.SHAPE_T_CIRCLE)
    s.SetLayer(layer)
    s.SetWidth(MM(w))
    fp.Add(s)
    s.SetCenter(pcbnew.VECTOR2I(MM(cx), MM(cy)))
    s.SetEnd(pcbnew.VECTOR2I(MM(cx + r), MM(cy)))


def rounded_rect(fp, layer, x0, x1, y0, y1, r, w=0.1):
    add_seg(fp, layer, x0 + r, y0, x1 - r, y0, w)
    add_seg(fp, layer, x1, y0 + r, x1, y1 - r, w)
    add_seg(fp, layer, x1 - r, y1, x0 + r, y1, w)
    add_seg(fp, layer, x0, y1 - r, x0, y0 + r, w)
    add_arc(fp, layer, x0 + r, y0 + r, r, 180, 270, w)
    add_arc(fp, layer, x1 - r, y0 + r, r, 270, 360, w)
    add_arc(fp, layer, x1 - r, y1 - r, r, 0, 90, w)
    add_arc(fp, layer, x0 + r, y1 - r, r, 90, 180, w)


def build_negative(name, contacts, pocket):
    """Contact pads plus the fixture's whole board geometry, as one footprint."""
    board = pcbnew.BOARD()
    fp = pcbnew.FOOTPRINT(board)
    fp.SetFPID(pcbnew.LIB_ID("", name))
    fp.SetLibDescription(
        "Negative of the ESC contact face: pads on the rim, ESC-shaped pocket, "
        "100 x 100 fixture outline and M3 corners")
    fp.SetAttributes(pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
    fp.SetReference("FID**")
    fp.Reference().SetVisible(False)
    fp.SetValue(name)
    fp.Value().SetVisible(False)

    for c in contacts:
        if c["group"] == "mount":
            continue
        pad = pcbnew.PAD(fp)
        pad.SetNumber(c["num"])
        pad.SetShape(c["shape"])
        pad.SetSize(pcbnew.VECTOR2I(MM(c["w"]), MM(c["h"])))
        if c["shape"] == pcbnew.PAD_SHAPE_ROUNDRECT:
            pad.SetRoundRectRadiusRatio(0.25)
        ls = pcbnew.LSET()
        for lay in (pcbnew.F_Cu, pcbnew.F_Mask, pcbnew.F_Paste):
            ls.AddLayer(lay)
        pad.SetLayerSet(ls)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetOrientationDegrees(c["angle"])
        fp.Add(pad)
        pad.SetFPRelativePosition(pcbnew.VECTOR2I(MM(c["x"]), MM(c["y"])))

    x0, x1, y0, y1 = pocket
    rounded_rect(fp, pcbnew.Edge_Cuts, x0, x1, y0, y1, POCKET_R)
    h = BOARD / 2.0
    rounded_rect(fp, pcbnew.Edge_Cuts, -h, h, -h, h, 4.0)
    for sx in (-1, 1):
        for sy in (-1, 1):
            add_circle(fp, pcbnew.Edge_Cuts, sx * MOUNT_XY, sy * MOUNT_XY, MOUNT_R)
    return fp


def make_testpoint(lib_dir, size):
    """Square SMD land, project-local so no global library is needed."""
    name = "TP_Pad_%.1fx%.1fmm" % (size, size)
    if os.path.isfile(os.path.join(lib_dir, name + ".kicad_mod")):
        return name
    fp = pcbnew.FOOTPRINT(pcbnew.BOARD())
    fp.SetFPID(pcbnew.LIB_ID("", name))
    fp.SetLibDescription("%.1f mm square solder land for wire or lug" % size)
    fp.SetAttributes(pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
    fp.SetReference("TP**")
    fp.SetValue(name)
    fp.Value().SetVisible(False)
    pad = pcbnew.PAD(fp)
    pad.SetNumber("1")
    pad.SetShape(pcbnew.PAD_SHAPE_RECT)
    pad.SetSize(pcbnew.VECTOR2I(MM(size), MM(size)))
    ls = pcbnew.LSET()
    for lay in (pcbnew.F_Cu, pcbnew.F_Mask, pcbnew.F_Paste):
        ls.AddLayer(lay)
    pad.SetLayerSet(ls)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    fp.Add(pad)
    pad.SetFPRelativePosition(pcbnew.VECTOR2I(0, 0))
    save(lib_dir, fp)
    return name


def save(lib_dir, fp):
    if not os.path.isdir(lib_dir):
        os.makedirs(lib_dir)
    pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP).FootprintSave(lib_dir, fp)


def load(lib_dir, name):
    return pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP).FootprintLoad(
        lib_dir, name)


def build_board(lib_dir, nick, neg_name, layers):
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(layers)
    cx = cy = MM(BOARD / 2.0 + 20.0)     # keep the sheet origin off the board

    neg = load(lib_dir, neg_name)
    neg.SetFPID(pcbnew.LIB_ID(nick, neg_name))
    neg.SetReference("DUT1")
    neg.Reference().SetVisible(False)
    board.Add(neg)
    neg.SetPosition(pcbnew.VECTOR2I(cx, cy))

    n = 0
    for group, spots in EDGE_PADS.items():
        fpname = make_testpoint(lib_dir, PAD_SIZE[group])
        for x, y in spots:
            for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
                tp = load(lib_dir, fpname)
                tp.SetFPID(pcbnew.LIB_ID(nick, fpname))
                n += 1
                tp.SetReference("TP%d" % n)
                tp.Reference().SetVisible(False)
                board.Add(tp)
                tp.SetPosition(pcbnew.VECTOR2I(cx + MM(x), cy + MM(y)))
                if layer == pcbnew.B_Cu:
                    tp.Flip(tp.GetPosition(), False)
    return board, n


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="ESC .kicad_pcb, read only")
    ap.add_argument("--dut", required=True)
    ap.add_argument("--lib", required=True, help="project-local <name>.pretty")
    ap.add_argument("--lib-nick", default="ESC-QC")
    ap.add_argument("--out", required=True)
    ap.add_argument("--layers", type=int, default=4)
    args = ap.parse_args()

    esc = pcbnew.LoadBoard(args.source)
    dut = find_dut(esc, args.dut)
    contacts = collect(dut)
    pocket = pocket_rect(contacts, dut)

    name = "%s-negative" % args.dut
    save(args.lib, build_negative(name, contacts, pocket))

    board, n = build_board(args.lib, args.lib_nick, name, args.layers)
    pcbnew.SaveBoard(args.out, board)

    print("%s: %d contact pads, pocket %.2f x %.2f mm, %d edge pads -> %s"
          % (os.path.basename(args.source),
             sum(1 for c in contacts if c["group"] != "mount"),
             pocket[1] - pocket[0], pocket[3] - pocket[2], n, args.out))


if __name__ == "__main__":
    main()
