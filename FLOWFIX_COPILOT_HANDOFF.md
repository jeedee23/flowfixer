<!--
Document: FLOWFIX_COPILOT_HANDOFF.md
Purpose: Implementation handoff for GitHub Copilot / VS Code — FlowFix G-code editor with persistent revision history
Created: 2026-10-25
Last modified: 2026-10-25
Version: 1.1
Changelog:
  1.1 — Link implemented 0.2.0 application, policies, tests and remaining platform validation.
  1.0 — Document current behavior, known limitations, repository design, JSON action history, GUI requirements, tests and implementation phases.
-->

# FlowFix — GitHub Copilot handoff

## Implementation status (0.2.0, 2026-10-25)

The application now has a streaming, byte-preserving core, validated shared
settings, immutable per-content originals/revisions, append-only JSON events,
OS-backed workspace locking, recovery, and a responsive Tkinter history GUI.
The original script is now the launcher; the baseline descriptions below remain
historical context, not a description of the refactored source layout.

See [README.md](README.md) for exact policies, launch commands and recovery;
see [tests/](tests/) for processing, storage and real-Tk integration tests.
Existing M221 commands retain baseline handling with an explicit warning.
Exports never overwrite. Legacy log migration remains opt-in future work;
the old log is untouched. Native Windows smoke testing remains outstanding.

## 1. Mission

Turn the existing `flowfixer_updated.py` into a maintainable, cross-platform (Windows and Linux) desktop application that can select an **original G-code file through the GUI**, apply configurable flow adjustments, and maintain a **persistent JSON-based, per-original-file revision/action database inside the repository's local workspace**.

**Non-negotiable:** Never modify the original G-code in place; preserve every generated revision and every action record. Make it possible to inspect settings, identify what changed, compare revisions, select a previous revision, and reproduce a run.

Before implementing, inspect the actual script in the repository and preserve all currently working functionality. The attached `flowfixer_updated.py` is the baseline; do not assume features absent from the file already exist.

## 2. Project history (only what is supported)

- **Initial FlowFix:** Python + Tkinter G-code postprocessor. Processes line-by-line, detects Cura-style `;LAYER:n` and optional `;LAYER_COUNT:n`, inserts `M221 S...` commands according to the Y-position and first/last-layer overrides. Exports a file with `_flowfix` suffix. Writes a JSONL-style registry to `D:\flowfixes.log`, overwriting the previous registry entry for a given input path.
- **Later requested enhancement:** Multiple ranges with fields `between_layer`, `until_layer`, `flow_between`; both boundaries **inclusive**. Example layers **1–20** = 70%, layers **234–300** = 80% outside the low-Y zone. Original low-Y rule still overrides all ranges.
- **Latest inspected baseline:** `flowfixer_updated.py`, which has a `BetweenRange` dataclass; a range table with Add, Update selected, Remove selected, Clear inputs, and Sort ranges; selection/double-click loading into edit fields; range sorting and overlap checks at run time. Overlap is inclusive: ranges 1–20 and 20–30 conflict; 1–20 and 21–30 do not.
- **2026-10-24 21:49:22 (user-supplied timestamp):** User established a permanent coding convention: every new or modified script/code file MUST start with a comment header identifying file name, purpose, version, date(s), and changelog. This timestamp is **not evidence of the original script's creation time**.
- **2026-10-25:** This handoff requests per-original G-code revision and action history. Earlier original creation dates and Git commit history are **unknown**; do not invent them.

## 3. Current processing behavior — preserve unless explicitly changed

- Uses regular expressions for `G1` at the beginning of a line, `Y` coordinates, `;LAYER:n`, and `;LAYER_COUNT:n`.
- `;LAYER:0`, `;LAYER:1`, etc. identify a layer. The detected layer stays active until another layer marker. If no `LAYER_COUNT`, estimates the count as the highest seen layer + 1.
- Only `G1` lines containing an explicit numeric `Y` are eligible for flow switching. Other lines are copied unchanged. Y is used by this printer/setup as the height axis; Z refers to belt travel. Do **not** silently reinterpret these axes.
- `Y < threshold_y` → `low_flow_percent` (default 50%).
- Otherwise, if the current layer falls in an inclusive configured range → that range's `flow_between`.
- Otherwise → first-layer, last-layer or default restore percentage (defaults first 2 layers 92%, last 3 layers 97%, all other layers 100%).
- Custom ranges take priority over first/last restore percentages, but **never** over low-Y flow.
- The script inserts a new `M221 Snn` only when its tracked target differs from its tracked current value. It may add a final restore command at EOF. Output uses the input stem plus `_flowfix` by default.
- Current registry stores input/output paths, timestamp, detected count, insertion counts and settings, but **only retains the latest record per input path**. This must be replaced or supplemented by append-only history.

### Known limitations to investigate, not silently change

- The internal `current_flow` starts at the assumed default restore percentage; the existing source may already contain `M221` commands, which are not fully modeled. Document handling and add regression tests before adjusting this behavior.
- `G1` without a Y parameter does not trigger a flow change; this includes moves after a `;LAYER:n` marker with no explicit Y. Preserve semantics initially and call out this constraint in the GUI/help.
- G-code layer markers and axis conventions are slicer/printer-specific. Show a warning when no compatible markers are detected, rather than inventing layer indices.
- The original code uses `errors="ignore"` on input decoding and rewrites output as UTF-8. Prefer lossless preservation for unchanged lines and preserve newline conventions; test explicitly.
- `D:\flowfixes.log` is Windows-specific. New storage must be cross-platform and configurable, avoiding writes to a drive that does not exist on Linux.

## 4. Repository and database model

Use a **JSON-per-action + per-original index** model. JSON is the source of truth, not a GUI-only cached table.

Suggested structure (adapt after inspecting repository):

```text
repo/
  README.md
  flowfixer_updated.py                 # initial baseline, or refactor later
  flowfix/
    core.py
    storage.py
    models.py
    gui.py
  tests/
    test_flow_logic.py
    test_storage.py
    test_revision_history.py
  workspace/                            # persistent local application data
    catalog.json                        # mapping content hash to original record
    originals/
      <original_sha256>/
        source.gcode                    # immutable byte-for-byte copy
        metadata.json
        events/
          000001_<action-id>.json
          000002_<action-id>.json
        revisions/
          <revision-id>.gcode
        index.json                      # derived summary; rebuildable
```

Prefer separate **original file identity** (SHA-256 of bytes) from **individual processing run identity** (UUID or stable unique event ID). The same original content opened from two paths should be recognized as the same original, while retaining both observed source paths as metadata. If modified input bytes are selected, that is a **different original identity** unless the user explicitly imports it as a revision.

`workspace/` may reside inside the repository when the user requests that layout, but **do not automatically commit large/private G-code files**. Provide `.gitignore` defaults and an explicit option/policy for tracking metadata-only versus full G-code backups. Keep backups; `gitignore` is not a deletion mechanism.

## 5. JSON schema (illustrative, versioned)

One immutable event per user action. Events should have `schema_version`, ISO 8601 timestamp with UTC offset, unique event ID, original ID, event type, source revision (if applicable), resulting revision (if applicable), complete settings snapshot (if applicable), status and metadata.

Example processing event:

```json
{
  "schema_version": 1,
  "event_id": "c244eaf7-9a21-49b6-9c1a-ecf87270c9f8",
  "sequence": 3,
  "timestamp": "2026-10-25T15:08:00+01:00",
  "type": "process",
  "status": "success",
  "original_id": "sha256:<64-char-hex-digest>",
  "source_revision_id": null,
  "output_revision_id": "rev-0003",
  "input_sha256": "<64-char-hex-digest>",
  "output_sha256": "<64-char-hex-digest>",
  "settings": {
    "low_flow_percent": 50,
    "threshold_y": 0.5,
    "restore_flow_percent": 100,
    "number_first_layers": 2,
    "first_layers_restore_flow_percent": 92,
    "number_last_layers": 3,
    "last_layers_restore_flow_percent": 97,
    "between_ranges": [
      {"between_layer": 1, "until_layer": 20, "flow_between": 70},
      {"between_layer": 234, "until_layer": 300, "flow_between": 80}
    ],
    "output_suffix": "_flowfix"
  },
  "results": {
    "total_layers_detected": 320,
    "inserted_low_m221": 12,
    "inserted_restore_m221": 34,
    "output_relative_path": "revisions/rev-0003.gcode"
  },
  "changes_from_previous_settings": {
    "between_ranges": {"before": [], "after": [
      {"between_layer": 1, "until_layer": 20, "flow_between": 70},
      {"between_layer": 234, "until_layer": 300, "flow_between": 80}
    ]}
  },
  "application_version": "0.2.0"
}
```

**This is example data, not an actual processing result or verified timestamp for a run.** Store the actual hashes, counters and timestamps at runtime; never fabricate them.

Possible event types: `import_original`, `settings_changed`, `range_added`, `range_updated`, `range_removed`, `process`, `export`, `restore_settings`, `delete_request` (only with explicit confirmation), `error`. For each useful user action, append an event with `before`/`after` where relevant; distinguish GUI edits that are **draft/unapplied** from a **committed processing run** to avoid excessive noisy records. Default to logging substantive saved/applied actions; decide the precise persistence point visibly in the interface.

Use a monotonically increasing sequence **per original**, atomic writes (temp file + replace), and lock or safe serialization to prevent duplicate sequence IDs and half-written JSON. Preserve event history, including failed processing attempts with error details but no revision artifact. Validate data on load; offer index rebuilding from event files.

## 6. GUI requirements

Keep the existing Tkinter GUI functional, with these additions:

1. **Open original G-code** (`.gcode`, `.gco`, `.gc`) through the file chooser. Display name, original SHA-256 short ID, detected layer count, import status and original location. Preserve/import original bytes into workspace on first use.
2. **Settings panel** retains low-flow, threshold, default restore, first/last-layer overrides and suffix.
3. **Between-ranges editor** retains Add, Update, Remove, Sort, Clear, selection/double-click editing; inclusive bounds and overlap rejection. Show input error messages.
4. **History panel per original** (table): event number, date/time, action, short description of changed settings, input/revision, output path, status. Sort chronologically; selecting an event shows complete JSON details and before/after settings.
5. **Revision panel**: list generated G-code versions with creation time, source/parent revision, hashes and sizes; allow opening/exporting an existing revision without changing the original.
6. Buttons for **Process → create new revision**, **Load previous settings**, **Compare two runs**, **Export revision**, **Open workspace folder**, **Refresh history**. No silent overwrites.
7. Optional preview before processing: current effective flow rules, selected inclusive ranges and warnings.
8. GUI history must survive restart, moving the original file, and multiple runs. If a previously imported content hash is encountered, reconnect to the correct history.

### Comparison behavior

Separate **settings differences** (field-by-field JSON diff) from **G-code changes** (e.g., number and locations of inserted `M221` commands, hashes and optional line-oriented diff). Don't claim that a hash difference explains *which* commands changed.

## 7. Processing/recording workflow

1. User selects original via GUI.
2. Compute SHA-256 of selected bytes; locate or create original record; make verified immutable copy if not already stored.
3. Load this original's complete history and offer last-applied settings.
4. User edits settings/ranges; validate valid numeric inputs, nonnegative layer indices, `from <= until`, no overlap, and sensible flow values. Ensure the inclusive meaning is explicit.
5. On **Process**, create a unique revision ID; read the **original** bytes by default (not a previous processed file), unless user explicitly chooses a revision as input. Avoid accumulated repeated insertions.
6. Process to a temporary output, validate creation and output hash, then atomically move to permanent revision path.
7. Append a successful `process` event recording full settings, source and output hashes, counts, and setting differences from previous successful run.
8. Update/rebuild catalog and per-original index, refresh GUI, show output.
9. If anything fails, preserve original and prior revisions and record an `error` event when possible.

## 8. Rules and acceptance tests

- `between_layer=1`, `until_layer=20`, `flow_between=70` applies to **both layers 1 and 20** and every layer between, outside the low-Y zone.
- A second rule 234–300 at 80% applies inclusively and independently.
- At `Y < threshold_y`, low flow overrides every custom range.
- At `Y == threshold_y`, use custom-range or restore flow, not low flow.
- Ranges 1–20 and 20–30 **overlap** and must be rejected; 1–20 and 21–30 are valid.
- First/last-layer settings remain unchanged outside matched ranges.
- Processing the same original twice creates **two different run events and revision IDs**, even if settings/output bytes are identical; the output hashes may match.
- Original source bytes and original SHA-256 stay unchanged after every operation.
- Importing the same original via a different path loads the same history.
- Selecting a different original never shows another original's history.
- A failed run cannot appear as a successful revision; prior output survives.
- All events and revisions remain discoverable after application restart.
- User can restore settings from an earlier successful run and run again without overwriting it.
- Runs on both Windows and Linux without assuming a `D:` drive.
- No code file may be created or modified without a **top-of-file comment header**: file name, purpose, version, created/modified dates, and concise changelog. For non-commentable strict JSON files, store provenance in metadata fields rather than invalid comments.

## 9. Implementation order for Copilot

1. Read existing script; summarize verified structure and unexpected ambiguities before refactoring.
2. Extract/test pure flow-processing functions, preserving behavior; add representative G-code fixtures.
3. Define typed data models and `schema_version=1` event format; implement atomic storage and source hashing.
4. Implement import-original and append-only event/revision creation; regression-test persistence and duplicates.
5. Connect storage to current GUI and add history/revisions views.
6. Add compare, restore settings, export, and data repair/index rebuild.
7. Finish docs, `.gitignore` policy, usability checks, and Windows/Linux smoke tests.

**Deliverable:** Running application plus tests and README documenting exact storage location, retention/privacy policy, schema, migration from `D:\flowfixes.log` (if requested), and recovery procedure. Do not silently delete or overwrite the existing log; migration must be optional and non-destructive.

## 10. Questions Copilot should surface as decisions (without blocking initial work)

- Should **all** saved/imported G-code files be committed to Git, or only JSON metadata while binaries live in an ignored workspace? Default: ignore G-code artifacts while retaining them locally.
- Should GUI draft edits be logged individually or only applied/saved edits? Default: record applied actions and committed run snapshots, not every keystroke.
- Should the app allow reprocessing an already generated revision? Default: original is always the processing input; explicit advanced source selection can be added later.
- Confirm slicer compatibility (layer markers) and custom Y/Z belt-printer coordinate semantics before expanding parsing to other commands.

Do not change the core flow percentages, priorities or inclusive-range semantics without explicit approval.
