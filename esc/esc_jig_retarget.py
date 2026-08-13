#!/usr/bin/env python3
"""Retarget an OpenESC press-contact jig from one ESC to another.

The jig layout (outline, mounting holes, headers, banana jacks, silkscreen) is
kept exactly as drawn. Only the pogo pins that land on the ESC move, to the
coordinates the target ESC actually uses. Contact geometry therefore cannot
drift from the board under test, and the jig keeps the layout that is already
proven in fab.

  esc_jig_retarget.py TEMPLATE.kicad_pcb TARGET_ESC.kicad_pcb --dut 4in1ESC30x30
                      --out 30x30-ESC-Flashing.kicad_pcb

Pogo pins are matched to their net name, so the template's own net naming is
what drives placement:

  /SWDn_CLK, /SWDn_DIO   SWD test point of channel n on the ESC's B.Cu face
  /VBAT, GND             the two battery pads

Run with KiCad's bundled Python (needs pcbnew).
"""

import argparse
import os
import re
import sys

import pcbnew

MM = pcbnew.FromMM
SWD_RE = re.compile(r"Net-\((U\d+)-(PA13|PA14)\)")


def to_mm(v):
    return v / 1e6


def find_dut(board, hint):
    cands = [fp for fp in board.GetFootprints() if hint in fp.GetFPIDAsString()]
    if not cands:
        sys.exit("no footprint matching %r in %s" % (hint, board.GetFileName()))
    cands.sort(key=lambda fp: -len(list(fp.Pads())))
    return cands[0]


def target_points(esc_path, dut_hint):
    """Where each jig net has to touch the target ESC, in ESC-local mm."""
    board = pcbnew.LoadBoard(esc_path)
    dut = find_dut(board, dut_hint)
    ox, oy = dut.GetPosition().x, dut.GetPosition().y
    pts = {}

    channel = {}
    for fp in board.GetFootprints():
        m = re.match(r"/ESC(\d)/", fp.GetSheetname() or "")
        if m:
            channel[fp.GetReference()] = m.group(1)
    for fp in board.GetFootprints():
        if "TestPoint" not in fp.GetFPIDAsString():
            continue
        for pad in fp.Pads():
            m = SWD_RE.match(pad.GetNetname())
            if not m:
                continue
            ch = channel.get(m.group(1))
            if ch is None:
                sys.exit("no ESC sheet owns %s" % m.group(1))
            sig = "DIO" if m.group(2) == "PA13" else "CLK"
            pts["/SWD%s_%s" % (ch, sig)] = (to_mm(pad.GetPosition().x - ox),
                                            to_mm(pad.GetPosition().y - oy))

    for pad in dut.Pads():
        if pad.GetNumber() not in ("1", "2"):
            continue
        net = pad.GetNetname()
        key = "/VBAT" if ("CSA" in net or "BATT" in net) else "GND"
        pts.setdefault(key, []) if False else None
        pts[key] = (to_mm(pad.GetPosition().x - ox), to_mm(pad.GetPosition().y - oy))
    return pts


def esc_outline(esc_path, dut_hint):
    """Target ESC board edge, ESC-local mm, as straight segments."""
    board = pcbnew.LoadBoard(esc_path)
    dut = find_dut(board, dut_hint)
    ox, oy = dut.GetPosition().x, dut.GetPosition().y
    segs = []
    for it in dut.GraphicalItems():
        if not isinstance(it, pcbnew.PCB_SHAPE) or it.GetLayer() != pcbnew.Edge_Cuts:
            continue
        s, e = it.GetStart(), it.GetEnd()
        if it.GetShape() == pcbnew.SHAPE_T_ARC:
            m = it.GetArcMid()
            segs.append((to_mm(s.x - ox), to_mm(s.y - oy), to_mm(m.x - ox), to_mm(m.y - oy)))
            segs.append((to_mm(m.x - ox), to_mm(m.y - oy), to_mm(e.x - ox), to_mm(e.y - oy)))
        else:
            segs.append((to_mm(s.x - ox), to_mm(s.y - oy), to_mm(e.x - ox), to_mm(e.y - oy)))
    return segs


def jig_origin(board):
    """Centre of the four mounting holes: the jig's reference for ESC-local mm."""
    holes = [fp for fp in board.GetFootprints()
             if "MountingHole" in fp.GetFPIDAsString()]
    if len(holes) != 4:
        sys.exit("expected 4 mounting holes in the template, found %d" % len(holes))
    return (sum(h.GetPosition().x for h in holes) / 4.0,
            sum(h.GetPosition().y for h in holes) / 4.0)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("template", help="jig .kicad_pcb to copy the layout from")
    ap.add_argument("esc", help="target ESC .kicad_pcb, read only")
    ap.add_argument("--dut", required=True, help="ESC footprint holding the external pads")
    ap.add_argument("--out", required=True)
    ap.add_argument("--pogo-fp", default="CONN-SMD_BD1.2",
                    help="substring identifying the pogo footprints to move")
    ap.add_argument("--strip-tracks", action="store_true",
                    help="delete all tracks and vias so the copper is re-routed by hand")
    ap.add_argument("--swap-outline", type=float, default=0.0, metavar="R",
                    help="replace silkscreen graphics within R mm of the jig centre "
                         "with the target ESC's own outline")
    ap.add_argument("--retext", action="append", default=[], metavar="OLD=NEW",
                    help="silkscreen text substitution, repeatable")
    args = ap.parse_args()

    want = target_points(args.esc, args.dut)
    board = pcbnew.LoadBoard(args.template)
    ox, oy = jig_origin(board)

    moved, skipped, unknown = 0, 0, []
    for fp in board.GetFootprints():
        if args.pogo_fp not in fp.GetFPIDAsString():
            continue
        nets = [p.GetNetname() for p in fp.Pads() if p.GetNetname()]
        if not nets:
            skipped += 1
            continue
        net = nets[0]
        if net not in want:
            unknown.append(net)
            continue
        x, y = want[net]
        old = fp.GetPosition()
        fp.SetPosition(pcbnew.VECTOR2I(int(ox + MM(x)), int(oy + MM(y))))
        moved += 1

    # collect text before any removal: the SWIG containers do not survive it
    texts = [i for i in board.Drawings() if isinstance(i, pcbnew.PCB_TEXT)]
    for fp in board.GetFootprints():
        texts += [i for i in fp.GraphicalItems() if isinstance(i, pcbnew.PCB_TEXT)]
    for sub in args.retext:
        old, _, new = sub.partition("=")
        for item in texts:
            if old in item.GetText():
                item.SetText(item.GetText().replace(old, new))

    if args.swap_outline > 0:
        r = MM(args.swap_outline)
        doomed = [i for i in board.Drawings()
                  if isinstance(i, pcbnew.PCB_SHAPE)
                  and i.GetLayer() == pcbnew.F_SilkS
                  and abs(i.GetBoundingBox().GetCenter().x - ox) < r
                  and abs(i.GetBoundingBox().GetCenter().y - oy) < r]
        for i in doomed:
            board.Remove(i)
        for x1, y1, x2, y2 in esc_outline(args.esc, args.dut):
            s = pcbnew.PCB_SHAPE(board)
            s.SetShape(pcbnew.SHAPE_T_SEGMENT)
            s.SetLayer(pcbnew.F_SilkS)
            s.SetWidth(MM(0.15))
            board.Add(s)
            s.SetStart(pcbnew.VECTOR2I(int(ox + MM(x1)), int(oy + MM(y1))))
            s.SetEnd(pcbnew.VECTOR2I(int(ox + MM(x2)), int(oy + MM(y2))))
        print("  silkscreen ESC outline swapped (%d old shapes removed)" % len(doomed))

    if args.strip_tracks:
        for t in list(board.GetTracks()):
            board.Remove(t)

    if unknown:
        print("  net not present on the target ESC, pin left in place: %s"
              % ", ".join(sorted(set(unknown))))
    pcbnew.SaveBoard(args.out, board)
    print("%-34s %2d pogo pins retargeted, %d without a net -> %s"
          % (os.path.basename(args.esc), moved, skipped, args.out))


if __name__ == "__main__":
    main()
