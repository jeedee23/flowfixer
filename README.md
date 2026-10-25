<!--
File: README.md
Purpose: FlowFix usage, processing policy, storage and recovery.
Version: 0.2.0 | Created: 2026-10-25 | Modified: 2026-10-25
Changelog: 0.2.0 - Document streaming editor and persistent JSON revisions.
-->

# FlowFix

Tkinter G-code postprocessor for a belt-printer setup where **Y is height** and
**Z is belt travel**. Runs on Python **3.10+**, using only the standard library.

```sh
python3 flowfixer_updated.py
python3 flowfixer_updated.py --workspace /path/to/private/flowfix-data
python3 -m unittest discover -s tests -v
```

On Windows, use `py -3` instead of `python3`. Python must include Tkinter
(`python3-tk` on distributions that package it separately) and the GUI needs a
graphical desktop. No pip dependencies are required. Windows code paths are
implemented, but native Windows smoke testing is still required.

## Workflow

1. **Import original** using the file chooser (`.gcode`, `.gco`, `.gc`).
   FlowFix copies bytes into its workspace and identifies them by SHA-256.
   Selecting identical content at another path reconnects to the same history.
2. Edit draft settings and inclusive layer ranges. Add/update/remove/sort/clear
   and selection/double-click editing are available; overlapping ranges are
   rejected on add/update and before processing.
3. **Process: new revision** always reads the verified stored **original**,
   never a previous processed revision. Every run gets a unique revision and
   event, even when its output hash matches an earlier run.
4. Select a history event to inspect its full JSON. Select a revision to load
   previous settings as a draft, view a read-only preview, or export a copy.
   Ctrl-select two revisions to compare them.
5. Reopen stored originals from the library after restart, even if the external
   input file moved or no longer exists.

All file-intensive operations run on one worker; Tkinter stays on the main
thread. Wait for operations to finish before closing. Draft edits are not logged
per keystroke: processing records complete applied settings and changes from the
previous successful run. Explicit settings restoration records a draft action.

## Exact processing policy

Only `G1` moves with an explicit numeric `Y` before a `;` comment trigger flow
switching. Other lines are preserved byte-for-byte.

1. `Y < threshold_y`: low flow (default 50%).
2. Otherwise, a matching inclusive layer range: its configured percentage.
3. Otherwise, first layers (default first 2 at 92%).
4. Otherwise, last layers (default last 3 at 97%).
5. Otherwise, default flow (100%).

At exactly the threshold, the range/restore rule applies. When first and last
regions overlap, first layers win (baseline behavior). Ranges 1–20 and 20–30
overlap; 1–20 and 21–30 do not. Percentages are positive integers, with no new
arbitrary upper cap; layer indices/counts must be nonnegative and Y must be
finite.

Layer detection uses Cura-style `;LAYER:n` and optional `;LAYER_COUNT:n`. Without
a count, the maximum layer index plus one is used. Without compatible layer
markers, the GUI warns and does not invent indices. A layer marker without an
explicit-Y G1 does not itself cause a flow change.

**Existing M221 policy:** existing commands are preserved but do not update
FlowFix's tracked flow. Tracked flow starts at the default percentage. This
matches the baseline and may not reflect printer state; the GUI warns when such
commands exist. Inspect outputs before printing. No new slicer dialect, G0
support, or Y/Z reinterpretation is introduced.

Processing scans metadata, then streams the transformation. Memory is bounded
by the longest line and a fixed read chunk rather than the whole file.
Unchanged bytes, non-UTF-8 comments, and mixed LF/CRLF/CR line endings survive.
Inserted commands use the first detected line ending (LF if none).
If EOF restoration is needed after an unterminated line, a separating newline
is added first. A final restore uses the last active layer's restore policy.

On a synthetic Linux benchmark (2,000,000 moves, 37 MB input), traced peak Python
allocation dropped from approximately **283.5 MiB to 0.27 MiB** with output hashes
identical to the baseline. This is a memory optimization, not a claimed speedup:
traced runtimes were 5.95 s baseline / 9.93 s streaming. A separate untraced
200,000-move test also showed a CPU trade-off (about 0.19 s / 0.27 s).
Actual results depend on file structure, storage and system load.

The old `flowfix_file(path, settings)` Python helper remains available, but now
refuses to overwrite an existing suffixed output. The desktop workflow stores
revisions internally and only writes external outputs through explicit export.

## Storage, schema and privacy

Default data location is `workspace/` beside the application, independent of
the launch working directory. Override with `--workspace` or the
`FLOWFIX_WORKSPACE` environment variable (`--workspace` takes precedence).

```text
workspace/
  .lock
  catalog.json
  originals/<64-character-sha256>/
    source.gcode
    metadata.json
    events/00000001_<event-id>.json
    revisions/rev-<uuid>.gcode
    pending/rev-<uuid>.json          # only while a run is in progress
    index.json
```

Each event has `schema_version: 1`, per-original sequence, UTC timestamp with
offset, unique event ID, original ID, application version, type, status and
metadata. Process events additionally record complete settings, source/output
hashes, original source selection, revision ID, size, insertion counts, warnings,
and settings changes from the previous successful run. Event types currently
used: `import_original`, `process`, `restore_settings`, `export`, and `error`.
Settings restoration is a draft action, not another processed revision.

Source bytes and committed JSON events/revisions are never overwritten by the
application. Original copies and revisions are hash-verified before use;
immutability is an application policy, not filesystem permission protection.
Do not manually edit managed artifacts.
Damaged originals are reported individually; healthy originals remain available
in the library. The catalog includes explicit issue descriptions rather than
silently resetting or hiding damaged history.

One OS-backed workspace lock serializes operations across application instances.
A competing instance gets an explicit busy error. Writes use fsynced temporary
files; immutable files are published with exclusive hard links, derived caches
with atomic replacement. Use a local filesystem supporting hard links and OS
file locks (for example ext4 or NTFS); unsupported/read-only filesystems fail
explicitly. No stale-lock deletion is needed: the OS releases locks on exit.
Directory fsync is used on POSIX; Windows filesystem/power-loss guarantees differ.

**Retention:** no automatic pruning or deletion. Local backups can be large.
The default workspace is ignored by Git, including JSON (which contains private
paths/settings) and G-code. A custom data directory must be ignored separately
if it is inside another Git worktree. Back up the **whole workspace**, not only
the catalog. Metadata-only tracking is a manual opt-in policy; it cannot
reconstruct lost G-code. Export never overwrites existing files or writes inside
the managed workspace, even if a save dialog confirms replacement.

## Comparison

Settings differences are reported separately from output hashes, insertion
counts and byte-line samples. The bounded comparison counts all differing
positional output lines and shows at most 200 samples (500 bytes per line).
It is **not an aligned edit diff**: one insertion can shift subsequent lines.
Hash equality proves byte equality; a differing hash alone is not an explanation
of which commands changed. Revision previews are read-only and limited to 1 MiB.

## Failure handling and recovery

Events and `source.gcode`/revision artifacts are authoritative; catalog,
metadata and indexes are rebuildable.

1. Back up the workspace before attempting manual repair.
2. Use **Verify / rebuild indexes** or:

   ```sh
   python3 flowfixer_updated.py --workspace /path/to/data --verify
   ```

   The headless command prints JSON and exits 1 if any issue needs attention.
3. Recovery verifies hashes, validates event schemas/sequences, rebuilds derived
   files, and records interrupted pending runs as errors. A run whose success
   event was already committed remains successful.
4. Uncommitted/orphan output and interrupted temporary files are retained and
   reported, not silently promoted to successful revisions or deleted.
5. Corrupt authoritative events or hash-mismatching artifacts are reported,
   never reset to empty history. Restore those files from a trusted backup.
   A source copied before its first import event committed can be re-imported
   from the external original to complete registration.

Publishing a revision and its event is not one filesystem transaction. Pending
markers make interruption discoverable. If a success event commits but index
maintenance fails, the application reports that the revision was committed and
requests rebuilding rather than claiming no output was created.

The legacy `D:\flowfixes.log` is **not read, changed or deleted**. Automatic
migration is intentionally not attempted: that registry retained only the latest
run per input path and cannot prove immutable original/revision identity.
Optional non-destructive migration can be added if requested; retain the log
as a historical reference and import actual source files through the chooser.

## Development

[The original handoff](FLOWFIX_COPILOT_HANDOFF.md) describes the acceptance rules.
Implementation is split into [models](flowfix/models.py),
[streaming core](flowfix/core.py), [storage](flowfix/storage.py), and
[GUI](flowfix/gui.py). Every new/modified Python file starts with provenance and
a changelog. No original creation date is invented for the baseline launcher.
