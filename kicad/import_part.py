#!/usr/bin/env python3
"""Import an LCSC part into a board repo's local KiCad libraries.

easyeda2kicad is the only sanctioned importer, but its raw output is not
usable as-is. Every import needs the same six repairs, and doing them by
hand is where the mistakes happen:

  1. It emits KiCad 6-era s-expressions. The board repos are on KiCad 10.
  2. It types every symbol pin "unspecified", so ERC sees nothing.
  3. It writes the symbol's Footprint property as "C1234567:NAME", a
     library nickname that does not exist in any repo.
  4. It points the footprint's 3D model at the absolute path it happened
     to write to, and at the .wrl rather than the .step.
  5. It leaves Description empty on both symbol and footprint.
  6. Its courtyards are frequently smaller than the part body, sometimes
     small enough to exclude the part's own pins.

The trap when fixing 1 is that `kicad-cli sym upgrade` rewrites every
symbol in the library, not just the new one. A board repo's symbol library
is hand-maintained and partly hand-spliced, so a wholesale rewrite is a
large meaningless diff over other people's work. This script upgrades a
throwaway *copy* of the library with the new symbol appended, lifts only
the new block out of that copy, and splices it into the real file, then
asserts the pre-existing bytes are untouched.

Library naming is per repo, not a convention: OpenESC-20x20 uses
components.kicad_sym + 4in1ESC.pretty, the hardware-template uses
lib.kicad_sym + lib.pretty. The nicknames are read from the repo's own
sym-lib-table and fp-lib-table rather than assumed.

Needs easyeda2kicad (pip install easyeda2kicad) and KiCad 10's kicad-cli.
Pure text surgery plus kicad-cli, so system python3 is fine; this one does
not import pcbnew.

Usage:
    python3 import_part.py C30170185 --repo ~/OpenDrone/hardware/OpenESC-20x20/hardware
    python3 import_part.py C30170185 C19268033 --repo <hardware-dir> --ref J \
        --description "MR30 3-pin THT power connector, male, PCB mount" \
        --description "MR30 3-pin THT power connector, female, PCB mount"
    python3 import_part.py C30170185 --repo <hardware-dir> --dry-run
    python3 import_part.py C30170185 --repo <hardware-dir> --footprint-only

KiCad caches library tables at process start. Quit KiCad (Cmd+Q) fully and
reopen before the import shows up, and close the Symbol Editor first: it
holds the whole library in memory and saving from it overwrites the splice.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
COURTYARD_CLEARANCE = 0.25  # KLC value for through-hole connectors

# a pad's own copper, before any clearance
PAD_RE = re.compile(
    r'\(pad "[^"]*" \w+ \w+\s*\n\s*\(at ([-\d.]+) ([-\d.]+)[^)]*\)\s*\n\s*\(size ([-\d.]+) ([-\d.]+)\)'
)
GRAPHIC_RE = re.compile(r"\t\((?:fp_line|fp_arc|fp_circle|fp_rect|fp_poly)\b.*?\n\t\)\n", re.S)
COORD_RE = re.compile(r"\((?:start|end|center|mid|xy) ([-\d.]+) ([-\d.]+)\)")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


# --- the repo's own library naming -----------------------------------------

def read_lib_tables(hardware: Path):
    """Return (fp_nickname, pretty_dir, sym_lib_path, shapes_dir).

    Both nicknames come from the repo's tables. A repo with more than one
    local library is ambiguous and is refused rather than guessed at.
    """
    def local_entries(table: Path):
        if not table.is_file():
            return []
        text = table.read_text()
        out = []
        for name, uri in re.findall(r'\(lib \(name "([^"]*)"\).*?\(uri "([^"]*)"\)', text):
            if "${KIPRJMOD}" in uri:
                out.append((name, uri.replace("${KIPRJMOD}", str(hardware))))
        return out

    fps = local_entries(hardware / "fp-lib-table")
    syms = local_entries(hardware / "sym-lib-table")
    if len(fps) != 1:
        sys.exit(f"expected exactly one project-local footprint library, found {len(fps)}: "
                 f"{[n for n, _ in fps]}")
    if len(syms) != 1:
        sys.exit(f"expected exactly one project-local symbol library, found {len(syms)}: "
                 f"{[n for n, _ in syms]}")

    fp_nick, pretty = fps[0]
    _, sym_lib = syms[0]
    pretty = Path(pretty)
    # the 3D directory is named after the .pretty, which is the convention
    # every board repo already follows
    shapes = pretty.with_suffix(".3dshapes")
    if not shapes.is_dir():
        candidates = sorted(hardware.glob("*.3dshapes"))
        if len(candidates) != 1:
            sys.exit(f"cannot locate the .3dshapes directory beside {pretty.name}")
        shapes = candidates[0]
    return fp_nick, pretty, Path(sym_lib), shapes


# --- footprint repairs ------------------------------------------------------

def graphic_extent(text: str, layer: str):
    xs, ys = [], []
    for m in GRAPHIC_RE.finditer(text):
        if f'(layer "{layer}")' not in m.group(0):
            continue
        for c in COORD_RE.finditer(m.group(0)):
            xs.append(float(c.group(1)))
            ys.append(float(c.group(2)))
    return (min(xs), max(xs), min(ys), max(ys)) if xs else None


def pad_extent(text: str):
    xs, ys = [], []
    for m in PAD_RE.finditer(text):
        x, y, w, h = map(float, m.groups())
        xs += [x - w / 2, x + w / 2]
        ys += [y - h / 2, y + h / 2]
    return (min(xs), max(xs), min(ys), max(ys)) if xs else None


COURTYARD_LINE = """\t(fp_line
\t\t(start {x0} {y0})
\t\t(end {x1} {y1})
\t\t(stroke
\t\t\t(width 0.05)
\t\t\t(type solid)
\t\t)
\t\t(layer "F.CrtYd")
\t\t(uuid "{uuid}")
\t)
"""


def fix_courtyard(text: str, name: str, report):
    """Replace F.CrtYd with a rectangle enclosing silk and pads.

    Left alone when the imported courtyard already encloses both, so a
    footprint EasyEDA got right keeps its original outline.
    """
    silk, pads = graphic_extent(text, "F.SilkS"), pad_extent(text)
    if not pads:
        return text
    lo = lambda i: min(silk[i], pads[i]) if silk else pads[i]
    hi = lambda i: max(silk[i], pads[i]) if silk else pads[i]
    want = (round(lo(0) - COURTYARD_CLEARANCE, 2), round(hi(1) + COURTYARD_CLEARANCE, 2),
            round(lo(2) - COURTYARD_CLEARANCE, 2), round(hi(3) + COURTYARD_CLEARANCE, 2))

    have = graphic_extent(text, "F.CrtYd")
    if have and have[0] <= want[0] + 1e-6 and have[1] >= want[1] - 1e-6 \
            and have[2] <= want[2] + 1e-6 and have[3] >= want[3] - 1e-6:
        return text

    out = text
    for m in reversed(list(GRAPHIC_RE.finditer(text))):
        if '(layer "F.CrtYd")' in m.group(0):
            out = out[: m.start()] + out[m.end():]

    stem = "c0117a2d-%04x-4c11-9f30-6d3a5b7e000" % (abs(hash(name)) & 0xFFFF)
    x0, x1, y0, y1 = want
    corners = [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]
    rect = "".join(
        COURTYARD_LINE.format(x0=a, y0=b, x1=c, y1=d, uuid=f"{stem}{i}")
        for i, (a, b, c, d) in enumerate(corners)
    )
    anchor = out.index('\t(pad "')
    report(f"    courtyard {_fmt(have)} -> {_fmt(want)}")
    return out[:anchor] + rect + out[anchor:]


def _fmt(e):
    return "none" if not e else "x %.2f..%.2f y %.2f..%.2f" % e


def install_footprint(mod: Path, pretty: Path, shapes: Path, models: list[Path],
                      description: str, report) -> str:
    text = mod.read_text()
    name = mod.stem

    if description:
        text = text.replace('(property "Description" ""',
                            f'(property "Description" "{description}"', 1)

    if models:
        copied = []
        for m in models:
            shutil.copy2(m, shapes / m.name)
            copied.append(m.name)
        step = [c for c in copied if c.endswith(".step")]
        target = step[0] if step else copied[0]
        text = re.sub(r'\(model "[^"]*"',
                      f'(model "${{KIPRJMOD}}/{shapes.name}/{target}"', text, count=1)
        report(f"    3D {', '.join(copied)}")
    else:
        report("    3D none available from EasyEDA")

    text = fix_courtyard(text, name, report)
    (pretty / mod.name).write_text(text)
    report(f"    footprint -> {pretty.name}/{mod.name}")
    return name


# --- symbol repairs ---------------------------------------------------------

def symbol_block(lib_text: str, name: str):
    m = re.search(r'^\t\(symbol "%s".*?(?=^\t\(symbol "|\Z)' % re.escape(name),
                  lib_text, re.S | re.M)
    return m.group(0).rstrip("\n") if m else None


def install_symbols(sym_lib, triples, raws, fp_nick, ref, pin_type, report):
    staged = [(n, f, d, raws[n]) for n, f, d in triples]
    original = sym_lib.read_text()
    head = original.rstrip()[:-1].rstrip("\n")
    existing = set(re.findall(r'^[\t ]*\(symbol "([^"]*)"', original, re.M))
    clash = [n for n, _, _, _ in staged if n in existing]
    if clash:
        sys.exit(f"symbol(s) already in {sym_lib.name}: {clash}")

    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "merged.kicad_sym"
        work.write_text(head + "\n" + "\n".join(r for _, _, _, r in staged) + "\n)\n")
        run([KICAD_CLI, "sym", "upgrade", "--force", str(work)])
        upgraded = work.read_text()

    blocks, names = [], []
    for name, fp_name, description, _ in staged:
        b = symbol_block(upgraded, name)
        if b is None:
            sys.exit(f"symbol {name} vanished during upgrade")
        b, n = re.subn(r'\(property "Footprint" "C\d+:[^"]*"',
                       f'(property "Footprint" "{fp_nick}:{fp_name}"', b)
        assert n == 1, f"{name}: footprint property not rewritten"
        b, n = re.subn(r'\(property "Reference" "[^"]*"',
                       f'(property "Reference" "{ref}"', b)
        assert n == 1, f"{name}: reference not rewritten"
        if description:
            b = re.sub(r'\(property "Description" ""',
                       f'(property "Description" "{description}"', b)
        pins = len(re.findall(r"\(pin unspecified line", b))
        b = b.replace("(pin unspecified line", f"(pin {pin_type} line")
        report(f"{name}\n    symbol -> {sym_lib.name}, ref {ref}, "
               f"footprint {fp_nick}:{fp_name}, {pins} pins {pin_type}")
        blocks.append(b)
        names.append(name)

    sym_lib.write_text(head + "\n" + "\n".join(blocks) + "\n)\n")
    if not sym_lib.read_text().startswith(head):
        sys.exit("splice modified pre-existing symbols; check git diff")
    return names


# --- verification -----------------------------------------------------------

def verify(sym_lib: Path, pretty: Path, symbols: list[str], footprints: list[str], report):
    ok = True
    for path in (sym_lib,):
        text = path.read_text()
        bal = text.count("(") - text.count(")")
        report(f"  {path.name}: paren balance {bal}")
        ok &= bal == 0
        for name in symbols:
            m = re.search(r'^[\t ]*\(symbol "%s"' % re.escape(name), text, re.M)
            depth = text[: m.start()].count("(") - text[: m.start()].count(")")
            report(f"    {name}: depth {depth}")
            ok &= depth == 1

    with tempfile.TemporaryDirectory() as td:
        # rendering every symbol, not only the new ones, is the check that
        # the splice did not corrupt the library for anything else
        for name in sorted(set(re.findall(r'^[\t ]*\(symbol "([^"]*)"',
                                          sym_lib.read_text(), re.M))):
            if re.search(r"_\d+_\d+$", name):
                continue
            try:
                run([KICAD_CLI, "sym", "export", "svg", "--symbol", name, "-o", td, str(sym_lib)])
            except subprocess.CalledProcessError:
                report(f"    RENDER FAILED: symbol {name}")
                ok = False
        for name in footprints:
            try:
                run([KICAD_CLI, "fp", "export", "svg", "--footprint", name, "-o", td, str(pretty)])
            except subprocess.CalledProcessError:
                report(f"    RENDER FAILED: footprint {name}")
                ok = False
    return ok


# --- driver -----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lcsc", nargs="+", help="LCSC part numbers, e.g. C30170185")
    ap.add_argument("--repo", required=True, type=Path,
                    help="the board repo's hardware/ directory (holds fp-lib-table)")
    ap.add_argument("--ref", default="J", help="reference prefix for the symbol (default J)")
    ap.add_argument("--pin-type", default="passive",
                    help="electrical type for every pin (default passive)")
    ap.add_argument("--description", action="append", default=[], metavar="TEXT",
                    help="Description property; repeat once per LCSC id, in the same order. "
                         "Omitted parts get an empty Description, which is what easyeda2kicad "
                         "hands over: LCSC's own description is marketing boilerplate "
                         "('Power connector / plug connector') and is not worth carrying.")
    ap.add_argument("--footprint-only", action="store_true", help="skip the symbol")
    ap.add_argument("--dry-run", action="store_true",
                    help="import and repair into a temp dir, change nothing")
    args = ap.parse_args()

    hardware = args.repo.expanduser().resolve()
    if not (hardware / "fp-lib-table").is_file():
        sys.exit(f"{hardware} has no fp-lib-table; point --repo at the hardware/ directory")
    if not Path(KICAD_CLI).exists():
        sys.exit(f"kicad-cli not found at {KICAD_CLI}")

    fp_nick, pretty, sym_lib, shapes = read_lib_tables(hardware)
    print(f"target: {hardware}")
    print(f"  footprints {fp_nick} -> {pretty.name}")
    print(f"  symbols       -> {sym_lib.name}")
    print(f"  3D models     -> {shapes.name}")
    if args.description and len(args.description) != len(args.lcsc):
        sys.exit(f"--description given {len(args.description)} times "
                 f"for {len(args.lcsc)} parts; give one per part, in order")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # pass 1: fetch and repair everything into the temp dir, so that a part
        # that clashes or fails is caught before a single repo file is written
        plan = []
        for index, lcsc in enumerate(args.lcsc):
            print(f"\n{lcsc}")
            try:
                run(["easyeda2kicad", "--full", f"--lcsc_id={lcsc}", "--output", str(td / lcsc)])
            except subprocess.CalledProcessError as e:
                out = (e.stdout or "") + (e.stderr or "")
                print(f"    FAILED: easyeda2kicad rejected {lcsc}")
                if out.strip():
                    print(f"    {out.strip().splitlines()[-1]}")
                continue

            mods = sorted((td / f"{lcsc}.pretty").glob("*.kicad_mod"))
            if not mods:
                print(f"    FAILED: no footprint produced for {lcsc}")
                continue

            # KiCad 6-era output has to become KiCad 10 before it is spliced
            stage = td / f"stage-{lcsc}.pretty"
            stage.mkdir()
            for m in mods:
                shutil.copy2(m, stage / m.name)
            run([KICAD_CLI, "fp", "upgrade", str(stage)])

            models = sorted((td / f"{lcsc}.3dshapes").glob("*")) \
                if (td / f"{lcsc}.3dshapes").is_dir() else []

            sym_file = td / f"{lcsc}.kicad_sym"
            sym_text = sym_file.read_text() if sym_file.is_file() else ""
            sym_name = raw = None
            m = re.search(r'^\s*\(symbol "([^"]*)"', sym_text, re.M)
            if m and not args.footprint_only:
                sym_name = m.group(1)
                raw = re.search(r'^\s*\(symbol ".*(?=\n\)\s*$)', sym_text, re.S | re.M).group(0)
                print(f"    symbol {sym_name}")

            description = args.description[index] if args.description else ""
            for mod in sorted(stage.glob("*.kicad_mod")):
                print(f"    footprint {mod.stem}")
                plan.append(dict(mod=mod, models=models, description=description,
                                 sym_name=sym_name, raw=raw))
                sym_name = raw = None  # a symbol belongs to the first footprint only

        if not plan:
            sys.exit("nothing to import")

        # pass 2: refuse before writing anything if a name is already taken
        clashes = []
        for item in plan:
            if (pretty / item["mod"].name).exists():
                clashes.append(f"footprint {item['mod'].stem} in {pretty.name}")
        existing = set(re.findall(r'^[\t ]*\(symbol "([^"]*)"', sym_lib.read_text(), re.M))
        for item in plan:
            if item["sym_name"] and item["sym_name"] in existing:
                clashes.append(f"symbol {item['sym_name']} in {sym_lib.name}")
        if clashes and not args.dry_run:
            sys.exit("already present, nothing written:\n  " + "\n  ".join(clashes))
        if clashes:
            print("\nwould clash: " + "; ".join(clashes))

        # pass 3: write
        dest_pretty = td / "preview.pretty" if args.dry_run else pretty
        dest_shapes = td / "preview.3dshapes" if args.dry_run else shapes
        dest_pretty.mkdir(exist_ok=True)
        dest_shapes.mkdir(exist_ok=True)

        print()
        footprints, staged = [], []
        for item in plan:
            print(item["mod"].stem)
            fp_name = install_footprint(item["mod"], dest_pretty, dest_shapes,
                                        item["models"], item["description"], print)
            footprints.append(fp_name)
            if item["sym_name"]:
                staged.append((item["sym_name"], fp_name, item["description"], item["raw"]))

        if args.dry_run:
            print("\ndry run: nothing written to the repo")
            return

        if staged:
            print()
            triples = [(n, f, d) for n, f, d, _ in staged]
            raws = {n: r for n, _, _, r in staged}
            names = install_symbols(sym_lib, triples, raws, fp_nick,
                                    args.ref, args.pin_type, print)
        else:
            names = []

    print("\nverify:")
    if verify(sym_lib, pretty, names, footprints, print):
        print("\nok. Quit KiCad fully (Cmd+Q) and reopen before the parts appear.")
    else:
        sys.exit("verification failed; check git diff before doing anything else")


if __name__ == "__main__":
    main()
