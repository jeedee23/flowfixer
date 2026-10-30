# File: flowfixer_updated.py
# Purpose: Cross-platform FlowFix desktop application entry point.
# Version: 0.2.2 | Created: unknown (baseline predates handoff) | Modified: 2026-10-29
# Changelog: 0.2.2 - Remove stray character preventing application startup.
# 0.2.0 - Extract core/GUI/storage; add workspace and verification options.

import argparse
import json
import os
from pathlib import Path
import sys

from flowfix import __version__
from flowfix.core import extract_y_value, flowfix_file, layer_restore_flow, matching_between_flow
from flowfix.models import BetweenRange, FlowFixSettings
from flowfix.storage import DEFAULT_WORKSPACE, Workspace


def main() -> int:
    parser = argparse.ArgumentParser(description="FlowFix G-code revision editor")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--workspace", type=Path,
        default=Path(os.environ.get("FLOWFIX_WORKSPACE", str(DEFAULT_WORKSPACE))),
        help="Persistent data directory (default: workspace beside this script)",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Verify artifacts/rebuild indexes without opening the GUI; report JSON",
    )
    arguments = parser.parse_args()
    try:
        workspace = Workspace(arguments.workspace)
        if arguments.verify:
            report = workspace.rebuild()
            print(json.dumps(report, indent=2))
            return 1 if report["issues"] else 0
        try:
            import tkinter as tk
            from flowfix.gui import App
        except ImportError as exc:
            print(f"FlowFix requires Tkinter for the GUI: {exc}", file=sys.stderr)
            return 1
        try:
            app = App(workspace)
            app.mainloop()
        except tk.TclError as exc:
            print(f"FlowFix needs a graphical Tk desktop: {exc}", file=sys.stderr)
            return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"FlowFix could not start: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
