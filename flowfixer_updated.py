import json
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# =========================
# Logging
# =========================
LOG_PATH = Path(r"D:\flowfixes.log")  # registry-like log (one entry per input file)


def load_log() -> dict:
    """
    Returns dict keyed by input_path (str) -> entry dict.
    Log file is JSONL (one JSON object per line).
    """
    entries = {}
    if not LOG_PATH.exists():
        return entries
    try:
        for line in LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            key = obj.get("input_path")
            if key:
                entries[key] = obj  # last wins (but we rewrite anyway)
    except Exception:
        # If log is corrupted, don't crash; just start fresh.
        return entries
    return entries


def save_log(entries: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for key in sorted(entries.keys(), key=lambda s: s.lower()):
        lines.append(json.dumps(entries[key], ensure_ascii=False))
    LOG_PATH.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# =========================
# Flowfix core
# =========================
g1_re = re.compile(r'^\s*G1\b', re.IGNORECASE)
y_re = re.compile(r'(?<![A-Z])Y([-+]?\d*\.?\d+)', re.IGNORECASE)
layer_count_re = re.compile(r';\s*LAYER_COUNT\s*:\s*(\d+)', re.IGNORECASE)
layer_mark_re = re.compile(r';\s*LAYER\s*:\s*(\d+)', re.IGNORECASE)


@dataclass
class BetweenRange:
    between_layer: int
    until_layer: int
    flow_between: int


@dataclass
class FlowFixSettings:
    low_flow_percent: int = 50
    threshold_y: float = 0.5
    restore_flow_percent: int = 100

    number_first_layers: int = 2
    first_layers_restore_flow_percent: int = 92

    number_last_layers: int = 3
    last_layers_restore_flow_percent: int = 97

    between_ranges: list[BetweenRange] = field(default_factory=list)

    output_suffix: str = "_flowfix"


def extract_y_value(line: str):
    code = line.split(';', 1)[0]
    m = y_re.search(code)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None



def pass1_collect_layers(lines):
    per_line_layer = [None] * len(lines)
    total_layers = None
    current_layer = None
    max_layer_seen = None

    for i, line in enumerate(lines):
        m_count = layer_count_re.search(line)
        if m_count:
            try:
                total_layers = int(m_count.group(1))
            except ValueError:
                pass

        m_layer = layer_mark_re.search(line)
        if m_layer:
            try:
                current_layer = int(m_layer.group(1))
                if max_layer_seen is None or current_layer > max_layer_seen:
                    max_layer_seen = current_layer
            except ValueError:
                pass

        per_line_layer[i] = current_layer

    if total_layers is None and max_layer_seen is not None:
        total_layers = max_layer_seen + 1

    return per_line_layer, total_layers



def matching_between_flow(layer_index, s: FlowFixSettings):
    if layer_index is None:
        return None

    for item in s.between_ranges:
        if item.between_layer <= layer_index <= item.until_layer:
            return item.flow_between
    return None



def layer_restore_flow(layer_index, total_layers, s: FlowFixSettings) -> int:
    between_flow = matching_between_flow(layer_index, s)
    if between_flow is not None:
        return between_flow

    if layer_index is None or total_layers is None:
        return s.restore_flow_percent

    if s.number_first_layers > 0 and layer_index < s.number_first_layers:
        return s.first_layers_restore_flow_percent

    if s.number_last_layers > 0 and layer_index >= max(0, total_layers - s.number_last_layers):
        return s.last_layers_restore_flow_percent

    return s.restore_flow_percent



def flowfix_file(input_path: Path, settings: FlowFixSettings) -> dict:
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    text = input_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines(keepends=True)

    per_line_layer, total_layers = pass1_collect_layers(lines)

    out_lines = []
    current_flow = settings.restore_flow_percent
    fmt_cmd = lambda p: f"M221 S{int(p)}\n"

    low_inserts = 0
    restore_inserts = 0

    for i, line in enumerate(lines):
        layer_idx = per_line_layer[i]
        restore_target = layer_restore_flow(layer_idx, total_layers, settings)

        if g1_re.match(line):
            y_val = extract_y_value(line)
            if y_val is not None:
                if y_val < settings.threshold_y:
                    if current_flow != settings.low_flow_percent:
                        out_lines.append(fmt_cmd(settings.low_flow_percent))
                        current_flow = settings.low_flow_percent
                        low_inserts += 1
                else:
                    if current_flow != restore_target:
                        out_lines.append(fmt_cmd(restore_target))
                        current_flow = restore_target
                        restore_inserts += 1

        out_lines.append(line)

    last_layer = next((idx for idx in reversed(per_line_layer) if idx is not None), None)
    eof_restore_target = layer_restore_flow(last_layer, total_layers, settings)
    if current_flow != eof_restore_target:
        out_lines.append(fmt_cmd(eof_restore_target))
        restore_inserts += 1

    out_path = input_path.with_name(input_path.stem + settings.output_suffix + input_path.suffix)
    out_path.write_text(''.join(out_lines), encoding="utf-8")

    return {
        "input_path": str(input_path),
        "output_path": str(out_path),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total_layers_detected": total_layers,
        "inserted_low_m221": low_inserts,
        "inserted_restore_m221": restore_inserts,
        "settings": asdict(settings),
    }


# =========================
# GUI
# =========================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FlowFix G-code (M221 by Y + layer-aware restore + between ranges)")
        self.geometry("1180x760")

        self.log_entries = load_log()
        self.input_var = tk.StringVar(value="")

        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Input G-code file:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.input_var, width=90).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(top, text="Browse...", command=self._browse).grid(row=0, column=2, sticky="e")
        top.columnconfigure(1, weight=1)

        settings = ttk.LabelFrame(self, text="Settings", padding=10)
        settings.pack(fill="x", padx=10, pady=(0, 10))

        self.low_flow_var = tk.IntVar(value=50)
        self.threshold_y_var = tk.DoubleVar(value=0.5)
        self.restore_flow_var = tk.IntVar(value=100)

        self.first_layers_n_var = tk.IntVar(value=2)
        self.first_restore_var = tk.IntVar(value=92)

        self.last_layers_n_var = tk.IntVar(value=3)
        self.last_restore_var = tk.IntVar(value=97)

        self.suffix_var = tk.StringVar(value="_flowfix")

        row = 0

        def add(label, var, col):
            ttk.Label(settings, text=label).grid(row=row, column=col, sticky="w")
            ttk.Entry(settings, textvariable=var, width=12).grid(row=row, column=col + 1, sticky="w", padx=(6, 18))

        add("LOW_FLOW_PERCENT", self.low_flow_var, 0)
        add("THRESHOLD_Y", self.threshold_y_var, 2)
        add("RESTORE_FLOW_PERCENT", self.restore_flow_var, 4)

        row = 1
        add("NUMBER_FIRST_LAYERS", self.first_layers_n_var, 0)
        add("FIRST_LAYERS_RESTORE_FLOW_PERCENT", self.first_restore_var, 2)
        add("NUMBER_LAST_LAYERS", self.last_layers_n_var, 4)
        ttk.Label(settings, text="LAST_LAYERS_RESTORE_FLOW_PERCENT").grid(row=row, column=6, sticky="w")
        ttk.Entry(settings, textvariable=self.last_restore_var, width=12).grid(row=row, column=7, sticky="w", padx=(6, 18))

        row = 2
        ttk.Label(settings, text="OUTPUT_SUFFIX").grid(row=row, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.suffix_var, width=18).grid(row=row, column=1, sticky="w", padx=(6, 18))

        ranges_frame = ttk.LabelFrame(self, text="Between layer ranges (inclusive)", padding=10)
        ranges_frame.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Label(
            ranges_frame,
            text="For matching layers: Y < THRESHOLD_Y keeps LOW_FLOW_PERCENT; Y >= THRESHOLD_Y uses FLOW_BETWEEN.",
        ).pack(anchor="w", pady=(0, 8))

        range_actions = ttk.Frame(ranges_frame)
        range_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(range_actions, text="Add range", command=self._add_between_range).pack(side="left")
        ttk.Button(range_actions, text="Update selected", command=self._update_selected_between_range).pack(side="left", padx=8)
        ttk.Button(range_actions, text="Remove selected", command=self._remove_selected_between_range).pack(side="left", padx=8)
        ttk.Button(range_actions, text="Sort ranges", command=self._sort_between_ranges).pack(side="left", padx=8)

        range_cols = ("between_layer", "until_layer", "flow_between")
        self.range_tree = ttk.Treeview(ranges_frame, columns=range_cols, show="headings", height=6)
        self.range_tree.pack(fill="x")
        self.range_tree.bind("<<TreeviewSelect>>", self._load_selected_between_range)
        self.range_tree.bind("<Double-1>", self._load_selected_between_range)

        self.range_tree.heading("between_layer", text="Between layer (from)")
        self.range_tree.heading("until_layer", text="Until layer")
        self.range_tree.heading("flow_between", text="Flow between (%)")

        self.range_tree.column("between_layer", width=180, anchor="center")
        self.range_tree.column("until_layer", width=180, anchor="center")
        self.range_tree.column("flow_between", width=180, anchor="center")

        entry_row = ttk.Frame(ranges_frame)
        entry_row.pack(fill="x", pady=(8, 0))

        self.range_from_var = tk.StringVar(value="")
        self.range_until_var = tk.StringVar(value="")
        self.range_flow_var = tk.StringVar(value="")

        ttk.Label(entry_row, text="From").pack(side="left")
        ttk.Entry(entry_row, textvariable=self.range_from_var, width=10).pack(side="left", padx=(6, 16))
        ttk.Label(entry_row, text="Until").pack(side="left")
        ttk.Entry(entry_row, textvariable=self.range_until_var, width=10).pack(side="left", padx=(6, 16))
        ttk.Label(entry_row, text="Flow between").pack(side="left")
        ttk.Entry(entry_row, textvariable=self.range_flow_var, width=10).pack(side="left", padx=(6, 16))
        ttk.Button(entry_row, text="Insert row", command=self._add_between_range).pack(side="left")
        ttk.Button(entry_row, text="Clear inputs", command=self._clear_between_range_inputs).pack(side="left", padx=(8, 0))

        actions = ttk.Frame(self, padding=(10, 0, 10, 10))
        actions.pack(fill="x")
        ttk.Button(actions, text="Run FlowFix", command=self._run).pack(side="left")
        ttk.Button(actions, text="Reload log", command=self._reload_log).pack(side="left", padx=8)

        lf = ttk.LabelFrame(self, text=f"FlowFix registry (from {LOG_PATH})", padding=10)
        lf.pack(fill="both", expand=True, padx=10, pady=10)

        cols = ("input_path", "output_path", "timestamp", "low", "restore", "layers")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings")
        self.tree.pack(fill="both", expand=True, side="left")

        headings = {
            "input_path": "Input file",
            "output_path": "Output file",
            "timestamp": "Last run",
            "low": "M221 low inserts",
            "restore": "M221 restore inserts",
            "layers": "Layers detected",
        }
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=150 if c in ("timestamp", "low", "restore", "layers") else 380, anchor="w")

        scroll = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Select G-code file",
            filetypes=[("G-code", "*.gcode *.gco *.gc"), ("All files", "*.*")],
        )
        if path:
            self.input_var.set(path)

    def _parse_between_row(self, between_raw: str, until_raw: str, flow_raw: str) -> BetweenRange:
        try:
            between_layer = int(str(between_raw).strip())
            until_layer = int(str(until_raw).strip())
            flow_between = int(str(flow_raw).strip())
        except ValueError as exc:
            raise ValueError("Between layer ranges must use integer values.") from exc

        if between_layer < 0 or until_layer < 0:
            raise ValueError("Layer values must be 0 or greater.")
        if until_layer < between_layer:
            raise ValueError("Until layer must be greater than or equal to Between layer.")
        if flow_between <= 0:
            raise ValueError("Flow between must be greater than 0.")

        return BetweenRange(
            between_layer=between_layer,
            until_layer=until_layer,
            flow_between=flow_between,
        )

    def _clear_between_range_inputs(self):
        self.range_from_var.set("")
        self.range_until_var.set("")
        self.range_flow_var.set("")

    def _load_selected_between_range(self, _event=None):
        selected = self.range_tree.selection()
        if not selected:
            return
        values = self.range_tree.item(selected[0], "values")
        if len(values) != 3:
            return
        self.range_from_var.set(str(values[0]))
        self.range_until_var.set(str(values[1]))
        self.range_flow_var.set(str(values[2]))

    def _add_between_range(self):
        try:
            item = self._parse_between_row(
                self.range_from_var.get(),
                self.range_until_var.get(),
                self.range_flow_var.get(),
            )
        except ValueError as e:
            messagebox.showerror("Invalid between range", str(e))
            return

        self.range_tree.insert(
            "",
            "end",
            values=(item.between_layer, item.until_layer, item.flow_between),
        )
        self._sort_between_ranges()
        self._clear_between_range_inputs()

    def _update_selected_between_range(self):
        selected = self.range_tree.selection()
        if not selected:
            messagebox.showerror("No selection", "Please select a range to update.")
            return

        try:
            item = self._parse_between_row(
                self.range_from_var.get(),
                self.range_until_var.get(),
                self.range_flow_var.get(),
            )
        except ValueError as e:
            messagebox.showerror("Invalid between range", str(e))
            return

        self.range_tree.item(
            selected[0],
            values=(item.between_layer, item.until_layer, item.flow_between),
        )
        self._sort_between_ranges()
        self._clear_between_range_inputs()

    def _remove_selected_between_range(self):
        selected = self.range_tree.selection()
        for item_id in selected:
            self.range_tree.delete(item_id)
        self._clear_between_range_inputs()

    def _sort_between_ranges(self):
        rows = []
        for item_id in self.range_tree.get_children():
            values = self.range_tree.item(item_id, "values")
            parsed = self._parse_between_row(*values)
            rows.append(parsed)

        rows.sort(key=lambda r: (r.between_layer, r.until_layer, r.flow_between))

        for item_id in self.range_tree.get_children():
            self.range_tree.delete(item_id)

        for row in rows:
            self.range_tree.insert(
                "",
                "end",
                values=(row.between_layer, row.until_layer, row.flow_between),
            )

    def _collect_between_ranges(self) -> list[BetweenRange]:
        collected = []
        for item_id in self.range_tree.get_children():
            values = self.range_tree.item(item_id, "values")
            collected.append(self._parse_between_row(*values))

        collected.sort(key=lambda r: (r.between_layer, r.until_layer, r.flow_between))

        for prev, curr in zip(collected, collected[1:]):
            if curr.between_layer <= prev.until_layer:
                raise ValueError(
                    "Between layer ranges overlap: "
                    f"{prev.between_layer}-{prev.until_layer} overlaps with "
                    f"{curr.between_layer}-{curr.until_layer}."
                )

        return collected

    def _get_settings(self) -> FlowFixSettings:
        return FlowFixSettings(
            low_flow_percent=int(self.low_flow_var.get()),
            threshold_y=float(self.threshold_y_var.get()),
            restore_flow_percent=int(self.restore_flow_var.get()),
            number_first_layers=int(self.first_layers_n_var.get()),
            first_layers_restore_flow_percent=int(self.first_restore_var.get()),
            number_last_layers=int(self.last_layers_n_var.get()),
            last_layers_restore_flow_percent=int(self.last_restore_var.get()),
            between_ranges=self._collect_between_ranges(),
            output_suffix=str(self.suffix_var.get()).strip() or "_flowfix",
        )

    def _run(self):
        ip = self.input_var.get().strip()
        if not ip:
            messagebox.showerror("Missing input", "Please choose an input G-code file.")
            return

        try:
            settings = self._get_settings()
            result = flowfix_file(Path(ip), settings)

            self.log_entries[result["input_path"]] = result
            save_log(self.log_entries)
            self._refresh_list()

            messagebox.showinfo(
                "FlowFix complete",
                f"Created:\n{result['output_path']}\n\nLog updated:\n{LOG_PATH}",
            )
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _reload_log(self):
        self.log_entries = load_log()
        self._refresh_list()

    def _refresh_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        for key in sorted(self.log_entries.keys(), key=lambda s: s.lower()):
            e = self.log_entries[key]
            self.tree.insert(
                "",
                "end",
                values=(
                    e.get("input_path", ""),
                    e.get("output_path", ""),
                    e.get("timestamp", ""),
                    e.get("inserted_low_m221", ""),
                    e.get("inserted_restore_m221", ""),
                    e.get("total_layers_detected", ""),
                ),
            )


if __name__ == "__main__":
    App().mainloop()
