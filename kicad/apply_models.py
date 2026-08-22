#!/usr/bin/env python3
"""apply_models.py - retarget 3D model references, in the LIBRARY and on boards.

Run with KiCad's bundled Python.

Fixing only the .kicad_pcb is surface level: the next board to place that
footprint pulls the bloated model again. Fixing only the .pretty is invisible:
boards carry their own copy of each footprint, so nothing changes until they are
refreshed. This does both, and touches ONLY the 3D model reference: pads, nets,
placement, values and the land pattern are never read or written.

Offset and rotation are reset on a swap. KiCad stock models are authored
origin-at-footprint-origin with the board face at Z=0; the easyeda imports carry
a compensating offset that does not transfer, which is what made an earlier
hand-swap drop the USB-C clean out of the render.

  apply_models.py --map map.json [--apply]     (default is a dry run)

map.json: {"<old model basename, no extension>": "<new ${KICAD10_3DMODEL_DIR}/... path>"}
"""
import json, os, sys, glob, argparse, collections
import pcbnew

HW = os.path.expanduser('~/OpenDrone/hardware')

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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--map', required=True)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
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
