# File: tests/test_cli.py
# Purpose: Verify portable launch options and headless recovery exit status.
# Version: 0.2.2 | Created: 2026-10-25 | Modified: 2026-10-29
# Changelog: 0.2.2 - Check current application version.
# 0.2.0 - Exercise CLI from an unrelated working directory.

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowfix.models import FlowFixSettings
from flowfix import __version__
from flowfix.storage import Workspace

SCRIPT = Path(__file__).resolve().parents[1] / "flowfixer_updated.py"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def invoke(self, *arguments, **environment):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments], cwd=self.root,
            env={**os.environ, **environment}, capture_output=True, text=True, timeout=10,
        )

    def test_version_without_workspace_creation(self):
        result = self.invoke("--version", FLOWFIX_WORKSPACE=str(self.root / "not-created"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), __version__)
        self.assertFalse((self.root / "not-created").exists())

    def test_workspace_option_overrides_environment_and_needs_no_display(self):
        environment_workspace = self.root / "environment"
        selected = self.root / "selected"
        result = self.invoke(
            "--workspace", str(selected), "--verify", DISPLAY="",
            FLOWFIX_WORKSPACE=str(environment_workspace),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["issues"], [])
        self.assertTrue((selected / "catalog.json").exists())
        self.assertFalse(environment_workspace.exists())

    def test_verify_returns_failure_for_corrupt_revision_without_deletion(self):
        source = self.root / "original.gcode"
        source.write_bytes(b"G1 Y0\n")
        workspace = Workspace(self.root / "workspace")
        original = workspace.import_original(source)["original_id"]
        run = workspace.process(original, FlowFixSettings())
        revision = workspace.revision_path(original, run["output_revision_id"])
        revision.write_bytes(b"damaged")
        result = self.invoke("--verify", FLOWFIX_WORKSPACE=str(workspace.root))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(json.loads(result.stdout)["issues"])
        self.assertEqual(revision.read_bytes(), b"damaged")


if __name__ == "__main__":
    unittest.main()
