import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
KICAD = REPO / "kicad"
sys.path.insert(0, str(KICAD))

from pcb_extract import parse_board  # noqa: E402


BOARD = '''(kicad_pcb
  (net "GND")
  (net "/Core/SIGNAL")
  (footprint "Package:QFN-4"
    (property "Reference" "U1")
    (property "Value" "Example MCU")
    (property "LCSC" "C123")
    (pad "1" smd rect (net "/Core/SIGNAL") (pinfunction "GPIO") (pintype "bidirectional"))
    (pad "2" smd rect (net "GND") (pinfunction "GND") (pintype "power_in"))))
'''

NETLIST = '''(export
  (components
    (comp
      (ref "U1")
      (value "Example MCU")
      (footprint "Package:QFN-4")
      (property (name "Sheetname") (value "Core"))
      (property (name "LCSC") (value "C123"))))
  (nets
    (net (code "1") (name "+3V3")
      (node (ref "U1") (pin "1") (pinfunction "VDD")))))
'''


class ConnectivityToolsTest(unittest.TestCase):
    def test_parse_board_accepts_kicad_10_net_form(self):
        with tempfile.TemporaryDirectory() as td:
            board = Path(td) / "example.kicad_pcb"
            board.write_text(BOARD)
            footprints, nets = parse_board(board)

        self.assertEqual(nets, {})
        self.assertEqual([fp.ref for fp in footprints], ["U1"])
        self.assertEqual(footprints[0].pads[0].net_name, "/Core/SIGNAL")

    def test_connectivity_report_uses_input_name_not_product_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            board = root / "example.kicad_pcb"
            out = root / "report"
            board.write_text(BOARD)
            subprocess.run(
                [sys.executable, str(KICAD / "connectivity_report.py"),
                 str(board), "--outdir", str(out)],
                check=True,
            )
            report = (out / "nets.md").read_text()

        self.assertIn("# example Net Connectivity", report)
        self.assertNotIn("OpenFC", report)
        self.assertIn("/Core/SIGNAL", report)

    def test_extractors_require_explicit_inputs(self):
        for script in ("netlist_extract.py", "pcb_extract.py", "connectivity_report.py"):
            result = subprocess.run(
                [sys.executable, str(KICAD / script)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0, script)
            self.assertIn("required", result.stderr.lower(), script)

    def test_netlist_extract_writes_generic_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            netlist = root / "example.net"
            out = root / "extract"
            netlist.write_text(NETLIST)
            subprocess.run(
                [sys.executable, str(KICAD / "netlist_extract.py"),
                 str(netlist), "--outdir", str(out)],
                check=True,
            )

            self.assertTrue((out / "components.csv").is_file())
            self.assertIn("+3V3", (out / "power_nets.json").read_text())


class ExplicitScopeTest(unittest.TestCase):
    def run_tool(self, script, *args):
        return subprocess.run(
            [sys.executable, str(KICAD / script), *args],
            capture_output=True,
            text=True,
        )

    def test_batch_step_export_requires_root(self):
        result = self.run_tool("export_step.py", "--all", "--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--root is required with --all", result.stderr)

    def test_catalogue_replacement_requires_catalogue(self):
        result = self.run_tool("wrl_to_step.py", "--prefer-catalogue", ".")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--catalogue is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
