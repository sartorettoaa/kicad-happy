#!/usr/bin/env python3
"""Unit tests for kicad_cli_review.py.

Tests kicad-cli discovery (env override, PATH, platform candidates,
Flatpak fallback), ERC/DRC JSON parsing, the ground-truth summary, and
command construction for ``run_kicad_cli`` — all via mocked ``subprocess.run``
so the suite runs fast, deterministically, and without a real KiCad install.

Run with:
    python3 skills/kicad/scripts/test_kicad_cli_review.py
    python3 -m unittest skills/kicad/scripts/test_kicad_cli_review.py
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add script directory to sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import kicad_cli_review as kcr


# ---------------------------------------------------------------------------
# find_kicad_cli()
# ---------------------------------------------------------------------------

class TestFindKicadCli(unittest.TestCase):
    """kicad-cli discovery: env override, PATH, install dirs, Flatpak."""

    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "kicad-cli.exe"
            exe.write_text("")
            with patch.dict(os.environ, {"KICAD_CLI_PATH": str(exe)}), \
                 patch.object(kcr.shutil, "which", return_value=None):
                self.assertEqual(kcr.find_kicad_cli(), [str(exe)])

    def test_env_override_ignored_when_not_a_file(self):
        with patch.dict(os.environ, {"KICAD_CLI_PATH": "/does/not/exist"}), \
             patch.object(kcr.shutil, "which", return_value="/usr/bin/kicad-cli"):
            self.assertEqual(kcr.find_kicad_cli(), ["/usr/bin/kicad-cli"])

    def test_path_lookup(self):
        with patch.dict(os.environ, {"KICAD_CLI_PATH": ""}), \
             patch.object(kcr.shutil, "which", return_value="/opt/bin/kicad-cli") as mock_which:
            self.assertEqual(kcr.find_kicad_cli(), ["/opt/bin/kicad-cli"])
            mock_which.assert_called_once_with("kicad-cli")

    def test_windows_candidate_picks_newest_available_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Only KiCad 9.0 is actually installed; 10.0 is checked first and absent.
            kicad_bin = Path(tmp) / "KiCad" / "9.0" / "bin"
            kicad_bin.mkdir(parents=True)
            exe = kicad_bin / "kicad-cli.exe"
            exe.write_text("")
            with patch.dict(os.environ, {"KICAD_CLI_PATH": "", "ProgramFiles": tmp}), \
                 patch.object(kcr.shutil, "which", return_value=None), \
                 patch.object(kcr.sys, "platform", "win32"):
                self.assertEqual(kcr.find_kicad_cli(), [str(exe)])

    def test_linux_standard_candidate(self):
        def _is_file(self):
            return self.as_posix() == "/usr/bin/kicad-cli"

        with patch.dict(os.environ, {"KICAD_CLI_PATH": ""}), \
             patch.object(kcr.shutil, "which", return_value=None), \
             patch.object(kcr.sys, "platform", "linux"), \
             patch.object(Path, "is_file", _is_file):
            # str(Path(...)) renders with the *host* OS separator even though
            # the candidate was built from a POSIX literal, so compare against
            # the same round-trip rather than a hardcoded forward-slash string.
            self.assertEqual(kcr.find_kicad_cli(), [str(Path("/usr/bin/kicad-cli"))])

    def test_flatpak_fallback_when_no_candidate_file_exists(self):
        def _which(name):
            return "/usr/bin/flatpak" if name == "flatpak" else None

        with patch.dict(os.environ, {"KICAD_CLI_PATH": ""}), \
             patch.object(kcr.shutil, "which", side_effect=_which), \
             patch.object(kcr.sys, "platform", "linux"), \
             patch.object(Path, "is_file", lambda self: False):
            self.assertEqual(
                kcr.find_kicad_cli(),
                ["flatpak", "run", "--command=kicad-cli", "org.kicad.KiCad"],
            )

    def test_not_found_returns_none(self):
        with patch.dict(os.environ, {"KICAD_CLI_PATH": ""}), \
             patch.object(kcr.shutil, "which", return_value=None), \
             patch.object(kcr.sys, "platform", "linux"), \
             patch.object(Path, "is_file", lambda self: False):
            self.assertIsNone(kcr.find_kicad_cli())


# ---------------------------------------------------------------------------
# parse_erc / parse_drc / _violation_to_finding
# ---------------------------------------------------------------------------

class TestParseErc(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(kcr.parse_erc(Path("/does/not/exist.json")), [])

    def test_invalid_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "erc.json"
            bad.write_text("{not valid json")
            self.assertEqual(kcr.parse_erc(bad), [])

    def test_parses_violations_with_sheet_and_refs(self):
        payload = {
            "sheets": [
                {
                    "path": "/root/sheet1",
                    "violations": [
                        {
                            "type": "pin_not_connected",
                            "severity": "error",
                            "description": "Pin not connected",
                            "items": [
                                {"description": "Symbol PS1 Pin 4 [VCC]", "pos": {"x": 1.0, "y": 2.0}},
                            ],
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            erc = Path(tmp) / "erc.json"
            erc.write_text(json.dumps(payload))
            findings = kcr.parse_erc(erc)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["rule_id"], "ERC-pin_not_connected")
        self.assertEqual(f["severity"], "error")
        self.assertIn("[/root/sheet1]", f["summary"])
        self.assertEqual(f["refs"], ["PS1"])
        self.assertEqual(f["position"], {"x": 1.0, "y": 2.0})
        self.assertFalse(f["excluded"])
        self.assertEqual(f["analysis_source"], "kicad")
        self.assertEqual(f["confidence"], "deterministic")

    def test_excluded_violation_downgrades_to_info(self):
        payload = {
            "sheets": [
                {
                    "path": "/root/sheet1",
                    "violations": [
                        {
                            "type": "unannotated",
                            "severity": "warning",
                            "excluded": True,
                            "description": "Symbol not annotated",
                            "items": [{"description": "Symbol U9"}],
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            erc = Path(tmp) / "erc.json"
            erc.write_text(json.dumps(payload))
            findings = kcr.parse_erc(erc)
        self.assertEqual(findings[0]["severity"], "info")
        self.assertTrue(findings[0]["excluded"])
        self.assertIn("(excluded in KiCad)", findings[0]["summary"])


class TestParseDrc(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(kcr.parse_drc(Path("/does/not/exist.json")), [])

    def test_violations_unconnected_and_parity(self):
        payload = {
            "violations": [
                {
                    "type": "courtyards_overlap",
                    "severity": "error",
                    "description": "Courtyards overlap",
                    "items": [{"description": "Footprint U5"}],
                }
            ],
            "unconnected_items": [
                {
                    "type": "unconnected_items",
                    "description": "Pad not connected",
                    "items": [{"description": "Pad 2 [GND] of U5 on F.Cu"}],
                }
            ],
            "schematic_parity": [
                {
                    "type": "net_conflict",
                    "description": "Net mismatch",
                    "items": [{"description": "Footprint H3"}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            drc = Path(tmp) / "drc.json"
            drc.write_text(json.dumps(payload))
            findings = kcr.parse_drc(drc)
        self.assertEqual(len(findings), 3)
        rule_ids = {f["rule_id"] for f in findings}
        self.assertIn("DRC-courtyards_overlap", rule_ids)
        self.assertIn("DRC-unconnected", rule_ids)
        self.assertIn("DRC-parity-net_conflict", rule_ids)
        unconnected = next(f for f in findings if f["rule_id"] == "DRC-unconnected")
        self.assertEqual(unconnected["severity"], "error")
        self.assertEqual(unconnected["refs"], ["U5"])
        parity = next(f for f in findings if f["rule_id"] == "DRC-parity-net_conflict")
        self.assertEqual(parity["refs"], ["H3"])


# ---------------------------------------------------------------------------
# drc_types / summarize
# ---------------------------------------------------------------------------

class TestSummarize(unittest.TestCase):
    def setUp(self):
        self.findings = [
            {"rule_id": "DRC-courtyards_overlap", "excluded": False},
            {"rule_id": "DRC-items_not_allowed", "excluded": False},
            {"rule_id": "DRC-edge_clearance", "excluded": False},
            {"rule_id": "DRC-lib_footprint_issues", "excluded": False},
            {"rule_id": "DRC-unconnected", "excluded": False},
            {"rule_id": "DRC-parity-net_conflict", "excluded": False},
            {"rule_id": "DRC-clearance", "excluded": True},
            {"rule_id": "ERC-pin_not_connected", "excluded": False},
            {"rule_id": "ERC-unannotated", "excluded": True},
        ]

    def test_drc_types_excludes_excluded(self):
        types = kcr.drc_types(self.findings)
        self.assertIn("courtyards_overlap", types)
        self.assertNotIn("clearance", types)  # excluded=True entry is skipped

    def test_summary_counts(self):
        summary = kcr.summarize(self.findings)
        # geometric = courtyards_overlap, items_not_allowed, edge_clearance
        # (lib_footprint_issues is non-geometric; unconnected/parity counted separately)
        self.assertEqual(summary["drc_geometric"], 3)
        self.assertEqual(summary["drc_courtyard"], 1)
        self.assertEqual(summary["drc_keepout"], 1)
        self.assertEqual(summary["drc_edge"], 1)
        self.assertEqual(summary["drc_unconnected"], 1)
        self.assertEqual(summary["drc_parity"], 1)
        self.assertEqual(summary["erc_violations"], 1)
        self.assertEqual(summary["erc_excluded"], 1)
        # drc_violations counts all non-excluded DRC-* except unconnected/parity
        self.assertEqual(summary["drc_violations"], 4)

    def test_empty_findings(self):
        summary = kcr.summarize([])
        self.assertTrue(all(v == 0 for v in summary.values()))

    def test_drc_ran_defaults_false(self):
        summary = kcr.summarize(self.findings)
        self.assertIs(summary["drc_ran"], False)

    def test_drc_ran_reflects_argument(self):
        summary = kcr.summarize(self.findings, drc_ran=True)
        self.assertIs(summary["drc_ran"], True)
        # A clean-looking zero count alongside drc_ran=True is real ground
        # truth, not an artifact of DRC never having run.
        self.assertEqual(summary["drc_keepout"], 1)


# ---------------------------------------------------------------------------
# run_kicad_cli() — command construction via mocked subprocess.run
# ---------------------------------------------------------------------------

def _subprocess_stub(calls):
    """subprocess.run replacement: records every call, writes the ``-o`` target."""

    def _stub(command, cwd=None, capture_output=None, text=None,
              encoding=None, errors=None, timeout=None, check=None):
        calls.append({"command": list(command), "cwd": cwd, "timeout": timeout})
        if "-o" in command:
            target = Path(command[command.index("-o") + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}" if target.suffix == ".json" else "")
        return subprocess.CompletedProcess(command, 0, stdout="10.0.0-rc1\n", stderr="")

    return _stub


class TestRunKicadCliCommandConstruction(unittest.TestCase):
    def test_schematic_and_pcb_both_flags_present_never_save_board(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            # Project files live in a *different* directory than output_dir,
            # mirroring real usage (input lives in the project tree, reports
            # land in a separate output_dir) and exercising the resolved
            # absolute paths used in commands below.
            project_dir = Path(tmp) / "project"
            project_dir.mkdir()
            sch = project_dir / "proj.kicad_sch"
            pcb = project_dir / "board.kicad_pcb"
            output_dir = Path(tmp) / "out"
            with patch.object(kcr, "find_kicad_cli", return_value=["kicad-cli"]), \
                 patch.object(kcr.subprocess, "run", side_effect=_subprocess_stub(calls)):
                result = kcr.run_kicad_cli(sch, pcb, output_dir)

        resolved_output_dir = output_dir.resolve()
        commands = [c["command"] for c in calls]
        self.assertEqual(commands[0], ["kicad-cli", "version"])

        erc_cmd = next(c for c in commands if c[1:3] == ["sch", "erc"])
        self.assertEqual(
            erc_cmd,
            ["kicad-cli", "sch", "erc", "--format", "json", "--severity-all",
             "-o", str(resolved_output_dir / "erc.json"), str(sch.resolve())],
        )

        net_cmd = next(c for c in commands if c[1:4] == ["sch", "export", "netlist"])
        self.assertEqual(
            net_cmd,
            ["kicad-cli", "sch", "export", "netlist", "--format", "kicadsexpr",
             "-o", str(resolved_output_dir / "netlist.net"), str(sch.resolve())],
        )

        drc_cmd = next(c for c in commands if c[1:3] == ["pcb", "drc"])
        self.assertEqual(
            drc_cmd,
            ["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm",
             "--schematic-parity", "--refill-zones",
             "-o", str(resolved_output_dir / "drc.json"), str(pcb.resolve())],
        )

        for cmd in commands:
            self.assertNotIn("--save-board", cmd)

        # Every command runs with cwd=output_dir, never the project directory.
        for c in calls:
            self.assertEqual(c["cwd"], str(resolved_output_dir))

        self.assertEqual(result.erc_path, resolved_output_dir / "erc.json")
        self.assertEqual(result.drc_path, resolved_output_dir / "drc.json")
        self.assertEqual(result.netlist_path, resolved_output_dir / "netlist.net")
        self.assertEqual(result.version, "10.0.0-rc1")
        self.assertEqual(result.command_prefix, ["kicad-cli"])
        self.assertTrue(result.drc_ran)
        self.assertIs(result.summary["drc_ran"], True)

    def test_pcb_only_omits_schematic_parity_and_sch_commands(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(kcr, "find_kicad_cli", return_value=["kicad-cli"]), \
                 patch.object(kcr.subprocess, "run", side_effect=_subprocess_stub(calls)):
                kcr.run_kicad_cli(None, Path("board.kicad_pcb"), output_dir)

        commands = [c["command"] for c in calls]
        self.assertFalse(any(c[1:3] == ["sch", "erc"] for c in commands))
        self.assertFalse(any(c[1:4] == ["sch", "export", "netlist"] for c in commands))
        drc_cmd = next(c for c in commands if c[1:3] == ["pcb", "drc"])
        self.assertNotIn("--schematic-parity", drc_cmd)
        self.assertIn("--refill-zones", drc_cmd)

    def test_schematic_only_omits_drc(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(kcr, "find_kicad_cli", return_value=["kicad-cli"]), \
                 patch.object(kcr.subprocess, "run", side_effect=_subprocess_stub(calls)):
                result = kcr.run_kicad_cli(Path("proj.kicad_sch"), None, output_dir)

        commands = [c["command"] for c in calls]
        self.assertFalse(any(c[1:3] == ["pcb", "drc"] for c in commands))
        self.assertIsNone(result.drc_path)
        self.assertFalse(result.drc_ran)
        self.assertIs(result.summary["drc_ran"], False)

    def test_no_refill_zones_omits_flag(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(kcr, "find_kicad_cli", return_value=["kicad-cli"]), \
                 patch.object(kcr.subprocess, "run", side_effect=_subprocess_stub(calls)):
                kcr.run_kicad_cli(
                    None, Path("board.kicad_pcb"), output_dir, refill_zones=False,
                )
        drc_cmd = next(c["command"] for c in calls if c["command"][1:3] == ["pcb", "drc"])
        self.assertNotIn("--refill-zones", drc_cmd)

    def test_flatpak_command_prefix_expands_in_every_call(self):
        calls = []
        flatpak_prefix = ["flatpak", "run", "--command=kicad-cli", "org.kicad.KiCad"]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(kcr, "find_kicad_cli", return_value=flatpak_prefix), \
                 patch.object(kcr.subprocess, "run", side_effect=_subprocess_stub(calls)):
                kcr.run_kicad_cli(Path("proj.kicad_sch"), None, output_dir)
        for c in calls:
            self.assertEqual(c["command"][:4], flatpak_prefix)


class TestRunKicadCliMissingOrFailingCli(unittest.TestCase):
    def test_missing_cli_records_note_and_skips_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(kcr, "find_kicad_cli", return_value=None), \
                 patch.object(kcr.subprocess, "run") as mock_run:
                result = kcr.run_kicad_cli(Path("proj.kicad_sch"), Path("board.kicad_pcb"), output_dir)
        mock_run.assert_not_called()
        self.assertIsNone(result.command_prefix)
        self.assertEqual(len(result.notes), 1)
        self.assertIn("kicad-cli not found", result.notes[0])
        self.assertEqual(result.findings, [])
        # P1 regression: drc_ran must be an explicit False in ``summary`` even
        # on this early-exit path — never a bare {} a consumer would have to
        # distinguish from "key absent means unknown".
        self.assertIn("drc_ran", result.summary)
        self.assertIs(result.summary["drc_ran"], False)
        # Resolved paths must still be recorded even though kicad-cli itself
        # never ran, so build_payload() always has canonical paths to echo.
        self.assertEqual(result.resolved_schematic, Path("proj.kicad_sch").resolve())
        self.assertEqual(result.resolved_pcb, Path("board.kicad_pcb").resolve())
        self.assertEqual(result.resolved_output_dir, output_dir.resolve())

    def test_timeout_degrades_to_note_not_exception(self):
        def _raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="kicad-cli", timeout=1)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(kcr, "find_kicad_cli", return_value=["kicad-cli"]), \
                 patch.object(kcr.subprocess, "run", side_effect=_raise_timeout):
                result = kcr.run_kicad_cli(Path("proj.kicad_sch"), None, output_dir, timeout=1)
        self.assertIsNone(result.erc_path)
        self.assertTrue(any("timed out" in n for n in result.notes))

    def test_missing_output_file_records_failure_note(self):
        def _no_output(command, **kwargs):
            # Simulate kicad-cli running but not producing the -o target (e.g. bad input file).
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="parse error")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(kcr, "find_kicad_cli", return_value=["kicad-cli"]), \
                 patch.object(kcr.subprocess, "run", side_effect=_no_output):
                result = kcr.run_kicad_cli(Path("proj.kicad_sch"), None, output_dir)
        self.assertIsNone(result.erc_path)
        self.assertTrue(any("ERC failed: parse error" in n for n in result.notes))

    def test_failed_drc_leaves_drc_ran_false_not_clean(self):
        """P1 regression: a PCB-only run where kicad-cli fails to produce
        drc.json must report ``drc_ran=False`` — never counts that look like
        a clean 0-violation DRC pass."""
        def _no_output(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="drc crashed")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(kcr, "find_kicad_cli", return_value=["kicad-cli"]), \
                 patch.object(kcr.subprocess, "run", side_effect=_no_output):
                result = kcr.run_kicad_cli(None, Path("board.kicad_pcb"), output_dir)
        self.assertIsNone(result.drc_path)
        self.assertFalse(result.drc_ran)
        self.assertIs(result.summary["drc_ran"], False)
        self.assertEqual(result.summary["drc_keepout"], 0)  # zero count, but NOT clean evidence
        self.assertTrue(any("DRC failed: drc crashed" in n for n in result.notes))


# ---------------------------------------------------------------------------
# Path canonicalization — relative schematic/pcb/output_dir vs. cwd=output_dir
# ---------------------------------------------------------------------------

class TestRunKicadCliPathCanonicalization(unittest.TestCase):
    """P1 regression: every command below runs with ``cwd=output_dir``, so a
    relative ``schematic``/``pcb``/``output_dir`` supplied by the caller must
    be resolved against the *caller's* cwd before being embedded in a
    command — never left relative, or kicad-cli would look it up relative to
    ``output_dir`` instead and fail to find it."""

    def test_relative_schematic_and_output_dir_resolve_against_callers_cwd(self):
        calls = []
        orig_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            project_dir = tmp_path / "project"
            project_dir.mkdir()
            (project_dir / "proj.kicad_sch").write_text("(kicad_sch)")
            out_dir = tmp_path / "out"
            try:
                os.chdir(project_dir)
                with patch.object(kcr, "find_kicad_cli", return_value=["kicad-cli"]), \
                     patch.object(kcr.subprocess, "run", side_effect=_subprocess_stub(calls)):
                    result = kcr.run_kicad_cli(Path("proj.kicad_sch"), None, Path("../out"))
            finally:
                os.chdir(orig_cwd)

        expected_sch = project_dir / "proj.kicad_sch"

        erc_cmd = next(c["command"] for c in calls if c["command"][1:3] == ["sch", "erc"])
        # The literal relative string must never reach the command — only
        # the absolute path resolved against the caller's cwd (project_dir).
        self.assertNotIn("proj.kicad_sch", erc_cmd)
        self.assertIn(str(expected_sch), erc_cmd)

        # output_dir itself (also supplied relatively) must be resolved too:
        # every command's cwd is output_dir, which must be an absolute path.
        for c in calls:
            self.assertEqual(c["cwd"], str(out_dir))
            self.assertNotEqual(c["cwd"], "../out")

        self.assertEqual(result.erc_path, out_dir / "erc.json")


# ---------------------------------------------------------------------------
# Stale output target rejection — erc.json/drc.json left over from a prior run
# ---------------------------------------------------------------------------

class TestRunKicadCliStaleArtifactRejected(unittest.TestCase):
    """P1 regression: a stale erc.json/drc.json left behind by a previous run
    into the same output_dir must never be mistaken for this run's ground
    truth — not when this run's kicad-cli invocation fails outright, and not
    as a leftover mixed into a successful rerun's own fresh output."""

    def test_stale_erc_json_not_accepted_when_rerun_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            stale = output_dir / "erc.json"
            stale.write_text(json.dumps({
                "sheets": [{"path": "/stale", "violations": [
                    {"type": "stale_violation", "severity": "error", "description": "stale",
                     "items": [{"description": "Symbol STALE"}]},
                ]}],
            }))

            def _fail_without_writing(command, **kwargs):
                # Simulates kicad-cli crashing/failing on this run — it must
                # not touch the pre-existing erc.json either way.
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

            with patch.object(kcr, "find_kicad_cli", return_value=["kicad-cli"]), \
                 patch.object(kcr.subprocess, "run", side_effect=_fail_without_writing):
                result = kcr.run_kicad_cli(Path("proj.kicad_sch"), None, output_dir)

        self.assertIsNone(result.erc_path)
        self.assertEqual(result.findings, [])  # stale "stale_violation" must never surface
        self.assertTrue(any("ERC failed: boom" in n for n in result.notes))
        self.assertFalse(stale.exists())  # removed up front, not left for a later caller to misread

    def test_stale_target_removed_before_successful_rerun_writes_fresh_output(self):
        def _stub(command, **kwargs):
            if "-o" in command:
                target = Path(command[command.index("-o") + 1])
                # The stale file must already be gone by the time this run's
                # own kicad-cli invocation goes to write its output.
                self.assertFalse(target.exists())
                target.write_text(json.dumps({"sheets": []}))
            return subprocess.CompletedProcess(command, 0, stdout="10.0.0\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "erc.json").write_text(json.dumps({
                "sheets": [{"path": "/stale", "violations": [
                    {"type": "stale_violation", "severity": "error", "description": "stale", "items": []},
                ]}],
            }))
            with patch.object(kcr, "find_kicad_cli", return_value=["kicad-cli"]), \
                 patch.object(kcr.subprocess, "run", side_effect=_stub):
                result = kcr.run_kicad_cli(Path("proj.kicad_sch"), None, output_dir)

        self.assertIsNotNone(result.erc_path)
        self.assertEqual(result.findings, [])  # fresh {"sheets": []}, not the stale violation


# ---------------------------------------------------------------------------
# build_payload() — resolved canonical paths, not the caller's originals
# ---------------------------------------------------------------------------

class TestBuildPayloadResolvedPaths(unittest.TestCase):
    """P1 regression: build_payload() must echo the canonical (resolved)
    input/output paths run_kicad_cli() actually used, not whatever
    possibly-relative schematic/pcb/output_dir the top-level caller passed
    in separately — those two can disagree once run_kicad_cli() resolves
    internally against its own cwd."""

    def test_payload_prefers_result_resolved_paths_over_caller_originals(self):
        resolved_sch = Path("/abs/project/proj.kicad_sch")
        resolved_pcb = Path("/abs/project/board.kicad_pcb")
        resolved_out = Path("/abs/out")
        result = kcr.KicadCliResult(
            resolved_schematic=resolved_sch,
            resolved_pcb=resolved_pcb,
            resolved_output_dir=resolved_out,
        )
        # Simulate main()'s original (possibly relative / stale) arguments
        # disagreeing with what run_kicad_cli() actually resolved and used.
        payload = kcr.build_payload(
            result,
            schematic=Path("proj.kicad_sch"),
            pcb=Path("board.kicad_pcb"),
            output_dir=Path("out"),
        )
        self.assertEqual(payload["schematic"], str(resolved_sch))
        self.assertEqual(payload["pcb"], str(resolved_pcb))
        self.assertEqual(payload["output_dir"], str(resolved_out))

    def test_payload_falls_back_to_params_when_result_has_no_resolved_paths(self):
        # A KicadCliResult built directly (not via run_kicad_cli()) has no
        # resolved_* fields — build_payload() must still work, falling back
        # to whatever schematic/pcb/output_dir the caller passed.
        result = kcr.KicadCliResult()
        payload = kcr.build_payload(
            result,
            schematic=Path("/abs/proj.kicad_sch"),
            pcb=None,
            output_dir=Path("/abs/out"),
        )
        self.assertEqual(payload["schematic"], str(Path("/abs/proj.kicad_sch")))
        self.assertIsNone(payload["pcb"])
        self.assertEqual(payload["output_dir"], str(Path("/abs/out")))

    def test_summary_drc_ran_is_serialized_in_payload(self):
        result = kcr.KicadCliResult(
            resolved_output_dir=Path("/abs/out"),
            summary={"drc_ran": True, "drc_geometric": 0},
        )
        payload = kcr.build_payload(result, schematic=None, pcb=Path("/abs/board.kicad_pcb"),
                                     output_dir=Path("/abs/out"))
        self.assertIn("drc_ran", payload["summary"])
        self.assertIs(payload["summary"]["drc_ran"], True)


class TestMainEndToEndResolvedPaths(unittest.TestCase):
    """End-to-end: main() with a *relative* --schematic/--output-dir must
    print the resolved canonical paths in the JSON payload, matching what
    run_kicad_cli() actually pointed kicad-cli at — never the raw relative
    strings the user typed on the command line."""

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kcr.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_relative_cli_args_resolve_in_printed_payload(self):
        orig_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            project_dir = tmp_path / "project"
            project_dir.mkdir()
            sch = project_dir / "proj.kicad_sch"
            sch.write_text("(kicad_sch)")
            try:
                os.chdir(project_dir)
                with patch.object(kcr, "find_kicad_cli", return_value=None):
                    code, out, err = self._run([
                        "--schematic", "proj.kicad_sch",
                        "--output-dir", "out",
                    ])
            finally:
                os.chdir(orig_cwd)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["schematic"], str(sch.resolve()))
        self.assertEqual(payload["output_dir"], str((project_dir / "out").resolve()))
        self.assertIn("drc_ran", payload["summary"])
        self.assertIs(payload["summary"]["drc_ran"], False)


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------

class TestMainCli(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kcr.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_requires_schematic_or_pcb(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._run(["--output-dir", tmp])
        self.assertEqual(code, 1)
        self.assertIn("provide --schematic and/or --pcb", err)

    def test_missing_schematic_file_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._run([
                "--schematic", str(Path(tmp) / "nope.kicad_sch"),
                "--output-dir", str(Path(tmp) / "out"),
            ])
        self.assertEqual(code, 1)
        self.assertIn("schematic not found", err)

    def test_missing_pcb_file_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._run([
                "--pcb", str(Path(tmp) / "nope.kicad_pcb"),
                "--output-dir", str(Path(tmp) / "out"),
            ])
        self.assertEqual(code, 1)
        self.assertIn("pcb not found", err)

    def test_json_to_stdout_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "proj.kicad_sch"
            sch.write_text("(kicad_sch)")
            out_dir = Path(tmp) / "out"
            fake_result = kcr.KicadCliResult(
                command_prefix=["kicad-cli"], version="10.0.0",
                findings=[{"rule_id": "ERC-x", "severity": "warning"}],
                summary={"erc_violations": 1}, notes=[],
            )
            with patch.object(kcr, "run_kicad_cli", return_value=fake_result) as mock_run:
                code, out, err = self._run([
                    "--schematic", str(sch), "--output-dir", str(out_dir),
                ])
            self.assertEqual(code, 0)
            self.assertTrue(out_dir.is_dir())
            payload = json.loads(out)
            self.assertEqual(payload["schema"], "kicad_cli_review/1")
            self.assertEqual(payload["schematic"], str(sch))
            self.assertIsNone(payload["pcb"])
            self.assertEqual(payload["kicad_cli"]["version"], "10.0.0")
            self.assertEqual(payload["summary"], {"erc_violations": 1})
            self.assertEqual(len(payload["findings"]), 1)
            mock_run.assert_called_once()
            called_args = mock_run.call_args.args
            self.assertEqual(called_args[0], sch)
            self.assertIsNone(called_args[1])
            self.assertEqual(called_args[2], out_dir)

    def test_output_file_writes_and_stays_silent_on_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            pcb = Path(tmp) / "board.kicad_pcb"
            pcb.write_text("(kicad_pcb)")
            out_dir = Path(tmp) / "out"
            result_path = Path(tmp) / "result.json"
            fake_result = kcr.KicadCliResult()
            with patch.object(kcr, "run_kicad_cli", return_value=fake_result):
                code, out, err = self._run([
                    "--pcb", str(pcb), "--output-dir", str(out_dir),
                    "--output", str(result_path),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(out, "")
            self.assertIn(f"Written to {result_path}", err)
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pcb"], str(pcb))

    def test_compact_output_has_no_indentation(self):
        with tempfile.TemporaryDirectory() as tmp:
            pcb = Path(tmp) / "board.kicad_pcb"
            pcb.write_text("(kicad_pcb)")
            out_dir = Path(tmp) / "out"
            with patch.object(kcr, "run_kicad_cli", return_value=kcr.KicadCliResult()):
                code, out, err = self._run([
                    "--pcb", str(pcb), "--output-dir", str(out_dir), "--compact",
                ])
        self.assertEqual(code, 0)
        self.assertNotIn("\n  ", out)

    def test_no_refill_zones_flag_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            pcb = Path(tmp) / "board.kicad_pcb"
            pcb.write_text("(kicad_pcb)")
            out_dir = Path(tmp) / "out"
            with patch.object(kcr, "run_kicad_cli", return_value=kcr.KicadCliResult()) as mock_run:
                self._run([
                    "--pcb", str(pcb), "--output-dir", str(out_dir), "--no-refill-zones",
                ])
        self.assertFalse(mock_run.call_args.kwargs["refill_zones"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
