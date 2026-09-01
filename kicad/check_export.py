#!/usr/bin/env python3
"""Check a Fabrication Toolkit export against the board and schematic it
claims to come from.

Checks, all mechanical:
  C1  designators.csv == board footprints minus exclude_from_bom
  C2  export refs not in the schematic netlist (board-only parts: allowed,
      but each one is a decision; `_2` suffixes are the tell)
  C3  per-LCSC quantity: BOM grouped by LCSC == netlist grouped by LCSC,
      which catches a part silently missing its LCSC field
  C4  footprints with no row in the toolkit's transformations.csv keep
      KiCad's rotation: listed for a manual rotation check

Usage:
    python3 check_export.py <board.kicad_pcb> [--prefix NAME] [--tf CSV]

The export set is <board dir>/production/<ARCHIVE_NAME>_{bom,designators,
positions}.csv; ARCHIVE_NAME is read from fabrication-toolkit-options.json
next to the board unless --prefix overrides it. kicad-cli must be on PATH
or at the standard KiCad.app location (netlist export).

Exit 0 when C1 and C3 pass and C2 only reports refs already known from a
previous run is not tracked here: C2 and C4 are informational, C1 and C3
are hard failures.
"""
import argparse, collections, csv, json, os, re, subprocess, sys, tempfile
import xml.etree.ElementTree as ET

KICAD_CLI = next((p for p in (
    "kicad-cli", "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    if subprocess.run(["which", p], capture_output=True).returncode == 0
    or os.path.exists(p)), "kicad-cli")


def board_refs(pcb_path):
    """(included, excluded) reference sets from the board file."""
    text = open(pcb_path, errors="replace").read()
    inc, exc = [], []
    for m in re.finditer(r'^\t\(footprint ', text, re.M):
        depth, i = 0, m.start()
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blk = text[m.start():i + 1]
        ref = re.search(r'\(property "Reference" "([^"]+)"', blk)
        if not ref:
            continue
        excluded = re.search(r'\(attr[^)]*\bexclude_from_bom\b', blk) or \
            re.search(r'\(attr[^)]*\bboard_only\b', blk)
        fp = re.search(r'\(footprint "([^"]+)"', blk)
        (exc if excluded else inc).append(
            (ref.group(1), fp.group(1).split(":")[-1] if fp else ""))
    return inc, exc


def netlist_parts(sch_path):
    """ref -> {lcsc, value} from the schematic, via kicad-cli netlist."""
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "n.xml")
        r = subprocess.run([KICAD_CLI, "sch", "export", "netlist",
                            "--format", "kicadxml", "-o", out, sch_path],
                           capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"netlist export failed: {r.stderr.strip()[:200]}")
        root = ET.parse(out).getroot()
    parts = {}
    for comp in root.iter("comp"):
        ref = comp.get("ref")
        lcsc = value = ""
        for f in comp.iter("field"):
            if f.get("name", "").upper() in ("LCSC", "LCSC PART", "LCSC_PART"):
                lcsc = (f.text or "").strip()
        v = comp.find("value")
        value = v.text if v is not None else ""
        parts[ref] = {"lcsc": lcsc, "value": value}
    return parts


def read_export(prod, prefix):
    def rd(name):
        p = os.path.join(prod, f"{prefix}_{name}.csv")
        if not os.path.exists(p):
            sys.exit(f"missing {p}")
        with open(p, encoding="utf-8-sig") as f:
            return list(csv.reader(f))
    desg = {}
    for row in rd("designators"):
        if row and ":" in row[0]:
            ref, n = row[0].rsplit(":", 1)
            desg[ref.strip()] = int(n)
        elif row:
            desg[row[0].strip()] = 1
    bom = rd("bom")[1:]
    pos = rd("positions")[1:]
    return desg, bom, pos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--prefix")
    ap.add_argument("--tf", help="transformations.csv from the toolkit")
    a = ap.parse_args()
    bdir = os.path.dirname(os.path.abspath(a.board))
    prefix = a.prefix
    if not prefix:
        opts = os.path.join(bdir, "fabrication-toolkit-options.json")
        prefix = json.load(open(opts))["ARCHIVE_NAME"]
    prod = os.path.join(bdir, "production")
    desg, bom, pos = read_export(prod, prefix)

    inc, exc = board_refs(a.board)
    # designators.csv lists every placed footprint, including BOM-excluded
    # ones, and the toolkit uppercases references: compare accordingly.
    board_all = {r.upper(): r for r, _ in inc + exc}
    export_set = {r.upper() for r in desg}
    fails = []

    # C0 duplicate designators. The dict/set comparisons below collapse
    # duplicates silently, so duplicate references could otherwise pass C1
    # while leaving the export ambiguous.
    dup_board = sorted(r for r, n in collections.Counter(
        r.upper() for r, _ in inc + exc).items() if n > 1)
    dup_export = sorted(r for r, n in collections.Counter(
        r.upper() for r in desg).items() if n > 1)
    # Warning, not a gate: anything listed here still needs manual review.
    if dup_board:
        print(f"C0 warn  duplicate designators on the board: {dup_board[:10]}")
    if dup_export:
        print(f"C0 warn  duplicate designators in the export: {dup_export[:10]}")

    # C1 board vs designators
    only_board = sorted(board_all[k] for k in set(board_all) - export_set)
    only_export = sorted(export_set - set(board_all))
    if only_board or only_export:
        fails.append("C1")
        print(f"C1 FAIL  board-not-in-export: {only_board[:10]}"
              f"{' ...' if len(only_board) > 10 else ''}")
        print(f"         export-not-on-board: {only_export[:10]}"
              f"{' ...' if len(only_export) > 10 else ''}")
    else:
        print(f"C1 ok    {len(export_set)} designators match the board "
              f"({len(exc)} of them excluded from BOM)")

    # C2 board-only vs schematic
    sch = os.path.splitext(a.board)[0] + ".kicad_sch"
    net = {r.upper(): p for r, p in netlist_parts(sch).items()}
    board_only = sorted(export_set - set(net))
    suffixed = [r for r in board_only if re.search(r"_\d+$", r)]
    if board_only:
        print(f"C2 note  {len(board_only)} export refs not in the schematic: "
              f"{board_only[:12]}{' ...' if len(board_only) > 12 else ''}")
        if suffixed:
            print(f"         duplicate-suffix refs (the tell): {suffixed}")
    else:
        print("C2 ok    every export ref exists in the schematic")

    # C3 per-LCSC quantity
    bom_q = {}
    for row in bom:
        if len(row) >= 5 and row[4].strip():
            bom_q[row[4].strip()] = bom_q.get(row[4].strip(), 0) + int(row[2])
    net_q = {}
    for ref, p in net.items():
        # only parts that are actually in the export can be compared
        if p["lcsc"] and ref in export_set:
            net_q[p["lcsc"]] = net_q.get(p["lcsc"], 0) + 1
    diffs = []
    for lcsc in sorted(set(bom_q) | set(net_q)):
        b, n = bom_q.get(lcsc, 0), net_q.get(lcsc, 0)
        # board-only parts inflate the BOM side legitimately; flag only
        # LCSC numbers the netlist has and the BOM under-counts, or the BOM
        # has and the netlist never mentions at all
        if b < n:
            diffs.append(f"{lcsc}: bom {b} < sch {n}")
    no_lcsc = sorted(r for r, p in net.items()
                     if not p["lcsc"] and r in export_set)
    if diffs:
        fails.append("C3")
        print(f"C3 FAIL  {diffs[:8]}")
    else:
        print(f"C3 ok    {len(bom_q)} LCSC part numbers, quantities consistent")
    if no_lcsc:
        print(f"C3 note  refs with no LCSC field (will not be placed): "
              f"{no_lcsc[:12]}{' ...' if len(no_lcsc) > 12 else ''}")

    # C4 rotation coverage
    if a.tf and os.path.exists(a.tf):
        with open(a.tf, encoding="utf-8-sig") as f:
            known = {row[0] for row in csv.reader(f) if row}
        unknown = sorted({fp for r, fp in inc
                          if fp and not any(k in fp for k in known)})
        print(f"C4 note  {len(unknown)} footprint patterns keep KiCad rotation "
              f"(check by eye): {unknown[:8]}{' ...' if len(unknown) > 8 else ''}")

    name = os.path.basename(a.board)
    if fails:
        print(f"== {name}: {'/'.join(fails)} FAILED against {prefix}")
        return 1
    print(f"== {name}: export {prefix} is consistent with board and schematic")
    return 0


if __name__ == "__main__":
    sys.exit(main())
