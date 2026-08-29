#!/usr/bin/env python3
"""KiCad board -> fab handoff package, one command.

    $KPY handoff_pack.py <board.kicad_pcb> --fab pcbgogo [--qty 250]
                         [--name STEM] [--skip-pack] [--out DIR]
                         [--copper "2 oz outer, 1 oz inner"] [--tg 170]
                         [--note TEXT ...]

Runs quote_pack.py (gerbers, BOMs, positions, check_export gate), gerber_check.py
(DFM double-check of the zip, report in <stem>_DFM-CHECK.txt next to the zip), then builds
production/handoff-<rev>/<stem>_<fab>.zip containing exactly what a fab
reviewer needs and nothing else:

  <stem>.zip                     gerbers + drill
  <stem>_bom_<fab>.xlsx          BOM in the fab's template, duplicate
                                 designators renamed to the FT _2/_3 names
                                 so BOM and positions agree line for line
  <stem>_positions_<fab>.csv     pick and place, ONLY parts that are in the
                                 BOM (test pads, bare pads, DNP dropped)
  <stem>_assembly_top.png        assembly drawing, pin 1 red
  <stem>_assembly_bottom.png     same, mirrored (viewed from below)
  <stem>_<FAB>-ORDER-SHEET.txt   one page: every value for their order form,
                                 plain English, measured from the board

Measured from the board: outline size, copper layer count, thickness,
copper weights (stackup, override with --copper), min track/space (design
rules), min drill (smallest hole actually used), placements per side.
Not measurable and therefore flags: --tg (default 170), --qty (250),
--note (free lines appended under PARTS, repeatable).

Fabs: pcbgogo, nextpcb, makerpcb (the latter two get the portal-safe gerber zip). Run with KiCad's Python.
"""
import argparse, collections, csv, json, os, re, shutil, subprocess, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import pcbnew
except ImportError:
    sys.exit("needs KiCad's bundled Python (pcbnew); see OpenDrone-Scripts README")

import assembly_drawing  # noqa: E402


def measure(board_path, copper_override=None):
    b = pcbnew.LoadBoard(board_path)
    ds = b.GetDesignSettings()
    bb = b.GetBoardEdgesBoundingBox()
    m = {}
    # subtract the edge line width: bbox includes half the stroke each side
    ew = 0.0
    for d in b.GetDrawings():
        if d.GetLayer() == pcbnew.Edge_Cuts and isinstance(d, pcbnew.PCB_SHAPE):
            ew = max(ew, d.GetWidth())
    for fp in b.GetFootprints():
        for it in fp.GraphicalItems():
            if isinstance(it, pcbnew.PCB_SHAPE) and it.GetLayer() == pcbnew.Edge_Cuts:
                ew = max(ew, it.GetWidth())
    m['size_mm'] = ((bb.GetWidth() - ew) / 1e6, (bb.GetHeight() - ew) / 1e6)
    m['layers'] = b.GetCopperLayerCount()
    m['thickness_mm'] = ds.GetBoardThickness() / 1e6
    m['min_track_mm'] = ds.m_TrackMinWidth / 1e6
    m['min_clearance_mm'] = ds.m_MinClearance / 1e6
    drills = set()
    for t in b.GetTracks():
        if isinstance(t, pcbnew.PCB_VIA):
            drills.add(t.GetDrillValue())
    for fp in b.GetFootprints():
        for p in fp.Pads():
            d = p.GetDrillSize()
            if d.x > 0:
                drills.add(d.x)
    m['min_drill_mm'] = min(drills) / 1e6 if drills else 0
    # via-in-pad count: vias whose centre lies inside an SMD pad on the same outer layer
    vip = 0
    pads = [(p, p.GetBoundingBox()) for fp in b.GetFootprints() for p in fp.Pads() if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD]
    for t in b.GetTracks():
        if isinstance(t, pcbnew.PCB_VIA):
            pos = t.GetPosition()
            if any(bbx.Contains(pos) for _, bbx in pads):
                vip += 1
    m['via_in_pad'] = vip
    # copper weights from the stackup block in the board file (text parse, read
    # only: the pcbnew stackup API is not reliable across KiCad versions)
    txt = open(board_path, errors='replace').read()
    cu = dict(re.findall(r'\(layer "([\w.]+\.Cu)"\s*\(type "copper"\)\s*\(thickness ([\d.]+)\)', txt))
    oz = lambda t: round(float(t) / 0.035 * 2) / 2  # 0.035 mm = 1 oz
    if copper_override:
        m['copper'] = copper_override
    elif 'F.Cu' in cu:
        outer = oz(cu['F.Cu'])
        inner = oz(cu['In1.Cu']) if 'In1.Cu' in cu else None
        m['copper'] = f"{outer:g} oz outer, " + (f"{inner:g} oz inner" if inner and inner >= 1 else "inner layers fab standard")
    else:
        m['copper'] = "1 oz"
    m['title_rev'] = b.GetTitleBlock().GetRevision()
    return m


def mil(v_mm):
    return v_mm / 0.0254


def track_tier(track_mm, clr_mm):
    """Smallest PCBGOGO tier that covers the design: 3/3, 4/4, 5/5, 6/6, 8/8 mil."""
    need = min(mil(track_mm), mil(clr_mm))
    best = 3
    for t in (3, 4, 5, 6, 8):
        if t <= need + 1e-6:
            best = t
    return f"{best}/{best}mil"


def hole_tier(d_mm):
    best = 0.15
    for t in (0.15, 0.2, 0.25, 0.3, 0.8, 1.0):
        if t <= d_mm + 1e-6:
            best = t
    return f"{best}mm"


def build_bom_positions(pack, stem, fab, out):
    """Fab BOM (from the universal CSV, through quote_pack's writer) with FT's
    unique duplicate names, and positions limited to BOM parts."""
    import quote_pack
    rows = quote_pack.read_universal(os.path.join(pack, f'{stem}_bom_universal.csv'))
    pos = list(csv.DictReader(open(os.path.join(pack, f'{stem}_positions.csv'), encoding='utf-8-sig')))
    names = collections.defaultdict(list)
    for x in pos:
        names[re.sub(r'_\d+$', '', x['Designator']).lower()].append(x['Designator'])
    keep, qty, dups, out_rows = set(), 0, set(), []
    for r in rows:
        if not r['MPN'] and not r['LCSC']:
            continue  # bare pads / test points: not a part
        refs = [x.strip() for x in r['Designator'].split(',')]
        used, new = collections.Counter(), []
        for ref in refs:
            c = names.get(ref.lower(), [])
            i = used[ref.lower()]
            used[ref.lower()] += 1
            if i < len(c):
                new.append(c[i])
                keep.add(c[i])
                if i:
                    dups.add(ref)
            else:
                sys.exit(f"BOM ref {ref} has no placement in positions file")
        r = dict(r, Designator=','.join(new))
        out_rows.append(r)
        qty += int(r['Quantity'])
    writer = getattr(quote_pack, f'write_{fab}')
    writer(out_rows, os.path.join(out, f'{stem}_bom_{fab}.{BOM_EXT[fab]}'))
    keep_rows = [x for x in pos if x['Designator'] in keep]
    with open(os.path.join(out, f'{stem}_positions_{fab}.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['Designator', 'Mid X', 'Mid Y', 'Rotation', 'Layer'])
        for x in keep_rows:
            w.writerow([x['Designator'], x['Mid X'], x['Mid Y'], x['Rotation'], x['Layer']])
    if qty != len(keep_rows):
        sys.exit(f"BOM quantity {qty} != placements {len(keep_rows)}")
    return qty, collections.Counter(x['Layer'] for x in keep_rows), sorted(dups), len(out_rows)


def sheet_pcbgogo(stem, m, qty_order, placements, sides, dups, lines, tg, notes):
    L, W = m['size_mm']
    dup = ("No duplicate names." if not dups else
           "Some parts exist 2 or 3 times with the same name in KiCad (%s). In the BOM and "
           "positions file they are named %s_2, %s_3. Place every line." % (", ".join(dups), dups[0], dups[0]))
    body = f"""{stem}  PCBGOGO ORDER SHEET
Incutec BV, Leuven, Belgium.  Contact: Stan Coene, stan@incutec.eu

FILES
  {stem}.zip                    Gerber + drill (KiCad)
  {stem}_bom_pcbgogo.xlsx       BOM, PCBGOGO template, {lines} lines
  {stem}_positions_pcbgogo.csv  Pick and place, mm, only parts in the BOM
  {stem}_assembly_top.png       Drawing top side. RED pad = pin 1
  {stem}_assembly_bottom.png    Drawing bottom side, MIRRORED (view from below). RED pad = pin 1

PCB (form values)
  Material            Normal FR-4, TG {tg}
  Size (single)       {L:.1f} x {W:.1f} mm
  Quantity (single)   {qty_order} pcs. This is a firm production order, not a prototype.
  Panel way           Panel by PCBgogo. We suggest 5 x 5 boards per panel; PCBgogo decides the best panel and depanel method. Ship DEPANELED, edges cleaned (no tab remnants).
  Layers              {m['layers']}
  Thickness           {m['thickness_mm']:.1f} mm
  Min track/spacing   {track_tier(m['min_track_mm'], m['min_clearance_mm'])}   (design: {m['min_track_mm']:.2f} mm track, {m['min_clearance_mm']:.2f} mm space)
  Min hole            {hole_tier(m['min_drill_mm'])}   (smallest hole used: {m['min_drill_mm']:.2f} mm)
  Solder mask         Green
  Silkscreen          White
  Surface finish      Immersion gold (ENIG), 1U" min.  Do NOT change to HASL.
  Finished copper     {m['copper']}
  Via process         Tenting vias  +  Additional options: "Via in pad" + "Via filled with resin" + "Confirm work file"
                      Vias must be filled and capped (IPC-4761 Type VII). {m['via_in_pad']} vias are inside SMD pads.
  Order number        Do not add PCB order number
  Gold fingers        No.  Castellated holes: No.  Edge plating: No.

ASSEMBLY (form values)
  Type                Turnkey (PCBgogo buys all parts)
  Number of PCB       {qty_order} single boards. We want to start production as soon as review passes; please tell us the fastest path.
  Unique parts        {lines}
  SMD parts / board   {placements}  (top {sides.get('top', 0)}, bottom {sides.get('bottom', 0)})
  Through-hole parts  0
  Panel way           Panelized PCBs
  Assembly sides      {'Double sides' if sides.get('top') and sides.get('bottom') else 'Single side'}
  Stencil             Yes, {'top + bottom' if sides.get('top') and sides.get('bottom') else 'one side'}
  X-ray test          No
  Conformal coating   No
  Inspection          100% AOI
  Before production   send DFM report + assembly drawing for our approval

PARTS
  The BOM is final and correct. If a different part gives a better price, please propose it (MPN, datasheet, price); we approve before production.
  {dup}
  Rotation in positions file = JLCPCB / KiCad convention. Check pin 1 against the RED pad in the drawings.
""" + "".join(f"  {n}\n" for n in notes) + """
SHIPPING AND PAYMENT
  DAP (Incoterms 2020), DHL, to: Incutec BV, Stapelhuisstraat 15, 3000 Leuven, Belgium
  VAT / EORI: BE1038934039. Incutec is importer of record. NO DDP, no Belgian VAT, no duty in the quote.
  Payment: bank wire (no credit card).
"""
    return body


def common_block(stem, m, qty_order, placements, sides, dups, lines, tg, notes, fab, bomext, gerber_note, qty_note, via_note):
    L, W = m['size_mm']
    dup = ("No duplicate names." if not dups else
           "Some parts exist 2 or 3 times with the same name in KiCad (%s). In the BOM and "
           "positions file they are named %s_2, %s_3. Place every line." % (", ".join(dups), dups[0], dups[0]))
    return f"""{stem}  {fab.upper()} ORDER SHEET
Incutec BV, Leuven, Belgium.  Contact: Stan Coene, stan@incutec.eu

FILES
  {stem}.zip                    Gerber + drill (KiCad). {gerber_note}
  {stem}_bom_{fab}.{bomext}       BOM, {fab} template, {lines} lines
  {stem}_positions_{fab}.csv  Pick and place, mm, only parts in the BOM
  {stem}_assembly_top.png       Drawing top side. RED pad = pin 1
  {stem}_assembly_bottom.png    Drawing bottom side, MIRRORED (view from below). RED pad = pin 1

PCB
  Material            FR-4, TG {tg}
  Size (single)       {L:.1f} x {W:.1f} mm
  Quantity            {qty_order} single boards. {qty_note} This is a firm production order, not a prototype.
  Panel               Panel by you. We suggest 5 x 5 boards per panel; you decide the best panel and depanel method. Ship DEPANELED, edges cleaned (no tab remnants).
  Layers              {m['layers']}
  Thickness           {m['thickness_mm']:.1f} mm
  Min track/spacing   {m['min_track_mm']:.2f} mm / {m['min_clearance_mm']:.2f} mm ({track_tier(m['min_track_mm'], m['min_clearance_mm'])})
  Min hole            {m['min_drill_mm']:.2f} mm
  Solder mask         Green.  Silkscreen: white
  Surface finish      ENIG, 1U" min. No HASL.
  Finished copper     {m['copper']}
  Vias                Filled and capped (IPC-4761 Type VII). {m['via_in_pad']} vias are inside SMD pads. {via_note}
  Marking             No order number on the board. No castellated holes, no edge plating.

ASSEMBLY
  Type                Turnkey (you buy all parts)
  Quantity            {qty_order} boards. We want to start production as soon as review passes; please tell us the fastest path.
  Unique parts        {lines}
  SMD parts / board   {placements}  (top {sides.get('top', 0)}, bottom {sides.get('bottom', 0)})
  Through-hole parts  0
  Assembly sides      {'Double' if sides.get('top') and sides.get('bottom') else 'Single'}
  Stencil             Yes, {'top + bottom' if sides.get('top') and sides.get('bottom') else 'one side'}
  X-ray test          No.  Conformal coating: No.  Inspection: 100% AOI
  Before production   send DFM report + assembly drawing for our approval

PARTS
  The BOM is final and correct. If a different part gives a better price, please propose it (MPN, datasheet, price); we approve before production.
  {dup}
  Rotation in positions file = JLCPCB / KiCad convention. Check pin 1 against the RED pad in the drawings.
""" + "".join(f"  {n}\n" for n in notes) + """
SHIPPING AND PAYMENT
  DAP (Incoterms 2020), DHL, to: Incutec BV, Stapelhuisstraat 15, 3000 Leuven, Belgium
  VAT / EORI: BE1038934039. Incutec is importer of record. NO DDP, no Belgian VAT, no duty in the quote.
  Payment: bank wire.
"""


def sheet_nextpcb(stem, m, qty_order, placements, sides, dups, lines, tg, notes):
    return common_block(stem, m, qty_order, placements, sides, dups, lines, tg, notes, 'nextpcb', 'csv',
        "Drill maps and attribute comments removed for your parser.",
        f"Your form counts panels: with 5 x 5 that is {-(-qty_order // 25)} panels = {qty_order} boards. Please verify the single size on the form.",
        'Form option: "Non-Conductive Fill & Cap (VII)".')


def sheet_makerpcb(stem, m, qty_order, placements, sides, dups, lines, tg, notes):
    return common_block(stem, m, qty_order, placements, sides, dups, lines, tg, notes, 'makerpcb', 'xlsx',
        "Drill maps and attribute comments removed for your parser.",
        f"{qty_order} single boards, not panels. Board size in the gerbers is exact; the form only takes whole mm.",
        'Resin plug + cap; quote after your review is fine.')


BOM_EXT = {'pcbgogo': 'xlsx', 'nextpcb': 'csv', 'makerpcb': 'xlsx'}
GERBER_SRC = {'pcbgogo': '{stem}.zip', 'nextpcb': '{stem}_portal.zip', 'makerpcb': '{stem}_portal.zip'}
ORDER_SHEETS = {'pcbgogo': sheet_pcbgogo, 'nextpcb': sheet_nextpcb, 'makerpcb': sheet_makerpcb}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('board')
    ap.add_argument('--fab', default='pcbgogo', choices=sorted(ORDER_SHEETS))
    ap.add_argument('--qty', type=int, default=250)
    ap.add_argument('--name')
    ap.add_argument('--skip-pack', action='store_true', help='reuse production/quote-pack-<rev>/ as is')
    ap.add_argument('--out', help='output dir (default production/handoff-<rev>/)')
    ap.add_argument('--copper', help='override stackup, e.g. "2 oz outer, 1 oz inner"')
    ap.add_argument('--tg', default='170')
    ap.add_argument('--thickness', type=float, default=1.6, help='ordered board thickness mm (org rule 1.6; stackup sums are not the order value)')
    ap.add_argument('--note', action='append', default=[], help='extra line under PARTS, repeatable')
    a = ap.parse_args()

    board = os.path.abspath(a.board)
    bdir = os.path.dirname(board)
    stem = a.name or json.load(open(os.path.join(bdir, 'fabrication-toolkit-options.json')))['ARCHIVE_NAME']
    rev = re.search(r'-(rev[\w.]+)$', stem)
    if not rev:
        sys.exit(f"stem '{stem}' must end in -rev<...>")
    rev = rev.group(1)
    pack = os.path.join(bdir, 'production', f'quote-pack-{rev}')
    if not a.skip_pack:
        r = subprocess.run([sys.executable, os.path.join(HERE, 'quote_pack.py'), board] + (['--name', stem] if a.name else []))
        if r.returncode != 0:
            sys.exit("quote_pack failed")
    out = a.out or os.path.join(bdir, 'production', f'handoff-{rev}')
    work = os.path.join(out, f'{stem}_{a.fab}')
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)

    shutil.copy2(os.path.join(pack, GERBER_SRC[a.fab].format(stem=stem)), os.path.join(work, f'{stem}.zip'))
    placements, sides, dups, lines = build_bom_positions(pack, stem, a.fab, work)
    m = measure(board, a.copper)
    if abs(m['thickness_mm'] - a.thickness) > 0.05:
        print(f"note: stackup sums to {m['thickness_mm']:.2f} mm, ordering {a.thickness} mm")
    m['thickness_mm'] = a.thickness
    for f in assembly_drawing.render(board, stem, work, dpi=300, png=True):
        if f.endswith('.svg'):
            os.remove(f)
    sheet = ORDER_SHEETS[a.fab](stem, m, a.qty, placements, sides, dups, lines, a.tg, a.note)
    open(os.path.join(work, f'{stem}_{a.fab.upper()}-ORDER-SHEET.txt'), 'w').write(sheet)

    # independent DFM double-check on the gerber zip the fab receives
    chk = os.path.join(out, f'{stem}_DFM-CHECK.txt')
    r = subprocess.run([sys.executable, os.path.join(HERE, 'gerber_check.py'),
                        os.path.join(work, f'{stem}.zip'), '--board', board,
                        '--min-track', f"{min(m['min_track_mm'], m['min_clearance_mm']):.3f}",
                        '--min-drill', f"{m['min_drill_mm']:.3f}", '-o', chk],
                       capture_output=True, text=True)
    print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:])
    if r.returncode != 0:
        sys.exit(f"gerber_check FAILED, see {chk}")

    zpath = os.path.join(out, f'{stem}_{a.fab}.zip')
    if os.path.exists(zpath):
        os.remove(zpath)
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(work)):
            z.write(os.path.join(work, f), f)
    print(f"{zpath}: {lines} BOM lines, {placements} placements {dict(sides)}, "
          f"{m['size_mm'][0]:.1f}x{m['size_mm'][1]:.1f} mm, {m['layers']}L, {m['copper']}, "
          f"min track {m['min_track_mm']:.2f}/{m['min_clearance_mm']:.2f} mm, min drill {m['min_drill_mm']:.2f} mm, "
          f"{m['via_in_pad']} via-in-pad, dups {dups or 'none'}")


if __name__ == '__main__':
    main()
