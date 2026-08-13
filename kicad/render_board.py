#!/usr/bin/env python3
"""
render_board.py — standardized KiCad PCB → PNG renders for the OpenDrone projects.

Produces the clean board renders used in every OpenDrone README: vias and solder
paste stripped (so copper pads show as gold, not gray paste deposits), no floor
shadow, transparent background, centered square output.

The source .kicad_pcb is NEVER changed permanently. The script backs up the file
bytes, strips vias/paste for the render only, then restores the EXACT original
bytes and verifies them (cmp). If anything fails, the original is restored in a
finally block.

MUST be run with KiCad's bundled Python (it imports pcbnew):

  /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 \
      software/OpenDrone-Scripts/kicad/render_board.py hardware/4in1.kicad_pcb --top images/front.png --bottom images/back.png

KiCad must be CLOSED before running (pcbnew writes the file). The script refuses to
run if it detects KiCad open; override with --force.

Examples:
  # explicit output names (what the OpenDrone READMEs use)
  render_board.py hardware/4in1.kicad_pcb --top images/front.png --bottom images/back.png

  # default names: <outdir>/<stem>-top.png and <stem>-bottom.png
  render_board.py hardware/OpenFC.kicad_pcb --outdir images/

  # keep paste/vias, only one side, custom size
  render_board.py board.kicad_pcb --sides top --keep-paste --keep-vias --size 2000

Dependencies: KiCad 9/10 (pcbnew + kicad-cli) and ImageMagick (`magick`).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

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


def kicad_is_running():
    try:
        return subprocess.run(["pgrep", "-i", "kicad"], capture_output=True).returncode == 0
    except Exception:
        return False


def strip_board(path, strip_vias, strip_paste):
    """Remove vias and/or solder paste in place. Returns (n_vias, n_pads)."""
    import pcbnew
    board = pcbnew.LoadBoard(path)
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
    pcbnew.SaveBoard(path, board)
    return nv, npad


def render_side(cli, pcb, side, out, render_px):
    subprocess.run(
        [cli, "pcb", "render", "--side", side, "-w", str(render_px), "-h", str(render_px),
         "--quality", "basic", "--background", "transparent", "-o", out, pcb],
        check=True, capture_output=True,
    )


def square(magick, out, size):
    if not magick:
        print(f"    (magick not found — left {os.path.basename(out)} untrimmed at render size)")
        return
    # -fuzz 1% so the alpha trim treats anti-aliased edge fringe (semi-transparent
    # halo around the board on the transparent background) as background and crops
    # it deterministically — without it, a stray near-transparent fringe pixel can
    # shift the trim by a pixel or two and jitter the registration between runs.
    subprocess.run(
        [magick, out, "-fuzz", "1%", "-trim", "+repage", "-background", "none",
         "-gravity", "center",
         "-resize", f"{size}x{size}", "-extent", f"{size}x{size}", out],
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
    ap.add_argument("--force", action="store_true", help="run even if KiCad appears to be open")
    args = ap.parse_args()

    pcb = os.path.abspath(args.pcb)
    if not pcb.endswith(".kicad_pcb") or not os.path.exists(pcb):
        sys.exit(f"not a .kicad_pcb: {pcb}")

    strip_vias = not args.keep_vias
    strip_paste = not args.keep_paste
    if (strip_vias or strip_paste) and kicad_is_running() and not args.force:
        sys.exit("KiCad appears to be running — close it (pcbnew must write the file), or pass --force.")

    sides = [s.strip() for s in args.sides.split(",") if s.strip()]
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
    magick = find_magick()

    print(f">>> {pcb}")
    backup = tempfile.NamedTemporaryFile(prefix="render_board_", suffix=".kicad_pcb", delete=False).name
    shutil.copy2(pcb, backup)
    try:
        if strip_vias or strip_paste:
            nv, npad = strip_board(pcb, strip_vias, strip_paste)
            print(f"    stripped: {nv} vias, paste cleared on {npad} pads")
        for side, out in outputs.items():
            os.makedirs(os.path.dirname(out), exist_ok=True)
            render_side(cli, pcb, side, out, args.render_size)
            square(magick, out, args.size)
            print(f"    {side} -> {out}")
    finally:
        shutil.copy2(backup, pcb)
        same = open(backup, "rb").read() == open(pcb, "rb").read()
        os.unlink(backup)
        if same:
            print("    restored OK (byte-identical)")
        else:
            sys.exit(f"!! RESTORE MISMATCH on {pcb} — investigate before committing")


if __name__ == "__main__":
    main()
