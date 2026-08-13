# OpenDrone-Scripts

Tooling shared across the OpenDrone hardware repos. Everything here spans more
than one repo. Scripts that only ever serve a single repo stay in that repo.

`kicad/` is board-agnostic: give it any `.kicad_pcb`. `esc/` is board-*family*
tooling: it works on any OpenESC, not on any board, and it lives here because it
reads a board in one repo and writes into another.

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
$KPY kicad/export_step.py --all --products          # the publishable set
$KPY kicad/export_step.py <board.kicad_pcb> -o out.step
```

`export/` is gitignored in every board repo. These files are **release assets**,
not tracked source: the FC/ESC/RX set measures 134 MB, and committing it would
store a fresh copy of every board in git history on each re-export. See
"Publishing a fit-check set" below.

Boards are **discovered, never listed**. A `.kicad_pcb` counts as a board when a
`.kicad_pro` of the same stem sits beside it, which both identifies a real
project and guarantees `${KIPRJMOD}` resolves. Without that project file KiCad
silently drops every project-relative 3D model, which is how
`OpenRX-Gemini-sameside` was exporting with 10 components missing and no error.
Directories named `.history`, `backups`, `archive`, `libs`, `.pio`, `export` and
`*.pretty` are skipped. Adding a repo or a variant needs no edit to the script.

Discovery finds every real board, but not every board is a **product**. Fab
panels and bench fixtures export fine and nobody fits one into their own design,
so `--products` drops board stems ending in `-panel`, `-all`, `-QC`, `-Flashing`
and `-MotorTest`. Product identity is not derivable from the files, so that list
lives in `export_step.py`, in one place, rather than as a marker file in each
board repo. `check_models.py --products` takes the same flag and selects the
same set.

The target is a model that matches the board in your hand. The export is board
body + components + pads + silkscreen, plus copper that a mask opening leaves
bare. Each decision is measured:

- **Soldermask excluded.** kicad-cli models no mask apertures: exporting F.Mask
  and B.Mask adds only 2-3 `ADVANCED_FACE`s over a body-only export, i.e. flat
  sheets with no openings cut, which buries every pad under an unbroken slab. It
  also carries a 17% transparency factor (silkscreen 10%), which made earlier
  exports look like frosted glass. Residual transparency is zeroed in the
  written STEP.
- **Mask graphics recovered instead.** Logos and lettering drawn on F.Mask or
  B.Mask are openings that expose bare copper on the real board, which is how
  the RX in "OpenRX" reads. Since kicad-cli emits no apertures, the exposed
  regions are synthesised: mask polygons intersected with copper polygons is
  exactly the bare metal, and it is added as flat pads.
- **Tracks and zones excluded, for file size.** Including them measures 1.53x on
  OpenRX-Lite and 1.79x on OpenFC-Lite. Do not repeat the older claim that they
  are hidden under the mask: the mask is excluded here, so that copper would sit
  about 35 um proud and would be plainly visible, exactly as the pads are.
  `--preset full` adds it back for copper inspection work; that output does not
  belong in a release.
- **Copper past Edge.Cuts removed.** Edge pads are drawn beyond the outline on
  purpose so the fab plates and routes through them, but the router cuts that
  copper away on the finished board. kicad-cli exports the uncut pad, leaving
  tabs hanging in space, plus a plated barrel at every straddling drill.
  Straddling drills are notched out of the outline first so the body carries the
  castellation, then any pad with copper outside is rebuilt from scratch as a
  drill-free SMD pad carrying the clipped shape. Editing pads in place does not
  work: an already custom pad, or a front/inner/back padstack, silently keeps
  its original size.

Measured on the 8 product boards after clipping: zero drill holes outside the
outline, and the only copper left outside is 0 to 12 slivers per board (both FC
boards are at zero) of at most 7.1e-5 mm2 each. That is an order of magnitude
under the 1e-3 mm2 `MIN_OUTSIDE_MM2` threshold that deliberately ignores them,
and far below fab resolution: they are polygonisation error on rounded pads, not
overhang. Do not restate this as "zero pads outside"; it is zero *visible* pads
outside. `--no-clip` disables the whole pass.

Silkscreen is not clipped by this script and can overhang in the `.kicad_pcb`
(65.7 mm2 on OpenFC-Lite, 54.5 mm2 on the Mini). **It does not reach the STEP**:
kicad-cli clips silk to the board outline itself. Verified by measuring the
board-frame geometry in `OpenFC-Lite.step`, which spans 37.942 x 37.942 mm
against a 37.940 x 37.940 mm outline, the 2 um being tessellation. Component 3D
models are *not* clipped, which needs a CAD kernel.

Clipping happens on a temp copy beside the source board (not in `/tmp`, or
`${KIPRJMOD}` breaks). The source `.kicad_pcb` is never written and KiCad may
stay open. An unresolved 3D model is only a warning to kicad-cli, which still
exits 0, so the script reports missing models explicitly rather than shipping a
half-empty board.

## `kicad/check_models.py` — 3D model pre-flight

kicad-cli treats an unresolvable 3D model as a warning and still exits 0, so a
board that has lost half its components exports "successfully" as a bare slab.
This turns that into a hard failure and runs it **before** the export. Run it
against the set you are about to publish; a fit-check model missing a connector
is worse than no model at all.

```bash
$KPY kicad/check_models.py --all --products
$KPY kicad/check_models.py <board.kicad_pcb> -v
```

Five checks per footprint. E3 is counted per model and can coexist with any
other code on the same footprint; E1, E2, E4 and E5 are mutually exclusive and
stop at the first hit, so fixing E1/E2 and re-running surfaces the drift
underneath.

| | meaning | effect on the STEP |
|---|---|---|
| E1 | library nickname in no `fp-lib-table` | none: the board embeds its own footprint copy |
| E2 | footprint gone from that library | none, same reason |
| E3 | referenced model file not on disk | **component is missing from the export** |
| E4 | library has models, board instance has none | component missing |
| E5 | board and library disagree on path, offset, scale, rotation or visibility | none directly: the board's own values are exported |

E1 and E2 are library hygiene, not export defects, and the table is easy to
misread: OpenESC-30x30's `4in1` reports 12 E1 and still exports every component,
because the board carries its own footprint copies. E3 and E4 are the ones that
silently empty a board.

E5 is the one a library-only fix cannot reach, because a `.kicad_pcb` embeds its
own copy of every footprint. On the FC boards it is not noise: the board
instances of `USB1`, `P1`, `U8`/`U14` and `Card1` carry hand-corrected offsets
the library never received (the 6P and 8P JST models are anchored on pin 1 in
the library and centred on the board, a 2.5 mm and 3.5 mm X shift). The exported
STEP uses the board values, so those exports are right and the library is stale.
`-v` prints which field moved and both sets of numbers.

Status as of 2026-08-13, all 17 discovered boards: the 8 FC/ESC/RX products are
free of E3 and E4, so they export complete. Three boards are **not publishable**
until their models are fixed:

- `Charger`, 14 E3. Those 14 refs carry a hardcoded absolute path from before
  the repo moved under `hardware/`, so the directory they name no longer exists,
  while the other 23 refs in that same footprint directory correctly use
  `${KIPRJMOD}`. All 5 distinct model files are present at the `${KIPRJMOD}`
  path, so rewriting the prefix fixes all 14. An absolute home-directory path in
  a board file also breaks for every other person who clones the repo.
- `OpenAIO` and `OpenAIO-Whoop`, 65 E3 and 60 E2 each. Both would export as
  near-empty slabs.

## Publishing a fit-check set

The STEPs exist so people can check whether a board fits their own design, so
they ship as **GitHub release assets** attached to the product revision they
were built from, not as tracked files. `export/` stays gitignored.

Build the set, then attach it to that repo's rev release:

```bash
$KPY kicad/check_models.py --all --products --repo OpenRX     # must be E3/E4 clean
rm -rf ~/OpenDrone/hardware/OpenRX/export                     # see below
$KPY kicad/export_step.py  --all --products --repo OpenRX
gh release upload rev3.1 ~/OpenDrone/hardware/OpenRX/export/*.step \
   -R OpenDrone-hw/OpenRX
```

**Clear `export/` first.** `--products` only decides what gets *written*; it
never removes what an earlier `--all` run left behind. A plain `--all` puts
`OpenRX-all.step` (a fab panel) and the QC and Flashing fixtures in the same
directory, so the glob above would publish a panel as if it were a product.
Deleting the directory costs one re-export and makes the glob mean exactly the
publishable set.

Tags are `rev1`, `rev2`, `rev3`, one release per board revision. Assets keep the
plain product name (`OpenRX-Lite.step`); the revision is carried by the release,
not the filename. Re-exporting for a new revision means a new tag and a new
release, never overwriting an old asset, so a user who built around rev3 can
still fetch exactly what they measured against.

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
silk and outline in gold, tracks/zones/vias/fab-text stripped) with each
component drawn as a silhouette derived from its 3D model.

**This tool has never produced a committed artifact.** One commit, never modified
since. Treat its output as unverified and check it before sending anything to
print. Measured across 2588 footprints on 17 boards:

| silhouette source | share |
|---|---|
| `.wrl` mesh, concavity preserved | **7.6%** |
| convex hull of STEP vertices | 66.0% |
| courtyard polygon | 6.6% |
| library Fab shape | 1.4% |
| pad-extent rectangle | 5.5% |
| nothing drawn | 5.7% |
| skipped (bare pads) | 7.3% |

The mesh path needs a `.wrl`, and KiCad 10's bundled library ships 7236 `.step`
and zero `.wrl`, so stock footprints can never reach it. A convex hull destroys
concavity by construction: a USB-C shell measures 7.6x its true silhouette area
(40.18x25.25 mm against 11.36x7.62 mm), and a SOT-23-6 on the 30x30 ESC draws
0.94 mm too wide on a 2.9 mm part, inside the courtyard slack so nothing warns.

Known defects, none fixed: a board with no Edge.Cuts loses **every** silhouette
silently (the size guards derive from a zero-size bbox); `have3d` is keyed by
reference, so on `4in1-panel` 12 footprints lose their body to a same-ref twin
elsewhere on the panel; interior cutouts are not clipped, so on `4in1-panel` 30
coordinate pairs land inside routed slots; unresolved 3D models are never
reported.

```bash
$KPY kicad/packaging_art.py <board.kicad_pcb> --outdir packaging/ --png
```

Key flags: `--top/--bottom` SVG outputs · `--sides` · `--color '#C9A227'` ·
`--holes` drill knockout colour · `--body '#F2E7C9'` component fill ·
`--keep-traces` · `--no-components` · `--no-clip` · `--edge-width 0.3` ·
`--png --png-size 1600 --png-bg white|black|transparent`.

The source board is never touched (all edits happen on temp copies), so KiCad
can stay open. This is the one safety claim that was verified: 17 boards x 2
sides plus ~30 runs left every project file byte-identical with no sidecars.

The bottom side is plotted with `--mirror`. Implementation note: two 0.005 mm
calibration dots are planted 5 mm outside the board bbox on every plotted layer,
because kicad-cli's SVG page origin is content-dependent and the dots pin an
exact mm-to-SVG mapping for the clip path. **They are not removed**, only hidden
behind the clip group, so with `--no-clip` or on a board with no outline they are
visible and they set the page: OpenRX-Lite plots 20.29 x 24.08 mm for a
10.05 x 11.55 mm board.

## `kicad/dimension_overlay.py` — dimensioned README image

Takes a transparent square render and produces the dark-background dimensioned
image, given the board's width and length in mm.

```bash
$KPY kicad/render_board.py <board.kicad_pcb> --top /tmp/front-raw.png --bottom /tmp/back-raw.png
python3 kicad/dimension_overlay.py /tmp/front-raw.png images/front.png --width-mm 10.00 --length-mm 21.50
```

## `kicad/openfc_netlist_extract.py`, `openfc_pcb_extract.py`, `openfc_connectivity_report.py`

Netlist, placement and connectivity dumps. `openfc_netlist_extract.py` reads a
`.net` (export one first with `kicad-cli sch export netlist`); the other two read
a `.kicad_pcb` via `--pcb`. They layer: netlist provides the parser, pcb_extract
adds `parse_board`, connectivity_report consumes it.

Verified net counts after the KiCad 10 fix below: OpenFC-Lite 110, OpenESC-30x30
192, OpenRX-Gemini 100.

Known limits, all board-specific leftovers from the OpenFC repos: the `--pcb`
default is `OpenFC.kicad_pcb`, `openfc_connectivity_report.py` hardcodes an
OpenFC report title and a Rev 1 refdes (`Net-\(U36-USB_D[PM]\)`) that matches
neither current board, and its default `--expand` list is OpenFC sheet names, so
on another board large nets get truncated silently. Pass paths explicitly and
check the output against the board you pointed at.

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
reproduces the 20x20 pocket to 0.051 mm worst case (Y edges; X edges 0.003 mm).

Regenerating reproduces the committed 30x30 fixture exactly: all 34 contact pads
and all 44 edge pads land within 0.000000 mm, and the negative footprint is
byte-identical once UUIDs are stripped.

**Footgun:** the script writes `<dut>-negative.kicad_mod` (hyphen) but the
30x30 board references `4in1ESC30x30_negative` (underscore, a hand-rename).
Re-running overwrites the unused hyphen file and silently leaves the board
alone, which is the opposite of the "regeneration destroys routing" warning.
Reconcile the names before trusting a regeneration.

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
