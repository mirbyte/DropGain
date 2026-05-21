# DropGain

EDM-oriented drop loudness normalization for DJ libraries. Analyzes each track’s **loudest section**, suggests gain to hit a target LUFS band, and optionally writes corrected copies.

**Preserves dynamics:** processing applies **linear gain only** — no limiting, no brick-wall clipping, and no loudness “squash.” Boost is capped by peak headroom and max boost so levels stay clean without reshaping the waveform.

## Status

**Still in active testing.** Behavior, defaults, and edge cases may change. Expect bugs and rough edges.

- **Tested platform:** **Windows only** (by the author so far)
- **macOS / Linux:** may work in theory (Python + Tkinter + FFmpeg), but **not tested**
- **Always** try on a **copy** of your library first; even if originals are never modified
- Processed files use the `_DG` suffix

## What it does

- Scans a folder recursively for **FLAC**, **MP3**, **WAV**, and **AIFF**
- Measures integrated LUFS and finds the **loudest sliding window** (default 30 s window, 10 s hop)
- Applies a subtle **bass-aware** adjustment so sub-heavy drops are not over- or under-corrected vs lean tracks
- Suggests **Raise**, **Lower**, or **Keep** based on a no-touch LUFS band (default **-7.0 to -6.0 LUFS** on the drop)
- Creates copies with FFmpeg **volume** filter when gain exceeds format-specific thresholds
- Copies tags via **Mutagen** and verifies lossless format integrity where possible

**Not for:** archival mastering, streaming normalization (LUFS-I), or broadcast compliance.

## Requirements

| Component | Notes |
|-----------|--------|
| OS | **Windows** (tested). Other platforms untested |
| Python 3.10+ | 3.11+ recommended |
| [FFmpeg](https://ffmpeg.org/download.html) | `ffmpeg` and `ffprobe` on your PATH |
| Python packages | See `requirements.txt` |

### Install

```bash
git clone <your-repo-url> DropGain
cd DropGain
python -m venv .venv
.venv\Scripts\activate    # Windows (tested)

pip install -r requirements.txt
```

On macOS or Linux, use `source .venv/bin/activate` instead — not verified.

Ensure FFmpeg works:

```bash
ffmpeg -version
ffprobe -version
```

### Run

```bash
python main.py
```

## Usage

1. Choose a **music folder** (originals stay untouched).
2. Set **Corrected copies** to next to originals or a custom output folder.
3. Adjust targets and analysis settings if needed (defaults suit typical EDM drops).
4. **Analyze Only** — dry run, summary popup, no files written.
5. **Analyze + Create Copies** — writes `*_DG.*` files when gain exceeds thresholds.

Settings are saved to `dropgain_settings.json` next to the app (see `.gitignore`).

## Output files

| Format | Behavior |
|--------|----------|
| FLAC / WAV / AIFF | Lossless re-encode with gain when \|gain\| ≥ lossless threshold (default **0.15 dB**) |
| MP3 | Re-encoded at **320k CBR** only when \|gain\| ≥ MP3 threshold (default **1.00 dB**) |

Existing `_DG` outputs are skipped during scans. The tool does not overwrite existing outputs unless you change `PROCESS_OVERWRITE_EXISTING` in `analysis.py`.

## Default processing behavior

- **Max boost:** 4.5 dB (also capped by peak headroom)
- **Peak reference:** -1.0 dBFS (limits how much gain is applied; avoids clipping without a limiter)
- **Bass base ratio:** 4.0 dB (expected bass vs drop LUFS offset)
- **Bass nod sensitivity:** 0.25 (0 = LUFS only, 1 = full bass compensation)
- **Meter sample rate:** 48 kHz (analysis decode only; lossless output keeps source rate)

## Project layout

```
main.py          Entry point
gui.py           Tkinter UI and batch orchestration
analysis.py      LUFS / bass analysis and gain logic
processing.py    FFmpeg encode, metadata, verification
requirements.txt Python dependencies
```

Logs are written to `dropgain.log` in the app directory.

## Safety notes

- This project is **under testing**; do not rely on it for irreplaceable masters without verifying outputs by ear.
- Always work on a **backup or copy** of your library until you trust the results.
- MP3 processing is **lossy**; only files above the MP3 gain threshold are re-encoded.
- Very quiet or heavily limited masters may get little or no boost (peak headroom cap).
- Cancellation finishes the current file(s) then stops; completed outputs are kept.
