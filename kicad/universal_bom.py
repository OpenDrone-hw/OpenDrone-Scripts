#!/usr/bin/env python3
"""Fab-agnostic BOM export: Designator,Value,Footprint,Quantity,LCSC,Manufacturer,MPN.

Placements come from the board, exactly like the Fabrication Toolkit BOM, so
parts that exist only in the layout (bulk cap banks) are counted. Manufacturer
and MPN gaps are joined by LCSC number from the other footprints and from any
.kicad_sch beside the board. Chinese fabs read the LCSC column, everyone else
reads Manufacturer+MPN; extra columns are ignored, never rejected.

Run with KiCad's bundled python:

    $KPY universal_bom.py <board.kicad_pcb> [--name ARCHIVE_NAME] [--exclude-dnp]

Writes <board dir>/production/<ARCHIVE_NAME>_bom_universal.csv. ARCHIVE_NAME
defaults to the one in fabrication-toolkit-options.json.
"""
import argparse, csv, glob, json, os, re, sys


def sch_join_maps(bdir):
    """Read-only schematic scan. Returns two join maps:
    by LCSC number -> (Manufacturer, MPN), and by reference -> (LCSC, Manufacturer, MPN).
    The reference map covers boards whose footprints were never field-synced
    from the schematic, so the PCB side carries no LCSC to join on."""
    by_lcsc, by_ref = {}, {}
    prop = r'\(property "%s"\s+"([^"]*)"'
    for f in glob.glob(os.path.join(bdir, "*.kicad_sch")):
        txt = open(f, encoding="utf-8").read()
        # split on symbol instances; lib_symbols definitions have no instance uuid/at
        for blk in re.split(r'\(symbol\s*\n?\s*\(lib_id', txt)[1:]:
            lcsc = re.search(prop % "LCSC", blk)
            lcsc = lcsc.group(1) if lcsc else ""
            if not lcsc:
                continue
            mfr = re.search(prop % "Manufacturer", blk)
            mpn = re.search(prop % "MPN", blk)
            mfr = mfr.group(1) if mfr else ""
            mpn = mpn.group(1) if mpn else ""
            cur = by_lcsc.get(lcsc, ("", ""))
            by_lcsc[lcsc] = (cur[0] or mfr, cur[1] or mpn)
            ref = re.search(prop % "Reference", blk)
            if ref and not ref.group(1).startswith("#"):
                by_ref.setdefault(ref.group(1), (lcsc, mfr, mpn))
    return by_lcsc, by_ref


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--name")
    ap.add_argument("--exclude-dnp", action="store_true")
    a = ap.parse_args()
    board_path = os.path.abspath(a.board)
    bdir = os.path.dirname(board_path)

    opts = os.path.join(bdir, "fabrication-toolkit-options.json")
    name = a.name or (json.load(open(opts)).get("ARCHIVE_NAME") if os.path.exists(opts) else None) \
        or os.path.splitext(os.path.basename(board_path))[0]

    import pcbnew
    board = pcbnew.LoadBoard(board_path)

    by_lcsc, by_ref = sch_join_maps(bdir)
    rows = []
    for fp in board.GetFootprints():
        if fp.IsExcludedFromBOM():
            continue
        if a.exclude_dnp and fp.IsDNP():
            continue
        fields = {k: v for k, v in fp.GetFieldsText().items()}
        rows.append({
            "ref": fp.GetReference(),
            "value": fp.GetValue(),
            "fp": str(fp.GetFPID().GetLibItemName()),
            "lcsc": fields.get("LCSC", ""),
            "mfr": fields.get("Manufacturer", ""),
            "mpn": fields.get("MPN", ""),
        })

    # fill gaps: same-LCSC footprints, then schematic by reference, then by LCSC
    for r in rows:
        if not r["lcsc"] and r["ref"] in by_ref:
            r["lcsc"] = by_ref[r["ref"]][0]
        if r["lcsc"] and (not r["mfr"] or not r["mpn"]):
            for src in rows:
                if src["lcsc"] == r["lcsc"]:
                    r["mfr"] = r["mfr"] or src["mfr"]
                    r["mpn"] = r["mpn"] or src["mpn"]
            sr = by_ref.get(r["ref"])
            if sr and sr[0] == r["lcsc"]:
                r["mfr"] = r["mfr"] or sr[1]
                r["mpn"] = r["mpn"] or sr[2]
            sm = by_lcsc.get(r["lcsc"])
            if sm:
                r["mfr"] = r["mfr"] or sm[0]
                r["mpn"] = r["mpn"] or sm[1]

    groups = {}
    for r in rows:
        key = r["lcsc"] or (r["value"], r["fp"])
        groups.setdefault(key, []).append(r)

    def refkey(ref):
        m = re.match(r"([A-Za-z]+)(\d+)", ref)
        return (m.group(1), int(m.group(2))) if m else (ref, 0)

    out = os.path.join(bdir, "production", f"{name}_bom_universal.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    incomplete = []
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Designator", "Value", "Footprint", "Quantity", "LCSC", "Manufacturer", "MPN"])
        for key, grp in sorted(groups.items(), key=lambda kv: refkey(kv[1][0]["ref"])):
            grp.sort(key=lambda r: refkey(r["ref"]))
            g0 = grp[0]
            w.writerow([",".join(r["ref"] for r in grp), g0["value"], g0["fp"],
                        len(grp), g0["lcsc"], g0["mfr"], g0["mpn"]])
            if not (g0["lcsc"] and g0["mfr"] and g0["mpn"]):
                incomplete.append(g0)

    print(f"{out}: {len(groups)} lines, {len(rows)} placements")
    for g in incomplete:
        print(f"  INCOMPLETE {g['ref']} {g['value']}: LCSC={g['lcsc'] or '-'} "
              f"Manufacturer={g['mfr'] or '-'} MPN={g['mpn'] or '-'}", file=sys.stderr)
    if incomplete:
        print(f"  {len(incomplete)} BOM lines missing part data", file=sys.stderr)


if __name__ == "__main__":
    main()
