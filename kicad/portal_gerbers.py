#!/usr/bin/env python3
"""Portal-safe gerber zip from a Fabrication Toolkit export.

    python3 portal_gerbers.py <stem>.zip [-o <out>.zip]

Takes the FT gerber zip and writes <stem>_portal.zip next to it:
  - drops the *-drl_map.gbr drill map documentation files
  - strips G04 #@! attribute comment lines (TF/TA/TO metadata) from
    gerber layers; they are spec-ignorable comments, geometry is untouched
  - renames the board outline from KiCad's -Edge_Cuts.gm1 to .gko, the
    Protel keepout name legacy portal tools expect the outline under
  - leaves Excellon drill files unmodified

Fabrication output is identical. Use this variant for quote portals with
weak parsers (NextPCB, MakerPCB); JLCPCB/PCBGOGO take the full FT zip.
"""
import sys, zipfile, argparse

GERBER_EXT = ('.gtl', '.gbl', '.gts', '.gbs', '.gtp', '.gbp', '.gto', '.gbo',
              '.gm1', '.gbr', '.g1', '.g2', '.g3', '.g4', '.g5', '.g6')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('zip')
    ap.add_argument('-o', '--out')
    a = ap.parse_args()
    out = a.out or a.zip[:-4] + '_portal.zip'
    kept = dropped = 0
    with zipfile.ZipFile(a.zip) as zin, \
         zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
        for i in zin.infolist():
            if i.filename.endswith('-drl_map.gbr'):
                dropped += 1
                continue
            data = zin.read(i.filename)
            if i.filename.lower().endswith(GERBER_EXT):
                lines = data.decode('utf-8', 'replace').splitlines(keepends=True)
                data = ''.join(l for l in lines if not l.startswith('G04 #@!')).encode()
            name = i.filename
            if name.endswith('-Edge_Cuts.gm1'):
                name = name[:-len('-Edge_Cuts.gm1')] + '.gko'
            zout.writestr(name, data)
            kept += 1
    print(f"{out}: {kept} files, {dropped} drill maps dropped")

if __name__ == '__main__':
    main()
