# OpenDrone-Scripts

Tooling shared across the OpenDrone hardware repos. Anything here works on *any*
board passed to it. Board-specific scripts stay in their board repo.

Before this repo existed these scripts lived in an unversioned `software/tools/`
directory, and the generic KiCad helpers were duplicated byte-for-byte between
OpenFC-Lite and OpenFC-Lite-Mini, where one copy had already drifted from the
other. One copy, under git, is the point.

## Layout

| Dir | Contents |
|---|---|
| `kicad/` | Board exports, renders, and generic KiCad file surgery |
| `esc/` | ESC test-fixture and jig generation |

## Interpreter

Most of these import `pcbnew`, so they need KiCad's bundled Python, not the
system one:

```bash
KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
$KPY ~/OpenDrone/software/OpenDrone-Scripts/kicad/export_step.py --all
```

`render_board.py` additionally needs ImageMagick (`magick`).

---

## `kicad/export_step.py` — standardized STEP exports

One command exports every board in every repo to `<repo>/export/<Product>.step`:

```bash
$KPY kicad/export_step.py --all
$KPY kicad/export_step.py --all --repo OpenRX
$KPY kicad/export_step.py <board.kicad_pcb> -o out.step
```

Boards are **discovered, never listed**. A `.kicad_pcb` counts as a board when a
`.kicad_pro` of the same stem sits beside it, which both identifies a real
project and guarantees `${KIPRJMOD}` resolves. Without that project file KiCad
silently drops every project-relative 3D model, which is how
`OpenRX-Gemini-sameside` was exporting with 10 components missing and no error.
Directories named `.history`, `backups`, `archive`, `libs`, `.pio`, `export` and
`*.pretty` are skipped. Adding a repo or a variant needs no edit to the script.

The export is board body + components + pads + silkscreen. Three decisions,
each measured rather than assumed:

- **Soldermask excluded.** kicad-cli gives the mask a 17% transparency factor
  and silkscreen 10%, which makes imported boards look like frosted glass. The
  mask solid also spans z 0.91-0.96 mm on a 1.0 mm board while pads top out at
  0.95 mm, so it covers the gold pads and greys them out. The board body is
  already opaque green. Residual transparency is zeroed in the written STEP.
- **Tracks and zones excluded.** Copper spans z 0.91-0.945 mm, inside where the
  mask sits, so it is invisible from outside a real board. Including it roughly
  doubles the file for geometry nobody can see. `--preset full` adds it back for
  copper inspection work; that output does not belong in a repo.
- **Copper past Edge.Cuts removed.** Edge pads are drawn beyond the outline on
  purpose so the fab plates and routes through them, but the router cuts that
  copper away on the finished board. kicad-cli exports the uncut pad, leaving
  tabs hanging in space. Partly-outside pads are clipped to the outline and
  wholly-outside ones deleted. `--no-clip` disables it.

Clipping happens on a temp copy beside the source board (not in `/tmp`, or
`${KIPRJMOD}` breaks). The source `.kicad_pcb` is never written and KiCad may
stay open. An unresolved 3D model is only a warning to kicad-cli, which still
exits 0, so the script reports missing models explicitly rather than shipping a
half-empty board.

## `kicad/render_board.py` — standardized board renders

The clean board PNGs used in every OpenDrone README: vias and solder paste
stripped so copper pads read as gold rather than grey paste, no floor shadow,
transparent background, centered 1568x1568 square.

The source `.kicad_pcb` is never permanently changed. The script backs up the
file bytes, strips vias/paste for the render, then restores the exact original
bytes and verifies with `cmp`, in a `finally` block. **Close KiCad first**: this
one writes the file via pcbnew. It refuses to run while KiCad is open; `--force`
overrides, but only ever point it at a throwaway copy.

```bash
R=~/OpenDrone/software/OpenDrone-Scripts/kicad/render_board.py
cd ~/OpenDrone/hardware

$KPY $R OpenRX/OpenRX-Gemini/OpenRX-Gemini.kicad_pcb     --top OpenRX/images/openrx-gemini-front.png    --bottom OpenRX/images/openrx-gemini-back.png
$KPY $R OpenRX/OpenRX-Lite/OpenRX-Lite.kicad_pcb         --top OpenRX/images/openrx-lite-front.png      --bottom OpenRX/images/openrx-lite-back.png
$KPY $R OpenRX/OpenRX-Lite-UFL/OpenRX-Lite-UFL.kicad_pcb --top OpenRX/images/openrx-lite-ufl-front.png  --bottom OpenRX/images/openrx-lite-ufl-back.png
$KPY $R OpenRX/OpenRX-Mono/OpenRX-Mono.kicad_pcb         --top OpenRX/images/openrx-mono-front.png      --bottom OpenRX/images/openrx-mono-back.png
$KPY $R OpenFC-Lite/hardware/OpenFC.kicad_pcb            --top OpenFC-Lite/images/openfc-lite-rev2-top.png           --bottom OpenFC-Lite/images/openfc-lite-rev2-bottom.png
$KPY $R OpenFC-Lite-Mini/hardware/OpenFC.kicad_pcb       --top OpenFC-Lite-Mini/images/openfc-lite-mini-rev2-top.png --bottom OpenFC-Lite-Mini/images/openfc-lite-mini-rev2-bottom.png
$KPY $R OpenESC-30x30/hardware/4in1.kicad_pcb            --top OpenESC-30x30/images/front.png --bottom OpenESC-30x30/images/back.png
$KPY $R OpenESC-20x20/hardware/4in1-mini.kicad_pcb       --top OpenESC-20x20/images/front.png --bottom OpenESC-20x20/images/back.png
```

READMEs reference these filenames directly, so re-rendering in place updates the
docs with no README edit. Commit only the PNGs.

Key flags: `--top/--bottom <path>` · `--outdir <dir>` for default
`<stem>-top.png`/`-bottom.png` · `--sides top,bottom` · `--size 1568` ·
`--keep-vias` / `--keep-paste` · `--kicad-cli PATH`.

Notes: `--quality basic` is deliberate, `high` adds a shadow halo on the
transparent background. KiCad 10's `--use-board-stackup-colors false` throws
`bad_any_cast` and `BOARD_STACKUP` is not exposed in pcbnew, so the soldermask
colour cannot be overridden here; renders use the board's own mask colour.

## `kicad/packaging_art.py` — flat vector board art for packaging

Single-colour gold-on-white vector art of a board's front/back for the black and
gold retail packaging. Not a 3D render: it composites kicad-cli SVG plots (pads,
silk and outline in gold, tracks/zones/vias/fab-text stripped) with every
component drawn as its real silhouette. Component shapes come from the
footprint's 3D model, with `.wrl` mesh triangles projected to the board plane
and unioned via pcbnew's polygon engine, so concavity is preserved and vertical
walls contribute their extents rather than collapsing to a top face. Parts
without a model fall back through library Fab shape, then courtyard polygon,
then pad-extent rectangle.

```bash
$KPY kicad/packaging_art.py <board.kicad_pcb> --outdir packaging/ --png
```

Key flags: `--top/--bottom` SVG outputs · `--sides` · `--color '#C9A227'` ·
`--holes` drill knockout colour · `--body '#F2E7C9'` component fill ·
`--keep-traces` · `--no-components` · `--no-clip` · `--edge-width 0.3` ·
`--png --png-size 1600 --png-bg white|black|transparent`.

The source board is never touched (all edits happen on temp copies), so KiCad
can stay open. The bottom side is plotted with `--mirror`. Implementation note:
two 0.005 mm calibration dots are planted 5 mm outside the board bbox on every
plotted layer, because kicad-cli's SVG page origin is content-dependent and the
dots pin an exact mm-to-SVG mapping for the clip path. They are clipped out of
the final art.

## `kicad/dimension_overlay.py` — dimensioned README image

Takes a transparent square render and produces the dark-background dimensioned
image, given the board's width and length in mm.

```bash
$KPY kicad/render_board.py <board.kicad_pcb> --top /tmp/front-raw.png --bottom /tmp/back-raw.png
python3 kicad/dimension_overlay.py /tmp/front-raw.png images/front.png --width-mm 10.00 --length-mm 21.50
```

## `kicad/openfc_netlist_extract.py`, `openfc_pcb_extract.py`, `openfc_connectivity_report.py`

Netlist, placement and connectivity dumps. All three take the board or schematic
as an argument. They keep an `OpenFC.kicad_pcb` default from when they lived in
the OpenFC repos, and `openfc_pcb_extract.py` still has a Rev 1 refdes (`U36`)
in one code path, so pass paths explicitly and check the output against the
board you actually pointed them at.

## `kicad/add_mpn_fields.py` — populate MPN/LCSC symbol fields

Queries `jlcsearch.tscircuit.com` and writes MPN and LCSC fields back into
schematic symbols. Takes the `.kicad_sch` as an argument.

## `kicad/set_edgecuts_width.py` — normalize Edge.Cuts stroke width

Fully argument-driven, no board assumptions.

## `esc/esc_qc_gen.py` — ESC-QC fixture generator

Rebuilds the `20x20-ESC-QC` fixture for another OpenESC. That board is one
negative footprint carrying the ESC contact pads plus all the board geometry
(100 x 100 outline, four M3 corner holes, and an ESC-shaped pocket cut into
Edge.Cuts), with 44 edge solder lands around it. This reproduces that from the
ESC design, read-only, so the contact geometry cannot drift from the board under
test.

```bash
cd ~/OpenDrone/hardware/OpenESC-30x30/30x30-ESC-QC
$KPY ~/OpenDrone/software/OpenDrone-Scripts/esc/esc_qc_gen.py ../hardware/4in1.kicad_pcb \
    --dut 4in1ESC30x30 --lib ESC-QC.pretty --out 30x30-ESC-QC.kicad_pcb
```

Emits `<dut>-negative.kicad_mod` plus local `TP_Pad_*` lands, and places the
negative and all 44 edge pads (F.Cu and B.Cu). **Nothing is routed.** Edge pad
positions are lifted from the 20x20 board and stay put, since the fixture is
100 x 100 for every ESC. The pocket is derived from the ESC's own pads and
reproduces the 20x20 pocket to within 0.05 mm.

## `esc/esc_jig_retarget.py` — move an existing jig to another ESC

Keeps a jig's layout exactly as drawn (outline, mounting holes, headers, banana
jacks, silkscreen) and moves only the pogo pins that touch the ESC. Used to make
the 30x30 flashing station out of the fabbed 20x20 one.

```bash
$KPY esc/esc_jig_retarget.py 20x20-ESC-Flashing.kicad_pcb ../hardware/4in1.kicad_pcb \
    --dut 4in1ESC30x30 --strip-tracks --swap-outline 30 --retext "20x20=30x30" \
    --out 30x30-ESC-Flashing.kicad_pcb
```

Pins are matched by net name, so the template's naming drives placement:
`/SWDn_CLK` and `/SWDn_DIO` go to that channel's PA14 and PA13 test points,
`/VBAT` and `GND` to the battery pads. Channel numbers come from the ESC
schematic sheets, not from position. `--strip-tracks` clears the old routing,
`--swap-outline R` replaces silkscreen graphics within R mm of the jig centre
with the target ESC's outline, `--retext` fixes silk labels.

## `esc/esc_fixture_gen.py`

Earlier fixture generator, superseded in practice by `esc_qc_gen.py`. Kept
because it is the only thing that produced the original fixture geometry.

---

## What deliberately stayed in the board repos

Not everything shared a name by accident. These are board-specific and belong
where they are:

- `OpenFC-Lite*/hardware/tools/audit_design.py` — exists in both OpenFC repos and
  the two copies have **already drifted**. It takes no arguments at all and
  hardcodes refdes and GPIO maps; the Mini copy is annotated stale against Rev 2
  (Rev 1 U36/QFN-80/GPIO0-47 vs Rev 2 U10/QFN-60/GPIO0-29). Deduplicating it
  means parameterizing that map first, otherwise a shared copy silently reports
  against the wrong board.
- `OpenFC-Lite-Mini/hardware/tools/add_emc_note.py` — Mini only.
- `OpenESC-20x20/hardware/tools/esc_thermal.py`, `flash_openesc20.sh`.
- `OpenRX/verification/` — BOM and GPIO continuity checks tied to that board set.
- `OpenDrone-Testing/Bench/` — drives bench hardware, belongs with the test records.
- `OpenDrone-Web/scripts/` — that app's own build and deploy tooling.
- `KiCad-Library/tools/` (`build-parts-index.py`, `bump-all.sh`) — operates on that
  repo's own contents and is vendored into 7 board repos as a submodule. The 7
  extra copies are submodule checkouts, not duplicates. Do not edit in place.
