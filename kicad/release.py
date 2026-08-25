#!/usr/bin/env python3
"""The release gate chain, steps 1-5 of the release procedure, per board.

Runs everything headless and refuses to continue past a failed gate, which
is the wrapper the README said was missing. What it does NOT do is also by
design: rev scope, silkscreen text, renders while KiCad is open, tags,
uploads, orders. Those stay human or get their own go-ahead.

    python3 release.py <board.kicad_pcb> [--skip-fab-export] [--out DIR]

Gates, in order; the first failure stops the run:
  G1  ERC and DRC (parity, refilled zones) against release-baselines.json:
      fails on any violation type not in the baseline, or a count above it
  G2  check_models.py --blocking-only on this board (E3/E4/E6)
  G3  quote pack (quote_pack.py, unless --skip-fab-export keeps an existing
      set) then check_export.py C1/C3 against board and schematic
  G4  STEP export via export_step.py
  G5  schematic PDF via kicad-cli

Artifacts land in the usual places: production/, export/. Run once per
board; the calling shell loops over a repo's boards.

ERC/DRC run with kicad-cli; check_models, fab and STEP export need KiCad's
bundled python (KPY env var, or the standard KiCad.app path). This script
itself runs under system python3 and shells out.
"""
import argparse, collections, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
KC = os.environ.get("KICAD_CLI", "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
KPY = os.environ.get("KPY", "/Applications/KiCad/KiCad.app/Contents/Frameworks/"
                            "Python.framework/Versions/Current/bin/python3")
BASELINES = os.path.join(HERE, "release-baselines.json")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def rel_key(board):
    parts = os.path.abspath(board).split(os.sep)
    return os.sep.join(parts[-3:]).removesuffix(".kicad_pcb")


def violations(report):
    j = json.load(open(report))
    v = j.get("violations", []) + [x for s in j.get("sheets", [])
                                   for x in s.get("violations", [])]
    return collections.Counter(f"{x['type']}:{x['severity']}" for x in v)


def gate1(board, tmp):
    sch = os.path.splitext(board)[0] + ".kicad_sch"
    erc = os.path.join(tmp, "erc.json")
    drc = os.path.join(tmp, "drc.json")
    run([KC, "sch", "erc", "--format", "json", "-o", erc,
         "--severity-error", "--severity-warning", sch])
    run([KC, "pcb", "drc", "--format", "json", "-o", drc, "--schematic-parity",
         "--refill-zones", "--severity-error", "--severity-warning", board])
    base = json.load(open(BASELINES))["boards"].get(rel_key(board))
    if base is None:
        return [f"no baseline for {rel_key(board)}: a human judges the full "
                "report first, then adds it to release-baselines.json"]
    problems = []
    for kind, rep in (("erc", erc), ("drc", drc)):
        for key, n in violations(rep).items():
            allowed = base[kind].get(key, 0)
            if n > allowed:
                problems.append(f"{kind} {key}: {n} > baseline {allowed}")
    return problems


def gate2(board):
    r = run([KPY, os.path.join(HERE, "check_models.py"), board, "--blocking-only"])
    blocking = "BLOCKS EXPORT" in r.stdout
    return ["check_models reports blocking model findings; run it directly "
            "for the E-codes"] if blocking else []


def gate3(board, skip_export):
    # quote_pack.py = rev-text sync, fab export, universal + per-fab BOMs,
    # portal gerbers, positions, then check_export as its own exit gate
    cmd = [KPY, os.path.join(HERE, "quote_pack.py"), board]
    if skip_export:
        cmd.append("--skip-ft")
    r = run(cmd)
    print(r.stdout.rstrip())
    if r.returncode:
        return [f"quote_pack failed: {(r.stderr or r.stdout).strip()[-300:]}"]
    return []


def product_name(board):
    """Repo name for hardware/ projects, variant dir name for multi-board."""
    bdir = os.path.basename(os.path.dirname(os.path.abspath(board)))
    repo = os.path.dirname(os.path.dirname(os.path.abspath(board)))
    return os.path.basename(repo) if bdir == "hardware" else bdir


def gate4(board):
    # --all --products --repo: export_step.py owns discovery, naming and
    # placement of the publishable set; a single-board -o would drift from it
    repo = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(board))))
    r = run([KPY, os.path.join(HERE, "export_step.py"),
             "--all", "--products", "--repo", repo])
    wrote = [l.strip() for l in r.stdout.splitlines() if ".step" in l]
    if r.returncode or not wrote:
        return [f"STEP export failed: {(r.stderr or r.stdout).strip()[-200:]}"]
    for line in wrote:
        print(f"    STEP: {line}")
    return []


def gate5(board):
    sch = os.path.splitext(board)[0] + ".kicad_sch"
    repo = os.path.dirname(os.path.dirname(os.path.abspath(board)))
    out = os.path.join(repo, "export",
                       product_name(board) + "-schematic.pdf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    r = run([KC, "sch", "export", "pdf", "-o", out, sch])
    if r.returncode:
        return [f"schematic PDF failed: {r.stderr.strip()[-200:]}"]
    print(f"    PDF:  {out}")
    return []


WEB_REPO = os.environ.get(
    "OPENDRONE_WEB", os.path.expanduser("~/OpenDrone/software/OpenDrone-Web"))


def gate6(board):
    """Website board art, components and schematic SVGs for this board's
    product handle. In the pipeline by default; only the Shopify push and
    the docs-site rebuild stay human-gated."""
    cfg = os.path.join(WEB_REPO, "scripts", "boards.config.json")
    if not os.path.exists(cfg):
        return [f"OpenDrone-Web checkout not found at {WEB_REPO} "
                "(set OPENDRONE_WEB, or --skip-web)"]
    pcb = os.path.abspath(board)
    cfg_data = json.load(open(cfg))
    root = os.path.expanduser(os.environ.get("OPENDRONE_HARDWARE",
                                             cfg_data.get("root", "")))
    handle = next((e["handle"] for e in cfg_data["boards"]
                   if os.path.abspath(os.path.join(root, e["pcb"])) == pcb),
                  None)
    if handle is None:
        print(f"    no product handle maps to {pcb}; skipping site art")
        return []
    sch = os.path.splitext(pcb)[0] + ".kicad_sch"
    for cmd in (["node", "scripts/export-board-art.mjs", pcb, handle],
                ["node", "scripts/export-board-art.mjs", pcb, handle,
                 "--components-only"],
                ["node", "scripts/export-schematics.mjs", sch, handle]):
        r = run(cmd, cwd=WEB_REPO)
        if r.returncode:
            return [f"{' '.join(cmd[1:3])} failed: "
                    f"{(r.stderr or r.stdout).strip()[-200:]}"]
    print(f"    site art: public/boards/{handle}/ and "
          f"public/schematics/{handle}/ in OpenDrone-Web (uncommitted; "
          "review and PR, merging deploys the shop)")
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--skip-fab-export", action="store_true",
                    help="check the existing production set instead of re-exporting")
    ap.add_argument("--skip-web", action="store_true",
                    help="skip the website art regeneration")
    a = ap.parse_args()
    import tempfile
    gates = [("G1 design gates (ERC/DRC vs baseline)", lambda t: gate1(a.board, t)),
             ("G2 3D model preflight", lambda t: gate2(a.board)),
             ("G3 fab set + export check", lambda t: gate3(a.board, a.skip_fab_export)),
             ("G4 STEP export", lambda t: gate4(a.board)),
             ("G5 schematic PDF", lambda t: gate5(a.board))]
    if not a.skip_web:
        gates.append(("G6 website board art", lambda t: gate6(a.board)))
    with tempfile.TemporaryDirectory() as tmp:
        for name, fn in gates:
            print(f"== {name}")
            problems = fn(tmp)
            if problems:
                for p in problems:
                    print(f"   FAIL {p}")
                print(f"stopped at {name}: nothing downstream was generated")
                return 1
    print("all gates passed; remaining steps are human: renders if KiCad is "
          "open, silkscreen rev text, tag + release upload, website")
    return 0


if __name__ == "__main__":
    sys.exit(main())
