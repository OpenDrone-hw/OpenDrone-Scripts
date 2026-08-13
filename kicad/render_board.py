#!/usr/bin/env python3
"""
render_board.py — standardized KiCad PCB → PNG renders for the OpenDrone projects.

Produces the clean board renders used in every OpenDrone README: vias and solder
paste stripped (so copper pads show as gold, not gray paste deposits), no floor
shadow, transparent background, centered square output.

The source .kicad_pcb is NEVER opened for writing. Vias and paste are stripped on
a temp COPY written beside the source, and the copy is deleted afterwards. The
source is md5-checked before and after every run.

An earlier version mutated the source and restored it from a backup in a finally
block. That is not a guarantee: SIGKILL, a crash or a power cut skips finally and
leaves the board on disk with every via and paste aperture removed, with the only
surviving copy in a randomly named $TMPDIR file that carries the source's mtime.
The measured exposure was 2-9 seconds per board. Rendering from a copy is
pixel-identical, so nothing was gained by the risk.

MUST be run with KiCad's bundled Python (it imports pcbnew):

  /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 \
      software/OpenDrone-Scripts/kicad/render_board.py hardware/4in1.kicad_pcb --top images/front.png --bottom images/back.png

KiCad may stay OPEN: the source board is never written.

Examples:
  # explicit output names (what the OpenDrone READMEs use)
  render_board.py hardware/4in1.kicad_pcb --top images/front.png --bottom images/back.png

  # default names: <outdir>/<stem>-top.png and <stem>-bottom.png
  render_board.py hardware/OpenFC.kicad_pcb --outdir images/

  # keep paste/vias, only one side, custom size
  render_board.py board.kicad_pcb --sides top --keep-paste --keep-vias --size 2000

Note: kicad-cli subtracts 24px from the requested render size, so --size must
stay at or below --render-size minus 24 or the result is upscaled.

Dependencies: KiCad 9/10 (pcbnew + kicad-cli) and ImageMagick (`magick`).
"""
import argparse
import glob
import hashlib
import os
import shutil
import subprocess
import sys

DEFAULT_KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"


def find_kicad_cli(explicit):
    if explicit:
        return explicit
    # sibling of the bundled python on macOS: .../Contents/Frameworks/.../python3
    here = sys.executable
    if "KiCad.app" in here:
        base = here.split("KiCad.app")[0] + "KiCad.app/Contents/MacOS/kicad-cli"
        if os.path.exists(base):
            return base
    for c in (DEFAULT_KICAD_CLI, shutil.which("kicad-cli")):
        if c and os.path.exists(c):
            return c
    sys.exit("kicad-cli not found — pass --kicad-cli PATH")


def find_magick():
    for c in (shutil.which("magick"), "/opt/homebrew/bin/magick", "/usr/local/bin/magick"):
        if c and os.path.exists(c):
            return c
    return None


TEMP_PREFIX = ".render_board_tmp_"


def stripped_copy(pcb, strip_vias, strip_paste, scratch):
    """Write a via/paste-stripped COPY beside the source. Returns (path, nv, npad).

    The source .kicad_pcb is never opened for writing. An earlier version mutated
    it in place and restored from a backup in a finally block, which is not a
    guarantee: SIGKILL, a crash or a power cut skips finally and leaves the board
    on disk with every via and paste aperture gone, with the only copy in a
    randomly named file under $TMPDIR. Measured window was 2-9 seconds.

    The copy must sit BESIDE the source, not in /tmp: footprints reference 3D
    models through ${KIPRJMOD}, which only resolves when a .kicad_pro of the same
    stem is next to the board. Rendering from /tmp silently drops those models.
    """
    import pcbnew
    stem = os.path.splitext(os.path.basename(pcb))[0]
    srcdir = os.path.dirname(os.path.abspath(pcb))
    tmp_stem = os.path.join(srcdir, f"{TEMP_PREFIX}{os.getpid()}_{stem}")
    scratch.append(tmp_stem)
    tmp = tmp_stem + ".kicad_pcb"

    src_pro = os.path.join(srcdir, stem + ".kicad_pro")
    if os.path.exists(src_pro):
        shutil.copy2(src_pro, tmp_stem + ".kicad_pro")
    shutil.copy2(pcb, tmp)

    if not (strip_vias or strip_paste):
        return tmp, 0, 0

    board = pcbnew.LoadBoard(tmp)
    nv = 0
    if strip_vias:
        for it in list(board.GetTracks()):
            if isinstance(it, pcbnew.PCB_VIA):
                board.RemoveNative(it)
                nv += 1
    npad = 0
    if strip_paste:
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                ls = pad.GetLayerSet()
                ls.RemoveLayer(pcbnew.F_Paste)
                ls.RemoveLayer(pcbnew.B_Paste)
                pad.SetLayerSet(ls)
                npad += 1
    # SaveBoard returns False on failure instead of raising. Ignoring it renders
    # the UNSTRIPPED board while printing a success line.
    if not pcbnew.SaveBoard(tmp, board):
        sys.exit(f"pcbnew could not write {tmp}")
    return tmp, nv, npad


def render_side(cli, pcb, side, out, render_px):
    # kicad-cli exits 0 on several real failures (bad --side value, bad_any_cast)
    # and writes no file, so check=True catches nothing. Verify the output exists
    # and surface kicad-cli's stderr, which capture_output would otherwise eat.
    r = subprocess.run(
        [cli, "pcb", "render", "--side", side, "-w", str(render_px), "-h", str(render_px),
         "--quality", "basic", "--background", "transparent", "-o", out, pcb],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not os.path.exists(out):
        detail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        sys.exit(f"kicad-cli render failed for side={side} (rc={r.returncode}): {detail}")


def square(magick, out, size):
    # Not a warning: an untrimmed render is the same pixel size as a trimmed one
    # (kicad-cli subtracts 24px from the requested dimensions), so the wrong file
    # passes any dimension check and ships looking correct.
    if not magick:
        sys.exit("ImageMagick 'magick' not found — install it or pass --magick PATH")
    # -fuzz 1% so the alpha trim treats anti-aliased edge fringe (semi-transparent
    # halo around the board on the transparent background) as background and crops
    # it deterministically — without it, a stray near-transparent fringe pixel can
    # shift the trim by a pixel or two and jitter the registration between runs.
    subprocess.run(
        [magick, out, "-fuzz", "1%", "-trim", "+repage", "-background", "none",
         "-gravity", "center",
         "-resize", f"{size}x{size}", "-extent", f"{size}x{size}",
         # Without this ImageMagick stamps tIME and three date: text chunks, so
         # every re-render is a 23-byte binary diff even when the board and the
         # pixels are identical. That dirties git on all 16 committed PNGs.
         "-define", "png:exclude-chunks=date,time", out],
        check=True,
    )


def main():
    ap = argparse.ArgumentParser(description="Standardized OpenDrone KiCad board renders (via/paste-free, no shadow).")
    ap.add_argument("pcb", help="path to the .kicad_pcb")
    ap.add_argument("--top", help="output path for the top render")
    ap.add_argument("--bottom", help="output path for the bottom render")
    ap.add_argument("--outdir", help="dir for default-named outputs (<stem>-top.png / -bottom.png)")
    ap.add_argument("--sides", default="top,bottom", help="comma list: top,bottom (default both)")
    ap.add_argument("--size", type=int, default=1568, help="final square size in px (default 1568)")
    ap.add_argument("--render-size", type=int, default=1600, help="raw render size before trim (default 1600)")
    ap.add_argument("--keep-vias", action="store_true", help="do not strip vias")
    ap.add_argument("--keep-paste", action="store_true", help="do not strip solder paste")
    ap.add_argument("--kicad-cli", help="path to kicad-cli (auto-detected by default)")
    ap.add_argument("--magick", help="path to ImageMagick magick (auto-detected by default)")
    args = ap.parse_args()

    pcb = os.path.abspath(args.pcb)
    if not pcb.endswith(".kicad_pcb") or not os.path.exists(pcb):
        sys.exit(f"not a .kicad_pcb: {pcb}")

    strip_vias = not args.keep_vias
    strip_paste = not args.keep_paste

    if args.size < 1 or args.render_size < 1:
        sys.exit("--size and --render-size must be positive")
    # kicad-cli subtracts 24px from the requested render dimensions, so the
    # defaults (1600 -> 1576 raw, trimmed to 1568) avoid any resampling. Raising
    # --size alone silently upscales an interpolated image.
    if args.size > args.render_size - 24:
        print(f"    note: --size {args.size} exceeds the usable render size "
              f"({args.render_size - 24}); raise --render-size to avoid upscaling")

    sides = [s.strip() for s in args.sides.split(",") if s.strip()]
    bad = [s for s in sides if s not in ("top", "bottom")]
    if bad or not sides:
        sys.exit(f"--sides must be top and/or bottom, got: {args.sides!r}")
    stem = os.path.splitext(os.path.basename(pcb))[0]
    outdir = args.outdir or os.path.dirname(pcb)
    outputs = {}
    for side in sides:
        if side == "top" and args.top:
            outputs["top"] = os.path.abspath(args.top)
        elif side == "bottom" and args.bottom:
            outputs["bottom"] = os.path.abspath(args.bottom)
        else:
            outputs[side] = os.path.abspath(os.path.join(outdir, f"{stem}-{side}.png"))

    cli = find_kicad_cli(args.kicad_cli)
    magick = args.magick if args.magick and os.path.exists(args.magick) else find_magick()

    print(f">>> {pcb}")
    before = hashlib.md5(open(pcb, "rb").read()).hexdigest()
    scratch = []
    try:
        src, nv, npad = stripped_copy(pcb, strip_vias, strip_paste, scratch)
        if strip_vias or strip_paste:
            print(f"    stripped on a copy: {nv} vias, paste cleared on {npad} pads")
        for side, out in outputs.items():
            os.makedirs(os.path.dirname(out), exist_ok=True)
            render_side(cli, src, side, out, args.render_size)
            square(magick, out, args.size)
            print(f"    {side} -> {out}")
    finally:
        # KiCad writes its lock as ~<basename>.lck, a tilde-prefixed sibling the
        # stem glob never matches. Sweep both or the locks pile up in the repo.
        for stem_path in scratch:
            d, base = os.path.dirname(stem_path), os.path.basename(stem_path)
            for pattern in (glob.escape(stem_path) + ".*",
                            os.path.join(d, "~" + glob.escape(base) + ".*")):
                for f in glob.glob(pattern):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
        if hashlib.md5(open(pcb, "rb").read()).hexdigest() != before:
            sys.exit(f"!! {pcb} CHANGED — it never should; investigate before committing")


if __name__ == "__main__":
    main()
