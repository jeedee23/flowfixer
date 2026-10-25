# File: flowfix/storage.py
# Purpose: Locked, append-only JSON history and immutable G-code revisions.
# Version: 0.2.0 | Created: 2026-10-25 | Modified: 2026-10-25
# Changelog: 0.2.0 - Add content identity, atomic publication, export and recovery.

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Iterator
from uuid import uuid4

from . import __version__
from .core import binary_lines, scan_file, transform_file
from .models import FlowFixSettings, settings_diff

SCHEMA_VERSION = 1
DEFAULT_WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
DIGEST = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"rev-[0-9a-f]{32}")
EVENT_TYPES = {
    "import_original", "process", "error", "export", "restore_settings",
}


class StorageError(RuntimeError):
    pass


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise StorageError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise StorageError(f"Invalid or unsupported schema in {path}.")
    return value


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_json(path: Path, value: dict[str, Any], *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        publish(temporary, path, immutable=immutable)
    finally:
        temporary.unlink(missing_ok=True)


def publish(temporary: Path, target: Path, *, immutable: bool = True) -> None:
    if immutable:
        # Unlike replace(), link() cannot overwrite an existing immutable artifact.
        os.link(temporary, target)
    else:
        os.replace(temporary, target)
    sync_directory(target.parent)


class Workspace:
    def __init__(self, root: Path = DEFAULT_WORKSPACE):
        self.root = root.expanduser().resolve()
        self.originals = self.root / "originals"
        self.originals.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self) -> Iterator[None]:
        lock_path = self.root / ".lock"
        with lock_path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StorageError(
                    "Workspace is busy or cannot be locked. Retry after the other "
                    f"operation finishes: {exc}"
                ) from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def original_dir(self, original_id: str) -> Path:
        if not isinstance(original_id, str) or not DIGEST.fullmatch(original_id):
            raise ValueError("Invalid original SHA-256 identity.")
        return self.originals / original_id

    def _revision_path(self, original_id: str, revision_id: str) -> Path:
        if not isinstance(revision_id, str) or not REVISION.fullmatch(revision_id):
            raise ValueError("Invalid revision identity.")
        return self.original_dir(original_id) / "revisions" / f"{revision_id}.gcode"

    def _events(self, original_id: str) -> list[dict[str, Any]]:
        directory = self.original_dir(original_id)
        if not (directory / "source.gcode").is_file():
            raise StorageError(f"Original source missing: {directory}")
        result = []
        identities = set()
        for path in sorted((directory / "events").glob("*.json")):
            event = read_json(path)
            sequence = event.get("sequence")
            event_id = event.get("event_id")
            if (
                type(sequence) is not int or sequence != len(result) + 1
                or not isinstance(event_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", event_id)
                or event_id in identities
                or path.name != f"{sequence:08d}_{event_id}.json"
                or event.get("original_id") != original_id
                or event.get("type") not in EVENT_TYPES
                or event.get("status") not in {"success", "error"}
                or not isinstance(event.get("metadata"), dict)
            ):
                raise StorageError(f"Invalid event identity, sequence or fields: {path}")
            try:
                date = datetime.fromisoformat(event["timestamp"])
                if date.utcoffset() is None:
                    raise ValueError("Timestamp lacks UTC offset.")
                if event["type"] in {"process", "restore_settings"}:
                    FlowFixSettings.from_dict(event["settings"])
                if event["type"] == "process":
                    self._validate_process(original_id, event)
                if event["type"] == "import_original":
                    metadata = event["metadata"]
                    if (
                        not isinstance(metadata.get("source_path"), str)
                        or not isinstance(metadata.get("name"), str)
                        or type(metadata.get("size_bytes")) is not int
                    ):
                        raise ValueError("Invalid import metadata.")
            except (KeyError, TypeError, ValueError) as exc:
                raise StorageError(f"Invalid event data in {path}: {exc}") from exc
            identities.add(event_id)
            result.append(event)
        return result

    def _validate_process(self, original_id: str, event: dict[str, Any]) -> None:
        revision_id = event.get("output_revision_id")
        self._revision_path(original_id, revision_id)
        results = event.get("results")
        if (
            event["status"] != "success"
            or event.get("input_sha256") != original_id
            or event.get("source_revision_id") is not None
            or not isinstance(event.get("output_sha256"), str)
            or not DIGEST.fullmatch(event["output_sha256"])
            or not isinstance(results, dict)
        ):
            raise ValueError("Invalid process result.")
        for key in ("size_bytes", "inserted_low_m221", "inserted_restore_m221"):
            if type(results.get(key)) is not int or results[key] < 0:
                raise ValueError(f"Invalid result counter: {key}")
        if results.get("output_relative_path") != f"revisions/{revision_id}.gcode":
            raise ValueError("Invalid relative revision path.")

    def history(self, original_id: str) -> list[dict[str, Any]]:
        with self.locked():
            return self._events(original_id)

    def _catalog_snapshot(self) -> dict[str, Any]:
        entries = {}
        issues = []
        for directory in sorted(self.originals.iterdir()):
            if directory.is_dir() and DIGEST.fullmatch(directory.name):
                try:
                    entries[directory.name] = self._metadata(
                        directory.name, self._events(directory.name)
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    issues.append(f"{directory.name}: {exc}")
        return {"schema_version": SCHEMA_VERSION, "originals": entries, "issues": issues}

    def list_originals(self) -> dict[str, Any]:
        with self.locked():
            snapshot = self._catalog_snapshot()
            return {"originals": list(snapshot["originals"].values()), "issues": snapshot["issues"]}

    def open_original(self, original_id: str) -> dict[str, Any]:
        with self.locked():
            source = self._verified_source(original_id)
            events = self._events(original_id)
            scan = scan_file(source)
            return {
                **self._metadata(original_id, events),
                "total_layers_detected": scan.total_layers,
                "warnings": scan.warnings,
            }

    def _append(
        self, original_id: str, event_type: str, *, status: str = "success", **data: Any
    ) -> dict[str, Any]:
        events = self._events(original_id)
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": uuid4().hex,
            "sequence": len(events) + 1,
            "timestamp": timestamp(),
            "type": event_type,
            "status": status,
            "original_id": original_id,
            "application_version": __version__,
            "metadata": {},
            **data,
        }
        path = self.original_dir(original_id) / "events" / (
            f"{event['sequence']:08d}_{event['event_id']}.json"
        )
        atomic_json(path, event, immutable=True)
        return event

    def _metadata(self, original_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        imports = [e for e in events if e["type"] == "import_original"]
        if not imports:
            raise StorageError(
                f"Original {original_id} has no committed import event; preserve "
                "its source and re-import the original file."
            )
        latest = imports[-1]["metadata"]
        return {
            "schema_version": SCHEMA_VERSION,
            "original_id": original_id,
            "name": latest["name"],
            "size_bytes": latest["size_bytes"],
            "observed_source_paths": sorted({e["metadata"]["source_path"] for e in imports}),
            "created_at": imports[0]["timestamp"],
        }

    def _update_index(self, original_id: str) -> dict[str, Any]:
        events = self._events(original_id)
        directory = self.original_dir(original_id)
        metadata = self._metadata(original_id, events)
        processes = [e for e in events if e["type"] == "process"]
        known = {f"{e['output_revision_id']}.gcode" for e in processes}
        index = {
            "schema_version": SCHEMA_VERSION,
            "original_id": original_id,
            "event_count": len(events),
            "revision_ids": [e["output_revision_id"] for e in processes],
            "orphan_revision_files": sorted(
                p.name for p in (directory / "revisions").glob("*.gcode") if p.name not in known
            ),
            "pending_runs": sorted(p.name for p in (directory / "pending").glob("*.json")),
        }
        atomic_json(directory / "metadata.json", metadata)
        atomic_json(directory / "index.json", index)
        return index

    def _update_catalog(self) -> None:
        atomic_json(self.root / "catalog.json", self._catalog_snapshot())

    def import_original(self, source: Path) -> dict[str, Any]:
        source = source.expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"Select a regular G-code file: {source}")
        with self.locked():
            descriptor, name = tempfile.mkstemp(prefix=".import-", dir=self.root)
            temporary = Path(name)
            try:
                digest = hashlib.sha256()
                size = 0
                with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as output:
                    while chunk := incoming.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                original_id = digest.hexdigest()
                directory = self.original_dir(original_id)
                directory.mkdir(exist_ok=True)
                for child in ("events", "revisions", "pending"):
                    (directory / child).mkdir(exist_ok=True)
                stored = directory / "source.gcode"
                if stored.exists():
                    if sha256_file(stored) != original_id:
                        raise StorageError(f"Stored original failed integrity check: {stored}")
                else:
                    publish(temporary, stored)
                self._append(original_id, "import_original", metadata={
                    "source_path": str(source), "name": source.name, "size_bytes": size,
                })
                self._update_index(original_id)
                self._update_catalog()
                scan = scan_file(stored)
                return {
                    **self._metadata(original_id, self._events(original_id)),
                    "total_layers_detected": scan.total_layers,
                    "warnings": scan.warnings,
                }
            finally:
                temporary.unlink(missing_ok=True)

    def _verified_source(self, original_id: str) -> Path:
        source = self.original_dir(original_id) / "source.gcode"
        if sha256_file(source) != original_id:
            raise StorageError(f"Original failed SHA-256 verification: {source}")
        return source

    def process(self, original_id: str, settings: FlowFixSettings) -> dict[str, Any]:
        with self.locked():
            self._events(original_id)
            revision_id = "rev-" + uuid4().hex
            directory = self.original_dir(original_id)
            target = self._revision_path(original_id, revision_id)
            pending = directory / "pending" / f"{revision_id}.json"
            temporary = None
            committed = False
            try:
                snapshot = settings.to_dict()
                source = self._verified_source(original_id)
                atomic_json(pending, {
                    "schema_version": SCHEMA_VERSION, "original_id": original_id,
                    "revision_id": revision_id, "settings": snapshot, "timestamp": timestamp(),
                }, immutable=True)
                descriptor, name = tempfile.mkstemp(prefix=".process-", dir=target.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as output:
                    results = transform_file(source, output, settings)
                    output.flush()
                    os.fsync(output.fileno())
                output_hash = sha256_file(temporary)
                self._verified_source(original_id)
                results.update(
                    size_bytes=temporary.stat().st_size,
                    output_relative_path=f"revisions/{revision_id}.gcode",
                )
                previous = [e for e in self._events(original_id) if e["type"] == "process"]
                changes = settings_diff(previous[-1]["settings"] if previous else {}, snapshot)
                publish(temporary, target)
                event = self._append(
                    original_id, "process", source_revision_id=None,
                    output_revision_id=revision_id, input_sha256=original_id,
                    output_sha256=output_hash, settings=snapshot, results=results,
                    changes_from_previous_settings=changes,
                )
                committed = True
                pending.unlink()
                self._update_index(original_id)
                return event
            except (OSError, ValueError, RuntimeError) as exc:
                if not committed:
                    committed = any(
                        e.get("output_revision_id") == revision_id
                        for e in self._events(original_id)
                    )
                if committed:
                    raise StorageError(
                        f"Revision {revision_id} was committed, but index/cleanup failed. "
                        f"Refresh or rebuild indexes. Details: {exc}"
                    ) from exc
                try:
                    self._append(original_id, "error", status="error", metadata={
                        "operation": "process", "revision_id": revision_id,
                        "error": str(exc), "error_type": type(exc).__name__,
                        "orphan_revision": target.name if target.exists() else None,
                    })
                    pending.unlink(missing_ok=True)
                    self._update_index(original_id)
                except (OSError, ValueError, RuntimeError) as recording_error:
                    raise StorageError(
                        f"Processing failed: {exc}. Recording the failure also failed: "
                        f"{recording_error}. Preserve workspace and run recovery."
                    ) from exc
                raise
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def _find_run(self, original_id: str, revision_id: str) -> dict[str, Any]:
        self._revision_path(original_id, revision_id)
        for event in self._events(original_id):
            if event["type"] == "process" and event["output_revision_id"] == revision_id:
                return event
        raise StorageError(f"No committed revision: {revision_id}")

    @contextmanager
    def _record_action_errors(self, original_id: str, operation: str) -> Iterator[None]:
        try:
            yield
        except (OSError, ValueError, RuntimeError) as exc:
            try:
                self._append(original_id, "error", status="error", metadata={
                    "operation": operation, "error": str(exc),
                    "error_type": type(exc).__name__,
                })
                self._update_index(original_id)
            except (OSError, ValueError, RuntimeError) as recording_error:
                raise StorageError(
                    f"{operation} failed: {exc}. Failure recording also failed: "
                    f"{recording_error}"
                ) from exc
            raise

    def revision_path(self, original_id: str, revision_id: str) -> Path:
        with self.locked():
            event = self._find_run(original_id, revision_id)
            path = self._revision_path(original_id, revision_id)
            if sha256_file(path) != event["output_sha256"]:
                raise StorageError(f"Revision failed integrity check: {path}")
            return path

    def restore_settings(self, original_id: str, revision_id: str) -> FlowFixSettings:
        with self.locked(), self._record_action_errors(original_id, "restore_settings"):
            event = self._find_run(original_id, revision_id)
            settings = FlowFixSettings.from_dict(event["settings"])
            self._append(original_id, "restore_settings", settings=settings.to_dict(),
                         metadata={"from_revision_id": revision_id, "draft": True})
            self._update_index(original_id)
            return settings

    def export(self, original_id: str, revision_id: str, destination: Path) -> dict[str, Any]:
        with self.locked(), self._record_action_errors(original_id, "export"):
            destination = destination.expanduser().resolve()
            if destination == self.root or self.root in destination.parents:
                raise ValueError("Export outside the managed workspace.")
            event = self._find_run(original_id, revision_id)
            source = self._revision_path(original_id, revision_id)
            if sha256_file(source) != event["output_sha256"]:
                raise StorageError(f"Revision failed integrity check: {source}")
            if destination.exists():
                raise FileExistsError(f"Export never overwrites an existing file: {destination}")
            descriptor, name = tempfile.mkstemp(prefix=".export-", dir=destination.parent)
            temporary = Path(name)
            try:
                with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if sha256_file(temporary) != event["output_sha256"]:
                    raise StorageError("Export copy failed integrity check.")
                publish(temporary, destination)
                try:
                    result = self._append(original_id, "export", metadata={
                        "revision_id": revision_id, "destination": str(destination),
                        "sha256": event["output_sha256"],
                    })
                    self._update_index(original_id)
                except (OSError, ValueError, RuntimeError) as exc:
                    raise StorageError(
                        f"Export exists at {destination}, but recording/index update failed: {exc}"
                    ) from exc
                return result
            finally:
                temporary.unlink(missing_ok=True)

    def compare(
        self, original_id: str, first_id: str, second_id: str, limit: int = 200
    ) -> dict[str, Any]:
        with self.locked():
            first = self._find_run(original_id, first_id)
            second = self._find_run(original_id, second_id)
            differences = []
            difference_count = 0
            from itertools import zip_longest
            paths = [self._revision_path(original_id, r) for r in (first_id, second_id)]
            for path, event in zip(paths, (first, second)):
                if sha256_file(path) != event["output_sha256"]:
                    raise StorageError(f"Revision failed integrity check: {path}")
            with paths[0].open("rb") as left, paths[1].open("rb") as right:
                for number, pair in enumerate(
                    zip_longest(binary_lines(left), binary_lines(right)), 1
                ):
                    if pair[0] != pair[1]:
                        difference_count += 1
                        if len(differences) < limit:
                            differences.append({
                                "output_line": number,
                                "before": None if pair[0] is None else repr(pair[0][:500]),
                                "after": None if pair[1] is None else repr(pair[1][:500]),
                            })
            return {
                "first_revision_id": first_id, "second_revision_id": second_id,
                "settings_diff": settings_diff(first["settings"], second["settings"]),
                "output_hashes": [first["output_sha256"], second["output_sha256"]],
                "identical_bytes": first["output_sha256"] == second["output_sha256"],
                "insertion_counts": [{
                    key: event["results"][key]
                    for key in ("inserted_low_m221", "inserted_restore_m221")
                } for event in (first, second)],
                "comparison": "Positional output-line comparison, not an aligned edit diff.",
                "different_output_lines": difference_count,
                "line_samples": differences,
                "samples_truncated": difference_count > len(differences),
            }

    def rebuild(self) -> dict[str, Any]:
        """Verify committed artifacts, resolve interrupted attempts, rebuild caches."""
        with self.locked():
            report: dict[str, Any] = {"rebuilt_originals": [], "issues": []}
            for directory in sorted(self.originals.iterdir()):
                original_id = directory.name
                if not directory.is_dir() or not DIGEST.fullmatch(original_id):
                    continue
                try:
                    self._verified_source(original_id)
                    events = self._events(original_id)
                    for event in events:
                        if event["type"] == "process":
                            path = self._revision_path(original_id, event["output_revision_id"])
                            if sha256_file(path) != event["output_sha256"]:
                                raise StorageError(f"Revision integrity mismatch: {path}")
                    for path in sorted((directory / "pending").glob("*.json")):
                        pending = read_json(path)
                        revision = pending.get("revision_id")
                        self._revision_path(original_id, revision)
                        if (
                            pending.get("original_id") != original_id
                            or path.name != f"{revision}.json"
                        ):
                            raise StorageError(f"Invalid pending run: {path}")
                        completed = any(
                            e.get("output_revision_id") == revision
                            or (e["type"] == "error"
                                and e["metadata"].get("revision_id") == revision)
                            for e in events
                        )
                        if not completed:
                            self._append(original_id, "error", status="error", metadata={
                                "operation": "recovery", "revision_id": revision,
                                "error": "Interrupted run; any uncommitted output is retained.",
                            })
                        path.unlink()
                    index = self._update_index(original_id)
                    for orphan in index["orphan_revision_files"]:
                        report["issues"].append(f"Retained uncommitted revision: {directory / 'revisions' / orphan}")
                    leftovers = list(directory.rglob(".process-*")) + list(directory.rglob(".write-*"))
                    for leftover in leftovers:
                        report["issues"].append(f"Retained temporary artifact: {leftover}")
                    report["rebuilt_originals"].append(original_id)
                except (OSError, ValueError, RuntimeError) as exc:
                    report["issues"].append(f"{original_id}: {exc}")
            try:
                self._update_catalog()
            except (OSError, ValueError, RuntimeError) as exc:
                report["issues"].append(f"Catalog not rebuilt: {exc}")
            for pattern in (".import-*", ".write-*"):
                for path in self.root.glob(pattern):
                    report["issues"].append(f"Retained interrupted workspace write: {path}")
            return report
