#!/usr/bin/env python3
"""One-shot fab-agnostic quote pack for a board.

    $KPY quote_pack.py <board.kicad_pcb> [--name STEM] [--skip-ft] [--boms-only]

STEM defaults to ARCHIVE_NAME in fabrication-toolkit-options.json next to
the board and must follow the org convention <Repo>-<rev> (lowercase rev,
matches release asset naming in the org CONTRIBUTING).

Produces production/quote-pack-<rev>/ with, for every big supplier:

  <stem>.zip                 FT gerbers+drill (JLCPCB, PCBGOGO)
  <stem>_portal.zip          same, drill maps dropped + G04 #@! attribute
                             comments stripped (NextPCB, MakerPCB, weak parsers)
  <stem>_bom_universal.csv   Designator,Value,Footprint,Quantity,LCSC,
                             Manufacturer,MPN (PCBGOGO, NextPCB, generic RFQ)
  <stem>_bom_jlcpcb.csv      FT's JLC-format BOM (Designator,Footprint,
                             Quantity,Value,LCSC Part #)
  <stem>_bom_nextpcb.csv     NextPCB template columns
  <stem>_bom_makerpcb.xlsx   MakerPCB template columns (their portal
                             rejects anything but their xlsx layout)
  <stem>_bom_pcbgogo.xlsx    PCBGOGO template columns (bare TPs marked DNS)
  <stem>_positions.csv/.zip  FT pick and place (JLC rotation convention)

--skip-ft reuses the existing FT export in production/ instead of
re-running Fabrication Toolkit headless. --boms-only additionally leaves
the gerber and positions files already in the pack untouched (use when the
pack gerbers are pinned to a submitted order). SPEC.md is never touched.
Fabrication Toolkit is loaded from the KiCad 3rdparty plugin dir; run with
KiCad's bundled python.
"""
import argparse, csv, json, os, re, shutil, subprocess, sys, zipfile

GERBER_EXT = ('.gtl', '.gbl', '.gts', '.gbs', '.gtp', '.gbp', '.gto', '.gbo',
              '.gm1', '.gbr', '.g1', '.g2', '.g3', '.g4', '.g5', '.g6')


def sync_rev_text(board_path, rev):
    """Make the board's rev silkscreen text and title block match the export
    rev. Only existing text matching rev<digits> is updated (case style
    preserved); boards with no rev text are left alone. Saves the board."""
    import pcbnew
    b = pcbnew.LoadBoard(board_path)
    changed = []
    tb = b.GetTitleBlock()
    if tb.GetRevision() != rev:
        tb.SetRevision(rev)
        changed.append(f"titleblock '{tb.GetRevision()}'")
    for d in b.GetDrawings():
        if isinstance(d, pcbnew.PCB_TEXT):
            t = d.GetText().strip()
            if re.fullmatch(r'(?i)rev[0-9][\w.]*', t) and t.lower() != rev:
                new_t = rev.upper() if t.startswith('REV') else rev
                d.SetText(new_t)
                changed.append(f"text '{t}' -> '{new_t}'")
    if changed:
        pcbnew.SaveBoard(board_path, b)
        print(f"rev sync: {', '.join(changed)}")
    return bool(changed)


def run_ft(board, stem):
    """Fabrication Toolkit headless, via fab_export.py in this directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run([sys.executable, os.path.join(here, 'fab_export.py'),
                        board, '--name', stem], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"fab_export failed:\n{r.stderr[-2000:]}")


def portal_zip(src, dst):
    with zipfile.ZipFile(src) as zin, \
         zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zout:
        for i in zin.infolist():
            if i.filename.endswith('-drl_map.gbr'):
                continue
            data = zin.read(i.filename)
            if i.filename.lower().endswith(GERBER_EXT):
                lines = data.decode('utf-8', 'replace').splitlines(keepends=True)
                data = ''.join(l for l in lines if not l.startswith('G04 #@!')).encode()
            zout.writestr(i, data)


def read_universal(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def write_nextpcb(rows, path):
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['Designator', 'Quantity', 'Manufacturer Part Number',
                    'Manufacturer', 'Package/Footprint', 'Description',
                    'Procurement Type', 'Customer Note'])
        for r in rows:
            w.writerow([r['Designator'], r['Quantity'], r['MPN'],
                        r['Manufacturer'], r['Footprint'], r['Value'], '', ''])


def write_xlsx(data, path):
    """Minimal stdlib single-sheet xlsx from a list of rows."""
    def esc(s):
        return (str(s).replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;'))

    def cell(ref, v):
        # empty cells are omitted entirely: some portal-side readers (MakerPCB)
        # crash on empty inline-string cells
        if v == '' or v is None:
            return ''
        if isinstance(v, int):
            return f'<c r="{ref}" s="0"><v>{v}</v></c>'
        return f'<c r="{ref}" s="0" t="inlineStr"><is><t>{esc(v)}</t></is></c>'

    body = []
    for rn, row in enumerate(data, start=1):
        cells = ''.join(cell(f'{chr(65 + cn)}{rn}', v) for cn, v in enumerate(row))
        body.append(f'<row r="{rn}">{cells}</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'<sheetData>{"".join(body)}</sheetData></worksheet>')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>')
        z.writestr('xl/styles.xml',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="1"><xf xfId="0"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>')
        z.writestr('_rels/.rels',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
        z.writestr('xl/workbook.xml',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Worksheet" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', sheet)


def write_makerpcb(rows, path):
    """MakerPCB template layout: Item, Ref., MPN, Digikey/Mouser PN, Quantity."""
    write_xlsx([['Item', 'Ref.', 'MPN', 'Digikey/Mouser PN', 'Quantity']] + [
        [i + 1, r['Designator'], r['MPN'], '', int(r['Quantity'])]
        for i, r in enumerate(rows)], path)


def write_pcbgogo(rows, path):
    """PCBGOGO template layout. Bare test pads (TP refs with no part) are
    marked DNS so their sourcing pass skips them."""
    data = [['Item #', '*Ref Des', '*Qty', 'Manufacturer', '*Mfg Part #',
             'Description / Value', '*Package', 'Type',
             'Your Instructions / Notes']]
    for i, r in enumerate(rows):
        bare = not r['MPN'] and not r['LCSC'] and r['Designator'].startswith('TP')
        data.append([i + 1, r['Designator'], int(r['Quantity']),
                     r['Manufacturer'], r['MPN'], r['Value'], r['Footprint'],
                     'DNS' if bare else 'SMD',
                     'Bare test pad, do not populate' if bare
                     else (f"LCSC {r['LCSC']}" if r['LCSC'] else '')])
    write_xlsx(data, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('board')
    ap.add_argument('--name')
    ap.add_argument('--skip-ft', action='store_true')
    ap.add_argument('--boms-only', action='store_true')
    a = ap.parse_args()

    board = os.path.abspath(a.board)
    bdir = os.path.dirname(board)
    prod = os.path.join(bdir, 'production')
    stem = a.name
    if not stem:
        oj = os.path.join(bdir, 'fabrication-toolkit-options.json')
        stem = json.load(open(oj))['ARCHIVE_NAME']
    m = re.search(r'-(rev[\w.]+)$', stem)
    if not m:
        sys.exit(f"stem '{stem}' does not end in -rev<...>; fix ARCHIVE_NAME "
                 "or pass --name <Repo>-<rev>")
    pack = os.path.join(prod, f'quote-pack-{m.group(1)}')
    os.makedirs(pack, exist_ok=True)

    if not a.boms_only:
        if sync_rev_text(board, m.group(1)) and a.skip_ft:
            print("warning: rev text changed but --skip-ft reuses an export "
                  "made before the change", file=sys.stderr)
    if not a.skip_ft and not a.boms_only:
        run_ft(board, stem)

    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run([sys.executable, os.path.join(here, 'universal_bom.py'),
                        board, '--name', stem], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"universal_bom failed:\n{r.stderr[-2000:]}")
    for line in r.stderr.splitlines():
        if 'INCOMPLETE' in line:
            print(line, file=sys.stderr)

    uni = os.path.join(prod, f'{stem}_bom_universal.csv')
    rows = read_universal(uni)
    shutil.copy2(uni, pack)
    write_nextpcb(rows, os.path.join(pack, f'{stem}_bom_nextpcb.csv'))
    write_makerpcb(rows, os.path.join(pack, f'{stem}_bom_makerpcb.xlsx'))
    write_pcbgogo(rows, os.path.join(pack, f'{stem}_bom_pcbgogo.xlsx'))
    ftbom = os.path.join(prod, f'{stem}_bom.csv')
    if os.path.exists(ftbom):
        shutil.copy2(ftbom, os.path.join(pack, f'{stem}_bom_jlcpcb.csv'))

    if not a.boms_only:
        gz = os.path.join(prod, f'{stem}.zip')
        shutil.copy2(gz, pack)
        portal_zip(gz, os.path.join(pack, f'{stem}_portal.zip'))
        pos = os.path.join(prod, f'{stem}_positions.csv')
        shutil.copy2(pos, pack)
        pz = os.path.join(pack, f'{stem}_positions.zip')
        if os.path.exists(pz):
            os.remove(pz)
        with zipfile.ZipFile(pz, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(os.path.join(pack, f'{stem}_positions.csv'),
                    f'{stem}_positions.csv')

    print(f"{pack}: {len(rows)} BOM lines -> universal, jlcpcb, nextpcb, makerpcb, pcbgogo{' (boms only)' if a.boms_only else ' + gerbers, portal, positions'}")

    r = subprocess.run([sys.executable, os.path.join(here, 'check_export.py'),
                        board, '--prefix', stem], capture_output=True, text=True)
    for line in (r.stdout + r.stderr).splitlines():
        if line.startswith(('C0', 'C1', 'C2', 'C3', '==')):
            print(line)
    if r.returncode != 0:
        sys.exit("check_export FAILED: the pack does not match the board")


if __name__ == '__main__':
    main()
