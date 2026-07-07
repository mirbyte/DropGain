"""
DropGain analysis and shared helpers.

This module contains configuration, path helpers, file discovery, ffprobe
helpers, loudness analysis, and summary reporting.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal, TypeAlias, TypedDict

try:
    import numpy as np
    import pyloudnorm as pyln
    from scipy.signal import resample_poly
except ImportError as exc:
    raise RuntimeError(
        "Required Python packages were not found.\n\n"
        "Install numpy, scipy, and pyloudnorm, then try again."
    ) from exc


# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================


APP_TITLE = "DropGain"

METER_SAMPLE_RATE = 48_000  # ITU-R BS.1770-4 specifies 48 kHz for loudness measurement.

DEFAULT_OUTPUT_CSV_NAME = "dropgain_report.csv"

ENABLE_BENCHMARK_TIMING_LOGS = str(os.environ.get("DROPGAIN_BENCHMARK_TIMING", "")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@contextmanager
def benchmark_timer(label: str, logger: logging.Logger | None = None):
    """Log elapsed time for a phase when benchmark timing is enabled."""
    if not ENABLE_BENCHMARK_TIMING_LOGS:
        yield
        return

    started_at = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started_at
        (logger or logging.getLogger("dropgain")).info("Timing %-28s %.3fs", label, elapsed)

PROCESSED_SUFFIX = "_DG"

OUTPUT_FORMAT_PRESERVE = "Preserve source format"
OUTPUT_FORMAT_MP3_TO_AIFF = "MP3 sources to AIFF"
OUTPUT_FORMAT_ALL_TO_AIFF = "All processed copies to AIFF"
OUTPUT_FORMAT_ALL_TO_MP3 = "All processed copies to MP3"
OUTPUT_FORMAT_MODE_CHOICES = (
    OUTPUT_FORMAT_PRESERVE,
    OUTPUT_FORMAT_MP3_TO_AIFF,
    OUTPUT_FORMAT_ALL_TO_AIFF,
    OUTPUT_FORMAT_ALL_TO_MP3,
)
DEFAULT_OUTPUT_FORMAT_MODE = OUTPUT_FORMAT_ALL_TO_AIFF

SUPPORTED_EXTENSIONS = {".flac", ".mp3", ".wav", ".aiff"}

SKIP_ALREADY_PROCESSED_FILES_IN_SCAN = True
PROCESS_OVERWRITE_EXISTING = False
# Treat tiny outputs as corrupt/truncated ffmpeg writes.
MIN_OUTPUT_FILE_BYTES = 10_000

LOSSLESS_MIN_ABS_GAIN_DB = 0.10
MP3_MIN_ABS_GAIN_DB = 0.10
EFFECTIVE_ZERO_GAIN_DB = 0.01
DEFAULT_APPLY_RENDER_GAIN_THRESHOLD = False

CPU_COUNT = max(1, os.cpu_count() or 1)

DEFAULT_RENDER_WORKER_THREADS = CPU_COUNT
MIN_RENDER_WORKER_THREADS = 1
MAX_RENDER_WORKER_THREADS = CPU_COUNT

DEFAULT_ANALYSIS_WORKER_THREADS = min(2, CPU_COUNT)
MIN_ANALYSIS_WORKER_THREADS = 1
MAX_ANALYSIS_WORKER_THREADS = CPU_COUNT
MAX_TRUE_PEAK_SUBPROCESSES = min(4, max(2, os.cpu_count() or 2))
TRUE_PEAK_OVERSAMPLE_FACTOR = 4
TRUE_PEAK_WINDOW_PADDING_SECONDS = 0.05


DEFAULT_LOUD_SECTION_WINDOW_SECONDS = 20.0
DEFAULT_LOUD_SECTION_HOP_SECONDS = 5.0

MIN_LOUD_SECTION_WINDOW_SECONDS = 10.0
MAX_LOUD_SECTION_WINDOW_SECONDS = 120.0

MIN_LOUD_SECTION_HOP_SECONDS = 5.0
MAX_LOUD_SECTION_HOP_SECONDS = 60.0

DEFAULT_TARGET_LOW_LUFS = -7.8
DEFAULT_TARGET_HIGH_LUFS = -7.5

DEFAULT_MAX_REDUCTION_DB = 3.0

DEFAULT_BASS_MAX_BOOST_REDUCTION_DB = 0.60
MIN_BASS_MAX_BOOST_REDUCTION_DB = 0.0
MAX_BASS_MAX_BOOST_REDUCTION_DB = 3.0

NORMALIZATION_MODE_LIMITER_ASSISTED = "Limiter-assisted"
NORMALIZATION_MODE_CLEAN_GAIN = "Clean gain"
NORMALIZATION_MODE_CHOICES = (
    NORMALIZATION_MODE_LIMITER_ASSISTED,
    NORMALIZATION_MODE_CLEAN_GAIN,
)
DEFAULT_NORMALIZATION_MODE = NORMALIZATION_MODE_LIMITER_ASSISTED

PEAK_CONTROL_SEVERITY_NONE = "none"
PEAK_CONTROL_SEVERITY_LIGHT = "light"
PEAK_CONTROL_SEVERITY_MODERATE = "moderate"
PEAK_CONTROL_SEVERITY_HEAVY = "heavy"

# True-peak ceiling for analysis, reporting, and Pro-L output level (-1.0 dBTP).
DEFAULT_BOOST_PEAK_CEILING_DBFS = -1.0

PROCESSING_ENGINE_PROL2 = "FabFilter Pro-L 2 Gain"
PROCESSING_ENGINE_LOUDMAX = "LoudMax Gain"
PROCESSING_ENGINE_CLEAN_GAIN = "Clean gain (no limiter)"
DEFAULT_PROCESSING_ENGINE = PROCESSING_ENGINE_PROL2

LIMITER_PROCESSING_ENGINES = {
    PROCESSING_ENGINE_PROL2,
    PROCESSING_ENGINE_LOUDMAX,
}

LIMITER_ENGINE_PROL2 = "FabFilter Pro-L 2"
LIMITER_ENGINE_LOUDMAX = "LoudMax"
LIMITER_ENGINE_CHOICES = (
    LIMITER_ENGINE_PROL2,
    LIMITER_ENGINE_LOUDMAX,
)
DEFAULT_LIMITER_ENGINE = LIMITER_ENGINE_PROL2

PROL2_DEFAULT_OUTPUT_LEVEL_DBFS = -1.0
PROL2_DEFAULT_TRUE_PEAK = True
PROL2_DEFAULT_OVERSAMPLING = "4x"

PROL2_STYLE_MODERN = "Modern"
PROL2_STYLE_TRANSPARENT = "Transparent"
PROL2_STYLE_SAFE = "Safe"
PROL2_STYLE_CHOICES = (
    PROL2_STYLE_MODERN,
    PROL2_STYLE_TRANSPARENT,
    PROL2_STYLE_SAFE,
)
# Transparent stays closest to the source instead of coloring hot EDM masters further.
PROL2_DEFAULT_STYLE = PROL2_STYLE_TRANSPARENT

PROL2_DEFAULT_PLUGIN_PATH = ""
PROL2_PROCESS_BUFFER_SIZE = 8192

LOUDMAX_DEFAULT_PLUGIN_PATH = ""
LOUDMAX_PROCESS_BUFFER_SIZE = 8192
# Empirical offset vs Pro-L 2 on limiter-assisted renders; LoudMax's simpler
# brickwall path tends to land slightly quieter at the same nominal settings.
LOUDMAX_LIMITER_CALIBRATION_DB = 0.10

MP3_OUTPUT_BITRATE = "320k"
MP3_ID3_VERSION = 3
# Extra true-peak headroom budget for forced MP3 encodes; skipped for preserve-format MP3.
MP3_ENCODE_TRUE_PEAK_LIFT_DB = 0.80

WAV_CODECS_TO_KEEP = {
    "pcm_u8",
    "pcm_s8",
    "pcm_s16le",
    "pcm_s24le",
    "pcm_s32le",
    "pcm_f32le",
    "pcm_f64le",
}

AIFF_CODECS_TO_KEEP = {
    "pcm_s16be",
    "pcm_s24be",
    "pcm_s32be",
}

STRICT_VERIFY_LOSSLESS_OUTPUT = True
POST_VERIFY_PROCESSED_AUDIO = True

POST_VERIFY_LUFS_TOLERANCE = 0.40
POST_VERIFY_PEAK_TOLERANCE_DB = 0.20

BASS_ANALYSIS_LOW_HZ = 45.0
BASS_ANALYSIS_HIGH_HZ = 150.0
BASS_REFERENCE_LOW_HZ = 150.0
BASS_REFERENCE_HIGH_HZ = 1000.0
BASS_PENALTY_START_DB = 3.0
BASS_PENALTY_FULL_DB = 12.0
BASS_ANALYSIS_FFT_SIZE = 16_384

SUB_ANALYSIS_LOW_HZ = 20.0
SUB_ANALYSIS_HIGH_HZ = 45.0
SUB_PENALTY_START_DB = 6.0
SUB_PENALTY_FULL_DB = 15.0

TrackRowValue: TypeAlias = str | int | float
TrackRowNumber: TypeAlias = float | str
TrackRowInt: TypeAlias = int | str
TrackRowKey: TypeAlias = Literal[
    "path",
    "output_path",
    "filename",
    "extension",
    "output_format_mode",
    "duration_sec",
    "original_sample_rate",
    "output_sample_rate",
    "channels",
    "output_channels",
    "audio_codec",
    "output_audio_codec",
    "audio_sample_fmt",
    "output_audio_sample_fmt",
    "original_bit_depth",
    "output_bit_depth",
    "original_bit_rate",
    "output_bit_rate",
    "file_size_mb",
    "output_file_size_mb",
    "integrated_lufs",
    "loudest_section_lufs",
    "loudest_section_start_sec",
    "loudest_section_end_sec",
    "sample_peak_dbfs",
    "true_peak_dbtp",
    "section_true_peak_dbtp",
    "peak_headroom_to_0_db",
    "true_peak_headroom_db",
    "target_low_lufs",
    "target_high_lufs",
    "normalization_mode",
    "bass_strength_db",
    "sub_strength_db",
    "bass_adjustment_db",
    "limiter_budget_db",
    "limiter_budget_adjustment_db",
    "raw_gain_db",
    "suggested_gain_db",
    "projected_loudest_section_lufs",
    "projected_sample_peak_dbfs",
    "projected_true_peak_dbtp",
    "estimated_peak_control_db",
    "peak_control_severity",
    "processing_engine",
    "output_integrated_lufs",
    "output_same_section_lufs",
    "output_sample_peak_dbfs",
    "output_true_peak_dbtp",
    "actual_same_section_gain_db",
    "actual_peak_gain_db",
    "actual_true_peak_gain_db",
    "audio_verification",
    "metadata_verification",
    "action",
    "processing_status",
    "processing_error",
    "warnings",
    "decision_notes",
    "true_peak_unreliable",
    "manual_check_required",
]


class TrackRow(TypedDict):
    """TypedDict representing all fields for one audio file in the CSV report."""
    path: str
    output_path: str
    filename: str
    extension: str
    output_format_mode: str
    duration_sec: TrackRowNumber
    original_sample_rate: int
    output_sample_rate: TrackRowInt
    channels: int
    output_channels: TrackRowInt
    audio_codec: str
    output_audio_codec: str
    audio_sample_fmt: str
    output_audio_sample_fmt: str
    original_bit_depth: TrackRowInt
    output_bit_depth: TrackRowInt
    original_bit_rate: str
    output_bit_rate: str
    file_size_mb: TrackRowNumber
    output_file_size_mb: TrackRowNumber
    integrated_lufs: TrackRowNumber
    loudest_section_lufs: TrackRowNumber
    loudest_section_start_sec: TrackRowNumber
    loudest_section_end_sec: TrackRowNumber
    sample_peak_dbfs: TrackRowNumber
    true_peak_dbtp: TrackRowNumber
    section_true_peak_dbtp: TrackRowNumber
    peak_headroom_to_0_db: TrackRowNumber
    true_peak_headroom_db: TrackRowNumber
    target_low_lufs: float
    target_high_lufs: float
    normalization_mode: str
    bass_strength_db: TrackRowNumber
    sub_strength_db: TrackRowNumber
    bass_adjustment_db: TrackRowNumber
    limiter_budget_db: TrackRowNumber
    limiter_budget_adjustment_db: TrackRowNumber
    raw_gain_db: TrackRowNumber
    suggested_gain_db: TrackRowNumber
    projected_loudest_section_lufs: TrackRowNumber
    projected_sample_peak_dbfs: TrackRowNumber
    projected_true_peak_dbtp: TrackRowNumber
    estimated_peak_control_db: TrackRowNumber
    peak_control_severity: str
    processing_engine: str
    output_integrated_lufs: TrackRowNumber
    output_same_section_lufs: TrackRowNumber
    output_sample_peak_dbfs: TrackRowNumber
    output_true_peak_dbtp: TrackRowNumber
    actual_same_section_gain_db: TrackRowNumber
    actual_peak_gain_db: TrackRowNumber
    actual_true_peak_gain_db: TrackRowNumber
    audio_verification: str
    metadata_verification: str
    action: str
    processing_status: str
    processing_error: str
    warnings: str
    decision_notes: str
    true_peak_unreliable: str
    manual_check_required: str


CSV_FIELDNAMES: tuple[TrackRowKey, ...] = (
    "path",
    "output_path",
    "filename",
    "extension",
    "output_format_mode",
    "duration_sec",
    "original_sample_rate",
    "output_sample_rate",
    "channels",
    "output_channels",
    "audio_codec",
    "output_audio_codec",
    "audio_sample_fmt",
    "output_audio_sample_fmt",
    "original_bit_depth",
    "output_bit_depth",
    "original_bit_rate",
    "output_bit_rate",
    "file_size_mb",
    "output_file_size_mb",
    "integrated_lufs",
    "loudest_section_lufs",
    "loudest_section_start_sec",
    "loudest_section_end_sec",
    "sample_peak_dbfs",
    "true_peak_dbtp",
    "section_true_peak_dbtp",
    "peak_headroom_to_0_db",
    "true_peak_headroom_db",
    "target_low_lufs",
    "target_high_lufs",
    "normalization_mode",
    "bass_strength_db",
    "sub_strength_db",
    "bass_adjustment_db",
    "limiter_budget_db",
    "limiter_budget_adjustment_db",
    "raw_gain_db",
    "suggested_gain_db",
    "projected_loudest_section_lufs",
    "projected_sample_peak_dbfs",
    "projected_true_peak_dbtp",
    "estimated_peak_control_db",
    "peak_control_severity",
    "processing_engine",
    "output_integrated_lufs",
    "output_same_section_lufs",
    "output_sample_peak_dbfs",
    "output_true_peak_dbtp",
    "actual_same_section_gain_db",
    "actual_peak_gain_db",
    "actual_true_peak_gain_db",
    "audio_verification",
    "metadata_verification",
    "action",
    "processing_status",
    "processing_error",
    "warnings",
    "decision_notes",
    "true_peak_unreliable",
    "manual_check_required",
)


def row_to_csv_dict(row: TrackRow) -> dict[TrackRowKey, TrackRowValue]:
    return {field: row[field] for field in CSV_FIELDNAMES}


# =============================================================================
# PATHS / GENERAL HELPERS
# =============================================================================


def script_folder() -> Path:
    """Return the folder containing the script or the frozen executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_csv_path() -> str:
    """Return the default path for the CSV report next to the executable/script."""
    return str(script_folder() / DEFAULT_OUTPUT_CSV_NAME)


def normalize_output_format_mode(value: object) -> str:
    """Return a valid output-format mode string, falling back to the default mode."""
    text = str(value or "").strip()
    if text in OUTPUT_FORMAT_MODE_CHOICES:
        return text
    return DEFAULT_OUTPUT_FORMAT_MODE


def output_extension_for_source(source_ext: str, output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE) -> str:
    """Return the output suffix for a source suffix under the selected output mode."""
    ext = str(source_ext or "").lower()
    mode = normalize_output_format_mode(output_format_mode)

    if mode == OUTPUT_FORMAT_ALL_TO_MP3:
        return ".mp3"
    if mode == OUTPUT_FORMAT_ALL_TO_AIFF:
        return ".aiff"
    if mode == OUTPUT_FORMAT_MP3_TO_AIFF and ext == ".mp3":
        return ".aiff"
    return ext


def output_format_mode_description(output_format_mode: object) -> str:
    """Return a concise log-friendly description of an output format mode."""
    mode = normalize_output_format_mode(output_format_mode)
    if mode == OUTPUT_FORMAT_MP3_TO_AIFF:
        return "MP3 sources render as AIFF; lossless sources preserve their format"
    if mode == OUTPUT_FORMAT_ALL_TO_AIFF:
        return "all processed copies render as Pioneer-compatible AIFF (44.1/48 kHz, 16/24-bit PCM)"
    if mode == OUTPUT_FORMAT_ALL_TO_MP3:
        return f"all processed copies render as {MP3_OUTPUT_BITRATE} MP3"
    return "preserve source format"


def output_format_mode_tooltip() -> str:
    """Return hover tooltip text for the output format setting."""
    return (
        "Recommended mode avoids encoding MP3 twice. "
        "Preserve and All-to-MP3 can push or approximate true peak above your ceiling; "
        "AIFF output stays closer to the limiter result."
    )


def output_format_mode_ui_hint(output_format_mode: object) -> tuple[str, bool]:
    """Return a short under-control note and whether it should use warning styling."""
    mode = normalize_output_format_mode(output_format_mode)
    if mode == OUTPUT_FORMAT_PRESERVE:
        return (
            "MP3 sources stay MP3: re-encoding can push true peak above your ceiling. "
            "Use MP3 sources to AIFF for safer true peak.",
            True,
        )
    if mode == OUTPUT_FORMAT_ALL_TO_MP3:
        return (
            f"Lossless sources become {MP3_OUTPUT_BITRATE} MP3 (destructive). "
            "True peak on MP3 outputs is approximate.",
            True,
        )
    if mode == OUTPUT_FORMAT_ALL_TO_AIFF:
        return (
            "Every processed copy becomes Pioneer-compatible AIFF (44.1/48 kHz, 16/24-bit PCM) "
            "for rekordbox and CDJ/XDJ import.",
            False,
        )
    return "", False


def processed_output_path(
    input_path: str,
    output_root: str | None = None,
    source_root: str | None = None,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
) -> str:
    """Build the output path by adding PROCESSED_SUFFIX, preserving subfolders.

    When the selected output mode changes the file extension, the source
    extension is added to the filename to avoid collisions such as Track.flac
    and Track.wav both rendering to Track_flac_DG.aiff and Track_wav_DG.aiff.
    """
    p = Path(input_path)
    mode = normalize_output_format_mode(output_format_mode)
    suffix = output_extension_for_source(p.suffix, mode)
    source_ext = p.suffix.lower()
    source_ext_marker = f"_{source_ext.lstrip('.')}" if suffix.lower() != source_ext else ""
    output_name = f"{p.stem}{source_ext_marker}{PROCESSED_SUFFIX}{suffix}"

    if output_root:
        root = Path(output_root)

        if source_root:
            try:
                relative_parent = p.resolve().parent.relative_to(Path(source_root).resolve())
            except ValueError:
                relative_parent = Path()

            return str(root / relative_parent / output_name)

        return str(root / output_name)

    return str(p.with_name(output_name))

def hidden_subprocess_kwargs() -> dict[str, int]:
    """Return subprocess kwargs that hide the console window on Windows."""
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def check_ffmpeg_available() -> None:
    """Raise RuntimeError if ffmpeg or ffprobe is not found in PATH."""
    for tool in ("ffmpeg", "ffprobe"):
        try:
            subprocess.run(
                [tool, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                **hidden_subprocess_kwargs(),
            )
        except Exception as exc:
            raise RuntimeError(
                f"{tool} was not found in PATH. Make sure ffmpeg and ffprobe are available."
            ) from exc


def normalized_path(path: str) -> str:
    """Return a case-normalized absolute path for deterministic sorting."""
    return os.path.normcase(os.path.abspath(path))


def make_error_track_row(
    path: str,
    error_message: str,
    *,
    source_root: str | None = None,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
) -> TrackRow:
    """Build a minimal TrackRow for a file that failed before analysis completed."""
    row = {field: "" for field in CSV_FIELDNAMES}
    source_path = Path(path)
    ext = source_path.suffix.lower()
    row.update(
        {
            "path": path,
            "output_path": processed_output_path(
                path,
                source_root=source_root,
                output_format_mode=output_format_mode,
            ),
            "filename": source_path.name,
            "extension": ext,
            "output_format_mode": normalize_output_format_mode(output_format_mode),
            "processing_status": "error",
            "processing_error": error_message,
            "audio_verification": "not_applicable",
            "metadata_verification": "not_applicable",
        }
    )
    return row  # type: ignore[return-value]


def sort_track_rows_by_path(rows: list[TrackRow]) -> list[TrackRow]:
    """Return rows sorted by normalized source path for stable library order."""
    return sorted(rows, key=lambda row: normalized_path(str(row.get("path", ""))))


def dbfs(value: float) -> float:
    """Convert a linear amplitude to dBFS. Non-positive values return -999.0."""
    if value <= 0.0 or not math.isfinite(value):
        return -999.0
    return 20.0 * math.log10(value)


_TRUE_PEAK_SUBPROCESS_SEMAPHORE = threading.BoundedSemaphore(MAX_TRUE_PEAK_SUBPROCESSES)


def decode_audio_ffmpeg_for_true_peak(
    path: str,
    channels: int,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> np.ndarray:
    """Decode native-rate float PCM for oversampled true-peak measurement."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
    ]

    if start_sec is not None:
        cmd.extend(["-ss", f"{max(0.0, float(start_sec)):.6f}"])

    if start_sec is not None and end_sec is not None:
        duration = max(0.001, float(end_sec) - float(start_sec))
        cmd.extend(["-t", f"{duration:.6f}"])

    cmd.extend([
        "-i",
        path,
        "-map",
        "0:a:0",
        "-vn",
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "pipe:1",
    ])

    with _TRUE_PEAK_SUBPROCESS_SEMAPHORE:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )

    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        if not err:
            err = "ffmpeg true-peak decode failed"
        raise RuntimeError(err)

    audio = np.frombuffer(result.stdout, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError("decoded true-peak audio is empty")

    remainder = audio.size % channels
    if remainder:
        audio = audio[: audio.size - remainder]

    if audio.size == 0:
        raise RuntimeError("decoded true-peak audio has invalid channel layout")

    return audio.reshape(-1, channels)


def _section_sample_bounds(
    start_sec: float,
    end_sec: float,
    sample_rate: int,
) -> tuple[int, int]:
    """Return native-rate [start, end) sample indices for an analyzed section."""
    start = max(0.0, float(start_sec))
    end = max(start + 0.001, float(end_sec))
    padding = TRUE_PEAK_WINDOW_PADDING_SECONDS
    padded_start = max(0.0, start - padding)
    trim_start_samples = max(0, int(round((start - padded_start) * sample_rate)))
    window_samples = max(1, int(round((end - start) * sample_rate)))
    decode_base = int(round(padded_start * sample_rate))
    section_start = decode_base + trim_start_samples
    section_end = section_start + window_samples
    return section_start, section_end


def _oversample_audio_for_true_peak(
    audio: np.ndarray,
    oversample_factor: int = TRUE_PEAK_OVERSAMPLE_FACTOR,
) -> tuple[np.ndarray, int]:
    """Oversample decoded native-rate audio for true-peak measurement."""
    oversample = max(1, int(oversample_factor))
    if oversample > 1:
        return resample_poly(audio, oversample, 1, axis=0), oversample
    return audio, oversample


def _true_peak_db_from_audio(measured: np.ndarray) -> float:
    """Return the true peak in dBTP from an oversampled audio array."""
    if measured.size == 0:
        raise RuntimeError("trimmed true-peak audio is empty")

    peak = float(np.max(np.abs(measured), initial=0.0))
    true_peak = dbfs(peak)
    if not math.isfinite(true_peak):
        raise RuntimeError("true peak measurement was not finite")
    return true_peak


def _resolve_true_peak_channels_and_rate(
    path: str,
    channels: int | None,
    sample_rate: int | None,
) -> tuple[int, int]:
    if channels is None or sample_rate is None:
        info = ffprobe_audio_info(path)
        if channels is None:
            channels = int(info["channels"])
        if sample_rate is None:
            sample_rate = int(info["sample_rate"])

    channels = int(channels)
    sample_rate = int(sample_rate)
    if channels <= 0 or sample_rate <= 0:
        raise RuntimeError("invalid channel count or sample rate for true-peak measurement")
    return channels, sample_rate


def measure_true_peak_oversampled(
    path: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
    *,
    channels: int | None = None,
    sample_rate: int | None = None,
    oversample_factor: int = TRUE_PEAK_OVERSAMPLE_FACTOR,
) -> float:
    """Measure true peak in dBTP with native-rate polyphase oversampling."""
    channels, sample_rate = _resolve_true_peak_channels_and_rate(path, channels, sample_rate)

    decode_start = start_sec
    decode_end = end_sec
    trim_start_samples = 0
    trim_end_samples: int | None = None

    if start_sec is not None and end_sec is not None:
        section_start, section_end = _section_sample_bounds(start_sec, end_sec, sample_rate)
        start = max(0.0, float(start_sec))
        end = max(start + 0.001, float(end_sec))
        padding = TRUE_PEAK_WINDOW_PADDING_SECONDS
        padded_start = max(0.0, start - padding)
        padded_end = end + padding
        decode_start = padded_start
        decode_end = padded_end
        decode_base = int(round(padded_start * sample_rate))
        trim_start_samples = section_start - decode_base
        trim_end_samples = section_end - decode_base

    audio = decode_audio_ffmpeg_for_true_peak(
        path,
        channels,
        start_sec=decode_start,
        end_sec=decode_end,
    )

    measured, oversample = _oversample_audio_for_true_peak(audio, oversample_factor)
    trim_start = trim_start_samples * oversample
    trim_end = None if trim_end_samples is None else trim_end_samples * oversample

    if trim_start or trim_end is not None:
        measured = measured[trim_start:trim_end]

    return _true_peak_db_from_audio(measured)


def measure_section_and_whole_true_peak_oversampled(
    path: str,
    start_sec: float,
    end_sec: float,
    *,
    channels: int | None = None,
    sample_rate: int | None = None,
    section_failure_label: str = "section true peak measurement failed",
    whole_failure_label: str = "whole-track true peak measurement failed",
) -> tuple[float | None, float | None, str]:
    """Measure section and whole-track true peaks from one decode and oversample pass."""
    section_true_peak: float | None = None
    whole_true_peak: float | None = None
    notes = ""

    try:
        channels, sample_rate = _resolve_true_peak_channels_and_rate(path, channels, sample_rate)
        audio = decode_audio_ffmpeg_for_true_peak(path, channels)
        oversampled, oversample_factor = _oversample_audio_for_true_peak(audio)
    except Exception as exc:
        notes = append_note(notes, f"{whole_failure_label}: {exc}")
        notes = append_note(notes, f"{section_failure_label}: {exc}")
        return section_true_peak, whole_true_peak, notes

    try:
        whole_true_peak = _true_peak_db_from_audio(oversampled)
    except Exception as exc:
        notes = append_note(notes, f"{whole_failure_label}: {exc}")

    try:
        section_start, section_end = _section_sample_bounds(start_sec, end_sec, sample_rate)
        section_start *= oversample_factor
        section_end *= oversample_factor
        section_end = min(section_end, oversampled.shape[0])
        section_audio = oversampled[section_start:section_end]
        section_true_peak = _true_peak_db_from_audio(section_audio)
    except Exception as exc:
        notes = append_note(notes, f"{section_failure_label}: {exc}")

    return section_true_peak, whole_true_peak, notes


def round_or_blank(value: float | None, digits: int = 2) -> float | str:
    """Round a number for CSV output. Return blank string for None or non-finite."""
    if value is None:
        return ""
    if not math.isfinite(float(value)):
        return ""
    return round(float(value), digits)


def parse_optional_float(value: object) -> float | None:
    """Safely parse a value to float, returning None on failure or non-finite."""
    try:
        f = float(value)
    except Exception:
        return None

    if not math.isfinite(f):
        return None

    return f


def parse_float_or_default(value: object, default: float = 0.0) -> float:
    """Parse a value to float, falling back to default on failure."""
    parsed = parse_optional_float(value)
    if parsed is None:
        return default
    return parsed


def parse_int_or_default(value: object, default: int = 0) -> int:
    """Parse a value to int via float(), falling back to default on failure."""
    try:
        if value is None:
            return default
        text = str(value).strip()
        if text == "":
            return default
        return int(float(text))
    except Exception:
        return default


def append_note(existing: object, note: str) -> str:
    """Append a semicolon-separated note to an existing string."""
    old = str(existing or "").strip()
    if not old:
        return note
    return old + "; " + note


def mp3_encode_peak_allowance_db(output_ext: str) -> float:
    """Return extra true-peak headroom to budget for any final MP3 encode."""
    if str(output_ext or "").lower() == ".mp3":
        return MP3_ENCODE_TRUE_PEAK_LIFT_DB
    return 0.0


def normalize_normalization_mode(value: object) -> str:
    """Return a valid normalization mode string, falling back to the default."""
    text = str(value or "").strip()
    if text in NORMALIZATION_MODE_CHOICES:
        return text
    return DEFAULT_NORMALIZATION_MODE


def normalize_limiter_engine(value: object) -> str:
    """Return a valid limiter engine string, falling back to the default."""
    text = str(value or "").strip()
    if text in LIMITER_ENGINE_CHOICES:
        return text
    return DEFAULT_LIMITER_ENGINE


def processing_engine_for_limiter(limiter_engine: object) -> str:
    """Return the processing-engine label a limiter-assisted row should record."""
    if normalize_limiter_engine(limiter_engine) == LIMITER_ENGINE_LOUDMAX:
        return PROCESSING_ENGINE_LOUDMAX
    return PROCESSING_ENGINE_PROL2


def is_limiter_processing_engine(value: object) -> bool:
    """Return True when a row's processing_engine used a limiter (any engine)."""
    return str(value or "") in LIMITER_PROCESSING_ENGINES


def peak_control_severity_label(estimated_peak_control_db: float) -> str:
    """Classify estimated peak limiting into none/light/moderate/heavy."""
    estimated = max(0.0, float(estimated_peak_control_db))

    if estimated <= 0.01:
        return PEAK_CONTROL_SEVERITY_NONE
    if estimated <= 1.0:
        return PEAK_CONTROL_SEVERITY_LIGHT
    if estimated <= 3.0:
        return PEAK_CONTROL_SEVERITY_MODERATE
    return PEAK_CONTROL_SEVERITY_HEAVY


def peak_control_reduction_percent(peak_control_db: float) -> int | None:
    """Return peak amplitude reduction as a linear percent from the limit depth in dB."""
    peak = max(0.0, float(peak_control_db))
    if peak <= 0.01:
        return None
    return min(100, round((1.0 - 10.0 ** (-peak / 20.0)) * 100.0))


def format_peak_control_display(
    estimated_peak_control_db: object,
    processing_engine: object = None,
    *,
    include_percent: bool = True,
) -> str:
    """Format estimated limiter peak control as dB and optional linear peak-reduction percent."""
    peak = parse_optional_float(estimated_peak_control_db)
    if peak is None or peak <= 0.01:
        return "-"

    uses_limiter = is_limiter_processing_engine(processing_engine)
    text = f"{peak:.2f} dB"

    if not uses_limiter:
        text = f"{text} (clean)"
    elif include_percent:
        pct = peak_control_reduction_percent(peak)
        if pct is not None:
            text = f"{text} ({pct}%)"

    return text


def min_abs_gain_for_extension(
    ext: str,
    mp3_threshold: float | None = None,
    lossless_threshold: float | None = None,
) -> float:
    """Return the minimum absolute gain required to trigger processing for a format.

    If mp3_threshold or lossless_threshold is supplied it overrides the module-level
    constant, allowing the GUI to control the threshold at runtime.
    """
    ext = ext.lower()
    lossless_val = lossless_threshold if lossless_threshold is not None else LOSSLESS_MIN_ABS_GAIN_DB

    if ext == ".mp3":
        return mp3_threshold if mp3_threshold is not None else MP3_MIN_ABS_GAIN_DB

    if ext in {".flac", ".wav", ".aiff"}:
        return lossless_val

    return lossless_val


def find_audio_files(root_dir: str) -> list[str]:
    """Recursively find supported audio files, skipping already-processed copies."""
    paths: list[str] = []

    def ignore_walk_error(_error: OSError) -> None:
        return

    for dirpath, _dirnames, filenames in os.walk(root_dir, onerror=ignore_walk_error):
        for filename in filenames:
            p = Path(filename)
            suffix = p.suffix.lower()
            stem = p.stem

            if suffix not in SUPPORTED_EXTENSIONS:
                continue

            if SKIP_ALREADY_PROCESSED_FILES_IN_SCAN and stem.casefold().endswith(PROCESSED_SUFFIX.casefold()):
                continue

            paths.append(os.path.join(dirpath, filename))

    return sorted(paths, key=normalized_path)


# =============================================================================
# AUDIO INFO / ANALYSIS
# =============================================================================


def ffprobe_audio_info(path: str) -> dict[str, object]:
    """Run ffprobe and return basic audio stream metadata as a dict."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_fmt,sample_rate,channels,duration,bits_per_sample,bits_per_raw_sample,bit_rate",
        "-show_entries",
        "format=duration,bit_rate",
        "-of",
        "json",
        path,
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **hidden_subprocess_kwargs(),
    )

    if result.returncode != 0:
        err = (result.stderr or "").strip() or f"ffprobe failed for {path}"
        raise RuntimeError(f"ffprobe error: {err}")

    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    if not streams:
        raise RuntimeError("no audio stream found")

    stream = streams[0]

    sample_rate = int(stream.get("sample_rate") or 0)
    channels = int(stream.get("channels") or 0)

    duration_raw = stream.get("duration") or fmt.get("duration") or 0.0
    try:
        duration = float(duration_raw)
    except Exception:
        duration = 0.0

    if channels <= 0:
        raise RuntimeError("could not determine channel count")

    bit_rate = stream.get("bit_rate") or fmt.get("bit_rate") or ""

    return {
        "codec_name": str(stream.get("codec_name") or ""),
        "sample_fmt": str(stream.get("sample_fmt") or ""),
        "sample_rate": sample_rate,
        "channels": channels,
        "duration": duration,
        "bits_per_sample": str(stream.get("bits_per_sample") or ""),
        "bits_per_raw_sample": str(stream.get("bits_per_raw_sample") or ""),
        "bit_rate": str(bit_rate or ""),
    }


def infer_bit_depth(info: dict[str, object], ext: str) -> int | str:
    """Guess bit depth from codec, sample_fmt, or ffprobe bit_depth fields."""
    ext = ext.lower()
    codec = str(info.get("codec_name") or "").lower()
    sample_fmt = str(info.get("sample_fmt") or "").lower()

    codec_bit_depths = {
        "pcm_u8": 8,
        "pcm_s8": 8,
        "pcm_s16le": 16,
        "pcm_s16be": 16,
        "pcm_s24le": 24,
        "pcm_s24be": 24,
        "pcm_s32le": 32,
        "pcm_s32be": 32,
        "pcm_f32le": 32,
        "pcm_f32be": 32,
        "pcm_f64le": 64,
        "pcm_f64be": 64,
    }

    if codec in codec_bit_depths:
        return codec_bit_depths[codec]

    raw = parse_int_or_default(info.get("bits_per_raw_sample"), 0)
    if raw > 0:
        return raw

    bps = parse_int_or_default(info.get("bits_per_sample"), 0)
    if bps > 0:
        return bps

    if ext == ".mp3":
        return ""

    if sample_fmt.startswith("u8"):
        return 8

    if sample_fmt.startswith("s16"):
        return 16

    if sample_fmt.startswith("s32"):
        # FFmpeg often reports decoded 24-bit FLAC as s32 because
        # the 24-bit samples are stored in a 32-bit container.
        if ext == ".flac":
            return 24
        return 32

    if sample_fmt.startswith("flt"):
        return 32

    if sample_fmt.startswith("dbl"):
        return 64

    return ""


def flac_sample_fmt_to_use(source_info: dict[str, object]) -> str:
    """Choose FLAC encoder sample format (s16 or s32) based on source bit depth."""
    bit_depth = infer_bit_depth(source_info, ".flac")
    bit_depth_int = parse_int_or_default(bit_depth, 0)

    if bit_depth_int <= 16:
        return "s16"

    return "s32"


def wav_codec_to_use(source_info: dict[str, object]) -> str:
    """Return the source WAV codec if it is in the safe-to-keep list."""
    codec = str(source_info.get("codec_name") or "").lower().strip()

    if codec in WAV_CODECS_TO_KEEP:
        return codec

    raise RuntimeError(f"unsupported WAV codec for safe processing: {codec or 'unknown'}")


def aiff_codec_to_use(source_info: dict[str, object], source_ext: str = "") -> str:
    """Return an AIFF PCM codec that matches the source bit depth.

    Native AIFF sources keep their original safe PCM codec. Cross-format copies
    map source bit depth to the closest big-endian AIFF PCM codec. Lossy
    sources with no detectable depth default to 24-bit PCM.
    """
    codec = str(source_info.get("codec_name") or "").lower().strip()

    if codec in AIFF_CODECS_TO_KEEP:
        return codec

    ext = str(source_ext or "").lower()
    bit_depth_int = parse_int_or_default(infer_bit_depth(source_info, ext), 0)

    if bit_depth_int > 0:
        if bit_depth_int <= 16:
            return "pcm_s16be"
        if bit_depth_int <= 24:
            return "pcm_s24be"
        return "pcm_s32be"

    return "pcm_s24be"


PIONEER_COMPATIBLE_AIFF_SAMPLE_RATES = (44_100, 48_000)
PIONEER_COMPATIBLE_AIFF_CODECS = frozenset({"pcm_s16be", "pcm_s24be"})
AIFF_OUTPUT_SUFFIXES = frozenset({".aiff", ".aif", ".aifc"})


def is_aiff_output(output_path: str) -> bool:
    """Return True when the output path uses an AIFF-family extension."""
    return Path(str(output_path or "")).suffix.lower() in AIFF_OUTPUT_SUFFIXES


def requires_pioneer_compatible_aiff(output_format_mode: object, output_path: str) -> bool:
    """Return True when an intentional AIFF export should use the DJ-safe profile."""
    if not is_aiff_output(output_path):
        return False

    mode = normalize_output_format_mode(output_format_mode)
    return mode in {
        OUTPUT_FORMAT_ALL_TO_AIFF,
        OUTPUT_FORMAT_MP3_TO_AIFF,
    }


def pioneer_compatible_aiff_sample_rate(source_sr: int) -> int:
    """Map any source rate to 44.1 or 48 kHz for Pioneer player compatibility."""
    if source_sr in PIONEER_COMPATIBLE_AIFF_SAMPLE_RATES:
        return source_sr
    if source_sr in (88_200, 176_400, 352_800):
        return 44_100
    if source_sr in (96_000, 192_000):
        return 48_000
    return 48_000


def pioneer_compatible_aiff_codec(
    source_info: dict[str, object],
    source_ext: str = "",
) -> str:
    """Return 16- or 24-bit PCM for All-to-AIFF exports, matching source depth when known."""
    bit_depth_int = parse_int_or_default(
        infer_bit_depth(source_info, str(source_ext or "")),
        0,
    )
    if bit_depth_int > 0 and bit_depth_int <= 16:
        return "pcm_s16be"
    return "pcm_s24be"


def decode_audio_ffmpeg(path: str, channels: int) -> np.ndarray:
    """Decode audio via ffmpeg to float64 (samples, channels) at METER_SAMPLE_RATE.

    Returns float64 because pyloudnorm's ITU-R BS.1770 integrated loudness
    measurement benefits from full double-precision accuracy. This differs
    from processing.decode_audio_ffmpeg_at_sample_rate which returns float32
    for pedalboard/VST3 rendering (VST3 does not use float64).
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        path,
        "-map",
        "0:a:0",
        "-vn",
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "-ar",
        str(METER_SAMPLE_RATE),
        "pipe:1",
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    )

    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        if not err:
            err = "ffmpeg decode failed"
        raise RuntimeError(err)

    audio = np.frombuffer(result.stdout, dtype=np.float32)

    if audio.size == 0:
        raise RuntimeError("decoded audio is empty")

    remainder = audio.size % channels
    if remainder:
        audio = audio[: audio.size - remainder]

    if audio.size == 0:
        raise RuntimeError("decoded audio has invalid channel layout")

    audio = audio.reshape(-1, channels)
    return audio.astype(np.float64, copy=False)


def audio_for_loudness_meter(audio: np.ndarray) -> np.ndarray:
    """Return audio in the shape pyloudnorm expects for integrated loudness."""
    if audio.ndim == 1:
        return audio

    if audio.shape[1] == 1:
        return audio[:, 0]

    return audio


def measure_lufs_input(meter: pyln.Meter, meter_input: np.ndarray) -> float:
    """Measure integrated loudness from data already shaped for pyloudnorm."""
    measured = float(meter.integrated_loudness(meter_input))

    if not math.isfinite(measured):
        raise RuntimeError("LUFS measurement was not finite")

    return measured


def measure_lufs(meter: pyln.Meter, audio: np.ndarray) -> float:
    """Measure integrated loudness, raising if the result is not finite."""
    return measure_lufs_input(meter, audio_for_loudness_meter(audio))


def loudest_section_lufs(
    audio: np.ndarray,
    meter: pyln.Meter,
    window_seconds: float,
    hop_seconds: float,
    meter_input: np.ndarray | None = None,
) -> tuple[float, float, float]:
    """Find the loudest window of audio using integrated LUFS and a sliding hop."""
    sample_count = audio.shape[0]
    meter_data = audio_for_loudness_meter(audio) if meter_input is None else meter_input

    window_samples = int(round(window_seconds * METER_SAMPLE_RATE))
    hop_samples = int(round(hop_seconds * METER_SAMPLE_RATE))

    window_samples = max(window_samples, int(1.0 * METER_SAMPLE_RATE))
    hop_samples = max(hop_samples, int(1.0 * METER_SAMPLE_RATE))

    if sample_count <= window_samples:
        lufs = measure_lufs_input(meter, meter_data)
        return lufs, 0.0, sample_count / METER_SAMPLE_RATE

    final_start = sample_count - window_samples
    starts = list(range(0, final_start + 1, hop_samples))
    if starts[-1] != final_start:
        starts.append(final_start)

    window_ranges = [(start, start + window_samples) for start in starts]
    best_lufs: float | None = None
    best_start = 0

    for start, end in window_ranges:
        try:
            value = measure_lufs_input(meter, meter_data[start:end])
        except Exception:
            continue

        if best_lufs is None or value > best_lufs:
            best_lufs = value
            best_start = start

    if best_lufs is None:
        lufs = measure_lufs_input(meter, meter_data)
        return lufs, 0.0, sample_count / METER_SAMPLE_RATE

    start_sec = best_start / METER_SAMPLE_RATE
    end_sec = (best_start + window_samples) / METER_SAMPLE_RATE

    return best_lufs, start_sec, end_sec



def measure_relative_band_strength_db(
    audio: np.ndarray,
    sample_rate: int,
    start_sec: float,
    end_sec: float,
    band_low_hz: float,
    band_high_hz: float,
    reference_low_hz: float = BASS_REFERENCE_LOW_HZ,
    reference_high_hz: float = BASS_REFERENCE_HIGH_HZ,
) -> float:
    """Return one frequency band's spectral strength relative to the reference band."""
    sr = max(1, int(sample_rate))
    start_sample = max(0, int(round(float(start_sec) * sr)))
    end_sample = max(start_sample + 1, int(round(float(end_sec) * sr)))
    end_sample = min(end_sample, audio.shape[0])

    section = audio[start_sample:end_sample]
    if section.size == 0:
        raise RuntimeError("band analysis window is empty")

    if section.ndim > 1:
        mono = np.mean(section, axis=1)
    else:
        mono = section

    mono = np.asarray(mono, dtype=np.float64)
    mono = mono[np.isfinite(mono)]
    if mono.size < 2048:
        raise RuntimeError("band analysis window is too short")

    fft_size = min(BASS_ANALYSIS_FFT_SIZE, 2 ** int(math.floor(math.log2(mono.size))))
    fft_size = max(2048, fft_size)
    hop_size = max(1, fft_size // 2)

    freqs = np.fft.rfftfreq(fft_size, d=1.0 / sr)
    band_mask = (freqs >= band_low_hz) & (freqs < band_high_hz)
    ref_mask = (freqs >= reference_low_hz) & (freqs < reference_high_hz)
    if not np.any(band_mask) or not np.any(ref_mask):
        raise RuntimeError("band analysis frequency bands are empty")

    window = np.hanning(fft_size)
    band_powers: list[float] = []
    ref_powers: list[float] = []

    for start in range(0, mono.size - fft_size + 1, hop_size):
        frame = mono[start:start + fft_size]
        frame = frame - float(np.mean(frame))
        spectrum = np.fft.rfft(frame * window)
        power = np.square(np.abs(spectrum))
        band_powers.append(float(np.mean(power[band_mask])))
        ref_powers.append(float(np.mean(power[ref_mask])))

    if not band_powers or not ref_powers:
        raise RuntimeError("band analysis produced no frames")

    eps = 1.0e-20
    band_power = float(np.median(np.array(band_powers, dtype=np.float64)))
    ref_power = float(np.median(np.array(ref_powers, dtype=np.float64)))
    strength = 10.0 * math.log10((band_power + eps) / (ref_power + eps))

    if not math.isfinite(strength):
        raise RuntimeError("band strength was not finite")

    return strength


def measure_bass_strength_db(
    audio: np.ndarray,
    sample_rate: int,
    start_sec: float,
    end_sec: float,
) -> float:
    """Return 45-150 Hz strength relative to the low-mid reference band."""
    return measure_relative_band_strength_db(
        audio=audio,
        sample_rate=sample_rate,
        start_sec=start_sec,
        end_sec=end_sec,
        band_low_hz=BASS_ANALYSIS_LOW_HZ,
        band_high_hz=BASS_ANALYSIS_HIGH_HZ,
    )


def measure_sub_strength_db(
    audio: np.ndarray,
    sample_rate: int,
    start_sec: float,
    end_sec: float,
) -> float:
    """Return 20-45 Hz strength relative to the low-mid reference band."""
    return measure_relative_band_strength_db(
        audio=audio,
        sample_rate=sample_rate,
        start_sec=start_sec,
        end_sec=end_sec,
        band_low_hz=SUB_ANALYSIS_LOW_HZ,
        band_high_hz=SUB_ANALYSIS_HIGH_HZ,
    )


def bass_aware_gain_trim_db(
    bass_strength_db: float | None,
    sub_strength_db: float | None,
    suggested_gain_db: float,
    bass_max_reduction_db: float = DEFAULT_BASS_MAX_BOOST_REDUCTION_DB,
) -> float:
    """Return a mild low-end trim magnitude applied against the suggested gain direction."""
    max_reduction = max(0.0, float(bass_max_reduction_db))
    if max_reduction <= 0.01:
        return 0.0

    gain = float(suggested_gain_db)
    if abs(gain) <= 0.01:
        return 0.0

    bass_reduction = boost_reduction_for_strength(
        bass_strength_db,
        BASS_PENALTY_START_DB,
        BASS_PENALTY_FULL_DB,
        max_reduction,
    )
    sub_reduction = boost_reduction_for_strength(
        sub_strength_db,
        SUB_PENALTY_START_DB,
        SUB_PENALTY_FULL_DB,
        max_reduction,
    )
    reduction = max(bass_reduction, sub_reduction)
    return min(abs(gain), reduction)


def mark_bass_aware_action(action: str) -> str:
    """Append a bass-aware suffix when trim was applied."""
    label = str(action).strip()
    if not label or "bass-aware" in label:
        return label
    return f"{label} bass-aware"


def boost_reduction_for_strength(
    strength_db: float | None,
    start_db: float,
    full_db: float,
    max_reduction_db: float,
) -> float:
    """Return a boost reduction for a measured low-end band strength."""
    if strength_db is None or not math.isfinite(float(strength_db)):
        return 0.0

    strength = float(strength_db)
    if strength <= start_db:
        return 0.0

    span = max(0.001, full_db - start_db)
    normalized = min(1.0, max(0.0, (strength - start_db) / span))
    return normalized * max(0.0, max_reduction_db)


def calculate_gain_suggestion(
    loudest_lufs: float,
    peak_reference_dbfs: float,
    target_low: float,
    target_high: float,
    max_reduction_db: float,
    peak_ceiling_dbfs: float,
    normalization_mode: str = DEFAULT_NORMALIZATION_MODE,
) -> tuple[float, float, str, str]:
    """Suggest a gain move for the loudest section.

    The loudest-section LUFS target decides the first gain move. In clean gain
    mode the true-peak ceiling is enforced against peak_reference_dbfs here.
    In limiter-assisted mode whole-track limiter budgeting is deferred to
    apply_whole_track_limiter_budget() once bass adjustment and MP3 encode
    allowance are known.
    """
    mode = normalize_normalization_mode(normalization_mode)
    notes: list[str] = []

    if loudest_lufs < target_low:
        raw_gain = target_low - loudest_lufs
        action = "raise"
    elif loudest_lufs > target_high:
        raw_gain = target_high - loudest_lufs
        action = "lower"
    else:
        raw_gain = 0.0
        action = "leave"

    suggested_gain = raw_gain
    projected_true_peak = peak_reference_dbfs + suggested_gain
    peak_overage = projected_true_peak - peak_ceiling_dbfs

    if peak_overage > 0.01 and mode == NORMALIZATION_MODE_CLEAN_GAIN:
        clean_peak_safe_gain = peak_ceiling_dbfs - peak_reference_dbfs
        if clean_peak_safe_gain < suggested_gain - 0.01:
            suggested_gain = clean_peak_safe_gain

            if action == "leave":
                action = "lower for true peak"
                notes.append(
                    f"in-target track lowered to meet true-peak ceiling ({peak_ceiling_dbfs:.1f} dBTP)"
                )
            elif action == "raise" and suggested_gain <= 0.01:
                action = "too quiet but peak-limited"
                notes.append(
                    f"gain limited by true-peak ceiling ({peak_ceiling_dbfs:.1f} dBTP)"
                )
            else:
                notes.append(
                    f"gain adjusted to meet true-peak ceiling ({peak_ceiling_dbfs:.1f} dBTP)"
                )

    if abs(suggested_gain) < 0.01:
        suggested_gain = 0.0

    return raw_gain, suggested_gain, action, "; ".join(notes)


def apply_whole_track_limiter_budget(
    suggested_gain: float,
    action: str,
    *,
    reference_true_peak_dbtp: float,
    peak_ceiling_dbfs: float,
    mp3_encode_lift_db: float,
    limiter_budget_db: float,
) -> tuple[float, float, str, str]:
    """Clamp gain so estimated whole-track peak control stays within the limiter budget.

    reference_true_peak_dbtp is the track's worst-case input true peak (max of
    loudest-section and whole-track measurements), not the section peak alone.
    """
    notes: list[str] = []
    limiter_adjustment = 0.0
    budget = max(0.0, float(limiter_budget_db))

    encode_adjusted = float(reference_true_peak_dbtp) + float(suggested_gain) + float(mp3_encode_lift_db)
    estimated_control = max(0.0, encode_adjusted - float(peak_ceiling_dbfs))
    over_budget = estimated_control - budget

    if over_budget <= 0.01:
        if estimated_control > 0.01:
            notes.append(f"limiter will handle {estimated_control:.2f} dB true-peak overage")
        return suggested_gain, limiter_adjustment, action, "; ".join(notes)

    suggested_gain -= over_budget
    limiter_adjustment = over_budget

    if action == "leave" and suggested_gain < -0.01:
        action = "lower for true peak"
    elif action.startswith("raise") and suggested_gain <= 0.01:
        action = "too quiet but peak-limited"

    notes.append(
        f"gain reduced {over_budget:.2f} dB so whole-track limiter peak control stays near "
        f"{budget:.1f} dB"
    )

    if abs(suggested_gain) < 0.01:
        suggested_gain = 0.0

    return suggested_gain, limiter_adjustment, action, "; ".join(notes)


@dataclass
class TrackDecision:
    """Gain/limiter decision fields derived from measured audio values."""

    target_low_lufs: float
    target_high_lufs: float
    normalization_mode: str
    limiter_budget_db: float | str
    bass_adjustment_db: float | str
    limiter_budget_adjustment_db: float | str
    raw_gain_db: float | str
    suggested_gain_db: float | str
    projected_loudest_section_lufs: float | str
    projected_sample_peak_dbfs: float | str
    projected_true_peak_dbtp: float | str
    estimated_peak_control_db: float | str
    peak_control_severity: str
    true_peak_headroom_db: float | str
    action: str
    decision_notes: str
    true_peak_unreliable: str
    manual_check_required: str
    uses_limiter: bool


def _resolve_section_true_peak_dbtp(
    section_true_peak_dbtp: float | None,
    true_peak_dbtp: float,
) -> float | None:
    if section_true_peak_dbtp is not None:
        return section_true_peak_dbtp
    return true_peak_dbtp


def decide_from_measurements(
    *,
    loudest_lufs: float,
    sample_peak_dbfs: float,
    true_peak_dbtp: float,
    section_true_peak_dbtp: float | None,
    bass_strength: float | None,
    sub_strength: float | None,
    source_ext: str,
    output_ext: str,
    output_format_mode: str,
    target_low: float,
    target_high: float,
    max_reduction_db: float,
    peak_ceiling_dbfs: float,
    normalization_mode: str,
    bass_max_reduction_db: float = DEFAULT_BASS_MAX_BOOST_REDUCTION_DB,
    allow_risky_true_peak_boost: bool = False,
    true_peak_measurements_present: bool = True,
) -> TrackDecision:
    """Pure gain/limiter decision from measured values (no audio decode)."""
    mode = normalize_normalization_mode(normalization_mode)
    section_tp = _resolve_section_true_peak_dbtp(section_true_peak_dbtp, true_peak_dbtp)

    if mode == NORMALIZATION_MODE_LIMITER_ASSISTED and section_tp is not None:
        peak_reference_dbfs = section_tp
    else:
        peak_reference_dbfs = true_peak_dbtp

    limiter_budget_db = max(0.0, float(max_reduction_db))
    limiter_budget_adjustment = 0.0
    decision_notes = ""
    if output_ext != source_ext:
        if source_ext == ".mp3" and output_ext == ".aiff":
            decision_notes = append_note(
                decision_notes,
                "source MP3 will be decoded to AIFF; second MP3 encode is avoided",
            )
        elif output_ext == ".aiff":
            decision_notes = append_note(decision_notes, "AIFF output")
        elif output_ext == ".mp3":
            decision_notes = append_note(decision_notes, "MP3 output")

    raw_gain, suggested_gain, action, gain_notes = calculate_gain_suggestion(
        loudest_lufs=loudest_lufs,
        peak_reference_dbfs=peak_reference_dbfs,
        target_low=target_low,
        target_high=target_high,
        max_reduction_db=limiter_budget_db,
        peak_ceiling_dbfs=peak_ceiling_dbfs,
        normalization_mode=mode,
    )
    if gain_notes:
        decision_notes = append_note(decision_notes, gain_notes)

    bass_adjustment = bass_aware_gain_trim_db(
        bass_strength,
        sub_strength,
        suggested_gain,
        bass_max_reduction_db=bass_max_reduction_db,
    )
    if bass_adjustment > 0.01:
        if suggested_gain > 0.01:
            suggested_gain -= bass_adjustment
            action = mark_bass_aware_action(action)
            decision_notes = append_note(
                decision_notes,
                f"bass-aware boost reduced by {bass_adjustment:.2f} dB",
            )
        elif suggested_gain < -0.01:
            suggested_gain -= bass_adjustment
            action = mark_bass_aware_action(action)
            decision_notes = append_note(
                decision_notes,
                f"bass-aware cut increased by {bass_adjustment:.2f} dB",
            )
        if abs(suggested_gain) < 0.01:
            suggested_gain = 0.0

    mp3_preserve_accepts_tp_quirks = (
        source_ext == ".mp3"
        and output_ext == ".mp3"
        and output_format_mode == OUTPUT_FORMAT_PRESERVE
    )
    mp3_encode_lift = 0.0 if mp3_preserve_accepts_tp_quirks else mp3_encode_peak_allowance_db(output_ext)
    if mp3_preserve_accepts_tp_quirks:
        decision_notes = append_note(
            decision_notes,
            "preserved source MP3 output: MP3 encode true-peak quirks are treated as accepted risk; use MP3 sources to AIFF for stricter TP stability",
        )
    true_peak_unreliable = not true_peak_measurements_present
    manual_check_required = False

    if mode == NORMALIZATION_MODE_LIMITER_ASSISTED:
        suggested_gain, limiter_budget_adjustment, action, budget_notes = apply_whole_track_limiter_budget(
            suggested_gain,
            action,
            reference_true_peak_dbtp=true_peak_dbtp,
            peak_ceiling_dbfs=peak_ceiling_dbfs,
            mp3_encode_lift_db=mp3_encode_lift,
            limiter_budget_db=limiter_budget_db,
        )
        if budget_notes:
            decision_notes = append_note(decision_notes, budget_notes)

    manual_check_required = (
        true_peak_unreliable
        and suggested_gain > 0.01
        and not allow_risky_true_peak_boost
    )
    if manual_check_required:
        capped_gain = suggested_gain
        decision_notes = append_note(
            decision_notes,
            f"true peak unreliable; boost capped at 0 dB (was {capped_gain:.2f} dB)",
        )
        suggested_gain = 0.0
        if action.startswith("raise"):
            action = "too quiet but peak-limited"

    projected_loudest = loudest_lufs + suggested_gain
    raw_projected_sample_peak = sample_peak_dbfs + suggested_gain
    raw_projected_true_peak = true_peak_dbtp + suggested_gain
    encode_adjusted_true_peak = raw_projected_true_peak + mp3_encode_lift
    estimated_peak_control = max(0.0, encode_adjusted_true_peak - peak_ceiling_dbfs)
    peak_control_severity = peak_control_severity_label(estimated_peak_control)

    projected_sample_peak = raw_projected_sample_peak
    projected_true_peak = raw_projected_true_peak
    if mode == NORMALIZATION_MODE_LIMITER_ASSISTED and estimated_peak_control > 0.0:
        projected_true_peak = peak_ceiling_dbfs

    uses_limiter = (
        mode == NORMALIZATION_MODE_LIMITER_ASSISTED
        and estimated_peak_control > 0.01
    )

    return TrackDecision(
        target_low_lufs=target_low,
        target_high_lufs=target_high,
        normalization_mode=mode,
        limiter_budget_db=round_or_blank(limiter_budget_db, 2),
        bass_adjustment_db=round_or_blank(bass_adjustment, 2),
        limiter_budget_adjustment_db=round_or_blank(limiter_budget_adjustment, 2),
        raw_gain_db=round_or_blank(raw_gain, 2),
        suggested_gain_db=round_or_blank(suggested_gain, 2),
        projected_loudest_section_lufs=round_or_blank(projected_loudest, 2),
        projected_sample_peak_dbfs=round_or_blank(projected_sample_peak, 2),
        projected_true_peak_dbtp=round_or_blank(projected_true_peak, 2),
        estimated_peak_control_db=round_or_blank(estimated_peak_control, 2),
        peak_control_severity=peak_control_severity,
        true_peak_headroom_db=round_or_blank(peak_ceiling_dbfs - true_peak_dbtp, 2),
        action=action,
        decision_notes=decision_notes,
        true_peak_unreliable="yes" if true_peak_unreliable else "",
        manual_check_required="yes" if manual_check_required else "",
        uses_limiter=uses_limiter,
    )


def apply_track_decision(
    row: TrackRow,
    decision: TrackDecision,
    *,
    limiter_engine: str = DEFAULT_LIMITER_ENGINE,
) -> None:
    """Write decision fields from TrackDecision onto a TrackRow in place."""
    row["target_low_lufs"] = decision.target_low_lufs
    row["target_high_lufs"] = decision.target_high_lufs
    row["normalization_mode"] = decision.normalization_mode
    row["limiter_budget_db"] = decision.limiter_budget_db
    row["bass_adjustment_db"] = decision.bass_adjustment_db
    row["limiter_budget_adjustment_db"] = decision.limiter_budget_adjustment_db
    row["raw_gain_db"] = decision.raw_gain_db
    row["suggested_gain_db"] = decision.suggested_gain_db
    row["projected_loudest_section_lufs"] = decision.projected_loudest_section_lufs
    row["projected_sample_peak_dbfs"] = decision.projected_sample_peak_dbfs
    row["projected_true_peak_dbtp"] = decision.projected_true_peak_dbtp
    row["estimated_peak_control_db"] = decision.estimated_peak_control_db
    row["peak_control_severity"] = decision.peak_control_severity
    row["true_peak_headroom_db"] = decision.true_peak_headroom_db
    row["action"] = decision.action
    row["decision_notes"] = decision.decision_notes
    row["true_peak_unreliable"] = decision.true_peak_unreliable
    row["manual_check_required"] = decision.manual_check_required
    row["processing_engine"] = (
        processing_engine_for_limiter(limiter_engine) if decision.uses_limiter else PROCESSING_ENGINE_CLEAN_GAIN
    )


def decision_from_row(
    row: TrackRow,
    *,
    target_low: float,
    target_high: float,
    max_reduction_db: float,
    peak_ceiling_dbfs: float,
    normalization_mode: str,
    bass_max_reduction_db: float = DEFAULT_BASS_MAX_BOOST_REDUCTION_DB,
    allow_risky_true_peak_boost: bool = False,
) -> TrackDecision:
    """Recompute decision fields for an existing analyzed row."""
    loudest = parse_float_or_default(row["loudest_section_lufs"], 0.0)
    sample_peak = parse_float_or_default(row["sample_peak_dbfs"], 0.0)
    true_peak = parse_float_or_default(row["true_peak_dbtp"], sample_peak)
    section_tp = parse_optional_float(row.get("section_true_peak_dbtp", ""))
    bass_strength = parse_optional_float(row.get("bass_strength_db", ""))
    sub_strength = parse_optional_float(row.get("sub_strength_db", ""))
    source_ext = str(row.get("extension", "")).lower()
    output_ext = output_extension_for_source(source_ext, row.get("output_format_mode", DEFAULT_OUTPUT_FORMAT_MODE))
    output_format_mode = normalize_output_format_mode(row.get("output_format_mode", DEFAULT_OUTPUT_FORMAT_MODE))
    true_peak_unreliable = str(row.get("true_peak_unreliable", "")).strip().lower() == "yes"
    if not true_peak_unreliable and section_tp is None:
        section_tp = parse_optional_float(row.get("true_peak_dbtp", ""))

    return decide_from_measurements(
        loudest_lufs=loudest,
        sample_peak_dbfs=sample_peak,
        true_peak_dbtp=true_peak,
        section_true_peak_dbtp=section_tp,
        bass_strength=bass_strength,
        sub_strength=sub_strength,
        source_ext=source_ext,
        output_ext=output_ext,
        output_format_mode=output_format_mode,
        target_low=target_low,
        target_high=target_high,
        max_reduction_db=max_reduction_db,
        peak_ceiling_dbfs=peak_ceiling_dbfs,
        normalization_mode=normalization_mode,
        bass_max_reduction_db=bass_max_reduction_db,
        allow_risky_true_peak_boost=allow_risky_true_peak_boost,
        true_peak_measurements_present=not true_peak_unreliable,
    )


def analyze_file(
    path: str,
    target_low: float,
    target_high: float,
    loud_window_seconds: float,
    loud_hop_seconds: float,
    max_reduction_db: float,
    peak_ceiling_dbfs: float,
    bass_max_reduction_db: float = DEFAULT_BASS_MAX_BOOST_REDUCTION_DB,
    normalization_mode: str = DEFAULT_NORMALIZATION_MODE,
    output_root: str | None = None,
    source_root: str | None = None,
    source_info: dict[str, object] | None = None,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
    allow_risky_true_peak_boost: bool = False,
    limiter_engine: str = DEFAULT_LIMITER_ENGINE,
) -> TrackRow:
    """Analyze a single audio file and return a populated TrackRow dict.

    If source_info is provided, it is used instead of calling ffprobe again.
    The same dict is suitable for passing to process_audio_with_gain, avoiding
    a redundant ffprobe invocation in the render path.
    """
    p = Path(path)
    ext = p.suffix.lower()
    normalization_mode = normalize_normalization_mode(normalization_mode)
    output_format_mode = normalize_output_format_mode(output_format_mode)
    output_ext = output_extension_for_source(ext, output_format_mode)

    if source_info is not None:
        info = source_info
    else:
        with benchmark_timer("ffprobe"):
            info = ffprobe_audio_info(path)

    original_sample_rate = int(info["sample_rate"])
    channels = int(info["channels"])
    duration = float(info["duration"])
    original_bit_depth = infer_bit_depth(info, ext)

    with benchmark_timer("decode"):
        audio = decode_audio_ffmpeg(path, channels)

    if duration <= 0:
        duration = audio.shape[0] / METER_SAMPLE_RATE

    sample_peak = float(np.max(np.abs(audio)))
    sample_peak_dbfs = dbfs(sample_peak)

    meter = pyln.Meter(METER_SAMPLE_RATE)
    meter_input = audio_for_loudness_meter(audio)

    with benchmark_timer("integrated LUFS"):
        integrated = measure_lufs_input(meter, meter_input)

    with benchmark_timer("loudest-section scan"):
        loudest, section_start, section_end = loudest_section_lufs(
            audio=audio,
            meter=meter,
            window_seconds=loud_window_seconds,
            hop_seconds=loud_hop_seconds,
            meter_input=meter_input,
        )

    warnings = ""
    with benchmark_timer("true peak"):
        section_true_peak_dbtp, whole_true_peak_dbtp, true_peak_note = measure_section_and_whole_true_peak_oversampled(
            path,
            section_start,
            section_end,
            channels=channels,
            sample_rate=original_sample_rate,
        )
    if true_peak_note:
        warnings = append_note(warnings, true_peak_note)

    true_peak_measurements = [
        measurement
        for measurement in (section_true_peak_dbtp, whole_true_peak_dbtp)
        if measurement is not None
    ]
    if true_peak_measurements:
        true_peak_dbtp = max(true_peak_measurements)
    else:
        true_peak_dbtp = sample_peak_dbfs
        warnings = append_note(
            warnings,
            "true peak measurement failed; sample peak used for peak estimate",
        )

    bass_strength: float | None = None
    sub_strength: float | None = None
    with benchmark_timer("bass/sub analysis"):
        try:
            bass_strength = measure_bass_strength_db(
                audio=audio,
                sample_rate=METER_SAMPLE_RATE,
                start_sec=section_start,
                end_sec=section_end,
            )
        except Exception as exc:
            warnings = append_note(warnings, f"bass analysis skipped: {exc}")

        try:
            sub_strength = measure_sub_strength_db(
                audio=audio,
                sample_rate=METER_SAMPLE_RATE,
                start_sec=section_start,
                end_sec=section_end,
            )
        except Exception as exc:
            warnings = append_note(warnings, f"sub analysis skipped: {exc}")

    decision = decide_from_measurements(
        loudest_lufs=loudest,
        sample_peak_dbfs=sample_peak_dbfs,
        true_peak_dbtp=true_peak_dbtp,
        section_true_peak_dbtp=section_true_peak_dbtp,
        bass_strength=bass_strength,
        sub_strength=sub_strength,
        source_ext=ext,
        output_ext=output_ext,
        output_format_mode=output_format_mode,
        target_low=target_low,
        target_high=target_high,
        max_reduction_db=max_reduction_db,
        peak_ceiling_dbfs=peak_ceiling_dbfs,
        normalization_mode=normalization_mode,
        bass_max_reduction_db=bass_max_reduction_db,
        allow_risky_true_peak_boost=allow_risky_true_peak_boost,
        true_peak_measurements_present=bool(true_peak_measurements),
    )
    if decision.manual_check_required == "yes":
        warnings = append_note(
            warnings,
            "true peak measurement failed; manual check recommended before boosting",
        )

    return {
        "path": path,
        "output_path": processed_output_path(
            path,
            output_root=output_root,
            source_root=source_root,
            output_format_mode=output_format_mode,
        ),
        "filename": os.path.basename(path),
        "extension": ext,
        "output_format_mode": output_format_mode,
        "duration_sec": round_or_blank(duration, 1),
        "original_sample_rate": original_sample_rate,
        "output_sample_rate": "",
        "channels": channels,
        "output_channels": "",
        "audio_codec": info.get("codec_name", ""),
        "output_audio_codec": "",
        "audio_sample_fmt": info.get("sample_fmt", ""),
        "output_audio_sample_fmt": "",
        "original_bit_depth": original_bit_depth,
        "output_bit_depth": "",
        "original_bit_rate": info.get("bit_rate", ""),
        "output_bit_rate": "",
        "file_size_mb": round_or_blank(os.path.getsize(path) / 1_048_576, 2),
        "output_file_size_mb": "",
        "integrated_lufs": round_or_blank(integrated, 2),
        "loudest_section_lufs": round_or_blank(loudest, 2),
        "loudest_section_start_sec": round_or_blank(section_start, 1),
        "loudest_section_end_sec": round_or_blank(section_end, 1),
        "sample_peak_dbfs": round_or_blank(sample_peak_dbfs, 2),
        "true_peak_dbtp": round_or_blank(true_peak_dbtp, 2),
        "section_true_peak_dbtp": round_or_blank(section_true_peak_dbtp, 2),
        "peak_headroom_to_0_db": round_or_blank(0.0 - sample_peak_dbfs, 2),
        "true_peak_headroom_db": decision.true_peak_headroom_db,
        "target_low_lufs": decision.target_low_lufs,
        "target_high_lufs": decision.target_high_lufs,
        "normalization_mode": decision.normalization_mode,
        "bass_strength_db": round_or_blank(bass_strength, 2),
        "sub_strength_db": round_or_blank(sub_strength, 2),
        "bass_adjustment_db": decision.bass_adjustment_db,
        "limiter_budget_db": decision.limiter_budget_db,
        "limiter_budget_adjustment_db": decision.limiter_budget_adjustment_db,
        "raw_gain_db": decision.raw_gain_db,
        "suggested_gain_db": decision.suggested_gain_db,
        "projected_loudest_section_lufs": decision.projected_loudest_section_lufs,
        "projected_sample_peak_dbfs": decision.projected_sample_peak_dbfs,
        "projected_true_peak_dbtp": decision.projected_true_peak_dbtp,
        "estimated_peak_control_db": decision.estimated_peak_control_db,
        "peak_control_severity": decision.peak_control_severity,
        "processing_engine": (
            processing_engine_for_limiter(limiter_engine) if decision.uses_limiter else PROCESSING_ENGINE_CLEAN_GAIN
        ),
        "output_integrated_lufs": "",
        "output_same_section_lufs": "",
        "output_sample_peak_dbfs": "",
        "output_true_peak_dbtp": "",
        "actual_same_section_gain_db": "",
        "actual_peak_gain_db": "",
        "actual_true_peak_gain_db": "",
        "audio_verification": "",
        "metadata_verification": "",
        "action": decision.action,
        "processing_status": "",
        "processing_error": "",
        "warnings": warnings,
        "decision_notes": decision.decision_notes,
        "true_peak_unreliable": decision.true_peak_unreliable,
        "manual_check_required": decision.manual_check_required,
    }


# =============================================================================
# LIBRARY STATS
# =============================================================================


@dataclass
class LibraryRowStats:
    """Aggregated statistics from a list of analyzed TrackRows."""

    track_count: int
    loudest_values: list[float]
    output_loudest_values: list[float]
    true_peak_values: list[float]
    output_true_peak_values: list[float]
    gain_values: list[float]
    peak_control_values: list[float]
    bass_adjustment_values: list[float]
    statuses: dict[str, int]
    limiter_severities: dict[str, int]
    over_ceiling_severities: dict[str, int]
    processed: int
    processed_warning: int
    would_process: int
    analyzed_only: int
    skipped: int
    warnings: int
    manual_check_count: int
    true_peak_unreliable_count: int
    metadata_warning_count: int
    output_exists_count: int
    zero_gain_mp3_skipped_count: int
    gain_below_threshold_count: int
    heavy_limiter_control_count: int
    mp3_count: int
    lossless_count: int
    check_rows: list[TrackRow]


def collect_library_row_stats(rows: list[TrackRow]) -> LibraryRowStats:
    """Collect per-library aggregates used by build_summary and optimizer."""
    loudest_values: list[float] = []
    output_loudest_values: list[float] = []
    true_peak_values: list[float] = []
    output_true_peak_values: list[float] = []
    gain_values: list[float] = []
    peak_control_values: list[float] = []
    bass_adjustment_values: list[float] = []

    statuses: dict[str, int] = {}
    limiter_severities: dict[str, int] = {}
    over_ceiling_severities: dict[str, int] = {}

    processed = 0
    processed_warning = 0
    would_process = 0
    analyzed_only = 0
    skipped = 0
    warnings = 0
    manual_check_count = 0
    true_peak_unreliable_count = 0
    metadata_warning_count = 0
    output_exists_count = 0
    zero_gain_mp3_skipped_count = 0
    gain_below_threshold_count = 0
    heavy_limiter_control_count = 0
    mp3_count = 0
    lossless_count = 0

    check_rows: list[TrackRow] = []

    for row in rows:
        loudest = parse_optional_float(row["loudest_section_lufs"])
        output_loudest = parse_optional_float(row["output_same_section_lufs"])
        true_peak = parse_optional_float(row["true_peak_dbtp"])
        output_true_peak = parse_optional_float(row["output_true_peak_dbtp"])
        gain = parse_optional_float(row["suggested_gain_db"])
        peak_control = parse_optional_float(row["estimated_peak_control_db"])
        bass_adjustment = parse_optional_float(row.get("bass_adjustment_db", ""))

        ext = str(row.get("extension", "")).lower()
        if ext == ".mp3":
            mp3_count += 1
        else:
            lossless_count += 1

        status = row["processing_status"] or "unknown"
        statuses[status] = statuses.get(status, 0) + 1

        if loudest is not None:
            loudest_values.append(loudest)
        if output_loudest is not None:
            output_loudest_values.append(output_loudest)
        if true_peak is not None:
            true_peak_values.append(true_peak)
        if output_true_peak is not None:
            output_true_peak_values.append(output_true_peak)
        if gain is not None:
            gain_values.append(gain)
        if peak_control is not None:
            peak_control_values.append(max(0.0, peak_control))
        if bass_adjustment is not None and bass_adjustment > 0.01:
            bass_adjustment_values.append(bass_adjustment)

        severity = row["peak_control_severity"] or PEAK_CONTROL_SEVERITY_NONE
        uses_limiter = is_limiter_processing_engine(row["processing_engine"])
        if peak_control is not None and peak_control > 0.01:
            if uses_limiter:
                limiter_severities[severity] = limiter_severities.get(severity, 0) + 1
            else:
                over_ceiling_severities[severity] = over_ceiling_severities.get(severity, 0) + 1

        if status == "processed":
            processed += 1
        elif status == "processed_warning":
            processed += 1
            processed_warning += 1
            warnings += 1
        elif status == "analyzed_would_process":
            analyzed_only += 1
            would_process += 1
        elif status.startswith("analyzed_"):
            analyzed_only += 1
            skipped += 1
        else:
            skipped += 1

        if status != "processed_warning" and (
            row["audio_verification"] == "warning" or row["metadata_verification"] == "warning"
        ):
            warnings += 1

        if severity == PEAK_CONTROL_SEVERITY_HEAVY and peak_control is not None and peak_control > 0.01 and uses_limiter:
            heavy_limiter_control_count += 1

        if str(row.get("manual_check_required", "")).strip().lower() == "yes" or status.endswith("needs_manual_check"):
            manual_check_count += 1

        if str(row.get("true_peak_unreliable", "")).strip().lower() == "yes":
            true_peak_unreliable_count += 1

        if row["metadata_verification"] == "warning":
            metadata_warning_count += 1

        if status in {"output_exists", "analyzed_output_exists"}:
            output_exists_count += 1

        if status in {"zero_gain_mp3_render_skipped", "analyzed_zero_gain_mp3_render_skipped"}:
            zero_gain_mp3_skipped_count += 1

        if status in {
            "mp3_gain_below_threshold",
            "lossless_gain_below_threshold",
            "analyzed_mp3_gain_below_threshold",
            "analyzed_lossless_gain_below_threshold",
        }:
            gain_below_threshold_count += 1

        if severity in {PEAK_CONTROL_SEVERITY_MODERATE, PEAK_CONTROL_SEVERITY_HEAVY}:
            check_rows.append(row)

    return LibraryRowStats(
        track_count=len(rows),
        loudest_values=loudest_values,
        output_loudest_values=output_loudest_values,
        true_peak_values=true_peak_values,
        output_true_peak_values=output_true_peak_values,
        gain_values=gain_values,
        peak_control_values=peak_control_values,
        bass_adjustment_values=bass_adjustment_values,
        statuses=statuses,
        limiter_severities=limiter_severities,
        over_ceiling_severities=over_ceiling_severities,
        processed=processed,
        processed_warning=processed_warning,
        would_process=would_process,
        analyzed_only=analyzed_only,
        skipped=skipped,
        warnings=warnings,
        manual_check_count=manual_check_count,
        true_peak_unreliable_count=true_peak_unreliable_count,
        metadata_warning_count=metadata_warning_count,
        output_exists_count=output_exists_count,
        zero_gain_mp3_skipped_count=zero_gain_mp3_skipped_count,
        gain_below_threshold_count=gain_below_threshold_count,
        heavy_limiter_control_count=heavy_limiter_control_count,
        mp3_count=mp3_count,
        lossless_count=lossless_count,
        check_rows=check_rows,
    )


def build_summary(
    rows: list[TrackRow],
    errors_this_run: int,
    elapsed_seconds: float,
    mp3_threshold: float | None = None,
    lossless_threshold: float | None = None,
) -> str:
    """Build a compact plain-text summary of the run from all TrackRows."""
    stats = collect_library_row_stats(rows)
    loudest_values = stats.loudest_values
    output_loudest_values = stats.output_loudest_values
    true_peak_values = stats.true_peak_values
    output_true_peak_values = stats.output_true_peak_values
    gain_values = stats.gain_values
    bass_adjustment_values = stats.bass_adjustment_values
    statuses = stats.statuses
    limiter_severities = stats.limiter_severities
    over_ceiling_severities = stats.over_ceiling_severities
    processed = stats.processed
    processed_warning = stats.processed_warning
    would_process = stats.would_process
    skipped = stats.skipped
    warnings = stats.warnings
    manual_check_count = stats.manual_check_count
    true_peak_unreliable_count = stats.true_peak_unreliable_count
    metadata_warning_count = stats.metadata_warning_count
    output_exists_count = stats.output_exists_count
    zero_gain_mp3_skipped_count = stats.zero_gain_mp3_skipped_count
    gain_below_threshold_count = stats.gain_below_threshold_count
    heavy_limiter_control_count = stats.heavy_limiter_control_count
    check_rows = stats.check_rows

    def _range_line(values: list[float], unit: str = "") -> str:
        arr = np.array(values, dtype=np.float64)
        suffix = f" {unit}" if unit else ""
        return f"median {np.median(arr):.2f}{suffix} (range {arr.min():.2f} to {arr.max():.2f})"

    lines: list[str] = []
    lines.append("-" * 78)
    lines.append("SUMMARY")
    lines.append("-" * 78)
    lines.append(f"Files analyzed:   {len(rows)}")
    lines.append(f"Copies created:   {processed}")
    lines.append(f"Would create:     {would_process}")
    lines.append(f"Skipped/left:     {skipped}")
    lines.append(f"Warnings:         {warnings}")
    lines.append(f"Errors:           {errors_this_run}")
    lines.append(f"Elapsed:          {elapsed_seconds / 60:.1f} min")

    if loudest_values:
        lines.append(f"Loudest section:  {_range_line(loudest_values, 'LUFS')}")
    if output_loudest_values:
        lines.append(f"Output section:   {_range_line(output_loudest_values, 'LUFS')}")
    if true_peak_values:
        lines.append(f"True peak:        {_range_line(true_peak_values, 'dBTP')}")
    if output_true_peak_values:
        lines.append(f"Output true peak: {_range_line(output_true_peak_values, 'dBTP')}")

    lines.append("")
    lines.append("Safety details:")
    lines.append(f"  Processed cleanly:             {processed - processed_warning}")
    lines.append(f"  Processed with warning:        {processed_warning}")
    lines.append(f"  Manual check required:         {manual_check_count}")
    lines.append(f"  True-peak unreliable/fallback: {true_peak_unreliable_count}")
    lines.append(f"  Metadata warning:              {metadata_warning_count}")
    lines.append(f"  Output already existed:        {output_exists_count}")
    lines.append(f"  Zero-gain MP3 render skipped:  {zero_gain_mp3_skipped_count}")
    lines.append(f"  Gain below threshold:          {gain_below_threshold_count}")
    lines.append(f"  Heavy limiter-control tracks:  {heavy_limiter_control_count}")
    lines.append(f"  Errors:                        {errors_this_run}")

    if gain_values:
        arr = np.array(gain_values, dtype=np.float64)
        above_threshold = 0
        for row in rows:
            gain = parse_optional_float(row["suggested_gain_db"])
            if gain is None:
                continue
            true_peak_headroom = parse_optional_float(row["true_peak_headroom_db"])
            needs_true_peak_safety = (
                true_peak_headroom is not None
                and true_peak_headroom < -0.01
                and gain < -0.01
            )
            if needs_true_peak_safety or abs(gain) >= min_abs_gain_for_extension(
                row["extension"].lower(),
                mp3_threshold=mp3_threshold,
                lossless_threshold=lossless_threshold,
            ):
                above_threshold += 1
        lines.append(
            f"Suggested gain:   mean {arr.mean():+.2f} dB "
            f"(range {arr.min():+.2f} to {arr.max():+.2f}); process-triggering: {above_threshold}"
        )

    if bass_adjustment_values:
        arr = np.array(bass_adjustment_values, dtype=np.float64)
        lines.append(
            f"Bass-aware trim:  {len(arr)} tracks, mean {arr.mean():.2f} dB "
            f"(max {arr.max():.2f} dB)"
        )

    severity_order = {
        PEAK_CONTROL_SEVERITY_NONE: 0,
        PEAK_CONTROL_SEVERITY_LIGHT: 1,
        PEAK_CONTROL_SEVERITY_MODERATE: 2,
        PEAK_CONTROL_SEVERITY_HEAVY: 3,
    }

    def _severity_parts(counts: dict[str, int]) -> str:
        parts = []
        for severity, count in sorted(counts.items(), key=lambda item: severity_order.get(item[0], 99)):
            parts.append(f"{severity} {count}")
        return ", ".join(parts) if parts else "none"

    if limiter_severities or over_ceiling_severities:
        lines.append("Limiter use/need: " + _severity_parts(limiter_severities))
        lines.append("TP over ceiling:  " + _severity_parts(over_ceiling_severities))

    if statuses:
        display_names = {
            "processed": "processed",
            "processed_warning": "processed with warning",
            "analyzed_would_process": "would process",
            "already_in_target_range": "already in target",
            "analyzed_already_in_target_range": "already in target",
            "mp3_gain_below_threshold": "below MP3 threshold",
            "analyzed_mp3_gain_below_threshold": "below MP3 threshold",
            "zero_gain_mp3_render_skipped": "zero-gain MP3 render skipped",
            "analyzed_zero_gain_mp3_render_skipped": "zero-gain MP3 render skipped",
            "lossless_gain_below_threshold": "below lossless threshold",
            "analyzed_lossless_gain_below_threshold": "below lossless threshold",
            "output_exists": "output exists",
            "analyzed_output_exists": "output exists",
            "missing_original": "missing original",
            "analyzed_missing_original": "missing original",
            "needs_manual_check": "needs manual check",
            "analyzed_needs_manual_check": "needs manual check",
            "manual_check_required": "needs manual check",
        }
        lines.append("")
        lines.append("Processing status:")
        for status, count in sorted(statuses.items()):
            lines.append(f"  {count:>4}  {display_names.get(status, status.replace('_', ' '))}")

    if check_rows:
        def _row_peak_control(row: TrackRow) -> float:
            return parse_float_or_default(row["estimated_peak_control_db"], 0.0)

        lines.append("")
        lines.append("Check by ear:")
        for row in sorted(check_rows, key=_row_peak_control, reverse=True)[:12]:
            start = parse_float_or_default(row["loudest_section_start_sec"], 0.0)
            end = parse_float_or_default(row["loudest_section_end_sec"], 0.0)
            gain = parse_float_or_default(row["suggested_gain_db"], 0.0)
            limit = parse_float_or_default(row["estimated_peak_control_db"], 0.0)
            true_peak = parse_float_or_default(row["true_peak_dbtp"], 0.0)
            if is_limiter_processing_engine(row["processing_engine"]):
                peak_note = f"limit est {limit:5.2f} dB"
            else:
                peak_note = f"TP over ceil {limit:5.2f} dB"
            start_text = f"{int(start // 60)}:{start % 60:04.1f}"
            end_text = f"{int(end // 60)}:{end % 60:04.1f}"
            lines.append(
                f"  {start_text}-{end_text}  "
                f"gain {gain:+5.2f} dB  {peak_note}  TP {true_peak:6.2f} dBTP  "
                f"{row['filename']}"
            )
        if len(check_rows) > 12:
            lines.append(f"  ... {len(check_rows) - 12} more")

    lines.append("-" * 78)
    return "\n".join(lines)

