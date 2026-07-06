[![License](https://img.shields.io/github/license/mirbyte/DropGain?color=00b4d8&maxAge=604800)](https://github.com/mirbyte/DropGain/blob/main/LICENSE)
![Size](https://img.shields.io/github/repo-size/mirbyte/DropGain?label=size&color=00b4d8&maxAge=86400)
![LastCommit](https://img.shields.io/github/last-commit/mirbyte/DropGain?color=00b4d8&label=repo+updated)
![Views](https://hits.sh/github.com/mirbyte/DropGain.svg?color=00b4d8&label=views)

<!-- release downloads [![Download Count](https://img.shields.io/github/downloads/mirbyte/DropGain/total?color=00b4d8&maxAge=86400)](https://github.com/mirbyte/DropGain/releases) -->

# DropGain

**The default open-source loudness prep tool for DJs.**

*Loudness matching for the loudest part of the track, not the whole-file average.*

EDM- and DJ-library oriented section loudness normalization: analyze the loudest section, suggest gain for a target LUFS band under a true-peak ceiling, write `_DG` copies. Complements DJ software and commercial prep tools.

## Status

**Still in active development.** App will contain bugs. Behavior, defaults, and edge cases will change.

- **Tested platform:** Windows only (by the author so far)
- **macOS / Linux:** may work (Python + Tkinter + FFmpeg), but not tested
- Always try on a **copy** of your library first
- Processed files use the `_DG` suffix; originals are not modified

### Track length and memory

Analysis and rendering currently load each file fully into memory; there is no chunked or streaming processing yet.

**Tracks longer than about 10 minutes** may cause high RAM use, long hangs, or the app or process to crash, especially with multiple analysis workers or during batch render plus verification.

If you work with long material:

- Split files before processing
- Lower **Analysis workers** in Preferences
- Wait for updates

### Throughput (author's machine)

On a Ryzen 7 with **Analysis workers** set to **4**, full analyze + render runs have averaged around **430 tracks/hour** (typical EDM-length material, limiter-assisted mode where needed). I would not go above 4 workers on that CPU even though it has 8 cores. Your numbers will vary with CPU, disk, track length, how many tracks need limiting vs clean gain, and output format.

## What it does

- **Library analysis** - recursive scan; programme and section LUFS, dBTP, sample peak, suggested gain, projections, and processing action per row
- **Section-based targeting** - loudest sliding window (default 20 s / 5 s hop); true-peak ceiling wins over LUFS when they conflict
- **Clean gain or limiter-assisted** - linear gain when the ceiling allows; a limiter engine (FabFilter Pro-L 2 or LoudMax) with `max_reduction` cap when peak control is needed
- **Bass-aware trim** - on positive gain only; low-band energy can reduce boost on bass-heavy sections
- **`_DG` outputs** - copies beside sources or under a separate root; preserve format, force AIFF/MP3, or decode MP3 to AIFF to avoid double lossy encode
- **Library Tuning** - profile the library; recommend targets, window/hop, thresholds, and ceiling
- **Verification** - post-render re-measurement; loudness-normalization tags stripped; optional CSV (`dropgain_report.csv`) and session log

## When DropGain makes sense (and when it doesn't)

Narrower than most commercial library tools on purpose: level and peak control, not repair, color, or all-in-one templates.

**Consider DropGain if you:**

- Want **section-based** loudness (drop/chorus), not whole-file average or playback-time trim
- Want a defined **true-peak ceiling** (default -1.0 dBTP) with gain reduced when level and peak conflict
- Want **auditable** per-track numbers (LUFS, dBTP, section boundaries, limiter estimate) before batch render
- Want **portable** output that does not depend on one app's auto-gain database
- Prefer a **minimal signal path**: linear gain when possible; limiting only when peak control is required
- Value **inspectability**: open source code, tweakable defaults, no black-box batch chain
- Are fine with a **work-in-progress** tool (see Status) and validating results on a library copy first

**Probably not for you if you:**

- Want one integrated app for library management, analysis, and decks with minimal setup
- Want clipped-peak repair, warmth, saturation, or template-style enhancement without tuning
- Need polished vendor support and broad cross-platform QA
- Do not want to install FFmpeg, Python, or (for full peak-limited prep) a limiter plugin (FabFilter Pro-L 2 or the free LoudMax)

Commercial DJ and prep tools are often the better fit for convenience, integration, and breadth. DropGain is an alternative when your workflow cares more about explicit targets, render-stage control, and a transparent path than about all-in-one polish.

## FAQ (nobody's asked yet lol)

### Why use a limiter on already mastered tracks?

Three reasons:

1. **You want to go loud** - boosting into a true-peak ceiling without clipping requires peak control, not just turning up the fader.
2. **You want modern EDM-style masters** - limiter-assisted mode uses a limiter (FabFilter Pro-L 2 or LoudMax, selectable in Preferences) when clean gain is not enough. That matches how a lot of current dance music is already mastered.
3. **It is a deliberate choice** - limiter-assisted is the default because that is how I plan to prep my own library this summer. Clean gain is there when you do not want limiting. A third peak repair mode is planned for a future update.

### Why are the defaults so loud?

Default target band is **-7.8 to -7.5 LUFS** (loudest section), with **Limiter-assisted** as the default normalization mode. That is intentional for modern EDM libraries: current masters are hot, and if you play B2Bs or a set between other DJs, their material is usually not matched down to streaming-style levels. The defaults assume you want your prep to sit in that world, not under it.

You can lower the target band, switch to **Clean gain**, or tune everything via **Library Tuning** if your library or venue needs something quieter.

### Why is this free and open source?

I am lazy. Shipping and supporting a commercial product is work I do not want to take on.

That said, open source is also the point: you can read and change how gain, ceilings, bass trim, and limiting are decided; fork or patch behavior without trusting a black-box batch processor. Free + open source is not a claim that DropGain sounds better than commercial tools. It is a claim that the workflow is inspectable and yours to adapt.

**Plus:** It reaches the widest possible audience, helps me get recognition as a bedroom DJ / beginner developer, and without a professional background in audio or software engineering, building something that can compete on a commercial level is an incredibly steep uphill battle.

## Compared to other tools

### Rekordbox (and DJ app) auto gain

Rekordbox and similar apps remain the right choice for library management and playback. DropGain is render-stage prep (see **When DropGain makes sense**).

DJ auto gain is a **playback-time trim** from library analysis. It does not rewrite audio and generally lacks render-stage true-peak limiting, codec headroom modeling, limiter budgeting, bass-aware trim, or post-render verification. DropGain bakes level into `_DG` files you can load outside that app's database. Disable playback auto gain when using those exports (**DJ software** below).

### Platinum Notes 10 and WaveAlign

Platinum Notes 10 is a broader enhancement product: volume standardization, clipped-peak repair, warmth, multiband processing, and templates (Official, Festival, The Big Boost). DropGain is narrower: measurable loudness matching with minimal color. Scope and trade-offs are in **When DropGain makes sense** above. I don't have enough information about WaveAlign yet; it's not publicly released.

**Use Platinum Notes when** you want repair, warmth/saturation, template voicing, or all-in-one processing without per-track inspection.

**Cost:** DropGain is free for analysis, clean-gain render, Library Tuning, CSV, and verification. **Limiter-assisted** mode needs a limiter VST3: licensed **FabFilter Pro-L 2**, or the free **LoudMax** if you would rather not buy one. Platinum Notes 10 is **98€** one-time with limiting included; the trade-off is upfront suite cost vs. owning your limiter and keeping enhancement out of the path unless you add it.

## Operation

```
Source folder  →  Analyze  →  Review table / waveform  →  Render  →  *_DG outputs
                      ↓
              Library Tuning (optional)
```

1. Set **Source folder**; configure LUFS band, dBTP ceiling, normalization mode.
2. **Analyze Library** (or **Analyze + Create Copies**); review gain, section LUFS, dBTP, peak-control estimate.
3. Optional **Library Tuning**.
4. **Render Analyzed** for a cached batch (sources and settings unchanged).

Analysis caches `ffprobe` metadata per file; render reuses it when source size and mtime are unchanged.

## How it works

Technical detail for developers and curious users. Module roles are at the end of this file.

### Analysis

**Loudness**  
Decode to 48 kHz float64 (ITU-R BS.1770-4 metering rate) and measure with `pyloudnorm`. Programme integrated LUFS is computed on the full file. Section loudness uses a fixed window (default 20 s, min 10 s) stepped by hop (default 5 s, min 5 s); the window with highest integrated LUFS defines the reference section. Section start/end times drive gain decisions and post-render verification.

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
Projected section LUFS, sample peak, and true peak are stored. In limiter-assisted mode, projected true peak is capped at the ceiling when limiting is expected. The selected limiter engine (FabFilter Pro-L 2 or LoudMax) is used when estimated peak control > ~0 dB; otherwise clean gain.

Severity: none / light (≤1 dB) / moderate (≤3 dB) / heavy (>3 dB).

**Render eligibility**  
Skipped when in target range, |gain| below format threshold (default 0.5 dB MP3, 0.1 dB lossless), manual check required, zero-gain MP3 transcode would be lossy with no level change, or valid `_DG` output exists. True-peak safety renders still apply for in-band material that must be attenuated for ceiling compliance.

### Processing

**Clean gain**  
Native-rate decode → linear gain (`10^(gain/20)`) → ffmpeg encode. No DSP.

**Limiter-assisted (FabFilter Pro-L 2)**  
Default limiter engine, when peak control is required. Single-threaded VST3 host (pedalboard); clean-gain renders may run in parallel.

1. Decode float32 at native rate.
2. **Gain split** - `compensated_drive = gain_db - output_level` (output level = peak ceiling, default -1.0 dBFS). Negative component: linear pre-gain. Non-negative component: Pro-L gain parameter. Cuts are pre-plugin; boosts pass through the limiter.
3. **Pro-L 2** - Transparent style, 4× oversampling, true-peak limiting enabled, `output_level` = ceiling.
4. Buffer processing via pedalboard.
5. **Post-limiter trim** - re-measure section LUFS; if above `target_high`, apply linear correction (peak ceiling met but section still above upper LUFS bound).
6. ffmpeg encode; metadata via mutagen with loudness-normalization tags removed.

**Limiter-assisted (LoudMax)**  
Alternate limiter engine (**Preferences → Limiter engine**), for anyone without a Pro-L 2 license. Same compensated drive as Pro-L 2 (`gain_db - output_level`) applied as linear pre-gain; LoudMax's `output_db` is the ceiling trim and its threshold stays neutral. True-peak/ISP catches peaks above the ceiling.

**MP3 retry**  
Post-encode true-peak re-measurement. One retry with reduced limiter output level (limiter path) or gain (clean path) if dBTP exceeds ceiling. Preserve-format MP3→MP3 skips retry.

**Post-render verification**  
Re-decode; compare section LUFS and dBTP to projections (tolerance 0.4 LU / 0.2 dB). Metadata parity check against source.

Gain priority: true-peak ceiling, then loudest-section LUFS, then limiter budget (limiter-assisted). Gain is reduced rather than exceeding `max_reduction`.

## DJ software

Most DJ apps still apply **playback-time auto gain** on load. That stacks on top of level already baked into `_DG` files. Disable it when using DropGain exports:

| Software | Typical setting |
|----------|-----------------|
| **rekordbox** | Preferences → disable **Auto Gain** |
| **Serato DJ** | Preferences → DJ Preferences → uncheck **Use Auto Gain** |
| **TRAKTOR** | Preferences → Mixer → disable **Enable Autogain** |
| **VirtualDJ** | Options → set **autoGain** off (or equivalent; gain is applied on load from the internal analysis DB) |

**Engine DJ** (standalone players) generally has no auto-gain on playback; pre-leveled `_DG` files are usually fine there.

Load `_DG` copies (or your output folder) into the library you play from, not unprocessed originals.

### Dependencies and launch

- **FFmpeg / ffprobe** - on `PATH`
- A limiter VST3 for limiter-assisted mode, selected in **Preferences → Limiter engine**: **FabFilter Pro-L 2** (`PROL2_PLUGIN_PATH` or auto-discovery) or **LoudMax**, free (`LOUDMAX_PLUGIN_PATH` or auto-discovery)
- **Python** - `customtkinter`, `numpy`, `scipy`, `pyloudnorm`, `mutagen`, `pedalboard`, `Pillow`

Launch `main.pyw`. **Preferences → Check Limiter / System** validates the toolchain before batch render.

### Code layout

| Module | Role |
|--------|------|
| `main.pyw` | Entry point |
| `gui_tk.py` | Main `App` window: navigation, settings I/O, threading, job orchestration, results table |
| `gui_process.py` | Process page UI: source folder, analyze/render actions, metrics, table, log |
| `gui_settings.py` | Preferences page, dependency checks |
| `gui_library_tuning.py` | Library Tuning page, profiling UI |
| `gui_waveform.py` | Waveform preview worker, decode, PIL canvas rendering |
| `gui_theme.py` | Shared colors, typography, layout constants, table column defs |
| `gui_utils.py` | DPI awareness, tooltips, queue log handler, window sizing |
| `analysis.py` | Measurement, gain logic, discovery, CSV schema |
| `processing.py` | Render paths, limiter (Pro-L 2 / LoudMax) host, metadata |
| `jobs.py` | Analyze / render / batch jobs, worker pools |
| `optimizer.py` | Library profiling, settings recommendations |

UI invokes `jobs.py`; work runs on background threads with queue-based progress.

<br>
<br>


<img width="3839" height="2019" alt="ui_launch" src="https://github.com/user-attachments/assets/7a5e8e2f-f977-4e23-8a14-55da47ea72a2" />


<img src="https://api.visitorbadge.io/api/VisitorHit?user=mirbyte&repo=DropGain&label=VIEWS&countColor=%2300b4d8" width="0" height="0" />

