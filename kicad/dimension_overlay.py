#!/usr/bin/env python3
"""Overlay engineering dimension arrows (width + length) on a board render.

Companion to render_board.py. Takes a transparent board PNG (board centered on a
transparent square), composites it on a dark engineering background, and draws
clean extension lines + double-headed dimension arrows with mm labels.

Does NOT touch any KiCad file — pure image post-process.

    python3 dimension_overlay.py in.png out.png --width-mm 10.00 --length-mm 21.50
    python3 dimension_overlay.py in.png out.png --no-dims      # dark-bg composite only
"""
import argparse, sys
from PIL import Image, ImageDraw, ImageFont

BG = (20, 22, 26, 255)      # dark engineering background
FG = (235, 237, 240, 255)   # dimension lines / text
LW = 3                      # line width
AH = 20                     # arrowhead length (px)
FONT = "/System/Library/Fonts/SFNSMono.ttf"


def load_font(size):
    try:
        return ImageFont.truetype(FONT, size)
    except OSError:
        return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", size)


def arrowhead(d, tip, dx, dy):
    """Filled triangle at `tip` pointing in direction (dx,dy) (unit-ish)."""
    import math
    ang = math.atan2(dy, dx)
    for s in (-1, 1):
        a = ang + s * 0.42
        bx = tip[0] - AH * math.cos(a)
        by = tip[1] - AH * math.sin(a)
        d.polygon([tip, (bx, by), (tip[0] - AH * math.cos(ang), tip[1] - AH * math.sin(ang))],
                  fill=FG)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp"); ap.add_argument("outp")
    ap.add_argument("--width-mm", type=float)
    ap.add_argument("--length-mm", type=float)
    ap.add_argument("--no-dims", action="store_true")
    ap.add_argument("--margin", type=int, default=90)
    ap.add_argument("--dim-gap", type=int, default=130, help="dim line offset from board edge")
    a = ap.parse_args()

    img = Image.open(a.inp).convert("RGBA")
    bbox = img.getbbox()                       # tight board bbox (non-transparent)
    if not bbox:
        sys.exit("input has no opaque content")
    board = img.crop(bbox)
    bw, bh = board.size

    m = a.margin
    extra = (a.dim_gap + 120) if not a.no_dims else m   # room for arrows + labels on R/B
    W = m + bw + extra
    H = m + bh + extra
    canvas = Image.new("RGBA", (W, H), BG)
    bx, by = m, m
    canvas.alpha_composite(board, (bx, by))
    d = ImageDraw.Draw(canvas)

    if not a.no_dims and a.width_mm and a.length_mm:
        x0, y0, x1, y1 = bx, by, bx + bw, by + bh
        font = load_font(46)

        # --- width (horizontal) below board ---
        wy = y1 + a.dim_gap
        d.line([(x0, y1 + 12), (x0, wy + 14)], fill=FG, width=2)     # ext lines
        d.line([(x1, y1 + 12), (x1, wy + 14)], fill=FG, width=2)
        d.line([(x0, wy), (x1, wy)], fill=FG, width=LW)             # dim line
        arrowhead(d, (x0, wy), -1, 0)
        arrowhead(d, (x1, wy), 1, 0)
        label = f"{a.width_mm:.2f} mm"
        tb = d.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        d.text(((x0 + x1) / 2 - tw / 2, wy + 22), label, fill=FG, font=font)

        # --- length (vertical) right of board ---
        lx = x1 + a.dim_gap
        d.line([(x1 + 12, y0), (lx + 14, y0)], fill=FG, width=2)
        d.line([(x1 + 12, y1), (lx + 14, y1)], fill=FG, width=2)
        d.line([(lx, y0), (lx, y1)], fill=FG, width=LW)
        arrowhead(d, (lx, y0), 0, -1)
        arrowhead(d, (lx, y1), 0, 1)
        label = f"{a.length_mm:.2f} mm"
        tb = d.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        txt = Image.new("RGBA", (tw + 8, th + 12), (0, 0, 0, 0))
        ImageDraw.Draw(txt).text((4, 2), label, fill=FG, font=font)
        txt = txt.rotate(-90, expand=True)
        canvas.alpha_composite(txt, (lx + 22, int((y0 + y1) / 2 - txt.size[1] / 2)))

    canvas.save(a.outp)
    print(f"{a.outp}  {canvas.size[0]}x{canvas.size[1]}  board {bw}x{bh}px")


if __name__ == "__main__":
    main()
