#!/usr/bin/env python3
"""model_audit.py - find 3D models that cost more than the shape they describe.

Component models imported by easyeda2kicad run 20-100x denser than KiCad's own
model for the same package, and most of them carry the LCEDA watermark modelled
as raised geometry: on a QFN-24 that is 98 spline-surfaced faces of lettering
sitting on a chip that is otherwise boxes and cylinders. This reports what each
model costs and, where one exists, a leaner replacement of the SAME SHAPE.

For one board: list every referenced 3D model, measure it, then look for a
cheaper replacement that is the SAME SHAPE. A candidate is only offered when its
bounding box matches the incumbent within a tolerance, so a swap can shrink a
model but never change what the board looks like.

Sources searched, in order of trust:
  1. other repositories under --root - same filename already used elsewhere
  2. KiCad stock 3dmodels       - matched on package designator in the name

Nothing is modified. Feed the accepted swaps to apply_models.py.

Usage:
  model_audit.py <board.kicad_pcb> [--root DIR] [--tol-mm 0.15]
  model_audit.py --measure <file.step> [...]      # just measure, no audit
"""
import argparse, re, os, glob, collections

KI = '/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels'
HW = ""

# ---------- STEP measurement ----------
_cache = {}
def measure(path):
    """(faces, kB, dims_mm, nverts, spline_surfaces, spline_curves) for a STEP.

    Two formatting traps, both of which silently produced empty measurements:
    entities wrap across lines, and the SolidWorks-derived easyeda exports write
    "CARTESIAN_POINT ( 'NONE', ( ..." with spaces where KiCad's own models write
    "CARTESIAN_POINT('',(...". Every pattern here tolerates both, or the exact
    models worth auditing are the ones that drop out.
    """
    if path in _cache: return _cache[path]
    try:
        s = open(path, encoding='utf8', errors='replace').read()
    except OSError:
        return None
    s = re.sub(r'\s*\n\s*', ' ', s)
    def count(ent):
        return len(re.findall(r'=\s*' + ent + r'\s*\(', s))
    faces = count('ADVANCED_FACE')
    # Extruded lettering and logos are spline-bounded. A moulded package is
    # planes and cylinders, so splines on one mean vector artwork was modelled
    # as real geometry: the LCEDA watermark easyeda2kicad bakes into its models.
    # Vector artwork shows up as spline CURVES. Some easyeda models keep the
    # lettering as spline-surfaced faces, but the densest ones tessellate it into
    # thousands of planar facets still bounded by spline curves, so a
    # surface-only test misses exactly the worst offenders. Stock KiCad models
    # sit at <= 24 spline curves (a rounded pin-1 dimple); hundreds means text.
    spl_s = count('B_SPLINE_SURFACE_WITH_KNOTS')
    spl_c = count('B_SPLINE_CURVE_WITH_KNOTS')
    pts = {}
    for i, a, b, c in re.findall(
            r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*"
            r"([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)", s):
        pts[i] = (float(a), float(b), float(c))
    vids = re.findall(r"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)", s)
    v = [pts[i] for i in vids if i in pts] or list(pts.values())
    if not v: return None
    dims = tuple(max(q[k] for q in v) - min(q[k] for q in v) for k in range(3))
    r = (faces, os.path.getsize(path)/1024, dims, len(v), spl_s, spl_c)
    _cache[path] = r
    return r

def resolve(m, prjdir):
    """kicad-cli --subst-models: a .wrl is served by its .step sibling."""
    p = (m.replace('${KIPRJMOD}', prjdir)
          .replace('${KICAD10_3DMODEL_DIR}', KI).replace('${KICAD9_3DMODEL_DIR}', KI)
          .replace('${KICAD8_3DMODEL_DIR}', KI))
    base = os.path.splitext(p)[0]
    for ext in ('.step', '.stp', '.STEP'):
        if os.path.exists(base+ext): return base+ext
    return p if os.path.exists(p) else None

# ---------- candidate index ----------
def build_index():
    idx = collections.defaultdict(list)     # basename -> [paths]
    allm = []
    for p in glob.glob(f'{HW}/**/*.3dshapes/*.step', recursive=True):
        idx[os.path.basename(p)].append(p); allm.append(('tree', p))
    for p in glob.glob(f'{KI}/*.3dshapes/*.step'):
        allm.append(('kicad', p))
    return idx, allm

# Package designator pulled out of a model name, used to propose stock matches.
PKG = re.compile(r'\b(QFN-\d+|DFN-\d+|WSON-\d+|X2SON-\d+|SON-\d+|SOT-\d+-\d+|SOT-\d+|'
                 r'SOD-\d+|SOIC-\d+|TSSOP-\d+|MSOP-\d+|LGA-\d+|BGA-\d+|SC-\d+|'
                 r'TO-\d+|PDFN-\d+|SOP-\d+)\b', re.I)

def pkg_of(name):
    m = PKG.search(name.replace('_', ' ').replace('-TL', '').upper())
    return m.group(1).upper() if m else None

def fits(a, b, tol):
    """Same shape? Compare sorted extents so orientation differences don't matter."""
    if not a or not b: return False
    A, B = sorted(a), sorted(b)
    return all(abs(x-y) <= tol for x, y in zip(A, B))

def main():
    global HW
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pcb', nargs='?')
    ap.add_argument('--root', help='root searched for same-name replacement models; defaults to the PCB directory')
    ap.add_argument('--tol-mm', type=float, default=0.15)
    ap.add_argument('--measure', nargs='+', metavar='STEP')
    a = ap.parse_args()
    if a.measure:
        for p in a.measure:
            r = measure(p)
            if not r:
                print(f"UNREADABLE  {p}")
                continue
            faces, kb, dims, _nv, _ss, spl = r
            d = sorted(dims)
            print(f"{faces:6d} faces {kb:8.0f} kB  dims {d[0]:6.2f} x {d[1]:6.2f} x {d[2]:6.2f}  "
                  f"{('ARTWORK(%d)' % spl) if spl >= 100 else 'clean':14s}  {p}")
        return
    if not a.pcb:
        ap.error('a .kicad_pcb or --measure is required')
    pcb = a.pcb
    tol = a.tol_mm
    HW = os.path.abspath(os.path.expanduser(a.root or os.path.dirname(pcb) or '.'))
    prjdir = os.path.dirname(os.path.abspath(pcb))
    s = open(pcb, encoding='utf8', errors='replace').read()
    used = collections.Counter(re.findall(r'\(model\s+"([^"]+)"', s))
    idx, allm = build_index()

    rows = []
    for m, n in used.items():
        p = resolve(m, prjdir)
        info = measure(p) if p and p.lower().endswith(('.step', '.stp')) else None
        if not info: continue
        faces, kb, dims, nv, spl_s, spl_c = info
        rows.append(dict(name=os.path.basename(os.path.splitext(m)[0]), path=p, n=n,
                         faces=faces, kb=kb, dims=dims, spl_s=spl_s, spl_c=spl_c))
    rows.sort(key=lambda r: -r['faces']*r['n'])

    print(f"\n{os.path.basename(pcb)}: {sum(used.values())} placements, "
          f"{len(rows)} STEP-resolvable models, "
          f"{sum(r['faces']*r['n'] for r in rows):,} faces total\n")
    print(f"{'faces*n':>9} {'each':>6} {'n':>4} {'KB':>7}  {'size mm':>18} {'art':>5}  model")
    for r in rows:
        d = ' x '.join(f'{x:.2f}' for x in r['dims'])
        art = 'ART' if r['spl_c'] >= 100 else ''
        print(f"{r['faces']*r['n']:9,} {r['faces']:6,} {r['n']:4d} {r['kb']:7.0f}  {d:>18} {art:>5}  {r['name'][:44]}")
    branded = [r for r in rows if r['spl_c'] >= 100]
    if branded:
        print(f"\n  {len(branded)} model(s) carry modelled vector artwork (LCEDA logo / part marking):")
        for r in branded:
            print(f"    {r['name'][:48]:48s} {r['spl_s']:4d} spline surfaces, {r['spl_c']:5d} spline curves, x{r['n']}")

    print(f"\n--- replacement candidates (same shape within {tol} mm) ---")
    found = 0
    for r in rows:
        cands = []
        base = os.path.basename(r['path'])
        # 1. same filename elsewhere in the org
        for q in idx.get(base, []):
            if os.path.abspath(q) == os.path.abspath(r['path']): continue
            info = measure(q)
            if info and info[0] < r['faces'] and fits(info[2], r['dims'], tol):
                cands.append(('same file, other repo', q, info))
        # 2. stock KiCad model of the same package
        pk = pkg_of(r['name'])
        if pk:
            for src, q in allm:
                if src != 'kicad': continue
                if pkg_of(os.path.basename(q)) != pk: continue
                info = measure(q)
                if info and info[0] < r['faces'] and fits(info[2], r['dims'], tol):
                    cands.append(('kicad stock', q, info))
        if not cands: continue
        cands.sort(key=lambda c: c[2][0])
        found += 1
        best = cands[0]
        saved = (r['faces'] - best[2][0]) * r['n']
        print(f"\n  {r['name'][:56]}")
        print(f"     now  {r['faces']:6,} f  {r['kb']:7.0f} KB  {' x '.join(f'{x:.2f}' for x in r['dims'])} mm  (x{r['n']})")
        for why, q, info in cands[:3]:
            print(f"     ->   {info[0]:6,} f  {info[1]:7.0f} KB  {' x '.join(f'{x:.2f}' for x in info[2])} mm  [{why}]")
            print(f"          {os.path.relpath(q, os.path.expanduser('~'))}")
        print(f"     saves {saved:,} faces")
    if not found:
        print("  none")

main()
