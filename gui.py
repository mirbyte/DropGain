from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any
import tkinter as tk

from analysis import (
    APP_TITLE,
    DEFAULT_BASS_BASE_RATIO,
    DEFAULT_BASS_NOD_SENSITIVITY,
    DEFAULT_BOOST_PEAK_CEILING_DBFS,
    DEFAULT_LOUD_SECTION_HOP_SECONDS,
    DEFAULT_LOUD_SECTION_WINDOW_SECONDS,
    DEFAULT_MAX_BOOST_DB,
    DEFAULT_TARGET_HIGH_LUFS,
    DEFAULT_TARGET_LOW_LUFS,
    DEFAULT_WORKER_THREADS,
    DEFAULT_LOSSLESS_MIN_ABS_GAIN_DB,
    DEFAULT_MP3_MIN_ABS_GAIN_DB,
    MAX_LOUD_SECTION_HOP_SECONDS,
    MAX_LOUD_SECTION_WINDOW_SECONDS,
    MAX_WORKER_THREADS,
    METER_SAMPLE_RATE,
    MIN_LOUD_SECTION_HOP_SECONDS,
    MIN_LOUD_SECTION_WINDOW_SECONDS,
    MIN_WORKER_THREADS,
    MP3_OUTPUT_BITRATE,
    PROCESS_OVERWRITE_EXISTING,
    PROCESSED_SUFFIX,
    SUPPORTED_EXTENSIONS,
    TrackRow,
    analyze_file,
    append_note,
    build_summary,
    check_ffmpeg_available,
    find_audio_files,
    ffprobe_audio_info,
    infer_bit_depth,
    parse_float_or_default,
    round_or_blank,
    script_folder,
)
from processing import (
    POST_VERIFY_PROCESSED_AUDIO,
    STRICT_VERIFY_LOSSLESS_OUTPUT,
    process_audio_with_gain,
    should_process_row,
    verify_metadata,
    verify_processed_audio_fast,
)

SETTINGS_FILE_NAME = "dropgain_settings.json"
SETTINGS_SCHEMA_VERSION = 5
LEGACY_SETTINGS_FILE_NAMES = ("drop_lufs_settings.json",)
LEGACY_DEFAULT_TARGET_LOW_LUFS = -7.5
LEGACY_DEFAULT_TARGET_HIGH_LUFS = -5.5
LEGACY_DEFAULT_PEAK_CEILING_DBFS = 0.0
OUTPUT_MODE_NEXT_TO_ORIGINALS = "Next to originals"
OUTPUT_MODE_CUSTOM_FOLDER = "Custom folder"
OUTPUT_MODE_CHOICES = (OUTPUT_MODE_NEXT_TO_ORIGINALS, OUTPUT_MODE_CUSTOM_FOLDER)
LOG_FILE_NAME = "dropgain.log"

RUN_COUNT_KEYS = (
    "processed",
    "would_process",
    "analyzed_only",
    "skipped",
    "warnings",
    "errors",
)


def enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return

    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


enable_windows_dpi_awareness()


# -------------------------------------------------------------------------
# GUI Application
# -------------------------------------------------------------------------


class GuiQueueLogHandler(logging.Handler):
    def __init__(self, ui_queue: queue.Queue[tuple[str, object]]) -> None:
        super().__init__()
        self._ui_queue = ui_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            if not message.endswith("\n"):
                message += "\n"

            if record.levelno >= logging.ERROR:
                tag = "error"
            elif record.levelno >= logging.WARNING:
                tag = "warn"
            else:
                tag = None

            self._ui_queue.put(("log_message", (message, tag)))
        except Exception:
            self.handleError(record)


class App(tk.Tk):
    """Desktop UI"""

    def __init__(self) -> None:
        super().__init__()

        self.title(APP_TITLE)
        self.minsize(940, 1150)

        self._settings = self._load_settings()
        self._run_counts = self._empty_run_counts()

        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._log_record_queue: queue.Queue[logging.LogRecord] = queue.Queue()
        self._cancel_flag = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._logger = logging.getLogger("dropgain")
        self._log_listener: logging.handlers.QueueListener | None = None

        self._configure_logging()
        self._build_ui()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._close_app)

        self.update_idletasks()
        width = 1120
        desired_height = 1500
        screen_height = self.winfo_screenheight()
        height = min(desired_height, max(1000, screen_height - 80))
        x = (self.winfo_screenwidth() - width) // 2
        y = max(0, (screen_height - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

        self._logger.info("Logging to %s", script_folder() / LOG_FILE_NAME)

    # -------------------------------------------------------------------------
    # Settings
    # -------------------------------------------------------------------------

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

    def _reset_run_counts(self) -> None:
        self._run_counts = self._empty_run_counts()
        if hasattr(self, "var_run_summary"):
            self.var_run_summary.set(self._format_run_counts(self._run_counts))

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
            return bool(settings.get(key, default))
        except Exception:
            return default

    def _upgrade_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        upgraded = dict(settings)

        schema_version = self._setting_int(upgraded, "settings_schema_version", 0)
        if schema_version < SETTINGS_SCHEMA_VERSION:
            target_low = self._setting_float(
                upgraded,
                "target_low",
                LEGACY_DEFAULT_TARGET_LOW_LUFS,
            )
            target_high = self._setting_float(
                upgraded,
                "target_high",
                LEGACY_DEFAULT_TARGET_HIGH_LUFS,
            )
            peak_ceiling = self._setting_float(
                upgraded,
                "peak_ceiling",
                LEGACY_DEFAULT_PEAK_CEILING_DBFS,
            )

            if (
                abs(target_low - LEGACY_DEFAULT_TARGET_LOW_LUFS) < 0.001
                and abs(target_high - LEGACY_DEFAULT_TARGET_HIGH_LUFS) < 0.001
            ):
                upgraded["target_low"] = DEFAULT_TARGET_LOW_LUFS
                upgraded["target_high"] = DEFAULT_TARGET_HIGH_LUFS

            if abs(peak_ceiling - LEGACY_DEFAULT_PEAK_CEILING_DBFS) < 0.001:
                upgraded["peak_ceiling"] = DEFAULT_BOOST_PEAK_CEILING_DBFS

            if "bass_base_ratio" not in upgraded:
                upgraded["bass_base_ratio"] = DEFAULT_BASS_BASE_RATIO
            if "bass_nod_sensitivity" not in upgraded:
                upgraded["bass_nod_sensitivity"] = DEFAULT_BASS_NOD_SENSITIVITY
            if "preserve_mtime" not in upgraded:
                upgraded["preserve_mtime"] = False
            if "lossless_threshold" not in upgraded:
                upgraded["lossless_threshold"] = DEFAULT_LOSSLESS_MIN_ABS_GAIN_DB
            if "mp3_threshold" not in upgraded:
                upgraded["mp3_threshold"] = DEFAULT_MP3_MIN_ABS_GAIN_DB

        upgraded["settings_schema_version"] = SETTINGS_SCHEMA_VERSION
        return upgraded

    def _load_settings(self) -> dict[str, Any]:
        paths = [self._settings_path()]
        paths.extend(str(script_folder() / name) for name in LEGACY_SETTINGS_FILE_NAMES)

        for path in paths:
            try:
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    return self._upgrade_settings(data)
            except Exception:
                continue

        return {}

    def _save_settings(self) -> None:
        if not hasattr(self, "var_folder"):
            return

        data = {
            "settings_schema_version": SETTINGS_SCHEMA_VERSION,
            "last_folder": self.var_folder.get().strip(),
            "output_mode": self.var_output_mode.get().strip(),
            "output_folder": self.var_output_folder.get().strip(),
            "target_low": float(self.var_target_low.get()),
            "target_high": float(self.var_target_high.get()),
            "window_seconds": float(self.var_window.get()),
            "hop_seconds": float(self.var_hop.get()),
            "workers": int(self.var_workers.get()),
            "max_boost": float(self.var_max_boost.get()),
            "peak_ceiling": float(self.var_peak_ceiling.get()),
            "bass_base_ratio": float(self.var_bass_base_ratio.get()),
            "bass_nod_sensitivity": float(self.var_bass_nod_sensitivity.get()),
            "preserve_mtime": self.var_preserve_mtime.get(),
            "lossless_threshold": float(self.var_lossless_threshold.get()),
            "mp3_threshold": float(self.var_mp3_threshold.get()),
        }

        try:
            with open(self._settings_path(), "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
        except Exception:
            pass

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

    # -------------------------------------------------------------------------
    # UI Construction
    # -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        settings = self._settings

        self.columnconfigure(0, weight=1)
        self.rowconfigure(6, weight=1)

        paths_frame = ttk.LabelFrame(self, text="Source and Output", padding=10)
        paths_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
        paths_frame.columnconfigure(1, weight=1)

        ttk.Label(paths_frame, text="Source directory:").grid(row=0, column=0, sticky="w")
        self.var_folder = tk.StringVar(value=str(settings.get("last_folder") or ""))
        ttk.Entry(paths_frame, textvariable=self.var_folder).grid(
            row=0, column=1, sticky="ew", padx=(8, 6)
        )
        ttk.Button(paths_frame, text="Browse...", command=self._pick_folder).grid(row=0, column=2)

        output_mode = str(settings.get("output_mode") or OUTPUT_MODE_NEXT_TO_ORIGINALS)
        if output_mode not in OUTPUT_MODE_CHOICES:
            output_mode = OUTPUT_MODE_NEXT_TO_ORIGINALS

        self.var_output_mode = tk.StringVar(value=output_mode)
        self.var_output_folder = tk.StringVar(value=str(settings.get("output_folder") or ""))

        ttk.Label(paths_frame, text="Output location:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.output_mode_combo = ttk.Combobox(
            paths_frame,
            textvariable=self.var_output_mode,
            values=OUTPUT_MODE_CHOICES,
            state="readonly",
            width=24,
        )
        self.output_mode_combo.grid(row=1, column=1, sticky="w", padx=(8, 6), pady=(6, 0))
        self.output_mode_combo.bind("<<ComboboxSelected>>", self._output_mode_changed)

        ttk.Label(paths_frame, text="Output folder:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.output_folder_entry = ttk.Entry(paths_frame, textvariable=self.var_output_folder)
        self.output_folder_entry.grid(row=2, column=1, sticky="ew", padx=(8, 6), pady=(6, 0))
        self.btn_output_folder = ttk.Button(
            paths_frame,
            text="Browse...",
            command=self._pick_output_folder,
        )
        self.btn_output_folder.grid(row=2, column=2, pady=(6, 0))

        self.var_output_note = tk.StringVar()
        ttk.Label(paths_frame, textvariable=self.var_output_note, foreground="gray").grid(
            row=3,
            column=1,
            columnspan=2,
            sticky="w",
            padx=(8, 6),
            pady=(4, 0),
        )

        analysis_frame = ttk.LabelFrame(self, text="Analysis settings", padding=10)
        analysis_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=4)

        self.var_window = tk.DoubleVar(
            value=self._setting_float(settings, "window_seconds", DEFAULT_LOUD_SECTION_WINDOW_SECONDS)
        )
        self.var_hop = tk.DoubleVar(
            value=self._setting_float(settings, "hop_seconds", DEFAULT_LOUD_SECTION_HOP_SECONDS)
        )
        self.var_workers = tk.IntVar(
            value=self._setting_int(settings, "workers", DEFAULT_WORKER_THREADS)
        )

        ttk.Label(analysis_frame, text="Analysis window:").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            analysis_frame,
            from_=MIN_LOUD_SECTION_WINDOW_SECONDS,
            to=MAX_LOUD_SECTION_WINDOW_SECONDS,
            increment=5.0,
            textvariable=self.var_window,
            width=7,
        ).grid(row=0, column=1, sticky="w", padx=(8, 16))
        ttk.Label(analysis_frame, text="sec").grid(row=0, column=2, sticky="w")

        ttk.Label(analysis_frame, text="Hop size:").grid(row=0, column=3, sticky="w", padx=(24, 0))
        ttk.Spinbox(
            analysis_frame,
            from_=MIN_LOUD_SECTION_HOP_SECONDS,
            to=MAX_LOUD_SECTION_HOP_SECONDS,
            increment=5.0,
            textvariable=self.var_hop,
            width=7,
        ).grid(row=0, column=4, sticky="w", padx=(8, 16))
        ttk.Label(analysis_frame, text="sec").grid(row=0, column=5, sticky="w")

        ttk.Label(analysis_frame, text="Worker threads:").grid(row=0, column=6, sticky="w", padx=(24, 0))
        ttk.Spinbox(
            analysis_frame,
            from_=MIN_WORKER_THREADS,
            to=MAX_WORKER_THREADS,
            textvariable=self.var_workers,
            width=5,
        ).grid(row=0, column=7, sticky="w", padx=(8, 0))

        threshold_frame = ttk.LabelFrame(self, text="Processing thresholds (minimum gain change)", padding=10)
        threshold_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=4)

        self.var_lossless_threshold = tk.DoubleVar(
            value=self._setting_float(settings, "lossless_threshold", DEFAULT_LOSSLESS_MIN_ABS_GAIN_DB)
        )
        self.var_mp3_threshold = tk.DoubleVar(
            value=self._setting_float(settings, "mp3_threshold", DEFAULT_MP3_MIN_ABS_GAIN_DB)
        )

        ttk.Label(threshold_frame, text="Lossless (FLAC/WAV):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            threshold_frame,
            from_=0.0,
            to=6.0,
            increment=0.05,
            textvariable=self.var_lossless_threshold,
            width=7,
        ).grid(row=0, column=1, sticky="w", padx=(8, 4))
        ttk.Label(threshold_frame, text="dB").grid(row=0, column=2, sticky="w")

        ttk.Label(threshold_frame, text="MP3:").grid(row=0, column=3, sticky="w", padx=(20, 0))
        ttk.Spinbox(
            threshold_frame,
            from_=0.0,
            to=6.0,
            increment=0.05,
            textvariable=self.var_mp3_threshold,
            width=7,
        ).grid(row=0, column=4, sticky="w", padx=(8, 4))
        ttk.Label(threshold_frame, text="dB (higher threshold = fewer re-encodes)").grid(row=0, column=5, sticky="w")

        bass_frame = ttk.LabelFrame(self, text="Bass compensation", padding=10)
        bass_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=4)

        self.var_bass_base_ratio = tk.DoubleVar(
            value=self._setting_float(settings, "bass_base_ratio", DEFAULT_BASS_BASE_RATIO)
        )
        self.var_bass_nod_sensitivity = tk.DoubleVar(
            value=self._setting_float(settings, "bass_nod_sensitivity", DEFAULT_BASS_NOD_SENSITIVITY)
        )

        ttk.Label(bass_frame, text="Bass offset (dB):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            bass_frame,
            from_=0.0,
            to=12.0,
            increment=0.5,
            textvariable=self.var_bass_base_ratio,
            width=7,
        ).grid(row=0, column=1, sticky="w", padx=(8, 4))
        ttk.Label(bass_frame, text="(expected bass - LUFS)").grid(row=0, column=2, sticky="w", padx=(8,0))

        ttk.Label(bass_frame, text="Bass sensitivity:").grid(row=1, column=0, sticky="w", pady=(6,0))
        ttk.Spinbox(
            bass_frame,
            from_=0.0,
            to=1.0,
            increment=0.05,
            textvariable=self.var_bass_nod_sensitivity,
            width=7,
        ).grid(row=1, column=1, sticky="w", padx=(8, 4), pady=(6,0))
        ttk.Label(bass_frame, text="(0.0 = disabled, 1.0 = full)").grid(row=1, column=2, sticky="w", padx=(8,0))

        target_frame = ttk.LabelFrame(self, text="Drop loudness target", padding=10)
        target_frame.grid(row=4, column=0, sticky="ew", padx=12, pady=4)

        self.var_target_low = tk.DoubleVar(
            value=self._setting_float(settings, "target_low", DEFAULT_TARGET_LOW_LUFS)
        )
        self.var_target_high = tk.DoubleVar(
            value=self._setting_float(settings, "target_high", DEFAULT_TARGET_HIGH_LUFS)
        )
        self.var_max_boost = tk.DoubleVar(
            value=self._setting_float(settings, "max_boost", DEFAULT_MAX_BOOST_DB)
        )
        self.var_peak_ceiling = tk.DoubleVar(
            value=self._setting_float(settings, "peak_ceiling", DEFAULT_BOOST_PEAK_CEILING_DBFS)
        )
        self.var_preserve_mtime = tk.BooleanVar(
            value=self._setting_bool(settings, "preserve_mtime", False)
        )

        ttk.Label(target_frame, text="Target low:").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            target_frame,
            from_=-20.0,
            to=0.0,
            increment=0.1,
            textvariable=self.var_target_low,
            width=7,
        ).grid(row=0, column=1, sticky="w", padx=(8, 4))
        ttk.Label(target_frame, text="LUFS").grid(row=0, column=2, sticky="w")

        ttk.Label(target_frame, text="Target high:").grid(row=0, column=3, sticky="w", padx=(20, 0))
        ttk.Spinbox(
            target_frame,
            from_=-20.0,
            to=0.0,
            increment=0.1,
            textvariable=self.var_target_high,
            width=7,
        ).grid(row=0, column=4, sticky="w", padx=(8, 4))
        ttk.Label(target_frame, text="LUFS").grid(row=0, column=5, sticky="w")

        ttk.Label(target_frame, text="Max boost:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(
            target_frame,
            from_=0.0,
            to=12.0,
            increment=0.1,
            textvariable=self.var_max_boost,
            width=7,
        ).grid(row=1, column=1, sticky="w", padx=(8, 4), pady=(8, 0))
        ttk.Label(target_frame, text="dB").grid(row=1, column=2, sticky="w", pady=(8, 0))

        ttk.Label(target_frame, text="Peak ceiling:").grid(
            row=1,
            column=3,
            sticky="w",
            padx=(20, 0),
            pady=(8, 0),
        )
        ttk.Spinbox(
            target_frame,
            from_=-12.0,
            to=0.0,
            increment=0.1,
            textvariable=self.var_peak_ceiling,
            width=7,
        ).grid(row=1, column=4, sticky="w", padx=(8, 4), pady=(8, 0))
        ttk.Label(target_frame, text="dBFS").grid(row=1, column=5, sticky="w", pady=(8, 0))

        ttk.Checkbutton(
            target_frame,
            text="Preserve original file modification time",
            variable=self.var_preserve_mtime,
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(8, 0))

        ttk.Label(
            target_frame,
            text=(
                f"Defaults: target {DEFAULT_TARGET_LOW_LUFS:.1f} to {DEFAULT_TARGET_HIGH_LUFS:.1f} LUFS. "
                f"Peak ceiling caps gain to avoid clipping; dynamics preserved (no limiting)."
            ),
            foreground="gray",
        ).grid(row=3, column=0, columnspan=6, sticky="w", pady=(10, 0))

        buttons_frame = ttk.Frame(self, padding=(12, 4))
        buttons_frame.grid(row=5, column=0, sticky="ew")

        self.btn_start = ttk.Button(
            buttons_frame,
            text="Analyze and Process",
            command=self._start_processing,
        )
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_analyze_only = ttk.Button(
            buttons_frame,
            text="Analysis Only",
            command=self._start_analyze_only,
        )
        self.btn_analyze_only.pack(side="left", padx=(0, 8))

        self.btn_cancel = ttk.Button(
            buttons_frame,
            text="Cancel",
            command=self._cancel,
            state="disabled",
        )
        self.btn_cancel.pack(side="left")

        progress_frame = ttk.Frame(self, padding=(12, 0))
        progress_frame.grid(row=6, column=0, sticky="ew")
        progress_frame.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew", pady=(4, 2))

        self.var_status = tk.StringVar(value="Ready. Source files are never modified.")
        ttk.Label(progress_frame, textvariable=self.var_status, foreground="gray").grid(
            row=1,
            column=0,
            sticky="w",
        )

        self.var_run_summary = tk.StringVar(value=self._format_run_counts(self._run_counts))
        ttk.Label(progress_frame, textvariable=self.var_run_summary).grid(
            row=2,
            column=0,
            sticky="w",
            pady=(2, 0),
        )

        output_frame = ttk.LabelFrame(self, text="Output", padding=6)
        output_frame.grid(row=7, column=0, sticky="nsew", padx=12, pady=(4, 12))
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)

        self.log = scrolledtext.ScrolledText(
            output_frame,
            state="disabled",
            wrap="none",
            font=("Consolas", 9),
            relief="flat",
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        self.log.tag_config("error", foreground="#c0392b")
        self.log.tag_config("good", foreground="#1e8449")
        self.log.tag_config("warn", foreground="#b9770e")

        self._update_output_controls()

    # -------------------------------------------------------------------------
    # UI Helpers
    # -------------------------------------------------------------------------

    def _update_output_controls(self) -> None:
        custom = self.var_output_mode.get() == OUTPUT_MODE_CUSTOM_FOLDER

        if custom:
            self.output_folder_entry.config(state="normal")
            self.btn_output_folder.config(state="normal")
            self.var_output_note.set(
                f"Copies use suffix {PROCESSED_SUFFIX}; subfolders are preserved inside the custom folder."
            )
        else:
            self.output_folder_entry.config(state="disabled")
            self.btn_output_folder.config(state="disabled")
            self.var_output_note.set(f"Copies are saved next to originals, with suffix {PROCESSED_SUFFIX}.")

    def _output_mode_changed(self, _event: object | None = None) -> None:
        self._update_output_controls()
        self._save_settings()

    def _pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select music folder")
        if not folder:
            return

        self.var_folder.set(folder)
        self._save_settings()

    def _pick_output_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select output folder for corrected copies")
        if not folder:
            return

        self.var_output_mode.set(OUTPUT_MODE_CUSTOM_FOLDER)
        self.var_output_folder.set(folder)
        self._update_output_controls()
        self._save_settings()

    def _set_busy_state(self) -> None:
        self.btn_start.config(state="disabled")
        self.btn_analyze_only.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.output_mode_combo.config(state="disabled")
        self.output_folder_entry.config(state="disabled")
        self.btn_output_folder.config(state="disabled")

    def _set_idle_state(self) -> None:
        self.btn_start.config(state="normal")
        self.btn_analyze_only.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self._update_output_controls()

    # -------------------------------------------------------------------------
    # Run Control
    # -------------------------------------------------------------------------

    def _start_processing(self) -> None:
        self._start(analyze_only=False)

    def _start_analyze_only(self) -> None:
        self._start(analyze_only=True)

    def _start(self, analyze_only: bool) -> None:
        folder = self.var_folder.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Folder missing", "Please choose a valid music folder.")
            return

        output_root: str | None = None
        output_mode = self.var_output_mode.get().strip()

        if output_mode == OUTPUT_MODE_CUSTOM_FOLDER:
            selected_output = self.var_output_folder.get().strip()
            if not selected_output:
                messagebox.showerror("Output folder missing", "Please choose an output folder, or use next to originals.")
                return
            if not os.path.isdir(selected_output):
                messagebox.showerror("Output folder missing", "Please choose an existing output folder.")
                return
            output_root = selected_output
        else:
            output_mode = OUTPUT_MODE_NEXT_TO_ORIGINALS
            self.var_output_mode.set(output_mode)

        target_low = float(self.var_target_low.get())
        target_high = float(self.var_target_high.get())

        if target_low >= target_high:
            messagebox.showerror(
                "Invalid target",
                "Target low must be lower than target high.\n\n"
                "For example: low -7.0, high -6.0.",
            )
            return

        peak_ceiling = float(self.var_peak_ceiling.get())
        if target_high > peak_ceiling + 6.0:
            if not messagebox.askyesno(
                "Target/Peak mismatch",
                f"Your target high LUFS ({target_high:.1f}) is more than 6 dB above the peak ceiling ({peak_ceiling:.1f} dBFS).\n"
                "This may cause many tracks to be peak‑limited (gain will be capped).\n\nDo you want to continue?",
                icon="warning",
            ):
                return

        window_seconds = max(1.0, float(self.var_window.get()))
        hop_seconds = max(1.0, float(self.var_hop.get()))

        if hop_seconds > window_seconds:
            hop_seconds = window_seconds
            self.var_hop.set(hop_seconds)

        workers = max(MIN_WORKER_THREADS, min(MAX_WORKER_THREADS, int(self.var_workers.get())))
        self.var_workers.set(workers)

        max_boost = max(0.0, float(self.var_max_boost.get()))

        bass_base_ratio = float(self.var_bass_base_ratio.get())
        bass_nod_sensitivity = max(0.0, min(1.0, float(self.var_bass_nod_sensitivity.get())))

        preserve_mtime = self.var_preserve_mtime.get()

        lossless_threshold = float(self.var_lossless_threshold.get())
        mp3_threshold = float(self.var_mp3_threshold.get())

        self._save_settings()
        self._cancel_flag.clear()
        self._clear_log()
        self._reset_run_counts()
        self._set_busy_state()
        self.progress["value"] = 0
        self.var_status.set("Starting analyze-only run..." if analyze_only else "Starting...")

        mode_text = "analyze only" if analyze_only else "analyze and create corrected copies"
        output_text = (
            "next to originals"
            if output_root is None
            else f"custom folder, preserving subfolders: {output_root}"
        )

        self._logger.info(APP_TITLE)
        self._logger.info("-" * 78)
        self._logger.info("Mode:                 %s", mode_text)
        self._logger.info("Folder:               %s", folder)
        self._logger.info("Output location:      %s", output_text)
        self._logger.info("Processed suffix:     %s", PROCESSED_SUFFIX)
        self._logger.info("Formats:              %s", ", ".join(sorted(SUPPORTED_EXTENSIONS)))
        self._logger.info("MP3 output:           %s CBR", MP3_OUTPUT_BITRATE)
        self._logger.info("Lossless threshold:   %.2f dB", lossless_threshold)
        self._logger.info("MP3 threshold:        %.2f dB", mp3_threshold)
        self._logger.info("Overwrite existing:   %s", PROCESS_OVERWRITE_EXISTING)
        self._logger.info("Lossless verify:      %s", STRICT_VERIFY_LOSSLESS_OUTPUT)
        self._logger.info("Metadata copy:        Mutagen after render")
        self._logger.info("Post-render verify:   %s", POST_VERIFY_PROCESSED_AUDIO)
        self._logger.info("Preserve mtime:       %s", preserve_mtime)
        self._logger.info("Meter sample rate:    %s Hz", METER_SAMPLE_RATE)
        self._logger.info("Analysis window:      %.1f sec", window_seconds)
        self._logger.info("Hop size:             %.1f sec", hop_seconds)
        self._logger.info("Target window:        %.1f to %.1f LUFS", target_low, target_high)
        self._logger.info("Max positive boost:   %.1f dB", max_boost)
        self._logger.info("Peak ceiling:         %.1f dBFS", peak_ceiling)
        self._logger.info("Bass offset:          %.1f dB", bass_base_ratio)
        self._logger.info("Bass sensitivity:     %.2f", bass_nod_sensitivity)
        self._logger.info("Processing:           Pure gain via FFmpeg (preserves dynamics; no limiting or clipping)")
        self._logger.info("Threads:              %s", workers)
        self._logger.info("-" * 78)
        self._logger.info("")

        if analyze_only:
            self._logger.info("Analyze-only mode: no corrected copies will be created.")
            self._logger.info("A summary of what would be done will appear at the end.")
            self._logger.info("")
        else:
            self._logger.warning("MP3 files are re-encoded only when gain exceeds the MP3 threshold.")
            self._logger.info("")

        self._worker_thread = threading.Thread(
            target=self._run_safely,
            args=(
                folder,
                target_low,
                target_high,
                window_seconds,
                hop_seconds,
                max_boost,
                peak_ceiling,
                bass_base_ratio,
                bass_nod_sensitivity,
                workers,
                analyze_only,
                output_root,
                preserve_mtime,
                lossless_threshold,
                mp3_threshold,
            ),
            daemon=True,
        )
        self._worker_thread.start()

    def _cancel(self) -> None:
        self._cancel_flag.set()
        self.var_status.set("Cancelling after current file(s) finish...")

    # -------------------------------------------------------------------------
    # Background Worker
    # -------------------------------------------------------------------------

    def _run_safely(
        self,
        folder: str,
        target_low: float,
        target_high: float,
        window_seconds: float,
        hop_seconds: float,
        max_boost: float,
        peak_ceiling: float,
        bass_base_ratio: float,
        bass_nod_sensitivity: float,
        workers: int,
        analyze_only: bool,
        output_root: str | None,
        preserve_mtime: bool,
        lossless_threshold: float,
        mp3_threshold: float,
    ) -> None:
        try:
            self._run(
                folder=folder,
                target_low=target_low,
                target_high=target_high,
                window_seconds=window_seconds,
                hop_seconds=hop_seconds,
                max_boost=max_boost,
                peak_ceiling=peak_ceiling,
                bass_base_ratio=bass_base_ratio,
                bass_nod_sensitivity=bass_nod_sensitivity,
                workers=workers,
                analyze_only=analyze_only,
                output_root=output_root,
                preserve_mtime=preserve_mtime,
                lossless_threshold=lossless_threshold,
                mp3_threshold=mp3_threshold,
            )
        except Exception as exc:
            self._logger.exception("Fatal error: %s", exc)
            self._queue.put(("fatal", None))

    def _run(
        self,
        folder: str,
        target_low: float,
        target_high: float,
        window_seconds: float,
        hop_seconds: float,
        max_boost: float,
        peak_ceiling: float,
        bass_base_ratio: float,
        bass_nod_sensitivity: float,
        workers: int,
        analyze_only: bool,
        output_root: str | None,
        preserve_mtime: bool,
        lossless_threshold: float,
        mp3_threshold: float,
    ) -> None:
        q = self._queue
        started_at = time.time()

        q.put(("status", "Checking ffmpeg and ffprobe..."))
        check_ffmpeg_available()

        q.put(("status", "Finding audio files..."))
        files = find_audio_files(folder)
        total = len(files)

        self._logger.info("Found %s supported original audio files.", total)
        q.put(("progress_max", total))
        q.put(("summary_counts", self._empty_run_counts()))

        if total == 0:
            self._logger.info("Nothing to do.")
            q.put(("finished", None))
            return

        completed = 0
        errors = 0
        all_rows: list[TrackRow] = []
        run_counts = self._empty_run_counts()
        write_lock = threading.Lock()

        def count_completed_row(row: TrackRow) -> None:
            status = row["processing_status"]
            audio_v = row["audio_verification"]
            meta_v = row["metadata_verification"]

            if status == "processed":
                run_counts["processed"] += 1
            elif status == "processed_warning":
                run_counts["processed"] += 1
                run_counts["warnings"] += 1
            elif status == "analyzed_would_process":
                run_counts["analyzed_only"] += 1
                run_counts["would_process"] += 1
            elif status.startswith("analyzed_"):
                run_counts["analyzed_only"] += 1
                run_counts["skipped"] += 1
            else:
                run_counts["skipped"] += 1

            if status != "processed_warning" and (audio_v == "warning" or meta_v == "warning"):
                run_counts["warnings"] += 1

        def tick() -> tuple[int, int, float, str, int, dict[str, int]]:
            elapsed = max(time.time() - started_at, 0.001)
            rate = completed / elapsed
            remaining = max(total - completed, 0)
            eta_seconds = remaining / rate if rate > 0 else 0.0
            eta = f"{int(eta_seconds // 60)}m {int(eta_seconds % 60)}s"
            snapshot = dict(run_counts)
            snapshot["errors"] = errors
            return completed, total, rate, eta, errors, snapshot

        def process_one(path: str) -> None:
            nonlocal completed, errors

            if self._cancel_flag.is_set():
                return

            row: TrackRow | None = None

            try:
                row = analyze_file(
                    path=path,
                    target_low=target_low,
                    target_high=target_high,
                    loud_window_seconds=window_seconds,
                    loud_hop_seconds=hop_seconds,
                    max_boost_db=max_boost,
                    peak_ceiling_dbfs=peak_ceiling,
                    bass_base_ratio=bass_base_ratio,
                    bass_nod_sensitivity=bass_nod_sensitivity,
                    output_root=output_root,
                    source_root=folder,
                )

                should_process, status = should_process_row(row, lossless_threshold, mp3_threshold)

                if analyze_only:
                    if should_process:
                        row["processing_status"] = "analyzed_would_process"
                    else:
                        row["processing_status"] = f"analyzed_{status}"

                    row["processing_error"] = ""
                    row["audio_verification"] = "not_applicable"
                    row["metadata_verification"] = "not_applicable"
                    row["notes"] = append_note(row["notes"], "analyze-only mode; no output created")

                    ext = row["extension"].lower()
                    if status in {"lossless_gain_below_threshold", "mp3_gain_below_threshold"}:
                        row["notes"] = append_note(
                            row["notes"],
                            f"gain change below processing threshold for {ext}",
                        )

                elif should_process:
                    gain = parse_float_or_default(row["suggested_gain_db"], 0.0)
                    output_path = row["output_path"]
                    source_info = ffprobe_audio_info(path)

                    output_info = process_audio_with_gain(
                        input_path=path,
                        output_path=output_path,
                        gain_db=gain,
                        source_info=source_info,
                        preserve_mtime=preserve_mtime,
                    )

                    row["output_sample_rate"] = output_info.get("sample_rate", "")
                    row["output_channels"] = output_info.get("channels", "")
                    row["output_audio_codec"] = output_info.get("codec_name", "")
                    row["output_audio_sample_fmt"] = output_info.get("sample_fmt", "")
                    row["output_bit_depth"] = infer_bit_depth(output_info, row["extension"])
                    row["output_bit_rate"] = output_info.get("bit_rate", "")
                    row["output_file_size_mb"] = round_or_blank(os.path.getsize(output_path) / 1_048_576, 2)

                    metadata_status, metadata_message = verify_metadata(path, output_path)
                    row["metadata_verification"] = metadata_status

                    if metadata_message:
                        row["notes"] = append_note(row["notes"], metadata_message)

                    audio_status, audio_message = verify_processed_audio_fast(row, output_path)
                    row["audio_verification"] = audio_status

                    if audio_message:
                        row["notes"] = append_note(row["notes"], audio_message)

                    if audio_status == "warning" or metadata_status == "warning":
                        row["processing_status"] = "processed_warning"
                    else:
                        row["processing_status"] = "processed"

                    row["processing_error"] = ""

                else:
                    row["processing_status"] = status
                    row["processing_error"] = ""
                    row["audio_verification"] = "not_applicable"
                    row["metadata_verification"] = "not_applicable"

                    ext = row["extension"].lower()

                    if status in {"lossless_gain_below_threshold", "mp3_gain_below_threshold"}:
                        row["notes"] = append_note(
                            row["notes"],
                            f"gain change below processing threshold for {ext}",
                        )

                with write_lock:
                    all_rows.append(row)
                    completed += 1
                    count_completed_row(row)

                    action = row["action"]
                    processing_status = row["processing_status"]
                    audio_v = row["audio_verification"]
                    meta_v = row["metadata_verification"]
                    filename = row["filename"]
                    loudest = row["loudest_section_lufs"]
                    gain = row["suggested_gain_db"]
                    notes = row["notes"]

                    log_line = (
                        f"{completed:>4}/{total:<4} "
                        f"[{action}] {filename} -> "
                        f"Drop: {loudest} LUFS | "
                        f"Gain: {gain} dB"
                    )
                    
                    if processing_status not in ("processed", "analyzed_would_process"):
                        log_line += f" | Status: {processing_status}"
                        
                    if notes:
                        log_line += f" [{notes}]"

                    self._logger.info(log_line)
                    q.put(("tick", tick()))

            except Exception as exc:
                with write_lock:
                    errors += 1
                    completed += 1

                    if row is not None:
                        row["processing_status"] = "error"
                        row["processing_error"] = str(exc)
                        all_rows.append(row)

                    self._logger.error(
                        f"{completed:>5}/{total:<5} ERROR  "
                        f"{os.path.basename(path)} - {exc}"
                    )
                    q.put(("tick", tick()))

        pool = ThreadPoolExecutor(max_workers=workers)
        futures = [pool.submit(process_one, path) for path in files]

        try:
            for _future in as_completed(futures):
                if self._cancel_flag.is_set():
                    break
        finally:
            if self._cancel_flag.is_set():
                for future in futures:
                    future.cancel()
                pool.shutdown(wait=True, cancel_futures=True)
            else:
                pool.shutdown(wait=True)

        elapsed = time.time() - started_at

        if self._cancel_flag.is_set():
            self._logger.warning("Cancelled. Completed files are saved.")
            q.put(("cancelled", None))
            return

        summary = build_summary(all_rows)
        self._logger.info("")
        self._logger.info("%s", summary)

        if analyze_only:
            would_raise = sum(1 for r in all_rows if r.get("action") == "Raise")
            would_lower = sum(1 for r in all_rows if r.get("action") == "Lower")
            would_keep = sum(1 for r in all_rows if r.get("action") == "Keep")
            gain_values = [parse_float_or_default(r.get("suggested_gain_db"), 0.0) for r in all_rows if r.get("action") in ("Raise", "Lower")]
            if gain_values:
                gain_stats = f"Min: {min(gain_values):+.2f} dB, Max: {max(gain_values):+.2f} dB, Mean: {sum(gain_values)/len(gain_values):+.2f} dB"
            else:
                gain_stats = "No gain changes needed."
            messagebox.showinfo(
                "Analysis Summary (Dry Run)",
                f"Total tracks analyzed: {total}\n\n"
                f"Would raise: {would_raise}\n"
                f"Would lower: {would_lower}\n"
                f"Would keep:   {would_keep}\n\n"
                f"Gain statistics (raise/lower only):\n{gain_stats}\n\n"
                f"See the output log for per‑file details."
            )

        q.put(("finished", None))

    # -------------------------------------------------------------------------
    # Queue Polling
    # -------------------------------------------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, data = self._queue.get_nowait()

                if kind == "log":
                    self._log(str(data))
                elif kind == "log_error":
                    self._log(str(data), "error")
                elif kind == "log_message":
                    text, tag = data  # type: ignore[misc]
                    self._log(str(text), tag)
                elif kind == "status":
                    self.var_status.set(str(data))
                elif kind == "progress_max":
                    maximum = max(int(data), 1)
                    self.progress["maximum"] = maximum
                    self.progress["value"] = 0
                elif kind == "summary_counts":
                    self._run_counts = dict(data)  # type: ignore[arg-type]
                    self.var_run_summary.set(self._format_run_counts(self._run_counts))
                elif kind == "tick":
                    done, total, rate, eta, errors, counts = data  # type: ignore[misc]
                    self.progress["value"] = done
                    self.var_status.set(
                        f"{done} / {total} files | "
                        f"{rate:.2f} files/s | ETA {eta} | {errors} errors"
                    )
                    self._run_counts = dict(counts)
                    self.var_run_summary.set(self._format_run_counts(self._run_counts))
                elif kind == "finished":
                    self._set_idle_state()
                    self.var_status.set("Done.")
                elif kind == "cancelled":
                    self._set_idle_state()
                    self.var_status.set("Cancelled.")
                elif kind == "fatal":
                    self._set_idle_state()
                    self.var_status.set("Error. See output above.")

        except queue.Empty:
            pass

        self.after(100, self._poll_queue)

    def _log(self, text: str, tag: str | None = None) -> None:
        self.log.config(state="normal")
        if tag:
            self.log.insert("end", text, tag)
        else:
            self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def _clear_log(self) -> None:
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _close_app(self) -> None:
        self._shutdown_logging()
        self.destroy()
