from __future__ import annotations

import copy
import os
import subprocess
from pathlib import Path

try:
    import numpy as np
    import pyloudnorm as pyln

    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, ID3NoHeaderError
    from mutagen.wave import WAVE
except ImportError as exc:
    raise RuntimeError(
        "Required Python packages were not found.\n\n"
        "Install numpy, pyloudnorm, and mutagen, then try again."
    ) from exc

from analysis import (
    METER_SAMPLE_RATE,
    MP3_ID3_VERSION,
    MP3_OUTPUT_BITRATE,
    POST_VERIFY_PROCESSED_AUDIO,
    PROCESS_OVERWRITE_EXISTING,
    STRICT_VERIFY_LOSSLESS_OUTPUT,
    SUPPORTED_EXTENSIONS,
    TrackRow,
    append_note,
    dbfs,
    decode_audio_ffmpeg,
    ffprobe_audio_info,
    flac_sample_fmt_to_use,
    hidden_subprocess_kwargs,
    infer_bit_depth,
    min_abs_gain_for_extension,
    measure_lufs,
    parse_float_or_default,
    parse_int_or_default,
    round_or_blank,
    wav_codec_to_use,
)


# -------------------------------------------------------------------------
# Metadata Handling
# -------------------------------------------------------------------------


def normalized_vorbis_comments(flac: FLAC) -> dict[str, list[str]]:
    if not flac.tags:
        return {}

    result: dict[str, list[str]] = {}
    for key, values in flac.tags.items():
        result[key.upper()] = sorted(str(v) for v in values)

    return result


def copy_flac_metadata_exact(source_path: str, output_path: str) -> None:
    source = FLAC(source_path)
    output = FLAC(output_path)

    output.clear()

    if source.tags:
        for key, values in source.tags.items():
            output[key] = [str(v) for v in values]

    output.clear_pictures()
    for picture in source.pictures:
        output.add_picture(picture)

    output.save()


def verify_flac_metadata(source_path: str, output_path: str) -> list[str]:
    source = FLAC(source_path)
    output = FLAC(output_path)

    problems: list[str] = []

    source_tags = normalized_vorbis_comments(source)
    output_tags = normalized_vorbis_comments(output)

    for key, values in source_tags.items():
        if key not in output_tags:
            problems.append(f"missing FLAC tag {key}")
        elif output_tags[key] != values:
            problems.append(f"changed FLAC tag {key}")

    if len(source.pictures) != len(output.pictures):
        problems.append(f"picture count changed {len(source.pictures)} -> {len(output.pictures)}")

    return problems


def copy_mp3_metadata_exact(source_path: str, output_path: str) -> None:
    try:
        source_tags = ID3(source_path)
    except ID3NoHeaderError:
        return

    source_tags.save(output_path, v2_version=MP3_ID3_VERSION)


def verify_mp3_metadata(source_path: str, output_path: str) -> list[str]:
    problems: list[str] = []

    try:
        source_tags = ID3(source_path)
    except ID3NoHeaderError:
        return problems

    try:
        output_tags = ID3(output_path)
    except ID3NoHeaderError:
        return ["missing ID3 tag"]

    source_keys = set(source_tags.keys())
    output_keys = set(output_tags.keys())

    missing = sorted(source_keys - output_keys)
    for key in missing[:20]:
        problems.append(f"missing ID3 frame {key}")

    if len(missing) > 20:
        problems.append(f"{len(missing) - 20} more ID3 frames missing")

    return problems


def copy_wav_metadata_best_effort(source_path: str, output_path: str) -> None:
    try:
        source = WAVE(source_path)
    except Exception:
        return

    if not source.tags:
        return

    output = WAVE(output_path)
    if output.tags is None:
        output.add_tags()

    output.tags.clear()
    for frame in source.tags.values():
        output.tags.add(copy.deepcopy(frame))

    output.save()


def verify_wav_metadata(source_path: str, output_path: str) -> list[str]:
    problems: list[str] = []

    try:
        source = WAVE(source_path)
    except Exception:
        return problems

    if not source.tags:
        return problems

    try:
        output = WAVE(output_path)
    except Exception as exc:
        return [f"could not read output WAV tags: {exc}"]

    if output.tags is None:
        return ["missing WAV ID3 tag"]

    source_keys = set(source.tags.keys())
    output_keys = set(output.tags.keys())

    missing = sorted(source_keys - output_keys)
    for key in missing[:20]:
        problems.append(f"missing WAV ID3 frame {key}")

    if len(missing) > 20:
        problems.append(f"{len(missing) - 20} more WAV ID3 frames missing")

    return problems


def copy_aiff_metadata_best_effort(source_path: str, output_path: str) -> None:
    try:
        source_tags = ID3(source_path)
    except ID3NoHeaderError:
        return

    source_tags.save(output_path, v2_version=MP3_ID3_VERSION)


def verify_aiff_metadata(source_path: str, output_path: str) -> list[str]:
    problems: list[str] = []

    try:
        source_tags = ID3(source_path)
    except ID3NoHeaderError:
        return problems

    try:
        output_tags = ID3(output_path)
    except ID3NoHeaderError:
        return ["missing ID3 tag"]

    source_keys = set(source_tags.keys())
    output_keys = set(output_tags.keys())

    missing = sorted(source_keys - output_keys)
    for key in missing[:20]:
        problems.append(f"missing AIFF ID3 frame {key}")

    if len(missing) > 20:
        problems.append(f"{len(missing) - 20} more AIFF ID3 frames missing")

    return problems


def copy_metadata_exact(source_path: str, output_path: str) -> None:
    ext = Path(source_path).suffix.lower()

    if ext == ".flac":
        copy_flac_metadata_exact(source_path, output_path)
    elif ext == ".mp3":
        copy_mp3_metadata_exact(source_path, output_path)
    elif ext == ".wav":
        copy_wav_metadata_best_effort(source_path, output_path)
    elif ext == ".aiff":
        copy_aiff_metadata_best_effort(source_path, output_path)


def verify_metadata(source_path: str, output_path: str) -> tuple[str, str]:
    ext = Path(source_path).suffix.lower()

    try:
        if ext == ".flac":
            problems = verify_flac_metadata(source_path, output_path)
        elif ext == ".mp3":
            problems = verify_mp3_metadata(source_path, output_path)
        elif ext == ".wav":
            problems = verify_wav_metadata(source_path, output_path)
        elif ext == ".aiff":
            problems = verify_aiff_metadata(source_path, output_path)
        else:
            problems = []
    except Exception as exc:
        problems = [f"metadata verification failed: {exc}"]

    if problems:
        return "warning", "; ".join(problems)

    return "ok", ""


# -------------------------------------------------------------------------
# Gain Processing
# -------------------------------------------------------------------------


def gain_to_volume_filter(gain_db: float) -> str:
    linear = 10.0 ** (gain_db / 20.0)
    return f"volume={linear:.12f}:precision=double"


def encoder_args_for_output(ext: str, source_info: dict[str, object]) -> list[str]:
    ext = ext.lower()

    if ext == ".flac":
        return [
            "-c:a",
            "flac",
            "-sample_fmt",
            flac_sample_fmt_to_use(source_info),
            "-compression_level",
            "8",
        ]

    if ext == ".mp3":
        return [
            "-c:a",
            "libmp3lame",
            "-b:a",
            MP3_OUTPUT_BITRATE,
            "-id3v2_version",
            str(MP3_ID3_VERSION),
            "-write_id3v1",
            "1",
        ]

    if ext == ".wav":
        return [
            "-c:a",
            wav_codec_to_use(source_info),
        ]

    if ext == ".aiff":
        return [
            "-c:a",
            "pcm_s16be",
        ]

    raise RuntimeError(f"processing not supported for {ext}")


def verify_lossless_output(
    input_path: str,
    output_path: str,
    input_info: dict[str, object],
    output_info: dict[str, object],
) -> None:
    ext = Path(input_path).suffix.lower()

    if ext not in {".flac", ".wav", ".aiff"}:
        return

    problems: list[str] = []

    input_sr = parse_int_or_default(input_info.get("sample_rate"), 0)
    output_sr = parse_int_or_default(output_info.get("sample_rate"), 0)

    input_channels = parse_int_or_default(input_info.get("channels"), 0)
    output_channels = parse_int_or_default(output_info.get("channels"), 0)

    input_depth = infer_bit_depth(input_info, ext)
    output_depth = infer_bit_depth(output_info, ext)

    input_depth_int = parse_int_or_default(input_depth, 0)
    output_depth_int = parse_int_or_default(output_depth, 0)

    output_codec = str(output_info.get("codec_name") or "").lower()

    if input_sr and output_sr and input_sr != output_sr:
        problems.append(f"sample rate changed {input_sr} -> {output_sr}")

    if input_channels and output_channels and input_channels != output_channels:
        problems.append(f"channel count changed {input_channels} -> {output_channels}")

    if input_depth_int and output_depth_int and input_depth_int != output_depth_int:
        problems.append(f"bit depth changed {input_depth_int} -> {output_depth_int}")

    if ext == ".flac" and output_codec != "flac":
        problems.append(f"FLAC output codec is {output_codec}")

    if ext == ".wav":
        expected_wav_codec = wav_codec_to_use(input_info)
        if output_codec != expected_wav_codec:
            problems.append(f"WAV codec changed {expected_wav_codec} -> {output_codec}")

    if problems:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass

        raise RuntimeError("lossless output verification failed: " + "; ".join(problems))


def preserve_source_mtime(source_path: str, output_path: str) -> None:
    try:
        stat = os.stat(source_path)
        os.utime(output_path, (stat.st_atime, stat.st_mtime))
    except Exception:
        pass


def apply_gain_ffmpeg(
    input_path: str,
    output_path: str,
    gain_db: float,
    source_info: dict[str, object],
    preserve_mtime: bool = False,
) -> dict[str, object]:
    ext = Path(input_path).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise RuntimeError(f"processing not supported for {ext}")
    if os.path.exists(output_path) and not PROCESS_OVERWRITE_EXISTING:
        raise RuntimeError("output already exists")

    output_parent = os.path.dirname(os.path.abspath(output_path))
    if output_parent:
        os.makedirs(output_parent, exist_ok=True)

    volume_filter = gain_to_volume_filter(gain_db)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        input_path,
        "-map",
        "0:a:0",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-af",
        volume_filter,
        "-y" if PROCESS_OVERWRITE_EXISTING else "-n",
    ]

    cmd.extend(encoder_args_for_output(ext, source_info))
    cmd.append(output_path)

    result = subprocess.run(cmd, **hidden_subprocess_kwargs())
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg volume filter failed (return code {result.returncode})")

    copy_metadata_exact(input_path, output_path)

    if preserve_mtime:
        preserve_source_mtime(input_path, output_path)

    output_info = ffprobe_audio_info(output_path)

    if STRICT_VERIFY_LOSSLESS_OUTPUT:
        verify_lossless_output(
            input_path=input_path,
            output_path=output_path,
            input_info=source_info,
            output_info=output_info,
        )

    return output_info


def process_audio_with_gain(
    input_path: str,
    output_path: str,
    gain_db: float,
    source_info: dict[str, object],
    preserve_mtime: bool = False,
) -> dict[str, object]:
    """Apply the requested gain with FFmpeg and return output stream metadata."""
    return apply_gain_ffmpeg(input_path, output_path, gain_db, source_info, preserve_mtime)


def should_process_row(
    row: TrackRow,
    lossless_threshold: float,
    mp3_threshold: float,
) -> tuple[bool, str]:
    path = row["path"]
    ext = row["extension"].lower()
    gain = parse_float_or_default(row["suggested_gain_db"], 0.0)
    output_path = row["output_path"]

    if ext not in SUPPORTED_EXTENSIONS:
        return False, "unsupported_format"

    min_gain = min_abs_gain_for_extension(ext, lossless_threshold, mp3_threshold)

    if abs(gain) < min_gain:
        if ext == ".mp3":
            return False, "mp3_gain_below_threshold"
        return False, "lossless_gain_below_threshold"

    if not os.path.exists(path):
        return False, "missing_original"

    if os.path.exists(output_path) and not PROCESS_OVERWRITE_EXISTING:
        return False, "output_exists"

    return True, "will_process"


def verify_processed_audio_fast(
    row: TrackRow,
    output_path: str,
) -> tuple[str, str]:
    """Compare the rendered file against the analysis expectations.

    Updates the row with measured output loudness and peak values, then
    returns a verification status plus any warning text.
    """
    if not POST_VERIFY_PROCESSED_AUDIO:
        return "skipped", ""

    output_info = ffprobe_audio_info(output_path)
    channels = int(output_info["channels"])
    output_audio = decode_audio_ffmpeg(output_path, channels)

    output_peak = float(np.max(np.abs(output_audio)))
    output_peak_dbfs = dbfs(output_peak)

    meter = pyln.Meter(METER_SAMPLE_RATE)
    output_integrated = measure_lufs(meter, output_audio)

    start_sec = parse_float_or_default(row["loudest_section_start_sec"], 0.0)
    end_sec = parse_float_or_default(row["loudest_section_end_sec"], 0.0)

    start_sample = max(0, int(round(start_sec * METER_SAMPLE_RATE)))
    end_sample = max(start_sample + 1, int(round(end_sec * METER_SAMPLE_RATE)))
    end_sample = min(end_sample, output_audio.shape[0])

    same_section = output_audio[start_sample:end_sample]
    output_same_section_lufs = measure_lufs(meter, same_section)

    row["output_integrated_lufs"] = round_or_blank(output_integrated, 2)
    row["output_same_section_lufs"] = round_or_blank(output_same_section_lufs, 2)
    row["output_sample_peak_dbfs"] = round_or_blank(output_peak_dbfs, 2)

    input_loudest = parse_float_or_default(row["loudest_section_lufs"], 0.0)
    input_peak = parse_float_or_default(row["sample_peak_dbfs"], 0.0)

    suggested_gain = parse_float_or_default(row["suggested_gain_db"], 0.0)
    actual_same_section_gain = output_same_section_lufs - input_loudest
    actual_peak_gain = output_peak_dbfs - input_peak

    row["actual_same_section_gain_db"] = round_or_blank(actual_same_section_gain, 2)
    row["actual_peak_gain_db"] = round_or_blank(actual_peak_gain, 2)

    notes: list[str] = []

    if abs(actual_same_section_gain - suggested_gain) > 0.1:
        notes.append(
            f"actual gain {actual_same_section_gain:+.2f} dB differs from suggested {suggested_gain:+.2f} dB"
        )

    if abs(output_peak_dbfs - (input_peak + suggested_gain)) > 0.5:
        notes.append(
            f"output peak {output_peak_dbfs:.1f} dBFS differs from projection {input_peak + suggested_gain:.1f} dBFS"
        )

    if output_peak_dbfs > -0.01:
        notes.append("output peak is above -0.01 dBFS (possible clipping)")

    if notes:
        return "warning", "; ".join(notes)

    return "ok", ""