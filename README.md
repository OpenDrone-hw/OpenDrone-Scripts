# OpenDrone-Scripts

Reusable KiCad tooling shared by the OpenDrone hardware repositories. Scripts
in this repository must accept an explicit board, model, or root directory and
must not encode the assumptions of one board family. Product-specific release
policy, accepted-violation baselines, supplier records, and internal runbooks
are outside this repository's scope.

## Requirements

Most board tools require KiCad's bundled Python because they import `pcbnew`:

```bash
KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
$KPY kicad/render_board.py path/to/board.kicad_pcb --outdir images
```

Tools that use only the Python standard library run with `python3`. STEP repair
and post-processing tools additionally require `cadquery-ocp`. Read `--help`
before using a tool that can write source files; write operations are opt-in
unless the command explicitly says otherwise.

## Board inspection

| Tool | Purpose |
| --- | --- |
| `kicad/netlist_extract.py` | Export component, IC, sheet, and power-net summaries from a KiCad s-expression netlist. |
| `kicad/pcb_extract.py` | Export footprints, IC pad connectivity, and net counts from a `.kicad_pcb`. |
| `kicad/connectivity_report.py` | Produce greppable CSV and readable Markdown connectivity reports from a `.kicad_pcb`. |
| `kicad/check_models.py` | Check board and library 3D-model references before export. |
| `kicad/check_export.py` | Compare a Fabrication Toolkit export with its board and schematic. |

The connectivity tools are deliberately generic and require an explicit input:

```bash
python3 kicad/netlist_extract.py path/to/board.net --outdir analysis/netlist
python3 kicad/pcb_extract.py path/to/board.kicad_pcb --outdir analysis/pcb
python3 kicad/connectivity_report.py path/to/board.kicad_pcb \
  --outdir analysis/connectivity --expand '^/USB/'
```

`--expand` is repeatable. No product-specific net names are expanded by
default.

## Manufacturing data

| Tool | Purpose |
| --- | --- |
| `kicad/fab_export.py` | Run the KiCad Fabrication Toolkit headlessly. |
| `kicad/universal_bom.py` | Generate a manufacturer/MPN-aware BOM from a board. |
| `kicad/quote_pack.py` | Assemble generic and supplier-formatted fabrication inputs. |
| `kicad/portal_gerbers.py` | Produce a compatibility copy of a Gerber archive for limited upload parsers. |
| `kicad/gerber_check.py` | Classify and validate a Gerber archive before handoff. |
| `kicad/handoff_pack.py` | Build a supplier-neutral handoff pack from explicit board inputs. |
| `kicad/assembly_drawing.py` | Render per-side assembly drawings with pin-1 markings. |
| `kicad/import_part.py` | Import an LCSC part into an explicitly selected project-local library. |
| `kicad/set_edgecuts_width.py` | Normalize `Edge.Cuts` widths; dry-run unless `--write` is passed. |

Generated fabrication data is output, not source. Whether it is reviewed,
published, quoted, or ordered is an organizational policy outside this public
tool repository.

## Images and CAD exports

| Tool | Purpose |
| --- | --- |
| `kicad/render_board.py` | Render standardized top and bottom board PNGs without modifying the source board. |
| `kicad/packaging_art.py` | Generate flat vector board artwork from the PCB geometry. |
| `kicad/dimension_overlay.py` | Add dimension annotations to an existing board image. |
| `kicad/export_step.py` | Export normalized board STEP models, individually or as an explicitly rooted batch. |
| `kicad/step_post.py` | Post-process STEP geometry using Open CASCADE. |
| `kicad/wrl_to_step.py` | Convert VRML meshes to STEP and repair model trees. |
| `kicad/model_audit.py` | Measure 3D-model cost and find same-size replacement candidates. |
| `kicad/apply_models.py` | Apply an explicit model map or placement-correction catalogue; dry-run unless `--apply` is passed. |

Batch operations require their scope to be stated:

```bash
$KPY kicad/export_step.py --all --root path/to/hardware --dry-run
$KPY kicad/apply_models.py --root path/to/hardware --audit
python3 kicad/model_audit.py path/to/board.kicad_pcb --root path/to/hardware
python3 kicad/wrl_to_step.py --prefer-catalogue path/to/hardware \
  --catalogue path/to/model-catalogue
```

## Multi-board plugin

`kicad/multiboard/` is an MIT-licensed fork of Kicad-Multi-PCB for projects in
which one schematic drives several PCB layouts. The upstream licence is kept in
`kicad/multiboard/LICENSE.upstream`.

Install it by linking the plugin into KiCad's action-plugin directory:

```bash
sh kicad/multiboard/install.sh
```

The headless updater accepts the project directory and optional board names:

```bash
$KPY kicad/multiboard/update.py path/to/project [board ...]
```

## Scope rule

A script belongs here when its implementation works across unrelated KiCad
projects through explicit inputs. Board-specific GPIO maps, reference
designators, release approvals, and operational state stay with their owning
project.

MIT licensed. See `LICENSE`.
