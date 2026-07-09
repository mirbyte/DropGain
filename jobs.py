"""
DropGain headless backend job runner.

This module encapsulates all execution logic for scanning audio libraries,
running multithreaded analysis, applying gains/limiters, writing reports,
and tracking progress in a completely UI-agnostic manner.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from analysis import (
    APP_TITLE,
    CSV_FIELDNAMES,
    DEFAULT_APPLY_RENDER_GAIN_THRESHOLD,
    DEFAULT_BASS_PENALTY_FULL_DB,
    DEFAULT_BASS_PENALTY_START_DB,
    DEFAULT_LIMITER_ENGINE,
    DEFAULT_OUTPUT_FORMAT_MODE,
    DEFAULT_SUB_PENALTY_FULL_DB,
    DEFAULT_SUB_PENALTY_START_DB,
    PROCESSED_SUFFIX,
    TrackRow,
    apply_track_decision,
    benchmark_timer,
    build_summary,
    check_ffmpeg_available,
    decision_from_row,
    ffprobe_audio_info,
    find_audio_files,
    is_limiter_processing_engine,
    make_error_track_row,
    processed_output_path,
    format_peak_control_display,
    parse_float_or_default,
    row_to_csv_dict,
    sort_track_rows_by_path,
)
from processing import (
    process_track,
    render_analyzed_row,
    row_should_use_limiter,
    should_process_row,
)

CSV_FLUSH_INTERVAL_ROWS = 25
_PROGRESS_ETA_MIN_INTERVAL_SEC = 0.35
_PROGRESS_ETA_ALPHA = 0.3
_PROGRESS_ETA_CUMULATIVE_BLEND = 0.9


class JobProgressTicker:
    """Track per-completion interval and derive an ETA for long-running jobs.

    One tick corresponds to one completed (or errored) track, so the interval
    between ticks already reflects the effective worker concurrency. The ETA is
    therefore ``remaining_tracks * seconds_per_completion``.
    """

    __slots__ = ("started_at", "total", "_last_tick_at", "_ema_spc")

    def __init__(self, started_at: float, total: int) -> None:
        self.started_at = started_at
        self.total = total
        self._last_tick_at: float | None = None
        self._ema_spc: float | None = None

    def snapshot(
        self,
        completed: int,
        errors: int,
        run_counts: dict[str, int],
    ) -> tuple[int, int, float, str, int, dict[str, int]]:
        now = time.time()
        elapsed = max(now - self.started_at, 0.001)
        rate = completed / elapsed
        remaining = max(self.total - completed, 0)

        if completed > 0:
            cumulative_spc = elapsed / completed
            if self._last_tick_at is not None:
                # Wall-clock interval between the last two completions. With
                # concurrency this is smaller than the processing time of one
                # track, and it is exactly the quantity we need for ETA.
                dt = max(now - self._last_tick_at, _PROGRESS_ETA_MIN_INTERVAL_SEC)
                if self._ema_spc is None:
                    self._ema_spc = cumulative_spc
                else:
                    self._ema_spc = (
                        _PROGRESS_ETA_ALPHA * dt
                        + (1.0 - _PROGRESS_ETA_ALPHA) * self._ema_spc
                    )
            else:
                self._ema_spc = cumulative_spc
            self._last_tick_at = now
            spc = max(self._ema_spc, cumulative_spc * _PROGRESS_ETA_CUMULATIVE_BLEND)
            eta_seconds = remaining * spc
        else:
            eta_seconds = 0.0

        eta = f"{int(eta_seconds // 60)}m {int(eta_seconds % 60)}s"
        snapshot = dict(run_counts)
        snapshot["errors"] = errors
        return completed, self.total, rate, eta, errors, snapshot


@dataclass
class AnalyzedWorkItem:
    """Cached analysis metadata for a source file, reused during render."""
    source_info: dict[str, object]
    source_mtime_ns: int
    source_size: int


@dataclass
class DropGainSettings:
    """Configuration settings for a DropGain run."""
    folder: str
    csv_path: str
    target_low_lufs: float
    target_high_lufs: float
    window_seconds: float
    hop_seconds: float
    max_reduction_db: float
    bass_max_reduction_db: float
    peak_ceiling_dbfs: float
    normalization_mode: str
    analysis_workers: int
    render_workers: int
    analyze_only: bool
    write_csv: bool
    mp3_threshold: float
    lossless_threshold: float
    output_format_mode: str = DEFAULT_OUTPUT_FORMAT_MODE
    limiter_engine: str = DEFAULT_LIMITER_ENGINE
    allow_risky_true_peak_boost: bool = False
    apply_render_gain_threshold: bool = DEFAULT_APPLY_RENDER_GAIN_THRESHOLD
    output_root: str | None = None
    bass_penalty_start_db: float = DEFAULT_BASS_PENALTY_START_DB
    bass_penalty_full_db: float = DEFAULT_BASS_PENALTY_FULL_DB
    sub_penalty_start_db: float = DEFAULT_SUB_PENALTY_START_DB
    sub_penalty_full_db: float = DEFAULT_SUB_PENALTY_FULL_DB


class CsvBatchWriter:
    """Write CSV rows with batched flush for large libraries."""

    def __init__(self, path: str, fieldnames: tuple[str, ...]) -> None:
        self._handle = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._handle, fieldnames=fieldnames)
        self._writer.writeheader()
        self._rows_since_flush = 0
        self.flush()

    def write_row(self, row: dict[str, object], *, force_flush: bool = False) -> None:
        with benchmark_timer("CSV write"):
            self._writer.writerow(row)
            self._rows_since_flush += 1
            if force_flush or self._rows_since_flush >= CSV_FLUSH_INTERVAL_ROWS:
                self.flush()

    def flush(self) -> None:
        self._handle.flush()
        self._rows_since_flush = 0

    def close(self) -> None:
        self.flush()
        self._handle.close()


def _write_csv_row(
    csv_batch: CsvBatchWriter | None,
    row: TrackRow,
    logger: logging.Logger,
    *,
    force_flush: bool = False,
) -> None:
    if csv_batch is None:
        return
    try:
        csv_batch.write_row(
            row_to_csv_dict(row),
            force_flush=force_flush,
        )
    except Exception as exc:
        path = str(row.get("path", ""))
        logger.warning("CSV write failed for %s: %s", path or "<unknown>", exc)


def _row_needs_immediate_csv_flush(row: TrackRow) -> bool:
    status = str(row.get("processing_status", ""))
    if status in {"error", "processed_warning"}:
        return True
    if str(row.get("warnings", "")).strip():
        return True
    if str(row.get("processing_error", "")).strip():
        return True
    if row.get("audio_verification") == "warning" or row.get("metadata_verification") == "warning":
        return True
    return False


def _source_file_matches_work_item(path: str, work_item: AnalyzedWorkItem) -> bool:
    try:
        stat = os.stat(path)
    except OSError:
        return False
    return stat.st_mtime_ns == work_item.source_mtime_ns and stat.st_size == work_item.source_size


def _resolve_render_source_info(path: str, work_items: dict[str, AnalyzedWorkItem]) -> dict[str, object]:
    """Return cached source metadata if the file still matches the analysis pass."""
    work_item = work_items.get(path)
    if work_item is None:
        return ffprobe_audio_info(path)
    if not _source_file_matches_work_item(path, work_item):
        raise RuntimeError("source file changed since analysis; re-analyze before rendering")
    return work_item.source_info


def _should_render_row(settings: DropGainSettings, row: TrackRow) -> tuple[bool, str]:
    """Apply render-time decision rules using current settings thresholds."""
    return should_process_row(
        row,
        mp3_threshold=settings.mp3_threshold,
        lossless_threshold=settings.lossless_threshold,
        allow_risky_true_peak_boost=settings.allow_risky_true_peak_boost,
        apply_gain_threshold=settings.apply_render_gain_threshold,
    )


def refresh_analyzed_render_statuses(
    settings: DropGainSettings,
    rows: list[TrackRow],
) -> int:
    """Re-evaluate analyzed row statuses from current render decision settings.

    Returns the number of rows that would process under the refreshed statuses.
    """
    would_process = 0
    for row in rows:
        status = str(row.get("processing_status", ""))
        if not status.startswith("analyzed_"):
            continue

        should_render, reason = _should_render_row(settings, row)
        if should_render:
            row["processing_status"] = "analyzed_would_process"
            would_process += 1
        else:
            row["processing_status"] = f"analyzed_{reason}"
        row["processing_error"] = ""
        row["audio_verification"] = "not_applicable"
        row["metadata_verification"] = "not_applicable"
    return would_process


def recompute_row_decision(
    settings: DropGainSettings,
    row: TrackRow,
    *,
    apply_gain_threshold: bool = True,
) -> None:
    """Recompute one row's gain/decision fields from cached measurements."""
    decision = decision_from_row(
        row,
        target_low=settings.target_low_lufs,
        target_high=settings.target_high_lufs,
        max_reduction_db=settings.max_reduction_db,
        peak_ceiling_dbfs=settings.peak_ceiling_dbfs,
        normalization_mode=settings.normalization_mode,
        bass_max_reduction_db=settings.bass_max_reduction_db,
        bass_penalty_start_db=settings.bass_penalty_start_db,
        bass_penalty_full_db=settings.bass_penalty_full_db,
        sub_penalty_start_db=settings.sub_penalty_start_db,
        sub_penalty_full_db=settings.sub_penalty_full_db,
        allow_risky_true_peak_boost=settings.allow_risky_true_peak_boost,
    )
    apply_track_decision(row, decision, limiter_engine=settings.limiter_engine)

    status = str(row.get("processing_status", ""))
    if status.startswith("analyzed_") or status == "":
        should_render, reason = should_process_row(
            row,
            mp3_threshold=settings.mp3_threshold,
            lossless_threshold=settings.lossless_threshold,
            allow_risky_true_peak_boost=settings.allow_risky_true_peak_boost,
            apply_gain_threshold=apply_gain_threshold,
        )
        if should_render:
            row["processing_status"] = "analyzed_would_process"
        else:
            row["processing_status"] = f"analyzed_{reason}"


def recompute_rows_decisions(
    settings: DropGainSettings,
    rows: list[TrackRow],
    *,
    apply_gain_threshold: bool = True,
) -> None:
    """Recompute gain/decision fields for every row from cached measurements."""
    for row in rows:
        recompute_row_decision(settings, row, apply_gain_threshold=apply_gain_threshold)


def recompute_rows_for_settings(
    settings: DropGainSettings,
    rows: list[TrackRow],
) -> None:
    """Refresh output paths/format for every row, then recompute decisions."""
    for row in rows:
        row["output_format_mode"] = settings.output_format_mode
        row["output_path"] = processed_output_path(
            row["path"],
            output_root=settings.output_root,
            source_root=settings.folder,
            output_format_mode=settings.output_format_mode,
        )
    recompute_rows_decisions(
        settings,
        rows,
        apply_gain_threshold=settings.apply_render_gain_threshold,
    )


def eligible_render_indices(
    settings: DropGainSettings,
    rows: list[TrackRow],
    *,
    refresh_stale_would_process: bool = False,
) -> frozenset[int]:
    """Return indices of analyzed rows eligible to render under current settings."""
    if refresh_stale_would_process:
        refresh_analyzed_render_statuses(settings, rows)

    indices: set[int] = set()
    for index, row in enumerate(rows):
        status = str(row.get("processing_status", ""))
        if status in {"error", "failed"} or "error" in status:
            continue
        should_render, _status = _should_render_row(settings, row)
        if should_render:
            indices.add(index)
    return frozenset(indices)


def run_analysis_job(
    settings: DropGainSettings,
    on_progress: Callable[[str, Any], None],
    cancel_flag: threading.Event,
    logger: logging.Logger,
    *,
    emit_summary: bool = True,
    emit_finished: bool = True,
    apply_gain_threshold: bool = True,
) -> tuple[list[TrackRow], dict[str, AnalyzedWorkItem]]:
    """Analyze all source files and return rows plus reusable source metadata.

    apply_gain_threshold controls analyze-only skip decisions. Batch mode passes
    the render setting here so the analysis table matches the later render pass.
    """
    started_at = time.time()
    source_folder_name = os.path.basename(os.path.normpath(settings.folder))
    empty_counts = {
        "processed": 0,
        "would_process": 0,
        "analyzed_only": 0,
        "skipped": 0,
        "warnings": 0,
        "errors": 0,
    }

    on_progress("status", "Checking ffmpeg and ffprobe...")
    check_ffmpeg_available()

    on_progress("status", "Finding audio files...")
    files = find_audio_files(settings.folder)
    total = len(files)

    logger.info("Found %s supported original audio files.", total)
    logger.info("Analysis workers: %s", max(1, int(settings.analysis_workers)))
    on_progress("phase", "analyze")
    on_progress("progress_max", total)
    on_progress("summary_counts", dict(empty_counts))

    if total == 0:
        logger.info("Nothing to do.")
        if emit_finished:
            on_progress("finished", settings.csv_path if (settings.write_csv and os.path.exists(settings.csv_path)) else None)
        return [], {}

    if settings.write_csv:
        output_dir = os.path.dirname(os.path.abspath(settings.csv_path))
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

    completed = 0
    errors = 0
    all_rows: list[TrackRow] = []
    work_items: dict[str, AnalyzedWorkItem] = {}
    run_counts = dict(empty_counts)
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

    progress = JobProgressTicker(started_at, total)

    def tick() -> tuple[int, int, float, str, int, dict[str, int]]:
        return progress.snapshot(completed, errors, run_counts)

    def process_one(path: str) -> None:
        nonlocal completed, errors

        if cancel_flag.is_set():
            return

        try:
            stat = os.stat(path)
            source_mtime_ns = stat.st_mtime_ns
            source_size = stat.st_size

            row, error_msg, used_source_info = process_track(
                path=path,
                target_low=settings.target_low_lufs,
                target_high=settings.target_high_lufs,
                window_seconds=settings.window_seconds,
                hop_seconds=settings.hop_seconds,
                max_reduction=settings.max_reduction_db,
                bass_max_reduction=settings.bass_max_reduction_db,
                bass_penalty_start_db=settings.bass_penalty_start_db,
                bass_penalty_full_db=settings.bass_penalty_full_db,
                sub_penalty_start_db=settings.sub_penalty_start_db,
                sub_penalty_full_db=settings.sub_penalty_full_db,
                peak_ceiling=settings.peak_ceiling_dbfs,
                normalization_mode=settings.normalization_mode,
                analyze_only=True,
                mp3_threshold=settings.mp3_threshold,
                lossless_threshold=settings.lossless_threshold,
                output_format_mode=settings.output_format_mode,
                allow_risky_true_peak_boost=settings.allow_risky_true_peak_boost,
                apply_gain_threshold=apply_gain_threshold,
                output_root=settings.output_root,
                source_root=settings.folder,
                source_folder_name=source_folder_name,
                limiter_engine=settings.limiter_engine,
            )

            if error_msg:
                with write_lock:
                    errors += 1
                    completed += 1
                    error_row = row if row is not None else make_error_track_row(
                        path,
                        error_msg,
                        source_root=settings.folder,
                        source_folder_name=source_folder_name,
                        output_format_mode=settings.output_format_mode,
                    )
                    if row is not None:
                        error_row["processing_status"] = "error"
                        error_row["processing_error"] = error_msg
                    all_rows.append(error_row)
                    _write_csv_row(
                        csv_batch,
                        error_row,
                        logger,
                        force_flush=True,
                    )

                    logger.error(
                        f"{completed:>5}/{total:<5} ERROR  "
                        f"{os.path.basename(path)} - {error_msg}"
                    )
                    on_progress("tick", tick())
                return

            with write_lock:
                if row is not None:
                    _write_csv_row(
                        csv_batch,
                        row,
                        logger,
                        force_flush=_row_needs_immediate_csv_flush(row),
                    )

                if row is not None:
                    all_rows.append(row)
                    if used_source_info is not None:
                        work_items[path] = AnalyzedWorkItem(
                            source_info=used_source_info,
                            source_mtime_ns=source_mtime_ns,
                            source_size=source_size,
                        )
                completed += 1
                if row is None:
                    errors += 1
                    error_row = make_error_track_row(
                        path,
                        "analysis returned no row",
                        source_root=settings.folder,
                        source_folder_name=source_folder_name,
                        output_format_mode=settings.output_format_mode,
                    )
                    all_rows.append(error_row)
                    _write_csv_row(
                        csv_batch,
                        error_row,
                        logger,
                        force_flush=True,
                    )
                    logger.error(
                        f"{completed:>5}/{total:<5} ERROR  "
                        f"{os.path.basename(path)} - analysis returned no row"
                    )
                    on_progress("tick", tick())
                    return

                count_completed_row(row)

                processing_status = row["processing_status"]
                filename = row["filename"]
                loudest = row["loudest_section_lufs"]
                true_peak = row["true_peak_dbtp"]
                gain = row["suggested_gain_db"]
                peak_control = row["estimated_peak_control_db"]
                processing_engine = str(row["processing_engine"] or "")

                def _fmt_float(value: object, digits: int = 2, signed: bool = False) -> str:
                    try:
                        number = float(value)
                    except Exception:
                        return ""
                    sign = "+" if signed else ""
                    return f"{number:{sign}.{digits}f}"

                result = "WOULD" if processing_status == "analyzed_would_process" else "SKIP"

                status_text = {
                    "analyzed_would_process": "Would process",
                    "analyzed_already_in_target_range": "Already in target",
                    "analyzed_mp3_gain_below_threshold": "Below threshold",
                    "analyzed_lossless_gain_below_threshold": "Below threshold",
                    "analyzed_zero_gain_mp3_render_skipped": "Zero-gain MP3 render skipped",
                    "analyzed_output_exists": "Output exists",
                    "analyzed_needs_manual_check": "Needs manual check",
                }.get(processing_status, processing_status.replace("_", " "))

                gain_text = f"{_fmt_float(gain, signed=True):>7} dB" if _fmt_float(gain, signed=True) else "     -- dB"
                loud_text = f"{_fmt_float(loudest):>6} LUFS"
                tp_text = f"TP {_fmt_float(true_peak):>6} dBTP"

                try:
                    peak_control_value = float(peak_control)
                except Exception:
                    peak_control_value = 0.0
                limiter_engine_selected = is_limiter_processing_engine(processing_engine)
                if peak_control_value <= 0.01:
                    peak_text = "TP safe"
                elif limiter_engine_selected:
                    peak_text = (
                        "would limit "
                        + format_peak_control_display(
                            peak_control,
                            processing_engine,
                        )
                    )
                else:
                    peak_text = f"TP over ceil {peak_control_value:.2f} dB"

                logger.info(
                    f"{completed:>4}/{total:<4} "
                    f"{result:<5} "
                    f"{gain_text:>10}  "
                    f"loud {loud_text:<20}  "
                    f"{tp_text:<24}  "
                    f"{peak_text:<30}  "
                    f"{status_text:<22}  "
                    f"{filename}"
                )
                on_progress("tick", tick())

        except Exception as exc:
            with write_lock:
                errors += 1
                completed += 1
                error_row = make_error_track_row(
                    path,
                    str(exc),
                    source_root=settings.folder,
                    source_folder_name=source_folder_name,
                    output_format_mode=settings.output_format_mode,
                )
                all_rows.append(error_row)
                _write_csv_row(
                    csv_batch,
                    error_row,
                    logger,
                    force_flush=True,
                )

                logger.error(
                    f"{completed:>5}/{total:<5} ERROR  "
                    f"{os.path.basename(path)} - {exc}"
                )
                on_progress("tick", tick())

    csv_batch: CsvBatchWriter | None = None
    if settings.write_csv:
        csv_batch = CsvBatchWriter(settings.csv_path, CSV_FIELDNAMES)

    try:
        pool = ThreadPoolExecutor(max_workers=max(1, int(settings.analysis_workers)))
        futures = [pool.submit(process_one, path) for path in files]

        try:
            for _future in as_completed(futures):
                if cancel_flag.is_set():
                    break
        finally:
            if cancel_flag.is_set():
                for future in futures:
                    future.cancel()
                pool.shutdown(wait=True, cancel_futures=True)
            else:
                pool.shutdown(wait=True)
    finally:
        if csv_batch is not None:
            csv_batch.close()

    elapsed = time.time() - started_at
    csv_output = settings.csv_path if settings.write_csv else None

    if cancel_flag.is_set():
        logger.warning("Cancelled. Analysis results remain saved.")
        on_progress("cancelled", csv_output)
        return sort_track_rows_by_path(all_rows), work_items

    if emit_summary:
        summary = build_summary(
            all_rows,
            errors,
            elapsed,
            mp3_threshold=settings.mp3_threshold,
            lossless_threshold=settings.lossless_threshold,
        )
        logger.info("")
        logger.info("%s", summary)

    if emit_finished:
        on_progress("finished", csv_output)

    return sort_track_rows_by_path(all_rows), work_items


def run_batch_job(
    settings: DropGainSettings,
    on_progress: Callable[[str, Any], None],
    cancel_flag: threading.Event,
    logger: logging.Logger,
) -> tuple[list[TrackRow], dict[str, AnalyzedWorkItem]]:
    """Analyze the library, then render rows that remain eligible to process.

    The analysis pass returns source metadata keyed by path. The render pass
    reuses it only when source size and mtime still match.
    """
    started_at = time.time()

    rows, work_items = run_analysis_job(
        settings,
        on_progress,
        cancel_flag,
        logger,
        emit_summary=False,
        emit_finished=False,
        apply_gain_threshold=settings.apply_render_gain_threshold,
    )
    if cancel_flag.is_set():
        return rows, work_items

    render_indices = eligible_render_indices(
        settings,
        rows,
        refresh_stale_would_process=True,
    )

    if render_indices:
        on_progress(
            "batch_phase",
            {
                "analysis_total": len(rows),
                "render_total": len(render_indices),
            },
        )
        rows = run_processing_job(
            settings,
            rows,
            on_progress,
            cancel_flag,
            logger,
            work_items=work_items,
            render_indices=render_indices,
            emit_summary=False,
            emit_finished=False,
        )
    else:
        logger.info("No tracks required rendering after analysis.")
        on_progress("progress_max", 1)
        on_progress("summary_counts", {
            "processed": 0,
            "would_process": 0,
            "analyzed_only": 0,
            "skipped": 0,
            "warnings": 0,
            "errors": 0,
        })

    elapsed = time.time() - started_at
    csv_output = settings.csv_path if settings.write_csv else None
    errors = sum(1 for row in rows if row.get("processing_status") == "error")

    if cancel_flag.is_set():
        logger.warning("Cancelled. Completed work and CSV rows remain saved.")
        on_progress("cancelled", csv_output)
        return rows, work_items

    summary = build_summary(
        rows,
        errors,
        elapsed,
        mp3_threshold=settings.mp3_threshold,
        lossless_threshold=settings.lossless_threshold,
    )
    logger.info("")
    logger.info("%s", summary)
    on_progress("finished", csv_output)

    return rows, work_items


def run_processing_job(
    settings: DropGainSettings,
    all_rows: list[TrackRow],
    on_progress: Callable[[str, Any], None],
    cancel_flag: threading.Event,
    logger: logging.Logger,
    work_items: dict[str, AnalyzedWorkItem] | None = None,
    *,
    render_indices: set[int] | frozenset[int] | None = None,
    refresh_stale_would_process: bool = False,
    emit_summary: bool = True,
    emit_finished: bool = True,
) -> list[TrackRow]:
    """Render eligible analyzed rows in place and return the full row list.

    When render_indices is omitted, every row eligible under current settings is
    rendered. Rows are rechecked before rendering. Cached source metadata is
    reused when work_items proves the source file is unchanged.
    """
    if render_indices is None:
        render_indices = eligible_render_indices(
            settings,
            all_rows,
            refresh_stale_would_process=refresh_stale_would_process,
        )

    cached_work_items = work_items or {}
    started_at = time.time()
    empty_counts = {
        "processed": 0,
        "would_process": 0,
        "analyzed_only": 0,
        "skipped": 0,
        "warnings": 0,
        "errors": 0,
    }

    on_progress("status", "Checking ffmpeg and ffprobe...")
    check_ffmpeg_available()

    rows_to_process: list[TrackRow] = []
    skipped_eligible = 0
    for index in sorted(render_indices):
        if index < 0 or index >= len(all_rows):
            continue
        row = all_rows[index]
        status = str(row.get("processing_status", ""))
        if status in {"error", "failed"} or "error" in status:
            continue
        should_process, status = should_process_row(
            row,
            mp3_threshold=settings.mp3_threshold,
            lossless_threshold=settings.lossless_threshold,
            allow_risky_true_peak_boost=settings.allow_risky_true_peak_boost,
            apply_gain_threshold=settings.apply_render_gain_threshold,
        )
        if should_process:
            rows_to_process.append(row)
        else:
            skipped_eligible += 1
            row["processing_status"] = f"analyzed_{status}"
            row["processing_error"] = ""
            row["audio_verification"] = "not_applicable"
            row["metadata_verification"] = "not_applicable"

    total = len(rows_to_process)
    if skipped_eligible:
        logger.info(
            "Skipped %s eligible row(s) because render rules no longer require processing.",
            skipped_eligible,
        )
    logger.info(
        "Directly rendering %s level-matched copies (%s clean-gain, %s limiter; clean workers=%s).",
        total,
        sum(1 for row in rows_to_process if not row_should_use_limiter(row)),
        sum(1 for row in rows_to_process if row_should_use_limiter(row)),
        max(1, int(settings.render_workers)),
    )
    on_progress("phase", "render")
    on_progress("progress_max", total)
    on_progress("summary_counts", dict(empty_counts))

    if total == 0:
        logger.info("Nothing to do.")
        if emit_finished:
            on_progress("finished", settings.csv_path if (settings.write_csv and os.path.exists(settings.csv_path)) else None)
        return all_rows

    if settings.write_csv:
        output_dir = os.path.dirname(os.path.abspath(settings.csv_path))
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

    completed = 0
    errors = 0
    run_counts = dict(empty_counts)
    write_lock = threading.Lock()

    def count_completed_row(row: TrackRow) -> None:
        status = row["processing_status"]
        audio_v = row["audio_verification"]
        meta_v = row["metadata_verification"]

        if status in {"processed", "processed_warning"}:
            run_counts["processed"] += 1
            if status == "processed_warning":
                run_counts["warnings"] += 1
        else:
            run_counts["skipped"] += 1

        if status != "processed_warning" and (audio_v == "warning" or meta_v == "warning"):
            run_counts["warnings"] += 1

    progress = JobProgressTicker(started_at, total)

    def tick() -> tuple[int, int, float, str, int, dict[str, int]]:
        return progress.snapshot(completed, errors, run_counts)

    def render_one(row: TrackRow) -> None:
        nonlocal completed, errors

        if cancel_flag.is_set():
            return

        path = row["path"]

        try:
            row["output_path"] = processed_output_path(
                path,
                output_root=settings.output_root,
                source_root=settings.folder,
                output_format_mode=settings.output_format_mode,
            )
            source_info = _resolve_render_source_info(path, cached_work_items)
            gain = parse_float_or_default(row["suggested_gain_db"], 0.0)
            render_analyzed_row(
                row,
                input_path=path,
                source_info=source_info,
                peak_ceiling_dbfs=settings.peak_ceiling_dbfs,
                target_high_lufs=settings.target_high_lufs,
                limiter_engine=settings.limiter_engine,
                post_loudness_window_seconds=settings.window_seconds,
                post_loudness_hop_seconds=settings.hop_seconds,
            )

            with write_lock:
                completed += 1
                count_completed_row(row)

                filename = row["filename"]
                loudest = row["loudest_section_lufs"]
                output_loudest = row["output_same_section_lufs"]
                true_peak = row["true_peak_dbtp"]
                output_true_peak = row["output_true_peak_dbtp"]
                peak_control = row["estimated_peak_control_db"]

                def _fmt_float(val: object, digits: int = 2, signed: bool = False) -> str:
                    try:
                        num = float(val)
                    except Exception:
                        return ""
                    sgn = "+" if signed else ""
                    return f"{num:{sgn}.{digits}f}"

                gain_text = f"{_fmt_float(gain, signed=True):>7} dB" if _fmt_float(gain, signed=True) else "     -- dB"
                loud_text = f"{_fmt_float(loudest):>6} -> {_fmt_float(output_loudest):>6} LUFS"
                tp_text = f"TP {_fmt_float(true_peak):>6} -> {_fmt_float(output_true_peak):>6} dBTP"

                try:
                    peak_ctrl = float(peak_control)
                except Exception:
                    peak_ctrl = 0.0
                if peak_ctrl <= 0.01:
                    peak_text = "TP safe"
                elif is_limiter_processing_engine(row["processing_engine"]):
                    peak_text = (
                        "limit "
                        + format_peak_control_display(
                            peak_control,
                            row.get("processing_engine"),
                        )
                    )
                else:
                    peak_text = f"TP over ceil {peak_ctrl:.2f} dB"

                status_label = "Processed" if row["processing_status"] == "processed" else "Processed with warning"

                logger.info(
                    f"{completed:>4}/{total:<4} RENDER "
                    f"{gain_text:>10}  "
                    f"loud {loud_text:<20}  "
                    f"{tp_text:<24}  "
                    f"{peak_text:<30}  "
                    f"{status_label:<22}  "
                    f"{filename}"
                )
                on_progress("tick", tick())

        except Exception as exc:
            with write_lock:
                errors += 1
                completed += 1
                row["processing_status"] = "error"
                row["processing_error"] = str(exc)

                logger.error(
                    f"{completed:>5}/{total:<5} ERROR  "
                    f"{os.path.basename(path)} - {exc}"
                )
                on_progress("tick", tick())

    csv_handle = None
    writer = None
    if settings.write_csv:
        csv_handle = open(settings.csv_path, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(csv_handle, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()

    clean_rows: list[TrackRow] = []
    limiter_rows: list[TrackRow] = []
    for row in rows_to_process:
        if row_should_use_limiter(row):
            limiter_rows.append(row)
        else:
            clean_rows.append(row)

    try:
        if clean_rows:
            with ThreadPoolExecutor(
                max_workers=max(1, int(settings.render_workers)),
                thread_name_prefix="DropGainCleanRender",
            ) as pool:
                futures = [pool.submit(render_one, row) for row in clean_rows]
                try:
                    for future in as_completed(futures):
                        if cancel_flag.is_set():
                            break
                        future.result()
                finally:
                    if cancel_flag.is_set():
                        for future in futures:
                            future.cancel()

        if not cancel_flag.is_set():
            for row in limiter_rows:
                if cancel_flag.is_set():
                    break
                render_one(row)
    finally:
        if writer is not None:
            with benchmark_timer("CSV write"):
                for row in all_rows:
                    try:
                        writer.writerow(row_to_csv_dict(row))
                    except Exception as exc:
                        path = str(row.get("path", ""))
                        logger.warning("CSV write failed for %s: %s", path or "<unknown>", exc)
                csv_handle.flush()
        if csv_handle is not None:
            csv_handle.close()

    elapsed = time.time() - started_at
    csv_output = settings.csv_path if settings.write_csv else None

    if cancel_flag.is_set():
        logger.warning("Cancelled. Completed copies and CSV rows remain saved.")
        on_progress("cancelled", csv_output)
        return all_rows

    if emit_summary:
        summary = build_summary(
            all_rows,
            errors,
            elapsed,
            mp3_threshold=settings.mp3_threshold,
            lossless_threshold=settings.lossless_threshold,
        )
        logger.info("")
        logger.info("%s", summary)

    if emit_finished:
        on_progress("finished", csv_output)

    return all_rows
