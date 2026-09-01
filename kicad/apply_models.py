#!/usr/bin/env python3
"""apply_models.py - retarget 3D model references, in the LIBRARY and on boards.

Run with KiCad's bundled Python.

Fixing only the .kicad_pcb is surface level: the next board to place that
footprint pulls the bloated model again. Fixing only the .pretty is invisible:
boards carry their own copy of each footprint, so nothing changes until they are
refreshed. This does both, and touches ONLY the 3D model reference: pads, nets,
placement, values and the land pattern are never read or written.

Offset and rotation are reset on a swap. KiCad stock models are authored
origin-at-footprint-origin with the board face at Z=0; imported models may carry
a compensating offset that does not transfer to a replacement.

  apply_models.py --root DIR --map map.json [--apply]      retarget model files
  apply_models.py --root DIR --fixes fixes.json [--apply]  correct placement
  apply_models.py --root DIR --audit                       find placement to correct

All three are a dry run unless --apply is given.

map.json: {"<old model basename, no extension>": "<new ${KICAD10_3DMODEL_DIR}/... path>"}

model-fixes.json is the CATALOGUE of placement corrections: a stock model
authored for a differently drawn footprint, or one authored below its own
origin, needs a rotation or offset that no library carries. Its values are
absolute, so applying it twice changes nothing and the file always states
the current intent. Format and the reasoning behind each entry are in the
file itself; --audit is how new entries are found rather than noticed by
eye in CAD.
"""
import json, math, os, re, glob, argparse, collections
import pcbnew

HW = ""
KICAD_3D = ('/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels')

def swap_models(container, mapping, stats, where):
    """container: a FOOTPRINT. Returns True if it changed."""
    rebuilt, changed = [], False
    for m in container.Models():
        stem = os.path.splitext(os.path.basename(str(m.m_Filename)))[0]
        nm = pcbnew.FP_3DMODEL()
        if stem in mapping:
            nm.m_Filename = mapping[stem]
            nm.m_Scale, nm.m_Show = m.m_Scale, m.m_Show   # offset/rotation reset
            changed = True
            stats[stem] += 1
        else:
            nm.m_Filename = str(m.m_Filename)
            nm.m_Offset, nm.m_Rotation = m.m_Offset, m.m_Rotation
            nm.m_Scale, nm.m_Show = m.m_Scale, m.m_Show
        rebuilt.append(nm)
    if changed:
        # Models() hands back copies in the SWIG binding, so mutating them writes
        # nothing back. The list has to be cleared and refilled.
        container.Models().clear()
        for nm in rebuilt:
            container.Models().push_back(nm)
    return changed

def stem_of(model):
    return os.path.splitext(os.path.basename(str(model.m_Filename)))[0]


def fp_name(container):
    """Footprint name without its library nickname."""
    return str(container.GetFPIDAsString()).split(':')[-1]


def matching_fix(container, model, fixes):
    name, stem = fp_name(container), stem_of(model)
    for fix in fixes:
        if re.search(fix['footprint'], name) and re.search(fix['model'], stem):
            return fix
    return None


def place_models(container, fixes, stats, where):
    """Set rotation and offset from the catalogue. Absolute, so idempotent."""
    rebuilt, changed = [], False
    for m in container.Models():
        nm = pcbnew.FP_3DMODEL()
        nm.m_Filename = str(m.m_Filename)
        nm.m_Scale, nm.m_Show = m.m_Scale, m.m_Show
        nm.m_Offset, nm.m_Rotation = m.m_Offset, m.m_Rotation
        fix = matching_fix(container, m, fixes)
        if fix:
            want_rot = [float(v) for v in fix.get('rotation', [0, 0, 0])]
            want_off = [float(v) for v in fix.get('offset', [0, 0, 0])]
            have = ([m.m_Rotation.x, m.m_Rotation.y, m.m_Rotation.z],
                    [m.m_Offset.x, m.m_Offset.y, m.m_Offset.z])
            if (any(abs(h - w) > 1e-6 for h, w in zip(have[0], want_rot)) or
                    any(abs(h - w) > 1e-6 for h, w in zip(have[1], want_off))):
                nm.m_Rotation = pcbnew.VECTOR3D(*want_rot)
                nm.m_Offset = pcbnew.VECTOR3D(*want_off)
                changed = True
                stats[f"{fp_name(container)} / {stem_of(m)}"] += 1
        rebuilt.append(nm)
    if changed:
        container.Models().clear()
        for nm in rebuilt:
            container.Models().push_back(nm)
    return changed


def resolve_model(path, near):
    """Absolute path of a model reference, or None if it cannot be found."""
    p = str(path)
    for var in ('KICAD10_3DMODEL_DIR', 'KICAD9_3DMODEL_DIR', 'KICAD8_3DMODEL_DIR',
                'KISYS3DMOD'):
        p = p.replace('${%s}' % var, KICAD_3D)
    p = p.replace('${KIPRJMOD}', near)
    p = os.path.expandvars(os.path.expanduser(p))
    if os.path.exists(p):
        return p
    alt = os.path.splitext(p)[0] + ('.wrl' if p.endswith('.step') else '.step')
    return alt if os.path.exists(alt) else None


_POINT = re.compile(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*"
                    r"([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)")
_WRL_TRIPLE = re.compile(r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s+"
                         r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s+"
                         r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")

_BBOX_CACHE = {}


def model_bbox(path):
    """(dx, dy, zmin, zmax) of a .step or .wrl, in mm, or None."""
    if path in _BBOX_CACHE:
        return _BBOX_CACHE[path]
    try:
        text = open(path, errors='replace').read()
    except OSError:
        return None
    pts = []
    if path.endswith('.wrl'):
        for m in re.finditer(r"point\s*\[(.*?)\]", text, re.S):
            pts += [(float(a) * 2.54, float(b) * 2.54, float(c) * 2.54)
                    for a, b, c in _WRL_TRIPLE.findall(m.group(1))]
    else:
        pts = [(float(a), float(b), float(c)) for a, b, c in _POINT.findall(text)]
    if not pts:
        return None
    xs, ys, zs = (sorted(p[i] for p in pts) for i in range(3))
    box = (xs[-1] - xs[0], ys[-1] - ys[0], zs[0], zs[-1])
    _BBOX_CACHE[path] = box
    return box


def pad_span(container):
    """(dx, dy) of the pad cluster in footprint-local mm, or None."""
    ang = math.radians(-container.GetOrientationDegrees())
    c = container.GetPosition()
    xs, ys = [], []
    for p in container.Pads():
        d = p.GetPosition()
        dx, dy = pcbnew.ToMM(d.x - c.x), pcbnew.ToMM(d.y - c.y)
        lx = dx * math.cos(ang) + dy * math.sin(ang)
        ly = -dx * math.sin(ang) + dy * math.cos(ang)
        w, h = pcbnew.ToMM(p.GetSize().x) / 2, pcbnew.ToMM(p.GetSize().y) / 2
        xs += [lx - w, lx + w]
        ys += [ly - h, ly + h]
    if not xs:
        return None
    return max(xs) - min(xs), max(ys) - min(ys)


def each_library():
    """(library dir, footprint name) for every footprint in every .pretty."""
    for lib in sorted({os.path.dirname(p) for p in
                       glob.glob(f'{HW}/**/*.pretty/*.kicad_mod', recursive=True)}):
        for mod in sorted(glob.glob(f'{lib}/*.kicad_mod')):
            yield lib, os.path.splitext(os.path.basename(mod))[0], mod


def each_board():
    for pcb in sorted(glob.glob(f'{HW}/**/*.kicad_pcb', recursive=True)):
        if any(x in pcb for x in ('.history', 'backup', 'archive', '_tmp')):
            continue
        if os.path.exists(os.path.splitext(pcb)[0] + '.kicad_pro'):
            yield pcb


def run_fixes(fixes, apply):
    """Apply the placement catalogue to every library and every board."""
    libstats, brdstats = collections.Counter(), collections.Counter()
    nlib = nbrd = 0
    print("=== footprint libraries ===")
    for lib, name, mod in each_library():
        fp = pcbnew.FootprintLoad(lib, name)
        if fp is None:
            continue
        if place_models(fp, fixes, libstats, lib):
            nlib += 1
            print(f"  {'FIXED' if apply else 'WOULD FIX'}  {os.path.relpath(mod, HW)}")
            if apply:
                pcbnew.FootprintSave(lib, fp)

    print("\n=== boards ===")
    for pcb in each_board():
        b = pcbnew.LoadBoard(pcb)
        hits = sum(place_models(fp, fixes, brdstats, pcb) for fp in b.GetFootprints())
        if hits:
            nbrd += 1
            print(f"  {'FIXED' if apply else 'WOULD FIX'}  {os.path.relpath(pcb, HW)}"
                  f"  ({hits} footprints)")
            if apply:
                pcbnew.SaveBoard(pcb, b)

    print(f"\n{nlib} library footprint(s), {nbrd} board(s)"
          + ("" if apply else "   [DRY RUN, nothing written]"))
    print("\nper catalogue entry (library / board placements):")
    for k in sorted(set(libstats) | set(brdstats)):
        print(f"  {k[:60]:60s} {libstats[k]:4d} / {brdstats[k]:4d}")


def audit(fixes):
    """Report footprints whose model does not line up with their pads.

    Two failures are decidable from the files, and both were found by eye in
    CAD before this existed: a model whose long axis crosses the pad cluster's
    (the stock model was drawn for a different footprint), and one authored
    entirely below Z=0 (it sinks into the board). Anything already covered by
    the catalogue is not reported.
    """
    seen, rows = set(), []
    for pcb in sorted(glob.glob(f'{HW}/**/*.kicad_pcb', recursive=True)):
        if any(x in pcb for x in ('.history', 'backup', 'archive', '_tmp')):
            continue
        if not os.path.exists(os.path.splitext(pcb)[0] + '.kicad_pro'):
            continue
        board, near = pcbnew.LoadBoard(pcb), os.path.dirname(pcb)
        for fp in board.GetFootprints():
            span = pad_span(fp)
            for m in fp.Models():
                key = (fp_name(fp), stem_of(m))
                if key in seen or matching_fix(fp, m, fixes):
                    continue
                path = resolve_model(m.m_Filename, near)
                box = model_bbox(path) if path else None
                if not box or not span:
                    continue
                seen.add(key)
                dx, dy, zmin, zmax = box
                rot_z = round(m.m_Rotation.z) % 180
                # a 90 degree model rotation swaps which model axis to compare
                mx, my = (dy, dx) if rot_z == 90 else (dx, dy)
                note = []
                if (max(span) > min(span) * 1.15 and max(mx, my) > min(mx, my) * 1.15
                        and (span[0] > span[1]) != (mx > my)):
                    note.append(f"axis crossed: pads {span[0]:.2f}x{span[1]:.2f}, "
                                f"model {mx:.2f}x{my:.2f}")
                if zmax + m.m_Offset.z <= 0.001:
                    note.append(f"sunk: model Z {zmin:.2f}..{zmax:.2f}, "
                                f"offset {m.m_Offset.z:.2f}")
                if note:
                    rows.append((os.path.relpath(pcb, HW), fp.GetReference(),
                                 key[0][:34], key[1][:30], '; '.join(note)))
    for r in rows:
        print(f"  {r[0][:38]:38s} {r[1]:6s} {r[2]:34s} {r[3]:30s} {r[4]}")
    print(f"\n{len(rows)} footprint/model pair(s) to look at, "
          f"{len(seen)} checked, {len(fixes)} already in the catalogue")


def main():
    global HW
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True,
                    help='root containing the boards and footprint libraries to inspect')
    ap.add_argument('--map')
    ap.add_argument('--fixes')
    ap.add_argument('--audit', action='store_true')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    HW = os.path.abspath(os.path.expanduser(a.root))
    if a.audit:
        cat = json.load(open(a.fixes))['fixes'] if a.fixes else []
        return audit(cat)
    if a.fixes:
        return run_fixes(json.load(open(a.fixes))['fixes'], a.apply)
    if not a.map:
        ap.error('one of --map, --fixes or --audit is required')
    mapping = json.load(open(a.map))
    libstats, brdstats = collections.Counter(), collections.Counter()
    nlib = nbrd = 0

    print("=== footprint libraries ===")
    for lib in sorted({os.path.dirname(p) for p in
                       glob.glob(f'{HW}/**/*.pretty/*.kicad_mod', recursive=True)}):
        for mod in sorted(glob.glob(f'{lib}/*.kicad_mod')):
            name = os.path.splitext(os.path.basename(mod))[0]
            fp = pcbnew.FootprintLoad(lib, name)
            if fp is None:
                continue
            if swap_models(fp, mapping, libstats, lib):
                nlib += 1
                print(f"  {'WOULD FIX' if not a.apply else 'FIXED'}  {os.path.relpath(mod, HW)}")
                if a.apply:
                    pcbnew.FootprintSave(lib, fp)

    print("\n=== boards ===")
    for pcb in sorted(glob.glob(f'{HW}/**/*.kicad_pcb', recursive=True)):
        if any(x in pcb for x in ('.history', 'backup', 'archive', '_tmp')):
            continue
        if not os.path.exists(os.path.splitext(pcb)[0] + '.kicad_pro'):
            continue
        b = pcbnew.LoadBoard(pcb)
        hits = sum(swap_models(fp, mapping, brdstats, pcb) for fp in b.GetFootprints())
        if hits:
            nbrd += 1
            print(f"  {'WOULD FIX' if not a.apply else 'FIXED'}  {os.path.relpath(pcb, HW)}  ({hits} footprints)")
            if a.apply:
                pcbnew.SaveBoard(pcb, b)

    print(f"\n{nlib} library footprint(s), {nbrd} board(s)"
          + ("" if a.apply else "   [DRY RUN, nothing written]"))
    print("\nper model (library / board placements):")
    for k in sorted(set(libstats) | set(brdstats)):
        print(f"  {k[:52]:52s} {libstats[k]:4d} / {brdstats[k]:4d}")
    missing = sorted(set(mapping) - set(libstats) - set(brdstats))
    if missing:
        print(f"\n{len(missing)} mapped model(s) matched nothing: {', '.join(missing)}")

main()
