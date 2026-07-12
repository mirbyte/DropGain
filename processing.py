"""
DropGain processing, metadata, and output verification.

This module contains the rendering path, metadata copy/verify helpers, and
post-render validation. Shared configuration and analysis helpers live in
analysis.py.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

logger = logging.getLogger("dropgain")

try:
    import numpy as np
    import pyloudnorm as pyln

    from mutagen import File as MutagenFile
    from mutagen.aiff import AIFF
    from mutagen.flac import FLAC
    from mutagen.id3 import (
        APIC,
        COMM,
        ID3,
        ID3NoHeaderError,
        TALB,
        TBPM,
        TCOM,
        TCON,
        TDRC,
        TIT2,
        TKEY,
        TPE1,
        TPE2,
        TRCK,
    )
    from mutagen.wave import WAVE
except ImportError as exc:
    raise RuntimeError(
        "Required Python packages were not found.\n\n"
        "Install numpy, pyloudnorm, and mutagen, then try again."
    ) from exc

from analysis import (
    DEFAULT_BASS_PENALTY_FULL_DB,
    DEFAULT_BASS_PENALTY_START_DB,
    DEFAULT_OUTPUT_FORMAT_MODE,
    DEFAULT_SUB_PENALTY_FULL_DB,
    DEFAULT_SUB_PENALTY_START_DB,
    EFFECTIVE_ZERO_GAIN_DB,
    OUTPUT_FORMAT_ALL_TO_AIFF,
    OUTPUT_FORMAT_PRESERVE,
    METER_SAMPLE_RATE,
    MIN_OUTPUT_FILE_BYTES,
    MP3_ENCODE_TRUE_PEAK_LIFT_DB,
    NORMALIZATION_MODE_LIMITER_ASSISTED,
    DEFAULT_LIMITER_ENGINE,
    LIMITER_ENGINE_LOUDMAX,
    LOUDMAX_DEFAULT_PLUGIN_PATH,
    LOUDMAX_LIMITER_CALIBRATION_DB,
    LOUDMAX_PROCESS_BUFFER_SIZE,
    MP3_ID3_VERSION,
    MP3_OUTPUT_BITRATE,
    PIONEER_COMPATIBLE_AIFF_CODECS,
    PIONEER_COMPATIBLE_AIFF_SAMPLE_RATES,
    POST_VERIFY_LUFS_TOLERANCE,
    POST_VERIFY_PEAK_TOLERANCE_DB,
    POST_VERIFY_PROCESSED_AUDIO,
    PROCESS_OVERWRITE_EXISTING,
    PROCESSING_ENGINE_CLEAN_GAIN,
    PROCESSING_ENGINE_LOUDMAX,
    PROCESSING_ENGINE_PROL2,
    PROL2_DEFAULT_OUTPUT_LEVEL_DBFS,
    PROL2_DEFAULT_OVERSAMPLING,
    PROL2_DEFAULT_PLUGIN_PATH,
    PROL2_DEFAULT_STYLE,
    PROL2_DEFAULT_TRUE_PEAK,
    PROL2_PROCESS_BUFFER_SIZE,
    STRICT_VERIFY_LOSSLESS_OUTPUT,
    SUPPORTED_EXTENSIONS,
    TrackRow,
    aiff_codec_to_use,
    audio_for_loudness_meter,
    benchmark_timer,
    append_note,
    dbfs,
    decode_audio_ffmpeg,
    ffprobe_audio_info,
    flac_sample_fmt_to_use,
    hidden_subprocess_kwargs,
    infer_bit_depth,
    loudest_section_lufs,
    min_abs_gain_for_extension,
    measure_lufs,
    measure_lufs_input,
    normalize_limiter_engine,
    normalize_output_format_mode,
    measure_section_and_whole_true_peak_oversampled,
    parse_float_or_default,
    parse_int_or_default,
    parse_optional_float,
    pioneer_compatible_aiff_codec,
    pioneer_compatible_aiff_sample_rate,
    processing_engine_for_limiter,
    is_aiff_output,
    requires_pioneer_compatible_aiff,
    round_or_blank,
    wav_codec_to_use,
)


# =============================================================================
# METADATA COPY / VERIFY
# =============================================================================


LOUDNESS_METADATA_KEY_PREFIXES = (
    "REPLAYGAIN_",
    "R128_",
    "EBU_R128",
)

# Exact or compact names for player-side loudness/normalization metadata.
# These tags should not be copied to baked-gain output files because a player
# could apply them on top of the audio gain DropGain just rendered.
LOUDNESS_METADATA_KEYS_EXACT = (
    "ITUNNORM",  # Apple/iTunes Sound Check, often stored as COMM:iTunNORM.
    "ITUNES_NORMALIZATION",
    "SOUND_CHECK",
    "SOUNDCHECK",
)

LOUDNESS_METADATA_COMPACT_PREFIXES = (
    "REPLAYGAIN",
    "R128",
    "EBUR128",
    "ITUNNORM",
    "ITUNESNORMALIZATION",
    "SOUNDCHECK",
)

LOUDNESS_METADATA_ID3_PREFIXES = (
    "RVA2",  # ID3 relative volume adjustment
    "RVAD",  # obsolete ID3 relative volume adjustment
    "RGAD",  # ReplayGain adjustment frame used by some tools
)


def compact_metadata_key(value: object) -> str:
    """Return a normalized metadata key suitable for loose loudness-tag matching."""
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def is_loudness_metadata_key(key: object) -> bool:
    """Return True for tags that describe previous playback loudness normalization."""
    text = str(key or "").strip().upper()
    if not text:
        return False

    # ID3 TXXX/COMM keys are often represented as TXXX:<description> or
    # COMM:<description>:<language>. Test every segment plus the whole key.
    parts = [text, *(part.strip() for part in text.split(":") if part.strip())]

    for part in parts:
        if part in LOUDNESS_METADATA_KEYS_EXACT:
            return True
        if part.startswith(LOUDNESS_METADATA_KEY_PREFIXES):
            return True

        compact = compact_metadata_key(part)
        if any(compact.startswith(prefix) for prefix in LOUDNESS_METADATA_COMPACT_PREFIXES):
            return True

    return False


def is_loudness_metadata_id3_frame(key: object, frame: object | None = None) -> bool:
    """Return True for ID3 frames that can apply stale playback gain/volume adjustment."""
    text = str(key or "").strip().upper()
    if is_loudness_metadata_key(text) or text.startswith(LOUDNESS_METADATA_ID3_PREFIXES):
        return True

    desc = str(getattr(frame, "desc", "") or "").strip().upper()
    return is_loudness_metadata_key(desc) or desc.startswith(LOUDNESS_METADATA_ID3_PREFIXES)


def strip_loudness_id3_frames(tags: ID3) -> None:
    """Remove ReplayGain/R128/relative-volume frames from copied ID3 tags."""
    for key in list(tags.keys()):
        frame = tags.get(key)
        if is_loudness_metadata_id3_frame(key, frame):
            del tags[key]




def normalized_id3_frame_value(frame: object) -> object:
    """Return a stable comparable representation of a Mutagen ID3 frame.

    Mutagen frame objects include implementation details such as text encoding,
    which can legitimately change when tags are saved as ID3v2.3. This helper
    compares the meaningful public frame data instead. Binary payloads, such as
    cover art, are represented by length and SHA-256 digest so large images are
    not copied into warning messages.
    """

    def normalize(value: object) -> object:
        if isinstance(value, bytes):
            return ("bytes", len(value), hashlib.sha256(value).hexdigest())

        if isinstance(value, bytearray):
            raw = bytes(value)
            return ("bytes", len(raw), hashlib.sha256(raw).hexdigest())

        if isinstance(value, (str, int, float, bool)) or value is None:
            return value

        if isinstance(value, (list, tuple)):
            return tuple(normalize(item) for item in value)

        if isinstance(value, dict):
            return tuple(
                sorted(
                    (str(key), normalize(item))
                    for key, item in value.items()
                )
            )

        if isinstance(value, set):
            return tuple(sorted((normalize(item) for item in value), key=repr))

        try:
            attrs = vars(value)
        except TypeError:
            attrs = None

        if attrs is not None:
            return (
                value.__class__.__name__,
                tuple(
                    sorted(
                        (str(key), normalize(item))
                        for key, item in attrs.items()
                        if not str(key).startswith("_") and str(key) != "encoding"
                    )
                ),
            )

        return str(value)

    try:
        attrs = vars(frame)
    except TypeError:
        return str(frame)

    return (
        frame.__class__.__name__,
        tuple(
            sorted(
                (str(key), normalize(value))
                for key, value in attrs.items()
                if not str(key).startswith("_") and str(key) != "encoding"
            )
        ),
    )


def append_changed_id3_frame_warnings(
    problems: list[str],
    source_tags: ID3,
    output_tags: ID3,
    source_keys: set[str],
    output_keys: set[str],
    label: str,
) -> None:
    """Append warnings for matching ID3 frame keys whose meaningful values changed."""
    changed = sorted(
        key for key in (source_keys & output_keys)
        if normalized_id3_frame_value(source_tags.get(key))
        != normalized_id3_frame_value(output_tags.get(key))
    )

    for key in changed[:20]:
        problems.append(f"changed {label} frame {key}")

    if len(changed) > 20:
        problems.append(f"{len(changed) - 20} more {label} frames changed")


def normalized_vorbis_comments(flac: FLAC) -> dict[str, list[str]]:
    """Return uppercase-sorted Vorbis comments, excluding stale loudness metadata."""
    if not flac.tags:
        return {}

    result: dict[str, list[str]] = {}
    for key, values in flac.tags.items():
        if is_loudness_metadata_key(key):
            continue
        result[key.upper()] = sorted(str(v) for v in values)

    return result


def copy_flac_metadata_exact(source_path: str, output_path: str) -> None:
    """Copy FLAC tags and pictures, excluding stale loudness-normalization tags."""
    source = FLAC(source_path)
    output = FLAC(output_path)

    output.clear()

    if source.tags:
        for key, values in source.tags.items():
            if is_loudness_metadata_key(key):
                continue
            output[key] = [str(v) for v in values]

    output.clear_pictures()
    for picture in source.pictures:
        output.add_picture(picture)

    output.save()


def verify_flac_metadata(source_path: str, output_path: str) -> list[str]:
    """Compare tags and pictures between source and output FLAC."""
    source = FLAC(source_path)
    output = FLAC(output_path)

    problems: list[str] = []

    source_tags = normalized_vorbis_comments(source)
    output_tags = normalized_vorbis_comments(output)

    if output.tags:
        stale = sorted(str(key).upper() for key in output.tags.keys() if is_loudness_metadata_key(key))
        for key in stale[:20]:
            problems.append(f"stale loudness FLAC tag {key}")
        if len(stale) > 20:
            problems.append(f"{len(stale) - 20} more stale loudness FLAC tags")

    for key, values in source_tags.items():
        if key not in output_tags:
            problems.append(f"missing FLAC tag {key}")
        elif output_tags[key] != values:
            problems.append(f"changed FLAC tag {key}")

    if len(source.pictures) != len(output.pictures):
        problems.append(f"picture count changed {len(source.pictures)} -> {len(output.pictures)}")

    return problems


def copy_mp3_metadata_exact(source_path: str, output_path: str) -> None:
    """Copy ID3 tags from source MP3, excluding stale loudness-normalization frames."""
    try:
        source_tags = copy.deepcopy(ID3(source_path))
    except ID3NoHeaderError:
        return

    strip_loudness_id3_frames(source_tags)
    source_tags.save(output_path, v2_version=MP3_ID3_VERSION)


def verify_mp3_metadata(source_path: str, output_path: str) -> list[str]:
    """Compare ID3 frames between source and output MP3."""
    problems: list[str] = []

    try:
        source_tags = ID3(source_path)
    except ID3NoHeaderError:
        return problems

    try:
        output_tags = ID3(output_path)
    except ID3NoHeaderError:
        return ["missing ID3 tag"]

    source_keys = {
        key for key in source_tags.keys()
        if not is_loudness_metadata_id3_frame(key, source_tags.get(key))
    }
    output_keys = {
        key for key in output_tags.keys()
        if not is_loudness_metadata_id3_frame(key, output_tags.get(key))
    }

    stale = sorted(
        key for key in output_tags.keys()
        if is_loudness_metadata_id3_frame(key, output_tags.get(key))
    )
    for key in stale[:20]:
        problems.append(f"stale loudness ID3 frame {key}")

    if len(stale) > 20:
        problems.append(f"{len(stale) - 20} more stale loudness ID3 frames")

    missing = sorted(source_keys - output_keys)
    for key in missing[:20]:
        problems.append(f"missing ID3 frame {key}")

    if len(missing) > 20:
        problems.append(f"{len(missing) - 20} more ID3 frames missing")

    append_changed_id3_frame_warnings(
        problems,
        source_tags,
        output_tags,
        source_keys,
        output_keys,
        "ID3",
    )

    return problems


def copy_wav_metadata_best_effort(source_path: str, output_path: str) -> None:
    """Copy WAV ID3 tags from source to output, if present."""
    try:
        source = WAVE(source_path)
    except Exception as exc:
        logger.warning(
            "Could not read source WAV tags from %s: %s",
            source_path,
            exc,
            exc_info=True,
        )
        return

    if not source.tags:
        return

    try:
        output = WAVE(output_path)
        if output.tags is None:
            output.add_tags()

        output.tags.clear()
        for key, frame in source.tags.items():
            if is_loudness_metadata_id3_frame(key, frame):
                continue
            output.tags.add(copy.deepcopy(frame))

        output.save()
    except Exception as exc:
        logger.warning(
            "Could not copy WAV tags from %s to %s: %s",
            source_path,
            output_path,
            exc,
            exc_info=True,
        )
        raise


def verify_wav_metadata(source_path: str, output_path: str) -> list[str]:
    """Compare ID3 frames between source and output WAV."""
    problems: list[str] = []

    try:
        source = WAVE(source_path)
    except Exception as exc:
        logger.warning(
            "Could not read source WAV tags from %s during verification: %s",
            source_path,
            exc,
            exc_info=True,
        )
        return [f"could not read source WAV tags: {exc}"]

    if not source.tags:
        return problems

    try:
        output = WAVE(output_path)
    except Exception as exc:
        logger.warning(
            "Could not read output WAV tags from %s during verification: %s",
            output_path,
            exc,
            exc_info=True,
        )
        return [f"could not read output WAV tags: {exc}"]

    if output.tags is None:
        return ["missing WAV ID3 tag"]

    source_keys = {
        key for key in source.tags.keys()
        if not is_loudness_metadata_id3_frame(key, source.tags.get(key))
    }
    output_keys = {
        key for key in output.tags.keys()
        if not is_loudness_metadata_id3_frame(key, output.tags.get(key))
    }

    stale = sorted(
        key for key in output.tags.keys()
        if is_loudness_metadata_id3_frame(key, output.tags.get(key))
    )
    for key in stale[:20]:
        problems.append(f"stale loudness WAV ID3 frame {key}")

    if len(stale) > 20:
        problems.append(f"{len(stale) - 20} more stale loudness WAV ID3 frames")

    missing = sorted(source_keys - output_keys)
    for key in missing[:20]:
        problems.append(f"missing WAV ID3 frame {key}")

    if len(missing) > 20:
        problems.append(f"{len(missing) - 20} more WAV ID3 frames missing")

    append_changed_id3_frame_warnings(
        problems,
        source.tags,
        output.tags,
        source_keys,
        output_keys,
        "WAV ID3",
    )

    return problems


def copy_aiff_metadata_best_effort(source_path: str, output_path: str) -> None:
    """Copy AIFF ID3 tags from source to output, if present."""
    try:
        source = AIFF(source_path)
    except Exception as exc:
        logger.warning(
            "Could not read source AIFF tags from %s: %s",
            source_path,
            exc,
            exc_info=True,
        )
        return

    if not source.tags:
        return

    try:
        output = AIFF(output_path)
        if output.tags is None:
            output.add_tags()

        output.tags.clear()
        for key, frame in source.tags.items():
            if is_loudness_metadata_id3_frame(key, frame):
                continue
            output.tags.add(copy.deepcopy(frame))

        output.save()
    except Exception as exc:
        logger.warning(
            "Could not copy AIFF tags from %s to %s: %s",
            source_path,
            output_path,
            exc,
            exc_info=True,
        )
        raise


def verify_aiff_metadata(source_path: str, output_path: str) -> list[str]:
    """Compare ID3 frames between source and output AIFF."""
    problems: list[str] = []

    try:
        source = AIFF(source_path)
    except Exception as exc:
        logger.warning(
            "Could not read source AIFF tags from %s during verification: %s",
            source_path,
            exc,
            exc_info=True,
        )
        return [f"could not read source AIFF tags: {exc}"]

    if not source.tags:
        return problems

    try:
        output = AIFF(output_path)
    except Exception as exc:
        logger.warning(
            "Could not read output AIFF tags from %s during verification: %s",
            output_path,
            exc,
            exc_info=True,
        )
        return [f"could not read output AIFF tags: {exc}"]

    if output.tags is None:
        return ["missing AIFF ID3 tag"]

    source_keys = {
        key for key in source.tags.keys()
        if not is_loudness_metadata_id3_frame(key, source.tags.get(key))
    }
    output_keys = {
        key for key in output.tags.keys()
        if not is_loudness_metadata_id3_frame(key, output.tags.get(key))
    }

    stale = sorted(
        key for key in output.tags.keys()
        if is_loudness_metadata_id3_frame(key, output.tags.get(key))
    )
    for key in stale[:20]:
        problems.append(f"stale loudness AIFF ID3 frame {key}")

    if len(stale) > 20:
        problems.append(f"{len(stale) - 20} more stale loudness AIFF ID3 frames")

    missing = sorted(source_keys - output_keys)
    for key in missing[:20]:
        problems.append(f"missing AIFF ID3 frame {key}")

    if len(missing) > 20:
        problems.append(f"{len(missing) - 20} more AIFF ID3 frames missing")

    append_changed_id3_frame_warnings(
        problems,
        source.tags,
        output.tags,
        source_keys,
        output_keys,
        "AIFF ID3",
    )

    return problems


def strip_loudness_tags_from_mp3(output_path: str) -> None:
    """Remove loudness-normalization ID3 frames that ffmpeg mapped into the output MP3."""
    try:
        tags = ID3(output_path)
    except ID3NoHeaderError:
        return

    strip_loudness_id3_frames(tags)
    tags.save(output_path, v2_version=MP3_ID3_VERSION)


def strip_loudness_tags_from_id3_container(output_path: str, audio_cls: Any) -> None:
    """Remove loudness-normalization ID3 frames from WAV/AIFF containers."""
    try:
        audio = audio_cls(output_path)
    except Exception as exc:
        logger.warning(
            "Could not read tags from %s for loudness strip: %s",
            output_path,
            exc,
            exc_info=True,
        )
        return

    tags = getattr(audio, "tags", None)
    if tags is None:
        return

    changed = False
    for key in list(tags.keys()):
        frame = tags.get(key)
        if is_loudness_metadata_id3_frame(key, frame):
            del tags[key]
            changed = True

    if changed:
        try:
            audio.save()
        except Exception as exc:
            logger.warning(
                "Could not save loudness-stripped tags to %s: %s",
                output_path,
                exc,
                exc_info=True,
            )


def strip_loudness_tags_from_aiff(output_path: str) -> None:
    """Remove stale loudness-normalization ID3 frames from an AIFF output."""
    strip_loudness_tags_from_id3_container(output_path, AIFF)


def strip_loudness_tags_from_wav(output_path: str) -> None:
    """Remove stale loudness-normalization ID3 frames from a WAV output."""
    strip_loudness_tags_from_id3_container(output_path, WAVE)


DJ_METADATA_GROUPS: dict[str, tuple[str, ...]] = {
    "title": ("title",),
    "artist": ("artist", "albumartist", "performer"),
    "album": ("album",),
    "genre": ("genre",),
    "comment": ("comment", "comments", "description"),
    "composer": ("composer",),
    "date/year": ("date", "year"),
    "track number": ("tracknumber",),
    "BPM": ("bpm",),
    "musical key": ("initialkey", "key"),
}

ID3_TEXT_FRAME_TO_EASY_KEY: dict[str, str] = {
    "tit2": "title",
    "tpe1": "artist",
    "tpe2": "albumartist",
    "tpe3": "performer",
    "talb": "album",
    "tcon": "genre",
    "tcom": "composer",
    "tdrc": "date",
    "tyer": "date",
    "trck": "tracknumber",
    "tbpm": "bpm",
    "tkey": "initialkey",
}


def easy_metadata_tags(path: str) -> dict[str, list[str]]:
    """Read easy, cross-format metadata tags for DJ-critical field checks."""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception as exc:
        logger.warning(
            "Could not read easy metadata from %s: %s",
            path,
            exc,
            exc_info=True,
        )
        return {}

    tags = getattr(audio, "tags", None)
    if not tags:
        return {}

    result: dict[str, list[str]] = {}
    try:
        items = tags.items()
    except Exception as exc:
        logger.warning(
            "Could not enumerate easy metadata tags from %s: %s",
            path,
            exc,
            exc_info=True,
        )
        return result

    for key, values in items:
        key_text = str(key or "").strip().lower()
        if not key_text or is_loudness_metadata_key(key_text):
            continue
        key_text = ID3_TEXT_FRAME_TO_EASY_KEY.get(key_text, key_text)
        if isinstance(values, (list, tuple)):
            normalized_values = [str(value).strip() for value in values if str(value).strip()]
        else:
            normalized_values = [str(values).strip()] if str(values).strip() else []
        if normalized_values:
            result[key_text] = normalized_values

    # Some containers expose ID3 text frames directly rather than through
    # Mutagen's easy-key aliases. MP3 EasyID3 also omits useful DJ fields such
    # as TKEY, so merge raw ID3 text frames as a fallback.
    raw_tags = source_id3_tags_for_copy(path)
    if raw_tags is not None:
        for raw_key in list(raw_tags.keys()):
            frame = raw_tags.get(raw_key)
            raw_key_text = str(raw_key or "").split(":", 1)[0].strip().lower()
            if raw_key_text == "comm":
                key_text = "comment"
            else:
                key_text = ID3_TEXT_FRAME_TO_EASY_KEY.get(raw_key_text, "")
            if not key_text or key_text in result or is_loudness_metadata_id3_frame(raw_key, frame):
                continue
            values = getattr(frame, "text", None)
            if values is None:
                continue
            if isinstance(values, (list, tuple)):
                normalized_values = [str(value).strip() for value in values if str(value).strip()]
            else:
                normalized_values = [str(values).strip()] if str(values).strip() else []
            if normalized_values:
                result[key_text] = normalized_values

    return result


def first_metadata_group_value(tags: dict[str, list[str]], aliases: tuple[str, ...]) -> list[str]:
    for alias in aliases:
        values = tags.get(alias)
        if values:
            return values
    return []


def has_embedded_art(path: str) -> bool:
    """Return True when common embedded artwork is present."""
    ext = Path(path).suffix.lower()
    try:
        if ext == ".flac":
            return bool(FLAC(path).pictures)
        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                return False
            return any(str(key).upper().startswith("APIC") for key in tags.keys())
        if ext == ".wav":
            tags = WAVE(path).tags
            return bool(tags and any(str(key).upper().startswith("APIC") for key in tags.keys()))
        if ext == ".aiff":
            tags = AIFF(path).tags
            return bool(tags and any(str(key).upper().startswith("APIC") for key in tags.keys()))
    except Exception as exc:
        logger.warning(
            "Could not check embedded artwork in %s: %s",
            path,
            exc,
            exc_info=True,
        )
        return False
    return False




def source_id3_tags_for_copy(source_path: str) -> ID3 | None:
    """Return ID3-style tags from MP3/WAV/AIFF sources, excluding unsupported files."""
    ext = Path(source_path).suffix.lower()
    try:
        if ext == ".mp3":
            try:
                return ID3(source_path)
            except ID3NoHeaderError:
                return None
        if ext == ".wav":
            return WAVE(source_path).tags
        if ext == ".aiff":
            return AIFF(source_path).tags
    except Exception as exc:
        logger.warning(
            "Could not read source ID3 tags from %s: %s",
            source_path,
            exc,
            exc_info=True,
        )
        return None
    return None


def add_non_loudness_id3_frames(target_tags: ID3, source_tags: ID3 | None) -> None:
    """Copy all source ID3 frames except stale playback loudness frames."""
    if source_tags is None:
        return

    for key in list(source_tags.keys()):
        frame = source_tags.get(key)
        if is_loudness_metadata_id3_frame(key, frame):
            continue
        try:
            target_tags.add(copy.deepcopy(frame))
        except Exception as exc:
            logger.warning(
                "Skipped copying ID3 frame %s: %s",
                key,
                exc,
                exc_info=True,
            )
            continue


def first_nonempty_metadata_values(
    tags: dict[str, list[str]],
    aliases: tuple[str, ...],
) -> list[str]:
    """Return cleaned values for the first present metadata alias."""
    for alias in aliases:
        values = tags.get(alias.lower())
        if values:
            cleaned = [str(value).strip() for value in values if str(value).strip()]
            if cleaned:
                return cleaned
    return []


def add_text_frame_if_present(
    tags: ID3,
    frame_cls: Any,
    values: list[str],
) -> None:
    """Add one ID3 text frame when source metadata contains usable values."""
    if not values:
        return
    frame_name = getattr(frame_cls, "__name__", str(frame_cls))
    try:
        tags.add(frame_cls(encoding=3, text=values))
    except Exception:
        try:
            tags.add(frame_cls(encoding=3, text=[str(values[0])]))
        except Exception as retry_exc:
            logger.warning(
                "Could not add ID3 frame %s with values %r: %s",
                frame_name,
                values,
                retry_exc,
                exc_info=True,
            )


def add_comm_frame_if_present(tags: ID3, values: list[str]) -> None:
    """Add one ID3 comment frame when source metadata contains usable values."""
    if not values:
        return
    try:
        tags.add(COMM(encoding=3, lang="eng", desc="", text=values))
    except Exception:
        try:
            tags.add(COMM(encoding=3, lang="eng", desc="", text=[str(values[0])]))
        except Exception as retry_exc:
            logger.warning(
                "Could not add ID3 COMM frame with values %r: %s",
                values,
                retry_exc,
                exc_info=True,
            )


def add_easy_metadata_as_id3_frames(target_tags: ID3, source_path: str) -> None:
    """Map common easy metadata fields to portable ID3 frames."""
    source_tags = easy_metadata_tags(source_path)
    if not source_tags:
        return

    field_map: tuple[tuple[Any, tuple[str, ...]], ...] = (
        (TIT2, ("title",)),
        (TPE1, ("artist", "performer", "albumartist")),
        (TPE2, ("albumartist",)),
        (TALB, ("album",)),
        (TCON, ("genre",)),
        (TCOM, ("composer",)),
        (TDRC, ("date", "year")),
        (TRCK, ("tracknumber",)),
        (TBPM, ("bpm",)),
        (TKEY, ("initialkey", "key")),
    )

    for frame_cls, aliases in field_map:
        values = first_nonempty_metadata_values(source_tags, aliases)
        add_text_frame_if_present(target_tags, frame_cls, values)

    comment_values = first_nonempty_metadata_values(
        source_tags,
        ("comment", "comments", "description"),
    )
    add_comm_frame_if_present(target_tags, comment_values)


def add_flac_pictures_as_apic_frames(target_tags: ID3, source_path: str) -> None:
    """Convert FLAC embedded pictures to ID3 APIC frames."""
    try:
        source = FLAC(source_path)
    except Exception as exc:
        logger.warning(
            "Could not read FLAC pictures from %s: %s",
            source_path,
            exc,
            exc_info=True,
        )
        return

    for index, picture in enumerate(source.pictures):
        data = getattr(picture, "data", b"") or b""
        if not data:
            continue
        mime = str(getattr(picture, "mime", "") or "image/jpeg")
        desc = str(getattr(picture, "desc", "") or "")
        pic_type = getattr(picture, "type", 3)
        try:
            pic_type = int(pic_type)
        except Exception:
            pic_type = 3
        try:
            target_tags.add(
                APIC(
                    encoding=3,
                    mime=mime,
                    type=pic_type,
                    desc=desc or f"Cover {index + 1}",
                    data=data,
                )
            )
        except Exception as exc:
            logger.warning(
                "Could not add APIC frame %s from %s: %s",
                index + 1,
                source_path,
                exc,
                exc_info=True,
            )
            continue


def save_id3_tags_to_output(output_path: str, tags: ID3) -> None:
    """Save ID3-style tags to an MP3/WAV/AIFF output container."""
    output_ext = Path(output_path).suffix.lower()
    strip_loudness_id3_frames(tags)

    if output_ext == ".mp3":
        tags.save(output_path, v2_version=MP3_ID3_VERSION)
        return

    if output_ext == ".wav":
        audio = WAVE(output_path)
    elif output_ext == ".aiff":
        audio = AIFF(output_path)
    else:
        return

    if audio.tags is None:
        audio.add_tags()

    audio.tags.clear()
    for frame in tags.values():
        audio.tags.add(copy.deepcopy(frame))
    audio.save()


def copy_cross_format_metadata(source_path: str, output_path: str) -> None:
    """Copy DJ-critical metadata and artwork when the output format changes.

    ffmpeg's generic metadata mapping is inconsistent across FLAC/MP3/WAV/AIFF,
    especially for artwork. Build a clean ID3-style tag set explicitly instead,
    then save it into the output container.
    """
    output_ext = Path(output_path).suffix.lower()
    if output_ext not in {".mp3", ".wav", ".aiff"}:
        return

    target_tags = ID3()
    # Keep any useful tags ffmpeg already managed to map, then override/fill
    # from the source using deterministic field mapping. This avoids erasing
    # valid fallback metadata when a source exposes unusual tag keys.
    add_non_loudness_id3_frames(target_tags, source_id3_tags_for_copy(output_path))
    add_non_loudness_id3_frames(target_tags, source_id3_tags_for_copy(source_path))
    add_easy_metadata_as_id3_frames(target_tags, source_path)

    if Path(source_path).suffix.lower() == ".flac":
        add_flac_pictures_as_apic_frames(target_tags, source_path)

    save_id3_tags_to_output(output_path, target_tags)

def verify_output_has_no_loudness_tags(output_path: str) -> list[str]:
    """Return stale loudness-tag warnings for any supported output container."""
    output_ext = Path(output_path).suffix.lower()
    problems: list[str] = []

    try:
        if output_ext == ".mp3":
            try:
                tags = ID3(output_path)
            except ID3NoHeaderError:
                return []
            stale = sorted(key for key in tags.keys() if is_loudness_metadata_id3_frame(key, tags.get(key)))
            return [f"stale loudness ID3 frame {key}" for key in stale[:20]]

        if output_ext == ".flac":
            flac = FLAC(output_path)
            if flac.tags:
                stale = sorted(str(key).upper() for key in flac.tags.keys() if is_loudness_metadata_key(key))
                return [f"stale loudness FLAC tag {key}" for key in stale[:20]]

        if output_ext in {".wav", ".aiff"}:
            audio = WAVE(output_path) if output_ext == ".wav" else AIFF(output_path)
            tags = getattr(audio, "tags", None)
            if tags:
                stale = sorted(key for key in tags.keys() if is_loudness_metadata_id3_frame(key, tags.get(key)))
                return [f"stale loudness ID3 frame {key}" for key in stale[:20]]
    except Exception as exc:
        logger.warning(
            "Loudness-tag check failed for %s: %s",
            output_path,
            exc,
            exc_info=True,
        )
        problems.append(f"metadata loudness-tag check failed: {exc}")

    return problems


def verify_cross_format_metadata(source_path: str, output_path: str) -> list[str]:
    """Best-effort verification for forced MP3/AIFF cross-format outputs."""
    problems = verify_output_has_no_loudness_tags(output_path)

    source_tags = easy_metadata_tags(source_path)
    output_tags = easy_metadata_tags(output_path)
    for label, aliases in DJ_METADATA_GROUPS.items():
        source_values = first_metadata_group_value(source_tags, aliases)
        if not source_values:
            continue
        output_values = first_metadata_group_value(output_tags, aliases)
        if not output_values:
            problems.append(f"missing converted metadata field {label}")

    if has_embedded_art(source_path) and not has_embedded_art(output_path):
        problems.append("missing converted embedded artwork")

    return problems


def copy_metadata_exact(source_path: str, output_path: str) -> None:
    """Dispatch to format-specific metadata copy (exact for FLAC/MP3, best-effort for WAV/AIFF)."""
    ext = Path(source_path).suffix.lower()

    output_ext = Path(output_path).suffix.lower()
    try:
        if ext != output_ext:
            copy_cross_format_metadata(source_path, output_path)
            return

        if ext == ".flac":
            copy_flac_metadata_exact(source_path, output_path)
        elif ext == ".mp3":
            copy_mp3_metadata_exact(source_path, output_path)
        elif ext == ".wav":
            copy_wav_metadata_best_effort(source_path, output_path)
        elif ext == ".aiff":
            copy_aiff_metadata_best_effort(source_path, output_path)
    except Exception as exc:
        logger.warning(
            "Metadata copy failed for %s -> %s: %s",
            source_path,
            output_path,
            exc,
            exc_info=True,
        )
        raise


def verify_metadata(source_path: str, output_path: str) -> tuple[str, str]:
    """Dispatch to format-specific metadata verification. Returns (status, message)."""
    ext = Path(source_path).suffix.lower()

    try:
        if ext != Path(output_path).suffix.lower():
            problems = verify_cross_format_metadata(source_path, output_path)
        elif ext == ".flac":
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
        logger.warning(
            "Metadata verification failed for %s -> %s: %s",
            source_path,
            output_path,
            exc,
            exc_info=True,
        )
        problems = [f"metadata verification failed: {exc}"]

    if problems:
        return "warning", "; ".join(problems)

    return "ok", ""



WINDOWS_TRANSIENT_FILE_ERRORS = {5, 32, 33}


def is_transient_windows_file_error(exc: OSError) -> bool:
    """Return True for common short-lived Windows file sharing/lock errors."""
    if os.name != "nt":
        return False

    winerror = getattr(exc, "winerror", None)
    if winerror in WINDOWS_TRANSIENT_FILE_ERRORS:
        return True

    return isinstance(exc, PermissionError)


def retry_windows_file_operation(
    operation: Any,
    *,
    description: str,
    attempts: int = 12,
    initial_delay_seconds: float = 0.12,
) -> None:
    """Retry a file operation that may briefly be blocked by Windows scanners/indexers."""
    last_exc: OSError | None = None

    for attempt in range(1, max(1, attempts) + 1):
        try:
            operation()
            return
        except OSError as exc:
            if not is_transient_windows_file_error(exc):
                raise

            last_exc = exc
            if attempt >= attempts:
                break

            time.sleep(min(1.25, initial_delay_seconds * (1.35 ** (attempt - 1))))

    if last_exc is not None:
        raise RuntimeError(f"{description} failed after {attempts} attempts: {last_exc}") from last_exc


def replace_file_with_retries(tmp_path: str, output_path: str) -> None:
    """Replace output_path with tmp_path, retrying transient Windows lock errors."""
    retry_windows_file_operation(
        lambda: os.replace(tmp_path, output_path),
        description=f"finalizing output {tmp_path!r} -> {output_path!r}",
    )


def remove_file_with_retries(path: str) -> None:
    """Remove a file, retrying transient Windows lock errors."""
    if not os.path.exists(path):
        return

    retry_windows_file_operation(
        lambda: os.remove(path),
        description=f"removing temporary file {path!r}",
        attempts=6,
        initial_delay_seconds=0.10,
    )



# =============================================================================
# PROCESSING / VERIFY
# =============================================================================


def gain_to_volume_filter(gain_db: float) -> str:
    """Build an ffmpeg volume filter string. Currently unused (Pro-L 2 path only)."""
    linear = 10.0 ** (gain_db / 20.0)
    return f"volume={linear:.12f}:precision=double"


def encoder_args_for_output(
    ext: str,
    source_info: dict[str, object],
    source_ext: str = "",
    *,
    pioneer_compatible_aiff: bool = False,
) -> list[str]:
    """Return ffmpeg encoder arguments tailored to the output format."""
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
        args = [
            "-c:a",
            "libmp3lame",
            "-b:a",
            MP3_OUTPUT_BITRATE,
            "-id3v2_version",
            str(MP3_ID3_VERSION),
            "-write_id3v1",
            "1",
        ]
        # libmp3lame supports at most 48 kHz; hi-res lossless sources rendered
        # as forced MP3 must be downsampled at encode time.
        if parse_int_or_default(source_info.get("sample_rate"), 0) > 48_000:
            args.extend(["-ar", "48000"])
        return args

    if ext == ".wav":
        return [
            "-c:a",
            wav_codec_to_use(source_info),
        ]

    if ext == ".aiff":
        if pioneer_compatible_aiff:
            source_sr = parse_int_or_default(source_info.get("sample_rate"), 0)
            target_sr = pioneer_compatible_aiff_sample_rate(source_sr)
            args = ["-c:a", pioneer_compatible_aiff_codec(source_info, source_ext)]
            if target_sr > 0 and target_sr != source_sr:
                args.extend(["-ar", str(target_sr)])
            return args
        return [
            "-c:a",
            aiff_codec_to_use(source_info, source_ext),
        ]

    raise RuntimeError(f"processing not supported for {ext}")


def verify_lossless_output(
    input_path: str,
    output_path: str,
    input_info: dict[str, object],
    output_info: dict[str, object],
) -> None:
    """Verify that lossless output matches input sample rate, channels, and bit depth.
    Deletes the output file on mismatch.
    """
    ext = Path(input_path).suffix.lower()

    if ext not in {".flac", ".wav", ".aiff"}:
        return

    if Path(output_path).suffix.lower() != ext:
        # Forced-MP3 output: lossless invariants do not apply.
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

    if ext == ".aiff":
        expected_aiff_codec = aiff_codec_to_use(input_info, ext)
        if output_codec != expected_aiff_codec:
            problems.append(f"AIFF codec changed {expected_aiff_codec} -> {output_codec}")

    if problems:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass

        raise RuntimeError("lossless output verification failed: " + "; ".join(problems))


PEAK_CONTROL_EPSILON_DB = 0.05
MP3_PEAK_CONTROL_EPSILON_DB = 0.01
MP3_TRUE_PEAK_INITIAL_RENDER_MARGIN_DB = 0.60
MP3_TRUE_PEAK_MAX_RENDER_MARGIN_DB = 2.00
MP3_TRUE_PEAK_RETRY_TARGET_OFFSET_DB = 0.05
POST_LIMITER_TRIM_EPSILON_LU = 0.05


def apply_linear_gain(audio: np.ndarray, gain_db: float) -> np.ndarray:
    """Apply transparent linear gain to decoded float audio."""
    if abs(float(gain_db)) < 0.000001:
        return audio.astype(np.float32, copy=False)

    linear = 10.0 ** (float(gain_db) / 20.0)
    return (audio.astype(np.float32, copy=False) * np.float32(linear)).astype(np.float32, copy=False)


def limiter_render_output_level_dbfs(
    path: str,
    peak_ceiling_dbfs: float,
    mp3_margin_db: float | None = None,
) -> float:
    """Return the Pro-L output level used while rendering this file."""
    output_level = float(peak_ceiling_dbfs)
    if Path(path).suffix.lower() == ".mp3":
        margin = MP3_TRUE_PEAK_INITIAL_RENDER_MARGIN_DB if mp3_margin_db is None else float(mp3_margin_db)
        output_level -= max(0.0, min(MP3_TRUE_PEAK_MAX_RENDER_MARGIN_DB, margin))
    return output_level


def initial_mp3_limiter_margin_db(path: str) -> float:
    if Path(path).suffix.lower() != ".mp3":
        return 0.0
    return MP3_TRUE_PEAK_INITIAL_RENDER_MARGIN_DB


def adjusted_mp3_limiter_margin_db(
    path: str,
    current_margin_db: float,
    output_true_peak_dbtp: object,
    peak_ceiling_dbfs: float,
) -> float | None:
    if Path(path).suffix.lower() != ".mp3":
        return None

    try:
        output_true_peak = float(output_true_peak_dbtp)
    except Exception:
        return None

    if not math.isfinite(output_true_peak):
        return None

    retry_target = float(peak_ceiling_dbfs) - MP3_TRUE_PEAK_RETRY_TARGET_OFFSET_DB
    if output_true_peak <= retry_target:
        return None

    current_margin = max(0.0, float(current_margin_db))
    required_extra = max(0.20, output_true_peak - retry_target)
    next_margin = min(MP3_TRUE_PEAK_MAX_RENDER_MARGIN_DB, current_margin + required_extra)

    if next_margin <= current_margin + 0.01:
        return None

    return next_margin


def adjusted_mp3_clean_gain_db(
    path: str,
    current_gain_db: float,
    output_true_peak_dbtp: object,
    peak_ceiling_dbfs: float,
) -> tuple[float, float] | None:
    """Return a lower clean-gain value for an MP3 output that encoded too hot."""
    if Path(path).suffix.lower() != ".mp3":
        return None

    try:
        output_true_peak = float(output_true_peak_dbtp)
    except Exception:
        return None

    if not math.isfinite(output_true_peak):
        return None

    retry_target = float(peak_ceiling_dbfs) - MP3_TRUE_PEAK_RETRY_TARGET_OFFSET_DB
    if output_true_peak <= retry_target:
        return None

    correction = output_true_peak - retry_target
    if correction <= 0.01:
        return None

    next_gain = float(current_gain_db) - correction
    return next_gain, correction


def update_clean_gain_projection_fields(row: TrackRow, gain_db: float, peak_ceiling_dbfs: float) -> None:
    """Update report projection fields after an MP3 clean-gain safety retry."""
    loudest = parse_float_or_default(row.get("loudest_section_lufs"), 0.0)
    sample_peak = parse_float_or_default(row.get("sample_peak_dbfs"), 0.0)
    true_peak = parse_float_or_default(row.get("true_peak_dbtp"), sample_peak)
    projected_loudest = loudest + float(gain_db)
    projected_sample_peak = sample_peak + float(gain_db)
    projected_true_peak = true_peak + float(gain_db)
    estimated_peak_control = max(0.0, projected_true_peak + MP3_ENCODE_TRUE_PEAK_LIFT_DB - float(peak_ceiling_dbfs))

    row["suggested_gain_db"] = round_or_blank(gain_db, 2)
    row["projected_loudest_section_lufs"] = round_or_blank(projected_loudest, 2)
    row["projected_sample_peak_dbfs"] = round_or_blank(projected_sample_peak, 2)
    row["projected_true_peak_dbtp"] = round_or_blank(projected_true_peak, 2)
    row["estimated_peak_control_db"] = round_or_blank(estimated_peak_control, 2)


def limiter_peak_control_epsilon_db(ext: str) -> float:
    """Return the limiter-routing threshold for a file extension."""
    if ext.lower() == ".mp3":
        return MP3_PEAK_CONTROL_EPSILON_DB
    return PEAK_CONTROL_EPSILON_DB


def section_lufs_from_audio(
    audio: np.ndarray,
    sample_rate: int,
    start_sec: float,
    end_sec: float,
) -> float:
    """Measure a time slice from an in-memory render using pyloudnorm."""
    sr = max(1, int(sample_rate))
    start_sample = max(0, int(round(float(start_sec) * sr)))
    end_sample = max(start_sample + 1, int(round(float(end_sec) * sr)))
    end_sample = min(end_sample, audio.shape[0])

    if end_sample <= start_sample:
        raise RuntimeError("post-limiter loudness window is empty")

    section = audio[start_sample:end_sample]
    meter = pyln.Meter(sr)
    return measure_lufs(meter, section)


def aiff_form_size_mismatch_bytes(path: str) -> int | None:
    """Return FORM payload size minus actual bytes after the header, or None if valid."""
    file_size = os.path.getsize(path)
    if file_size < 12:
        return None

    with open(path, "rb") as handle:
        header = handle.read(12)
    if len(header) < 12 or header[:4] != b"FORM":
        return None

    form_type = header[8:12]
    if form_type not in {b"AIFF", b"AIFC"}:
        return None

    declared_payload = struct.unpack(">I", header[4:8])[0]
    actual_payload = file_size - 8
    mismatch = declared_payload - actual_payload
    if mismatch == 0:
        return None
    return mismatch


def validate_aiff_container(output_path: str) -> list[str]:
    """Walk AIFF chunks and return structural container problems."""
    problems: list[str] = []

    try:
        file_size = os.path.getsize(output_path)
    except OSError as exc:
        return [f"cannot read AIFF file size: {exc}"]

    if file_size < 12:
        return ["AIFF file is too small"]

    with open(output_path, "rb") as handle:
        data = handle.read()

    if data[:4] != b"FORM":
        return ["missing FORM chunk"]

    declared_payload = struct.unpack(">I", data[4:8])[0]
    form_type = data[8:12]
    if form_type not in {b"AIFF", b"AIFC"}:
        problems.append(f"unsupported FORM type {form_type.decode('ascii', errors='replace')!r}")

    form_end = 8 + declared_payload
    if form_end != file_size:
        problems.append(f"AIFF FORM size mismatch ({declared_payload - (file_size - 8):+d} bytes)")

    offset = 12
    has_comm = False
    has_ssnd = False

    while offset < form_end:
        if offset + 8 > file_size:
            problems.append(f"truncated AIFF chunk header at offset {offset}")
            break

        chunk_id = data[offset : offset + 4]
        chunk_size = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        chunk_data_start = offset + 8
        chunk_data_end = chunk_data_start + chunk_size
        padded_end = chunk_data_end + (chunk_size % 2)

        if chunk_data_end > file_size:
            problems.append(
                f"chunk {chunk_id.decode('ascii', errors='replace')!r} exceeds file length"
            )
            break
        if padded_end > form_end:
            problems.append(
                f"chunk {chunk_id.decode('ascii', errors='replace')!r} extends past FORM boundary"
            )
            break

        if chunk_id == b"COMM":
            has_comm = True
        elif chunk_id == b"SSND":
            has_ssnd = True

        offset = padded_end

    if offset < form_end and not any("truncated" in problem or "extends past" in problem for problem in problems):
        problems.append(f"AIFF chunk walk ended at {offset}, expected {form_end}")

    if not has_comm:
        problems.append("missing COMM chunk")
    if not has_ssnd:
        problems.append("missing SSND chunk")

    return problems


def validate_pioneer_compatible_aiff_format(output_info: dict[str, object]) -> list[str]:
    """Return Pioneer/Rekordbox format problems for an already-probed AIFF output."""
    problems: list[str] = []

    sample_rate = parse_int_or_default(output_info.get("sample_rate"), 0)
    if sample_rate not in PIONEER_COMPATIBLE_AIFF_SAMPLE_RATES:
        problems.append(f"unsupported AIFF sample rate {sample_rate}")

    codec = str(output_info.get("codec_name") or "").lower()
    if codec not in PIONEER_COMPATIBLE_AIFF_CODECS:
        problems.append(f"unsupported AIFF codec {codec or 'unknown'}")

    channels = parse_int_or_default(output_info.get("channels"), 0)
    if channels not in {1, 2}:
        problems.append(f"unsupported channel count {channels}")

    return problems


def validate_pioneer_compatible_aiff(output_path: str) -> list[str]:
    """Return container and Pioneer-format problems for DJ-safe AIFF exports."""
    problems = validate_aiff_container(output_path)
    try:
        output_info = ffprobe_audio_info(output_path)
    except Exception as exc:
        problems.append(f"ffprobe failed: {exc}")
        return problems

    problems.extend(validate_pioneer_compatible_aiff_format(output_info))
    return problems


def delete_output_on_validation_failure(output_path: str) -> None:
    """Remove an output file after a hard compatibility validation failure."""
    try:
        remove_file_with_retries(output_path)
    except OSError:
        pass


def repair_aiff_form_size(output_path: str) -> bool:
    """Sync the AIFF FORM chunk size to the actual file length."""
    file_size = os.path.getsize(output_path)
    if file_size < 12:
        return False

    with open(output_path, "r+b") as handle:
        header = handle.read(12)
        if header[:4] != b"FORM" or header[8:12] not in {b"AIFF", b"AIFC"}:
            return False

        declared_payload = struct.unpack(">I", header[4:8])[0]
        actual_payload = file_size - 8
        if declared_payload == actual_payload:
            return False

        handle.seek(4)
        handle.write(struct.pack(">I", actual_payload))

    return True


def finalize_processed_output(
    input_path: str,
    output_path: str,
    source_info: dict[str, object],
    *,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
) -> dict[str, object]:
    """Copy metadata, probe output, and verify lossless technical properties."""
    aiff_output = is_aiff_output(output_path)
    pioneer_compatible_aiff = requires_pioneer_compatible_aiff(output_format_mode, output_path)

    with benchmark_timer("metadata copy"):
        copy_metadata_exact(input_path, output_path)

    if aiff_output:
        with benchmark_timer("aiff container repair"):
            repair_aiff_form_size(output_path)

    with benchmark_timer("output ffprobe"):
        output_info = ffprobe_audio_info(output_path)

    if aiff_output:
        problems = validate_aiff_container(output_path)
        if pioneer_compatible_aiff:
            problems.extend(validate_pioneer_compatible_aiff_format(output_info))
        if problems:
            delete_output_on_validation_failure(output_path)
            label = "Pioneer-compatible AIFF" if pioneer_compatible_aiff else "AIFF"
            raise RuntimeError(f"{label} validation failed: " + "; ".join(problems))
    elif STRICT_VERIFY_LOSSLESS_OUTPUT:
        verify_lossless_output(
            input_path=input_path,
            output_path=output_path,
            input_info=source_info,
            output_info=output_info,
        )

    return output_info


def process_audio_with_clean_gain(
    input_path: str,
    output_path: str,
    gain_db: float,
    source_info: dict[str, object],
    *,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
) -> dict[str, object]:
    """Render with transparent linear gain only; no limiter/plugin is touched."""
    ext = Path(input_path).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise RuntimeError(f"processing not supported for {ext}")
    if os.path.exists(output_path) and not PROCESS_OVERWRITE_EXISTING:
        raise RuntimeError("output already exists")

    output_parent = os.path.dirname(os.path.abspath(output_path))
    if output_parent:
        os.makedirs(output_parent, exist_ok=True)

    sr = parse_int_or_default(source_info.get("sample_rate"), 0)
    channels = parse_int_or_default(source_info.get("channels"), 0)
    if sr <= 0 or channels <= 0:
        raise RuntimeError("invalid sample rate or channel count")

    with benchmark_timer("render"):
        audio = decode_audio_ffmpeg_at_sample_rate(input_path, channels, sr)
        processed = apply_linear_gain(audio, gain_db)
        encode_float_audio_ffmpeg(
            processed,
            input_path,
            output_path,
            source_info,
            output_format_mode=output_format_mode,
        )

    output_info = finalize_processed_output(
        input_path,
        output_path,
        source_info,
        output_format_mode=output_format_mode,
    )
    output_info["_processing_engine"] = PROCESSING_ENGINE_CLEAN_GAIN
    output_info["_limiter_used"] = False
    output_info["_post_limiter_trim_db"] = 0.0
    return output_info


def is_preserved_source_mp3_output(row: TrackRow) -> bool:
    """Return True when an MP3 source is intentionally kept as MP3 output."""
    source_ext = str(row.get("extension", "")).lower()
    output_ext = Path(str(row.get("output_path", ""))).suffix.lower()
    output_mode = str(row.get("output_format_mode", DEFAULT_OUTPUT_FORMAT_MODE))
    return (
        source_ext == ".mp3"
        and output_ext == ".mp3"
        and output_mode == OUTPUT_FORMAT_PRESERVE
    )


def mp3_output_encode_peak_allowance_db(row: TrackRow) -> float:
    """Return encode peak allowance when strict final-MP3 TP safety is active."""
    output_ext = Path(str(row.get("output_path", ""))).suffix.lower()
    if output_ext == ".mp3" and not is_preserved_source_mp3_output(row):
        return MP3_ENCODE_TRUE_PEAK_LIFT_DB
    return 0.0


def effective_estimated_peak_control_db(row: TrackRow) -> float:
    """Return peak-control estimate, including final-MP3 encode allowance."""
    peak_control = parse_float_or_default(row.get("estimated_peak_control_db"), 0.0)
    allowance = mp3_output_encode_peak_allowance_db(row)
    if allowance <= 0.0:
        return peak_control

    true_peak = parse_float_or_default(row.get("true_peak_dbtp"), 0.0)
    gain = parse_float_or_default(row.get("suggested_gain_db"), 0.0)
    headroom = parse_float_or_default(row.get("true_peak_headroom_db"), 0.0)
    peak_ceiling = true_peak + headroom
    encode_adjusted = true_peak + gain + allowance - peak_ceiling
    return max(peak_control, max(0.0, encode_adjusted))


def apply_render_warnings(
    row: TrackRow,
    base_warnings: str,
    *,
    render_extras: Iterable[str] = (),
    post_limiter_note: str = "",
    metadata_status: str = "",
    metadata_message: str = "",
    audio_status: str = "",
    audio_message: str = "",
) -> None:
    """Merge analysis warnings with render-time issues only."""
    warnings = str(base_warnings or "").strip()
    for extra in render_extras:
        text = str(extra or "").strip()
        if text:
            warnings = append_note(warnings, text)
    if post_limiter_note.strip():
        warnings = append_note(warnings, post_limiter_note.strip())
    if metadata_status == "warning" and metadata_message.strip():
        warnings = append_note(warnings, metadata_message.strip())
    if audio_status == "warning" and audio_message.strip():
        warnings = append_note(warnings, audio_message.strip())
    row["warnings"] = warnings


def _verify_prol2_plugin_impl(configured_path: str | None = None) -> tuple[str, str]:
    """Load Pro-L 2 on the render host thread and verify exposed parameters."""
    try:
        from pedalboard import load_plugin
    except Exception as exc:
        raise RuntimeError("pedalboard is required for FabFilter Pro-L 2 processing") from exc

    plugin_path = find_prol2_plugin_path(configured_path)
    plugin = load_plugin(plugin_path)
    names = set(plugin_parameter_names(plugin))
    required = ("true_peak_limiting", "output_level", "gain")
    missing = [name for name in required if name not in names]
    if missing:
        raise RuntimeError(
            "FabFilter Pro-L 2 is missing required parameters through pedalboard: "
            + ", ".join(missing)
        )

    configure_prol2_for_gain(
        plugin,
        gain_db=0.0,
        output_level_dbfs=PROL2_DEFAULT_OUTPUT_LEVEL_DBFS,
        true_peak=PROL2_DEFAULT_TRUE_PEAK,
        oversampling=PROL2_DEFAULT_OVERSAMPLING,
        style=PROL2_DEFAULT_STYLE,
    )
    detail = f"{plugin_path} ({len(names)} parameters exposed)"
    return plugin_path, detail


def verify_prol2_plugin(configured_path: str | None = None) -> tuple[str, str]:
    """Load Pro-L 2 and verify required pedalboard parameters are exposed."""
    return get_prol2_render_host().run(_verify_prol2_plugin_impl, configured_path)


def _verify_loudmax_plugin_impl(configured_path: str | None = None) -> tuple[str, str]:
    """Load LoudMax on the render host thread and verify exposed parameters."""
    try:
        from pedalboard import load_plugin
    except Exception as exc:
        raise RuntimeError("pedalboard is required for LoudMax processing") from exc

    plugin_path = find_loudmax_plugin_path(configured_path)
    plugin = load_plugin(plugin_path)
    names = plugin_parameter_names(plugin)

    configure_loudmax_for_gain(
        plugin,
        output_level_dbfs=PROL2_DEFAULT_OUTPUT_LEVEL_DBFS,
        true_peak=True,
    )

    detail = f"{plugin_path} ({len(names)} parameters exposed, true peak enabled)"
    return plugin_path, detail


def verify_loudmax_plugin(configured_path: str | None = None) -> tuple[str, str]:
    """Load LoudMax and verify required pedalboard parameters are exposed."""
    return get_prol2_render_host().run(_verify_loudmax_plugin_impl, configured_path)


def finalize_processed_render(
    row: TrackRow,
    *,
    input_path: str,
    output_path: str,
    output_info: dict[str, object],
    base_warnings: str,
    render_extras: list[str],
    peak_ceiling_dbfs: float,
    post_loudness_window_seconds: float | None = None,
    post_loudness_hop_seconds: float | None = None,
) -> tuple[str, str, str, str]:
    """Update output fields, verify metadata/audio, and merge render warnings."""
    row["processing_engine"] = str(output_info.get("_processing_engine", row["processing_engine"]))
    post_limiter_note = str(output_info.get("_post_limiter_note", "") or "")

    row["output_sample_rate"] = output_info.get("sample_rate", "")
    row["output_channels"] = output_info.get("channels", "")
    row["output_audio_codec"] = output_info.get("codec_name", "")
    row["output_audio_sample_fmt"] = output_info.get("sample_fmt", "")
    row["output_bit_depth"] = infer_bit_depth(output_info, Path(output_path).suffix)
    row["output_bit_rate"] = output_info.get("bit_rate", "")
    row["output_file_size_mb"] = round_or_blank(os.path.getsize(output_path) / 1_048_576, 2)

    with benchmark_timer("metadata verification"):
        metadata_status, metadata_message = verify_metadata(input_path, output_path)
    row["metadata_verification"] = metadata_status

    with benchmark_timer("audio verification"):
        audio_status, audio_message = verify_processed_audio_fast(
            row,
            output_path,
            peak_ceiling_dbfs,
            accept_mp3_true_peak_issues=is_preserved_source_mp3_output(row),
            post_loudness_window_seconds=post_loudness_window_seconds,
            post_loudness_hop_seconds=post_loudness_hop_seconds,
            output_info=output_info,
        )
    row["audio_verification"] = audio_status
    apply_render_warnings(
        row,
        base_warnings,
        render_extras=render_extras,
        post_limiter_note=post_limiter_note,
        metadata_status=metadata_status,
        metadata_message=metadata_message,
        audio_status=audio_status,
        audio_message=audio_message,
    )
    return metadata_status, metadata_message, audio_status, audio_message


def row_should_use_limiter(row: TrackRow) -> bool:
    """Return True when limiter-assisted mode is expected to control true peaks."""
    mode = str(row.get("normalization_mode", ""))
    output_ext = Path(str(row.get("output_path", ""))).suffix or str(row.get("extension", ""))
    threshold = limiter_peak_control_epsilon_db(output_ext)
    peak_control = effective_estimated_peak_control_db(row)

    return (
        mode == NORMALIZATION_MODE_LIMITER_ASSISTED
        and peak_control > threshold
    )


def row_needs_final_mp3_peak_safety(row: TrackRow) -> bool:
    """Return True when a forced MP3 encode needs true-peak safety handling.

    Independent of limiter selection: forced MP3 may exceed the ceiling after
    encode lift, so clean-gain renders can retry with lower gain. Preserve-format
    MP3 outputs skip this path.
    """
    output_ext = Path(str(row.get("output_path", ""))).suffix or str(row.get("extension", ""))
    if output_ext.lower() != ".mp3" or is_preserved_source_mp3_output(row):
        return False

    threshold = limiter_peak_control_epsilon_db(output_ext)
    return effective_estimated_peak_control_db(row) > threshold


# =============================================================================
# FabFilter Pro-L 2 rendering
# =============================================================================

T = TypeVar("T")


class ProL2RenderHost:
    """Run FabFilter Pro-L 2 / pedalboard work on one persistent thread.

    Pedalboard binds VST3 plugin load to the thread that first touches the
    plugin. All limiter renders go through this host so batches share one
    plugin instance.
    """

    def __init__(self) -> None:
        self._start_lock = threading.Lock()
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def run(self, func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Execute func on the dedicated Pro-L 2 worker thread."""
        self._ensure_started()
        done = threading.Event()
        result: list[T] = []
        error: list[BaseException] = []

        def job() -> None:
            try:
                result.append(func(*args, **kwargs))
            except BaseException as exc:
                error.append(exc)
            finally:
                done.set()

        self._queue.put(job)
        done.wait()
        if error:
            raise error[0]
        return result[0]

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop the worker thread. Safe to call multiple times."""
        with self._start_lock:
            thread = self._thread
            self._thread = None
        if thread is None or not thread.is_alive():
            return
        self._queue.put(None)
        if wait:
            thread.join()

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._worker,
                name="ProL2RenderHost",
                daemon=True,
            )
            self._thread.start()

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                job()
            finally:
                self._queue.task_done()


_prol2_render_host: ProL2RenderHost | None = None
_prol2_render_host_lock = threading.Lock()


def get_prol2_render_host() -> ProL2RenderHost:
    """Return the process-wide Pro-L 2 render host."""
    global _prol2_render_host
    with _prol2_render_host_lock:
        if _prol2_render_host is None:
            _prol2_render_host = ProL2RenderHost()
        return _prol2_render_host


def shutdown_prol2_render_host(*, wait: bool = True) -> None:
    """Shut down the process-wide Pro-L 2 render host."""
    global _prol2_render_host
    with _prol2_render_host_lock:
        host = _prol2_render_host
        _prol2_render_host = None
    if host is not None:
        host.shutdown(wait=wait)


def common_vst3_roots() -> list[Path]:
    """Return the app-local and standard VST3 plugin directories."""
    roots: list[Path] = [Path(__file__).resolve().parent / "plugins"]
    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        roots.append(Path(program_files) / "Common Files" / "VST3")
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        if program_files_x86:
            roots.append(Path(program_files_x86) / "Common Files" / "VST3")
    elif sys.platform == "darwin":
        roots.extend(
            [
                Path("/Library/Audio/Plug-Ins/VST3"),
                Path.home() / "Library" / "Audio" / "Plug-Ins" / "VST3",
            ]
        )
    else:
        roots.extend([Path.home() / ".vst3", Path("/usr/lib/vst3"), Path("/usr/local/lib/vst3")])
    return [p for p in roots if p.exists()]


# -----------------------------------------------------------------------------
# Cached plugin path (populated on first successful find, reused thereafter)
# -----------------------------------------------------------------------------

_cached_prol2_path: str | None = None


def find_prol2_plugin_path(configured_path: str | None = None) -> str:
    """Locate Pro-L 2 VST3: configured path > env > app-local/system roots.

    The result is cached after the first successful lookup so subsequent calls
    in the same process skip the filesystem scan.
    """
    global _cached_prol2_path

    # An explicit configured_path or env var always takes precedence and is not cached.
    if configured_path:
        p = Path(configured_path).expanduser()
        if p.exists():
            return str(p)
        raise RuntimeError(f"Configured Pro-L 2 plugin path does not exist: {p}")

    env_path = os.environ.get("PROL2_PLUGIN_PATH", "").strip()
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return str(p)
        raise RuntimeError(f"PROL2_PLUGIN_PATH does not exist: {p}")

    # Use cache for auto-found paths (no explicit override).
    if _cached_prol2_path is not None:
        return _cached_prol2_path

    patterns = (
        "*FabFilter*Pro-L*2*.vst3",
        "*FabFilter*Pro L*2*.vst3",
        "*Pro-L*2*.vst3",
        "*Pro L*2*.vst3",
    )
    matches: list[Path] = []
    for root in common_vst3_roots():
        for pattern in patterns:
            try:
                matches.extend(root.rglob(pattern))
            except Exception:
                continue
        if matches:
            break

    unique = sorted(
        set(matches),
        key=lambda p: (
            "pro-l 2" not in str(p).lower() and "pro-l2" not in str(p).lower(),
            len(str(p)),
            str(p),
        ),
    )
    if unique:
        _cached_prol2_path = str(unique[0])
        return _cached_prol2_path

    roots = "\n".join(f"  - {p}" for p in common_vst3_roots()) or "  - no VST3 roots found"
    raise RuntimeError(
        "Could not auto-find FabFilter Pro-L 2 VST3. Set PROL2_PLUGIN_PATH to the .vst3 path.\n"
        f"Searched VST3 roots:\n{roots}"
    )


_cached_loudmax_path: str | None = None


def find_loudmax_plugin_path(configured_path: str | None = None) -> str:
    """Locate LoudMax VST3: configured path > env > app-local/system roots.

    The result is cached after the first successful lookup so subsequent calls
    in the same process skip the filesystem scan.
    """
    global _cached_loudmax_path

    if configured_path:
        p = Path(configured_path).expanduser()
        if p.exists():
            return str(p)
        raise RuntimeError(f"Configured LoudMax plugin path does not exist: {p}")

    env_path = os.environ.get("LOUDMAX_PLUGIN_PATH", "").strip()
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return str(p)
        raise RuntimeError(f"LOUDMAX_PLUGIN_PATH does not exist: {p}")

    if _cached_loudmax_path is not None:
        return _cached_loudmax_path

    matches: list[Path] = []
    for root in common_vst3_roots():
        try:
            matches.extend(root.rglob("*LoudMax*.vst3"))
        except Exception:
            continue
        if matches:
            break

    unique = sorted(set(matches), key=lambda p: (len(str(p)), str(p)))
    if unique:
        _cached_loudmax_path = str(unique[0])
        return _cached_loudmax_path

    roots = "\n".join(f"  - {p}" for p in common_vst3_roots()) or "  - no VST3 roots found"
    raise RuntimeError(
        "Could not auto-find LoudMax VST3. Set LOUDMAX_PLUGIN_PATH to the .vst3 path.\n"
        f"Searched VST3 roots:\n{roots}"
    )


def plugin_parameter_names(plugin: Any) -> list[str]:
    """Safely read parameter names from a pedalboard VST3 plugin."""
    try:
        return sorted(str(k) for k in plugin.parameters.keys())
    except Exception:
        return []


def set_plugin_parameter(plugin: Any, name: str, candidates: Iterable[Any]) -> Any:
    """Try setting a VST3 parameter using a list of candidate value types."""
    names = plugin_parameter_names(plugin)
    if names and name not in names:
        raise RuntimeError(f"plugin parameter {name!r} was not exposed through pedalboard")

    last_exc: Exception | None = None
    tried: list[str] = []
    for candidate in candidates:
        tried.append(repr(candidate))
        try:
            setattr(plugin, name, candidate)
            try:
                return getattr(plugin, name)
            except Exception:
                return candidate
        except Exception as exc:
            last_exc = exc

    if last_exc is None:
        raise RuntimeError(f"Could not set plugin parameter {name!r}")

    short = ", ".join(tried[:10])
    if len(tried) > 10:
        short += f", ... ({len(tried)} candidates tried)"
    message = str(last_exc).splitlines()[0]
    raise RuntimeError(f"Could not set plugin parameter {name!r}. Tried {short}. Last error: {message}") from last_exc


def resolve_parameter_name(plugin: Any, candidates: Iterable[str]) -> str | None:
    """Resolve a logical parameter name to the exact name pedalboard exposes.

    Tries exact (case-insensitive) matches first, then falls back to
    alphanumeric-only comparison so names like "output_db" match "Output".
    """
    names = plugin_parameter_names(plugin)
    by_lower = {name.lower(): name for name in names}

    for candidate in candidates:
        exact = by_lower.get(candidate.lower())
        if exact is not None:
            return exact

    compact = {
        "".join(ch for ch in name.lower() if ch.isalnum()): name
        for name in names
    }
    for candidate in candidates:
        key = "".join(ch for ch in candidate.lower() if ch.isalnum())
        exact = compact.get(key)
        if exact is not None:
            return exact

    return None


def set_required_plugin_parameter(
    plugin: Any,
    *,
    plugin_label: str,
    logical_name: str,
    parameter_names: Iterable[str],
    candidates: Iterable[Any],
) -> Any:
    """Resolve and set a mandatory plugin parameter, failing closed if it is missing."""
    name = resolve_parameter_name(plugin, parameter_names)
    if name is None:
        exposed = ", ".join(plugin_parameter_names(plugin))
        raise RuntimeError(
            f"{plugin_label} {logical_name} parameter was not exposed through pedalboard. "
            f"Exposed parameters: {exposed}"
        )
    return set_plugin_parameter(plugin, name, candidates)


def prol2_numeric_candidates(value: float) -> list[Any]:
    """FabFilter accepts normal floats; avoid comma decimals used by Ozone."""
    if not math.isfinite(float(value)):
        value = 0.0
    rounded2 = round(float(value), 2)
    rounded1 = round(float(value), 1)
    candidates: list[Any] = []
    for candidate in (
        float(value),
        float(rounded2),
        float(rounded1),
        f"{rounded2:.2f} dB",
        f"{rounded1:.1f} dB",
        f"{rounded2:.2f}",
        f"{rounded1:.1f}",
    ):
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def prol2_bool_candidates(value: bool) -> list[Any]:
    """Return a list of candidate boolean representations for FabFilter parameters."""
    if value:
        return [True, 1, 1.0, "On", "ON", "on", "True", "true", "Enabled", "enabled", "Yes", "yes"]
    return [False, 0, 0.0, "Off", "OFF", "off", "False", "false", "Disabled", "disabled", "No", "no"]


def configure_prol2_for_gain(
    plugin: Any,
    *,
    gain_db: float,
    output_level_dbfs: float = PROL2_DEFAULT_OUTPUT_LEVEL_DBFS,
    true_peak: bool = PROL2_DEFAULT_TRUE_PEAK,
    oversampling: str | None = PROL2_DEFAULT_OVERSAMPLING,
    style: str | None = PROL2_DEFAULT_STYLE,
) -> None:
    """Configure a Pro-L 2 plugin instance for gain adjustment."""
    names = set(plugin_parameter_names(plugin))

    if "bypass" in names:
        set_plugin_parameter(plugin, "bypass", ["Not Bypassed", False, 0, "Off"])
    if "host_bypass" in names:
        set_plugin_parameter(plugin, "host_bypass", ["Not Bypassed", False, 0, "Off"])

    if style:
        if "style" not in names:
            raise RuntimeError("FabFilter Pro-L 2 style parameter was not exposed through pedalboard")

        actual_style = set_plugin_parameter(
            plugin,
            "style",
            [style, style.title(), style.lower(), style.upper()],
        )

        if str(actual_style).strip().lower() != str(style).strip().lower():
            raise RuntimeError(
                f"FabFilter Pro-L 2 style did not stick: requested {style!r}, got {actual_style!r}"
            )

    if oversampling and "oversampling" in names:
        set_plugin_parameter(
            plugin,
            "oversampling",
            [oversampling, oversampling.replace("x", "X"), oversampling.lower(), oversampling.upper()],
        )

    if "true_peak_limiting" not in names:
        raise RuntimeError("FabFilter Pro-L 2 true_peak_limiting parameter was not exposed through pedalboard")
    set_plugin_parameter(plugin, "true_peak_limiting", prol2_bool_candidates(true_peak))

    if "output_level" not in names:
        raise RuntimeError("FabFilter Pro-L 2 output_level parameter was not exposed through pedalboard")
    set_plugin_parameter(plugin, "output_level", prol2_numeric_candidates(output_level_dbfs))

    if "gain" not in names:
        raise RuntimeError("FabFilter Pro-L 2 gain parameter was not exposed through pedalboard")
    set_plugin_parameter(plugin, "gain", prol2_numeric_candidates(gain_db))


# LoudMax (build verified via pedalboard) exposes: bypass, fader_link,
# isp_detection, large_gui, output_db, thresh_db. Real names go first;
# the rest are fallbacks for other LoudMax builds/versions.
LOUDMAX_TRUE_PEAK_PARAMETER_NAMES = (
    "isp_detection",
    "isp",
    "true_peak",
    "true_peak_limiting",
)

LOUDMAX_OUTPUT_PARAMETER_NAMES = (
    "output_db",
    "output",
    "ceiling",
    "limit",
)

LOUDMAX_THRESHOLD_PARAMETER_NAMES = (
    "thresh_db",
    "threshold",
    "thresh",
)


def configure_loudmax_for_gain(
    plugin: Any,
    *,
    output_level_dbfs: float,
    true_peak: bool = True,
) -> None:
    """Configure a LoudMax plugin instance as a peak-safety limiter.

    DropGain applies compensated pre-gain before LoudMax; threshold stays
    neutral and output_db is the ceiling trim. True peak/ISP is mandatory.
    """
    names = set(plugin_parameter_names(plugin))

    if "bypass" in names:
        set_plugin_parameter(plugin, "bypass", ["Not Bypassed", False, 0, "Off"])
    if "fader_link" in names:
        set_plugin_parameter(plugin, "fader_link", prol2_bool_candidates(False))

    set_required_plugin_parameter(
        plugin,
        plugin_label="LoudMax",
        logical_name="true-peak/ISP",
        parameter_names=LOUDMAX_TRUE_PEAK_PARAMETER_NAMES,
        candidates=prol2_bool_candidates(true_peak),
    )

    set_required_plugin_parameter(
        plugin,
        plugin_label="LoudMax",
        logical_name="output/ceiling",
        parameter_names=LOUDMAX_OUTPUT_PARAMETER_NAMES,
        candidates=prol2_numeric_candidates(output_level_dbfs),
    )

    threshold_name = resolve_parameter_name(plugin, LOUDMAX_THRESHOLD_PARAMETER_NAMES)
    if threshold_name is not None:
        set_plugin_parameter(plugin, threshold_name, prol2_numeric_candidates(0.0))


def decode_audio_ffmpeg_at_sample_rate(path: str, channels: int, sample_rate: int) -> np.ndarray:
    """Decode audio via ffmpeg at the original sample rate for Pro-L 2 processing.

    Returns float32 (samples, channels) - pedalboard/VST3 expects 32-bit float
    PCM and does not benefit from float64 precision. This differs from
    analysis.decode_audio_ffmpeg which returns float64 for pyloudnorm accuracy.
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
        str(sample_rate),
        "pipe:1",
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_subprocess_kwargs())
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip() or "ffmpeg decode failed"
        raise RuntimeError(err)
    audio = np.frombuffer(result.stdout, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError("decoded audio is empty")
    remainder = audio.size % channels
    if remainder:
        audio = audio[: audio.size - remainder]
    if audio.size == 0:
        raise RuntimeError("decoded audio has invalid channel layout")
    return audio.reshape(-1, channels)


def audio_to_pedalboard_shape(audio: np.ndarray) -> np.ndarray:
    """Convert (samples, channels) or (samples,) to pedalboard's (channels, samples) float32."""
    if audio.ndim == 1:
        return audio[None, :].astype(np.float32, copy=False)
    return audio.T.astype(np.float32, copy=False)


def audio_from_pedalboard_shape(audio: np.ndarray, expected_channels: int, expected_samples: int) -> np.ndarray:
    """Convert pedalboard output back to (samples, channels) float32, padding or trimming if needed."""
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim == 1:
        out = arr[:, None]
    elif arr.ndim == 2 and arr.shape[0] == expected_channels:
        out = arr.T
    elif arr.ndim == 2 and arr.shape[1] == expected_channels:
        out = arr
    elif arr.ndim == 2:
        # Neither dimension matches expected_channels; transpose as best-effort
        # but warn so unexpected shapes are visible during debugging.
        logger.warning(
            "audio_from_pedalboard_shape: unexpected shape %s (expected_channels=%d); "
            "transposing as fallback",
            arr.shape, expected_channels,
        )
        out = arr.T
    else:
        raise RuntimeError(
            f"audio_from_pedalboard_shape: unsupported array shape {arr.shape} "
            f"(ndim={arr.ndim}, expected_channels={expected_channels})"
        )
    if out.shape[0] > expected_samples:
        out = out[:expected_samples]
    elif out.shape[0] < expected_samples:
        out = np.pad(out, ((0, expected_samples - out.shape[0]), (0, 0)), mode="constant")
    return out.astype(np.float32, copy=False)


def encode_float_audio_ffmpeg(
    audio: np.ndarray,
    input_path: str,
    output_path: str,
    source_info: dict[str, object],
    *,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
) -> None:
    """Encode processed float audio via ffmpeg, copying metadata from the original file."""
    ext = Path(output_path).suffix.lower()
    sample_rate = parse_int_or_default(source_info.get("sample_rate"), 0)
    channels = parse_int_or_default(source_info.get("channels"), 0)
    if sample_rate <= 0 or channels <= 0:
        raise RuntimeError("invalid sample rate or channel count for Pro-L 2 render")

    pioneer_compatible_aiff = requires_pioneer_compatible_aiff(output_format_mode, output_path)

    # Write via temp file and rename on success so interrupted ffmpeg runs leave no corrupt output.
    # Temp name keeps the real extension for muxer detection.
    base, ext_suffix = os.path.splitext(output_path)
    tmp_path = f"{base}.tmp{ext_suffix}"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-f",
        "f32le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-i",
        "pipe:0",
        "-i",
        input_path,
    ]

    if ext in {".wav", ".aiff"}:
        cmd.extend(["-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1"])
    else:
        cmd.extend(["-map", "0:a:0", "-map", "1:v?", "-map_metadata", "1", "-map_chapters", "1", "-c:v", "copy"])

    cmd.extend(
        encoder_args_for_output(
            ext,
            source_info,
            Path(input_path).suffix.lower(),
            pioneer_compatible_aiff=pioneer_compatible_aiff,
        )
    )
    cmd.append(tmp_path)

    pcm = np.ascontiguousarray(audio.astype(np.float32, copy=False)).tobytes()
    try:
        result = subprocess.run(cmd, input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_subprocess_kwargs())
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace").strip() or "ffmpeg encode failed"
            raise RuntimeError(err)
        replace_file_with_retries(tmp_path, output_path)
    except BaseException:
        # Clean up the temp file on any failure (including KeyboardInterrupt).
        try:
            remove_file_with_retries(tmp_path)
        except OSError:
            pass
        raise


def _process_audio_with_prol2_gain_impl(
    input_path: str,
    output_path: str,
    gain_db: float,
    source_info: dict[str, object],
    plugin_path: str | None = None,
    output_level_dbfs: float = PROL2_DEFAULT_OUTPUT_LEVEL_DBFS,
    true_peak: bool = PROL2_DEFAULT_TRUE_PEAK,
    oversampling: str | None = PROL2_DEFAULT_OVERSAMPLING,
    style: str | None = PROL2_DEFAULT_STYLE,
    post_loudness_start_sec: float | None = None,
    post_loudness_end_sec: float | None = None,
    post_target_high_lufs: float | None = None,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
) -> dict[str, object]:
    """Render audio through FabFilter Pro-L 2 on the dedicated host thread."""
    ext = Path(input_path).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise RuntimeError(f"processing not supported for {ext}")
    if os.path.exists(output_path) and not PROCESS_OVERWRITE_EXISTING:
        raise RuntimeError("output already exists")

    output_parent = os.path.dirname(os.path.abspath(output_path))
    if output_parent:
        os.makedirs(output_parent, exist_ok=True)

    try:
        from pedalboard import load_plugin
    except Exception as exc:
        raise RuntimeError("pedalboard is required for FabFilter Pro-L 2 processing") from exc

    sr = parse_int_or_default(source_info.get("sample_rate"), 0)
    channels = parse_int_or_default(source_info.get("channels"), 0)
    if sr <= 0 or channels <= 0:
        raise RuntimeError("invalid sample rate or channel count")

    with benchmark_timer("render"):
        audio = decode_audio_ffmpeg_at_sample_rate(input_path, channels, sr)
        compensated_drive_db = float(gain_db) - float(output_level_dbfs)
        pre_limiter_gain_db = min(compensated_drive_db, 0.0)
        plugin_gain_db = max(compensated_drive_db, 0.0)
        if pre_limiter_gain_db < -0.000001:
            audio = apply_linear_gain(audio, pre_limiter_gain_db)

        plugin_file = find_prol2_plugin_path(plugin_path or PROL2_DEFAULT_PLUGIN_PATH)
        plugin = load_plugin(plugin_file)
        configure_prol2_for_gain(
            plugin,
            gain_db=plugin_gain_db,
            output_level_dbfs=output_level_dbfs,
            true_peak=true_peak,
            oversampling=oversampling,
            style=style,
        )

        plugin_input = audio_to_pedalboard_shape(audio)
        # reset=True: pedalboard trims Pro-L 2 plugin latency, False leaves leading silence.
        plugin_output = plugin(plugin_input, float(sr), buffer_size=PROL2_PROCESS_BUFFER_SIZE, reset=True)
        processed = audio_from_pedalboard_shape(plugin_output, channels, audio.shape[0])
        del plugin

        post_trim_db = 0.0
        post_trim_note = ""
        if (
            post_target_high_lufs is not None
            and post_loudness_start_sec is not None
            and post_loudness_end_sec is not None
        ):
            try:
                rendered_section_lufs = section_lufs_from_audio(
                    processed,
                    sr,
                    post_loudness_start_sec,
                    post_loudness_end_sec,
                )
                if rendered_section_lufs > float(post_target_high_lufs) + POST_LIMITER_TRIM_EPSILON_LU:
                    post_trim_db = float(post_target_high_lufs) - rendered_section_lufs
                    processed = apply_linear_gain(processed, post_trim_db)
                    post_trim_note = (
                        f"post-limiter clean trim {post_trim_db:.2f} dB "
                        f"to keep loudest section within target"
                    )
            except Exception as exc:
                post_trim_note = f"post-limiter loudness trim skipped: {exc}"

        encode_float_audio_ffmpeg(
            processed,
            input_path,
            output_path,
            source_info,
            output_format_mode=output_format_mode,
        )

    output_info = finalize_processed_output(
        input_path,
        output_path,
        source_info,
        output_format_mode=output_format_mode,
    )
    output_info["_processing_engine"] = PROCESSING_ENGINE_PROL2
    output_info["_limiter_used"] = True
    output_info["_pre_limiter_gain_db"] = pre_limiter_gain_db
    output_info["_plugin_gain_db"] = plugin_gain_db
    output_info["_compensated_drive_db"] = compensated_drive_db
    output_info["_post_limiter_trim_db"] = post_trim_db
    output_info["_post_limiter_note"] = post_trim_note
    return output_info


def process_audio_with_prol2_gain(
    input_path: str,
    output_path: str,
    gain_db: float,
    source_info: dict[str, object],
    plugin_path: str | None = None,
    output_level_dbfs: float = PROL2_DEFAULT_OUTPUT_LEVEL_DBFS,
    true_peak: bool = PROL2_DEFAULT_TRUE_PEAK,
    oversampling: str | None = PROL2_DEFAULT_OVERSAMPLING,
    style: str | None = PROL2_DEFAULT_STYLE,
    post_loudness_start_sec: float | None = None,
    post_loudness_end_sec: float | None = None,
    post_target_high_lufs: float | None = None,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
) -> dict[str, object]:
    """Render audio through FabFilter Pro-L 2 and encode to the target format."""
    return get_prol2_render_host().run(
        _process_audio_with_prol2_gain_impl,
        input_path,
        output_path,
        gain_db,
        source_info,
        plugin_path,
        output_level_dbfs,
        true_peak,
        oversampling,
        style,
        post_loudness_start_sec,
        post_loudness_end_sec,
        post_target_high_lufs,
        output_format_mode,
    )


def _process_audio_with_loudmax_gain_impl(
    input_path: str,
    output_path: str,
    gain_db: float,
    source_info: dict[str, object],
    plugin_path: str | None = None,
    output_level_dbfs: float = PROL2_DEFAULT_OUTPUT_LEVEL_DBFS,
    post_loudness_start_sec: float | None = None,
    post_loudness_end_sec: float | None = None,
    post_target_high_lufs: float | None = None,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
) -> dict[str, object]:
    """Render audio through LoudMax on the dedicated host thread.

    LoudMax's output_db acts as a final ceiling trim, so external pre-gain
    uses compensated drive (gain_db - output_level_dbfs) plus a small
    LoudMax-only calibration offset. Threshold stays neutral; true-peak/ISP
    catches peaks above the ceiling.
    """
    ext = Path(input_path).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise RuntimeError(f"processing not supported for {ext}")
    if os.path.exists(output_path) and not PROCESS_OVERWRITE_EXISTING:
        raise RuntimeError("output already exists")

    output_parent = os.path.dirname(os.path.abspath(output_path))
    if output_parent:
        os.makedirs(output_parent, exist_ok=True)

    try:
        from pedalboard import load_plugin
    except Exception as exc:
        raise RuntimeError("pedalboard is required for LoudMax processing") from exc

    sr = parse_int_or_default(source_info.get("sample_rate"), 0)
    channels = parse_int_or_default(source_info.get("channels"), 0)
    if sr <= 0 or channels <= 0:
        raise RuntimeError("invalid sample rate or channel count")

    with benchmark_timer("render"):
        audio = decode_audio_ffmpeg_at_sample_rate(input_path, channels, sr)

        compensated_drive_db = (
            float(gain_db) - float(output_level_dbfs) + LOUDMAX_LIMITER_CALIBRATION_DB
        )
        if abs(compensated_drive_db) > 0.000001:
            audio = apply_linear_gain(audio, compensated_drive_db)

        plugin_file = find_loudmax_plugin_path(plugin_path or LOUDMAX_DEFAULT_PLUGIN_PATH)
        plugin = load_plugin(plugin_file)
        configure_loudmax_for_gain(
            plugin,
            output_level_dbfs=output_level_dbfs,
            true_peak=True,
        )

        plugin_input = audio_to_pedalboard_shape(audio)
        plugin_output = plugin(plugin_input, float(sr), buffer_size=LOUDMAX_PROCESS_BUFFER_SIZE, reset=False)
        processed = audio_from_pedalboard_shape(plugin_output, channels, audio.shape[0])
        del plugin

        post_trim_db = 0.0
        post_trim_note = ""
        if (
            post_target_high_lufs is not None
            and post_loudness_start_sec is not None
            and post_loudness_end_sec is not None
        ):
            try:
                rendered_section_lufs = section_lufs_from_audio(
                    processed,
                    sr,
                    post_loudness_start_sec,
                    post_loudness_end_sec,
                )
                if rendered_section_lufs > float(post_target_high_lufs) + POST_LIMITER_TRIM_EPSILON_LU:
                    post_trim_db = float(post_target_high_lufs) - rendered_section_lufs
                    processed = apply_linear_gain(processed, post_trim_db)
                    post_trim_note = (
                        f"post-limiter clean trim {post_trim_db:.2f} dB "
                        f"to keep loudest section within target"
                    )
            except Exception as exc:
                post_trim_note = f"post-limiter loudness trim skipped: {exc}"

        encode_float_audio_ffmpeg(
            processed,
            input_path,
            output_path,
            source_info,
            output_format_mode=output_format_mode,
        )

    output_info = finalize_processed_output(
        input_path,
        output_path,
        source_info,
        output_format_mode=output_format_mode,
    )
    output_info["_processing_engine"] = PROCESSING_ENGINE_LOUDMAX
    output_info["_limiter_used"] = True
    output_info["_pre_limiter_gain_db"] = compensated_drive_db
    output_info["_plugin_gain_db"] = 0.0
    output_info["_compensated_drive_db"] = compensated_drive_db
    output_info["_loudmax_calibration_db"] = LOUDMAX_LIMITER_CALIBRATION_DB
    output_info["_post_limiter_trim_db"] = post_trim_db
    output_info["_post_limiter_note"] = post_trim_note
    return output_info


def process_audio_with_loudmax_gain(
    input_path: str,
    output_path: str,
    gain_db: float,
    source_info: dict[str, object],
    plugin_path: str | None = None,
    output_level_dbfs: float = PROL2_DEFAULT_OUTPUT_LEVEL_DBFS,
    post_loudness_start_sec: float | None = None,
    post_loudness_end_sec: float | None = None,
    post_target_high_lufs: float | None = None,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
) -> dict[str, object]:
    """Render audio through LoudMax and encode to the target format."""
    return get_prol2_render_host().run(
        _process_audio_with_loudmax_gain_impl,
        input_path,
        output_path,
        gain_db,
        source_info,
        plugin_path,
        output_level_dbfs,
        post_loudness_start_sec,
        post_loudness_end_sec,
        post_target_high_lufs,
        output_format_mode,
    )


def process_audio_with_gain(
    input_path: str,
    output_path: str,
    gain_db: float,
    source_info: dict[str, object],
    output_level_dbfs: float = PROL2_DEFAULT_OUTPUT_LEVEL_DBFS,
    true_peak: bool = PROL2_DEFAULT_TRUE_PEAK,
    oversampling: str | None = PROL2_DEFAULT_OVERSAMPLING,
    style: str | None = PROL2_DEFAULT_STYLE,
    post_loudness_start_sec: float | None = None,
    post_loudness_end_sec: float | None = None,
    post_target_high_lufs: float | None = None,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
    limiter_engine: str = DEFAULT_LIMITER_ENGINE,
) -> dict[str, object]:
    """Limiter-assisted processing path, routed to the selected limiter engine.

    The GUI's peak-reference/output-level control is passed through here so
    analysis, reporting, and the actual render use the same value.
    """
    if normalize_limiter_engine(limiter_engine) == LIMITER_ENGINE_LOUDMAX:
        return process_audio_with_loudmax_gain(
            input_path=input_path,
            output_path=output_path,
            gain_db=gain_db,
            source_info=source_info,
            output_level_dbfs=output_level_dbfs,
            post_loudness_start_sec=post_loudness_start_sec,
            post_loudness_end_sec=post_loudness_end_sec,
            post_target_high_lufs=post_target_high_lufs,
            output_format_mode=output_format_mode,
        )

    return process_audio_with_prol2_gain(
        input_path=input_path,
        output_path=output_path,
        gain_db=gain_db,
        source_info=source_info,
        output_level_dbfs=output_level_dbfs,
        true_peak=true_peak,
        oversampling=oversampling,
        style=style,
        post_loudness_start_sec=post_loudness_start_sec,
        post_loudness_end_sec=post_loudness_end_sec,
        post_target_high_lufs=post_target_high_lufs,
        output_format_mode=output_format_mode,
    )


def row_needs_true_peak_safety_render(row: TrackRow, gain: float | None = None) -> bool:
    """Return True when a render is required to address true-peak safety."""
    gain_db = parse_float_or_default(row["suggested_gain_db"], 0.0) if gain is None else float(gain)
    true_peak_headroom = parse_float_or_default(row.get("true_peak_headroom_db"), 0.0)
    action = str(row.get("action", ""))
    return (
        true_peak_headroom < -0.01
        and (gain_db < -0.01 or "lower for true peak" in action)
    )


def is_zero_gain_mp3_render(row: TrackRow) -> bool:
    """Return True when an MP3 render would re-encode without a meaningful level change."""
    if str(row.get("extension", "")).lower() != ".mp3":
        return False

    gain = parse_float_or_default(row["suggested_gain_db"], 0.0)
    if abs(gain) >= EFFECTIVE_ZERO_GAIN_DB:
        return False
    if row_needs_true_peak_safety_render(row, gain):
        return False
    if row_should_use_limiter(row):
        return False
    if row_needs_final_mp3_peak_safety(row):
        return False

    output_ext = Path(str(row.get("output_path", ""))).suffix.lower()
    output_mode = normalize_output_format_mode(row.get("output_format_mode", DEFAULT_OUTPUT_FORMAT_MODE))

    if output_ext == ".mp3":
        return True

    if output_ext == ".aiff" and output_mode == OUTPUT_FORMAT_ALL_TO_AIFF:
        return False

    return True


def should_process_row(
    row: TrackRow,
    mp3_threshold: float | None = None,
    lossless_threshold: float | None = None,
    allow_risky_true_peak_boost: bool = False,
    apply_gain_threshold: bool = True,
) -> tuple[bool, str]:
    """Decide whether a track needs rendering.

    Safety skips always apply. Gain-threshold skips apply only when
    apply_gain_threshold is True.
    """
    path = row["path"]
    ext = row["extension"].lower()
    gain = parse_float_or_default(row["suggested_gain_db"], 0.0)
    output_path = row["output_path"]

    if ext not in SUPPORTED_EXTENSIONS:
        return False, "unsupported_format"

    if (
        str(row.get("manual_check_required", "")).strip().lower() == "yes"
        and not allow_risky_true_peak_boost
    ):
        return False, "needs_manual_check"

    if is_zero_gain_mp3_render(row):
        return False, "zero_gain_mp3_render_skipped"

    needs_true_peak_safety = row_needs_true_peak_safety_render(row, gain)
    needs_limiter_peak_control = row_should_use_limiter(row)
    needs_final_mp3_peak_safety = row_needs_final_mp3_peak_safety(row)

    if apply_gain_threshold:
        min_gain = min_abs_gain_for_extension(
            ext, mp3_threshold=mp3_threshold, lossless_threshold=lossless_threshold
        )
        if (
            abs(gain) < min_gain
            and not needs_true_peak_safety
            and not needs_limiter_peak_control
            and not needs_final_mp3_peak_safety
        ):
            if str(row.get("action", "")) == "leave":
                return False, "already_in_target_range"
            if ext == ".mp3":
                return False, "mp3_gain_below_threshold"
            return False, "lossless_gain_below_threshold"

    if not os.path.exists(path):
        return False, "missing_original"

    if os.path.exists(output_path) and not PROCESS_OVERWRITE_EXISTING:
        size = os.path.getsize(output_path)
        if size >= MIN_OUTPUT_FILE_BYTES:
            return False, "output_exists"
        logger.warning(
            "output %s is only %d bytes (below %d), treating as corrupt and re-processing",
            output_path, size, MIN_OUTPUT_FILE_BYTES,
        )
        try:
            os.remove(output_path)
        except OSError as exc:
            logger.warning("failed to remove corrupt output %s: %s", output_path, exc)

    return True, "will_process"


def verify_processed_audio_fast(
    row: TrackRow,
    output_path: str,
    peak_ceiling_dbfs: float | None = None,
    accept_mp3_true_peak_issues: bool = False,
    post_loudness_window_seconds: float | None = None,
    post_loudness_hop_seconds: float | None = None,
    output_info: dict[str, object] | None = None,
) -> tuple[str, str]:
    """Measure output loudness and verify same-section plus re-scanned loudest section."""
    if not POST_VERIFY_PROCESSED_AUDIO:
        return "skipped", ""

    if output_info is None:
        with benchmark_timer("output ffprobe"):
            output_info = ffprobe_audio_info(output_path)
    channels = int(output_info["channels"])
    with benchmark_timer("output decode"):
        output_audio = decode_audio_ffmpeg(output_path, channels)

    output_peak = float(np.max(np.abs(output_audio)))
    output_peak_dbfs = dbfs(output_peak)

    meter = pyln.Meter(METER_SAMPLE_RATE)
    meter_input = audio_for_loudness_meter(output_audio)
    with benchmark_timer("output integrated LUFS"):
        output_integrated = measure_lufs_input(meter, meter_input)

    start_sec = parse_float_or_default(row["loudest_section_start_sec"], 0.0)
    end_sec = parse_float_or_default(row["loudest_section_end_sec"], 0.0)

    total_samples = int(output_audio.shape[0])
    start_sample = max(0, int(round(start_sec * METER_SAMPLE_RATE)))
    if total_samples <= 0:
        raise RuntimeError("decoded output audio is empty")
    if start_sample >= total_samples:
        start_sample = max(0, total_samples - 1)
    end_sample = max(start_sample + 1, int(round(end_sec * METER_SAMPLE_RATE)))
    end_sample = min(end_sample, total_samples)

    same_section = meter_input[start_sample:end_sample]
    if same_section.size == 0:
        raise RuntimeError("post-render analyzed-section window is empty")
    with benchmark_timer("output same-section LUFS"):
        output_same_section_lufs = measure_lufs_input(meter, same_section)

    output_loudest_section_lufs: float | None = None
    output_loudest_section_start_sec: float | None = None
    output_loudest_section_end_sec: float | None = None
    output_loudest_rescan_note = ""

    try:
        if post_loudness_window_seconds is None:
            rescan_window_seconds = max(1.0, end_sec - start_sec)
        else:
            rescan_window_seconds = max(1.0, float(post_loudness_window_seconds))

        if post_loudness_hop_seconds is None:
            rescan_hop_seconds = max(1.0, min(rescan_window_seconds, rescan_window_seconds / 3.0))
        else:
            rescan_hop_seconds = max(1.0, float(post_loudness_hop_seconds))

        with benchmark_timer("output loudest-section scan"):
            (
                output_loudest_section_lufs,
                output_loudest_section_start_sec,
                output_loudest_section_end_sec,
            ) = loudest_section_lufs(
                audio=output_audio,
                meter=meter,
                window_seconds=rescan_window_seconds,
                hop_seconds=rescan_hop_seconds,
                meter_input=meter_input,
            )
    except Exception as exc:
        output_loudest_rescan_note = f"post-render loudest-section rescan failed: {exc}"

    output_true_peak_dbtp: float | None = None
    with benchmark_timer("output true peak"):
        output_section_true_peak, output_whole_true_peak, true_peak_note = measure_section_and_whole_true_peak_oversampled(
            output_path,
            start_sec,
            end_sec,
            channels=channels,
            sample_rate=parse_int_or_default(output_info["sample_rate"], METER_SAMPLE_RATE),
            section_failure_label="output section true peak measurement failed",
            whole_failure_label="output whole-track true peak measurement failed",
        )

    output_true_peak_measurements = [
        measurement
        for measurement in (output_section_true_peak, output_whole_true_peak)
        if measurement is not None
    ]
    if output_true_peak_measurements:
        output_true_peak_dbtp = max(output_true_peak_measurements)

    row["output_integrated_lufs"] = round_or_blank(output_integrated, 2)
    row["output_same_section_lufs"] = round_or_blank(output_same_section_lufs, 2)
    row["output_sample_peak_dbfs"] = round_or_blank(output_peak_dbfs, 2)
    row["output_true_peak_dbtp"] = round_or_blank(output_true_peak_dbtp, 2)

    input_loudest = parse_float_or_default(row["loudest_section_lufs"], 0.0)
    input_peak = parse_float_or_default(row["sample_peak_dbfs"], 0.0)
    input_true_peak = parse_float_or_default(row["true_peak_dbtp"], input_peak)

    projected_loudest = parse_optional_float(row.get("projected_loudest_section_lufs"))
    actual_same_section_gain = output_same_section_lufs - input_loudest
    actual_peak_gain = output_peak_dbfs - input_peak

    row["actual_same_section_gain_db"] = round_or_blank(actual_same_section_gain, 2)
    row["actual_peak_gain_db"] = round_or_blank(actual_peak_gain, 2)
    if output_true_peak_dbtp is not None:
        row["actual_true_peak_gain_db"] = round_or_blank(output_true_peak_dbtp - input_true_peak, 2)
    else:
        row["actual_true_peak_gain_db"] = ""

    notes: list[str] = []

    if output_loudest_rescan_note:
        notes.append(output_loudest_rescan_note)

    if true_peak_note:
        notes.append(true_peak_note)

    if (
        peak_ceiling_dbfs is not None
        and output_true_peak_dbtp is not None
        and not accept_mp3_true_peak_issues
    ):
        allowed_true_peak = float(peak_ceiling_dbfs) + POST_VERIFY_PEAK_TOLERANCE_DB
        if output_true_peak_dbtp > allowed_true_peak:
            notes.append(
                f"output true peak {output_true_peak_dbtp:.2f} dBTP exceeds "
                f"ceiling {float(peak_ceiling_dbfs):.1f} dBTP by "
                f"{output_true_peak_dbtp - float(peak_ceiling_dbfs):.2f} dB"
            )

    target_low = parse_float_or_default(row.get("target_low_lufs"), -999.0)
    target_high = parse_float_or_default(row.get("target_high_lufs"), 999.0)
    effective_low = min(target_low, projected_loudest) if projected_loudest is not None else target_low
    effective_high = max(target_high, projected_loudest) if projected_loudest is not None else target_high

    if output_same_section_lufs < effective_low - POST_VERIFY_LUFS_TOLERANCE:
        notes.append(
            f"output analyzed section {output_same_section_lufs:.2f} LUFS is below "
            f"expected {effective_low:.2f} LUFS"
        )
    elif output_same_section_lufs > effective_high + POST_VERIFY_LUFS_TOLERANCE:
        notes.append(
            f"output analyzed section {output_same_section_lufs:.2f} LUFS is above "
            f"expected {effective_high:.2f} LUFS"
        )

    if output_loudest_section_lufs is not None:
        rescan_matches_same_section = (
            abs(output_loudest_section_lufs - output_same_section_lufs)
            <= POST_VERIFY_LUFS_TOLERANCE
        )
        if not rescan_matches_same_section:
            section_range = ""
            if (
                output_loudest_section_start_sec is not None
                and output_loudest_section_end_sec is not None
            ):
                section_range = (
                    f" at {output_loudest_section_start_sec:.1f}-"
                    f"{output_loudest_section_end_sec:.1f}s"
                )

            if output_loudest_section_lufs < effective_low - POST_VERIFY_LUFS_TOLERANCE:
                notes.append(
                    f"output re-scanned loudest section{section_range} "
                    f"{output_loudest_section_lufs:.2f} LUFS is below "
                    f"expected {effective_low:.2f} LUFS"
                )
            elif output_loudest_section_lufs > effective_high + POST_VERIFY_LUFS_TOLERANCE:
                notes.append(
                    f"output re-scanned loudest section{section_range} "
                    f"{output_loudest_section_lufs:.2f} LUFS is above "
                    f"expected {effective_high:.2f} LUFS"
                )

    if notes:
        return "warning", "; ".join(notes)

    return "ok", ""


def render_analyzed_row(
    row: TrackRow,
    *,
    input_path: str,
    source_info: dict[str, object],
    peak_ceiling_dbfs: float,
    target_high_lufs: float,
    limiter_engine: str = DEFAULT_LIMITER_ENGINE,
    post_loudness_window_seconds: float | None = None,
    post_loudness_hop_seconds: float | None = None,
) -> tuple[str, str, str, str]:
    """Render one analyzed row using cached source metadata.

    The row is updated in place with output metrics, verification fields,
    processing status, and retry-adjusted gain fields when MP3 safety retries
    are needed. Returns verification status and message pairs for metadata and
    audio checks.
    """
    output_path = row["output_path"]
    gain = parse_float_or_default(row["suggested_gain_db"], 0.0)
    output_format_mode = row.get("output_format_mode", DEFAULT_OUTPUT_FORMAT_MODE)
    use_limiter = row_should_use_limiter(row)
    row["processing_engine"] = processing_engine_for_limiter(limiter_engine) if use_limiter else PROCESSING_ENGINE_CLEAN_GAIN
    base_warnings = str(row.get("warnings") or "").strip()
    render_extras: list[str] = []

    def finalize_render_attempt(output_info: dict[str, object]) -> tuple[str, str, str, str]:
        return finalize_processed_render(
            row,
            input_path=input_path,
            output_path=output_path,
            output_info=output_info,
            base_warnings=base_warnings,
            render_extras=render_extras,
            peak_ceiling_dbfs=peak_ceiling_dbfs,
            post_loudness_window_seconds=post_loudness_window_seconds,
            post_loudness_hop_seconds=post_loudness_hop_seconds,
        )

    preserved_mp3_output = is_preserved_source_mp3_output(row)

    if use_limiter:
        mp3_margin = 0.0 if preserved_mp3_output else initial_mp3_limiter_margin_db(output_path)
        output_info = process_audio_with_gain(
            input_path=input_path,
            output_path=output_path,
            gain_db=gain,
            source_info=source_info,
            output_level_dbfs=limiter_render_output_level_dbfs(
                output_path,
                peak_ceiling_dbfs,
                mp3_margin_db=mp3_margin,
            ),
            post_loudness_start_sec=parse_float_or_default(row["loudest_section_start_sec"], 0.0),
            post_loudness_end_sec=parse_float_or_default(row["loudest_section_end_sec"], 0.0),
            post_target_high_lufs=target_high_lufs,
            output_format_mode=output_format_mode,
            limiter_engine=limiter_engine,
        )
        metadata_status, metadata_message, audio_status, audio_message = finalize_render_attempt(output_info)

        retry_margin = None if preserved_mp3_output else adjusted_mp3_limiter_margin_db(
            output_path,
            mp3_margin,
            row.get("output_true_peak_dbtp"),
            peak_ceiling_dbfs,
        )
        if retry_margin is not None:
            remove_file_with_retries(output_path)
            render_extras.append(
                f"MP3 true-peak retry used {retry_margin:.2f} dB render margin"
            )
            output_info = process_audio_with_gain(
                input_path=input_path,
                output_path=output_path,
                gain_db=gain,
                source_info=source_info,
                output_level_dbfs=limiter_render_output_level_dbfs(
                    output_path,
                    peak_ceiling_dbfs,
                    mp3_margin_db=retry_margin,
                ),
                post_loudness_start_sec=parse_float_or_default(row["loudest_section_start_sec"], 0.0),
                post_loudness_end_sec=parse_float_or_default(row["loudest_section_end_sec"], 0.0),
                post_target_high_lufs=target_high_lufs,
                output_format_mode=output_format_mode,
                limiter_engine=limiter_engine,
            )
            metadata_status, metadata_message, audio_status, audio_message = finalize_render_attempt(output_info)
    else:
        output_info = process_audio_with_clean_gain(
            input_path=input_path,
            output_path=output_path,
            gain_db=gain,
            source_info=source_info,
            output_format_mode=output_format_mode,
        )
        metadata_status, metadata_message, audio_status, audio_message = finalize_render_attempt(output_info)

        retry_gain = None if preserved_mp3_output else adjusted_mp3_clean_gain_db(
            output_path,
            gain,
            row.get("output_true_peak_dbtp"),
            peak_ceiling_dbfs,
        )
        if retry_gain is not None:
            next_gain, correction = retry_gain
            remove_file_with_retries(output_path)
            render_extras.append(
                f"MP3 clean-gain true-peak retry applied -{correction:.2f} dB correction after encode"
            )
            update_clean_gain_projection_fields(row, next_gain, peak_ceiling_dbfs)
            gain = next_gain
            output_info = process_audio_with_clean_gain(
                input_path=input_path,
                output_path=output_path,
                gain_db=gain,
                source_info=source_info,
                output_format_mode=output_format_mode,
            )
            metadata_status, metadata_message, audio_status, audio_message = finalize_render_attempt(output_info)

    if audio_status == "warning" or metadata_status == "warning":
        row["processing_status"] = "processed_warning"
    else:
        row["processing_status"] = "processed"

    row["processing_error"] = ""
    return metadata_status, metadata_message, audio_status, audio_message


def process_track(
    path: str,
    target_low: float,
    target_high: float,
    window_seconds: float,
    hop_seconds: float,
    max_reduction: float,
    bass_max_reduction: float,
    peak_ceiling: float,
    normalization_mode: str,
    analyze_only: bool,
    mp3_threshold: float,
    lossless_threshold: float,
    output_format_mode: object = DEFAULT_OUTPUT_FORMAT_MODE,
    allow_risky_true_peak_boost: bool = False,
    source_info: dict[str, object] | None = None,
    apply_gain_threshold: bool = True,
    output_root: str | None = None,
    source_root: str | None = None,
    source_folder_name: str = "",
    limiter_engine: str = DEFAULT_LIMITER_ENGINE,
    bass_penalty_start_db: float = DEFAULT_BASS_PENALTY_START_DB,
    bass_penalty_full_db: float = DEFAULT_BASS_PENALTY_FULL_DB,
    sub_penalty_start_db: float = DEFAULT_SUB_PENALTY_START_DB,
    sub_penalty_full_db: float = DEFAULT_SUB_PENALTY_FULL_DB,
) -> tuple[TrackRow | None, str, dict[str, object] | None]:
    """Analyze one track, optionally render it, and return row, error, source info.

    When source_info is supplied, it is reused for analysis and rendering.
    Otherwise the source is probed once and the resolved metadata is returned so
    batch rendering can avoid a second probe if the file is unchanged.
    """
    from analysis import (
        analyze_file,
        ffprobe_audio_info,
        infer_bit_depth,
        parse_float_or_default,
        round_or_blank,
    )

    row: TrackRow | None = None
    resolved_source_info: dict[str, object] | None = None

    try:
        resolved_source_info = source_info if source_info is not None else ffprobe_audio_info(path)

        row = analyze_file(
            path=path,
            target_low=target_low,
            target_high=target_high,
            loud_window_seconds=window_seconds,
            loud_hop_seconds=hop_seconds,
            max_reduction_db=max_reduction,
            peak_ceiling_dbfs=peak_ceiling,
            bass_max_reduction_db=bass_max_reduction,
            bass_penalty_start_db=bass_penalty_start_db,
            bass_penalty_full_db=bass_penalty_full_db,
            sub_penalty_start_db=sub_penalty_start_db,
            sub_penalty_full_db=sub_penalty_full_db,
            normalization_mode=normalization_mode,
            source_info=resolved_source_info,
            output_format_mode=output_format_mode,
            allow_risky_true_peak_boost=allow_risky_true_peak_boost,
            output_root=output_root,
            source_root=source_root,
            source_folder_name=source_folder_name,
            limiter_engine=limiter_engine,
        )

        use_limiter = row_should_use_limiter(row)
        row["processing_engine"] = processing_engine_for_limiter(limiter_engine) if use_limiter else PROCESSING_ENGINE_CLEAN_GAIN

        should_process, status = should_process_row(
            row,
            mp3_threshold=mp3_threshold,
            lossless_threshold=lossless_threshold,
            allow_risky_true_peak_boost=allow_risky_true_peak_boost,
            apply_gain_threshold=apply_gain_threshold,
        )

        if analyze_only:
            if should_process:
                row["processing_status"] = "analyzed_would_process"
            else:
                row["processing_status"] = f"analyzed_{status}"

            row["processing_error"] = ""
            row["audio_verification"] = "not_applicable"
            row["metadata_verification"] = "not_applicable"

        elif should_process:
            render_analyzed_row(
                row,
                input_path=path,
                source_info=resolved_source_info,
                peak_ceiling_dbfs=peak_ceiling,
                target_high_lufs=target_high,
                limiter_engine=limiter_engine,
                post_loudness_window_seconds=window_seconds,
                post_loudness_hop_seconds=hop_seconds,
            )

        else:
            row["processing_status"] = status
            row["processing_error"] = ""
            row["audio_verification"] = "not_applicable"
            row["metadata_verification"] = "not_applicable"

        return row, "", resolved_source_info

    except Exception as exc:
        if row is not None:
            row["processing_status"] = "error"
            row["processing_error"] = str(exc)
        return row, str(exc), resolved_source_info
