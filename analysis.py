from __future__ import annotations

import json
import math
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from typing import Literal, TypeAlias, TypedDict

try:
    import numpy as np
    import pyloudnorm as pyln
except ImportError as exc:
    raise RuntimeError(
        "Required Python packages were not found.\n\n"
        "Install numpy and pyloudnorm, then try again."
    ) from exc


# -------------------------------------------------------------------------
# Configuration Constants
# -------------------------------------------------------------------------

APP_TITLE = "DropGain"

METER_SAMPLE_RATE = 48_000

PROCESSED_SUFFIX = "_DG"

SUPPORTED_EXTENSIONS = {".flac", ".mp3", ".wav", ".aiff"}

SKIP_ALREADY_PROCESSED_FILES_IN_SCAN = True
PROCESS_OVERWRITE_EXISTING = False

DEFAULT_LOSSLESS_MIN_ABS_GAIN_DB = 0.15
DEFAULT_MP3_MIN_ABS_GAIN_DB = 1.00

MAX_WORKER_THREADS = min(multiprocessing.cpu_count(), 4)
DEFAULT_WORKER_THREADS = 2
MIN_WORKER_THREADS = 1

DEFAULT_LOUD_SECTION_WINDOW_SECONDS = 30.0
DEFAULT_LOUD_SECTION_HOP_SECONDS = 10.0

MIN_LOUD_SECTION_WINDOW_SECONDS = 10.0
MAX_LOUD_SECTION_WINDOW_SECONDS = 120.0

MIN_LOUD_SECTION_HOP_SECONDS = 5.0
MAX_LOUD_SECTION_HOP_SECONDS = 60.0

DEFAULT_TARGET_LOW_LUFS = -7.0
DEFAULT_TARGET_HIGH_LUFS = -6.0

DEFAULT_MAX_BOOST_DB = 4.5

DEFAULT_PEAK_CEILING_DBFS = -1.0
DEFAULT_BOOST_PEAK_CEILING_DBFS = DEFAULT_PEAK_CEILING_DBFS

# Expected bass-vs-LUFS offset for a balanced drop; sensitivity converts deviation into a LUFS adjustment.
DEFAULT_BASS_BASE_RATIO = 4.0
DEFAULT_BASS_NOD_SENSITIVITY = 0.25

MP3_OUTPUT_BITRATE = "320k"
MP3_ID3_VERSION = 3

WAV_CODECS_TO_KEEP = {
    "pcm_u8",
    "pcm_s8",
    "pcm_s16le",
    "pcm_s24le",
    "pcm_s32le",
    "pcm_f32le",
    "pcm_f64le",
}

STRICT_VERIFY_LOSSLESS_OUTPUT = True
POST_VERIFY_PROCESSED_AUDIO = True

TrackRowKey: TypeAlias = Literal[
    "path",
    "output_path",
    "filename",
    "extension",
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
    "drop_bass_dbfs",
    "sample_peak_dbfs",
    "peak_headroom_to_0_db",
    "target_low_lufs",
    "target_high_lufs",
    "normalization_mode",
    "raw_gain_db",
    "suggested_gain_db",
    "projected_loudest_section_lufs",
    "projected_sample_peak_dbfs",
    "estimated_peak_control_db",
    "peak_control_severity",
    "processing_engine",
    "output_integrated_lufs",
    "output_same_section_lufs",
    "output_sample_peak_dbfs",
    "actual_same_section_gain_db",
    "actual_peak_gain_db",
    "audio_verification",
    "metadata_verification",
    "action",
    "processing_status",
    "processing_error",
    "notes",
]


class TrackRow(TypedDict):
    path: str
    output_path: str
    filename: str
    extension: str
    duration_sec: float | str
    original_sample_rate: int
    output_sample_rate: int | str
    channels: int
    output_channels: int | str
    audio_codec: str
    output_audio_codec: str
    audio_sample_fmt: str
    output_audio_sample_fmt: str
    original_bit_depth: int | str
    output_bit_depth: int | str
    original_bit_rate: str
    output_bit_rate: str
    file_size_mb: float | str
    output_file_size_mb: float | str
    integrated_lufs: float | str
    loudest_section_lufs: float | str
    loudest_section_start_sec: float | str
    loudest_section_end_sec: float | str
    drop_bass_dbfs: float | str
    sample_peak_dbfs: float | str
    peak_headroom_to_0_db: float | str
    target_low_lufs: float
    target_high_lufs: float
    normalization_mode: str
    raw_gain_db: float | str
    suggested_gain_db: float | str
    projected_loudest_section_lufs: float | str
    projected_sample_peak_dbfs: float | str
    estimated_peak_control_db: float | str
    peak_control_severity: str
    processing_engine: str
    output_integrated_lufs: float | str
    output_same_section_lufs: float | str
    output_sample_peak_dbfs: float | str
    actual_same_section_gain_db: float | str
    actual_peak_gain_db: float | str
    audio_verification: str
    metadata_verification: str
    action: str
    processing_status: str
    processing_error: str
    notes: str


# -------------------------------------------------------------------------
# Path Helpers
# -------------------------------------------------------------------------


def script_folder() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def processed_output_path(
    input_path: str,
    output_root: str | None = None,
    source_root: str | None = None,
) -> str:
    p = Path(input_path)
    output_name = f"{p.stem}{PROCESSED_SUFFIX}{p.suffix}"

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
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def check_ffmpeg_available() -> None:
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
    return os.path.normcase(os.path.abspath(path))


def dbfs(value: float) -> float:
    if value <= 0.0 or not math.isfinite(value):
        return -999.0
    return 20.0 * math.log10(value)


def round_or_blank(value: float | None, digits: int = 2) -> float | str:
    if value is None:
        return ""
    if not math.isfinite(float(value)):
        return ""
    return round(float(value), digits)


def parse_optional_float(value: object) -> float | None:
    try:
        f = float(value)
    except Exception:
        return None

    if not math.isfinite(f):
        return None

    return f


def parse_float_or_default(value: object, default: float = 0.0) -> float:
    parsed = parse_optional_float(value)
    if parsed is None:
        return default
    return parsed


def parse_int_or_default(value: object, default: int = 0) -> int:
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
    old = str(existing or "").strip()
    if not old:
        return note
    return old + "; " + note


def min_abs_gain_for_extension(ext: str, lossless_threshold: float, mp3_threshold: float) -> float:
    ext = ext.lower()
    if ext == ".mp3":
        return mp3_threshold
    if ext in {".flac", ".wav", ".aiff"}:
        return lossless_threshold
    return lossless_threshold


def find_audio_files(root_dir: str) -> list[str]:
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

            if SKIP_ALREADY_PROCESSED_FILES_IN_SCAN and stem.endswith(PROCESSED_SUFFIX):
                continue

            paths.append(os.path.join(dirpath, filename))

    return sorted(paths, key=normalized_path)


# -------------------------------------------------------------------------
# Audio Analysis
# -------------------------------------------------------------------------


def ffprobe_audio_info(path: str) -> dict[str, object]:
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
        check=True,
        **hidden_subprocess_kwargs(),
    )

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
        if ext == ".flac":
            return 24
        return 32

    if sample_fmt.startswith("flt"):
        return 32

    if sample_fmt.startswith("dbl"):
        return 64

    return ""


def flac_sample_fmt_to_use(source_info: dict[str, object]) -> str:
    bit_depth = infer_bit_depth(source_info, ".flac")
    bit_depth_int = parse_int_or_default(bit_depth, 0)

    if bit_depth_int <= 16:
        return "s16"

    return "s32"


def wav_codec_to_use(source_info: dict[str, object]) -> str:
    codec = str(source_info.get("codec_name") or "").lower().strip()

    if codec in WAV_CODECS_TO_KEEP:
        return codec

    raise RuntimeError(f"unsupported WAV codec for safe processing: {codec or 'unknown'}")


def decode_audio_ffmpeg(path: str, channels: int) -> np.ndarray:
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
    if audio.ndim == 1:
        return audio

    if audio.shape[1] == 1:
        return audio[:, 0]

    return audio


def measure_lufs(meter: pyln.Meter, audio: np.ndarray) -> float:
    measured = float(meter.integrated_loudness(audio_for_loudness_meter(audio)))

    if not math.isfinite(measured):
        raise RuntimeError("LUFS measurement was not finite")

    return measured


def measure_bass_level(
    audio: np.ndarray, sample_rate: int, cutoff_hz: float = 150.0
) -> float:
    """Return the RMS level in dBFS for content below ``cutoff_hz``."""
    if audio.size == 0:
        return -999.0

    if audio.ndim == 2:
        mono = np.mean(audio, axis=1)
    else:
        mono = audio

    n = len(mono)
    freq_resolution = sample_rate / n
    freq_bin_limit = int(cutoff_hz / freq_resolution) + 1

    window = np.hanning(n)
    windowed = mono * window

    fft = np.fft.rfft(windowed)
    magnitude = np.abs(fft)
    power_spec = magnitude ** 2

    low_power_windowed = power_spec[0]
    if freq_bin_limit > 1:
        low_power_windowed += 2.0 * np.sum(power_spec[1:freq_bin_limit])

    low_power_windowed /= n ** 2
    window_energy_gain = np.mean(window ** 2)
    low_power_corrected = low_power_windowed / window_energy_gain

    low_rms = np.sqrt(low_power_corrected)

    if low_rms <= 0.0:
        return -120.0

    return 20.0 * math.log10(low_rms)


def loudest_section_lufs(
    audio: np.ndarray,
    meter: pyln.Meter,
    window_seconds: float,
    hop_seconds: float,
) -> tuple[float, float, float]:
    """Return the loudest measured window and its start/end times in seconds."""
    sample_count = audio.shape[0]

    window_samples = int(round(window_seconds * METER_SAMPLE_RATE))
    hop_samples = int(round(hop_seconds * METER_SAMPLE_RATE))

    window_samples = max(window_samples, int(1.0 * METER_SAMPLE_RATE))
    hop_samples = max(hop_samples, int(1.0 * METER_SAMPLE_RATE))

    if sample_count <= window_samples:
        lufs = measure_lufs(meter, audio)
        return lufs, 0.0, sample_count / METER_SAMPLE_RATE

    starts = list(range(0, sample_count - window_samples + 1, hop_samples))

    final_start = sample_count - window_samples
    if starts[-1] != final_start:
        starts.append(final_start)

    best_lufs: float | None = None
    best_start = 0

    for start in starts:
        end = start + window_samples
        segment = audio[start:end]

        try:
            value = measure_lufs(meter, segment)
        except Exception:
            continue

        if best_lufs is None or value > best_lufs:
            best_lufs = value
            best_start = start

    if best_lufs is None:
        lufs = measure_lufs(meter, audio)
        return lufs, 0.0, sample_count / METER_SAMPLE_RATE

    start_sec = best_start / METER_SAMPLE_RATE
    end_sec = (best_start + window_samples) / METER_SAMPLE_RATE

    return best_lufs, start_sec, end_sec


def calculate_gain_suggestion(
    loudest_lufs: float,
    sample_peak_dbfs: float,
    drop_bass_dbfs: float,
    target_low: float,
    target_high: float,
    max_boost_db: float,
    peak_ceiling_dbfs: float,
    bass_base_ratio: float,
    bass_nod_sensitivity: float,
) -> tuple[float, float, str, str]:
    """Compute the recommended gain change for the analyzed drop section.

    Balances the target LUFS window against peak headroom and the optional
    bass-based LUFS adjustment, then returns the raw gain, applied gain,
    action label, and any decision notes.
    """
    notes: list[str] = []

    actual_bass_ratio = drop_bass_dbfs - loudest_lufs
    bass_deviation = actual_bass_ratio - bass_base_ratio
    lufs_nod = bass_deviation * bass_nod_sensitivity
    effective_lufs = loudest_lufs + lufs_nod

    if abs(lufs_nod) >= 0.1:
        dir_str = "heavy-bass" if lufs_nod > 0 else "lean-bass"
        notes.append(f"BassNod:{lufs_nod:+.1f} ({dir_str})")

    if effective_lufs < target_low:
        raw_gain = target_low - effective_lufs
        max_positive_boost = max(0.0, max_boost_db)

        peak_headroom = peak_ceiling_dbfs - sample_peak_dbfs
        peak_safe_boost = max(0.0, peak_headroom)

        suggested_gain = min(raw_gain, max_positive_boost, peak_safe_boost)
        action = "Raise"

        if suggested_gain < raw_gain - 0.01:
            if suggested_gain >= peak_safe_boost - 0.01:
                notes.append(f"Cap:PeakHeadroom({peak_ceiling_dbfs:.1f}dBFS)")
            else:
                notes.append("Cap:MaxBoost")

        if suggested_gain <= 0.01:
            action = "Skip (At Peak)" if peak_safe_boost <= 0.01 else "Skip"

    elif effective_lufs > target_high:
        raw_gain = target_high - effective_lufs
        suggested_gain = raw_gain
        action = "Lower"
    else:
        raw_gain = 0.0
        suggested_gain = 0.0
        action = "Keep"

    if abs(suggested_gain) < 0.01:
        suggested_gain = 0.0

    return raw_gain, suggested_gain, action, " | ".join(notes)


def analyze_file(
    path: str,
    target_low: float,
    target_high: float,
    loud_window_seconds: float,
    loud_hop_seconds: float,
    max_boost_db: float,
    peak_ceiling_dbfs: float,
    bass_base_ratio: float,
    bass_nod_sensitivity: float,
    output_root: str | None = None,
    source_root: str | None = None,
) -> TrackRow:
    """Analyze one audio file and build the corresponding report row."""
    p = Path(path)
    ext = p.suffix.lower()

    info = ffprobe_audio_info(path)

    original_sample_rate = int(info["sample_rate"])
    channels = int(info["channels"])
    duration = float(info["duration"])
    original_bit_depth = infer_bit_depth(info, ext)

    audio = decode_audio_ffmpeg(path, channels)

    if duration <= 0:
        duration = audio.shape[0] / METER_SAMPLE_RATE

    sample_peak = float(np.max(np.abs(audio)))
    sample_peak_dbfs = dbfs(sample_peak)

    meter = pyln.Meter(METER_SAMPLE_RATE)

    integrated = measure_lufs(meter, audio)

    loudest, section_start, section_end = loudest_section_lufs(
        audio=audio,
        meter=meter,
        window_seconds=loud_window_seconds,
        hop_seconds=loud_hop_seconds,
    )

    start_sample = int(round(section_start * METER_SAMPLE_RATE))
    end_sample = int(round(section_end * METER_SAMPLE_RATE))
    loud_section = audio[start_sample:end_sample]

    bass_level = measure_bass_level(loud_section, METER_SAMPLE_RATE, cutoff_hz=150.0)

    raw_gain, suggested_gain, action, notes = calculate_gain_suggestion(
        loudest_lufs=loudest,
        sample_peak_dbfs=sample_peak_dbfs,
        drop_bass_dbfs=bass_level,
        target_low=target_low,
        target_high=target_high,
        max_boost_db=max_boost_db,
        peak_ceiling_dbfs=peak_ceiling_dbfs,
        bass_base_ratio=bass_base_ratio,
        bass_nod_sensitivity=bass_nod_sensitivity,
    )

    projected_loudest = loudest + suggested_gain
    projected_peak = sample_peak_dbfs + suggested_gain
    estimated_peak_control = 0.0
    peak_control_severity = "none"

    return {
        "path": path,
        "output_path": processed_output_path(
            path,
            output_root=output_root,
            source_root=source_root,
        ),
        "filename": os.path.basename(path),
        "extension": ext,
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
        "drop_bass_dbfs": round_or_blank(bass_level, 2),
        "sample_peak_dbfs": round_or_blank(sample_peak_dbfs, 2),
        "peak_headroom_to_0_db": round_or_blank(0.0 - sample_peak_dbfs, 2),
        "target_low_lufs": target_low,
        "target_high_lufs": target_high,
        "normalization_mode": "Bass-aware gain",
        "raw_gain_db": round_or_blank(raw_gain, 2),
        "suggested_gain_db": round_or_blank(suggested_gain, 2),
        "projected_loudest_section_lufs": round_or_blank(projected_loudest, 2),
        "projected_sample_peak_dbfs": round_or_blank(projected_peak, 2),
        "estimated_peak_control_db": round_or_blank(estimated_peak_control, 2),
        "peak_control_severity": peak_control_severity,
        "processing_engine": "FFmpeg volume filter (pure gain)",
        "output_integrated_lufs": "",
        "output_same_section_lufs": "",
        "output_sample_peak_dbfs": "",
        "actual_same_section_gain_db": "",
        "actual_peak_gain_db": "",
        "audio_verification": "",
        "metadata_verification": "",
        "action": action,
        "processing_status": "",
        "processing_error": "",
        "notes": notes,
    }


# -------------------------------------------------------------------------
# Summary Reporting
# -------------------------------------------------------------------------


def build_summary(rows: list[TrackRow]) -> str:
    if not rows:
        return "No track data available to summarize."

    total_tracks = len(rows)
    actions: dict[str, int] = {}
    statuses: dict[str, int] = {}
    
    gains: list[float] = []

    for r in rows:
        act = r.get("action", "Unknown")
        actions[act] = actions.get(act, 0) + 1

        status = r.get("processing_status", "Unknown")
        statuses[status] = statuses.get(status, 0) + 1

        gain_val = parse_float_or_default(r.get("suggested_gain_db"), 0.0)
        if act in ("Raise", "Lower"):
            gains.append(gain_val)

    lines = [
        "==============================================================================",
        f" EXECUTION SUMMARY ({total_tracks} Tracks Total)",
        "==============================================================================",
    ]

    lines.append("Decisions:")
    for action, count in sorted(actions.items()):
        lines.append(f"  {action:<16} : {count} tracks")
    lines.append("")

    if gains:
        arr = np.array(gains)
        lines.append("Applied Gain Stats (Linear Changes Only):")
        lines.append(f"  Min Gain       : {np.min(arr):+.2f} dB")
        lines.append(f"  Max Gain       : {np.max(arr):+.2f} dB")
        lines.append(f"  Mean Gain      : {np.mean(arr):+.2f} dB")
        lines.append(f"  Median Gain    : {np.median(arr):+.2f} dB")
        lines.append("")

    lines.append("File Processing Status:")
    for status, count in sorted(statuses.items()):
        lines.append(f"  {status:<16} : {count} files")
        
    lines.append("------------------------------------------------------------------------------")
    lines.append(f"Config: Target Window={DEFAULT_LOUD_SECTION_WINDOW_SECONDS}s | Suffix={PROCESSED_SUFFIX}")
    lines.append("==============================================================================")

    return "\n".join(lines)
