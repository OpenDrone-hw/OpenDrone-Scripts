#!/usr/bin/env python3
"""
export_step.py — standardized STEP exports for the OpenDrone boards.

kicad-cli's default STEP export writes only the board body and the component 3D
models: no copper, no pads, no silkscreen. Imported into Fusion that is a bare
green slab with parts on it. This script drives kicad-cli with a fixed flag set
so every OpenDrone STEP looks the same and lands in one predictable place, the
same in every repo:

    <repo root>/export/<ProductName>.step

Repo root, not next to the .kicad_pcb — board files live at different depths per
repo (OpenRX/OpenRX-Lite/, OpenFC-Lite/hardware/), so anchoring to the board
would scatter the output. One directory per repo, always at the top.

THE STANDARD EXPORT IS: board body + components + pads + silkscreen.
Three deliberate decisions, each measured rather than assumed:

  Soldermask is EXCLUDED. kicad-cli gives the mask a 17% transparency factor and
  silkscreen a 10% one, which is what made earlier exports look like frosted
  glass with the components showing through. The mask solid also spans
  z 0.91-0.96 mm on a 1.0 mm board while the pads top out at 0.95 mm, so it sat
  *over* the gold pads and greyed them out. The board body is already opaque
  green, so dropping the mask gives one solid PCB with visible gold pads.

  Tracks and zones are EXCLUDED. Copper spans z 0.91-0.945 mm, entirely inside
  where the mask would be, so it is invisible from outside on a real board.
  Including it roughly doubles the file for geometry nobody can see.

  Copper outside the board outline is REMOVED (--clip, on by default). Edge
  pads are drawn past Edge.Cuts on purpose so the fab plates and routes through
  them, but on the finished board the router cuts that copper away. kicad-cli
  exports the full uncut pad, leaving tabs hanging in space. This clips partly
  outside pads to the outline and deletes fully outside ones, on a TEMP COPY of
  the board. The source .kicad_pcb is never written.

Any leftover transparency is zeroed in the written STEP so nothing renders
see-through.

--preset exists only as an escape hatch for copper inspection work, and its
output does not belong in a repo:
  full   standard + tracks + zones (outer layers)
  inner  full + inner copper layers
  body   board body + components only (kicad-cli's bare default)

Clipping needs pcbnew, so run with KiCad's bundled Python, same as
render_board.py. Without it the export still runs, unclipped, with a warning:

  KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
  $KPY software/tools/export_step.py --all

KiCad may stay open: every board edit happens on a temp copy.

Usage:
  export_step.py --all                       # every board discovered under hardware/
  export_step.py --all --repo OpenRX         # one repo
  export_step.py <board.kicad_pcb> -o out.step
  export_step.py --all --dry-run
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time

DEFAULT_KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
ROOT = os.path.expanduser("~/OpenDrone/hardware")

# --subst-models pulls the STEP model where a footprint ships both STEP and
# VRML; --no-dnp keeps unpopulated parts out so the model matches a shipped board.
COMMON = ["--subst-models", "--no-dnp"]

STANDARD = ["--include-pads", "--include-silkscreen"]

PRESETS = {
    "standard": STANDARD,
    "body": [],
    "full": STANDARD + ["--include-soldermask", "--include-tracks", "--include-zones"],
    "inner": STANDARD + ["--include-soldermask", "--include-tracks", "--include-zones",
                         "--include-inner-copper"],
}

MIN_OUTSIDE_MM2 = 0.001  # ignore rounding-level slivers of copper past the edge

# Boards are DISCOVERED, never listed. A new repo or a new variant is picked up
# with no edit to this file. Two rules do the whole job:
#
#   1. A board counts if a .kicad_pro of the same stem sits next to it. That is
#      what makes it a real KiCad project rather than a stray or a backup, and
#      it is also required for export: ${KIPRJMOD} only resolves when the
#      project file is there, and without it every project-relative 3D model
#      silently vanishes from the output.
#   2. These directories are skipped wherever they appear.
SKIP_DIRS = {".history", ".git", "backups", "archive", "libs", ".pio", "__pycache__",
             "node_modules", ".venv", "export"}


def product_name(repo, stem):
    """Name the STEP after the product, derived from repo + board stem.

    Board stems are not unique on their own (OpenFC-Lite and OpenFC-Lite-Mini
    both use OpenFC.kicad_pcb) and are sometimes internal (4in1). Where repo and
    stem overlap, the longer one is the real product name; where they do not,
    both are needed to stay unambiguous.
    """
    r, s = repo.lower(), stem.lower()
    if s in r or r in s:
        return repo if len(repo) >= len(stem) else stem
    return f"{repo}-{stem}"


def discover(root, only_repo=None):
    """Yield (repo, board_path, product_name) for every project board found."""
    found = []
    for repo in sorted(os.listdir(root)):
        repo_dir = os.path.join(root, repo)
        if not os.path.isdir(repo_dir) or repo.startswith("."):
            continue
        if only_repo and repo != only_repo:
            continue
        for dirpath, dirnames, filenames in os.walk(repo_dir):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".pretty")]
            for fn in sorted(filenames):
                if not fn.endswith(".kicad_pcb"):
                    continue
                stem = fn[: -len(".kicad_pcb")]
                if not os.path.exists(os.path.join(dirpath, stem + ".kicad_pro")):
                    continue
                found.append((repo, os.path.join(dirpath, fn), product_name(repo, stem)))
    return found


def find_kicad_cli(explicit):
    for c in (explicit, DEFAULT_KICAD_CLI, shutil.which("kicad-cli")):
        if c and os.path.exists(c):
            return c
    sys.exit("kicad-cli not found — pass --kicad-cli PATH")


def human(n):
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} kB"


def clip_board_to_outline(board_path, scratch):
    """Copy the board and remove copper that falls outside Edge.Cuts.

    Returns (path_to_use, clipped_count, deleted_count). The copy is what gets
    exported; board_path itself is only ever read.

    The copy has to live in the SOURCE directory, not a temp dir: footprints
    reference 3D models through ${KIPRJMOD}, which resolves to the project
    directory, and KiCad only sets it when a .kicad_pro of the same stem sits
    next to the board. Exporting from /tmp silently drops every project-relative
    model. Both temp files are registered with the caller for cleanup.
    """
    try:
        import pcbnew
    except ImportError:
        return board_path, None, None

    stem = os.path.splitext(os.path.basename(board_path))[0]
    srcdir = os.path.dirname(os.path.abspath(board_path))
    tmp_stem = os.path.join(srcdir, f".export_step_tmp_{stem}")
    tmp = tmp_stem + ".kicad_pcb"
    # Register the STEM, not the files: saving a board also makes a .kicad_prl
    # (and sometimes a .kicad_pro) that we never asked for. Cleanup globs.
    scratch.append(tmp_stem)

    src_pro = os.path.join(srcdir, stem + ".kicad_pro")
    if os.path.exists(src_pro):
        shutil.copy(src_pro, tmp_stem + ".kicad_pro")

    shutil.copy(board_path, tmp)
    board = pcbnew.LoadBoard(tmp)

    outline = pcbnew.SHAPE_POLY_SET()
    if not board.GetBoardPolygonOutlines(outline, False) or outline.OutlineCount() == 0:
        return board_path, None, None

    L = pcbnew.UNDEFINED_LAYER  # padstack "all layers"
    clipped = deleted = 0
    for fp in board.GetFootprints():
        for pad in list(fp.Pads()):
            effective = pad.GetEffectivePolygon(L, pcbnew.ERROR_INSIDE)

            outside = pcbnew.SHAPE_POLY_SET(effective)
            outside.BooleanSubtract(outline)
            area = sum(abs(outside.Outline(i).Area())
                       for i in range(outside.OutlineCount())) / 1e12
            if area <= MIN_OUTSIDE_MM2:
                continue

            keep = pcbnew.SHAPE_POLY_SET(effective)
            keep.BooleanIntersection(outline)
            if keep.OutlineCount() == 0:
                # Wholly past the edge: the router removes it, so should we.
                fp.Delete(pad)
                deleted += 1
                continue

            pos = pad.GetPosition()
            keep.Move(pcbnew.VECTOR2I(-pos.x, -pos.y))
            pad.SetShape(L, pcbnew.PAD_SHAPE_CUSTOM)
            pad.SetAnchorPadShape(L, pcbnew.PAD_SHAPE_CIRCLE)
            pad.SetSize(L, pcbnew.VECTOR2I(10000, 10000))
            pad.DeletePrimitivesList(L)
            pad.AddPrimitivePoly(L, keep, 0, True)
            clipped += 1

    board.Save(tmp)
    return tmp, clipped, deleted


def deopacify(step_path):
    """Zero every transparency factor so nothing renders see-through."""
    with open(step_path, encoding="utf8", errors="surrogateescape") as fh:
        text = fh.read()
    text, n = re.subn(r"SURFACE_STYLE_TRANSPARENT\([^)]*\)",
                      "SURFACE_STYLE_TRANSPARENT(0.)", text)
    if n:
        with open(step_path, "w", encoding="utf8", errors="surrogateescape") as fh:
            fh.write(text)
    return n


def export(cli, board, out, preset, extra, dry_run, clip):
    cmd = [cli, "pcb", "export", "step", "-f", "-o", out]
    cmd += COMMON + PRESETS[preset] + extra + [board]
    if dry_run:
        print(" ".join(cmd))
        return True

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    t0 = time.time()
    scratch = []
    note = ""
    try:
        if clip:
            src, nclip, ndel = clip_board_to_outline(board, scratch)
            if nclip is None:
                note = "  (clip skipped: pcbnew unavailable — run with KiCad's Python)"
            elif nclip or ndel:
                note = f"  (clipped {nclip} pad(s), removed {ndel} fully outside the outline)"
            cmd[-1] = src
        result = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        for stem_path in scratch:
            for f in glob.glob(glob.escape(stem_path) + ".*"):
                os.remove(f)

    dt = time.time() - t0
    if result.returncode != 0 or not os.path.exists(out):
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        print(f"  FAIL rc={result.returncode}  {tail}")
        return False

    deopacify(out)
    print(f"  {human(os.path.getsize(out)):>9}  {dt:5.1f}s  {out}{note}")

    # kicad-cli reports an unresolvable 3D model as a warning and still exits 0,
    # so a board with a broken model path exports "successfully" as a bare slab.
    # Surface it: the fix belongs in the footprint library, not here.
    missing = sorted({ln.split("File not found: ", 1)[1].strip()
                      for ln in (result.stderr + result.stdout).splitlines()
                      if "File not found: " in ln})
    if missing:
        print(f"  WARNING: {len(missing)} unresolved 3D model path(s) — components are missing:")
        for m in missing[:10]:
            print(f"    {m}")
        if len(missing) > 10:
            print(f"    ... and {len(missing) - 10} more")
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("board", nargs="?", help="path to a .kicad_pcb")
    p.add_argument("-o", "--output", help="output .step (single-board mode)")
    p.add_argument("--all", action="store_true", help="export every board in the manifest")
    p.add_argument("--repo", help="with --all, limit to one repo")
    p.add_argument("--preset", default="standard", choices=sorted(PRESETS),
                   help="escape hatch only; the repo standard is 'standard'")
    p.add_argument("--no-clip", dest="clip", action="store_false",
                   help="keep copper that hangs past Edge.Cuts")
    p.add_argument("--root", default=ROOT, help=f"hardware dir (default {ROOT})")
    p.add_argument("--kicad-cli", help="path to kicad-cli")
    p.add_argument("--dry-run", action="store_true", help="print commands only")
    p.add_argument("extra", nargs="*", help="extra kicad-cli flags")
    a = p.parse_args()

    cli = find_kicad_cli(a.kicad_cli)

    if a.all:
        jobs = [(board, os.path.join(a.root, repo, "export", name + ".step"))
                for repo, board, name in discover(a.root, a.repo)]
        if not jobs:
            sys.exit("no boards discovered")
    elif a.board:
        out = a.output or os.path.join(os.path.dirname(os.path.abspath(a.board)), "export",
                                       os.path.splitext(os.path.basename(a.board))[0] + ".step")
        jobs = [(a.board, out)]
    else:
        p.error("pass a board or --all")

    print(f"preset: {a.preset}   {len(jobs)} board(s)")
    ok = 0
    for board, out in jobs:
        print(os.path.relpath(board, a.root) if a.all else board)
        ok += export(cli, board, out, a.preset, a.extra, a.dry_run, a.clip)
    print(f"{ok}/{len(jobs)} exported")
    return 0 if ok == len(jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
