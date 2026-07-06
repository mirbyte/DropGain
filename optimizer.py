"""Pure backend library profiling and settings recommendation for DropGain."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np

from analysis import (
    PEAK_CONTROL_SEVERITY_HEAVY,
    TrackRow,
    collect_library_row_stats,
    is_limiter_processing_engine,
    parse_float_or_default,
    parse_optional_float,
)
from jobs import DropGainSettings, recompute_row_decision

TARGET_BAND_MIN = -12.0
TARGET_BAND_MAX = -5.0
TARGET_BAND_WIDTH = 1.0
CLEAN_ACHIEVABLE_PERCENTILE = 40.0
HEAVY_LIMITER_MAX_RATIO = 0.10
HEAVY_LIMITER_MAX_ABS = 2
REFINE_STEP_DB = 0.5
TRUSTWORTHY_SAMPLE_MIN = 20
TRUSTWORTHY_SAMPLE_MED = 50


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.array(values, dtype=np.float64), pct))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(np.median(np.array(values, dtype=np.float64)))


@dataclass
class LibraryProfile:
    track_count: int
    median_loudest_lufs: float | None
    p10_loudest_lufs: float | None
    p75_loudest_lufs: float | None
    p90_loudest_lufs: float | None
    median_suggested_gain_db: float | None
    would_render_count: int
    manual_check_count: int
    heavy_limiter_count: int
    mp3_count: int
    lossless_count: int
    limiter_severities: dict[str, int] = field(default_factory=dict)
    gain_values: list[float] = field(default_factory=list)
    loudest_values: list[float] = field(default_factory=list)


@dataclass
class PeakControlStats:
    limiter_track_count: int
    median_peak_control_db: float | None
    p90_peak_control_db: float | None
    budget_clamp_count: int


@dataclass
class RecommendedSettings:
    target_low_lufs: float
    target_high_lufs: float
    window_seconds: float
    hop_seconds: float
    peak_ceiling_dbfs: float
    max_reduction_db: float
    mp3_threshold: float
    lossless_threshold: float
    confidence: str
    notes: list[str] = field(default_factory=list)
    projected_would_render_count: int = 0
    projected_heavy_limiter_count: int = 0


def _row_uses_limiter(row: TrackRow) -> bool:
    return is_limiter_processing_engine(row.get("processing_engine", ""))


def peak_control_stats(rows: list[TrackRow]) -> PeakControlStats:
    """Summarize estimated limiter depth and budget clamping for analyzed rows."""
    peak_values: list[float] = []
    budget_clamp_count = 0
    for row in rows:
        uses_limiter = _row_uses_limiter(row)
        peak_control = parse_optional_float(row.get("estimated_peak_control_db", ""))
        if uses_limiter and peak_control is not None and peak_control > 0.01:
            peak_values.append(peak_control)
        if uses_limiter:
            adjustment = parse_optional_float(row.get("limiter_budget_adjustment_db", ""))
            if adjustment is not None and adjustment > 0.01:
                budget_clamp_count += 1
    return PeakControlStats(
        limiter_track_count=len(peak_values),
        median_peak_control_db=_median(peak_values),
        p90_peak_control_db=_percentile(peak_values, 90),
        budget_clamp_count=budget_clamp_count,
    )


def format_peak_control_diagnostics(
    stats: PeakControlStats,
    max_reduction_db: float,
) -> str:
    parts: list[str] = []
    if stats.limiter_track_count > 0 and stats.median_peak_control_db is not None:
        median = stats.median_peak_control_db
        p90 = stats.p90_peak_control_db
        if p90 is not None:
            parts.append(
                f"Peak control needed (limiter tracks): median {median:.1f} dB, p90 {p90:.1f} dB."
            )
        else:
            parts.append(f"Peak control needed (limiter tracks): median {median:.1f} dB.")
    else:
        parts.append("No limiter peak control needed at this preview.")
    parts.append(f"Limiter budget: {max_reduction_db:.1f} dB.")
    if stats.budget_clamp_count > 0:
        noun = "track" if stats.budget_clamp_count == 1 else "tracks"
        parts.append(f"{stats.budget_clamp_count} {noun} had gain reduced due to budget.")
    return " ".join(parts)


def build_library_profile(rows: list[TrackRow]) -> LibraryProfile:
    stats = collect_library_row_stats(rows)
    loudest = stats.loudest_values
    return LibraryProfile(
        track_count=stats.track_count,
        median_loudest_lufs=_median(loudest),
        p10_loudest_lufs=_percentile(loudest, 10),
        p75_loudest_lufs=_percentile(loudest, 75),
        p90_loudest_lufs=_percentile(loudest, 90),
        median_suggested_gain_db=_median(stats.gain_values),
        would_render_count=stats.would_process,
        manual_check_count=stats.manual_check_count,
        heavy_limiter_count=stats.heavy_limiter_control_count,
        mp3_count=stats.mp3_count,
        lossless_count=stats.lossless_count,
        limiter_severities=dict(stats.limiter_severities),
        gain_values=list(stats.gain_values),
        loudest_values=list(loudest),
    )


def _confidence(track_count: int, p10: float | None, p90: float | None) -> str:
    if track_count < TRUSTWORTHY_SAMPLE_MIN:
        return "Low"
    if p10 is None or p90 is None:
        return "Low"
    spread = p90 - p10
    if track_count < TRUSTWORTHY_SAMPLE_MED and spread > 2.0:
        return "Low"
    if spread <= 2.0:
        return "High"
    if spread <= 4.0:
        return "Medium"
    return "Low"


def _row_is_trustworthy_for_recommendation(row: TrackRow) -> bool:
    if str(row.get("manual_check_required", "")).strip().lower() == "yes":
        return False
    if str(row.get("true_peak_unreliable", "")).strip().lower() == "yes":
        return False
    status = str(row.get("processing_status", "")).lower()
    if status.startswith("analyzed_error") or status.startswith("error"):
        return False
    return True


def _trustworthy_loudest_values(rows: list[TrackRow]) -> list[float]:
    values: list[float] = []
    for row in rows:
        if not _row_is_trustworthy_for_recommendation(row):
            continue
        loudest = parse_optional_float(row.get("loudest_section_lufs", ""))
        if loudest is None:
            continue
        values.append(loudest)
    return values


def _trustworthy_measurements(rows: list[TrackRow]) -> list[tuple[float, float]]:
    """Return (loudest_section_lufs, true_peak_dbtp) pairs for recommendation."""
    values: list[tuple[float, float]] = []
    for row in rows:
        if not _row_is_trustworthy_for_recommendation(row):
            continue
        loudest = parse_optional_float(row.get("loudest_section_lufs", ""))
        if loudest is None:
            continue
        true_peak = parse_optional_float(row.get("true_peak_dbtp", ""))
        if true_peak is None:
            true_peak = parse_optional_float(row.get("sample_peak_dbfs", ""))
        if true_peak is None:
            continue
        values.append((loudest, true_peak))
    return values


def _clean_max_lufs(loudest_lufs: float, true_peak_dbtp: float, peak_ceiling_dbfs: float) -> float:
    """Loudest-section LUFS reachable without whole-track limiting."""
    headroom = peak_ceiling_dbfs - true_peak_dbtp
    return loudest_lufs + max(0.0, headroom)


def _clamp_target_band(target_low: float, target_high: float) -> tuple[float, float]:
    if target_low > target_high:
        target_low, target_high = target_high, target_low
    if target_high - target_low < 0.5:
        target_high = round(target_low + TARGET_BAND_WIDTH, 1)
    target_low = max(TARGET_BAND_MIN, target_low)
    target_high = min(TARGET_BAND_MAX, target_high)
    if target_low > target_high:
        target_low = target_high
    if target_high - target_low < 0.5:
        target_high = min(TARGET_BAND_MAX, round(target_low + TARGET_BAND_WIDTH, 1))
    return target_low, target_high


def _band_from_center(center: float, *, width: float = TARGET_BAND_WIDTH) -> tuple[float, float]:
    half = width / 2.0
    return _clamp_target_band(round(center - half, 1), round(center + half, 1))


def _candidate_target_band(
    rows: list[TrackRow],
    *,
    peak_ceiling_dbfs: float,
    loudest_fallback: list[float] | None = None,
) -> tuple[float, float]:
    """Pick a 1 dB target band anchored to the library median and headroom."""
    measurements = _trustworthy_measurements(rows)
    if not measurements:
        if loudest_fallback:
            median_loudest = _percentile(loudest_fallback, 50)
            if median_loudest is not None:
                return _band_from_center(median_loudest)
        return -8.0, -7.0

    loudest_values = [loudest for loudest, _ in measurements]
    clean_max_values = [
        _clean_max_lufs(loudest, true_peak, peak_ceiling_dbfs)
        for loudest, true_peak in measurements
    ]

    median_loudest = _percentile(loudest_values, 50) or -8.0
    safe_ceiling = _percentile(clean_max_values, CLEAN_ACHIEVABLE_PERCENTILE)
    center = median_loudest
    if safe_ceiling is not None:
        center = min(center, safe_ceiling - (TARGET_BAND_WIDTH / 2.0))

    return _band_from_center(center)


def _settings_with_targets(
    current: DropGainSettings,
    target_low_lufs: float,
    target_high_lufs: float,
) -> DropGainSettings:
    return DropGainSettings(
        folder=current.folder,
        csv_path=current.csv_path,
        target_low_lufs=target_low_lufs,
        target_high_lufs=target_high_lufs,
        window_seconds=current.window_seconds,
        hop_seconds=current.hop_seconds,
        max_reduction_db=current.max_reduction_db,
        peak_ceiling_dbfs=current.peak_ceiling_dbfs,
        normalization_mode=current.normalization_mode,
        limiter_engine=current.limiter_engine,
        analysis_workers=current.analysis_workers,
        render_workers=current.render_workers,
        analyze_only=True,
        write_csv=current.write_csv,
        mp3_threshold=current.mp3_threshold,
        lossless_threshold=current.lossless_threshold,
        output_format_mode=current.output_format_mode,
        allow_risky_true_peak_boost=current.allow_risky_true_peak_boost,
        apply_render_gain_threshold=current.apply_render_gain_threshold,
    )


def _refine_target_band_for_impact(
    rows: list[TrackRow],
    current: DropGainSettings,
    target_low: float,
    target_high: float,
) -> tuple[float, float]:
    """Lower the target center until projected heavy limiting stays within budget."""
    if not rows:
        return target_low, target_high

    max_heavy = max(
        HEAVY_LIMITER_MAX_ABS,
        int(round(len(rows) * HEAVY_LIMITER_MAX_RATIO)),
    )
    width = max(TARGET_BAND_WIDTH, target_high - target_low)
    center = (target_low + target_high) / 2.0
    floor_center = TARGET_BAND_MIN + (width / 2.0)
    best_low, best_high = target_low, target_high

    while center >= floor_center:
        candidate_low, candidate_high = _band_from_center(center, width=width)
        _, heavy = _project_impact(rows, _settings_with_targets(current, candidate_low, candidate_high))
        best_low, best_high = candidate_low, candidate_high
        if heavy <= max_heavy:
            return best_low, best_high
        center -= REFINE_STEP_DB

    return best_low, best_high


def _project_impact(
    rows: list[TrackRow],
    candidate: DropGainSettings,
) -> tuple[int, int]:
    would_render = 0
    heavy_limiter = 0
    for source in rows:
        row = copy.deepcopy(source)
        recompute_row_decision(
            candidate,
            row,
            apply_gain_threshold=candidate.apply_render_gain_threshold,
        )
        status = str(row.get("processing_status", ""))
        if status == "analyzed_would_process":
            would_render += 1
        severity = str(row.get("peak_control_severity", ""))
        peak_control = parse_float_or_default(row.get("estimated_peak_control_db", ""), 0.0)
        if (
            severity == PEAK_CONTROL_SEVERITY_HEAVY
            and peak_control > 0.01
            and _row_uses_limiter(row)
        ):
            heavy_limiter += 1
    return would_render, heavy_limiter


def recommend_settings(rows: list[TrackRow], current: DropGainSettings) -> RecommendedSettings:
    profile = build_library_profile(rows)
    trustworthy_loudest = _trustworthy_loudest_values(rows)
    recommendation_loudest = trustworthy_loudest or profile.loudest_values
    target_low, target_high = _candidate_target_band(
        rows,
        peak_ceiling_dbfs=current.peak_ceiling_dbfs,
        loudest_fallback=recommendation_loudest,
    )
    initial_low, initial_high = target_low, target_high
    target_low, target_high = _refine_target_band_for_impact(rows, current, target_low, target_high)
    confidence = _confidence(
        len(recommendation_loudest),
        _percentile(recommendation_loudest, 10),
        _percentile(recommendation_loudest, 90),
    )

    notes: list[str] = []
    if recommendation_loudest:
        notes.append(
            "Target anchored to library median loudest section, capped by true-peak headroom."
        )
        below_target = sum(
            1
            for value in recommendation_loudest
            if value < target_low - 1.0
        )
        pct = 100.0 * below_target / len(recommendation_loudest)
        notes.append(f"{pct:.0f}% of tracks fall more than 1 dB below the recommended target low.")
        rec_p10 = _percentile(recommendation_loudest, 10)
        rec_p90 = _percentile(recommendation_loudest, 90)
        if rec_p10 is not None and rec_p90 is not None:
            notes.append(
                f"Loudest-section spread (p10 to p90): "
                f"{rec_p10:.1f} to {rec_p90:.1f} LUFS."
            )
        if (target_low, target_high) != (initial_low, initial_high):
            notes.append(
                f"Target lowered from {(initial_low + initial_high) / 2.0:.1f} to "
                f"{(target_low + target_high) / 2.0:.1f} LUFS to reduce heavy limiting."
            )
    else:
        notes.append("No analyzed tracks; using default target band.")

    candidate = _settings_with_targets(current, target_low, target_high)
    projected_would, projected_heavy = _project_impact(rows, candidate)
    notes.append(
        f"Under recommended targets: {projected_would} would render, "
        f"{projected_heavy} heavy limiter risk."
    )

    return RecommendedSettings(
        target_low_lufs=target_low,
        target_high_lufs=target_high,
        window_seconds=current.window_seconds,
        hop_seconds=current.hop_seconds,
        peak_ceiling_dbfs=current.peak_ceiling_dbfs,
        max_reduction_db=current.max_reduction_db,
        mp3_threshold=current.mp3_threshold,
        lossless_threshold=current.lossless_threshold,
        confidence=confidence,
        notes=notes,
        projected_would_render_count=projected_would,
        projected_heavy_limiter_count=projected_heavy,
    )


def histogram_bins(values: list[float], *, bin_width: float = 0.5) -> list[tuple[float, float, int]]:
    """Return (bin_start, bin_end, count) tuples using fixed-width LUFS bins."""
    if not values:
        return []
    width = max(0.25, float(bin_width))
    arr = np.array(values, dtype=np.float64)
    lo = float(arr.min())
    hi = float(arr.max())
    if abs(hi - lo) < 0.01:
        lo -= width
        hi += width
    else:
        lo = float(np.floor(lo / width) * width - width)
        hi = float(np.ceil(hi / width) * width + width)
    edges = np.arange(lo, hi + width * 0.5, width)
    if len(edges) < 2:
        edges = np.array([lo, hi + width])
    counts, edges = np.histogram(arr, bins=edges)
    bins: list[tuple[float, float, int]] = []
    for index in range(len(counts)):
        bins.append((float(edges[index]), float(edges[index + 1]), int(counts[index])))
    return bins


def lufs_distribution_curve(
    values: list[float],
    *,
    sample_count: int = 120,
) -> tuple[list[float], list[float], float, float]:
    """Return smoothed (lufs, density 0-1) samples and the plotted LUFS range."""
    if not values:
        return [], [], 0.0, 0.0

    arr = np.array(values, dtype=np.float64)
    data_lo = float(arr.min())
    data_hi = float(arr.max())
    span = max(1.0, data_hi - data_lo)
    pad = max(0.75, span * 0.12)
    lo = data_lo - pad
    hi = data_hi + pad

    hist_bins = max(16, min(48, int(round(span / 0.5)) + 8))
    edges = np.linspace(lo, hi, hist_bins + 1)
    counts, edges = np.histogram(arr, bins=edges)
    centers = (edges[:-1] + edges[1:]) / 2.0

    kernel_size = 9
    sigma = 1.35
    offsets = np.arange(kernel_size, dtype=np.float64) - (kernel_size // 2)
    kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
    kernel /= kernel.sum()
    smoothed = np.convolve(counts.astype(np.float64), kernel, mode="same")

    x_samples = np.linspace(lo, hi, max(32, sample_count))
    y_samples = np.interp(x_samples, centers, smoothed)
    peak = float(y_samples.max()) or 1.0
    y_norm = (y_samples / peak).tolist()
    return x_samples.tolist(), y_norm, lo, hi


def limiter_bucket_counts(rows: list[TrackRow]) -> dict[str, int]:
    buckets = {"clean": 0, "light": 0, "moderate": 0, "heavy": 0}
    for row in rows:
        peak_control = parse_optional_float(row.get("estimated_peak_control_db", ""))
        if peak_control is None or peak_control <= 0.01:
            buckets["clean"] += 1
            continue
        severity = str(row.get("peak_control_severity", "none"))
        if severity == "heavy":
            buckets["heavy"] += 1
        elif severity == "moderate":
            buckets["moderate"] += 1
        elif severity == "light":
            buckets["light"] += 1
        else:
            buckets["clean"] += 1
    return buckets
