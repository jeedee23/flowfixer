# File: flowfix/gui.py
# Purpose: Responsive Tkinter editor with original-specific revision history.
# Version: 0.2.2 | Created: 2026-10-25 | Modified: 2026-10-29
# Changelog: 0.2.2 - Expose plunge depth and processing warnings.
# 0.2.1 - Scale table rows to fonts and reserve visible revision space.
# 0.2.0 - Add asynchronous import/process, history, compare and recovery.

from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk
from typing import Any, Callable

from . import __version__
from .models import BetweenRange, FlowFixSettings, validate_range
from .storage import Workspace

SETTING_LABELS = {
    "low_flow_percent": "Low-Y flow (%)",
    "threshold_y": "Y threshold",
    "restore_flow_percent": "Default flow (%)",
    "number_first_layers": "First layer count",
    "first_layers_restore_flow_percent": "First-layer flow (%)",
    "number_last_layers": "Last layer count",
    "last_layers_restore_flow_percent": "Last-layer flow (%)",
    "output_suffix": "Export suffix",
    "plunge_into_belt_mm": "Plunge into belt (mm; 0 = off)",
}


class App(tk.Tk):
    def __init__(self, workspace: Workspace):
        super().__init__()
        self.workspace = workspace
        self.title(f"FlowFix {__version__} - G-code revision editor")
        self.geometry("1250x900")
        self.minsize(900, 650)
        self.original_id: str | None = None
        self.original: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.library: list[dict[str, Any]] = []
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flowfix")
        self.job: Future[Any] | None = None
        self.job_callback: Callable[[Any], None] | None = None
        self.refresh_on_error = False
        self.controls: list[ttk.Button] = []
        self.input_var = tk.StringVar()
        self.original_var = tk.StringVar(value="Import an original or reopen one from the library.")
        self.status_var = tk.StringVar(value="Ready")
        self.warning_var = tk.StringVar()
        self._configure_table_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._submit(self.workspace.list_originals, self._set_library, "Loading library")

    def _button(self, parent, text, command, **pack):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side="left", padx=(0, 6), pady=2, **pack)
        self.controls.append(button)
        return button

    def _configure_table_style(self):
        self.table_font = font.nametofont("TkDefaultFont").copy()
        if 0 < self.table_font.cget("size") < 11:
            self.table_font.configure(size=11)
        self.table_heading_font = self.table_font.copy()
        self.table_heading_font.configure(weight="bold")
        self.table_row_height = max(32, self.table_font.metrics("linespace") + 12)
        style = ttk.Style(self)
        style.configure(
            "FlowFix.Treeview", font=self.table_font, rowheight=self.table_row_height,
        )
        style.configure(
            "FlowFix.Treeview.Heading", font=self.table_heading_font, padding=(6, 6),
        )

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1, minsize=140)
        self.rowconfigure(1, weight=2, minsize=self.table_row_height * 4 + 80)
        settings_area = ttk.Frame(self)
        settings_area.grid(row=0, column=0, sticky="nsew")
        self.settings_canvas = tk.Canvas(settings_area, highlightthickness=0, height=480)
        settings_scroll = ttk.Scrollbar(
            settings_area, orient="vertical", command=self.settings_canvas.yview,
        )
        self.settings_canvas.configure(yscrollcommand=settings_scroll.set)
        settings_scroll.pack(side="right", fill="y")
        self.settings_canvas.pack(side="left", fill="both", expand=True)
        controls_frame = ttk.Frame(self.settings_canvas)
        controls_window = self.settings_canvas.create_window(
            (0, 0), window=controls_frame, anchor="nw",
        )
        controls_frame.bind(
            "<Configure>",
            lambda _event: self.settings_canvas.configure(
                scrollregion=self.settings_canvas.bbox("all"),
            ),
        )
        self.settings_canvas.bind(
            "<Configure>",
            lambda event: self.settings_canvas.itemconfigure(controls_window, width=event.width),
        )
        top = ttk.Frame(controls_frame, padding=10)
        top.pack(fill="x")
        self._button(top, "Import original...", self._browse)
        self.library_combo = ttk.Combobox(top, state="readonly", width=55)
        self.library_combo.pack(side="left", fill="x", expand=True, padx=6)
        self._button(top, "Open stored original", self._open_stored)
        self._button(top, "Open workspace", self._open_workspace)
        ttk.Label(controls_frame, textvariable=self.original_var, padding=(10, 0)).pack(anchor="w")
        ttk.Entry(controls_frame, textvariable=self.input_var, state="readonly").pack(fill="x", padx=10)
        ttk.Label(controls_frame, textvariable=self.warning_var, foreground="#9b3600",
                  wraplength=1150, padding=(10, 4)).pack(anchor="w")

        settings_frame = ttk.LabelFrame(controls_frame, text="Draft settings (saved with each processing run)", padding=8)
        settings_frame.pack(fill="x", padx=10, pady=4)
        self.setting_vars: dict[str, tk.StringVar] = {}
        defaults = FlowFixSettings().to_dict()
        for index, (name, label) in enumerate(SETTING_LABELS.items()):
            row, column = divmod(index, 4)
            ttk.Label(settings_frame, text=label).grid(row=row * 2, column=column, sticky="w")
            variable = tk.StringVar(value=str(defaults[name]))
            self.setting_vars[name] = variable
            ttk.Entry(settings_frame, textvariable=variable, width=22).grid(
                row=row * 2 + 1, column=column, sticky="ew", padx=(0, 12), pady=(0, 5)
            )
            settings_frame.columnconfigure(column, weight=1)
        ttk.Label(
            settings_frame,
            text="Priority: low Y > inclusive custom range > first layers > last layers > default. "
                 "Only G1 with explicit Y triggers a change. Y is height; Z is belt travel.",
            wraplength=1100,
        ).grid(row=((len(SETTING_LABELS) + 3) // 4) * 2, column=0, columnspan=4, sticky="w")

        ranges = ttk.LabelFrame(controls_frame, text="Between-layer ranges (both bounds inclusive)", padding=8)
        ranges.pack(fill="x", padx=10, pady=4)
        self.range_tree = ttk.Treeview(
            ranges, columns=("from", "until", "flow"), show="headings", height=4,
            selectmode="browse", style="FlowFix.Treeview",
        )
        for name, heading in (("from", "From layer"), ("until", "Until layer"), ("flow", "Flow (%)")):
            self.range_tree.heading(name, text=heading)
            self.range_tree.column(name, anchor="center", width=180)
        self.range_tree.pack(fill="x")
        self.range_tree.bind("<<TreeviewSelect>>", self._load_selected_between_range)
        self.range_tree.bind("<Double-1>", self._load_selected_between_range)
        editor = ttk.Frame(ranges)
        editor.pack(fill="x", pady=(5, 0))
        self.range_vars = [tk.StringVar() for _ in range(3)]
        for label, variable in zip(("From", "Until", "Flow (%)"), self.range_vars):
            ttk.Label(editor, text=label).pack(side="left")
            ttk.Entry(editor, textvariable=variable, width=7).pack(side="left", padx=(4, 10))
        range_actions = ttk.Frame(ranges)
        range_actions.pack(fill="x", pady=(5, 0))
        self._button(range_actions, "Add range", self._add_between_range)
        self._button(range_actions, "Update selected", self._update_selected_between_range)
        self._button(range_actions, "Remove selected", self._remove_selected_between_range)
        self._button(range_actions, "Sort ranges", self._sort_between_ranges)
        self._button(range_actions, "Clear inputs", self._clear_between_range_inputs)

        actions = ttk.Frame(self, padding=(10, 4))
        actions.grid(row=2, column=0, sticky="ew")
        self._button(actions, "Process: new revision", self._run)
        self._button(actions, "Load previous settings", self._restore)
        self._button(actions, "Compare two runs", self._compare)
        self._button(actions, "View revision", self._view_revision)
        secondary_actions = ttk.Frame(self, padding=(10, 0))
        secondary_actions.grid(row=3, column=0, sticky="ew")
        self._button(secondary_actions, "Export revision...", self._export)
        self._button(secondary_actions, "Refresh history", self._refresh)
        self._button(secondary_actions, "Verify / rebuild indexes", self._rebuild)

        notebook = ttk.Notebook(self)
        notebook.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        history_frame = ttk.Frame(notebook)
        revision_frame = ttk.Frame(notebook)
        details_frame = ttk.Frame(notebook)
        notebook.add(history_frame, text="History (select to inspect JSON)")
        notebook.add(revision_frame, text="Revisions (select two to compare)")
        notebook.add(details_frame, text="Event JSON / comparisons")
        self.notebook = notebook
        self.history_tree = self._table(history_frame, (
            ("sequence", "#", 45), ("timestamp", "UTC timestamp", 220),
            ("type", "Action", 120), ("summary", "Changed settings / description", 280),
            ("source", "Input", 100), ("output", "Output revision path", 260),
            ("status", "Status", 80),
        ))
        self.history_tree.bind("<<TreeviewSelect>>", self._show_event)
        self.revision_tree = self._table(revision_frame, (
            ("revision", "Revision", 260), ("timestamp", "Created (UTC)", 220),
            ("source", "Source", 100), ("hash", "SHA-256", 260), ("size", "Bytes", 90),
        ))
        self.details = tk.Text(details_frame, wrap="none", state="disabled")
        self.details.pack(fill="both", expand=True, side="left")
        scrollbar = ttk.Scrollbar(details_frame, command=self.details.yview)
        scrollbar.pack(side="right", fill="y")
        self.details.configure(yscrollcommand=scrollbar.set)
        footer = ttk.Frame(self, padding=(10, 4))
        footer.grid(row=4, column=0, sticky="ew")
        ttk.Label(footer, textvariable=self.status_var).pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=160)
        self.progress.pack(side="right")

    def _table(self, parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            frame, columns=[c[0] for c in columns], show="headings",
            style="FlowFix.Treeview",
        )
        for name, label, width in columns:
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=40)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _submit(self, function, callback, description, *, refresh_on_error=False):
        if self.job is not None:
            messagebox.showwarning("Busy", "Wait for the current operation.", parent=self)
            return
        for button in self.controls:
            button.state(["disabled"])
        self.status_var.set(description + "...")
        self.progress.start()
        self.job_callback = callback
        self.refresh_on_error = refresh_on_error
        self.job = self.executor.submit(function)
        self.after(60, self._poll_job)

    def _poll_job(self):
        if self.job is None:
            return
        if not self.job.done():
            self.after(60, self._poll_job)
            return
        job, callback = self.job, self.job_callback
        refresh_on_error = self.refresh_on_error
        self.job = self.job_callback = None
        self.refresh_on_error = False
        self.progress.stop()
        for button in self.controls:
            button.state(["!disabled"])
        try:
            result = job.result()
        except (OSError, ValueError, RuntimeError) as exc:
            self.status_var.set("Operation failed; inspect history / recovery if needed.")
            messagebox.showerror("FlowFix error", str(exc), parent=self)
            if refresh_on_error and self.original_id is not None:
                self._refresh_after_error()
            return
        self.status_var.set("Ready")
        if callback is not None:
            callback(result)

    def _refresh_after_error(self):
        original_id = self.original_id
        self._submit(
            lambda: self.workspace.history(original_id),
            self._set_history, "Reloading recorded history",
        )

    def _set_library(self, library):
        self.library = library["originals"]
        self.library_combo["values"] = [
            f"{item['name']} [{item['original_id'][:12]}]" for item in self.library
        ]
        if self.original_id is not None:
            for index, item in enumerate(self.library):
                if item["original_id"] == self.original_id:
                    self.library_combo.current(index)
                    break
        if library["issues"]:
            self._set_details(json.dumps({"library_issues": library["issues"]}, indent=2))
            self.notebook.select(2)
            messagebox.showwarning(
                "Library needs attention",
                "Some originals could not be loaded. Healthy originals remain available. "
                "See details and use Verify / rebuild indexes.", parent=self,
            )

    def _browse(self):
        path = filedialog.askopenfilename(
            parent=self, title="Import original G-code (never modified)",
            filetypes=[("G-code", "*.gcode *.gco *.gc"), ("All files", "*.*")],
        )
        if path:
            self._clear_original()
            self._submit(
                lambda: self.workspace.import_original(Path(path)),
                self._original_opened, "Importing and hashing original",
            )

    def _open_stored(self):
        index = self.library_combo.current()
        if index < 0:
            messagebox.showerror("No original", "Choose an original from the library.", parent=self)
            return
        original_id = self.library[index]["original_id"]
        self._clear_original()
        self._submit(
            lambda: self.workspace.open_original(original_id),
            self._original_opened, "Verifying stored original",
        )

    def _clear_original(self):
        self.original_id = self.original = None
        self.input_var.set("")
        self.original_var.set("Opening original...")
        self.warning_var.set("")
        self._set_history([])
        self._apply_settings(FlowFixSettings())

    def _original_opened(self, original):
        self.original = original
        self.original_id = original["original_id"]
        count = original["total_layers_detected"]
        self.original_var.set(
            f"{original['name']} | SHA-256 {self.original_id[:16]} | "
            f"Layers: {count if count is not None else 'unknown'} | Imported / verified"
        )
        self.input_var.set(" | ".join(original["observed_source_paths"]))
        self.warning_var.set("\n".join(original["warnings"]))
        original_id = self.original_id
        self._submit(
            lambda: (self.workspace.history(original_id), self.workspace.list_originals()),
            self._loaded_original_history, "Loading original history",
        )

    def _loaded_original_history(self, value):
        events, library = value
        self._set_history(events)
        self._set_library(library)
        previous = [event for event in events if event["type"] == "process"]
        if previous:
            self._apply_settings(FlowFixSettings.from_dict(previous[-1]["settings"]))
            self.status_var.set("Latest successful run settings loaded as draft.")

    def _range_input(self):
        try:
            item = BetweenRange(*(int(variable.get().strip()) for variable in self.range_vars))
        except ValueError as exc:
            raise ValueError("Range fields must be integers.") from exc
        validate_range(item)
        return item

    def _collect_between_ranges(self, replacing=None, candidate=None):
        ranges = []
        for item_id in self.range_tree.get_children():
            if item_id != replacing:
                values = self.range_tree.item(item_id, "values")
                ranges.append(BetweenRange(*(int(value) for value in values)))
        if candidate is not None:
            ranges.append(candidate)
        settings = FlowFixSettings(between_ranges=ranges)
        settings.validate()
        return sorted(ranges, key=lambda r: (r.between_layer, r.until_layer))

    def _add_between_range(self):
        self._edit_range(False)

    def _update_selected_between_range(self):
        self._edit_range(True)

    def _edit_range(self, update):
        selected = self.range_tree.selection()
        if update and not selected:
            messagebox.showerror("No selection", "Select a range to update.", parent=self)
            return
        try:
            item = self._range_input()
            ranges = self._collect_between_ranges(selected[0] if update else None, item)
        except ValueError as exc:
            messagebox.showerror("Invalid range", str(exc), parent=self)
            return
        self._set_ranges(ranges)
        self._clear_between_range_inputs()

    def _set_ranges(self, ranges):
        self.range_tree.delete(*self.range_tree.get_children())
        for item in ranges:
            self.range_tree.insert("", "end", values=(
                item.between_layer, item.until_layer, item.flow_between,
            ))

    def _remove_selected_between_range(self):
        self.range_tree.delete(*self.range_tree.selection())
        self._clear_between_range_inputs()

    def _sort_between_ranges(self):
        self._set_ranges(self._collect_between_ranges())

    def _clear_between_range_inputs(self):
        for variable in self.range_vars:
            variable.set("")

    def _load_selected_between_range(self, _event=None):
        selected = self.range_tree.selection()
        if selected:
            for variable, value in zip(
                self.range_vars, self.range_tree.item(selected[0], "values")
            ):
                variable.set(str(value))

    def _get_settings(self):
        values: dict[str, Any] = {}
        for name, variable in self.setting_vars.items():
            value = variable.get().strip()
            try:
                values[name] = (
                    value if name == "output_suffix"
                    else float(value) if name in {"threshold_y", "plunge_into_belt_mm"} else int(value)
                )
            except ValueError as exc:
                raise ValueError(f"Invalid number for {SETTING_LABELS[name]}.") from exc
        settings = FlowFixSettings(**values, between_ranges=self._collect_between_ranges())
        settings.validate()
        return settings

    def _apply_settings(self, settings):
        values = settings.to_dict()
        for name, variable in self.setting_vars.items():
            variable.set(str(values[name]))
        self._set_ranges(settings.between_ranges)
        self._clear_between_range_inputs()

    def _require_original(self):
        if self.original_id is None:
            messagebox.showerror("No original", "Import or open a stored original first.", parent=self)
            return False
        return True

    def _run(self):
        if not self._require_original():
            return
        try:
            settings = self._get_settings()
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc), parent=self)
            return
        original_id = self.original_id
        self._submit(
            lambda: self.workspace.process(original_id, settings),
            self._processed, "Creating immutable revision", refresh_on_error=True,
        )

    def _processed(self, event):
        warnings = event["results"]["warnings"]
        self.warning_var.set("\n".join(warnings))
        messagebox.showinfo(
            "Revision committed",
            f"Created {event['output_revision_id']}\n"
            f"Low inserts: {event['results']['inserted_low_m221']}; "
            f"restore inserts: {event['results']['inserted_restore_m221']}\n"
            f"Belt plunges: {event['results']['inserted_belt_plunges']}\n\n"
            "Use Export revision to save a copy outside the workspace."
            + ("\n\nWarnings:\n" + "\n".join(warnings) if warnings else ""),
            parent=self,
        )
        self._refresh()

    def _refresh(self):
        if self._require_original():
            original_id = self.original_id
            self._submit(lambda: self.workspace.history(original_id), self._set_history, "Refreshing history")

    def _set_history(self, events):
        self.events = events
        self.history_tree.delete(*self.history_tree.get_children())
        self.revision_tree.delete(*self.revision_tree.get_children())
        self._set_details("")
        for event in events:
            metadata = event["metadata"]
            summary = ", ".join(event.get("changes_from_previous_settings", {}))
            if not summary:
                summary = metadata.get("error", metadata.get("source_path", event["type"]))
            output = event.get("results", {}).get("output_relative_path", "")
            self.history_tree.insert("", "end", iid=event["event_id"], values=(
                event["sequence"], event["timestamp"], event["type"], summary,
                "original" if event["type"] == "process" else "",
                output, event["status"],
            ))
            if event["type"] == "process":
                self.revision_tree.insert(
                    "", "end", iid=event["output_revision_id"], values=(
                        event["output_revision_id"], event["timestamp"], "original",
                        event["output_sha256"], event["results"]["size_bytes"],
                    ),
                )

    def _set_details(self, content):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", content)
        self.details.configure(state="disabled")

    def _show_event(self, _event=None):
        selected = set(self.history_tree.selection())
        if selected:
            event = next(event for event in self.events if event["event_id"] in selected)
            self._set_details(json.dumps(event, indent=2, ensure_ascii=True))
            self.notebook.select(2)

    def _selected_revisions(self, count):
        selected = list(self.revision_tree.selection())
        if not selected:
            event_ids = set(self.history_tree.selection())
            selected = [
                e["output_revision_id"] for e in self.events
                if e["event_id"] in event_ids and e["type"] == "process"
            ]
        if len(selected) != count:
            messagebox.showerror(
                "Select revisions", f"Select exactly {count} successful revision(s). "
                "Use Ctrl-click to select two in the Revisions tab.", parent=self,
            )
            return []
        return selected

    def _restore(self):
        selected = self._selected_revisions(1)
        if selected and self._require_original():
            original_id = self.original_id
            self._submit(
                lambda: self.workspace.restore_settings(original_id, selected[0]),
                self._restored, "Restoring settings as draft", refresh_on_error=True,
            )

    def _restored(self, settings):
        self._apply_settings(settings)
        self._refresh()

    def _compare(self):
        selected = self._selected_revisions(2)
        if selected and self._require_original():
            original_id = self.original_id
            self._submit(
                lambda: self.workspace.compare(original_id, *selected),
                self._comparison_ready, "Comparing settings and output bytes",
            )

    def _comparison_ready(self, comparison):
        self._set_details(json.dumps(comparison, indent=2, ensure_ascii=True))
        self.notebook.select(2)

    def _export(self):
        selected = self._selected_revisions(1)
        if not selected or not self._require_original():
            return
        try:
            settings = self._get_settings()
        except ValueError as exc:
            messagebox.showerror("Invalid export settings", str(exc), parent=self)
            return
        name = Path(self.original["name"])
        destination = filedialog.asksaveasfilename(
            parent=self, title="Export revision (choose a new filename)",
            initialfile=f"{name.stem}{settings.output_suffix}_{selected[0][:12]}.gcode",
            defaultextension=".gcode", filetypes=[("G-code", "*.gcode")],
        )
        if destination:
            original_id = self.original_id
            self._submit(
                lambda: self.workspace.export(original_id, selected[0], Path(destination)),
                lambda _event: self._refresh(), "Exporting verified revision",
                refresh_on_error=True,
            )

    def _view_revision(self):
        selected = self._selected_revisions(1)
        if selected and self._require_original():
            original_id = self.original_id

            def preview():
                path = self.workspace.revision_path(original_id, selected[0])
                with path.open("rb") as stream:
                    data = stream.read(1024 * 1024 + 1)
                truncated = len(data) > 1024 * 1024
                return data[:1024 * 1024].decode("utf-8", errors="backslashreplace"), truncated

            def show(value):
                text, truncated = value
                self._set_details(
                    ("Read-only preview (first 1 MiB; truncated)\n" if truncated else "Read-only preview\n")
                    + text
                )
                self.notebook.select(2)

            self._submit(preview, show, "Verifying revision and preparing preview")

    def _rebuild(self):
        self._submit(self.workspace.rebuild, self._rebuilt, "Verifying files and rebuilding indexes")

    def _rebuilt(self, report):
        self._set_details(json.dumps(report, indent=2))
        self.notebook.select(2)
        if report["issues"]:
            messagebox.showwarning(
                "Recovery needs attention",
                "Some files need attention. Nothing was deleted. See the recovery report.",
                parent=self,
            )
        self._submit(self.workspace.list_originals, self._set_library, "Refreshing library")

    def _open_workspace(self):
        def open_folder():
            try:
                if os.name == "nt":
                    os.startfile(str(self.workspace.root))
                else:
                    subprocess.run(
                        ["xdg-open", str(self.workspace.root)],
                        check=True, capture_output=True, text=True, timeout=15,
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(f"Cannot open workspace folder: {exc}") from exc

        self._submit(open_folder, lambda _result: None, "Opening workspace folder")

    def _close(self):
        if self.job is not None:
            messagebox.showwarning(
                "Operation running", "Wait for the operation before closing FlowFix.", parent=self
            )
            return
        self.executor.shutdown(wait=True)
        self.destroy()
