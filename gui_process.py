"""DropGain Process page (analyze, render, results table, waveform)."""

from __future__ import annotations

RESULTS_EMPTY_PLACEHOLDER = (
    "No tracks analyzed yet.\nSelect a source folder and click Analyze."
)

from typing import TYPE_CHECKING, Any

import tkinter as tk
from tkinter import scrolledtext, ttk

import customtkinter as ctk

from gui_utils import telemetry_caption
from gui_theme import (
    BG_FIELD,
    BG_MAIN,
    BG_PANEL,
    BORDER_COLOR,
    CARD_PAD,
    DASHBOARD_LAYOUT_COMPACT_WIDTH,
    DASHBOARD_LAYOUT_STACKED_WIDTH,
    ERROR_FG,
    FG_MAIN,
    FG_MUTED,
    ICE_FILL,
    ICE_SOFT,
    LOG_BG,
    METRIC_BG,
    METRIC_TILE_CORNER_RADIUS,
    METRIC_TILE_GAP,
    METRIC_TILE_HEIGHT,
    METRIC_TILE_LABEL_SIZE,
    METRIC_TILE_VALUE_SIZE,
    METRIC_TILE_WIDTH,
    OUTPUT_CONTENT_HEIGHT,
    OUTPUT_LOG_LINES,
    PAGE_PADX,
    PAGE_PADY_TOP,
    BUTTON_HEIGHT,
    PROCESS_ACTION_COLUMN_WIDTH,
    PROCESS_DASHBOARD_PROGRESS_GAP,
    PROGRESS_BAR_CORNER_RADIUS,
    PROGRESS_BAR_HEIGHT,
    PROCESS_SETTINGS_ROW_PADY,
    PROCESS_SETTINGS_WRAP_WIDTH,
    RESULTS_TABLE_COLUMNS,
    SECTION_GAP,
    SELECTION_BG,
    SIGNAL_PANEL_CORNER_RADIUS,
    SPACE_1,
    SPACE_2,
    SPACE_3,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_LABEL,
    TYPE_MICRO,
    WARN_FG,
    WAVEFORM_MIN_HEIGHT,
)
from gui_utils import TreeviewHeadingTooltip, logical_widget_width

if TYPE_CHECKING:
    from gui_tk import App


class ProcessPage(ctk.CTkFrame):
    """Current processing workflow: folder picker, actions, table, waveform/log."""

    _DASHBOARD_SIDE_UNIFORM = "dashboard_sides"
    _ACTIONS_HORIZONTAL_NUDGE = 120

    def __init__(self, app: App, master: Any) -> None:
        super().__init__(master, fg_color=BG_MAIN, corner_radius=0)
        self._app = app
        self._dashboard_mode = ""
        self._settings_wrap = PROCESS_SETTINGS_WRAP_WIDTH
        self._refresh_after_id: str | None = None
        self._align_after_id: str | None = None
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build(app)
        self.bind("<Configure>", self._on_page_configure, add="+")
        self._schedule_refresh_layout(idle=True)

    def refresh_layout(self) -> None:
        self._refresh_after_id = None
        self._update_dashboard_layout()
        self._schedule_align_dashboard_row(idle=True)

    def settle_layout_for_reveal(self) -> None:
        """Cancel deferred layout callbacks and settle the dashboard while hidden."""
        self._cancel_deferred_layout()
        self.update_idletasks()
        self._dashboard_mode = ""
        self._update_dashboard_layout()
        if self._align_after_id is not None:
            try:
                self.after_cancel(self._align_after_id)
            except Exception:
                pass
            self._align_after_id = None
        self.update_idletasks()
        self._align_dashboard_row()
        self.update_idletasks()

    def _cancel_deferred_layout(self) -> None:
        for attr in ("_refresh_after_id", "_align_after_id"):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)

    def _schedule_refresh_layout(self, *, idle: bool = False, delay_ms: int | None = None) -> None:
        if self._refresh_after_id is not None:
            try:
                self.after_cancel(self._refresh_after_id)
            except Exception:
                pass
            self._refresh_after_id = None
        if delay_ms is not None:
            self._refresh_after_id = self.after(delay_ms, self.refresh_layout)
        elif idle:
            self._refresh_after_id = self.after_idle(self.refresh_layout)
        else:
            self.refresh_layout()

    def _schedule_align_dashboard_row(self, *, idle: bool = False, delay_ms: int | None = None) -> None:
        if self._align_after_id is not None:
            try:
                self.after_cancel(self._align_after_id)
            except Exception:
                pass
            self._align_after_id = None
        if delay_ms is not None:
            self._align_after_id = self.after(delay_ms, self._align_dashboard_row)
        elif idle:
            self._align_after_id = self.after_idle(self._align_dashboard_row)
        else:
            self._align_dashboard_row()

    def _align_dashboard_row(self) -> None:
        self._align_after_id = None
        if self._dashboard_mode != "wide":
            self._folder_block.grid_configure(pady=0)
            self._metrics_column.grid_configure(pady=0, sticky="n")
            self._actions_block.grid_configure(pady=0, padx=0)
            return
        self.update_idletasks()
        folder_h = self._folder_block.winfo_reqheight()
        actions_h = self._actions_block.winfo_reqheight()
        if folder_h <= 1 or actions_h <= 1:
            self._schedule_align_dashboard_row(delay_ms=50)
            return
        row_h = max(folder_h, actions_h)
        folder_top = max(0, (row_h - folder_h) // 2)
        bubble_top = max(0, (row_h - METRIC_TILE_HEIGHT) // 2)
        actions_top = max(folder_top, (folder_top + bubble_top) // 2)
        self._folder_block.grid_configure(pady=(folder_top, 0))
        self._metrics_column.grid_configure(pady=(bubble_top, 0), sticky="nsew")

        bbox = self._dashboard.grid_bbox(0, 2)
        actions_w = self._actions_block.winfo_reqwidth()
        if bbox is None or actions_w <= 1:
            self._schedule_align_dashboard_row(delay_ms=50)
            return
        _x, _y, col2_w, _h = bbox
        pad_left = max(0, (col2_w - actions_w) // 2 + self._ACTIONS_HORIZONTAL_NUDGE)
        self._actions_block.grid_configure(sticky="nw", pady=(actions_top, 0), padx=(pad_left, 0))

    def _on_page_configure(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._update_dashboard_layout()
        self._schedule_align_dashboard_row(idle=True)

    def _update_dashboard_layout(self) -> None:
        logical_w = logical_widget_width(self)
        if logical_w <= 0:
            self._schedule_refresh_layout(delay_ms=50)
            return
        if logical_w < DASHBOARD_LAYOUT_STACKED_WIDTH:
            mode = "stacked"
        elif logical_w < DASHBOARD_LAYOUT_COMPACT_WIDTH:
            mode = "compact"
        else:
            mode = "wide"
        if mode == self._dashboard_mode:
            return
        self._dashboard_mode = mode
        self._apply_dashboard_layout(mode)

    def _layout_metrics_column(self, mode: str) -> None:
        bubbles = self._metrics_bubbles_host
        settings = self._metrics_settings
        column = self._metrics_column

        bubbles.pack_forget()
        settings.pack_forget()
        bubbles.grid_forget()
        settings.grid_forget()

        if mode == "wide":
            column.grid_rowconfigure(1, weight=1)
            column.grid_columnconfigure(0, weight=1)
            bubbles.grid(row=0, column=0, sticky="n")
            settings.grid(
                row=2,
                column=0,
                sticky="s",
                pady=(PROCESS_SETTINGS_ROW_PADY, 0),
            )
            return

        column.grid_rowconfigure(1, weight=0)
        bubbles.pack()
        settings.pack(pady=(PROCESS_SETTINGS_ROW_PADY, 0))

    def _apply_dashboard_layout(self, mode: str) -> None:
        folder = self._folder_block
        metrics = self._metrics_column
        actions = self._actions_block
        dashboard = self._dashboard

        folder.grid_forget()
        actions.grid_forget()
        metrics.grid_forget()

        for column in range(3):
            dashboard.grid_columnconfigure(column, weight=0, uniform="")

        side_uniform = self._DASHBOARD_SIDE_UNIFORM
        dashboard.grid_columnconfigure(0, weight=1, uniform=side_uniform)
        dashboard.grid_columnconfigure(1, weight=0)
        dashboard.grid_columnconfigure(2, weight=1, uniform=side_uniform)

        if mode == "wide":
            folder.grid(row=0, column=0, sticky="nw", padx=0, pady=0)
            metrics.grid(row=0, column=1, sticky="nsew", padx=(4, 4), pady=0)
            actions.grid(row=0, column=2, sticky="nw", padx=0, pady=0)
            self._layout_metrics_column(mode)
            self._schedule_align_dashboard_row(idle=True)
            return

        if mode == "compact":
            folder.grid(row=0, column=0, sticky="nw", padx=0, pady=0)
            actions.grid(row=0, column=2, sticky="n", padx=0, pady=0)
            metrics.grid(row=1, column=1, sticky="n", pady=(12, 0))
            self._layout_metrics_column(mode)
            return

        folder.grid(row=0, column=0, columnspan=3, sticky="ew", padx=0, pady=0)
        metrics.grid(row=1, column=1, sticky="n", pady=(12, 0))
        actions.grid(row=2, column=0, columnspan=3, sticky="ew", padx=0, pady=(12, 0))
        self._layout_metrics_column(mode)

    def _build(self, app: App) -> None:
        command_bar = ctk.CTkFrame(self, fg_color=BG_MAIN, corner_radius=0)
        command_bar.grid(row=0, column=0, sticky="ew", padx=PAGE_PADX, pady=(PAGE_PADY_TOP, PROCESS_DASHBOARD_PROGRESS_GAP))
        command_bar.grid_columnconfigure(0, weight=1)

        dashboard = ctk.CTkFrame(command_bar, fg_color="transparent")
        dashboard.grid(row=0, column=0, sticky="ew")
        self._dashboard = dashboard
        side_uniform = self._DASHBOARD_SIDE_UNIFORM
        dashboard.grid_columnconfigure(0, weight=1, uniform=side_uniform)
        dashboard.grid_columnconfigure(1, weight=0)
        dashboard.grid_columnconfigure(2, weight=1, uniform=side_uniform)

        folder_block = ctk.CTkFrame(dashboard, fg_color="transparent")
        self._folder_block = folder_block
        folder_entry_width = app._folder_entry_width_px()
        app._label(folder_block, text="Source folder", color=FG_MUTED, bg=BG_MAIN, size=TYPE_BODY).grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        folder_input_row = ctk.CTkFrame(folder_block, fg_color="transparent")
        folder_input_row.grid(row=1, column=0, sticky="w")
        app.entry_folder = app._entry(
            folder_input_row,
            textvariable=app.var_folder,
            width=folder_entry_width,
        )
        app.entry_folder.grid(row=0, column=0, sticky="w", padx=(0, 6))
        app.lbl_source_folder_status = app._label(
            folder_input_row, text="", size=TYPE_BODY, weight="bold", anchor="center", bg=BG_MAIN
        )
        app.lbl_source_folder_status.configure(width=18, height=BUTTON_HEIGHT)
        app.lbl_source_folder_status.grid(row=0, column=1, padx=(0, 6))
        app.btn_browse_folder = app._button(folder_input_row, text="Browse", command=app._pick_folder)
        app.btn_browse_folder.grid(row=0, column=2, sticky="w")

        app._label(folder_block, text="Output folder", color=FG_MUTED, bg=BG_MAIN, size=TYPE_BODY).grid(
            row=2, column=0, sticky="w", pady=(10, 6)
        )
        output_input_row = ctk.CTkFrame(folder_block, fg_color="transparent")
        output_input_row.grid(row=3, column=0, sticky="w")
        app.entry_output_folder = app._entry(
            output_input_row,
            textvariable=app.var_output_folder,
            width=folder_entry_width,
        )
        app.entry_output_folder.grid(row=0, column=0, sticky="w", padx=(0, 6))
        app.lbl_output_folder_status = app._label(
            output_input_row, text="", size=TYPE_BODY, weight="bold", anchor="center", bg=BG_MAIN
        )
        app.lbl_output_folder_status.configure(width=18, height=BUTTON_HEIGHT)
        app.lbl_output_folder_status.grid(row=0, column=1, padx=(0, 6))
        app.btn_browse_output_folder = app._button(
            output_input_row, text="Browse", command=app._pick_output_folder
        )
        app.btn_browse_output_folder.grid(row=0, column=2, sticky="w")
        app._add_tooltip(
            output_input_row,
            "Optional. Leave empty to save processed copies beside originals. "
            "When set, subfolders from the source folder are preserved under this path.",
            wraplength=420,
        )

        metrics_column = ctk.CTkFrame(dashboard, fg_color="transparent")
        self._metrics_column = metrics_column

        metrics_bubbles_host = ctk.CTkFrame(metrics_column, fg_color="transparent")
        self._metrics_bubbles_host = metrics_bubbles_host

        metrics_row = ctk.CTkFrame(metrics_bubbles_host, fg_color="transparent")
        metrics_row.pack()

        app.var_metric_would = tk.StringVar(value="0")
        app.var_metric_processed = tk.StringVar(value="0")
        app.var_metric_warnings = tk.StringVar(value="0")
        app.var_metric_errors = tk.StringVar(value="0")
        app._summary_chip_labels: dict[str, ctk.CTkLabel] = {}
        app._summary_metric_tiles: dict[str, ctk.CTkFrame] = {}
        tile_specs = (
            ("would", "Would process", app.var_metric_would, FG_MAIN),
            ("processed", "Processed", app.var_metric_processed, FG_MAIN),
            ("warnings", "Warnings", app.var_metric_warnings, WARN_FG),
            ("errors", "Errors", app.var_metric_errors, ERROR_FG),
        )
        for index, (chip_id, label, variable, value_color) in enumerate(tile_specs):
            tile = ctk.CTkFrame(
                metrics_row,
                fg_color=METRIC_BG,
                border_color=BORDER_COLOR,
                border_width=1,
                corner_radius=METRIC_TILE_CORNER_RADIUS,
                width=METRIC_TILE_WIDTH,
                height=METRIC_TILE_HEIGHT,
            )
            tile.grid(row=0, column=index, padx=(0 if index == 0 else METRIC_TILE_GAP, 0))
            tile.grid_propagate(False)
            value_label = app._label(
                tile,
                textvariable=variable,
                color=value_color,
                bg=METRIC_BG,
                size=METRIC_TILE_VALUE_SIZE,
                display=True,
                anchor="center",
            )
            value_label.place(relx=0.5, rely=0.36, anchor="center")
            app._label(
                tile,
                text=label,
                color=FG_MUTED,
                bg=METRIC_BG,
                size=METRIC_TILE_LABEL_SIZE,
                accent=True,
                anchor="center",
            ).place(relx=0.5, rely=0.76, anchor="center")
            app._summary_chip_labels[chip_id] = value_label
            app._summary_metric_tiles[chip_id] = tile

        metrics_settings = ctk.CTkFrame(metrics_column, fg_color="transparent")
        self._metrics_settings = metrics_settings
        settings_wrap = self._settings_wrap
        settings_line = ctk.CTkFrame(metrics_settings, fg_color="transparent")
        settings_line.pack(anchor="center")
        app._label(
            settings_line,
            text=telemetry_caption("ACTIVE SETTINGS"),
            color=FG_MUTED,
            bg=BG_MAIN,
            size=TYPE_MICRO,
            weight="bold",
            mono=True,
        ).pack(side="left", padx=(0, 12))
        app.lbl_process_settings_summary = app._label(
            settings_line,
            textvariable=app.var_process_settings_summary,
            color=FG_MUTED,
            bg=BG_MAIN,
            size=TYPE_MICRO,
            wraplength=settings_wrap,
            justify="left",
            anchor="w",
        )
        app.lbl_process_settings_summary.pack(side="left")

        def _on_metrics_settings_configure(event: tk.Event) -> None:
            if event.widget is not metrics_settings:
                return
            app.lbl_process_settings_summary.configure(
                wraplength=max(int(event.width) - 108, 160)
            )

        metrics_settings.bind("<Configure>", _on_metrics_settings_configure, add="+")
        app._refresh_settings_summary()

        actions_block = ctk.CTkFrame(dashboard, fg_color="transparent")
        self._actions_block = actions_block
        app.btn_batch = app._button(
            actions_block,
            text="Analyze + Create Copies",
            command=app._start_batch,
            accent=True,
        )
        app.btn_batch.pack(fill="x", pady=(0, 6))
        app.btn_batch.configure(width=PROCESS_ACTION_COLUMN_WIDTH)
        app.btn_analyze_only = app._button(
            actions_block,
            text="Analyze Only",
            command=app._start_analyze_only,
        )
        app.btn_analyze_only.pack(fill="x", pady=(0, 6))
        app.btn_analyze_only.configure(width=PROCESS_ACTION_COLUMN_WIDTH)
        app.btn_start = app._button(
            actions_block,
            text="Render Analyzed",
            command=app._start_processing,
            state="disabled",
        )
        app.btn_start.pack(fill="x", pady=(0, 6))
        app.btn_start.configure(width=PROCESS_ACTION_COLUMN_WIDTH)
        utility_row = ctk.CTkFrame(actions_block, fg_color="transparent")
        utility_row.pack(fill="x")
        app.btn_cancel = app._button(utility_row, text="Cancel", command=app._cancel, state="disabled")
        app.btn_cancel.pack(side="left", fill="x", expand=True, padx=(0, 4))
        app.btn_open_output = app._button(
            utility_row, text="Open Output", command=app._open_output_folder, state="disabled"
        )
        app.btn_open_output.pack(side="left", fill="x", expand=True, padx=(0, 4))
        app.btn_open_csv = app._button(utility_row, text="Open CSV", command=app._open_csv, state="disabled")
        app.btn_open_csv.pack(side="left", fill="x", expand=True)

        self._dashboard_mode = ""
        self._apply_dashboard_layout("wide")
        self._dashboard_mode = "wide"
        self._layout_metrics_column("wide")
        self.bind("<Map>", self._on_page_map, add="+")

        app.var_run_summary = tk.StringVar(value=app._format_run_counts(app._run_counts))

        progress_frame = ctk.CTkFrame(self, fg_color=BG_MAIN, corner_radius=0)
        progress_frame.grid(row=1, column=0, sticky="ew", padx=PAGE_PADX, pady=(0, SECTION_GAP))
        progress_frame.grid_columnconfigure(0, weight=1)

        header_row = ctk.CTkFrame(progress_frame, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", pady=(0, 3))
        header_row.grid_columnconfigure(0, weight=1)

        app.var_operation_phase = tk.StringVar(value=telemetry_caption("Ready"))
        app.var_operation_fraction = tk.StringVar(value="")
        app._label(
            header_row,
            textvariable=app.var_operation_phase,
            color=FG_MAIN,
            bg=BG_MAIN,
            size=TYPE_BODY,
            weight="bold",
            anchor="w",
            mono=True,
        ).grid(row=0, column=0, sticky="w")
        app._label(
            header_row,
            textvariable=app.var_operation_fraction,
            color=FG_MUTED,
            bg=BG_MAIN,
            size=TYPE_BODY,
            anchor="e",
            mono=True,
        ).grid(row=0, column=1, sticky="e")

        app.progress = ctk.CTkProgressBar(
            progress_frame,
            fg_color=BG_FIELD,
            progress_color=ICE_FILL,
            border_color=BORDER_COLOR,
            border_width=1,
            height=PROGRESS_BAR_HEIGHT,
            corner_radius=PROGRESS_BAR_CORNER_RADIUS,
        )
        app.progress.grid(row=1, column=0, sticky="ew")
        app.progress.set(0)

        app.var_status = tk.StringVar(value=telemetry_caption("Ready."))

        review_card = app._card(self, BG_PANEL, (CARD_PAD, CARD_PAD))
        review_card.grid(row=2, column=0, sticky="nsew", padx=PAGE_PADX, pady=(0, SECTION_GAP))
        review = app._inner(review_card)
        review.grid_columnconfigure(0, weight=1)
        review.grid_rowconfigure(0, weight=1)

        columns = tuple(column_id for column_id, _heading, _anchor, _sample, _tooltip in RESULTS_TABLE_COLUMNS)
        app.results_table = ttk.Treeview(review, columns=columns, show="headings", height=18, style="DropGain.Treeview")
        for column_id, heading, _anchor, _sample, _tooltip in RESULTS_TABLE_COLUMNS:
            app.results_table.heading(column_id, text=heading)
        app._results_table_heading_tooltip = TreeviewHeadingTooltip(
            app.results_table,
            {
                column_id: tooltip
                for column_id, _heading, _anchor, _sample, tooltip in RESULTS_TABLE_COLUMNS
            },
        )
        app._resize_results_table_columns()
        app.results_table.grid(row=0, column=0, sticky="nsew")
        review_scroll_y = ttk.Scrollbar(
            review, orient="vertical", command=app.results_table.yview, style="DropGain.Vertical.TScrollbar"
        )
        review_scroll_y.grid(row=0, column=1, sticky="ns")
        review_scroll_x = ttk.Scrollbar(
            review, orient="horizontal", command=app.results_table.xview, style="DropGain.Horizontal.TScrollbar"
        )
        review_scroll_x.grid(row=1, column=0, sticky="ew")
        app.results_table.configure(yscrollcommand=review_scroll_y.set, xscrollcommand=review_scroll_x.set)
        app.results_table.bind("<<TreeviewSelect>>", app._handle_results_table_select, add="+")
        app.results_table.bind("<Configure>", app._resize_results_table_columns, add="+")
        app._configure_results_table_tags()

        app.var_results_empty = tk.StringVar(value=RESULTS_EMPTY_PLACEHOLDER)
        app.results_empty_label = app._label(
            review,
            textvariable=app.var_results_empty,
            color=FG_MUTED,
            bg=BG_PANEL,
            size=TYPE_BODY,
            anchor="center",
            justify="center",
        )
        app.results_empty_label.grid(row=0, column=0, sticky="nsew")
        app._update_results_empty_state(has_rows=False)

        output_card = app._card(self, BG_PANEL, (CARD_PAD, SPACE_2))
        output_card.grid(row=3, column=0, sticky="ew", padx=PAGE_PADX, pady=(0, SPACE_3))
        output = app._inner(output_card)
        output.grid_columnconfigure(0, weight=1)
        output.grid_rowconfigure(1, weight=0)

        output_header = ctk.CTkFrame(output, fg_color="transparent")
        output_header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        output_header.grid_columnconfigure(1, weight=1)

        output_tab_buttons = ctk.CTkFrame(output_header, fg_color="transparent")
        output_tab_buttons.grid(row=0, column=0, sticky="w")
        app.btn_output_waveform = ctk.CTkButton(
            output_tab_buttons,
            text="Waveform",
            width=80,
            height=26,
            command=lambda: app._show_output_tab("Waveform"),
        )
        app.btn_output_waveform.grid(row=0, column=0, padx=(0, SPACE_1))
        app._register_tab_button(app.btn_output_waveform, active=True)
        app.btn_output_log = ctk.CTkButton(
            output_tab_buttons,
            text="Log",
            width=52,
            height=26,
            command=lambda: app._show_output_tab("Log"),
        )
        app.btn_output_log.grid(row=0, column=1)
        app._register_tab_button(app.btn_output_log, active=False)

        app.var_waveform_title = tk.StringVar(value="Select an analyzed track to preview its waveform.")
        app.var_waveform_stats = tk.StringVar(value="")
        app.output_track_info = ctk.CTkFrame(output_header, fg_color="transparent")
        app.output_track_info.grid(row=0, column=1, sticky="e", padx=(SPACE_2, 0))
        app.lbl_waveform_stats = app._label(
            app.output_track_info,
            textvariable=app.var_waveform_stats,
            color=FG_MUTED,
            bg=BG_PANEL,
            size=TYPE_LABEL,
        )
        app.lbl_waveform_stats.pack(side="right")
        app.lbl_waveform_title = app._label(
            app.output_track_info,
            textvariable=app.var_waveform_title,
            bg=BG_PANEL,
            size=TYPE_BODY,
            weight="bold",
            anchor="e",
        )
        app.lbl_waveform_title.pack(side="right", padx=(0, SPACE_2))

        app.output_content = ctk.CTkFrame(
            output,
            fg_color=LOG_BG,
            corner_radius=SIGNAL_PANEL_CORNER_RADIUS,
            border_color=BORDER_COLOR,
            border_width=1,
        )
        app.output_content.grid(row=1, column=0, sticky="ew")
        app.output_content.configure(height=OUTPUT_CONTENT_HEIGHT)
        app.output_content.grid_propagate(False)
        app.output_content.grid_columnconfigure(0, weight=1)
        app.output_content.grid_rowconfigure(0, weight=1)

        app.waveform_panel = ctk.CTkFrame(
            app.output_content,
            fg_color=LOG_BG,
            corner_radius=SIGNAL_PANEL_CORNER_RADIUS,
        )
        app.waveform_panel.grid(row=0, column=0, sticky="nsew")
        app.waveform_panel.grid_columnconfigure(0, weight=1)
        app.waveform_panel.grid_rowconfigure(0, weight=1)

        app.waveform_canvas = tk.Canvas(
            app.waveform_panel,
            bg=LOG_BG,
            highlightthickness=0,
            bd=0,
            relief="flat",
            height=max(WAVEFORM_MIN_HEIGHT, OUTPUT_CONTENT_HEIGHT),
        )
        app.waveform_canvas.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        app.waveform_canvas.bind("<Configure>", app._on_waveform_canvas_configure, add=True)
        app.waveform_canvas.bind("<Motion>", app._on_waveform_canvas_motion, add=True)
        app.waveform_canvas.bind("<Leave>", app._on_waveform_canvas_leave, add=True)
        app.waveform_display = app.waveform_canvas

        app.log_panel = ctk.CTkFrame(
            app.output_content,
            fg_color=LOG_BG,
            corner_radius=SIGNAL_PANEL_CORNER_RADIUS,
        )
        app.log_panel.grid(row=0, column=0, sticky="nsew")
        app.log_panel.grid_columnconfigure(0, weight=1)
        app.log_panel.grid_rowconfigure(0, weight=1)
        app.log = scrolledtext.ScrolledText(
            app.log_panel,
            state="disabled",
            wrap="none",
            height=OUTPUT_LOG_LINES,
            font=("Cascadia Mono", TYPE_MICRO),
            relief="flat",
            bg=LOG_BG,
            fg=FG_MAIN,
            insertbackground=FG_MAIN,
            selectbackground=SELECTION_BG,
            selectforeground=FG_MAIN,
            highlightthickness=0,
            borderwidth=0,
        )
        app.log.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        app.log.tag_config("error", foreground=ERROR_FG)
        app.log.tag_config("good", foreground=ICE_SOFT)
        app.log.tag_config("warn", foreground=WARN_FG)
        app._show_output_tab("Waveform")

        app._validate_paths()
        app._update_summary_cards()

    def _on_page_map(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._dashboard_mode = ""
        self._schedule_refresh_layout(idle=True)
