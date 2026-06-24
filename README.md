# DropGain

*Loudness matching for the loudest part of the track, not the whole-file average.*

EDM- and DJ-library oriented section loudness normalization. DropGain analyzes each track's loudest section, suggests gain to hit a target LUFS band under a true-peak ceiling, and optionally writes `_DG` copies alongside the sources.

**Clean gain when possible:** linear gain only, no DSP, when the adjustment fits under the ceiling. Limiter-assisted mode uses FabFilter Pro-L 2 only when peak control is required.

## Status

**Still in active testing.** Behavior, defaults, and edge cases may change.

- **Tested platform:** Windows only (by the author so far)
- **macOS / Linux:** may work (Python + Tkinter + FFmpeg), but not tested
- Always try on a **copy** of your library first
- Processed files use the `_DG` suffix; originals are not modified

## What it does

**Library analysis**  
Recursively scans supported files, decodes each track, and reports programme integrated LUFS, loudest-section LUFS (with section boundaries), sample peak, and oversampled true peak (dBTP). Each row includes suggested gain, projected post-gain metrics, peak-control estimate, and a processing action.

**Section-based targeting**  
Gain is derived from the loudest sliding window (default 30 s window, 10 s hop), so low-level intros, outros, and breakdowns do not anchor the measurement. A true-peak ceiling (default -1.0 dBTP) takes precedence over the LUFS target when the two conflict.

**Two processing modes**

- **Clean gain** - linear gain only. Selected when the required adjustment fits under the true-peak ceiling without limiting.
- **Limiter-assisted** - FabFilter Pro-L 2 applies gain with true-peak limiting when boost would exceed the ceiling. `max_reduction` sets the acceptable limiter depth; tracks requiring more gain reduction than the budget allows are flagged for review.

**Bass-aware gain reduction**  
On positive gain only, measured low-band energy (45–150 Hz and 20–45 Hz vs a 150–1000 Hz reference) can reduce the applied boost on bass-heavy programme material.

**Processed outputs**  
Eligible tracks render to `_DG` copies (adjacent to sources or under a separate output root, preserving relative paths). Output format may be preserved, forced to AIFF or MP3, or MP3 sources may be decoded to AIFF to avoid a second lossy generation.

**Library Tuning**  
Profiles analyzed libraries (LUFS distribution, limiter severity, format mix) and recommends target band, analysis window/hop, gain thresholds, and peak ceiling. Preview projected render counts before applying recommendations.

**Verification**  
Post-render re-measurement of LUFS and true peak against projections; metadata verification. ReplayGain, Sound Check, and R128 loudness tags are stripped from outputs to prevent downstream double normalization. Optional CSV report (`dropgain_report.csv`) and session log.

## Why not Rekordbox auto gain?

DJ software auto gain is usually a playback-time trim value derived during library analysis. It is useful for rough level matching, but it does not rewrite the audio and generally does not provide a render-stage DSP path with true-peak limiting, codec-aware headroom, or post-render verification.

DropGain is different in these areas:

- **Section-based analysis** - many DJ auto-gain systems are based on whole-track loudness or similar library-analysis values. DropGain targets the loudest sliding section, so quiet intros, breakdowns, and outros do not dominate the gain calculation.
- **True-peak ceiling** - DropGain measures oversampled dBTP and treats the ceiling as a hard constraint. If clean gain cannot fit under the ceiling, limiter-assisted mode can use FabFilter Pro-L 2 rather than leaving the track under target or relying on downstream mixer headroom.
- **Limiter budget** - Pro-L 2 peak control is estimated before render and capped by `max_reduction`; excessive limiting reduces gain instead of pushing the limiter past the configured budget.
- **Codec-aware output** - MP3 renders include a +0.8 dB true-peak allowance for encode-related peak lift and can retry with safer settings after post-render measurement.
- **Low-band handling** - bass and sub energy in the reference section can reduce positive gain, avoiding excessive boost on low-frequency-heavy material.
- **Baked, portable output** - the rendered `_DG` copy contains the level change in the audio data. It is independent of a specific DJ application's database, provided that playback auto gain is disabled.

## Why not Platinum Notes 10?

Platinum Notes 10 is a broader file enhancement and repair product: public documentation describes volume standardization, clipped-peak repair, warmth options, multiband processing, and processing templates such as Official, Festival, and The Big Boost. DropGain is narrower by design. It focuses on measurable library loudness matching while preserving the source as much as possible.

Use DropGain when you want:

- **Explicit targets** - exact LUFS target band, true-peak ceiling, analysis window, hop, gain thresholds, and limiter budget instead of choosing a broad processing template.
- **Clean gain first** - linear gain is used whenever it satisfies the target and ceiling. DSP is engaged only when true-peak control is required.
- **External mastering limiter** - limiter-assisted renders use FabFilter Pro-L 2 through the VST3 host, with true-peak limiting, oversampling, and output level tied to the configured ceiling.
- **Per-track accountability** - CSV and log output expose section boundaries, measured LUFS, dBTP, suggested gain, projected metrics, limiter estimate, render status, and warnings.
- **Post-render validation** - rendered audio is decoded and re-measured against projections, including section LUFS and true peak.
- **Codec-aware rendering** - MP3 outputs account for encode-related peak lift and can retry with safer settings after measurement.
- **Source-preserving operation** - DropGain writes `_DG` copies and does not modify originals.

Use Platinum Notes when you explicitly want its broader enhancement workflow: clipped-peak repair, warmth/saturation, template voicing, or all-in-one processing without inspecting per-track measurements. DropGain intentionally avoids global color, repair, or enhancement passes unless they are required for level and peak control.

### Cost
DropGain is free. Analysis, clean-gain rendering, Library Tuning, CSV reporting, and post-render verification do not require paid plugins. **Limiter-assisted** mode needs a licensed **FabFilter Pro-L 2** VST3 install; that is a separate purchase and the main cost if you want the full workflow on material that needs peak control beyond clean gain. More limiter options are planned for future updates.

Platinum Notes 10 is a one-time **98€** license. Its processing chain, including limiting, is included in that price. The trade-off is upfront software cost vs. owning your limiter choice and keeping enhancement/color out of the signal path unless you add it yourself.

**In short:**

- Use **Platinum Notes 10** for one-click enhancement: clipped-peak repair, warmth, template voicing, and a polished commercial workflow.
- Use **DropGain** for transparent, section-based loudness matching with true-peak safety, limiter budgeting, bass-aware gain restraint, and reports.

## How it works

### Workflow

```
Source folder  →  Analyze  →  Review table / waveform  →  Render  →  *_DG outputs
                      ↓
              Library Tuning (optional parameter recommendations)
```

Run modes:

1. **Analyze Library** - measurement and gain decisions only.
2. **Analyze + Create Copies** - analyze, then render all eligible tracks.
3. **Render Analyzed** - render from cached analysis (requires unchanged sources and compatible settings).

Analysis and render are separate passes. Analysis caches `ffprobe` metadata per file; render reuses it only when source size and mtime are unchanged.

### Analysis

**Loudness**  
Decode to 48 kHz float64 (ITU-R BS.1770-4 metering rate) and measure with `pyloudnorm`. Programme integrated LUFS is computed on the full file. Section loudness uses a fixed window (default 30 s, min 10 s) stepped by hop (default 10 s, min 5 s); the window with highest integrated LUFS defines the reference section. Section start/end times drive gain decisions and post-render verification.

Files shorter than one window are measured as a single block.

**True peak**  
dBTP via native-rate decode, 4× polyphase oversampling, and peak detection on the oversampled waveform. Section and whole-programme true peaks are measured in one pass; the higher value is retained. Section measurement includes edge padding to capture boundary inter-sample peaks.

Failed true-peak measurement blocks positive gain and flags manual review (overridable via **allow risky true-peak boost**).

**Spectral band strength**  
FFT band energy on the reference section only, relative to 150–1000 Hz:

- 45–150 Hz (bass)
- 20–45 Hz (sub)

Band strength affects bass-aware gain trim only, not section selection.

### Gain decision

Computed in `decide_from_measurements` (`analysis.py`). Steps run in order.

**1. LUFS target**  
Map loudest-section LUFS to `[target_low, target_high]`:

- Below band → gain toward `target_low`
- Above band → gain toward `target_high`
- In band → 0 dB

**2. Peak reference**  
Mode-dependent peak for initial ceiling evaluation:

- **Clean gain** - whole-programme true peak
- **Limiter-assisted** - section true peak

**3. Clean-gain ceiling** (clean gain mode)  
If `peak_reference + gain` exceeds the dBTP ceiling, gain is clamped. No limiter is modeled.

**4. Bass-aware trim** (positive gain only)  
Ramp from band-strength thresholds (bass: +3 to +12 dB relative strength, max 0.6 dB gain reduction; sub: analogous curve). Attenuation suggestions are unchanged.

**5. MP3 encode allowance**  
Re-encoded MP3 outputs (non-preserve mode) add +0.8 dB to true-peak projections for encoder inter-sample peak lift. Preserve-format MP3→MP3 omits this allowance.

**6. Limiter budget** (limiter-assisted mode)  
Reference reverts to whole-programme true peak. Estimated limiter depth:

```
estimated_control = max(0, whole_track_TP + gain + mp3_lift - ceiling)
```

`max_reduction` caps acceptable peak control. Over-budget gain is reduced by the excess. Reported as estimated peak control (dB and approximate linear amplitude reduction %).

**7. Engine selection**  
Projected section LUFS, sample peak, and true peak are stored. In limiter-assisted mode, projected true peak is capped at the ceiling when limiting is expected. Pro-L 2 is used when estimated peak control > ~0 dB; otherwise clean gain.

Severity: none / light (≤1 dB) / moderate (≤3 dB) / heavy (>3 dB).

**Render eligibility**  
Skipped when in target range, |gain| below format threshold (default 0.5 dB MP3, 0.1 dB lossless), manual check required, zero-gain MP3 transcode would be lossy with no level change, or valid `_DG` output exists. True-peak safety renders still apply for in-band material that must be attenuated for ceiling compliance.

### Processing

**Clean gain**  
Native-rate decode → linear gain (`10^(gain/20)`) → ffmpeg encode. No DSP.

**Limiter-assisted (FabFilter Pro-L 2)**  
When peak control is required. Single-threaded VST3 host (pedalboard); clean-gain renders may run in parallel.

1. Decode float32 at native rate.
2. **Gain split** - `compensated_drive = gain_db - output_level` (output level = peak ceiling, default -1.0 dBFS). Negative component: linear pre-gain. Non-negative component: Pro-L gain parameter. Cuts are pre-plugin; boosts pass through the limiter.
3. **Pro-L 2** - Modern style, 4× oversampling, true-peak limiting enabled, `output_level` = ceiling.
4. Buffer processing via pedalboard.
5. **Post-limiter trim** - re-measure section LUFS; if above `target_high`, apply linear correction (peak ceiling met but section still above upper LUFS bound).
6. ffmpeg encode; metadata via mutagen with loudness-normalization tags removed.

**MP3 retry**  
Post-encode true-peak re-measurement. One retry with reduced Pro-L output level (limiter path) or gain (clean path) if dBTP exceeds ceiling. Preserve-format MP3→MP3 skips retry.

**Post-render verification**  
Re-decode; compare section LUFS and dBTP to projections (tolerance 0.4 LU / 0.2 dB). Metadata parity check against source.

### Priority order

1. True-peak ceiling  
2. Loudest-section LUFS target  
3. Limiter budget (limiter-assisted mode)

Gain is reduced rather than exceeding `max_reduction`. Heavy limiting is reported, not applied beyond the configured budget.

### Code layout

| Module | Role |
|--------|------|
| `main.pyw` | Entry point |
| `gui_tk.py` | Main window, threading, waveform, run orchestration |
| `gui_process.py` | Process page: folder, table, log |
| `gui_settings.py` | Preferences, dependency checks |
| `gui_library_tuning.py` | Library profile, recommendations |
| `analysis.py` | Measurement, gain logic, discovery, CSV schema |
| `processing.py` | Render paths, Pro-L 2 host, metadata |
| `jobs.py` | Analyze / render / batch jobs, worker pools |
| `optimizer.py` | Library profiling, settings recommendations |

UI invokes `jobs.py`; work runs on background threads with queue-based progress.

### Dependencies

- **FFmpeg / ffprobe** - on `PATH`
- **FabFilter Pro-L 2** (VST3) - limiter-assisted path via `pedalboard`; `PROL2_PLUGIN_PATH` or auto-discovery
- **Python** - `customtkinter`, `numpy`, `scipy`, `pyloudnorm`, `mutagen`, `pedalboard`, `Pillow`

Launch `main.pyw`. **Preferences → Check Pro-L 2 / System** validates the toolchain before batch render.

## Operation

1. Set **Source folder**.
2. Configure LUFS target band, dBTP ceiling, normalization mode.
3. **Analyze Library** - review gain, section LUFS, dBTP, peak-control estimate, status.
4. Optional: **Library Tuning** from analyzed data.
5. **Render Analyzed** or **Analyze + Create Copies**.

Outputs: `Trackname_DG.ext` (or under configured output root). Source files are not modified.

## DJ software

DropGain renders gain into the file. Loudness-normalization metadata is stripped from outputs, but most DJ applications still apply **playback-time auto gain** from their own library analysis (deck gain/trim moved on load, not a rewrite of the file). That stacks on top of the level already baked in by DropGain.

Disable auto gain (or equivalent) in whatever software you use with `_DG` exports:

| Software | Typical setting |
|----------|-----------------|
| **rekordbox** | Preferences → disable **Auto Gain** |
| **Serato DJ** | Preferences → DJ Preferences → uncheck **Use Auto Gain** |
| **TRAKTOR** | Preferences → Mixer → disable **Enable Autogain** |
| **VirtualDJ** | Options → set **autoGain** off (or equivalent; gain is applied on load from the internal analysis DB) |

**Engine DJ** (standalone players) generally has no auto-gain on playback; level is set with hardware trim. Pre-leveled `_DG` files are usually fine there without an extra preference change.

Load the `_DG` copies (or your configured output folder) into the library you actually play from, not the unprocessed originals, when you want the normalized level on the decks.
