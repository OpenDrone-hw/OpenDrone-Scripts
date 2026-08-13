#!/usr/bin/env python3
"""Generate the OpenESC press-contact fixtures from the ESC design itself.

Two fixtures per ESC, both 100 x 100 mm, 4 layer, pogo pins on F.Cu pointing up,
ESC pressed down onto them. This follows the fabbed 20x20 pair
(`4in1-ESC-Flashing`, `4in1-ESC-MotorTest`), same pogo part, same connectors,
same M3 pattern.

  flash   SWD jig. Reads the ESC's B.Cu face: 4x SWDIO/SWCLK plus VBAT and GND
          to power the target. ESC sits the right way up, so the geometry is a
          direct copy of the ESC design.
  motor   Load/QC jig. Reads the ESC's F.Cu face: battery, 12 motor phases and
          the 8-pin signal row, plus a second pogo per phase at the board edge
          for the motor lead. ESC sits flipped, so the geometry is mirrored.

Flipping a board mirrors it: that is the whole rule, and it is why the two
fixtures differ.

Run with KiCad's bundled Python (needs pcbnew).
"""

import argparse
import os
import re
import shutil
import sys

import pcbnew

MM = pcbnew.FromMM
POGO = "CONN-SMD_BD1.2_YZ118311024R-02"      # LCSC C5157376, 1.2 mm SMD pogo
JACK_RED = "CONN-TH_24.243.1_RED"            # LCSC C7437321
JACK_BLACK = "CONN-TH_24.243.2"              # LCSC C7437322
HDR4 = "HDR-TH_4P-P2.54-V-M"                 # LCSC C124378
MHOLE = "Mount_M3_3.2mm"                     # generated, kept project-local
FIXTURE_FPS = [POGO, JACK_RED, JACK_BLACK, HDR4]


def to_mm(v):
    return v / 1e6


class Contact:
    """One accessible feature of the ESC, in ESC-footprint-local mm."""

    def __init__(self, name, x, y, w, h, angle, face, group, drill=0.0):
        self.name = name
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.angle = angle
        self.face = face      # 'F', 'B' or 'both'
        self.group = group    # battery | phase | signal | swd | mount
        self.drill = drill


# ---------------------------------------------------------------- extraction

SIGNAL_NAMES = ["VBAT", "GND", "CURR", "TX", "M1", "M2", "M3", "M4"]
SWD_RE = re.compile(r"Net-\((U\d+)-(PA13|PA14)\)")


def find_dut(board, hint):
    cands = [fp for fp in board.GetFootprints() if hint in fp.GetFPIDAsString()]
    if not cands:
        sys.exit("no footprint matching %r in %s" % (hint, board.GetFileName()))
    cands.sort(key=lambda fp: -len(list(fp.Pads())))
    return cands[0]


def pad_face(pad):
    f, b = pad.IsOnLayer(pcbnew.F_Cu), pad.IsOnLayer(pcbnew.B_Cu)
    return "both" if (f and b) else ("F" if f else "B")


def collect_dut_contacts(dut):
    ox, oy = dut.GetPosition().x, dut.GetPosition().y
    out = []
    for pad in dut.Pads():
        num = pad.GetNumber()
        px, py = to_mm(pad.GetPosition().x - ox), to_mm(pad.GetPosition().y - oy)
        w, h = to_mm(pad.GetSize().x), to_mm(pad.GetSize().y)
        ang, net, face = pad.GetOrientationDegrees(), pad.GetNetname(), pad_face(pad)
        if num in ("1", "2"):
            nm = "VBAT" if ("CSA" in net or "BATT" in net) else "GND"
            out.append(Contact(nm, px, py, w, h, ang, face, "battery"))
        elif num in [str(n) for n in range(3, 11)]:
            out.append(Contact(SIGNAL_NAMES[int(num) - 3], px, py, w, h, ang,
                               face, "signal"))
        elif num == "11":
            out.append(Contact("GND", px, py, w, h, ang, face, "mount",
                               to_mm(pad.GetDrillSize().x)))
        else:
            m = re.match(r"/ESC(\d)/Motor([ABC])", net)
            nm = "ESC%s_%s" % (m.group(1), m.group(2)) if m else "PHASE_%s" % num
            out.append(Contact(nm, px, py, w, h, ang, face, "phase"))
    return out


def collect_swd_contacts(board, dut):
    """Named from the schematic sheet of the MCU that owns each test point."""
    ox, oy = dut.GetPosition().x, dut.GetPosition().y
    channel = {}
    for fp in board.GetFootprints():
        m = re.match(r"/(ESC(\d))/", fp.GetSheetname() or "")
        if m:
            channel[fp.GetReference()] = m.group(2)
    out = []
    for fp in board.GetFootprints():
        if "TestPoint" not in fp.GetFPIDAsString():
            continue
        for pad in fp.Pads():
            m = SWD_RE.match(pad.GetNetname())
            if not m:
                continue
            ch = channel.get(m.group(1))
            if ch is None:
                sys.exit("no ESC sheet for %s" % m.group(1))
            sig = "DIO" if m.group(2) == "PA13" else "CLK"
            out.append(Contact("SWD%s_%s" % (ch, sig),
                               to_mm(pad.GetPosition().x - ox),
                               to_mm(pad.GetPosition().y - oy),
                               0, 0, 0, "B", "swd"))
    return out


def dut_outline(dut):
    ox, oy = dut.GetPosition().x, dut.GetPosition().y
    segs = []
    for it in dut.GraphicalItems():
        if not isinstance(it, pcbnew.PCB_SHAPE) or it.GetLayer() != pcbnew.Edge_Cuts:
            continue
        s, e = it.GetStart(), it.GetEnd()
        if it.GetShape() == pcbnew.SHAPE_T_SEGMENT:
            segs.append((to_mm(s.x - ox), to_mm(s.y - oy),
                         to_mm(e.x - ox), to_mm(e.y - oy)))
        elif it.GetShape() == pcbnew.SHAPE_T_ARC:
            m = it.GetArcMid()
            segs.append((to_mm(s.x - ox), to_mm(s.y - oy),
                         to_mm(m.x - ox), to_mm(m.y - oy)))
            segs.append((to_mm(m.x - ox), to_mm(m.y - oy),
                         to_mm(e.x - ox), to_mm(e.y - oy)))
    return segs


# ------------------------------------------------------------------ assembly

def net_of(board, name, cache):
    if name not in cache:
        ni = pcbnew.NETINFO_ITEM(board, name)
        board.Add(ni)
        cache[name] = ni
    return cache[name]


def load_fp(lib_dir, name):
    plugin = pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP)
    return plugin.FootprintLoad(lib_dir, name)


def make_mount_footprint(lib_dir, drill=3.2, ring=6.0):
    """M3 clearance hole, non-plated. Generated so no global library is needed."""
    path = os.path.join(lib_dir, MHOLE + ".kicad_mod")
    if os.path.isfile(path):
        return
    fp = pcbnew.FOOTPRINT(pcbnew.BOARD())
    fp.SetFPID(pcbnew.LIB_ID("", MHOLE))
    fp.SetLibDescription("M3 clearance mounting hole, non-plated")
    fp.SetAttributes(pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
    fp.SetReference("H**")
    fp.Reference().SetVisible(False)
    fp.SetValue(MHOLE)
    fp.Value().SetVisible(False)
    pad = pcbnew.PAD(fp)
    pad.SetNumber("")
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
    pad.SetSize(pcbnew.VECTOR2I(MM(drill), MM(drill)))
    pad.SetDrillSize(pcbnew.VECTOR2I(MM(drill), MM(drill)))
    ls = pcbnew.LSET()
    for lay in (pcbnew.F_Mask, pcbnew.B_Mask):
        ls.AddLayer(lay)
    pad.SetLayerSet(ls)
    fp.Add(pad)
    pad.SetFPRelativePosition(pcbnew.VECTOR2I(0, 0))
    circ = pcbnew.PCB_SHAPE(fp)
    circ.SetShape(pcbnew.SHAPE_T_CIRCLE)
    circ.SetLayer(pcbnew.F_SilkS)
    circ.SetWidth(MM(0.15))
    fp.Add(circ)
    circ.SetCenter(pcbnew.VECTOR2I(0, 0))
    circ.SetEnd(pcbnew.VECTOR2I(MM(ring / 2.0), 0))
    pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP).FootprintSave(
        lib_dir, fp)


class Fixture:
    """A fixture board under construction."""

    def __init__(self, lib_dir, lib_nick, size, layers):
        self.lib_dir = lib_dir
        self.lib_nick = lib_nick
        self.size = size
        self.board = pcbnew.BOARD()
        self.board.SetCopperLayerCount(layers)
        self.nets = {}
        self.refs = {}
        self.cx = self.cy = MM(size / 2.0)
        for a, b in (((0, 0), (size, 0)), ((size, 0), (size, size)),
                     ((size, size), (0, size)), ((0, size), (0, 0))):
            sh = pcbnew.PCB_SHAPE(self.board)
            sh.SetShape(pcbnew.SHAPE_T_SEGMENT)
            sh.SetLayer(pcbnew.Edge_Cuts)
            sh.SetWidth(MM(0.15))
            self.board.Add(sh)
            sh.SetStart(pcbnew.VECTOR2I(MM(a[0]), MM(a[1])))
            sh.SetEnd(pcbnew.VECTOR2I(MM(b[0]), MM(b[1])))

    def ref(self, prefix):
        self.refs[prefix] = self.refs.get(prefix, 0) + 1
        return "%s%d" % (prefix, self.refs[prefix])

    def place(self, name, prefix, x, y, nets=None, angle=0.0, lib=None):
        """Place a library footprint at x,y mm relative to the board centre."""
        fp = load_fp(lib if lib is not None else self.lib_dir, name)
        if fp is None:
            sys.exit("cannot load footprint %s" % name)
        short = name.split(":")[-1]
        fp.SetFPID(pcbnew.LIB_ID(self.lib_nick, short))
        fp.SetReference(self.ref(prefix))
        self.board.Add(fp)
        fp.SetPosition(pcbnew.VECTOR2I(self.cx + MM(x), self.cy + MM(y)))
        if angle:
            fp.SetOrientationDegrees(angle)
        for pad in fp.Pads():
            n = (nets or {}).get(pad.GetNumber())
            if n:
                pad.SetNet(net_of(self.board, n, self.nets))
        return fp

    def pogo(self, net, x, y):
        fp = self.place(POGO, "TP", x, y, {"1": net})
        fp.Reference().SetVisible(False)
        return fp

    def label(self, x, y, text, size=1.0, layer=pcbnew.F_SilkS):
        t = pcbnew.PCB_TEXT(self.board)
        t.SetLayer(layer)
        t.SetText(text)
        t.SetTextSize(pcbnew.VECTOR2I(MM(size), MM(size)))
        t.SetTextThickness(MM(size / 6.5))
        self.board.Add(t)
        t.SetPosition(pcbnew.VECTOR2I(self.cx + MM(x), self.cy + MM(y)))
        return t

    def outline(self, segs, mirror, layer=pcbnew.Dwgs_User):
        sx = -1.0 if mirror else 1.0
        for x1, y1, x2, y2 in segs:
            sh = pcbnew.PCB_SHAPE(self.board)
            sh.SetShape(pcbnew.SHAPE_T_SEGMENT)
            sh.SetLayer(layer)
            sh.SetWidth(MM(0.12))
            self.board.Add(sh)
            sh.SetStart(pcbnew.VECTOR2I(self.cx + MM(sx * x1), self.cy + MM(y1)))
            sh.SetEnd(pcbnew.VECTOR2I(self.cx + MM(sx * x2), self.cy + MM(y2)))

    def furniture(self, title):
        h = self.size / 2.0 - 5.0
        for x, y in ((-h, -h), (h, -h), (-h, h), (h, h)):
            self.place(MHOLE, "H", x, y)
        y = self.size / 2.0 - 8.0
        self.place(JACK_RED, "J", -24.0, y, {"1": "VBAT", "2": "VBAT"})
        self.place(JACK_BLACK, "J", 12.0, y, {"1": "GND", "2": "GND"})
        self.label(-24.0, y - 7.0, "VBAT", 1.6)
        self.label(12.0, y - 7.0, "GND", 1.6)
        self.label(0, -h - 1.5, title, 2.5)


# ------------------------------------------------------------- the two boards

def one_pad_per_net(contacts, group):
    """First contact of each net in a group, in stable order."""
    seen, out = set(), []
    for c in contacts:
        if c.group == group or (group == "any" and c.group != "mount"):
            if c.name in seen:
                continue
            seen.add(c.name)
            out.append(c)
    return out


def build_flash(fx, contacts, swd, outline):
    """SWD jig. ESC the right way up, so nothing is mirrored."""
    fx.outline(outline, mirror=False)
    for c in sorted(swd, key=lambda c: c.name):
        fx.pogo(c.name, c.x, c.y)
    # VBAT / GND straight off the battery pads so the target is powered
    for c in one_pad_per_net(contacts, "battery"):
        for d in (-2.75, 2.75):
            fx.pogo(c.name, c.x, c.y + d)
    # one 4-pin breakout per channel: CLK, DIO, spare, GND
    row = -(fx.size / 2.0) + 14.0
    for i in range(1, 5):
        y = row + (i - 1) * 6.0
        fx.place(HDR4, "J", 32.0, y, {"1": "SWD%d_CLK" % i, "2": "SWD%d_DIO" % i,
                                      "4": "GND"}, angle=90.0)
        fx.label(23.0, y, "SWD%d" % i, 1.4)
    fx.furniture("ESC flash jig")


def build_motor(fx, contacts, outline):
    """Load jig. ESC flipped onto the fixture, so everything is mirrored."""
    fx.outline(outline, mirror=True)
    edge = fx.size / 2.0 - 10.0

    for c in one_pad_per_net(contacts, "battery"):
        for d in (-2.75, 2.75):
            fx.pogo(c.name, -c.x, c.y + d)
    for c in one_pad_per_net(contacts, "signal"):
        fx.pogo(c.name, -c.x, c.y)
    sig = one_pad_per_net(contacts, "signal")
    if sig:
        fx.label(0, min(c.y for c in sig) - 3.0, "signal row", 1.2)

    phases = one_pad_per_net(contacts, "phase")
    left = sorted([c for c in phases if -c.x < 0], key=lambda c: c.y)
    right = sorted([c for c in phases if -c.x > 0], key=lambda c: c.y)
    for col, group in ((-edge, left), (edge, right)):
        for i, c in enumerate(group):
            fx.pogo(c.name, -c.x, c.y)                       # on the ESC pad
            y = (i - (len(group) - 1) / 2.0) * 7.0
            fx.pogo(c.name, col, y)                          # motor lead lands here
            fx.label(col - (5.0 if col > 0 else -5.0), y, c.name, 1.2)

    y0 = -(fx.size / 2.0) + 14.0
    fx.place(HDR4, "J", -32.0, y0,
             {"1": "M1", "2": "M2", "3": "M3", "4": "M4"}, angle=90.0)
    fx.place(HDR4, "J", -32.0, y0 + 6.0,
             {"1": "CURR", "2": "TX", "3": "VBAT", "4": "GND"}, angle=90.0)
    fx.label(-32.0, y0 - 5.0, "DShot / telemetry", 1.2)
    fx.furniture("ESC motor test jig")


# ------------------------------------------------------------------- driver

def copy_fixture_lib(src_lib, dst_lib):
    if not os.path.isdir(dst_lib):
        os.makedirs(dst_lib)
    for name in FIXTURE_FPS:
        src = os.path.join(src_lib, name + ".kicad_mod")
        if os.path.isfile(src) and not os.path.isfile(
                os.path.join(dst_lib, name + ".kicad_mod")):
            shutil.copy2(src, dst_lib)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="ESC .kicad_pcb, read only")
    ap.add_argument("--dut", required=True,
                    help="footprint carrying the ESC's external pads")
    ap.add_argument("--fixture", required=True, choices=("flash", "motor"))
    ap.add_argument("--lib", required=True, help="project-local <name>.pretty")
    ap.add_argument("--lib-nick", default="ESC-QC")
    ap.add_argument("--seed-lib", help="copy the pogo/jack/header footprints from here")
    ap.add_argument("--out", required=True, help="fixture .kicad_pcb to write")
    ap.add_argument("--size", type=float, default=100.0)
    ap.add_argument("--layers", type=int, default=4)
    args = ap.parse_args()

    if args.seed_lib:
        copy_fixture_lib(args.seed_lib, args.lib)
    make_mount_footprint(args.lib)

    board = pcbnew.LoadBoard(args.source)
    dut = find_dut(board, args.dut)
    contacts = collect_dut_contacts(dut)
    swd = collect_swd_contacts(board, dut)
    outline = dut_outline(dut)

    fx = Fixture(args.lib, args.lib_nick, args.size, args.layers)
    if args.fixture == "flash":
        build_flash(fx, contacts, swd, outline)
    else:
        build_motor(fx, [c for c in contacts if c.face in ("both", "F")], outline)

    pcbnew.SaveBoard(args.out, fx.board)
    pogos = sum(1 for fp in fx.board.GetFootprints() if POGO in fp.GetFPIDAsString())
    print("%-38s %2d pogo pins, %2d footprints, %2d nets -> %s"
          % (os.path.basename(args.source) + " " + args.fixture, pogos,
             len(fx.board.GetFootprints()), len(fx.nets), args.out))


if __name__ == "__main__":
    main()
