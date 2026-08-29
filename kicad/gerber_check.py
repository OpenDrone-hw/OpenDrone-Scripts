#!/usr/bin/env python3
"""Independent DFM double-check of an exported gerber set, read from the zip
the fab will receive, not from the board. Stdlib only, plus optional
kicad-cli DRC. Written because the only checks before this ran on the board
file; nothing looked at what actually left the building.

    python3 gerber_check.py <gerbers.zip> [--board board.kicad_pcb]
                            [--min-track 0.09] [--min-drill 0.2] [-o report.txt]

Checks, each PASS / WARN / FAIL:
  G1  file set: F/B copper, inner layers, F/B mask, F/B paste, F/B silk,
      edge cuts, PTH drill (NPTH optional); every gerber carries an X2
      FileFunction attribute and the same %FS/%MO format
  G2  min drawn line width per copper layer (smallest circular aperture used
      by a D01 draw) against --min-track; smallest flash/draw on outer layers
  G3  drill: tool sizes and hit counts, smallest against --min-drill,
      PTH/NPTH split, any tool below 0.15 mm
  G4  outline: Edge.Cuts is closed (every endpoint has even degree);
      overlapping segments reported as WARN; reports the outline size
  G5  every copper/mask/paste feature lies inside the outline bbox (+0.3 mm);
      WARN lists the layer and extents (stray text or shapes outside the board)
  G6  optional: kicad-cli DRC on --board (errors only), summarised
Exit 1 on any FAIL.
"""
import argparse, collections, io, math, os, re, subprocess, sys, zipfile

KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"


def classify(name, text):
    m = re.search(r'(?:%TF|G04 #@! TF)\.FileFunction,([^*]+)\*', text)
    ff = m.group(1) if m else ''
    n = name.lower()
    if not ff:  # portal zips have attributes stripped: fall back to KiCad names
        ext = os.path.splitext(n)[1]
        by_ext = {'.gtl': 'Copper,L1,Top', '.gbl': 'Copper,Bot', '.gts': 'Soldermask,Top', '.gbs': 'Soldermask,Bot',
                  '.gtp': 'Paste,Top', '.gbp': 'Paste,Bot', '.gto': 'Legend,Top', '.gbo': 'Legend,Bot',
                  '.gm1': 'Profile,NP', '.gko': 'Profile,NP'}
        ff = by_ext.get(ext, 'Copper,Inner' if re.fullmatch(r'\.g\d', ext) else '')
    if ff.startswith('Copper'):
        side = 'top' if 'Top' in ff else 'bot' if 'Bot' in ff else 'inner'
        return ('cu', side, ff)
    if ff.startswith('Soldermask'):
        return ('mask', 'top' if 'Top' in ff else 'bot', ff)
    if ff.startswith('Paste'):
        return ('paste', 'top' if 'Top' in ff else 'bot', ff)
    if ff.startswith('Legend'):
        return ('silk', 'top' if 'Top' in ff else 'bot', ff)
    if ff.startswith('Profile') or n.endswith('.gm1'):
        return ('edge', '', ff)
    if ff.startswith('Plated'):
        return ('pth', '', ff)
    if ff.startswith('NonPlated'):
        return ('npth', '', ff)
    if n.endswith('.drl'):
        return ('npth' if 'npth' in n else 'pth', '', 'drill')
    if 'drl_map' in n:
        return ('map', '', ff)
    return ('other', '', ff)


def parse_gerber(text):
    """Apertures, draws/flashes with coordinates in mm; format from %FS."""
    fs = re.search(r'%FSLAX(\d)(\d)Y(\d)(\d)\*%', text)
    dec = int(fs.group(2)) if fs else 6
    unit = 25.4 if '%MOIN*%' in text else 1.0
    scale = unit / (10 ** dec)
    aps = {}
    for m in re.finditer(r'%ADD(\d+)([A-Za-z_.$0-9]+),([^*]+)\*%', text):
        code, shape, params = m.group(1), m.group(2), [float(p) * unit for p in m.group(3).split('X')]
        aps[code] = (shape, params)
    cur = None
    draws = collections.Counter()   # aperture -> D01 count
    flashes = collections.Counter()
    segs, pts = [], []
    x = y = 0.0
    last = None
    for line in text.splitlines():
        m = re.match(r'^(?:G0[123])?D(\d+)\*$', line)
        if m and m.group(1) not in ('01', '02', '03'):
            cur = m.group(1)
            continue
        m = re.match(r'^(?:G0[123])?(?:X(-?\d+))?(?:Y(-?\d+))?(?:I-?\d+)?(?:J-?\d+)?D0([123])\*$', line)
        if not m:
            continue
        if m.group(1) is not None:
            x = int(m.group(1)) * scale
        if m.group(2) is not None:
            y = int(m.group(2)) * scale
        op = m.group(3)
        if op == '1':
            draws[cur] += 1
            if last is not None:
                segs.append((last, (x, y)))
        elif op == '3':
            flashes[cur] += 1
        pts.append((x, y))
        last = (x, y)
    return aps, draws, flashes, segs, pts, (dec, unit)


def min_width(aps, draws):
    ws = [aps[a][1][0] for a in draws if a in aps and aps[a][0] == 'C' and aps[a][1]]
    return min(ws) if ws else None


def parse_drill(text):
    unit = 25.4 if re.search(r'^INCH', text, re.M) else 1.0
    tools = {}
    for m in re.finditer(r'^T(\d+)C([\d.]+)', text, re.M):
        tools[m.group(1)] = float(m.group(2)) * unit
    hits = collections.Counter()
    cur = None
    body = text.split('%', 1)[-1]
    for line in body.splitlines():
        m = re.match(r'^T(\d+)$', line)
        if m:
            cur = m.group(1)
            continue
        if re.match(r'^X-?[\d.]+Y-?[\d.]+', line) and cur:
            hits[cur] += 1
    return tools, hits


def closed_loops(segs, tol=0.002):
    """(open endpoints, overlap junctions): endpoint degree odd = open,
    degree > 2 = outline segments overlapping (board edge + footprint edge)."""
    deg = collections.Counter()
    key = lambda p: (round(p[0] / tol), round(p[1] / tol))
    for a, b in segs:
        deg[key(a)] += 1
        deg[key(b)] += 1
    return [k for k, v in deg.items() if v % 2], [k for k, v in deg.items() if v > 2]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('zip')
    ap.add_argument('--board')
    ap.add_argument('--min-track', type=float, default=0.09)
    ap.add_argument('--min-drill', type=float, default=0.2)
    ap.add_argument('-o', '--output')
    a = ap.parse_args()

    out, fails = [], 0

    def say(code, status, msg):
        nonlocal fails
        if status == 'FAIL':
            fails += 1
        out.append(f"{code} {status:4s} {msg}")

    z = zipfile.ZipFile(a.zip)
    files = {}
    for n in z.namelist():
        if n.endswith('/'):
            continue
        t = z.read(n).decode('latin-1')
        files[n] = (classify(n, t), t)

    # G1 file set + format consistency
    kinds = collections.defaultdict(list)
    for n, (c, t) in files.items():
        kinds[(c[0], c[1])].append(n)
    need = [('cu', 'top'), ('cu', 'bot'), ('mask', 'top'), ('mask', 'bot'), ('paste', 'top'),
            ('paste', 'bot'), ('silk', 'top'), ('silk', 'bot'), ('edge', ''), ('pth', '')]
    missing = [k for k in need if k not in kinds]
    inner = len(kinds.get(('cu', 'inner'), []))
    fmts = set()
    noattr = []
    for n, (c, t) in files.items():
        if c[0] in ('cu', 'mask', 'paste', 'silk', 'edge'):
            fs = re.search(r'%FSLAX\d\dY\d\d\*%', t)
            mo = re.search(r'%MO(MM|IN)\*%', t)
            fmts.add((fs.group(0) if fs else None, mo.group(1) if mo else None))
            if not re.search(r'(?:%TF|G04 #@! TF)\.FileFunction', t):
                noattr.append(n)
    say('G1', 'FAIL' if missing else 'PASS',
        f"{2 + inner} copper layers ({inner} inner), {len(files)} files" + (f"; MISSING {missing}" if missing else ''))
    say('G1', 'PASS' if len(fmts) == 1 else 'FAIL', f"gerber format {sorted(fmts)}")
    if noattr:
        say('G1', 'WARN', f"no X2 FileFunction attribute: {noattr}")

    # G4 outline first (needed for G5)
    edge_pts, edge_segs = [], []
    for n in kinds.get(('edge', ''), []):
        aps, draws, flashes, segs, pts, _ = parse_gerber(files[n][1])
        edge_pts += pts
        edge_segs += segs
    if edge_pts:
        xs, ys = [p[0] for p in edge_pts], [p[1] for p in edge_pts]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        odd, over = closed_loops(edge_segs)
        say('G4', 'PASS' if not odd else 'FAIL',
            f"outline {bbox[2] - bbox[0]:.2f} x {bbox[3] - bbox[1]:.2f} mm, {len(edge_segs)} segments, "
            f"{'closed' if not odd else f'{len(odd)} open endpoints'}")
        if over:
            say('G4', 'WARN', f"{len(over)} points where outline segments overlap (edge drawn twice, e.g. board edge + footprint edge); CAM may ask")
    else:
        bbox = None
        say('G4', 'FAIL', 'no outline layer')

    # G2 copper widths, G5 containment
    for n in sorted(files):
        c, t = files[n]
        if c[0] not in ('cu', 'mask', 'paste'):
            continue
        aps, draws, flashes, segs, pts, _ = parse_gerber(t)
        if c[0] == 'cu':
            w = min_width(aps, draws)
            st = 'PASS' if w is None or w >= a.min_track - 1e-6 else 'FAIL'
            say('G2', st, f"{n}: min drawn width {w if w is None else f'{w:.3f} mm'}, "
                          f"{sum(draws.values())} draws, {sum(flashes.values())} flashes")
        if bbox and pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            tol = 0.3
            outside = (min(xs) < bbox[0] - tol or min(ys) < bbox[1] - tol or
                       max(xs) > bbox[2] + tol or max(ys) > bbox[3] + tol)
            if outside:
                say('G5', 'WARN', f"{n}: features extend past the outline bbox "
                                  f"({min(xs):.1f}..{max(xs):.1f}, {min(ys):.1f}..{max(ys):.1f})")
    say('G5', 'PASS', 'containment checked on every copper/mask/paste layer (WARN lines above if any)')

    # G3 drills
    for kind in ('pth', 'npth'):
        for n in kinds.get((kind, ''), []):
            if not n.lower().endswith('.drl'):
                continue
            tools, hits = parse_drill(files[n][1])
            if not tools:
                say('G3', 'WARN', f"{n}: no tools")
                continue
            dmin = min(tools.values())
            desc = ', '.join(f"{tools[k]:.2f}mm x{hits.get(k, 0)}" for k in sorted(tools, key=lambda k: tools[k]))
            st = 'PASS' if (kind == 'npth' or dmin >= a.min_drill - 1e-6) else 'FAIL'
            if dmin < 0.15:
                st = 'FAIL'
            say('G3', st, f"{n}: {desc}")

    # G6 DRC
    if a.board and os.path.exists(KICAD_CLI):
        rep = os.path.join(os.path.dirname(os.path.abspath(a.zip)), '_drc.json')
        r = subprocess.run([KICAD_CLI, 'pcb', 'drc', '--severity-error', '--format', 'json', '-o', rep, a.board],
                           capture_output=True, text=True)
        if os.path.exists(rep):
            import json
            d = json.load(open(rep))
            viol = d.get('violations', [])
            unc = d.get('unconnected_items', [])
            by = collections.Counter(v.get('type') for v in viol)
            say('G6', 'PASS' if not viol and not unc else 'WARN',
                f"kicad DRC errors: {len(viol)} ({dict(by)}), unconnected: {len(unc)}")
            os.remove(rep)
        else:
            say('G6', 'WARN', f"kicad-cli drc produced no report: {r.stderr.strip()[-200:]}")

    text = "\n".join(out) + f"\n== {'FAIL' if fails else 'PASS'}: {os.path.basename(a.zip)}\n"
    print(text, end='')
    if a.output:
        open(a.output, 'w').write(text)
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
