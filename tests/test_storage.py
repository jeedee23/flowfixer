# File: tests/test_storage.py
# Purpose: Persistence, immutability, locking and recovery integration tests.
# Version: 0.2.2 | Created: 2026-10-25 | Modified: 2026-10-29
# Changelog: 0.2.2 - Verify legacy plunge compatibility and rejected-run history.
# 0.2.0 - Verify revision lifecycle, failures, exports and rebuilds.

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowfix.models import FlowFixSettings
from flowfix.storage import StorageError, Workspace, atomic_json, sha256_file


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "original.gcode"
        self.data = b";LAYER_COUNT:10\r\n;LAYER:5\r\n;\xff\r\nG1 Y0.1"
        self.source.write_bytes(self.data)
        self.workspace = Workspace(self.root / "workspace")
        self.original_id = self.workspace.import_original(self.source)["original_id"]

    def run_revision(self, **settings):
        return self.workspace.process(self.original_id, FlowFixSettings(**settings))

    def test_duplicate_runs_restart_and_original_integrity(self):
        first = self.run_revision()
        second = self.run_revision()
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertNotEqual(first["output_revision_id"], second["output_revision_id"])
        self.assertEqual(first["output_sha256"], second["output_sha256"])
        self.assertEqual(second["changes_from_previous_settings"], {})
        restarted = Workspace(self.workspace.root)
        self.assertEqual(len(restarted.history(self.original_id)), 3)
        self.assertEqual(self.source.read_bytes(), self.data)
        stored = restarted.original_dir(self.original_id) / "source.gcode"
        self.assertEqual(stored.read_bytes(), self.data)
        self.assertEqual(sha256_file(stored), self.original_id)

    def test_same_content_moved_and_changed_original(self):
        moved = self.root / "moved.gcode"
        moved.write_bytes(self.data)
        self.assertEqual(
            self.workspace.import_original(moved)["original_id"], self.original_id
        )
        metadata = json.loads(
            (self.workspace.original_dir(self.original_id) / "metadata.json").read_text()
        )
        self.assertEqual(len(metadata["observed_source_paths"]), 2)
        moved.write_bytes(b"different")
        other = self.workspace.import_original(moved)["original_id"]
        self.assertNotEqual(other, self.original_id)
        self.assertEqual(len(self.workspace.history(other)), 1)
        self.assertEqual(len(self.workspace.history(self.original_id)), 2)

    def test_settings_restore_compare_and_export(self):
        first = self.run_revision()
        second = self.run_revision(low_flow_percent=60)
        comparison = self.workspace.compare(
            self.original_id, first["output_revision_id"], second["output_revision_id"]
        )
        self.assertEqual(comparison["settings_diff"]["low_flow_percent"], {
            "before": 50, "after": 60,
        })
        self.assertFalse(comparison["identical_bytes"])
        self.assertGreater(comparison["different_output_lines"], 0)
        settings = self.workspace.restore_settings(self.original_id, first["output_revision_id"])
        third = self.workspace.process(self.original_id, settings)
        self.assertEqual(first["output_sha256"], third["output_sha256"])
        destination = self.root / "export.gcode"
        self.workspace.export(self.original_id, third["output_revision_id"], destination)
        self.assertEqual(sha256_file(destination), third["output_sha256"])
        with self.assertRaises(FileExistsError):
            self.workspace.export(self.original_id, third["output_revision_id"], destination)
        with self.assertRaises(ValueError):
            self.workspace.export(
                self.original_id, third["output_revision_id"], self.workspace.root / "bad.gcode"
            )

    def test_failure_recorded_no_success_revision_and_prior_survives(self):
        first = self.run_revision()
        path = self.workspace.revision_path(self.original_id, first["output_revision_id"])
        before = path.read_bytes()
        with patch("flowfix.storage.transform_file", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.run_revision()
        history = self.workspace.history(self.original_id)
        self.assertEqual(history[-1]["type"], "error")
        self.assertEqual(history[-1]["status"], "error")
        self.assertEqual(sum(e["type"] == "process" for e in history), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(path.parent.glob(".process-*")))

    def test_invalid_settings_recorded(self):
        with self.assertRaises(ValueError):
            self.run_revision(threshold_y=float("nan"))
        self.assertEqual(self.workspace.history(self.original_id)[-1]["type"], "error")

    def test_corrupt_source_cannot_process(self):
        (self.workspace.original_dir(self.original_id) / "source.gcode").write_bytes(b"tampered")
        with self.assertRaisesRegex(StorageError, "verification"):
            self.run_revision()
        self.assertEqual(self.workspace.history(self.original_id)[-1]["type"], "error")
        self.assertTrue(self.workspace.rebuild()["issues"])

    def test_corrupt_revision_cannot_export_or_compare(self):
        run = self.run_revision()
        path = self.workspace.revision_path(self.original_id, run["output_revision_id"])
        path.write_bytes(b"tampered")
        with self.assertRaises(StorageError):
            self.workspace.export(self.original_id, run["output_revision_id"], self.root / "copy")
        with self.assertRaises(StorageError):
            self.workspace.compare(self.original_id, run["output_revision_id"], run["output_revision_id"])
        self.assertTrue(self.workspace.rebuild()["issues"])

    def test_rebuild_derived_files_and_interrupted_attempt(self):
        run = self.run_revision()
        directory = self.workspace.original_dir(self.original_id)
        for path in (directory / "index.json", directory / "metadata.json", self.workspace.root / "catalog.json"):
            path.write_text("broken")
        revision = "rev-" + "a" * 32
        orphan = directory / "revisions" / f"{revision}.gcode"
        orphan.write_bytes(b"unfinished output")
        pending = directory / "pending" / f"{revision}.json"
        atomic_json(pending, {
            "schema_version": 1, "original_id": self.original_id,
            "revision_id": revision, "settings": FlowFixSettings().to_dict(),
        })
        report = self.workspace.rebuild()
        self.assertEqual(report["rebuilt_originals"], [self.original_id])
        self.assertTrue(any("uncommitted" in issue for issue in report["issues"]))
        self.assertTrue(orphan.exists())
        self.assertFalse(pending.exists())
        self.assertEqual(self.workspace.history(self.original_id)[-1]["type"], "error")
        self.assertTrue(self.workspace.revision_path(self.original_id, run["output_revision_id"]).exists())
        before = len(self.workspace.history(self.original_id))
        self.workspace.rebuild()
        self.assertEqual(len(self.workspace.history(self.original_id)), before)

    def test_event_corruption_is_not_silently_ignored(self):
        path = next((self.workspace.original_dir(self.original_id) / "events").glob("*.json"))
        original = path.read_bytes()
        path.write_text("{broken")
        with self.assertRaises(StorageError):
            self.workspace.history(self.original_id)
        self.assertTrue(self.workspace.rebuild()["issues"])
        self.assertEqual(path.read_text(), "{broken")
        path.write_bytes(original)
        self.assertFalse(self.workspace.rebuild()["issues"])

    def test_workspace_lock_prevents_competing_writers(self):
        competitor = Workspace(self.workspace.root)
        with self.workspace.locked():
            with self.assertRaisesRegex(StorageError, "busy"):
                competitor.process(self.original_id, FlowFixSettings())
        self.run_revision()

    def test_identity_validation_and_immutable_json(self):
        with self.assertRaises(ValueError):
            self.workspace.history("../escape")
        with self.assertRaises(ValueError):
            self.workspace.revision_path(self.original_id, "../escape")
        path = self.root / "event.json"
        atomic_json(path, {"schema_version": 1}, immutable=True)
        with self.assertRaises(FileExistsError):
            atomic_json(path, {"schema_version": 2}, immutable=True)
        self.assertEqual(json.loads(path.read_text())["schema_version"], 1)

    def test_library_and_catalog_keep_healthy_originals_available(self):
        directory = self.workspace.original_dir(self.original_id)
        next((directory / "events").glob("*.json")).write_text("broken")
        other = self.root / "healthy.gcode"
        other.write_bytes(b";healthy\n")
        healthy_id = self.workspace.import_original(other)["original_id"]
        library = self.workspace.list_originals()
        self.assertEqual([item["original_id"] for item in library["originals"]], [healthy_id])
        self.assertTrue(library["issues"])
        catalog = json.loads((self.workspace.root / "catalog.json").read_text())
        self.assertTrue(catalog["issues"])
        self.assertEqual(self.workspace.open_original(healthy_id)["original_id"], healthy_id)

    def test_commit_then_directory_sync_failure_is_reported_as_committed(self):
        from flowfix.storage import sync_directory

        def fail_event_sync(path):
            if path.name == "events":
                raise OSError("event directory fsync failed")
            sync_directory(path)

        with patch("flowfix.storage.sync_directory", side_effect=fail_event_sync):
            with self.assertRaisesRegex(StorageError, "was committed"):
                self.run_revision()
        self.assertEqual(self.workspace.history(self.original_id)[-1]["type"], "process")
        self.assertFalse(self.workspace.rebuild()["issues"])

    def test_publication_before_event_failure_retains_orphan_not_success(self):
        append = self.workspace._append

        def fail_process_event(original_id, event_type, **data):
            if event_type == "process":
                raise OSError("event publication failed")
            return append(original_id, event_type, **data)

        with patch.object(self.workspace, "_append", side_effect=fail_process_event):
            with self.assertRaisesRegex(OSError, "event publication"):
                self.run_revision()
        history = self.workspace.history(self.original_id)
        self.assertEqual(history[-1]["type"], "error")
        self.assertFalse(any(event["type"] == "process" for event in history))
        outputs = list((self.workspace.original_dir(self.original_id) / "revisions").glob("*.gcode"))
        self.assertEqual(len(outputs), 1)
        self.assertTrue(self.workspace.rebuild()["issues"])
        self.assertTrue(outputs[0].exists())

    def test_committed_revision_survives_index_write_failure(self):
        from flowfix.storage import atomic_json as write_json

        def fail_index(path, value, **options):
            if path.name == "index.json":
                raise OSError("index write failed")
            return write_json(path, value, **options)

        with patch("flowfix.storage.atomic_json", side_effect=fail_index):
            with self.assertRaisesRegex(StorageError, "was committed"):
                self.run_revision()
        history = self.workspace.history(self.original_id)
        self.assertEqual(history[-1]["type"], "process")
        self.assertFalse(self.workspace.rebuild()["issues"])
        self.assertTrue(self.workspace.revision_path(
            self.original_id, history[-1]["output_revision_id"]
        ).exists())

    def test_failed_export_and_settings_restore_have_error_events(self):
        run = self.run_revision()
        with self.assertRaises(FileExistsError):
            self.workspace.export(self.original_id, run["output_revision_id"], self.source)
        event = self.workspace.history(self.original_id)[-1]
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["metadata"]["operation"], "export")
        with self.assertRaises(StorageError):
            self.workspace.restore_settings(self.original_id, "rev-" + "f" * 32)
        event = self.workspace.history(self.original_id)[-1]
        self.assertEqual(event["metadata"]["operation"], "restore_settings")
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_pre_plunge_history_still_loads_restores_and_rebuilds(self):
        run = self.run_revision(plunge_into_belt_mm=0)
        directory = self.workspace.original_dir(self.original_id)
        path = next(p for p in (directory / "events").glob("*.json")
                    if run["event_id"] in p.name)
        legacy_event = json.loads(path.read_text())
        del legacy_event["settings"]["plunge_into_belt_mm"]
        del legacy_event["results"]["inserted_belt_plunges"]
        path.write_text(json.dumps(legacy_event))
        original_bytes = path.read_bytes()
        restarted = Workspace(self.workspace.root)
        self.assertEqual(len(restarted.history(self.original_id)), 2)
        restored = restarted.restore_settings(self.original_id, run["output_revision_id"])
        self.assertEqual(restored.plunge_into_belt_mm, 0)
        repeated = restarted.process(self.original_id, restored)
        self.assertEqual(repeated["output_sha256"], run["output_sha256"])
        self.assertFalse(restarted.rebuild()["issues"])
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_duplicate_plunge_fails_without_publishing_revision(self):
        self.source.write_bytes(b"G90\nG21\nG28\nG0 Y-0.7\nG92 Y0\n;LAYER:0\n")
        original_id = self.workspace.import_original(self.source)["original_id"]
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "already contains"):
            self.workspace.process(original_id, FlowFixSettings())
        event = self.workspace.history(original_id)[-1]
        self.assertEqual(event["status"], "error")
        self.assertEqual(event["type"], "error")
        self.assertEqual(list(
            (self.workspace.original_dir(original_id) / "revisions").glob("*.gcode")
        ), [])
        self.assertEqual(self.source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
