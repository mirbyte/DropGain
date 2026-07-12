"""
DropGain CustomTkinter GUI.

The GUI owns user interaction, progress display, threading, and settings.
Background job execution (including audio processing and CSV report writing)
is delegated to jobs.py.
"""

from __future__ import annotations

import faulthandler
import json
import logging
import logging.handlers
import math
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from typing import Any, Callable
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import customtkinter as ctk
except ImportError as exc:
    raise RuntimeError(
        "Required Python package customtkinter was not found.\n\n"
        "Install it with:\n\n"
        "    pip install customtkinter\n\n"
        "Then start DropGain again."
    ) from exc

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
except ImportError as exc:
    raise RuntimeError(
        "Required Python package Pillow was not found.\n\n"
        "Install it with:\n\n"
        "    pip install pillow\n\n"
        "Then start DropGain again."
    ) from exc

from analysis import (
    APP_TITLE,
    DEFAULT_BOOST_PEAK_CEILING_DBFS,
    DEFAULT_LIMITER_ENGINE,
    DEFAULT_LOUD_SECTION_HOP_SECONDS,
    DEFAULT_LOUD_SECTION_WINDOW_SECONDS,
    DEFAULT_MAX_REDUCTION_DB,
    DEFAULT_BASS_MAX_BOOST_REDUCTION_DB,
    MIN_BASS_MAX_BOOST_REDUCTION_DB,
    MAX_BASS_MAX_BOOST_REDUCTION_DB,
    DEFAULT_BASS_PENALTY_START_DB,
    DEFAULT_BASS_PENALTY_FULL_DB,
    DEFAULT_SUB_PENALTY_START_DB,
    DEFAULT_SUB_PENALTY_FULL_DB,
    MIN_BASS_PENALTY_THRESHOLD_DB,
    MAX_BASS_PENALTY_THRESHOLD_DB,
    DEFAULT_NORMALIZATION_MODE,
    DEFAULT_APPLY_RENDER_GAIN_THRESHOLD,
    DEFAULT_OUTPUT_FORMAT_MODE,
    DEFAULT_TARGET_HIGH_LUFS,
    DEFAULT_TARGET_LOW_LUFS,
    DEFAULT_ANALYSIS_WORKER_THREADS,
    DEFAULT_RENDER_WORKER_THREADS,
    LIMITER_ENGINE_LOUDMAX,
    LOSSLESS_MIN_ABS_GAIN_DB,
    MAX_ANALYSIS_WORKER_THREADS,
    MIN_ANALYSIS_WORKER_THREADS,
    MP3_MIN_ABS_GAIN_DB,
    OUTPUT_FORMAT_ALL_TO_MP3,
    OUTPUT_FORMAT_ALL_TO_AIFF,
    OUTPUT_FORMAT_MP3_TO_AIFF,
    OUTPUT_FORMAT_PRESERVE,
    PROCESSED_SUFFIX,
    benchmark_timer,
    check_ffmpeg_available,
    default_csv_path,
    format_peak_control_display,
    hidden_subprocess_kwargs,
    normalize_limiter_engine,
    normalize_normalization_mode,
    normalize_output_format_mode,
    output_format_mode_description,
    output_format_mode_ui_hint,
    script_folder,
)
from jobs import (
    AnalyzedWorkItem,
    DropGainSettings,
    eligible_render_indices,
    refresh_analyzed_render_statuses,
    recompute_rows_for_settings,
    run_analysis_job,
    run_batch_job,
    run_processing_job,
)
from processing import (
    find_loudmax_plugin_path,
    find_prol2_plugin_path,
    shutdown_prol2_render_host,
    verify_loudmax_plugin,
    verify_prol2_plugin,
)
from gui_waveform import WaveformMixin
from gui_process import RESULTS_EMPTY_PLACEHOLDER
from gui_theme import *  # noqa: F403
from gui_utils import (  # noqa: F401
    ContentFadeTransition,
    DropGainTooltip,
    GuiQueueLogHandler,
    TreeviewHeadingTooltip,
    apply_hand_cursor,
    enable_windows_dpi_awareness,
    fit_window_bounds,
    logical_screen_size,
    logical_widget_width,
    make_tooltip_label,
    pointer_inside_widget,
    position_tooltip_window,
    scaled_px,
    scaled_px_from_float,
    telemetry_caption,
    treeview_column_width_px,
    treeview_rowheight_px,
    ui_scale_for,
    wire_ctk_button_press,
)

GUI_TICK_MIN_INTERVAL_SEC = 0.35
PROGRESS_TWEEN_INTERVAL_MS = 16
PROGRESS_TWEEN_DURATION_SEC = 0.2
SETTING_CHANGE_DEBOUNCE_MS = 200
SAVE_SETTINGS_DEBOUNCE_MS = 500
RESULTS_TABLE_RESIZE_DEBOUNCE_MS = 50
SETTINGS_FILE_NAME = "dropgain_settings.json"
SETTINGS_SCHEMA_VERSION = 1

LOG_FILE_NAME = "dropgain.log"
CRASH_LOG_FILE_NAME = "dropgain_crash.log"
START_MAXIMIZED = True


def enable_crash_diagnostics() -> None:
    """Write hard-crash tracebacks to a log file next to the app."""
    crash_log = script_folder() / CRASH_LOG_FILE_NAME
    try:
        handle = open(crash_log, "a", encoding="utf-8")
    except OSError:
        return
    try:
        faulthandler.enable(file=handle, all_threads=True)
    except Exception:
        try:
            handle.close()
        except OSError:
            pass

RUN_COUNT_KEYS = (
    "processed",
    "would_process",
    "analyzed_only",
    "skipped",
    "warnings",
    "errors",
)


class App(WaveformMixin, ctk.CTk):
    def __init__(self) -> None:
        ctk.set_appearance_mode("dark")
        super().__init__(fg_color=BG_MAIN)

        self.title(APP_TITLE)
        self._last_ui_scale = ui_scale_for(self)
        self._dpi_refresh_after_id: str | None = None
        self._app_icon: ImageTk.PhotoImage | None = None

        self._settings = self._load_settings()
        self._run_counts = self._empty_run_counts()
        self._analyzed_rows: list[dict[str, object]] = []
        self._analyzed_work_items: dict[str, AnalyzedWorkItem] = {}
        self._analysis_signature: dict[str, object] | None = None
        self._active_run_signature: dict[str, object] | None = None
        self._progress_max = 1
        self._active_pipeline: str | None = None
        self._active_phase = "idle"
        self._run_completed = False
        self._progress_done = 0
        self._progress_total = 1
        self._batch_analysis_total = 0
        self._batch_analysis_done = 0
        self._batch_render_total = 0
        self._batch_render_done = 0
        self._busy_button: ctk.CTkButton | None = None
        self._busy_button_idle_text = ""
        self._operation_started_at: float | None = None
        self._operation_elapsed_after_id: str | None = None
        self._operation_last_rate = 0.0
        self._operation_last_eta = ""
        self._operation_last_errors = 0
        self._progress_target = 0.0
        self._progress_display = 0.0
        self._progress_tween_after_id: str | None = None
        self._progress_indeterminate = False
        self._building_ui = True
        self._suspend_setting_traces = False
        self._analysis_setting_after_id: str | None = None
        self._decision_setting_after_id: str | None = None
        self._render_rule_setting_after_id: str | None = None
        self._save_settings_after_id: str | None = None
        self._results_table_resize_after_id: str | None = None
        self._main_page_visuals_dirty = False
        self._init_waveform_state()

        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._log_record_queue: queue.Queue[logging.LogRecord] = queue.Queue()
        self._cancel_flag = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._close_requested = False
        self._previous_thread_excepthook: Callable[[threading.ExceptHookArgs], Any] | None = None
        self._panel_fade = ContentFadeTransition(self)
        self._logger = logging.getLogger("dropgain")
        self._log_listener: logging.handlers.QueueListener | None = None
        self.preferences_page = None
        self.lbl_output_format_hint = None
        self.mode_menu = None
        self.limiter_engine_menu = None
        self.output_format_menu = None
        self.chk_allow_risky_true_peak_boost = None
        self.chk_apply_render_gain_threshold = None
        self.chk_write_csv = None
        self._number_inputs: list[Any] = []

        self._init_settings_variables()
        self._configure_logging()
        self._install_exception_handlers()
        self._configure_treeview_style()
        self._build_ui()
        self._building_ui = False
        self._install_window_icon()
        self._wire_setting_traces()
        self._validate_paths()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._close_app)
        self.bind("<Configure>", self._on_root_configure_for_dpi, add="+")

        self.update_idletasks()
        actual_scale = self._ui_scale()
        if abs(actual_scale - self._last_ui_scale) >= 0.05:
            self._last_ui_scale = actual_scale
            self._configure_treeview_style()
            if hasattr(self, "results_table"):
                try:
                    self._resize_results_table_columns()
                except Exception:
                    pass
        self._apply_window_bounds(reposition=True)
        if START_MAXIMIZED:
            self.after(0, self._maximize_window)

        if os.path.exists(default_csv_path(self.var_folder.get().strip())):
            self._apply_action_button_state(self.btn_open_csv, "normal")

        self._logger.info("Logging to %s", script_folder() / LOG_FILE_NAME)

    # ---------------------------------------------------------------------
    # Settings and setup
    # ---------------------------------------------------------------------

    def _maximize_window(self) -> None:
        try:
            self.state("zoomed")
            return
        except tk.TclError:
            pass

        try:
            self.attributes("-zoomed", True)
            return
        except tk.TclError:
            pass

        self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")

    def _apply_window_bounds(self, *, reposition: bool = False) -> None:
        width, height, min_w, min_h = fit_window_bounds(self)
        self.minsize(min_w, min_h)
        if not reposition:
            return
        if self._is_window_maximized():
            return
        screen_height = self.winfo_screenheight()
        x = (self.winfo_screenwidth() - width) // 2
        y = max(0, (screen_height - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _is_window_maximized(self) -> bool:
        try:
            if str(self.state()) == "zoomed":
                return True
        except tk.TclError:
            pass
        try:
            return bool(self.attributes("-zoomed"))
        except tk.TclError:
            return False

    def _ui_scale(self) -> float:
        return ui_scale_for(self)

    def _scaled(self, value: float | int) -> int:
        return scaled_px(self, value)

    def _on_root_configure_for_dpi(self, _event: tk.Event | None = None) -> None:
        if self._dpi_refresh_after_id is not None:
            try:
                self.after_cancel(self._dpi_refresh_after_id)
            except Exception:
                pass
        self._dpi_refresh_after_id = self.after(200, self._refresh_scaled_ui)

    def _refresh_scaled_ui(self) -> None:
        self._dpi_refresh_after_id = None
        new_scale = self._ui_scale()
        if abs(new_scale - self._last_ui_scale) < 0.05:
            return

        self._last_ui_scale = new_scale
        self._apply_window_bounds()
        self._configure_treeview_style()
        if hasattr(self, "results_table"):
            try:
                self._resize_results_table_columns()
            except Exception:
                pass
        if hasattr(self, "waveform_canvas"):
            try:
                if self._current_waveform_data is not None:
                    self._draw_waveform_canvas()
            except Exception:
                pass
        if self.library_tuning_page is not None:
            try:
                self.library_tuning_page._redraw_charts()
            except Exception:
                pass
        try:
            self._update_folder_entry_width()
        except Exception:
            pass
        process_page = getattr(self, "process_page", None)
        if process_page is not None:
            try:
                process_page.refresh_layout()
            except Exception:
                pass
        if self.preferences_page is not None:
            try:
                self.preferences_page.refresh_layout()
            except Exception:
                pass

    @staticmethod
    def _empty_run_counts() -> dict[str, int]:
        return {key: 0 for key in RUN_COUNT_KEYS}

    @staticmethod
    def _format_run_counts(counts: dict[str, int]) -> str:
        return (
            f"Processed: {counts.get('processed', 0)} | "
            f"Would process: {counts.get('would_process', 0)} | "
            f"Analyzed only: {counts.get('analyzed_only', 0)} | "
            f"Skipped: {counts.get('skipped', 0)} | "
            f"Warnings: {counts.get('warnings', 0)} | "
            f"Errors: {counts.get('errors', 0)}"
        )

    @staticmethod
    def _settings_path() -> str:
        return str(script_folder() / SETTINGS_FILE_NAME)

    @staticmethod
    def _setting_float(settings: dict[str, Any], key: str, default: float) -> float:
        try:
            return float(settings.get(key, default))
        except Exception:
            return default

    @staticmethod
    def _setting_int(settings: dict[str, Any], key: str, default: int) -> int:
        try:
            return int(float(settings.get(key, default)))
        except Exception:
            return default

    @staticmethod
    def _setting_bool(settings: dict[str, Any], key: str, default: bool) -> bool:
        try:
            value = settings.get(key, default)
            if isinstance(value, bool):
                return value
            return str(value).lower() in {"true", "1", "yes"}
        except Exception:
            return default

    def _upgrade_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        upgraded = dict(settings)

        if "output_format_mode" not in upgraded:
            upgraded["output_format_mode"] = DEFAULT_OUTPUT_FORMAT_MODE
        upgraded["output_format_mode"] = normalize_output_format_mode(upgraded.get("output_format_mode"))

        if "apply_render_gain_threshold" not in upgraded:
            upgraded["apply_render_gain_threshold"] = DEFAULT_APPLY_RENDER_GAIN_THRESHOLD

        upgraded["settings_schema_version"] = SETTINGS_SCHEMA_VERSION
        return upgraded

    def _load_settings(self) -> dict[str, Any]:
        path = self._settings_path()
        try:
            if not os.path.exists(path):
                return {}
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return self._upgrade_settings(data)
        except Exception:
            pass

        return {}

    def _schedule_debounced(self, attr_name: str, callback: Callable[[], None], delay_ms: int) -> None:
        after_id = getattr(self, attr_name, None)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        setattr(self, attr_name, self.after(delay_ms, callback))

    def _setting_change_blocked(self) -> bool:
        return (
            getattr(self, "_building_ui", False)
            or getattr(self, "_suspend_setting_traces", False)
            or self._is_run_busy()
        )

    def _schedule_save_settings(self) -> None:
        if not hasattr(self, "var_folder"):
            return
        self._schedule_debounced(
            "_save_settings_after_id",
            self._save_settings,
            SAVE_SETTINGS_DEBOUNCE_MS,
        )

    def _save_settings(self) -> None:
        self._save_settings_after_id = None
        if not hasattr(self, "var_folder"):
            return

        try:
            data = {
                "settings_schema_version": SETTINGS_SCHEMA_VERSION,
                "last_folder": self.var_folder.get().strip(),
                "last_output_folder": self.var_output_folder.get().strip(),
                "target_low": float(self.var_target_low.get()),
                "target_high": float(self.var_target_high.get()),
                "window_seconds": float(self.var_window.get()),
                "hop_seconds": float(self.var_hop.get()),
                "workers": int(float(self.var_workers.get())),
                "max_reduction": float(self.var_max_reduction.get()),
                "bass_max_reduction": float(self.var_bass_max_reduction.get()),
                "bass_penalty_start": float(self.var_bass_penalty_start.get()),
                "bass_penalty_full": float(self.var_bass_penalty_full.get()),
                "sub_penalty_start": float(self.var_sub_penalty_start.get()),
                "sub_penalty_full": float(self.var_sub_penalty_full.get()),
                "peak_ceiling": float(self.var_peak_ceiling.get()),
                "normalization_mode": normalize_normalization_mode(self.var_normalization_mode.get()),
                "limiter_engine": normalize_limiter_engine(self.var_limiter_engine.get()),
                "mp3_threshold": float(self.var_mp3_threshold.get()),
                "lossless_threshold": float(self.var_lossless_threshold.get()),
                "output_format_mode": normalize_output_format_mode(self.var_output_format_mode.get()),
                "allow_risky_true_peak_boost": bool(self.var_allow_risky_true_peak_boost.get()),
                "apply_render_gain_threshold": bool(self.var_apply_render_gain_threshold.get()),
                "write_csv": bool(self.var_write_csv.get()),
            }
        except Exception:
            return

        try:
            with open(self._settings_path(), "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
        except Exception:
            self._logger.exception("Failed to save settings")

    def _configure_logging(self) -> None:
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.handlers.clear()

        queue_handler = logging.handlers.QueueHandler(self._log_record_queue)
        self._logger.addHandler(queue_handler)

        gui_handler = GuiQueueLogHandler(self._queue)
        gui_handler.setLevel(logging.INFO)
        gui_handler.setFormatter(logging.Formatter("%(message)s"))

        file_handler = logging.FileHandler(script_folder() / LOG_FILE_NAME, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s")
        )

        self._log_listener = logging.handlers.QueueListener(
            self._log_record_queue,
            gui_handler,
            file_handler,
            respect_handler_level=True,
        )
        self._log_listener.start()

    def _shutdown_logging(self) -> None:
        if self._log_listener is not None:
            self._log_listener.stop()
            self._log_listener = None
        self._logger.handlers.clear()

    def _install_exception_handlers(self) -> None:
        previous = threading.excepthook
        self._previous_thread_excepthook = previous

        def thread_excepthook(args: threading.ExceptHookArgs) -> None:
            if args.exc_value is None:
                previous(args)
                return
            thread_name = args.thread.name if args.thread is not None else "unknown"
            detail = "".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            )
            try:
                self._logger.error("Uncaught exception in thread %s:\n%s", thread_name, detail)
                self._queue.put(("thread_error", (thread_name, detail)))
            except Exception:
                previous(args)

        threading.excepthook = thread_excepthook

    def _restore_exception_handlers(self) -> None:
        if self._previous_thread_excepthook is not None:
            threading.excepthook = self._previous_thread_excepthook
            self._previous_thread_excepthook = None

    def report_callback_exception(
        self,
        exc: type[BaseException],
        val: BaseException,
        tb: Any,
    ) -> None:
        detail = "".join(traceback.format_exception(exc, val, tb))
        self._logger.error("Unhandled Tk callback error:\n%s", detail)
        try:
            self._queue.put(("callback_error", detail))
        except Exception:
            self._show_unexpected_error_dialog("Unexpected error")

    def _log_file_path(self) -> str:
        return str(script_folder() / LOG_FILE_NAME)

    def _show_unexpected_error_dialog(self, title: str, *, thread_name: str | None = None) -> None:
        if self._close_requested:
            return
        log_path = self._log_file_path()
        if thread_name:
            body = (
                f"Thread {thread_name} failed unexpectedly.\n\n"
                f"Details are in the log:\n{log_path}"
            )
        else:
            body = (
                "An unexpected error occurred.\n\n"
                f"Details are in the log:\n{log_path}"
            )
        messagebox.showerror(title, body)

    def _show_fatal_job_error_dialog(self) -> None:
        if self._close_requested:
            return
        messagebox.showerror(
            "Processing error",
            "The current operation failed unexpectedly.\n\n"
            f"Details are in the log:\n{self._log_file_path()}",
        )

    def _install_window_icon(self) -> None:
        try:
            size = 64
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            margin = 8
            draw.rounded_rectangle(
                (margin, margin, size - margin, size - margin),
                radius=14,
                fill=ICE_FILL,
            )
            bar_w = 8
            bar_bottom = size - margin - 10
            bar_top = margin + 14
            draw.rounded_rectangle(
                (size // 2 - bar_w // 2, bar_top, size // 2 + bar_w // 2, bar_bottom),
                radius=3,
                fill=BUTTON_TEXT_DARK,
            )
            draw.polygon(
                [
                    (size // 2 - 12, bar_top + 6),
                    (size // 2 + 12, bar_top + 6),
                    (size // 2, bar_top - 4),
                ],
                fill=BUTTON_TEXT_DARK,
            )
            self._app_icon = ImageTk.PhotoImage(img)
            self.iconphoto(True, self._app_icon)
        except Exception:
            self._app_icon = None

    def _resolve_table_font(self, size: int, *, weight: str | None = None) -> tkfont.Font:
        available = {str(name).lower(): str(name) for name in self.tk.call("font", "families")}
        for family in (TABLE_CELL_FONT_FAMILY, *TABLE_CELL_FONT_FALLBACKS):
            resolved = available.get(family.lower())
            if resolved:
                return tkfont.Font(family=resolved, size=size, weight=weight or "normal")
        return tkfont.Font(family="Segoe UI", size=size, weight=weight or "normal")

    def _configure_treeview_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self._treeview_heading_font = self._resolve_table_font(TABLE_HEADING_SIZE, weight="bold")
        self._treeview_cell_font = self._resolve_table_font(TABLE_CELL_SIZE)
        rowheight = treeview_rowheight_px(
            self._treeview_cell_font.metrics("linespace"),
            self._ui_scale(),
        )

        style.configure(
            "DropGain.Treeview",
            background=BG_FIELD,
            fieldbackground=BG_FIELD,
            foreground=FG_MAIN,
            bordercolor=BORDER_COLOR,
            lightcolor=BG_FIELD,
            darkcolor=BG_FIELD,
            font=self._treeview_cell_font,
            rowheight=rowheight,
        )
        self._neutralize_treeview_selected_style(style)
        style.configure(
            "DropGain.Treeview.Heading",
            background=HEADER_BG,
            foreground=FG_MUTED,
            bordercolor=BORDER_COLOR,
            lightcolor=HEADER_BG,
            darkcolor=HEADER_BG,
            font=self._treeview_heading_font,
            relief="flat",
        )
        style.map(
            "DropGain.Treeview.Heading",
            background=[("active", BORDER_COLOR)],
            foreground=[("active", FG_MAIN)],
        )
        style.configure("DropGain.Vertical.TScrollbar", background=BG_CARD, troughcolor=BG_FIELD)
        style.configure("DropGain.Horizontal.TScrollbar", background=BG_CARD, troughcolor=BG_FIELD)
        style.map(
            "DropGain.Vertical.TScrollbar",
            background=[("active", BORDER_COLOR), ("pressed", BORDER_COLOR)],
        )
        style.map(
            "DropGain.Horizontal.TScrollbar",
            background=[("active", BORDER_COLOR), ("pressed", BORDER_COLOR)],
        )
        if hasattr(self, "results_table"):
            self.results_table.configure(style="DropGain.Treeview")

    def _neutralize_treeview_selected_style(self, style: ttk.Style) -> None:
        for option in ("foreground", "background"):
            mapped = list(style.map("DropGain.Treeview", queryopt=option))
            filtered = [entry for entry in mapped if not str(entry[0]).startswith("selected")]
            style.map("DropGain.Treeview", **{option: filtered})

    def _results_row_base_tag(self, tag: str) -> str:
        if tag.endswith("_selected"):
            return tag[: -len("_selected")]
        return tag

    def _results_row_display_tag(self, base_tag: str, *, selected: bool) -> str:
        return f"{base_tag}_selected" if selected else base_tag

    def _sync_results_table_selection_appearance(self) -> None:
        if not hasattr(self, "results_table"):
            return
        table = self.results_table
        selected = set(table.selection())
        for item in table.get_children():
            tags = table.item(item, "tags")
            if not tags:
                continue
            base_tag = self._results_row_base_tag(str(tags[0]))
            display_tag = self._results_row_display_tag(base_tag, selected=item in selected)
            if tags[0] != display_tag:
                table.item(item, tags=(display_tag,))

    def _handle_results_table_select(self, event: tk.Event[tk.Widget] | None = None) -> None:
        self._sync_results_table_selection_appearance()
        self._on_results_table_select(event)

    def _configure_results_table_tags(self) -> None:
        row_styles: tuple[tuple[str, str, str], ...] = (
            ("odd", TABLE_ROW_ODD, FG_MAIN),
            ("even", TABLE_ROW_EVEN, FG_MAIN),
            ("odd_warn", TABLE_ROW_ODD, WARN_FG),
            ("even_warn", TABLE_ROW_EVEN, WARN_FG),
            ("odd_error", TABLE_ROW_ODD, ERROR_FG),
            ("even_error", TABLE_ROW_EVEN, ERROR_FG),
            ("odd_ok", TABLE_ROW_ODD, SUCCESS_FG),
            ("even_ok", TABLE_ROW_EVEN, SUCCESS_FG),
        )
        for tag_name, background, foreground in row_styles:
            self.results_table.tag_configure(tag_name, background=background, foreground=foreground)
            self.results_table.tag_configure(
                f"{tag_name}_selected",
                background=TABLE_SELECTION_BG,
                foreground=foreground,
            )

    def _should_show_results_operation_overlay(self) -> bool:
        if not self._analyzed_rows:
            return True
        return self._active_phase == "render" and self._is_run_busy()

    def _refresh_results_empty_message(self) -> None:
        if not hasattr(self, "var_results_empty"):
            return
        if not self._should_show_results_operation_overlay():
            return
        if self._is_run_busy():
            if self._operation_started_at is not None:
                message = telemetry_caption(
                    self._operation_stats_text(
                        self._operation_last_rate,
                        self._operation_last_eta,
                        self._operation_last_errors,
                    )
                )
            else:
                message = self.var_status.get().strip() or RESULTS_EMPTY_PLACEHOLDER
            self.var_results_empty.set(message)
            return
        self.var_results_empty.set(RESULTS_EMPTY_PLACEHOLDER)

    def _update_results_empty_state(self, *, has_rows: bool) -> None:
        if not hasattr(self, "results_empty_label"):
            return
        if self._should_show_results_operation_overlay():
            self.results_empty_label.grid()
            self._refresh_results_empty_message()
        elif has_rows:
            self.results_empty_label.grid_remove()
        else:
            self.results_empty_label.grid()
            self._refresh_results_empty_message()

    def _results_table_column_width(self, heading: str, sample_cell: str) -> int:
        heading_width = treeview_column_width_px(
            self._treeview_heading_font.measure(heading),
            TREEVIEW_HEADING_PAD,
        )
        cell_width = (
            treeview_column_width_px(self._treeview_cell_font.measure(sample_cell), TREEVIEW_CELL_PAD)
            if sample_cell
            else 0
        )
        return max(heading_width, cell_width)

    def _results_table_column_widths_from_content(self) -> dict[str, int]:
        widths: dict[str, int] = {}
        column_ids: list[str] = []

        for column_id, heading, _anchor, sample_cell, _tooltip in RESULTS_TABLE_COLUMNS:
            column_ids.append(column_id)
            widths[column_id] = self._results_table_column_width(heading, sample_cell)

        widths["filename"] = max(widths["filename"], self._scaled(RESULTS_TABLE_FILENAME_MIN))

        for item in self.results_table.get_children():
            values = self.results_table.item(item, "values")
            for column_id, value in zip(column_ids, values, strict=True):
                text = str(value or "")
                if text:
                    cell_width = treeview_column_width_px(
                        self._treeview_cell_font.measure(text),
                        TREEVIEW_CELL_PAD,
                    )
                    widths[column_id] = max(widths[column_id], cell_width)

        widths["filename"] = min(widths["filename"], self._scaled(RESULTS_TABLE_FILENAME_ABSOLUTE_MAX))
        return widths

    # ---------------------------------------------------------------------
    # CustomTkinter widget helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def _font(size: int, weight: str | None = None) -> ctk.CTkFont:
        return ctk.CTkFont(family="Segoe UI", size=size, weight=weight)

    def _mono_font(self, size: int, weight: str | None = None) -> ctk.CTkFont:
        resolved = self._resolve_table_font(size, weight=weight)
        return ctk.CTkFont(family=resolved.cget("family"), size=size, weight=weight or "normal")

    def _entry(
        self,
        master: Any,
        *,
        textvariable: tk.Variable | None = None,
        width: int | None = None,
        height: int = ENTRY_HEIGHT,
        size: int = TYPE_BODY,
        mono: bool = False,
    ) -> ctk.CTkEntry:
        font = self._mono_font(size) if mono else self._font(size)
        kwargs: dict[str, Any] = {
            "master": master,
            "fg_color": BG_FIELD,
            "border_color": BORDER_COLOR,
            "text_color": FG_MAIN,
            "font": font,
            "height": height,
            "corner_radius": FIELD_CORNER_RADIUS,
        }
        if textvariable is not None:
            kwargs["textvariable"] = textvariable
        if width is not None:
            kwargs["width"] = width
        return ctk.CTkEntry(**kwargs)

    def _section_label(
        self,
        master: Any,
        *,
        text: str,
        bg: str = BG_MAIN,
        anchor: str = "w",
    ) -> ctk.CTkLabel:
        return self._label(
            master,
            text=text.upper(),
            color=FG_MUTED,
            bg=bg,
            size=TYPE_MICRO,
            weight="bold",
            anchor=anchor,
        )

    def _label(
        self,
        master: Any,
        *,
        text: str | None = None,
        textvariable: tk.Variable | None = None,
        color: str = FG_MAIN,
        bg: str = BG_CARD,
        size: int = TYPE_CAPTION,
        weight: str | None = None,
        anchor: str = "w",
        wraplength: int = 0,
        justify: str = "center",
        mono: bool = False,
    ) -> ctk.CTkLabel:
        kwargs: dict[str, Any] = {
            "master": master,
            "text": text or "",
            "textvariable": textvariable,
            "fg_color": "transparent",
            "text_color": color,
            "font": self._mono_font(size, weight) if mono else self._font(size, weight),
            "anchor": anchor,
            "justify": justify,
        }
        if wraplength > 0:
            kwargs["wraplength"] = wraplength
        return ctk.CTkLabel(**kwargs)

    def _button(
        self,
        master: Any,
        *,
        text: str,
        command: Any,
        accent: bool = False,
        state: str = "normal",
    ) -> ctk.CTkButton:
        if accent:
            button = ctk.CTkButton(
                master,
                text=text,
                command=command,
                fg_color=ACCENT,
                hover_color=ACCENT_HOVER,
                border_color=ACCENT,
                border_width=1,
                text_color=BUTTON_TEXT_DARK,
                text_color_disabled=BUTTON_DISABLED_TEXT,
                font=self._font(TYPE_BODY, "bold"),
                height=BUTTON_HEIGHT,
                corner_radius=ACTION_BUTTON_CORNER_RADIUS,
            )
        else:
            button = ctk.CTkButton(
                master,
                text=text,
                command=command,
                fg_color=BUTTON_SECONDARY_BG,
                hover_color=BUTTON_SECONDARY_HOVER,
                border_color=BUTTON_SECONDARY_BORDER,
                border_width=1,
                text_color=FG_MAIN,
                text_color_disabled=BUTTON_DISABLED_TEXT,
                font=self._font(TYPE_BODY),
                height=BUTTON_HEIGHT,
                corner_radius=ACTION_BUTTON_CORNER_RADIUS,
            )

        button._dropgain_accent = accent  # type: ignore[attr-defined]
        self._apply_action_button_state(button, state)
        self._wire_button_hover(button)

        def _restore_after_press(b: ctk.CTkButton = button) -> None:
            self._apply_action_button_state(b, str(b.cget("state")))
            tween = getattr(b, "_dropgain_button_tween", None)
            if tween is not None and pointer_inside_widget(b):
                tween(True)

        wire_ctk_button_press(
            button,
            lambda b=button: ACCENT_ACTIVE if getattr(b, "_dropgain_accent", False) else BUTTON_SECONDARY_ACTIVE,
            restore=_restore_after_press,
        )
        return button

    def _register_tab_button(self, button: ctk.CTkButton, *, active: bool) -> None:
        button._dropgain_tab_active = active  # type: ignore[attr-defined]
        if not getattr(button, "_dropgain_tab_hover_wired", False):
            self._wire_tab_button_hover(button)
        if not getattr(button, "_dropgain_press_wired", False):
            wire_ctk_button_press(
                button,
                lambda b=button: (
                    TAB_ACTIVE_PRESS
                    if getattr(b, "_dropgain_tab_active", False)
                    else BUTTON_SECONDARY_ACTIVE
                ),
                restore=lambda b=button: b.configure(
                    **self._tab_button_style(active=getattr(b, "_dropgain_tab_active", False))
                ),
            )
        apply_hand_cursor(button)
        button.configure(**self._tab_button_style(active=active))

    def _configure_tab_button(self, button: ctk.CTkButton, *, active: bool) -> None:
        button._dropgain_tab_active = active  # type: ignore[attr-defined]
        button.configure(**self._tab_button_style(active=active))

    def _wire_tab_button_hover(self, button: ctk.CTkButton) -> None:
        button._dropgain_tab_hover_wired = True  # type: ignore[attr-defined]
        # Remove CTkButton's native hover bindings so we can drive the hover animation ourselves.
        try:
            button.unbind("<Enter>")
            button.unbind("<Leave>")
        except Exception:
            pass
        hover_state = {"target": False, "step": 0, "after_id": None}

        def _rest_colors() -> tuple[str, str, str]:
            active = getattr(button, "_dropgain_tab_active", False)
            if active:
                return ACCENT_DIM, ICE_DIM, ACCENT
            return BG_MAIN, BG_MAIN, FG_MUTED

        def _set_colors(step: int, steps: int) -> None:
            rest_fg, rest_hover, rest_text = _rest_colors()
            if step <= 0:
                button.configure(fg_color=rest_fg, hover_color=rest_hover, text_color=rest_text)
                return
            if step >= steps:
                button.configure(fg_color=TAB_INACTIVE_HOVER_BG, hover_color=TAB_INACTIVE_HOVER_BG, text_color=TAB_INACTIVE_HOVER_TEXT)
                return
            ratio = step / steps
            button.configure(
                fg_color=self._blend_color(rest_fg, TAB_INACTIVE_HOVER_BG, ratio),
                hover_color=self._blend_color(rest_hover, TAB_INACTIVE_HOVER_BG, ratio),
                text_color=self._blend_color(rest_text, TAB_INACTIVE_HOVER_TEXT, ratio),
            )

        def _tween(target: bool) -> None:
            if hover_state["target"] == target:
                return
            hover_state["target"] = target
            if hover_state["after_id"] is not None:
                try:
                    button.after_cancel(hover_state["after_id"])
                except Exception:
                    pass
                hover_state["after_id"] = None

            def step() -> None:
                hover_state["after_id"] = None
                if hover_state["target"]:
                    hover_state["step"] = min(hover_state["step"] + 1, TAB_HOVER_TWEEN_STEPS)
                else:
                    hover_state["step"] = max(hover_state["step"] - 1, 0)
                _set_colors(hover_state["step"], TAB_HOVER_TWEEN_STEPS)
                if (hover_state["target"] and hover_state["step"] < TAB_HOVER_TWEEN_STEPS) or (
                    not hover_state["target"] and hover_state["step"] > 0
                ):
                    hover_state["after_id"] = button.after(TAB_HOVER_TWEEN_MS, step)

            step()

        def _on_enter(event: tk.Event) -> None:
            try:
                if str(button.cget("state")) == "disabled":
                    return
            except Exception:
                return
            if getattr(button, "_dropgain_tab_active", False):
                return
            _tween(True)

        def _on_leave(event: tk.Event) -> None:
            try:
                if str(button.cget("state")) == "disabled":
                    return
            except Exception:
                return
            _tween(False)

        button.bind("<Enter>", _on_enter, add="+")
        button.bind("<Leave>", _on_leave, add="+")

    @staticmethod
    def _blend_color(from_hex: str, to_hex: str, ratio: float) -> str:
        """Linearly interpolate between two hex colors. Transparent is treated as BG_MAIN."""
        if from_hex == "transparent":
            from_hex = BG_MAIN
        if to_hex == "transparent":
            to_hex = BG_MAIN
        from_rgb = tuple(int(from_hex[i:i+2], 16) for i in (1, 3, 5))
        to_rgb = tuple(int(to_hex[i:i+2], 16) for i in (1, 3, 5))
        return "#" + "".join(f"{int(round(f + (t - f) * ratio)):02x}" for f, t in zip(from_rgb, to_rgb))

    def _wire_button_hover(self, button: ctk.CTkButton) -> None:
        if getattr(button, "_dropgain_button_hover_wired", False):
            return
        button._dropgain_button_hover_wired = True  # type: ignore[attr-defined]
        try:
            button.unbind("<Enter>")
            button.unbind("<Leave>")
        except Exception:
            pass

        hover_state = {"target": False, "step": 0, "after_id": None}

        def _rest_colors() -> tuple[str, str, str]:
            accent = bool(getattr(button, "_dropgain_accent", False))
            if accent:
                return ACCENT, ACCENT, BUTTON_TEXT_DARK
            return BUTTON_SECONDARY_BG, BUTTON_SECONDARY_BG, FG_MAIN

        def _hover_colors() -> tuple[str, str, str]:
            accent = bool(getattr(button, "_dropgain_accent", False))
            if accent:
                return ACCENT_HOVER, ACCENT_HOVER, BUTTON_TEXT_DARK
            return BUTTON_SECONDARY_HOVER, BUTTON_SECONDARY_HOVER, FG_MAIN

        def _set_colors(step: int, steps: int) -> None:
            rest_fg, rest_hover, rest_text = _rest_colors()
            hover_fg, hover_hover, hover_text = _hover_colors()
            if step <= 0:
                button.configure(fg_color=rest_fg, hover_color=rest_hover, text_color=rest_text)
                return
            if step >= steps:
                button.configure(fg_color=hover_fg, hover_color=hover_hover, text_color=hover_text)
                return
            ratio = step / steps
            button.configure(
                fg_color=self._blend_color(rest_fg, hover_fg, ratio),
                hover_color=self._blend_color(rest_hover, hover_hover, ratio),
                text_color=self._blend_color(rest_text, hover_text, ratio),
            )

        def _tween(target: bool) -> None:
            if hover_state["target"] == target:
                return
            hover_state["target"] = target
            if hover_state["after_id"] is not None:
                try:
                    button.after_cancel(hover_state["after_id"])
                except Exception:
                    pass
                hover_state["after_id"] = None

            def step() -> None:
                hover_state["after_id"] = None
                if hover_state["target"]:
                    hover_state["step"] = min(hover_state["step"] + 1, HOVER_TWEEN_STEPS)
                else:
                    hover_state["step"] = max(hover_state["step"] - 1, 0)
                _set_colors(hover_state["step"], HOVER_TWEEN_STEPS)
                if (hover_state["target"] and hover_state["step"] < HOVER_TWEEN_STEPS) or (
                    not hover_state["target"] and hover_state["step"] > 0
                ):
                    hover_state["after_id"] = button.after(HOVER_TWEEN_MS, step)

            step()

        button._dropgain_button_tween = _tween  # type: ignore[attr-defined]
        _set_colors(0, HOVER_TWEEN_STEPS)

        def _on_enter(event: tk.Event) -> None:
            try:
                if str(button.cget("state")) == "disabled":
                    return
            except Exception:
                return
            if getattr(button, "_dropgain_pressing", False):
                return
            _tween(True)

        def _on_leave(event: tk.Event) -> None:
            try:
                if str(button.cget("state")) == "disabled":
                    return
            except Exception:
                return
            if getattr(button, "_dropgain_pressing", False):
                return
            _tween(False)

        button.bind("<Enter>", _on_enter, add="+")
        button.bind("<Leave>", _on_leave, add="+")

    def _update_window_cursor(self) -> None:
        desired = CURSOR_BUSY if self._is_run_busy() else ""
        if getattr(self, "_dropgain_cursor", None) == desired:
            return
        try:
            self.configure(cursor=desired)
            self._dropgain_cursor = desired  # type: ignore[attr-defined]
        except Exception:
            pass

    def _apply_action_button_state(self, button: ctk.CTkButton, state: str) -> None:
        disabled = state == "disabled"
        accent = bool(getattr(button, "_dropgain_accent", False))
        if disabled:
            button.configure(
                state="disabled",
                fg_color=BUTTON_DISABLED_FG,
                hover_color=BUTTON_DISABLED_FG,
                border_width=0,
                cursor="",
            )
            return

        if accent:
            button.configure(
                state="normal",
                fg_color=ACCENT,
                hover_color=ACCENT,
                border_color=ACCENT,
                text_color=BUTTON_TEXT_DARK,
                cursor=CURSOR_POINTER,
            )
            return

        button.configure(
            state="normal",
            fg_color=BUTTON_SECONDARY_BG,
            hover_color=BUTTON_SECONDARY_BG,
            border_color=BUTTON_SECONDARY_BORDER,
            border_width=1,
            text_color=FG_MAIN,
            cursor=CURSOR_POINTER,
        )

    def _tab_button_style(self, *, active: bool) -> dict[str, Any]:
        base = {"corner_radius": TAB_CORNER_RADIUS, "cursor": CURSOR_POINTER}
        if active:
            return {
                **base,
                "fg_color": ACCENT_DIM,
                "hover_color": ICE_DIM,
                "border_color": ACCENT,
                "border_width": 1,
                "text_color": ACCENT,
                "font": self._font(TYPE_LABEL, "bold"),
            }
        return {
            **base,
            "fg_color": BG_MAIN,
            "hover_color": BG_MAIN,
            "border_width": 0,
            "text_color": FG_MUTED,
            "font": self._font(TYPE_LABEL),
        }

    def _card(self, master: Any, color: str = BG_CARD, padding: int | tuple[int, int] = 0) -> ctk.CTkFrame:
        padx = pady = 0
        if isinstance(padding, tuple):
            padx, pady = padding
        else:
            padx = pady = padding
        frame = ctk.CTkFrame(
            master,
            fg_color=color,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=CARD_CORNER_RADIUS,
        )
        frame._dropgain_padx = padx  # type: ignore[attr-defined]
        frame._dropgain_pady = pady  # type: ignore[attr-defined]
        return frame

    def _inner(self, master: ctk.CTkFrame) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(master, fg_color="transparent")
        frame.pack(
            fill="both",
            expand=True,
            padx=getattr(master, "_dropgain_padx", 0),
            pady=getattr(master, "_dropgain_pady", 0),
        )
        return frame

    def _add_tooltip(self, widget: tk.Widget, text: str, *, wraplength: int = 390) -> None:
        """Attach a delayed hover tooltip and keep it alive with the widget."""
        if not text.strip():
            return

        tooltip = DropGainTooltip(widget, text, wraplength=wraplength)
        existing = getattr(widget, "_dropgain_tooltips", [])
        existing.append(tooltip)
        widget._dropgain_tooltips = existing  # type: ignore[attr-defined]

    def _init_settings_variables(self) -> None:
        settings = self._settings
        self.var_folder = tk.StringVar(value=str(settings.get("last_folder") or ""))
        self.var_output_folder = tk.StringVar(value=str(settings.get("last_output_folder") or ""))
        self.var_csv = tk.StringVar(value=default_csv_path(self.var_folder.get().strip()))
        self.var_write_csv = tk.BooleanVar(value=self._setting_bool(settings, "write_csv", True))
        self.var_window = tk.DoubleVar(value=self._setting_float(settings, "window_seconds", DEFAULT_LOUD_SECTION_WINDOW_SECONDS))
        self.var_hop = tk.DoubleVar(value=self._setting_float(settings, "hop_seconds", DEFAULT_LOUD_SECTION_HOP_SECONDS))
        self.var_workers = tk.IntVar(value=self._setting_int(settings, "workers", DEFAULT_ANALYSIS_WORKER_THREADS))
        self.var_target_low = tk.DoubleVar(value=self._setting_float(settings, "target_low", DEFAULT_TARGET_LOW_LUFS))
        self.var_target_high = tk.DoubleVar(value=self._setting_float(settings, "target_high", DEFAULT_TARGET_HIGH_LUFS))
        self.var_max_reduction = tk.DoubleVar(value=self._setting_float(settings, "max_reduction", DEFAULT_MAX_REDUCTION_DB))
        self.var_bass_max_reduction = tk.DoubleVar(
            value=self._setting_float(settings, "bass_max_reduction", DEFAULT_BASS_MAX_BOOST_REDUCTION_DB)
        )
        self.var_bass_penalty_start = tk.DoubleVar(
            value=self._setting_float(settings, "bass_penalty_start", DEFAULT_BASS_PENALTY_START_DB)
        )
        self.var_bass_penalty_full = tk.DoubleVar(
            value=self._setting_float(settings, "bass_penalty_full", DEFAULT_BASS_PENALTY_FULL_DB)
        )
        self.var_sub_penalty_start = tk.DoubleVar(
            value=self._setting_float(settings, "sub_penalty_start", DEFAULT_SUB_PENALTY_START_DB)
        )
        self.var_sub_penalty_full = tk.DoubleVar(
            value=self._setting_float(settings, "sub_penalty_full", DEFAULT_SUB_PENALTY_FULL_DB)
        )
        self.var_peak_ceiling = tk.DoubleVar(value=self._setting_float(settings, "peak_ceiling", DEFAULT_BOOST_PEAK_CEILING_DBFS))
        self.var_mp3_threshold = tk.DoubleVar(value=self._setting_float(settings, "mp3_threshold", MP3_MIN_ABS_GAIN_DB))
        self.var_lossless_threshold = tk.DoubleVar(value=self._setting_float(settings, "lossless_threshold", LOSSLESS_MIN_ABS_GAIN_DB))
        self.var_apply_render_gain_threshold = tk.BooleanVar(
            value=self._setting_bool(settings, "apply_render_gain_threshold", DEFAULT_APPLY_RENDER_GAIN_THRESHOLD)
        )
        self.var_normalization_mode = tk.StringVar(
            value=normalize_normalization_mode(settings.get("normalization_mode", DEFAULT_NORMALIZATION_MODE))
        )
        self.var_limiter_engine = tk.StringVar(
            value=normalize_limiter_engine(settings.get("limiter_engine", DEFAULT_LIMITER_ENGINE))
        )
        self.var_output_format_mode = tk.StringVar(
            value=normalize_output_format_mode(settings.get("output_format_mode", DEFAULT_OUTPUT_FORMAT_MODE))
        )
        self.var_allow_risky_true_peak_boost = tk.BooleanVar(
            value=self._setting_bool(settings, "allow_risky_true_peak_boost", False)
        )
        self.var_process_settings_summary = tk.StringVar(value="")

    def _open_settings(self) -> None:
        self._show_main_page("preferences")

    def _ensure_preferences_page(self) -> None:
        if self.preferences_page is None:
            from gui_settings import PreferencesPage

            self.preferences_page = PreferencesPage(self, self.pages_container)
            self.preferences_page.grid(row=0, column=0, sticky="nsew")

    @staticmethod
    def _output_format_short_label(output_format_mode: object) -> str:
        mode = normalize_output_format_mode(output_format_mode)
        if mode == OUTPUT_FORMAT_MP3_TO_AIFF:
            return "MP3 to AIFF"
        if mode == OUTPUT_FORMAT_ALL_TO_AIFF:
            return "AIFF"
        if mode == OUTPUT_FORMAT_ALL_TO_MP3:
            return "MP3"
        return "Preserve"

    def _is_preferences_active(self) -> bool:
        return getattr(self, "_active_main_page", "process") == "preferences"

    def _mark_main_page_visuals_dirty(self) -> None:
        self._main_page_visuals_dirty = True

    def _flush_main_page_visuals(self) -> None:
        if not self._main_page_visuals_dirty:
            return
        self._main_page_visuals_dirty = False
        self._refresh_settings_summary(force=True)
        self._validate_paths(force=True)
        self._refresh_output_format_hint()
        self._update_summary_cards(force=True)
        if self._analyzed_rows and hasattr(self, "results_table"):
            self._sync_results_table_rows(self._analyzed_rows)
        self._apply_idle_state_controls()

    def _refresh_settings_summary(self, *, force: bool = False) -> None:
        try:
            low = float(self.var_target_low.get())
            high = float(self.var_target_high.get())
            peak = float(self.var_peak_ceiling.get())
            output = self._output_format_short_label(self.var_output_format_mode.get())
            mode = normalize_normalization_mode(self.var_normalization_mode.get())
            text = (
                f"Target {low:.1f} to {high:.1f} LUFS{PROCESS_SETTINGS_VALUE_SEP}"
                f"{peak:.1f} dBTP{PROCESS_SETTINGS_VALUE_SEP}"
                f"{output}{PROCESS_SETTINGS_VALUE_SEP}"
                f"{mode}"
            )
        except Exception:
            text = ""

        if not force and self._is_preferences_active():
            self._mark_main_page_visuals_dirty()
            return
        if self.var_process_settings_summary.get() == text:
            return
        self.var_process_settings_summary.set(text)

    # ---------------------------------------------------------------------
    # UI construction
    # ---------------------------------------------------------------------

    def _build_ui(self) -> None:
        from gui_process import ProcessPage

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color=HEADER_BG, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        header_inner = ctk.CTkFrame(header, fg_color="transparent")
        header_inner.grid(row=0, column=0, sticky="ew", padx=PAGE_PADX, pady=SPACE_3)
        header_inner.grid_columnconfigure(0, weight=1)
        title_area = ctk.CTkFrame(header_inner, fg_color="transparent")
        title_area.grid(row=0, column=0, sticky="nsw")
        title_area.grid_rowconfigure(0, weight=1)
        title_area.grid_rowconfigure(2, weight=1)
        self._label(title_area, text="DROPGAIN", bg=HEADER_BG, size=TYPE_DISPLAY, weight="bold").grid(
            row=1, column=0, sticky="w"
        )

        nav = ctk.CTkFrame(header_inner, fg_color="transparent")
        nav.grid(row=0, column=1, sticky="nse")
        nav.grid_rowconfigure(0, weight=1)
        nav.grid_rowconfigure(2, weight=1)
        nav_buttons = ctk.CTkFrame(nav, fg_color="transparent")
        nav_buttons.grid(row=1, column=0, sticky="e")
        self.btn_nav_process = ctk.CTkButton(
            nav_buttons,
            text="PROCESS",
            width=100,
            height=28,
            command=lambda: self._show_main_page("process"),
        )
        self.btn_nav_process.grid(row=0, column=0, padx=(0, SPACE_2))
        self._register_tab_button(self.btn_nav_process, active=True)
        self.btn_nav_library = ctk.CTkButton(
            nav_buttons,
            text="LIBRARY TUNING",
            width=130,
            height=28,
            command=lambda: self._show_main_page("library"),
        )
        self.btn_nav_library.grid(row=0, column=1, padx=(0, SPACE_2))
        self._register_tab_button(self.btn_nav_library, active=False)
        self.btn_nav_preferences = ctk.CTkButton(
            nav_buttons,
            text="PREFERENCES",
            width=110,
            height=28,
            command=lambda: self._show_main_page("preferences"),
        )
        self.btn_nav_preferences.grid(row=0, column=2)
        self._register_tab_button(self.btn_nav_preferences, active=False)
        self.btn_settings = self.btn_nav_preferences

        self.btn_report_issue = ctk.CTkButton(
            nav_buttons,
            text="Issue",
            width=55,
            height=28,
            fg_color=ISSUE_BUTTON_BG,
            hover_color=ISSUE_BUTTON_HOVER,
            text_color=FG_MUTED,
            corner_radius=TAB_CORNER_RADIUS,
            font=self._font(TYPE_LABEL),
            command=self._open_report_issue,
        )
        self.btn_report_issue.grid(row=0, column=3, padx=(SPACE_2, 0))
        apply_hand_cursor(self.btn_report_issue)

        ctk.CTkFrame(header, fg_color=BORDER_COLOR, height=1, corner_radius=0).grid(row=1, column=0, sticky="ew")

        self.pages_container = ctk.CTkFrame(self, fg_color=BG_MAIN, corner_radius=0)
        self.pages_container.grid(row=1, column=0, sticky="nsew")
        self.pages_container.grid_columnconfigure(0, weight=1)
        self.pages_container.grid_rowconfigure(0, weight=1)

        self.process_page = ProcessPage(self, self.pages_container)
        self.process_page.grid(row=0, column=0, sticky="nsew")

        self.library_tuning_page = None
        self._active_main_page = "process"

    def _fade_panel_swap(
        self,
        container: tk.Widget,
        swap: Callable[[], None],
        *,
        before_reveal: Callable[[], None] | None = None,
        on_complete: Callable[[], None] | None = None,
        settle_ms: int | None = None,
    ) -> None:
        self._panel_fade.run(
            container,
            swap,
            before_reveal=before_reveal,
            on_complete=on_complete,
            settle_ms=settle_ms,
        )

    def _ensure_main_page_for_transition(self, name: str) -> tk.Widget | None:
        """Create lazy pages before the visible swap so construction is never revealed."""
        if name == "library" and self.library_tuning_page is None:
            from gui_library_tuning import LibraryTuningPage

            self.library_tuning_page = LibraryTuningPage(self, self.pages_container)
            self.library_tuning_page.grid(row=0, column=0, sticky="nsew")
            self.library_tuning_page.grid_remove()

        if name == "preferences":
            self._ensure_preferences_page()
            if self._active_main_page != "preferences" and self.preferences_page is not None:
                self.preferences_page.grid_remove()

        return {
            "process": self.process_page,
            "library": self.library_tuning_page,
            "preferences": self.preferences_page,
        }.get(name)

    def _prepare_main_page_for_reveal(self, name: str, prev_page: str) -> None:
        """Run page refresh/layout work while the curtain is still up."""
        if prev_page == "preferences" and name != "preferences":
            self._flush_main_page_visuals()

        if name == "process":
            try:
                self.process_page.settle_layout_for_reveal()
            except Exception:
                try:
                    self.process_page.refresh_layout()
                except Exception:
                    pass
            if hasattr(self, "results_table"):
                try:
                    self._resize_results_table_columns()
                except Exception:
                    pass

        elif name == "library" and self.library_tuning_page is not None:
            try:
                self.library_tuning_page.settle_layout_for_reveal()
            except Exception:
                self.library_tuning_page.refresh_from_app()
            if self._is_run_busy():
                self._set_busy_state()

        elif name == "preferences" and self.preferences_page is not None:
            self.preferences_page.refresh_from_app()
            if self._is_run_busy():
                self.preferences_page.set_controls_state("disabled")
            else:
                self.preferences_page.set_controls_state("normal")
            try:
                self.preferences_page.settle_layout_for_reveal()
            except Exception:
                try:
                    self.preferences_page._apply_scheduled_preferences_layout()
                except Exception:
                    pass

        self.update_idletasks()

    def _show_main_page(self, name: str) -> None:
        if name == self._active_main_page or self._panel_fade.active:
            return

        prev_page = self._active_main_page
        target_page = self._ensure_main_page_for_transition(name)
        if target_page is None:
            return

        self._configure_tab_button(self.btn_nav_process, active=name == "process")
        self._configure_tab_button(self.btn_nav_library, active=name == "library")
        self._configure_tab_button(self.btn_nav_preferences, active=name == "preferences")

        def swap() -> None:
            page_map = {
                "process": self.process_page,
                "library": self.library_tuning_page,
                "preferences": self.preferences_page,
            }
            for page_name, page in page_map.items():
                if page is None:
                    continue
                if page_name == name:
                    page.grid(row=0, column=0, sticky="nsew")
                    try:
                        page.tkraise()
                    except Exception:
                        pass
                else:
                    page.grid_remove()

            self._active_main_page = name

        settle_ms = {
            "process": 130,
            "library": 190,
            "preferences": 150,
        }.get(name, 150)

        self._fade_panel_swap(
            self.pages_container,
            swap,
            before_reveal=lambda: self._prepare_main_page_for_reveal(name, prev_page),
            settle_ms=settle_ms,
        )

    def _open_report_issue(self) -> None:
        webbrowser.open("https://github.com/mirbyte/DropGain/issues")

    def _show_output_tab(self, name: str) -> None:
        """Show the compact output panel tab without the extra CTkTabview top padding."""
        if not hasattr(self, "waveform_panel") or not hasattr(self, "log_panel"):
            return

        show_log = name == "Log"
        if show_log and self.log_panel.winfo_viewable():
            return
        if not show_log and self.waveform_panel.winfo_viewable():
            return

        if hasattr(self, "btn_output_waveform"):
            self._configure_tab_button(self.btn_output_waveform, active=not show_log)
        if hasattr(self, "btn_output_log"):
            self._configure_tab_button(self.btn_output_log, active=show_log)
        if hasattr(self, "output_track_info"):
            if show_log:
                self.output_track_info.grid_remove()
            else:
                self.output_track_info.grid(row=0, column=1, sticky="e", padx=(SPACE_2, 0))

        def swap() -> None:
            if show_log:
                self.waveform_panel.grid_remove()
                self.log_panel.grid()
            else:
                self.log_panel.grid_remove()
                self.waveform_panel.grid()

        self._fade_panel_swap(
            self.output_content,
            swap,
            before_reveal=lambda: self.update_idletasks(),
            settle_ms=90,
        )

    def _wire_setting_traces(self) -> None:
        self.var_folder.trace_add("write", self._on_analysis_setting_changed)
        for var in (
            self.var_window,
            self.var_hop,
            self.var_workers,
        ):
            var.trace_add("write", self._on_analysis_setting_changed)
        for var in (
            self.var_target_low,
            self.var_target_high,
            self.var_max_reduction,
            self.var_bass_max_reduction,
            self.var_bass_penalty_start,
            self.var_bass_penalty_full,
            self.var_sub_penalty_start,
            self.var_sub_penalty_full,
            self.var_peak_ceiling,
            self.var_mp3_threshold,
            self.var_lossless_threshold,
            self.var_normalization_mode,
            self.var_limiter_engine,
            self.var_output_format_mode,
            self.var_allow_risky_true_peak_boost,
        ):
            var.trace_add("write", self._on_decision_setting_changed)
        self.var_apply_render_gain_threshold.trace_add("write", self._on_render_rule_setting_changed)
        self.var_write_csv.trace_add("write", self._on_non_analysis_setting_changed)
        self.var_output_folder.trace_add("write", self._on_decision_setting_changed)

    # ---------------------------------------------------------------------
    # Stale-analysis protection
    # ---------------------------------------------------------------------

    @staticmethod
    def _normalized_folder_signature(folder: str) -> str:
        if not folder:
            return ""
        return os.path.normcase(os.path.abspath(folder))

    @staticmethod
    def _rounded(value: object, digits: int = 4) -> float:
        return round(float(value), digits)

    @classmethod
    def _analysis_signature_from_values(
        cls,
        *,
        folder: str,
        window_seconds: float,
        hop_seconds: float,
    ) -> dict[str, object]:
        return {
            "folder": cls._normalized_folder_signature(folder),
            "window_seconds": cls._rounded(window_seconds),
            "hop_seconds": cls._rounded(hop_seconds),
        }

    def _current_analysis_signature(self) -> dict[str, object] | None:
        try:
            return self._analysis_signature_from_values(
                folder=self.var_folder.get().strip(),
                window_seconds=max(1.0, float(self.var_window.get())),
                hop_seconds=max(1.0, float(self.var_hop.get())),
            )
        except Exception:
            return None

    def _analysis_is_current(self) -> bool:
        current = self._current_analysis_signature()
        return current is not None and self._analysis_signature is not None and current == self._analysis_signature

    def _analysis_stale_changes(self) -> list[str]:
        current = self._current_analysis_signature()
        previous = self._analysis_signature
        if current is None or previous is None:
            return []

        labels = {
            "folder": "folder",
            "window_seconds": "analysis window",
            "hop_seconds": "analysis hop",
        }
        return [labels[key] for key in labels if current.get(key) != previous.get(key)]

    def _on_output_format_mode_selected(self, _choice: str) -> None:
        self._refresh_output_format_hint()

    def _refresh_output_format_hint(self) -> None:
        label = self.lbl_output_format_hint
        if label is None:
            return
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            return

        hint, warn = output_format_mode_ui_hint(self.var_output_format_mode.get())
        text_color = WARN_FG if warn else FG_MUTED
        try:
            visible = bool(label.winfo_ismapped())
        except tk.TclError:
            visible = False

        if hint:
            if (
                str(label.cget("text")) != hint
                or str(label.cget("text_color")) != text_color
                or not visible
            ):
                label.configure(text=hint, text_color=text_color)
                if not visible:
                    label.grid()
        elif visible or str(label.cget("text")):
            label.configure(text="")
            label.grid_remove()

    def _on_analysis_setting_changed(self, *args: Any) -> None:
        if self._setting_change_blocked():
            return
        self._schedule_debounced(
            "_analysis_setting_after_id",
            self._apply_analysis_setting_changed,
            SETTING_CHANGE_DEBOUNCE_MS,
        )

    def _apply_analysis_setting_changed(self) -> None:
        self._analysis_setting_after_id = None
        if self._setting_change_blocked():
            return

        self._refresh_settings_summary()
        self._schedule_save_settings()
        self._validate_paths()

        if self._analyzed_rows and not self._analysis_is_current():
            changes = self._analysis_stale_changes()
            detail = f" Changed: {', '.join(changes[:4])}." if changes else ""
            self._set_telemetry_status("Settings changed since analysis. Re-analyze before rendering." + detail)

        self._set_idle_state()

    def _on_decision_setting_changed(self, *args: Any) -> None:
        if self._setting_change_blocked():
            return
        self._schedule_debounced(
            "_decision_setting_after_id",
            self._apply_decision_setting_changed,
            SETTING_CHANGE_DEBOUNCE_MS,
        )

    def _apply_decision_setting_changed(self) -> None:
        self._decision_setting_after_id = None
        if self._setting_change_blocked():
            return

        self._refresh_settings_summary()
        self._schedule_save_settings()
        self._validate_paths()
        self._refresh_output_format_hint()

        if self._analyzed_rows and self._analysis_is_current():
            settings = self._current_dropgain_settings()
            if settings is not None:
                recompute_rows_for_settings(settings, self._analyzed_rows)  # type: ignore[arg-type]
                self._run_counts["would_process"] = sum(
                    1
                    for row in self._analyzed_rows
                    if row.get("processing_status") == "analyzed_would_process"
                )
                self.var_run_summary.set(self._format_run_counts(self._run_counts))
                self._update_summary_cards()
                if not self._is_preferences_active():
                    self._sync_results_table_rows(self._analyzed_rows)
                else:
                    self._mark_main_page_visuals_dirty()
                if (
                    self.library_tuning_page is not None
                    and self._active_main_page == "library"
                ):
                    self.library_tuning_page.refresh_from_app()

        self._set_idle_state()

    def _on_render_rule_setting_changed(self, *args: Any) -> None:
        if self._setting_change_blocked():
            return
        self._schedule_debounced(
            "_render_rule_setting_after_id",
            self._apply_render_rule_setting_changed,
            SETTING_CHANGE_DEBOUNCE_MS,
        )

    def _apply_render_rule_setting_changed(self) -> None:
        self._render_rule_setting_after_id = None
        if self._setting_change_blocked():
            return

        self._schedule_save_settings()

        if self._analyzed_rows and self._analysis_is_current():
            settings = self._current_dropgain_settings()
            if settings is not None:
                self._run_counts["would_process"] = refresh_analyzed_render_statuses(
                    settings,
                    self._analyzed_rows,  # type: ignore[arg-type]
                )
                self.var_run_summary.set(self._format_run_counts(self._run_counts))
                self._update_summary_cards()
                if not self._is_preferences_active():
                    self._sync_results_table_rows(self._analyzed_rows)
                else:
                    self._mark_main_page_visuals_dirty()

        self._set_idle_state()

    def _on_non_analysis_setting_changed(self, *args: Any) -> None:
        if self._setting_change_blocked():
            return
        self._schedule_save_settings()

    # ---------------------------------------------------------------------
    # UI state
    # ---------------------------------------------------------------------

    def _update_summary_cards(self, *, force: bool = False) -> None:
        if not hasattr(self, "var_metric_would"):
            return
        if not force and self._is_preferences_active():
            self._mark_main_page_visuals_dirty()
            return
        self.var_metric_would.set(str(self._run_counts.get("would_process", 0)))
        self.var_metric_processed.set(str(self._run_counts.get("processed", 0)))
        self.var_metric_warnings.set(str(self._run_counts.get("warnings", 0)))
        self.var_metric_errors.set(str(self._run_counts.get("errors", 0)))

    def _reset_run_counts(self) -> None:
        self._run_counts = self._empty_run_counts()
        self.var_run_summary.set(self._format_run_counts(self._run_counts))
        self._update_summary_cards()

    def _folder_entry_width_px(self) -> int:
        return scaled_px_from_float(self._ui_scale(), FOLDER_ENTRY_WIDTH)

    def _update_folder_entry_width(self) -> None:
        if not hasattr(self, "entry_folder"):
            return

        width = self._folder_entry_width_px()
        for entry_name in ("entry_folder", "entry_output_folder"):
            entry = getattr(self, entry_name, None)
            if entry is None:
                continue
            try:
                entry.configure(width=width)
            except tk.TclError:
                pass

    def _validate_paths(self, *args: Any, force: bool = False) -> None:
        if not force and self._is_preferences_active():
            self._mark_main_page_visuals_dirty()
            return
        source_folder = self.var_folder.get().strip()
        if hasattr(self, "lbl_source_folder_status"):
            if not source_folder:
                self.lbl_source_folder_status.configure(text="⚠", text_color=ERROR_FG)
            elif os.path.isdir(source_folder):
                self.lbl_source_folder_status.configure(text="✓", text_color=ICE)
            else:
                self.lbl_source_folder_status.configure(text="⚠", text_color=ERROR_FG)
        output_folder = self.var_output_folder.get().strip()
        if hasattr(self, "lbl_output_folder_status"):
            if not output_folder:
                self.lbl_output_folder_status.configure(text="✓", text_color=ICE)
            elif os.path.isdir(output_folder):
                self.lbl_output_folder_status.configure(text="✓", text_color=ICE)
            else:
                self.lbl_output_folder_status.configure(text="⚠", text_color=ERROR_FG)

    def _is_run_busy(self) -> bool:
        worker = self._worker_thread
        return worker is not None and worker.is_alive()

    def _set_run_controls_state(self, state: str) -> None:
        if not hasattr(self, "entry_folder"):
            return

        self.entry_folder.configure(state="disabled" if state == "disabled" else "normal")
        if hasattr(self, "entry_output_folder"):
            self.entry_output_folder.configure(state="disabled" if state == "disabled" else "normal")
        if hasattr(self, "entry_lt_folder"):
            self.entry_lt_folder.configure(state="disabled" if state == "disabled" else "normal")
        self._apply_action_button_state(self.btn_browse_folder, state)
        if hasattr(self, "btn_browse_output_folder"):
            self._apply_action_button_state(self.btn_browse_output_folder, state)
        if hasattr(self, "btn_lt_browse"):
            self._apply_action_button_state(self.btn_lt_browse, state)
        if self.preferences_page is not None:
            try:
                if self.preferences_page.winfo_exists():
                    self.preferences_page.set_controls_state(state)
            except tk.TclError:
                pass

    def _set_busy_state(self) -> None:
        for button in (
            self.btn_batch,
            self.btn_start,
            self.btn_analyze_only,
            self.btn_open_csv,
            self.btn_open_output,
        ):
            self._apply_action_button_state(button, "disabled")
        self._apply_action_button_state(self.btn_cancel, "normal")
        if hasattr(self, "btn_lt_analyze"):
            self._apply_action_button_state(self.btn_lt_analyze, "disabled")
        self._set_run_controls_state("disabled")
        self._update_busy_button_label()
        self._update_window_cursor()

    def _set_idle_state(self) -> None:
        self._logger.debug(
            "_set_idle_state: busy=%s phase=%s completed=%s",
            self._is_run_busy(),
            self._active_phase,
            self._run_completed,
        )
        if self._is_run_busy():
            if self._active_main_page == "process":
                self._refresh_create_button_text()
            self._update_window_cursor()
            return

        if self._is_preferences_active():
            if self.preferences_page is not None:
                try:
                    if self.preferences_page.winfo_exists():
                        self.preferences_page.set_controls_state("normal")
                except tk.TclError:
                    pass
            self._mark_main_page_visuals_dirty()
            self._update_window_cursor()
            return

        self._apply_idle_state_controls()
        self._update_window_cursor()

    def _apply_idle_state_controls(self) -> None:
        self._set_run_controls_state("normal")
        self._apply_action_button_state(self.btn_batch, "normal")
        self._apply_action_button_state(self.btn_analyze_only, "normal")
        self._apply_action_button_state(self.btn_cancel, "disabled")
        if hasattr(self, "btn_lt_analyze"):
            self._apply_action_button_state(self.btn_lt_analyze, "normal")

        if self._analyzed_rows and self._renderable_analysis_indices() and self._analysis_is_current():
            self._apply_action_button_state(self.btn_start, "normal")
        else:
            self._apply_action_button_state(self.btn_start, "disabled")
        self._reset_operation_chrome()
        self._refresh_create_button_text()

        if os.path.exists(self.var_csv.get().strip()):
            self._apply_action_button_state(self.btn_open_csv, "normal")

        output_path = self._resolved_output_folder()
        if output_path and os.path.isdir(output_path):
            self._apply_action_button_state(self.btn_open_output, "normal")
        elif hasattr(self, "btn_open_output"):
            self._apply_action_button_state(self.btn_open_output, "disabled")

    def _operation_progress_fraction(self) -> float:
        if self._active_pipeline == "batch":
            if self._active_phase in {"preflight", "analyze"}:
                if self._batch_analysis_total <= 0:
                    return 0.0
                a_total = max(self._batch_analysis_total, 1)
                a_done = min(self._batch_analysis_done, a_total)
                return 0.5 * (a_done / a_total)
            r_total = max(self._batch_render_total, 1)
            r_done = min(self._batch_render_done, r_total)
            return min(1.0, 0.5 + 0.5 * (r_done / r_total))
        total = max(self._progress_total, 1)
        done = min(self._progress_done, total)
        return done / total

    def _progress_bars(self) -> list[ctk.CTkProgressBar]:
        bars: list[ctk.CTkProgressBar] = []
        if hasattr(self, "progress"):
            bars.append(self.progress)
        if hasattr(self, "progress_lt"):
            bars.append(self.progress_lt)
        return bars

    def _cancel_progress_tween(self) -> None:
        if self._progress_tween_after_id is None:
            return
        try:
            self.after_cancel(self._progress_tween_after_id)
        except tk.TclError:
            pass
        self._progress_tween_after_id = None

    def _set_progress_indeterminate(self, active: bool) -> None:
        if active == self._progress_indeterminate:
            return
        self._cancel_progress_tween()
        self._progress_indeterminate = active
        for bar in self._progress_bars():
            if active:
                bar.stop()
                bar.configure(mode="indeterminate")
                bar.start()
            else:
                bar.stop()
                bar.configure(mode="determinate")
                bar.set(self._progress_display)

    def _apply_progress_bar_values(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        self._progress_display = fraction
        if self._progress_indeterminate:
            return
        for bar in self._progress_bars():
            bar.set(fraction)

    def _set_progress_bars(self, fraction: float, *, immediate: bool = False) -> None:
        if self._progress_indeterminate:
            self._set_progress_indeterminate(False)
        self._progress_target = max(0.0, min(1.0, fraction))
        if immediate or abs(self._progress_target - self._progress_display) < 1e-6:
            self._cancel_progress_tween()
            self._apply_progress_bar_values(self._progress_target)
            return
        self._schedule_progress_tween()

    def _schedule_progress_tween(self) -> None:
        if self._progress_tween_after_id is not None:
            return
        self._progress_tween_step()

    def _progress_tween_step(self) -> None:
        self._progress_tween_after_id = None
        if self._progress_indeterminate:
            return
        delta = self._progress_target - self._progress_display
        if abs(delta) < 0.001:
            self._apply_progress_bar_values(self._progress_target)
            return
        alpha = min(
            1.0,
            PROGRESS_TWEEN_INTERVAL_MS / (PROGRESS_TWEEN_DURATION_SEC * 1000.0),
        )
        self._apply_progress_bar_values(self._progress_display + delta * alpha)
        if abs(self._progress_target - self._progress_display) >= 0.001:
            self._progress_tween_after_id = self.after(
                PROGRESS_TWEEN_INTERVAL_MS,
                self._progress_tween_step,
            )

    def _set_operation_phase_display(self, text: str) -> None:
        if hasattr(self, "var_operation_phase"):
            self.var_operation_phase.set(telemetry_caption(text))

    def _set_telemetry_status(self, text: str) -> None:
        if hasattr(self, "var_status"):
            self.var_status.set(telemetry_caption(text))

    def _operation_phase_label(self) -> str:
        if self._active_phase == "idle":
            return "Done" if self._run_completed else "Ready"
        labels = {
            "preflight": "Preparing",
            "analyze": "Analyzing library",
            "render": "Rendering copies",
        }
        return labels.get(self._active_phase, "Working")

    def _operation_fraction_text(self) -> str:
        if self._active_phase == "idle":
            return ""
        if self._active_pipeline == "batch" and self._active_phase == "render":
            if self._batch_render_total > 0:
                return f"{self._batch_render_done} / {self._batch_render_total}"
            return ""
        if self._active_phase in {"analyze", "render"} and self._progress_total > 0:
            return f"{self._progress_done} / {self._progress_total}"
        return ""

    def _update_metric_phase_highlight(self) -> None:
        if not hasattr(self, "_summary_metric_tiles"):
            return
        active_chip: str | None = None
        if self._is_run_busy():
            if self._active_phase == "analyze":
                active_chip = "would"
            elif self._active_phase == "render":
                active_chip = "processed"
        for chip_id, tile in self._summary_metric_tiles.items():
            tile.configure(border_color=ICE_FILL if chip_id == active_chip else BORDER_COLOR)

    def _set_busy_button_label(self, pipeline: str) -> None:
        if pipeline == "batch":
            self._busy_button = self.btn_batch
        elif pipeline == "analyze_only":
            self._busy_button = self.btn_analyze_only
        else:
            self._busy_button = self.btn_start
        self._busy_button_idle_text = str(self._busy_button.cget("text"))

    def _update_busy_button_label(self) -> None:
        if self._busy_button is None or self._active_pipeline is None:
            return
        labels = {
            "batch": {
                "preflight": "Working...",
                "analyze": "Analyzing...",
                "render": "Rendering...",
            },
            "analyze_only": {
                "preflight": "Analyzing...",
                "analyze": "Analyzing...",
                "render": "Analyzing...",
            },
            "review_render": {
                "preflight": "Rendering...",
                "analyze": "Rendering...",
                "render": "Rendering...",
            },
        }
        pipeline_labels = labels.get(self._active_pipeline, {})
        text = pipeline_labels.get(self._active_phase, pipeline_labels.get("preflight", "Working..."))
        self._busy_button.configure(text=text)

    def _restore_busy_button_label(self) -> None:
        if self._busy_button is None:
            return
        if self._busy_button is self.btn_start:
            self._refresh_create_button_text()
        elif self._busy_button_idle_text:
            self._busy_button.configure(text=self._busy_button_idle_text)
        self._busy_button = None
        self._busy_button_idle_text = ""

    @staticmethod
    def _format_elapsed_duration(seconds: float) -> str:
        total = max(int(seconds), 0)
        minutes, secs = divmod(total, 60)
        if minutes > 0:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    def _operation_elapsed_seconds(self) -> float:
        if self._operation_started_at is None:
            return 0.0
        return max(time.monotonic() - self._operation_started_at, 0.0)

    def _operation_stats_text(self, rate: float, eta: str, errors: int) -> str:
        elapsed = self._format_elapsed_duration(self._operation_elapsed_seconds())
        if self._cancel_flag.is_set() and self._is_run_busy():
            return f"{elapsed} elapsed · finishing current track(s)"
        if self._active_phase in {"analyze", "render"}:
            error_label = "error" if errors == 1 else "errors"
            return f"{rate:.1f} tracks/s · {elapsed} elapsed · ~{eta} left · {errors} {error_label}"
        return f"{elapsed} elapsed"

    def _cancel_operation_elapsed_refresh(self) -> None:
        if self._operation_elapsed_after_id is None:
            return
        try:
            self.after_cancel(self._operation_elapsed_after_id)
        except tk.TclError:
            pass
        self._operation_elapsed_after_id = None

    def _schedule_operation_elapsed_refresh(self) -> None:
        self._cancel_operation_elapsed_refresh()
        if self._active_phase == "idle":
            return
        self._refresh_operation_elapsed()
        self._operation_elapsed_after_id = self.after(1000, self._schedule_operation_elapsed_refresh)

    def _refresh_operation_elapsed(self) -> None:
        if not hasattr(self, "var_status") or self._active_phase == "idle":
            return
        if self._operation_started_at is None:
            return
        self._set_telemetry_status(
            self._operation_stats_text(
                self._operation_last_rate,
                self._operation_last_eta,
                self._operation_last_errors,
            )
        )
        self._refresh_results_empty_message()

    def _refresh_operation_display(self, rate: float = 0.0, eta: str = "", errors: int = 0) -> None:
        if not hasattr(self, "var_operation_phase"):
            return
        if self._active_phase == "idle":
            return

        self._operation_last_rate = rate
        self._operation_last_eta = eta
        self._operation_last_errors = errors

        self._set_operation_phase_display(self._operation_phase_label())
        self.var_operation_fraction.set(self._operation_fraction_text())

        if self._active_phase in {"preflight", "analyze", "render"}:
            self._set_telemetry_status(self._operation_stats_text(rate, eta, errors))
        self._refresh_results_empty_message()

        if self._active_phase != "preflight":
            fraction = self._operation_progress_fraction()
            self._set_progress_bars(fraction)

        fraction_text = self._operation_fraction_text()
        if fraction_text:
            self.title(f"{APP_TITLE} — {self._operation_phase_label()} ({fraction_text})")
        elif self._active_phase != "idle":
            self.title(f"{APP_TITLE} — {self._operation_phase_label()}")

    def _reset_operation_chrome(self) -> None:
        self._active_pipeline = None
        self._active_phase = "idle"
        self._progress_done = 0
        self._progress_total = 1
        self._batch_analysis_total = 0
        self._batch_analysis_done = 0
        self._batch_render_total = 0
        self._batch_render_done = 0
        self._operation_started_at = None
        self._operation_last_rate = 0.0
        self._operation_last_eta = ""
        self._operation_last_errors = 0
        self._cancel_operation_elapsed_refresh()
        self._restore_busy_button_label()
        self._cancel_progress_tween()
        self._set_progress_indeterminate(False)
        if hasattr(self, "var_operation_phase"):
            self._set_operation_phase_display(self._operation_phase_label())
            self.var_operation_fraction.set("")
        self._set_progress_bars(0.0, immediate=True)
        self._update_metric_phase_highlight()
        self.title(APP_TITLE)
        self._update_results_empty_state(has_rows=bool(self._analyzed_rows))

    def _begin_operation_run(self, pipeline: str, initial_status: str) -> None:
        self._run_completed = False
        self._active_pipeline = pipeline
        self._active_phase = "preflight"
        self._progress_done = 0
        self._progress_total = 1
        self._progress_max = 1
        self._batch_analysis_total = 0
        self._batch_analysis_done = 0
        self._batch_render_total = 0
        self._batch_render_done = 0
        self._operation_started_at = time.monotonic()
        self._operation_last_rate = 0.0
        self._operation_last_eta = ""
        self._operation_last_errors = 0
        self._set_busy_button_label(pipeline)
        self._update_busy_button_label()
        self._update_metric_phase_highlight()
        if hasattr(self, "var_operation_phase"):
            self._set_operation_phase_display("Preparing")
            self.var_operation_fraction.set("")
        self._set_telemetry_status(initial_status)
        self._set_progress_bars(0.0, immediate=True)
        self._set_progress_indeterminate(True)
        self._refresh_results_empty_message()
        self._schedule_operation_elapsed_refresh()

    def _set_active_phase(self, phase: str) -> None:
        leaving_preflight = self._active_phase == "preflight" and phase != "preflight"
        self._active_phase = phase
        if leaving_preflight:
            self._set_progress_indeterminate(False)
        self._update_metric_phase_highlight()
        self._update_busy_button_label()
        if hasattr(self, "var_operation_phase"):
            self._set_operation_phase_display(self._operation_phase_label())
            self.var_operation_fraction.set(self._operation_fraction_text())
            self.update_idletasks()
        if hasattr(self, "results_empty_label"):
            self._update_results_empty_state(has_rows=bool(self._analyzed_rows))

    def _on_batch_phase(self, data: object) -> None:
        info = dict(data)  # type: ignore[arg-type]
        self._batch_analysis_total = max(int(info.get("analysis_total", 0)), 0)
        self._batch_analysis_done = self._batch_analysis_total
        self._batch_render_total = max(int(info.get("render_total", 0)), 0)
        self._batch_render_done = 0
        self._progress_done = 0
        self._progress_total = max(self._batch_render_total, 1)
        self._set_active_phase("render")

    def _on_progress_tick(self, data: object) -> None:
        done, total, rate, eta, errors, counts = data  # type: ignore[misc]
        self._progress_done = int(done)
        self._progress_total = max(int(total), 1)
        self._progress_max = self._progress_total

        if self._active_pipeline == "batch":
            if self._active_phase == "analyze":
                self._batch_analysis_done = int(done)
                self._batch_analysis_total = max(int(total), 1)
            elif self._active_phase == "render":
                self._batch_render_done = int(done)
                self._batch_render_total = max(int(total), 1)

        self._run_counts = dict(counts)
        self.var_run_summary.set(self._format_run_counts(self._run_counts))
        self._update_summary_cards()
        self._refresh_operation_display(float(rate), str(eta), int(errors))

    def _set_progress_max(self, maximum: int) -> None:
        self._set_progress_indeterminate(False)
        self._progress_total = max(int(maximum), 1)
        self._progress_max = self._progress_total
        self._progress_done = 0
        preserve_bar = self._active_pipeline == "batch" and self._active_phase == "render"
        fraction = self._operation_progress_fraction() if preserve_bar else 0.0
        self._set_progress_bars(fraction, immediate=preserve_bar or fraction == 0.0)
        if hasattr(self, "var_operation_fraction"):
            self.var_operation_fraction.set(self._operation_fraction_text())

    def _set_progress_value(self, value: int) -> None:
        self._progress_done = int(value)
        fraction = self._operation_progress_fraction()
        self._set_progress_bars(fraction)

    # ---------------------------------------------------------------------
    # User actions
    # ---------------------------------------------------------------------

    def _reset_defaults(self) -> None:
        if not messagebox.askyesno("Reset Defaults", "Are you sure you want to reset all settings to their default values?"):
            return

        self._suspend_setting_traces = True
        try:
            self.var_window.set(DEFAULT_LOUD_SECTION_WINDOW_SECONDS)
            self.var_hop.set(DEFAULT_LOUD_SECTION_HOP_SECONDS)
            self.var_workers.set(DEFAULT_ANALYSIS_WORKER_THREADS)
            self.var_target_low.set(DEFAULT_TARGET_LOW_LUFS)
            self.var_target_high.set(DEFAULT_TARGET_HIGH_LUFS)
            self.var_max_reduction.set(DEFAULT_MAX_REDUCTION_DB)
            self.var_bass_max_reduction.set(DEFAULT_BASS_MAX_BOOST_REDUCTION_DB)
            self.var_bass_penalty_start.set(DEFAULT_BASS_PENALTY_START_DB)
            self.var_bass_penalty_full.set(DEFAULT_BASS_PENALTY_FULL_DB)
            self.var_sub_penalty_start.set(DEFAULT_SUB_PENALTY_START_DB)
            self.var_sub_penalty_full.set(DEFAULT_SUB_PENALTY_FULL_DB)
            self.var_peak_ceiling.set(DEFAULT_BOOST_PEAK_CEILING_DBFS)
            self.var_normalization_mode.set(DEFAULT_NORMALIZATION_MODE)
            self.var_limiter_engine.set(DEFAULT_LIMITER_ENGINE)
            self.var_mp3_threshold.set(MP3_MIN_ABS_GAIN_DB)
            self.var_lossless_threshold.set(LOSSLESS_MIN_ABS_GAIN_DB)
            self.var_output_format_mode.set(DEFAULT_OUTPUT_FORMAT_MODE)
            self.var_allow_risky_true_peak_boost.set(False)
            self.var_apply_render_gain_threshold.set(DEFAULT_APPLY_RENDER_GAIN_THRESHOLD)
            self.var_write_csv.set(True)
            self.var_output_folder.set("")
        finally:
            self._suspend_setting_traces = False

        self._refresh_output_format_hint()
        self._apply_analysis_setting_changed()
        self._apply_decision_setting_changed()
        self._save_settings()

    def _pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select source folder")
        if not folder:
            return
        self.var_folder.set(folder)
        self.var_csv.set(default_csv_path(folder))
        self._flush_pending_settings_save()

    def _pick_output_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select output folder")
        if not folder:
            return
        self.var_output_folder.set(folder)
        self._flush_pending_settings_save()

    def _resolved_output_folder(self) -> str:
        if not hasattr(self, "var_output_folder"):
            return ""
        output_folder = self.var_output_folder.get().strip()
        if output_folder:
            return output_folder
        if hasattr(self, "var_folder"):
            return self.var_folder.get().strip()
        return ""

    def _open_output_folder(self) -> None:
        folder = self._resolved_output_folder()
        if not folder:
            messagebox.showinfo("Output folder", "Select a source or output folder first.")
            return
        if not os.path.isdir(folder):
            messagebox.showinfo("Output folder", "The output folder does not exist yet.")
            return

        try:
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            messagebox.showerror("Open output folder failed", str(exc))

    def _open_csv(self) -> None:
        csv_path = self.var_csv.get().strip()
        if not os.path.exists(csv_path):
            messagebox.showinfo("CSV not found", "The CSV report does not exist yet.")
            return

        try:
            if os.name == "nt":
                os.startfile(csv_path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", csv_path])
            else:
                subprocess.Popen(["xdg-open", csv_path])
        except Exception as exc:
            messagebox.showerror("Open CSV failed", str(exc))

    def _run_system_check(self) -> None:
        results: list[tuple[str, bool, str]] = []

        def add(name: str, ok: bool, detail: str = "") -> None:
            results.append((name, ok, detail))

        try:
            check_ffmpeg_available()
            add("ffmpeg / ffprobe", True, "found")
        except Exception as exc:
            add("ffmpeg / ffprobe", False, str(exc))

        for module_name in ("customtkinter", "numpy", "scipy", "pyloudnorm", "mutagen", "pedalboard"):
            try:
                __import__(module_name)
                add(module_name, True, "found")
            except Exception as exc:
                add(module_name, False, str(exc))

        if normalize_limiter_engine(self.var_limiter_engine.get()) == LIMITER_ENGINE_LOUDMAX:
            try:
                plugin_path, plugin_detail = verify_loudmax_plugin()
                add("LoudMax VST3", True, plugin_detail)
            except Exception as exc:
                try:
                    plugin_path = find_loudmax_plugin_path()
                    add("LoudMax VST3 path", True, plugin_path)
                except Exception:
                    pass
                add("LoudMax preflight", False, str(exc))
        else:
            try:
                plugin_path, plugin_detail = verify_prol2_plugin()
                add("FabFilter Pro-L 2 VST3", True, plugin_detail)
            except Exception as exc:
                try:
                    plugin_path = find_prol2_plugin_path()
                    add("FabFilter Pro-L 2 VST3 path", True, plugin_path)
                except Exception:
                    pass
                add("FabFilter Pro-L 2 preflight", False, str(exc))

        folder = self.var_folder.get().strip()
        if folder:
            add("source folder", os.path.isdir(folder), folder)
        else:
            add("source folder", False, "not selected")

        try:
            csv_dir = os.path.dirname(os.path.abspath(default_csv_path(self.var_folder.get().strip()))) or os.getcwd()
            os.makedirs(csv_dir, exist_ok=True)
            test_path = os.path.join(csv_dir, ".dropgain_write_test")
            with open(test_path, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(test_path)
            add("report folder", True, csv_dir)
        except Exception as exc:
            add("report folder", False, str(exc))

        if self.preferences_page is not None:
            try:
                if self.preferences_page.winfo_exists():
                    self.preferences_page.show_system_check_results(results)
                    return
            except tk.TclError:
                pass

        lines = ["System check", "-" * 60]
        for name, ok, detail in results:
            prefix = "OK" if ok else "CHECK"
            lines.append(f"{prefix:<5} {name}: {detail}")
        all_ok = all(ok for _, ok, _ in results)
        if all_ok:
            messagebox.showinfo("System check", "\n".join(lines))
        else:
            messagebox.showwarning("System check", "\n".join(lines))

    # ---------------------------------------------------------------------
    # Run setup
    # ---------------------------------------------------------------------

    def _start_batch(self) -> None:
        self._start(pipeline="batch")

    def _start_processing(self) -> None:
        if not self._renderable_analysis_indices():
            messagebox.showinfo(
                "Nothing to render",
                "No analyzed tracks are ready to render. Adjust settings or re-analyze the library.",
            )
            return

        if not self._analysis_is_current():
            changes = self._analysis_stale_changes()
            detail = "\n\nChanged settings: " + ", ".join(changes) if changes else ""
            messagebox.showwarning(
                "Re-analyze required",
                "The current analysis no longer matches the settings. Re-analyze before rendering."
                + detail,
            )
            self._set_telemetry_status("Re-analyze required before rendering.")
            self._set_idle_state()
            return

        self._start(pipeline="review_render")

    def _start_analyze_only(self) -> None:
        self._start(pipeline="analyze_only")

    def _start(self, pipeline: str) -> None:
        if self._is_run_busy():
            return

        folder = self.var_folder.get().strip()
        csv_path = default_csv_path(folder)
        self.var_csv.set(csv_path)

        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Folder missing", "Please choose a valid source folder.")
            return

        try:
            target_low = float(self.var_target_low.get())
            target_high = float(self.var_target_high.get())
            window_seconds = max(1.0, float(self.var_window.get()))
            hop_seconds = max(1.0, float(self.var_hop.get()))
            workers = max(MIN_ANALYSIS_WORKER_THREADS, min(MAX_ANALYSIS_WORKER_THREADS, int(float(self.var_workers.get()))))
            max_reduction = max(0.0, float(self.var_max_reduction.get()))
            bass_max_reduction = max(
                MIN_BASS_MAX_BOOST_REDUCTION_DB,
                min(MAX_BASS_MAX_BOOST_REDUCTION_DB, float(self.var_bass_max_reduction.get())),
            )
            bass_penalty_start = max(
                MIN_BASS_PENALTY_THRESHOLD_DB,
                min(MAX_BASS_PENALTY_THRESHOLD_DB, float(self.var_bass_penalty_start.get())),
            )
            bass_penalty_full = max(
                MIN_BASS_PENALTY_THRESHOLD_DB,
                min(MAX_BASS_PENALTY_THRESHOLD_DB, float(self.var_bass_penalty_full.get())),
            )
            sub_penalty_start = max(
                MIN_BASS_PENALTY_THRESHOLD_DB,
                min(MAX_BASS_PENALTY_THRESHOLD_DB, float(self.var_sub_penalty_start.get())),
            )
            sub_penalty_full = max(
                MIN_BASS_PENALTY_THRESHOLD_DB,
                min(MAX_BASS_PENALTY_THRESHOLD_DB, float(self.var_sub_penalty_full.get())),
            )
            peak_ceiling = float(self.var_peak_ceiling.get())
            normalization_mode = normalize_normalization_mode(self.var_normalization_mode.get())
            limiter_engine = normalize_limiter_engine(self.var_limiter_engine.get())
            write_csv = bool(self.var_write_csv.get())
            mp3_threshold = max(0.0, float(self.var_mp3_threshold.get()))
            lossless_threshold = max(0.0, float(self.var_lossless_threshold.get()))
            output_format_mode = normalize_output_format_mode(self.var_output_format_mode.get())
            allow_risky_true_peak_boost = bool(self.var_allow_risky_true_peak_boost.get())
            apply_render_gain_threshold = bool(self.var_apply_render_gain_threshold.get())
            output_root = self.var_output_folder.get().strip() or None
        except Exception as exc:
            messagebox.showerror("Invalid settings", f"Check the numeric settings.\n\n{exc}")
            return

        if target_low > target_high:
            messagebox.showerror("Invalid target", "Target low must not be higher than target high.\n\nFor example: low -8.0, high -7.0.")
            return

        if hop_seconds > window_seconds:
            hop_seconds = window_seconds
            self.var_hop.set(hop_seconds)

        self.var_workers.set(workers)
        self.var_normalization_mode.set(normalization_mode)

        run_signature = self._analysis_signature_from_values(
            folder=folder,
            window_seconds=window_seconds,
            hop_seconds=hop_seconds,
        )

        if pipeline == "review_render" and run_signature != self._analysis_signature:
            messagebox.showwarning(
                "Re-analyze required",
                "The analysis was run with different settings. Re-analyze before rendering.",
            )
            self._set_idle_state()
            return

        self._flush_pending_settings_save()
        self._cancel_flag.clear()
        self._clear_log()
        self._reset_run_counts()

        if pipeline in {"analyze_only", "batch"}:
            self._analyzed_rows = []
            self._analyzed_work_items = {}
            self._analysis_signature = None
            self._active_run_signature = run_signature
            self._populate_results_table(self._analyzed_rows)
        else:
            self._active_run_signature = None

        self._begin_operation_run(
            pipeline,
            {
                "batch": "Starting analyze + create copies...",
                "analyze_only": "Starting analyze library run...",
                "review_render": "Starting render...",
            }[pipeline],
        )
        self._set_busy_state()

        mode_text = {
            "batch": "analyze + create copies",
            "analyze_only": "analyze library",
            "review_render": "render analyzed",
        }[pipeline]
        self._logger.info(APP_TITLE)
        self._logger.info("-" * 78)
        self._logger.info("Mode:   %s", mode_text)
        self._logger.info("Gain:   %s", normalization_mode)
        self._logger.info("Limiter: %s", limiter_engine)
        if pipeline == "review_render":
            self._logger.info("Analysis workers: not used")
        else:
            self._logger.info("Analysis workers: %s", workers)
        self._logger.info("Render workers: %s (clean gain); limiter stays single-threaded", DEFAULT_RENDER_WORKER_THREADS)
        self._logger.info("Priority: true-peak ceiling, then loudness target")
        self._logger.info("Folder: %s", folder)
        if output_root:
            self._logger.info("Output folder: %s", output_root)
        else:
            self._logger.info("Output folder: beside originals (%s suffix)", PROCESSED_SUFFIX)
        self._logger.info("CSV:    %s", csv_path if write_csv else "disabled")
        self._logger.info("Output: %s", output_format_mode_description(output_format_mode))
        self._logger.info("-" * 78)
        if pipeline == "analyze_only":
            self._logger.info("Analyze Library run. No processed copies will be created.")
        elif pipeline == "batch":
            self._logger.info(
                "Batch run. Supported tracks will be analyzed, then rendered with suffix %s.",
                PROCESSED_SUFFIX,
            )
        else:
            self._logger.info("Rendering analyzed tracks with suffix %s.", PROCESSED_SUFFIX)
        self._logger.info("")

        all_analyzed_rows = list(self._analyzed_rows)
        all_analyzed_work_items = dict(self._analyzed_work_items)

        self._worker_thread = threading.Thread(
            target=self._run_safely,
            args=(
                folder,
                csv_path,
                target_low,
                target_high,
                window_seconds,
                hop_seconds,
                max_reduction,
                bass_max_reduction,
                bass_penalty_start,
                bass_penalty_full,
                sub_penalty_start,
                sub_penalty_full,
                peak_ceiling,
                normalization_mode,
                limiter_engine,
                workers,
                pipeline,
                write_csv,
                mp3_threshold,
                lossless_threshold,
                output_format_mode,
                allow_risky_true_peak_boost,
                apply_render_gain_threshold,
                output_root,
                all_analyzed_rows,
                all_analyzed_work_items,
                run_signature,
            ),
            daemon=False,
        )
        self._worker_thread.start()

    def _cancel(self) -> None:
        self._cancel_flag.set()
        if hasattr(self, "var_operation_phase"):
            self._set_operation_phase_display("Cancelling")
            self.var_operation_fraction.set("")
        self._refresh_operation_elapsed()

    # ---------------------------------------------------------------------
    # Worker thread
    # ---------------------------------------------------------------------

    @staticmethod
    def _throttle_gui_progress(sink: Callable[[str, Any], None]) -> Callable[[str, Any], None]:
        """Emit GUI progress ticks at a bounded rate while preserving backend work."""
        state = {
            "last_emit": 0.0,
            "pending": None,
            "last_errors": 0,
            "last_warnings": 0,
        }

        def emit_pending() -> None:
            pending = state["pending"]
            if pending is None:
                return
            sink("tick", pending)
            state["last_emit"] = time.monotonic()
            _done, _total, _rate, _eta, errors, counts = pending
            state["last_errors"] = int(errors)
            state["last_warnings"] = int(counts.get("warnings", 0))
            state["pending"] = None

        def on_progress(kind: str, data: Any) -> None:
            if kind == "tick":
                state["pending"] = data
                _done, _total, _rate, _eta, errors, counts = data
                warnings = int(counts.get("warnings", 0))
                force = (
                    int(errors) > state["last_errors"]
                    or warnings > state["last_warnings"]
                )
                now = time.monotonic()
                if force or now - state["last_emit"] >= GUI_TICK_MIN_INTERVAL_SEC:
                    emit_pending()
                return

            if kind in {"finished", "cancelled", "fatal"}:
                emit_pending()

            sink(kind, data)

        return on_progress

    def _run_safely(
        self,
        folder: str,
        csv_path: str,
        target_low: float,
        target_high: float,
        window_seconds: float,
        hop_seconds: float,
        max_reduction: float,
        bass_max_reduction: float,
        bass_penalty_start: float,
        bass_penalty_full: float,
        sub_penalty_start: float,
        sub_penalty_full: float,
        peak_ceiling: float,
        normalization_mode: str,
        limiter_engine: str,
        workers: int,
        pipeline: str,
        write_csv: bool,
        mp3_threshold: float,
        lossless_threshold: float,
        output_format_mode: str,
        allow_risky_true_peak_boost: bool,
        apply_render_gain_threshold: bool,
        output_root: str | None,
        all_analyzed_rows: list[dict[str, object]],
        all_analyzed_work_items: dict[str, AnalyzedWorkItem],
        run_signature: dict[str, object],
    ) -> None:
        try:
            settings = DropGainSettings(
                folder=folder,
                csv_path=csv_path,
                target_low_lufs=target_low,
                target_high_lufs=target_high,
                window_seconds=window_seconds,
                hop_seconds=hop_seconds,
                max_reduction_db=max_reduction,
                bass_max_reduction_db=bass_max_reduction,
                bass_penalty_start_db=bass_penalty_start,
                bass_penalty_full_db=bass_penalty_full,
                sub_penalty_start_db=sub_penalty_start,
                sub_penalty_full_db=sub_penalty_full,
                peak_ceiling_dbfs=peak_ceiling,
                normalization_mode=normalization_mode,
                limiter_engine=limiter_engine,
                analysis_workers=workers,
                render_workers=DEFAULT_RENDER_WORKER_THREADS,
                analyze_only=(pipeline == "analyze_only"),
                write_csv=write_csv,
                mp3_threshold=mp3_threshold,
                lossless_threshold=lossless_threshold,
                output_format_mode=output_format_mode,
                allow_risky_true_peak_boost=allow_risky_true_peak_boost,
                apply_render_gain_threshold=apply_render_gain_threshold,
                output_root=output_root,
            )

            on_progress = self._throttle_gui_progress(
                lambda kind, data: self._queue.put((kind, data))
            )

            if pipeline == "batch":
                rows, work_items = run_batch_job(
                    settings=settings,
                    on_progress=on_progress,
                    cancel_flag=self._cancel_flag,
                    logger=self._logger,
                )
                self._queue.put(("batch_rows", (rows, run_signature, work_items)))
            elif pipeline == "analyze_only":
                rows, work_items = run_analysis_job(
                    settings=settings,
                    on_progress=on_progress,
                    cancel_flag=self._cancel_flag,
                    logger=self._logger,
                    apply_gain_threshold=settings.apply_render_gain_threshold,
                )
                self._queue.put(("analysis_rows", (rows, run_signature, work_items)))
            else:
                rows = run_processing_job(
                    settings=settings,
                    all_rows=list(all_analyzed_rows),
                    on_progress=on_progress,
                    cancel_flag=self._cancel_flag,
                    logger=self._logger,
                    work_items=all_analyzed_work_items,
                    refresh_stale_would_process=True,
                )
                self._queue.put(("processing_rows", rows))
        except Exception as exc:
            self._logger.exception("Fatal error: %s", exc)
            self._queue.put(("fatal", None))

    # ---------------------------------------------------------------------
    # Main-thread queue polling
    # ---------------------------------------------------------------------

    def _poll_queue(self) -> None:
        stop_polling = False
        try:
            try:
                kind, data = self._queue.get_nowait()
            except queue.Empty:
                return

            try:
                with benchmark_timer("GUI progress overhead", self._logger):
                    while True:
                        if kind == "log":
                            self._log(str(data))
                        elif kind == "log_error":
                            self._log(str(data), "error")
                        elif kind == "log_message":
                            text, tag = data  # type: ignore[misc]
                            self._log(str(text), tag)
                        elif kind == "status":
                            self._set_telemetry_status(str(data))
                            self._refresh_results_empty_message()
                        elif kind == "phase":
                            self._set_active_phase(str(data))
                        elif kind == "batch_phase":
                            self._on_batch_phase(data)
                        elif kind == "progress_max":
                            self._set_progress_max(max(int(data), 1))
                        elif kind == "summary_counts":
                            self._run_counts = dict(data)  # type: ignore[arg-type]
                            self.var_run_summary.set(self._format_run_counts(self._run_counts))
                            self._update_summary_cards()
                        elif kind == "tick":
                            self._on_progress_tick(data)
                        elif kind == "analysis_rows":
                            rows, signature, work_items = data  # type: ignore[misc]
                            self._analyzed_rows = list(rows)
                            self._analyzed_work_items = dict(work_items)
                            self._analysis_signature = dict(signature)
                            self._active_run_signature = None
                            self._populate_results_table(self._analyzed_rows)
                            if self.library_tuning_page is not None:
                                self.library_tuning_page.refresh_from_app()
                            self._set_idle_state()
                        elif kind == "batch_rows":
                            rows, signature, work_items = data  # type: ignore[misc]
                            self._analyzed_rows = list(rows)
                            self._analyzed_work_items = dict(work_items)
                            self._analysis_signature = dict(signature)
                            self._active_run_signature = None
                            self._populate_results_table(self._analyzed_rows)
                            if self.library_tuning_page is not None:
                                self.library_tuning_page.refresh_from_app()
                            self._set_idle_state()
                        elif kind == "processing_rows":
                            self._analyzed_rows = list(data)  # type: ignore[arg-type]
                            self._populate_results_table(self._analyzed_rows)
                            self._set_idle_state()
                        elif kind == "finished":
                            self._run_completed = True
                            self._set_idle_state()
                            self._set_telemetry_status("Done.")
                            if self._close_requested:
                                self._finish_close()
                                stop_polling = True
                                return
                        elif kind == "cancelled":
                            self._set_idle_state()
                            self._set_telemetry_status("Cancelled.")
                            if self._close_requested:
                                self._finish_close()
                                stop_polling = True
                                return
                        elif kind == "fatal":
                            self._set_idle_state()
                            self._set_telemetry_status("Error. See output above.")
                            self._show_fatal_job_error_dialog()
                            if self._close_requested:
                                self._finish_close()
                                stop_polling = True
                                return
                        elif kind == "thread_error":
                            thread_name, _detail = data  # type: ignore[misc]
                            self._show_unexpected_error_dialog(
                                "Background thread error",
                                thread_name=str(thread_name),
                            )
                        elif kind == "callback_error":
                            self._show_unexpected_error_dialog("Unexpected error")
                        elif kind == "waveform_preview":
                            request_id, preview = data  # type: ignore[misc]
                            if int(request_id) == self._waveform_request_id:
                                self._show_waveform_preview(dict(preview))
                        elif kind == "waveform_error":
                            request_id, message, filename = data  # type: ignore[misc]
                            if int(request_id) == self._waveform_request_id:
                                self._show_waveform_error(str(message), str(filename))

                        kind, data = self._queue.get_nowait()
            except queue.Empty:
                pass
            except Exception:
                self._logger.exception("GUI queue handler failed")
        finally:
            if not stop_polling:
                try:
                    if self.winfo_exists():
                        if not self._is_run_busy() and self._active_phase != "idle":
                            self._set_idle_state()
                        self.after(100, self._poll_queue)
                except tk.TclError:
                    pass

    # ---------------------------------------------------------------------
    # Log and table helpers
    # ---------------------------------------------------------------------

    def _log(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        if tag:
            self.log.insert("end", text, tag)
        else:
            self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    @staticmethod
    def _optional_float(value: object) -> float | None:
        try:
            number = float(value)
        except Exception:
            return None
        if not math.isfinite(number):
            return None
        return number

    @staticmethod
    def _row_warnings_text(row: dict[str, object]) -> str:
        parts = [
            str(row.get("warnings", "") or "").strip(),
            str(row.get("decision_notes", "") or "").strip(),
            str(row.get("processing_error", "") or "").strip(),
        ]
        return "; ".join(part for part in parts if part)

    @staticmethod
    def _format_db(value: object) -> str:
        try:
            return f"{float(value):+.2f} dB"
        except Exception:
            return "--"

    @staticmethod
    def _format_lufs(value: object) -> str:
        try:
            return f"{float(value):.2f} LUFS"
        except Exception:
            return "--"

    @staticmethod
    def _format_dbtp(value: object) -> str:
        try:
            return f"{float(value):.2f} dBTP"
        except Exception:
            return "--"

    @staticmethod
    def _status_category(row: dict[str, object]) -> str:
        status = str(row.get("processing_status", "") or "")
        audio_verification = str(row.get("audio_verification", "") or "")
        metadata_verification = str(row.get("metadata_verification", "") or "")
        if status == "analyzed_would_process":
            return "Ready"
        if status == "analyzed_already_in_target_range":
            return "Already OK"
        if status in {"analyzed_mp3_gain_below_threshold", "analyzed_lossless_gain_below_threshold"}:
            return "Below threshold"
        if status == "analyzed_zero_gain_mp3_render_skipped":
            return "Zero-gain MP3 skip"
        if status == "analyzed_needs_manual_check":
            return "Manual check"
        if status == "analyzed_output_exists":
            return "Output exists"
        if status == "processed":
            return "Processed"
        if status == "processed_warning":
            return "Warning"
        if "error" in status or status == "failed":
            return "Error"
        if audio_verification == "warning" or metadata_verification == "warning":
            return "Warning"
        return "Skipped"

    @staticmethod
    def _status_display(row: dict[str, object]) -> str:
        labels = {
            "Ready": "● Ready",
            "Already OK": "● In range",
            "Below threshold": "● Below min",
            "Zero-gain MP3 skip": "● Zero-gain skip",
            "Manual check": "● Manual check",
            "Output exists": "● Output exists",
            "Processed": "● Processed",
            "Warning": "● Warning",
            "Error": "● Error",
            "Skipped": "● Skipped",
        }
        category = App._status_category(row)
        return labels.get(category, f"● {category}")

    @staticmethod
    def _row_status_severity(row: dict[str, object]) -> str:
        status = str(row.get("processing_status", "") or "")
        if "error" in status or status == "failed":
            return "error"
        if str(row.get("processing_error", "") or "").strip():
            return "error"
        if App._status_category(row) == "Warning":
            return "warn"
        if App._status_category(row) in {"Ready", "Already OK", "Processed"}:
            return "ok"
        return ""

    @staticmethod
    def _limiting_label(row: dict[str, object]) -> str:
        return format_peak_control_display(
            row.get("estimated_peak_control_db"),
            row.get("processing_engine"),
            include_percent=False,
        )

    def _refresh_create_button_text(self) -> None:
        eligible_count = len(self._renderable_analysis_indices())
        if self._analyzed_rows and not self._analysis_is_current():
            self.btn_start.configure(text="Render Analyzed (re-analyze required)")
        elif eligible_count > 0:
            self.btn_start.configure(text=f"Render Analyzed ({eligible_count})")
        else:
            self.btn_start.configure(text="Render Analyzed")

    def _resize_results_table_columns(self, event: tk.Event[tk.Widget] | None = None) -> None:
        width = int(getattr(event, "width", 0) or self.results_table.winfo_width() or 0)
        if width <= 1:
            return

        usable_width = max(width - 4, 1)
        column_widths = self._results_table_column_widths_from_content()
        total = sum(column_widths[column_id] for column_id, *_rest in RESULTS_TABLE_COLUMNS)

        if total < usable_width:
            extra = usable_width - total
            filename_headroom = max(
                0,
                self._scaled(RESULTS_TABLE_FILENAME_ABSOLUTE_MAX) - column_widths["filename"],
            )
            filename_extra = min(extra, filename_headroom)
            column_widths["filename"] += filename_extra
            column_widths["warnings"] += extra - filename_extra

        for column_id, _heading, anchor, _sample, _tooltip in RESULTS_TABLE_COLUMNS:
            col_width = column_widths[column_id]
            self.results_table.column(
                column_id,
                width=col_width,
                minwidth=col_width,
                anchor=anchor,
                stretch=False,
            )

    def _current_dropgain_settings(self) -> DropGainSettings | None:
        try:
            folder = self.var_folder.get().strip()
            if not folder:
                return None
            workers = max(
                MIN_ANALYSIS_WORKER_THREADS,
                min(MAX_ANALYSIS_WORKER_THREADS, int(float(self.var_workers.get()))),
            )
            return DropGainSettings(
                folder=folder,
                csv_path=self.var_csv.get().strip(),
                target_low_lufs=float(self.var_target_low.get()),
                target_high_lufs=float(self.var_target_high.get()),
                window_seconds=max(1.0, float(self.var_window.get())),
                hop_seconds=max(1.0, float(self.var_hop.get())),
                max_reduction_db=max(0.0, float(self.var_max_reduction.get())),
                bass_max_reduction_db=max(
                    MIN_BASS_MAX_BOOST_REDUCTION_DB,
                    min(MAX_BASS_MAX_BOOST_REDUCTION_DB, float(self.var_bass_max_reduction.get())),
                ),
                bass_penalty_start_db=max(
                    MIN_BASS_PENALTY_THRESHOLD_DB,
                    min(MAX_BASS_PENALTY_THRESHOLD_DB, float(self.var_bass_penalty_start.get())),
                ),
                bass_penalty_full_db=max(
                    MIN_BASS_PENALTY_THRESHOLD_DB,
                    min(MAX_BASS_PENALTY_THRESHOLD_DB, float(self.var_bass_penalty_full.get())),
                ),
                sub_penalty_start_db=max(
                    MIN_BASS_PENALTY_THRESHOLD_DB,
                    min(MAX_BASS_PENALTY_THRESHOLD_DB, float(self.var_sub_penalty_start.get())),
                ),
                sub_penalty_full_db=max(
                    MIN_BASS_PENALTY_THRESHOLD_DB,
                    min(MAX_BASS_PENALTY_THRESHOLD_DB, float(self.var_sub_penalty_full.get())),
                ),
                peak_ceiling_dbfs=float(self.var_peak_ceiling.get()),
                normalization_mode=normalize_normalization_mode(self.var_normalization_mode.get()),
                limiter_engine=normalize_limiter_engine(self.var_limiter_engine.get()),
                analysis_workers=workers,
                render_workers=DEFAULT_RENDER_WORKER_THREADS,
                analyze_only=True,
                write_csv=bool(self.var_write_csv.get()),
                mp3_threshold=max(0.0, float(self.var_mp3_threshold.get())),
                lossless_threshold=max(0.0, float(self.var_lossless_threshold.get())),
                output_format_mode=normalize_output_format_mode(self.var_output_format_mode.get()),
                allow_risky_true_peak_boost=bool(self.var_allow_risky_true_peak_boost.get()),
                apply_render_gain_threshold=bool(self.var_apply_render_gain_threshold.get()),
                output_root=self.var_output_folder.get().strip() or None,
            )
        except Exception:
            return None

    def _renderable_analysis_indices(self) -> set[int]:
        if not self._analysis_is_current():
            return set()
        settings = self._current_dropgain_settings()
        if settings is None:
            return set()
        return set(eligible_render_indices(settings, list(self._analyzed_rows)))  # type: ignore[arg-type]

    def _results_table_values_for_row(self, row: dict[str, object]) -> tuple[str, ...]:
        return (
            str(row.get("filename", "") or ""),
            self._format_db(row.get("suggested_gain_db")),
            self._format_lufs(row.get("loudest_section_lufs")),
            self._format_lufs(row.get("projected_loudest_section_lufs")),
            self._format_dbtp(row.get("true_peak_dbtp")),
            self._format_dbtp(row.get("projected_true_peak_dbtp")),
            self._limiting_label(row),
            self._format_db(row.get("bass_strength_db")),
            self._format_db(row.get("sub_strength_db")),
            self._status_display(row),
            self._row_warnings_text(row),
        )

    @staticmethod
    def _results_table_tags_for_index(index: int, row: dict[str, object]) -> tuple[str, ...]:
        stripe_tag = "odd" if index % 2 else "even"
        severity = App._row_status_severity(row)
        row_tag = f"{stripe_tag}_{severity}" if severity else stripe_tag
        return (row_tag,)

    def _schedule_resize_results_table_columns(self) -> None:
        self._schedule_debounced(
            "_results_table_resize_after_id",
            self._apply_results_table_column_resize,
            RESULTS_TABLE_RESIZE_DEBOUNCE_MS,
        )

    def _apply_results_table_column_resize(self) -> None:
        self._results_table_resize_after_id = None
        self._resize_results_table_columns()

    def _sync_results_table_rows(
        self,
        rows: list[dict[str, object]],
        *,
        resize_columns: bool = True,
    ) -> None:
        children = list(self.results_table.get_children())
        if len(children) != len(rows) or any(child != str(index) for index, child in enumerate(children)):
            self._populate_results_table(rows)
            return

        selected = self.results_table.selection()
        selected_iid = str(selected[0]) if selected else None
        content_changed = False

        for index, row in enumerate(rows):
            iid = str(index)
            values = self._results_table_values_for_row(row)
            tags = self._results_table_tags_for_index(index, row)
            if tuple(self.results_table.item(iid, "values")) != values:
                self.results_table.item(iid, values=values)
                content_changed = True
            self.results_table.item(iid, tags=tags)

        self._update_results_empty_state(has_rows=bool(rows))
        self._refresh_create_button_text()

        if resize_columns and content_changed:
            self._schedule_resize_results_table_columns()

        if selected_iid and selected_iid in children:
            self.results_table.selection_set(selected_iid)
            self.results_table.focus(selected_iid)
            self._sync_results_table_selection_appearance()
        elif children:
            first_item = str(children[0])
            self.results_table.selection_set(first_item)
            self.results_table.focus(first_item)
            self._sync_results_table_selection_appearance()

    def _populate_results_table(self, rows: list[dict[str, object]]) -> None:
        self.results_table.delete(*self.results_table.get_children())
        self._clear_waveform_preview()
        self._update_results_empty_state(has_rows=bool(rows))

        for index, row in enumerate(rows):
            self.results_table.insert(
                "",
                "end",
                iid=str(index),
                tags=self._results_table_tags_for_index(index, row),
                values=self._results_table_values_for_row(row),
            )
        self._refresh_create_button_text()
        self._schedule_resize_results_table_columns()

        children = self.results_table.get_children()
        if children:
            first_item = str(children[0])
            self.results_table.selection_set(first_item)
            self.results_table.focus(first_item)
            self._sync_results_table_selection_appearance()
            self._queue_waveform_for_item(first_item)
        else:
            self._sync_results_table_selection_appearance()

    def _flush_pending_settings_save(self) -> None:
        if self._save_settings_after_id is not None:
            try:
                self.after_cancel(self._save_settings_after_id)
            except Exception:
                pass
            self._save_settings_after_id = None
        self._save_settings()

    def _has_unrendered_analyzed_rows(self) -> bool:
        return any(
            str(row.get("processing_status", "")).startswith("analyzed_")
            for row in self._analyzed_rows
        )

    def _quit_confirmation_message(self) -> str | None:
        busy = self._is_run_busy()
        unrendered = self._has_unrendered_analyzed_rows()
        if not busy and not unrendered:
            return None
        if busy and unrendered:
            return (
                "A job is still running and unrendered analysis results are in memory.\n\n"
                "Closing now will cancel the running job and discard the analyzed results. "
                "Are you sure you want to quit?"
            )
        if busy:
            return (
                "A job is still running.\n\n"
                "Closing now will cancel it and discard any results collected so far. "
                "Are you sure you want to quit?"
            )
        return (
            "You have unrendered analysis results that will be lost when you close.\n\n"
            "Render the analyzed tracks first if you want to keep them. "
            "Are you sure you want to quit?"
        )

    def _finish_close(self) -> None:
        self._flush_pending_settings_save()
        self._restore_exception_handlers()
        self._shutdown_waveform_worker()
        shutdown_prol2_render_host()
        self._shutdown_logging()
        self.destroy()

    def _close_app(self) -> None:
        if self._close_requested:
            return

        message = self._quit_confirmation_message()
        if message is not None and not messagebox.askyesno("Quit DropGain?", message, icon="warning"):
            return

        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._close_requested = True
            self._cancel_flag.set()
            self._set_telemetry_status("Closing after current track(s) finish...")
            if hasattr(self, "var_operation_phase"):
                self._set_operation_phase_display("Cancelling")
                self.var_operation_fraction.set("")
            self._refresh_results_empty_message()
            self._logger.warning("Close requested. Cancelling after current file(s) finish.")
            try:
                self._apply_action_button_state(self.btn_cancel, "disabled")
            except Exception:
                pass
            return
        self._finish_close()
