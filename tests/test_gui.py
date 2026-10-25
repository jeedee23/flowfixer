# File: tests/test_gui.py
# Purpose: Real-Tk smoke tests for asynchronous revision workflows.
# Version: 0.2.0 | Created: 2026-10-25 | Modified: 2026-10-25
# Changelog: 0.2.0 - Exercise range editing, history, export and worker failures.

from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from flowfix.gui import App
from flowfix.storage import Workspace


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "input.gcode"
        self.source.write_bytes(b";LAYER_COUNT:10\n;LAYER:5\nG1 Y0.1\nG1 Y1\n")
        self.workspace = Workspace(self.root / "workspace")
        self.errors = []
        for name in ("showerror", "showwarning", "showinfo"):
            mock = patch(f"flowfix.gui.messagebox.{name}")
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)
        try:
            self.app = App(self.workspace)
        except tk.TclError as exc:
            self.skipTest(f"Graphical Tk unavailable: {exc}")
        self.app.withdraw()
        self.app.report_callback_exception = lambda _kind, error, _trace: self.errors.append(error)
        self.addCleanup(self.close_app)
        self.wait()

    def close_app(self):
        if self.app.winfo_exists():
            self.wait()
            self.app._close()

    def wait(self):
        deadline = time.monotonic() + 10
        while self.app.job is not None:
            self.app.update()
            if time.monotonic() > deadline:
                self.fail("GUI operation timed out")
            time.sleep(0.005)
        self.app.update()
        self.assertEqual(self.errors, [])

    def import_source(self):
        with patch("flowfix.gui.filedialog.askopenfilename", return_value=str(self.source)):
            self.app._browse()
        self.wait()
        self.assertIsNotNone(self.app.original_id)
        self.assertEqual(len(self.app.events), 1)

    def test_import_process_restore_compare_export_preview_and_reopen(self):
        self.import_source()
        original_id = self.app.original_id
        self.app._run()
        self.wait()
        first = self.app.revision_tree.get_children()[0]
        self.app.setting_vars["low_flow_percent"].set("60")
        self.app._run()
        self.wait()
        revisions = self.app.revision_tree.get_children()
        self.assertEqual(len(revisions), 2)
        self.app.revision_tree.selection_set(revisions)
        self.app._compare()
        self.wait()
        self.assertIn('"before": 50', self.app.details.get("1.0", "end"))
        self.app.revision_tree.selection_set(first)
        self.app._restore()
        self.wait()
        self.assertEqual(self.app.setting_vars["low_flow_percent"].get(), "50")
        self.assertEqual(self.app.events[-1]["type"], "restore_settings")
        self.app.revision_tree.selection_set(first)
        self.app._view_revision()
        self.wait()
        self.assertIn("M221 S50", self.app.details.get("1.0", "end"))
        destination = self.root / "export.gcode"
        with patch("flowfix.gui.filedialog.asksaveasfilename", return_value=str(destination)):
            self.app._export()
        self.wait()
        self.assertTrue(destination.exists())
        self.assertEqual(self.app.events[-1]["type"], "export")
        self.source.unlink()
        self.app.library_combo.current(0)
        self.app._open_stored()
        self.wait()
        self.assertEqual(self.app.original_id, original_id)
        self.assertEqual(len(self.app.revision_tree.get_children()), 2)
        self.showerror.assert_not_called()

    def test_range_editor_rejects_overlap_and_preserves_actions(self):
        for variable, value in zip(self.app.range_vars, ("1", "20", "70")):
            variable.set(value)
        self.app._add_between_range()
        self.assertEqual(len(self.app.range_tree.get_children()), 1)
        for variable, value in zip(self.app.range_vars, ("20", "30", "80")):
            variable.set(value)
        self.app._add_between_range()
        self.showerror.assert_called_once()
        self.assertEqual(len(self.app.range_tree.get_children()), 1)
        self.app.range_vars[0].set("21")
        self.app._add_between_range()
        rows = self.app.range_tree.get_children()
        self.assertEqual(len(rows), 2)
        self.app.range_tree.selection_set(rows[1])
        self.app._load_selected_between_range()
        self.assertEqual(self.app.range_vars[0].get(), "21")
        self.app.range_vars[2].set("85")
        self.app._update_selected_between_range()
        self.assertEqual(self.app._collect_between_ranges()[1].flow_between, 85)
        self.app.range_tree.selection_set(self.app.range_tree.get_children()[0])
        self.app._remove_selected_between_range()
        self.app._sort_between_ranges()
        self.assertEqual(len(self.app._collect_between_ranges()), 1)

    def test_switch_original_clears_history_and_drafts(self):
        self.import_source()
        first_id = self.app.original_id
        self.app._run()
        self.wait()
        self.source.write_bytes(b";other original\n")
        self.import_source()
        self.assertNotEqual(self.app.original_id, first_id)
        self.assertFalse(self.app.revision_tree.get_children())
        self.assertEqual(self.app.setting_vars["low_flow_percent"].get(), "50")
        self.assertIn("No compatible", self.app.warning_var.get())

    def test_worker_failure_is_visible_and_recorded(self):
        self.import_source()
        with patch("flowfix.storage.transform_file", side_effect=OSError("test write failure")):
            self.app._run()
            self.wait()
        self.showerror.assert_called_once()
        self.assertEqual(self.app.events[-1]["type"], "error")
        self.assertFalse(self.app.revision_tree.get_children())

    def test_corrupt_history_error_does_not_retry_forever(self):
        self.import_source()
        directory = self.workspace.original_dir(self.app.original_id)
        next((directory / "events").glob("*.json")).write_text("broken")
        self.app._refresh()
        self.wait()
        self.showerror.assert_called_once()

    def test_worker_does_not_block_tk_event_loop(self):
        gate = threading.Event()
        thread_ids = []
        main_thread = threading.get_ident()

        def work():
            thread_ids.append(threading.get_ident())
            if not gate.wait(3):
                raise RuntimeError("Tk heartbeat did not run")
            return "done"

        self.app._submit(work, lambda _value: None, "Testing worker")
        self.app.after(10, gate.set)
        self.wait()
        self.assertNotEqual(thread_ids, [main_thread])
        self.showerror.assert_not_called()


if __name__ == "__main__":
    unittest.main()
