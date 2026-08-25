# OpenDrone-Scripts

Tooling shared across the OpenDrone hardware repos. Everything here spans more
than one repo. Scripts that only ever serve a single repo stay in that repo.

`kicad/` is board-agnostic: give it any `.kicad_pcb`.

Before this repo existed these scripts lived in an unversioned `software/tools/`
directory, and the generic KiCad helpers were duplicated byte-for-byte between
OpenFC-Lite and OpenFC-Lite-Mini, where one copy had already drifted from the
other. One copy, under git, is the point.

## Layout

| Dir | Contents |
|---|---|
| `kicad/` | Board exports, renders, and generic KiCad file surgery |
| `kicad/multiboard/` | KiCad 10 action plugin: several boards from one schematic (OpenDrone fork of Kicad-Multi-PCB) |

## Interpreter

Most of these import `pcbnew`, so they need KiCad's bundled Python, not the
system one:

```bash
KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
$KPY ~/OpenDrone/software/OpenDrone-Scripts/kicad/export_step.py --all
```

`render_board.py` additionally needs ImageMagick (`magick`).

---

## Release procedure

One board revision is one tag and one GitHub release per repo. The order below
is a gate chain: nothing downstream is worth generating until the design checks
pass, because every artifact after step 2 is derived from the board files.

An agent can run most of this unattended. The column says which:

| | Step | Who |
|---|---|---|
| 0 | Rev number and scope | maintainer |
| 1 | ERC, DRC, parity, 3D model preflight | agent, new violation types to the maintainer |
| 2 | Revision strings | agent, except the silkscreen |
| 3 | JLCPCB fab set | agent |
| 3b | Export checked against board and schematic | agent |
| 4 | STEP set | agent |
| 5 | Schematic PDFs | agent |
| 6 | Renders | agent, **after the maintainer quits KiCad** |
| 7 | Docs and firmware | agent drafts, maintainer reads |
| 7b | Compliance evidence and DoC | agent drafts, maintainer signs |
| 8 | Tag, release, assets | agent, one go-ahead for the push |
| 9 | Website and docs site | agent, one go-ahead for Shopify |

**0. Scope.** Which boards carry the revision and what the number is. Not
derivable from the files: the four FC and ESC repos tag independently of
OpenRX, which has run a revision behind since rev2, and a revision that changes
one board does not oblige the other seven.

**1. Design gates.** All three must pass before anything is exported.

```bash
KC=/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli
$KC sch erc --severity-error --severity-warning --exit-code-violations board.kicad_sch
$KC pcb drc --schematic-parity --refill-zones --severity-error --severity-warning \
            --exit-code-violations board.kicad_pcb
$KPY kicad/check_models.py --all --products --blocking-only
```

DRC with `--schematic-parity` is the check that matters most here, because it is
the one that catches a footprint pasted straight onto the board: it reports
`extra_footprint` for a part the schematic does not have and
`duplicate_footprints` when two carry the same reference. The Fabrication
Toolkit reads the board, not the schematic, so those parts do get fabbed and
placed, duplicates under a `C6_2`, `C6_3` suffix. The cost is that the schematic
stops being the BOM, the fab set carries designators that exist nowhere else,
and one update-from-schematic deletes the lot. Both ESC boards carry PCB-only
bulk caps of exactly this kind, 89 parity items on the 30x30, which is enough
noise to hide a real parity error.

Pass `--refill-zones` or stale fills invent clearance errors: it drops
OpenFC-Lite-Mini from six errors to three. It does not write the board unless
`--save-board` is given too. Some violations are benign and get waived by hand:
multi-pad test-point footprints report as shorting nets, USB-C shell pins report
as `no pad found for pin A8`, and `lib_footprint_mismatch` is library drift that
the board does not care about, since a `.kicad_pcb` embeds its own footprint
copy.

**2. Revision strings.** The rev number lives in four places: `ARCHIVE_NAME` in
the board's `fabrication-toolkit-options.json`, the silkscreen rev text if the
board has one, the README status line, and the tag. Three of those are plain
text files. The silkscreen is not: it means writing the `.kicad_pcb` through
pcbnew, so it stays the maintainer's, and only OpenESC-20x20 carries one today.

**3. JLCPCB fab set.** The Fabrication Toolkit is a GUI plugin, but it runs
headless and its output is identical: on OpenRX-Lite the BOM, designator and
position CSVs come out byte-identical to the committed GUI export, and the
gerber zip differs only in the `TF.CreationDate` line of each file. Output lands
in `<board dir>/production/`, which is gitignored in every repo.

Two things stop the plugin's own `cli.py` from working, and both are worked
around from outside it. Its package directory name contains hyphens, so it
cannot be imported as a module and has to be copied or symlinked to a legal
name. Then it reaches into wx and `pcbnew.GetBoard()` even in CLI mode, which is
`None` outside the editor and takes down the archive-naming step after the CSVs
are already written:

```python
import wx; app = wx.App(False)
import pcbnew
from jlc_plugin.thread import ProcessThread
from jlc_plugin.options import *
board = pcbnew.LoadBoard(path)
pcbnew.GetBoard = lambda *a, **k: board
ProcessThread(wx=None, cli=path, nonInteractive=True, openBrowser=False,
              options={ARCHIVE_NAME: 'Rev3.1', AUTO_TRANSLATE_OPT: True,
                       AUTO_FILL_OPT: True, NO_BACKUP_OPT: True, ...}).join()
```

**3a. Fab-agnostic BOM, when ordering anywhere but JLCPCB.**
`$KPY kicad/universal_bom.py <board.kicad_pcb>` adds
`*_bom_universal.csv` with Manufacturer + MPN columns to the set. See its
section below.

**3b. Check the export against the design. Every time.** Scripted:
`python3 kicad/check_export.py <board.kicad_pcb>` runs the three comparisons
below against the set in `production/` and fails on C1/C3. The plugin is a third
party tool with its own translation table and its own naming rules, the export
is the one artifact the fab actually builds from, and a stale or wrong set looks
exactly like a good one. Three comparisons, all mechanical:

- **Designators against the board.** `*_designators.csv` must equal the board's
  footprints minus `exclude_from_bom`, counts included. `30x30-Rev3.zip` fails
  this against every committed state of that board: it carries the input TVS
  that rev3 deleted, and no C6 where the board of the day had three.
- **Designators against the schematic.** Anything in the export that the
  netlist does not have is a board-only part. That is allowed, but it has to be
  a decision, not a surprise, and `_2` suffixed refs are the tell.
- **Per-part quantity against the netlist.** Group the BOM rows by LCSC number
  and compare with the same grouping over the schematic. This is what catches a
  part silently missing its LCSC field, since those rows do not fail any of the
  other checks, they just quietly do not get placed.

Also eyeball the positions file for rotation and side: the toolkit applies its
own `transformations.csv` per footprint, and a package it does not know keeps
KiCad's rotation, which is how a part arrives on the board turned 90 degrees
with a perfectly clean BOM.

**4. STEP set.** `check_models.py` then `export_step.py`, clearing `export/`
first. See "Publishing a fit-check set" below.

**5. Schematic PDFs.** `$KC sch export pdf -o <out.pdf> board.kicad_sch`. Only
OpenRX has these today, in `exports/schematics/`, and they date from before the
last two revisions.

**6. Renders.** `render_board.py` into the repo's `images/`, KiCad closed. The
commands are listed under that script below. **This is the one step that stalls
on a human**: the script writes the board through pcbnew and refuses while KiCad
is open, and quitting KiCad is not an agent's call to make with a project open.
Everything else in this list runs headless.

READMEs reference the render filenames directly, so re-rendering in place
updates the docs with no edit, which is why a filename should not carry a rev
number. Both FC repos still name theirs `-rev2-`, and `OpenDrone-Docs`
`build/*-groups.json` pins those same names as GitHub raw URLs, so renaming
them is a two-repo change.

**7. Docs and firmware.** README status line and export set name,
`hardware/docs/DESIGN.md` against the current netlist, and any changelist items
the revision closed. Verify against the design files, not against the other
docs. Then confirm the committed firmware still matches the board: a revision
that moves no GPIO net leaves the Betaflight uf2 and the AM32 target valid, and
that is a netlist diff, not an assumption.

**7b. Compliance evidence and DoC.** A revision that ships to a customer ships
with a Declaration of Conformity issued for that revision, re-issued when the
revision changes. The standards-to-evidence matrix, run status and the scope
argument live in `testing/OpenDrone-Testing/Compliance/README.md`; the DoC and
technical file templates and the company-side records (registrations,
insurance) live in the incutec vault under `compliance/`. Per revision: diff
against the last shipped revision and record in the technical file whether the
change touches EMC-relevant circuitry (switchers, clocks, the RF path, I/O
filtering); an untouched circuit keeps its previous evidence, a touched one
re-runs the affected pre-screens. For OpenRX the DoC pins the firmware version,
so a firmware change re-issues it too. The maintainer signs; an agent never
signs or publishes a DoC.

**8. Tag and publish.** Tag `revN`, then attach the fab zip, the STEP set and
the schematic PDFs to the release, named so the website can read them:
`<Repo>-<rev>-fab.zip` (the JLCPCB set), `<Repo>-<rev>.step`,
`<Repo>-<rev>-schematic.pdf`. `OpenDrone-Web/scripts/sync-downloads.mjs`
maps assets to product-page download kinds by these shapes and skips anything
it does not recognise, so a differently named asset is invisible to the shop.
The rev 3.1 / 2.1 releases predate the convention and are named freely.

```bash
gh release create rev3.1 --title "OpenX Rev 3.1" --notes-file notes.md
gh release upload rev3.1 export/OpenX-rev3.1.step hardware/production/OpenX-rev3.1-fab.zip \
    OpenX-rev3.1-schematic.pdf
```

**9. Website and docs site.** `OpenDrone-Web` is a maintained mirror of the
board repos: art from the board files, specs from the README tables, release
assets from GitHub. `scripts/boards.config.json` and
`scripts/repo-sync.config.json` map product handles to the checkouts under
`~/OpenDrone/hardware` (`OPENDRONE_HARDWARE` overrides the root):

```bash
cd ~/OpenDrone/software/OpenDrone-Web
npm run gen:board-art     # public/boards/<handle>/{front,back}.png, board.svg
npm run gen:components    # public/boards/<handle>/components.json
npm run gen:schematics    # public/schematics/<handle>/*.svg + manifest.json
npm run sync:specs        # README "## Specifications" -> content/products/<handle>.json
npm run sync:downloads    # release assets -> downloads array; --check only until the chapter is wanted
```

`sync:specs` refuses to write from a dirty or stale checkout. OpenRX is the one
hand-maintained product JSON (one repo, four boards). All of it lands as one
reviewed PR: merging deploys the shop. Then `OpenDrone-Docs` (`build.py`, whose
`build/*-groups.json` pin GitHub raw image URLs by filename, so a renamed
render breaks the docs site silently).

### What no script can do

Everything above is a command or a short script, with these exceptions. They
are not tooling gaps, so do not try to route around them:

- **Quitting KiCad.** Blocks step 6 and nothing else. Unsaved work is not an
  agent's to gamble with.
- **Any design change.** Nets, placement, routing, values, footprints, zone
  refill saved back to the board. If a gate turns up something real, report and
  stop.
- **A DRC or ERC violation type nobody has judged yet.** The benign list in
  step 1 is a record of past decisions, not a rule that generalises.
- **OSHWA certification.** Each board carries a UID (BE000026, BE000029 and so
  on) and the listing has a version field. Web form, per board, no API.
- **The JLCPCB order.** Upload, DFM review, part substitution, quote, payment.
  Stock for every line can be checked beforehand; the order cannot be placed.
- **Bench validation and product photos.** A render is not a photo and an
  export is not a working board.

Both gaps this section used to name are closed: `kicad/release.py` runs steps
1 to 5 as a gate chain and refuses to continue past a failure, and
`kicad/check_export.py` is step 3b as a script. Its first run caught a real
one: Lite-UFL's J1 had no usable LCSC number and would have shipped without
its U.FL connector.

## `kicad/release.py`: the release gate chain, steps 1-5

One board through ERC/DRC-vs-baseline, model preflight, the full quote pack
(quote_pack.py: rev sync, fab export, all BOM forms, portal gerbers, export
check), STEP and schematic PDF; the first failed gate stops the run and
nothing downstream is generated.

```bash
python3 kicad/release.py <board.kicad_pcb>                    # full run
python3 kicad/release.py <board.kicad_pcb> --skip-fab-export  # keep the existing set
```

`kicad/release-baselines.json` holds the ERC/DRC violation types and counts a
human has judged; the gate fails only on a new type or a higher count. After
judging a new finding, update the baseline in the same commit that introduces
it. Boards without a baseline entry fail closed.

G6 regenerates the website board art, components.json and schematic SVGs for
the board's product handle in the OpenDrone-Web checkout (default on,
`--skip-web` to skip). The output is left uncommitted: merging it to the web
repo's main deploys the shop, so that stays a reviewed PR.

What it deliberately does not do: rev scope, silkscreen text, renders (KiCad
may be open), tags, uploads, orders, the Shopify metafield push and the docs
site rebuild. See "What no script can do".

## `kicad/check_export.py`: step 3b as a script

Checks a Fabrication Toolkit set in `production/` against the board and
schematic: designators vs board (C1), board-only refs vs schematic (C2,
informational), per-LCSC quantities (C3, catches a part silently missing its
LCSC field), and footprints the toolkit rotation table does not know (C4,
informational). C1/C3 are hard failures.

```bash
python3 kicad/check_export.py <board.kicad_pcb>
```

## `kicad/fab_export.py`: headless Fabrication Toolkit run

The GUI plugin driven from the command line, options read from the board's
`fabrication-toolkit-options.json`. Needs KiCad's bundled python.

```bash
$KPY kicad/fab_export.py <board.kicad_pcb> [--name ARCHIVE_NAME]
```

## `kicad/universal_bom.py`: fab-agnostic BOM

The Fabrication Toolkit BOM is JLCPCB-shaped: five columns, LCSC only. Other
fabs match parts on Manufacturer + MPN. This exports one CSV any fab accepts,
`Designator,Value,Footprint,Quantity,LCSC,Manufacturer,MPN`: LCSC for the
Chinese fabs, Manufacturer + MPN for everyone else, extra columns ignored by
all. Placements come from the board like the Toolkit's, so layout-only parts
(bulk cap banks) are counted, and Manufacturer/MPN gaps are joined by LCSC
number from the other footprints and the schematics beside the board. It
reports any BOM line still missing part data, which means the schematic needs
its fields filled, not the CSV hand-edited.

```bash
$KPY kicad/universal_bom.py <board.kicad_pcb> [--name ARCHIVE_NAME] [--exclude-dnp]
```

Writes `production/<ARCHIVE_NAME>_bom_universal.csv` beside the Toolkit set.
The positions CSV needs no translation: every fab reads the Toolkit's format,
though rotations follow the JLCPCB convention, so tell any other fab to verify
polarity against the gerbers in their DFM review.

## `kicad/quote_pack.py`: the one export pipeline for quoting

One command per board produces everything the big suppliers need, named to
the org convention (`<Repo>-<rev>`, the release asset stem, lowercase rev):

```bash
$KPY kicad/quote_pack.py <board.kicad_pcb> [--name STEM] [--skip-ft] [--boms-only]
```

STEM defaults to `ARCHIVE_NAME` in the board's
`fabrication-toolkit-options.json` and must end in `-rev<...>`; the rev names
the pack dir. Output, in `production/quote-pack-<rev>/`:

| File | Feeds |
|---|---|
| `<stem>.zip` | JLCPCB, PCBGOGO (full Fabrication Toolkit gerber set) |
| `<stem>_portal.zip` | NextPCB, MakerPCB and other weak parsers: drill maps dropped, `G04 #@!` attribute comments stripped, geometry identical |
| `<stem>_bom_universal.csv` | PCBGOGO, NextPCB, generic RFQ (LCSC + Manufacturer + MPN) |
| `<stem>_bom_jlcpcb.csv` | JLCPCB (the Toolkit BOM, copied) |
| `<stem>_bom_nextpcb.csv` | NextPCB template columns |
| `<stem>_bom_makerpcb.xlsx` | MakerPCB (their portal rejects everything but their xlsx layout) |
| `<stem>_bom_pcbgogo.xlsx` | PCBGOGO template layout (bare test pads marked DNS) |
| `<stem>_positions.csv` + `.zip` | Everyone; rotations follow the JLCPCB convention, other fabs verify polarity in DFM |

`--skip-ft` reuses the FT set already in `production/`; `--boms-only` also
leaves the pack's gerbers and positions untouched (use while those files are
pinned to a submitted order). `SPEC.md` in the pack dir is hand-written per
board and never touched by the script. Steps: fab_export.py (unless skipped),
universal_bom.py, then the per-fab conversions, so the BOM chain has one
source: the board plus its schematics.

`kicad/portal_gerbers.py` is the standalone form of the portal zip step:
`python3 kicad/portal_gerbers.py <stem>.zip`.

## `kicad/multiboard/`: several boards from one schematic

Fork of [Eliot-Abramo/Kicad-Multi-PCB](https://github.com/Eliot-Abramo/Kicad-Multi-PCB)
(MIT, upstream commit `175cd7d`, licence kept in `LICENSE.upstream`). One root
schematic, N `.kicad_pcb` files; a footprint belongs to the board it was placed
on first, and "Update" pulls into a board only its own footprints plus symbols
that are on no board yet. KiCad's own Update PCB from Schematic cannot do this,
it imports the whole schematic into every board. First user: OpenAIO (Base
carrier + Core hat).

```sh
sh kicad/multiboard/install.sh            # symlink into KiCad 10's plugin dir, once per machine
KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
$KPY kicad/multiboard/update.py <project dir> [board ...]   # headless Update, boards closed
```

Changed against upstream, every hunk marked `OpenDrone fork` in the code:

- `layout: "flat"` in `.kicad_multiboard.json`: the board projects live next to
  the root project instead of `boards/<name>/`, so `${KIPRJMOD}` and the lib
  tables and 3D models resolve without copies. Linking a sheet onto itself is
  refused; lib tables are not rewritten in flat layout.
- Relative symlinks for the schematic links (git stores them, a clone comes up
  linked); hardlink is the fallback. `default_board` receives unplaced parts in
  an all-boards `update.py` run; a one-board run (and the GUI Update button)
  keeps upstream semantics, the named board takes them.
- Full footprint paths (`/sheet uuid/symbol uuid`, what KiCad writes) instead of
  the bare symbol uuid, so cross-probing and a native update still match.
  Upstream also read the wrong netlist tag and never set a path at all.
- Loaded footprints keep their library nickname (upstream re-"replaced" every
  new part on the next update).
- Finds `kicad-cli` inside KiCad.app (GUI apps on macOS have no shell PATH).
- `generate_blocks: false` skips the block footprints; `work_dir` moves the log
  and temp netlist into a gitignored directory. Board delete never removes a
  directory that is not `boards/<name>/`.

Requires the schematic file's root uuid to equal `top_level_sheets[].uuid` in
the `.kicad_pro`; KiCad 10's kicad-cli otherwise resolves shared sheets to
their default references (upstream KiCad #24409). The plugin's netlist comes
from kicad-cli, so it inherits that.

## `kicad/wrl_to_step.py`: rebuild a STEP from the trusted wrl

The E6 fix path: when the .step beside a .wrl is a different part and
upstream ships the same wrong file, the .wrl the 3D viewer renders is the
geometry of record. Produces a tessellated STEP solid, exact dimensions,
faceted faces, no colors. Needs `pip install cadquery-ocp`, system python.

```bash
python3 kicad/wrl_to_step.py model.wrl -o model.step
```

## `kicad/export_step.py`: standardized STEP exports

One command exports every board in every repo to `<repo>/export/<Product>.step`:

```bash
$KPY kicad/export_step.py --all
$KPY kicad/export_step.py --all --repo OpenRX
$KPY kicad/export_step.py --all --products          # the publishable set
$KPY kicad/export_step.py <board.kicad_pcb> -o out.step
```

Four things the export does that are not obvious, all at export time on a temp
copy, with the source board never written:

- **`--fill-all-vias` is always on.** A via-in-pad array otherwise punches a
  grid of circles through every pad, which is what the Onshape import kept
  showing. Measured on OpenESC-20x20: 16.16 MB -> 14.47 MB, and byte-identical
  to deleting all 1050 vias with pcbnew, so the flag alone is enough.
  `render_board.py` has always stripped vias for the README images, which is why
  the 2D renders never revealed this.
- **Footprint silkscreen is stripped** (`--keep-fp-silk` to opt out). Component
  outlines and polarity ticks are clutter in a 3D export and mostly hidden under
  the part that owns them. The BOARD legend lives in board drawings and is
  untouched.
- **Exposed copper is repainted ENIG gold** (`--grey-pads` to opt out), the same
  0.90/0.72/0.36 `export-boards.mjs` paints the web GLB, so the STEP and the GLB
  agree instead of one reading gold and the other bare tin. The pad colour is
  found, not hardcoded: STYLED_ITEMs are grouped by the COLOUR_RGB they resolve
  to and the pad colour is the neutral grey whose styled items are all
  MANIFOLD_SOLID_BREPs. That is exactly the pad solids and never a component,
  whose models are styled per ADVANCED_FACE.
- **A nonzero kicad-cli exit is not a failure if the file was written.**
  kicad-cli exits 2 on "Cannot use VRML models when exporting to non-mesh
  formats" but still emits a complete STEP. The old check skipped post_process,
  leaving the file on disk with its products still named after the clipped temp
  board. Both OpenAIO boards hit this, so the set was silently 8/10 with two
  broken files.

### The Onshape import set

`--outdir` collects every discovered product into ONE directory instead of each
repo's `export/`, which is the folder you drag into Onshape:

```bash
$KPY kicad/export_step.py --all --products --preset standard --outdir ~/OpenDrone/_onshape
```

10 boards, 152 MB. `--preset outline` (`--board-only`) gives the same set at
2.9 MB, one solid and one product per board, if placement is all you need.

Onshape takes `.stp`/`.step` only: **`.stpz` is not supported**, and neither is
a zipped STEP. Of everything kicad-cli emits, only `step`, `stl` and `gltf` are
on Onshape's import list, and STL is a size regression (37.7 MB against STEP's
13.1 MB on the same board) that also loses B-rep, face colour and mateable
faces. `.glb` support is contradictory in Onshape's own docs (their changelog
says it shipped in 2022, their formats page lists only `.gltf`), and renaming
will not work because `.glb` is a binary container.

Getting one part per board in Onshape is an import-side operation, not a file
property. The "Create a composite part" checkbox does nothing here because
kicad-cli writes STEP as an assembly (19 PRODUCTs on the ESC), so Onshape builds
several Part Studios and the composite is per-Part-Studio; worse, the "Combine
to a single Part Studio" option that would flatten it explicitly disables the
composite checkbox. The route that works: import normally, then apply the
**Composite part** feature in the Part Studio with Closed ticked. Place a mate
connector on the board body first, because composites never own mate connectors.

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

## `kicad/model_audit.py` + `kicad/apply_models.py`: 3D model diet

easyeda2kicad models are 20-100x denser than KiCad's own model for the same
package, and most carry the LCEDA watermark **modelled as raised geometry**: a
0.8 x 0.8 mm X2SON came in at 2654 faces and 6.1 MB. That lettering renders on
every CAD picture of the board, and it is not on the real part.

`model_audit.py` reports what every model on a board costs and proposes leaner
replacements. `apply_models.py` applies an accepted mapping. Both are read-only
on geometry: **only the `(model ...)` reference is ever rewritten**, never a
footprint, pad, land pattern, net, placement or value.

```bash
python3 kicad/model_audit.py hardware/board.kicad_pcb          # audit one board
python3 kicad/model_audit.py --measure a.step b.step           # just measure
$KPY kicad/apply_models.py --map map.json                      # dry run
$KPY kicad/apply_models.py --map map.json --apply              # write
```

Three rules keep the audit honest:

- **Artwork is detected, not assumed.** Vector lettering shows up as spline
  CURVES. An earlier version keyed on spline surfaces and was wrong both ways:
  it false-positived on stock models (a rounded pin-1 dimple uses splines) and
  missed the densest easyeda models entirely, because those tessellate the
  lettering into thousands of planar facets that are still spline-bounded.
  Stock models sit at <= 24 spline curves; the easyeda ones run to 4482.
- **A candidate must be the same shape.** Sorted bounding boxes have to match
  within a tolerance (0.15 mm default), so a swap can shrink a model but never
  change what the board looks like. This is what stops a 6-pin part replacing a
  4-pin one.
- **Measure, never parse by eye.** The easyeda models are SolidWorks exports
  written `CARTESIAN_POINT ( 'NONE', (` with spaces and wrapped lines, where
  KiCad's own are `CARTESIAN_POINT('',(`. A tight regex silently skips exactly
  the models worth auditing and reports them as empty.

`apply_models.py` writes the **library and the boards**. Fixing only the
`.pretty` is invisible, because each board carries its own copy of the
footprint; fixing only the `.kicad_pcb` is surface level, because the next
placement pulls the bloated model back.

It also resets offset and rotation on a swap, and that is where the work
actually is. **KiCad stock models are authored centred on the footprint origin;
easyeda models bake in a compensating offset that does not transfer.** Carrying
the old offset across dropped a USB-C clean out of the render; zeroing it left
the 5x6 DFN MOSFETs rotated 90 degrees and the JST bodies 2.5 mm off their pads.
Every swap needs its placement re-fitted against the footprint's real pad
geometry and then checked in `render_board.py`. There is no shortcut, and the
offset is per FOOTPRINT, not per model: `JST_SM0xB-SRSS-TB` puts its origin on
the signal-pad row and needs Y +2.5, while `CONN-SMD/TH_SM0xB-*` is already
centred and needs zero.

What the audit cannot fix: a part with no stock equivalent. USB-C 16P QTWT
(10822 faces), RP2354B QFN-80, SKY13373 QFN-12, the tact switch and the microSD
sockets have no match in KiCad's library, so they keep their watermark. The USB-C
is worth keeping anyway: every stock substitute renders as a featureless block
with no receptacle mouth.

Two cheap wins the audit surfaces that are not swaps at all. Several models
exist in more than one repo under the same filename with wildly different
content (`SOT-23-6_L2.9-W1.6-P0.95-LS2.9-BL` is 3108 faces in the FC repos and
251 in the ESC repos), so re-vendoring the leaner copy costs nothing. And
`--fuse-shapes` is not a general win: 7% on the ESCs, which have overlapping
paralleled pads, and **-1% on OpenFC-Lite**, where it adds imprint edges. STEP
size tracks face count at 1.2-1.5 kB per face, and a fuse only deletes faces
where solids genuinely interpenetrate. Components sit ON the board, they do not
intersect it.

## `kicad/check_models.py`: 3D model pre-flight

kicad-cli treats an unresolvable 3D model as a warning and still exits 0, so a
board that has lost half its components exports "successfully" as a bare slab.
This turns that into a hard failure and runs it **before** the export. Run it
against the set you are about to publish; a fit-check model missing a connector
is worse than no model at all.

```bash
$KPY kicad/check_models.py --all --products
$KPY kicad/check_models.py --all --products --blocking-only   # release gate
$KPY kicad/check_models.py <board.kicad_pcb> -v
```

Six checks per footprint. E3 and E6 are counted per model and can coexist with
any other code on the same footprint; E1, E2, E4 and E5 are mutually exclusive
and stop at the first hit, so fixing E1/E2 and re-running surfaces the drift
underneath.

| | meaning | effect on the STEP |
|---|---|---|
| E1 | library nickname in no `fp-lib-table` | none: the board embeds its own footprint copy |
| E2 | footprint gone from that library | none, same reason |
| E3 | referenced model file not on disk | **component is missing from the export** |
| E4 | library has models, board instance has none | **component missing** |
| E5 | board and library disagree on path, offset, scale, rotation or visibility | none directly: the board's own values are exported |
| E6 | the `.step` substituted for a `.wrl` is a different shape | **component is exported at the wrong size or place** |

E3, E4 and E6 are blocking. E1, E2 and E5 are library hygiene, not export
defects, and the table is easy to misread: OpenESC-30x30's `4in1` reports 12 E1
and still exports every component, because the board carries its own footprint
copies.

### E6 and `--subst-models`, the one that fools the 3D viewer

KiCad's STEP exporter cannot read VRML. `export_step.py` therefore passes
`--subst-models`, which makes kicad-cli use a same-named `.step` in place of the
`.wrl` a board references. The flag is not optional: without it every
`.wrl`-referenced part is dropped outright, taking OpenRX-Lite from 9.1 MB to
3.1 MB of missing connectors, crystal and antenna.

The catch is that nothing checks the substitute is the same part. **The 3D
viewer renders the `.wrl` and the STEP export renders the `.step`**, so a bad
substitute looks perfect in KiCad and wrong in Onshape, with no warning
anywhere. That is not a defect in this script's clipping, and switching to
KiCad's File > Export > STEP does not avoid it: the same substitution option
exists there, and turning it off drops the parts instead of misplacing them.

Measured across the 8 products, 21 instances of 8 distinct models:

| model | viewer `.wrl` | exported `.step` | on |
|---|---|---|---|
| `USB-TYPE-C-SMD_TYPE-C-16P-QTWT` | 11.34 x 7.60 x 3.89 | **40.18 x 25.25 x 16.51** | both FCs, `USB1` |
| `TF-SMD_TF-PUSH` | 16.15 x 15.20 x 2.65 | **21.79 x 42.71 x 9.90** | OpenFC-Lite `Card1` |
| `ANT-SMD_L3.2-W1.6-H1.3` | 3.20 x 1.60 x 1.30 | 9.06 x 1.60 x 1.49 | all 4 RX, `AE1` |
| `CRYSTAL-SMD_4P-L1.6-W1.2-BL` | 1.60 x 1.20 x 0.41 | 1.60 x 1.20 x 2.39 | all 4 RX, `X1` |
| `SW-SMD_4P-L3.0-W2.0-P0.85-LS3.5` | 3.50 x 2.01 x 0.58 | 3.50 x 2.01 x 2.12 | both FCs, Gemini |
| `SOT-23-6_L2.9-W1.6-P0.95-LS2.9-BL` | 2.90 x 2.90 x 1.15 | 3.85 x 2.90 x 1.15 | Mini `U3`/`U4` |
| `FILTER-SMD_10P-L2.0-W1.6-BL` | 2.02 x 1.62 x 0.97 | 2.51 x 1.62 x 0.97 | Gemini, Mono |
| `X2SON-4_L0.8-W0.8-P0.48-TL-DPW` | 0.80 x 0.80 x 0.40 | 0.80 x 0.80 x 0.90 | both FCs |

The USB-C substitute is 3.5x oversized in every axis and the ratios are not
uniform, so it is a different model rather than a units error. The same 40.18 x
25.25 mm figure already appears in the `packaging_art.py` notes below, from a
separate measurement of the same file. Both ESC boards are E6 clean.

The fix is per model, in the library, not in this script: replace the bad
`.step` with one that matches the `.wrl`. Until then those exports carry wrong
geometry and `--blocking-only` refuses to build them.

E5 is the one a library-only fix cannot reach, because a `.kicad_pcb` embeds its
own copy of every footprint. On the FC boards it is not noise: the board
instances of `USB1`, `P1`, `U8`/`U14` and `Card1` carry hand-corrected offsets
the library never received (the 6P and 8P JST models are anchored on pin 1 in
the library and centred on the board, a 2.5 mm and 3.5 mm X shift). The exported
STEP uses the board values, so those exports are right and the library is stale.
`-v` prints which field moved and both sets of numbers.

Status as of 2026-08-13, all 17 discovered boards. The 8 FC/ESC/RX products are
free of E3 and E4, so every component is present, but **6 of the 8 are blocked
on E6**: both FCs and all four RX carry at least one substituted model that is
the wrong shape. Only OpenESC-20x20 and OpenESC-30x30 are releasable as they
stand. Three further boards are not publishable at all until their models are
fixed:

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
$KPY kicad/check_models.py --all --products --repo OpenRX --blocking-only && {
  rm -rf ~/OpenDrone/hardware/OpenRX/export                   # see below
  $KPY kicad/export_step.py --all --products --repo OpenRX
  gh release upload rev3.1 ~/OpenDrone/hardware/OpenRX/export/*.step \
     -R OpenDrone-hw/OpenRX
}
```

`--blocking-only` is what makes that chain usable: without it the check exits
non-zero on E1/E2/E5 too, and every FC and ESC board reports drift that has no
effect on the export, so the gate would never open.

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

## `kicad/render_board.py`: standardized board renders

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

## `kicad/packaging_art.py`: flat vector board art for packaging

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

## `kicad/dimension_overlay.py`: dimensioned README image

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

## `kicad/set_edgecuts_width.py`: normalise Edge.Cuts stroke width

Sets every Edge.Cuts graphic on a `.kicad_pcb` to one stroke width, 0.05 mm by
default. Dry-run by default, `--write` to apply; makes a `.bak` copy first.
Moved from the OpenFC repos, where both copies were identical and generic.

```bash
python3 kicad/set_edgecuts_width.py <board.kicad_pcb> --write
```

It edits the file as text, so it is the one exception to the no-raw-edit rule:
stroke widths only, nothing structural. Close KiCad before running it.

---

## What deliberately stayed in the board repos

Not everything shared a name by accident. These are board-specific and belong
where they are:

- `OpenFC-Lite*/hardware/tools/add_mpn_fields.py`: looks generic (LCSC → MPN
  via jlcsearch) but hardcodes the OpenFC root-sheet list to exclude stale
  orphan sheets. Parameterize that list before moving it here.
- `OpenFC-Lite*/hardware/tools/audit_design.py`: exists in both OpenFC repos and
  the two copies have **already drifted**. It takes no arguments at all and
  hardcodes refdes and GPIO maps; the Mini copy is annotated stale against Rev 2
  (Rev 1 U36/QFN-80/GPIO0-47 vs Rev 2 U10/QFN-60/GPIO0-29). Deduplicating it
  means parameterizing that map first, otherwise a shared copy silently reports
  against the wrong board.
- `OpenFC-Lite-Mini/hardware/tools/add_emc_note.py`: Mini only.
- `OpenESC-20x20/hardware/tools/esc_thermal.py`, `flash_openesc20.sh`.
- `OpenRX/verification/`: BOM and GPIO continuity checks tied to that board set.
- `OpenDrone-Testing/Bench/`: drives bench hardware, belongs with the test records.
- `OpenDrone-Web/scripts/`: that app's own build and deploy tooling.
- `KiCad-Library/tools/` (`build-parts-index.py`, `bump-all.sh`): operates on that
  repo's own contents and is vendored into 7 board repos as a submodule. The 7
  extra copies are submodule checkouts, not duplicates. Do not edit in place.
