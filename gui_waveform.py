"""DropGain waveform preview: worker thread, ffmpeg decode, and PIL rendering."""

from __future__ import annotations

import math
import os
import queue
import subprocess
import threading
from typing import TYPE_CHECKING, Any

import tkinter as tk
from PIL import Image, ImageDraw, ImageFont, ImageTk

from analysis import hidden_subprocess_kwargs, is_limiter_processing_engine, parse_optional_float
from gui_decor import (
    draw_corner_brackets,
    draw_inset_frame,
    draw_scanlines,
    draw_signal_grid,
)
from gui_theme import (
    ACCENT,
    BG_CARD,
    BORDER_COLOR,
    BUTTON_TEXT_DARK,
    FG_MAIN,
    FG_MUTED,
    ICE_SOFT,
    LOG_BG,
    OUTPUT_CONTENT_HEIGHT,
    SIGNAL_BRACKET_ALPHA,
    SIGNAL_FRAME_HIGHLIGHT,
    SIGNAL_FRAME_SHADOW,
    SIGNAL_GRID_ALPHA,
    SIGNAL_GRID_SPACING,
    SIGNAL_SCANLINE_ALPHA,
    SIGNAL_SCANLINE_SPACING,
    WAVEFORM_BAND_LOW,
    WAVEFORM_BAND_OTHER,
    WAVEFORM_BG,
    WAVEFORM_CURVE,
    WAVEFORM_DROP_BG,
    WAVEFORM_FONT_LABEL_BASE,
    WAVEFORM_FONT_MESSAGE_BASE,
    WAVEFORM_FONT_PILL_BASE,
    WAVEFORM_FONT_VALUE_BASE,
    WAVEFORM_GRID,
    WAVEFORM_LIMITER_ZONE,
    WAVEFORM_LOUDNESS_SMOOTH_SECONDS,
    WAVEFORM_MARKER,
    WAVEFORM_MIN_HEIGHT,
    WAVEFORM_MIN_WIDTH,
    WAVEFORM_PEAK_FILL,
    WAVEFORM_PREVIEW_MAX_POINTS,
    WAVEFORM_PREVIEW_SAMPLE_RATE,
    WAVEFORM_SUPERSAMPLE,
    WAVEFORM_TARGET_BAND,
    WAVEFORM_TEXT_BADGE_ALPHA,
    SPACE_1,
    SPACE_2,
)
from gui_utils import make_tooltip_label, position_tooltip_window, ui_scale_for

if TYPE_CHECKING:
    from gui_tk import App


class WaveformMixin:
    """Waveform preview worker, decode, and canvas rendering for App."""

    def _init_waveform_state(self: "App") -> None:
        self._waveform_request_id = 0
        self._waveform_job_queue: queue.Queue[tuple[int, dict[str, object]] | None] = queue.Queue()
        self._waveform_worker_thread: threading.Thread | None = None
        self._waveform_worker_lock = threading.Lock()
        self._waveform_ffmpeg_proc: subprocess.Popen[bytes] | None = None
        self._waveform_ffmpeg_lock = threading.Lock()
        self._waveform_item_id: str | None = None
        self._current_waveform_data: dict[str, object] | None = None
        self._waveform_loudest_region: tuple[int, int, int, int] | None = None
        self._waveform_loudest_tooltip_text = ""
        self._waveform_tip_window: tk.Toplevel | None = None
        self._waveform_tip_after_id: str | None = None
        self._waveform_tip_pending_text = ""
        self._waveform_ctk_image: Any = None
        self._waveform_pil_image: Image.Image | None = None
        self._waveform_photo: ImageTk.PhotoImage | None = None
        self._waveform_configure_after_id: str | None = None

    def _on_results_table_select(self, _event: tk.Event[tk.Widget] | None = None) -> None:
        selection = self.results_table.selection()
        if not selection:
            return
        self._queue_waveform_for_item(str(selection[0]))

    def _ensure_waveform_worker(self) -> None:
        with self._waveform_worker_lock:
            if self._waveform_worker_thread is not None and self._waveform_worker_thread.is_alive():
                return
            self._waveform_worker_thread = threading.Thread(
                target=self._waveform_worker_loop,
                name="waveform-preview-worker",
                daemon=True,
            )
            self._waveform_worker_thread.start()

    def _waveform_worker_loop(self) -> None:
        while True:
            job = self._waveform_job_queue.get()
            if job is None:
                return
            request_id, row = job
            self._run_waveform_preview(request_id, row)

    def _drain_waveform_job_queue(self) -> None:
        while True:
            try:
                self._waveform_job_queue.get_nowait()
            except queue.Empty:
                break

    def _cancel_waveform_ffmpeg(self) -> None:
        with self._waveform_ffmpeg_lock:
            proc = self._waveform_ffmpeg_proc
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass

    def _cancel_waveform_preview_work(self) -> None:
        self._cancel_waveform_ffmpeg()
        self._drain_waveform_job_queue()

    def _shutdown_waveform_worker(self) -> None:
        self._cancel_waveform_preview_work()
        with self._waveform_worker_lock:
            thread = self._waveform_worker_thread
        if thread is None or not thread.is_alive():
            return
        self._waveform_job_queue.put(None)
        thread.join(timeout=2.0)

    def _queue_waveform_for_item(self, item_id: str) -> None:
        try:
            index = int(item_id)
        except ValueError:
            return

        if not (0 <= index < len(self._analyzed_rows)):
            return

        row = self._analyzed_rows[index]
        if not isinstance(row, dict):
            return

        if item_id == self._waveform_item_id and (
            self._current_waveform_data is not None
            or self.var_waveform_stats.get() == "Loading waveform preview..."
        ):
            return

        path = str(row.get("path", "") or "")
        filename = str(row.get("filename", "") or os.path.basename(path) or "Selected track")
        self._waveform_request_id += 1
        request_id = self._waveform_request_id
        self._waveform_item_id = item_id
        self._current_waveform_data = None
        self.var_waveform_title.set(filename)
        self.var_waveform_stats.set("Loading waveform preview...")
        self._draw_waveform_canvas(message="Loading waveform preview...")

        self._cancel_waveform_preview_work()
        self._ensure_waveform_worker()
        self._waveform_job_queue.put((request_id, dict(row)))

    def _run_waveform_preview(self, request_id: int, row: dict[str, object]) -> None:
        try:
            preview = self._build_waveform_preview(row)
        except Exception as exc:
            if request_id != self._waveform_request_id:
                return
            filename = str(row.get("filename", "") or os.path.basename(str(row.get("path", "") or "")) or "Selected track")
            self._queue.put(("waveform_error", (request_id, str(exc), filename)))
            return
        if request_id != self._waveform_request_id:
            return
        self._queue.put(("waveform_preview", (request_id, preview)))

    def _build_waveform_preview(self, row: dict[str, object]) -> dict[str, object]:
        import numpy as np

        path = str(row.get("path", "") or "")
        if not path:
            raise RuntimeError("Track path is missing.")
        if not os.path.exists(path):
            raise RuntimeError("Audio file was not found on disk.")

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
            "-ac",
            "1",
            "-ar",
            str(WAVEFORM_PREVIEW_SAMPLE_RATE),
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "pipe:1",
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        with self._waveform_ffmpeg_lock:
            self._waveform_ffmpeg_proc = proc
        try:
            stdout, stderr = proc.communicate()
        finally:
            with self._waveform_ffmpeg_lock:
                if self._waveform_ffmpeg_proc is proc:
                    self._waveform_ffmpeg_proc = None
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip() or "ffmpeg waveform decode failed"
            raise RuntimeError(err)

        audio = np.frombuffer(stdout, dtype=np.float32)
        if audio.size == 0:
            raise RuntimeError("Decoded waveform preview was empty.")

        audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
        abs_audio = np.abs(audio)
        samples_per_point = max(1, int(math.ceil(abs_audio.size / WAVEFORM_PREVIEW_MAX_POINTS)))
        point_count = max(1, int(math.ceil(abs_audio.size / samples_per_point)))
        padded_size = point_count * samples_per_point
        if abs_audio.size < padded_size:
            abs_audio = np.pad(abs_audio, (0, padded_size - abs_audio.size), mode="constant")

        envelope_peak_raw = abs_audio.reshape(point_count, samples_per_point).max(axis=1)
        max_peak = float(envelope_peak_raw.max(initial=0.0))
        envelope = envelope_peak_raw.copy()
        if max_peak > 0.0 and math.isfinite(max_peak):
            envelope = envelope / max_peak

        duration_from_audio = float(audio.size) / float(WAVEFORM_PREVIEW_SAMPLE_RATE)
        duration = self._optional_float(row.get("duration_sec")) or duration_from_audio
        if duration <= 0.0:
            duration = duration_from_audio

        audio_for_rms = np.frombuffer(stdout, dtype=np.float32)
        audio_for_rms = np.nan_to_num(audio_for_rms, nan=0.0, posinf=0.0, neginf=0.0)
        if audio_for_rms.size < padded_size:
            audio_for_rms = np.pad(audio_for_rms, (0, padded_size - audio_for_rms.size), mode="constant")
        rms_blocks = audio_for_rms.reshape(point_count, samples_per_point)
        rms = np.sqrt(np.mean(np.square(rms_blocks.astype(np.float64)), axis=1))

        seconds_per_point = duration / float(max(1, point_count))
        smooth_points = max(1, int(round(WAVEFORM_LOUDNESS_SMOOTH_SECONDS / max(seconds_per_point, 0.001))))
        if smooth_points > 1 and rms.size > 1:
            kernel = np.ones(smooth_points, dtype=np.float64) / float(smooth_points)
            rms = np.convolve(rms, kernel, mode="same")

        band_low, band_mid, band_high = self._compute_frequency_band_envelopes(rms_blocks)

        section_start = self._optional_float(row.get("loudest_section_start_sec"))
        section_end = self._optional_float(row.get("loudest_section_end_sec"))
        gain_db = self._optional_float(row.get("suggested_gain_db")) or 0.0
        scan_window_seconds = self._optional_float(row.get("loudness_scan_window_seconds"))
        scan_hop_seconds = self._optional_float(row.get("loudness_scan_hop_seconds"))

        window_curve = self._parse_loudness_window_curve(row.get("loudness_window_curve"))
        loudness_curve_lufs = self._interpolate_loudness_curve(window_curve, duration, point_count)

        projected_loudness_curve_lufs: list[float] = []
        loudest_section_lufs = self._optional_float(row.get("loudest_section_lufs"))
        if loudest_section_lufs is not None and rms.size > 0:
            rms_db = 20.0 * np.log10(np.maximum(rms, 1.0e-8))
            if duration > 0.0 and section_start is not None and section_end is not None and section_end > section_start:
                idx1 = max(0, min(point_count - 1, int((section_start / duration) * point_count)))
                idx2 = max(idx1 + 1, min(point_count, int(math.ceil((section_end / duration) * point_count))))
                reference_db = float(np.mean(rms_db[idx1:idx2]))
            else:
                reference_db = float(np.max(rms_db))

            if math.isfinite(reference_db):
                calibrated = (rms_db - reference_db) + float(loudest_section_lufs)
                projected_loudness_curve_lufs = [
                    float(value)
                    for value in (calibrated + float(gain_db)).tolist()
                    if math.isfinite(float(value))
                ]

        return {
            "filename": str(row.get("filename", "") or os.path.basename(path)),
            "path": path,
            "duration_sec": duration,
            "envelope": [float(v) for v in envelope.tolist()],
            "band_low": [float(v) for v in band_low.tolist()],
            "band_mid": [float(v) for v in band_mid.tolist()],
            "band_high": [float(v) for v in band_high.tolist()],
            "loudness_curve_lufs": loudness_curve_lufs,
            "projected_loudness_curve_lufs": projected_loudness_curve_lufs,
            "loudness_scan_window_seconds": scan_window_seconds,
            "loudness_scan_hop_seconds": scan_hop_seconds,
            "target_low_lufs": self._optional_float(row.get("target_low_lufs")),
            "target_high_lufs": self._optional_float(row.get("target_high_lufs")),
            "loudest_section_start_sec": section_start,
            "loudest_section_end_sec": section_end,
            "suggested_gain_db": gain_db,
            "loudest_section_lufs": row.get("loudest_section_lufs"),
            "projected_loudest_section_lufs": row.get("projected_loudest_section_lufs"),
            "true_peak_dbtp": row.get("true_peak_dbtp"),
            "projected_true_peak_dbtp": row.get("projected_true_peak_dbtp"),
            "estimated_peak_control_db": row.get("estimated_peak_control_db"),
            "processing_engine": row.get("processing_engine"),
        }

    @staticmethod
    def _parse_lufs_curve_values(raw: object) -> list[float]:
        if not isinstance(raw, (list, tuple)):
            return []
        try:
            return [
                float(value)
                for value in raw
                if math.isfinite(float(value))
            ]
        except Exception:
            return []

    @staticmethod
    def _lufs_curve_points(
        values: list[float],
        plot_left: float,
        plot_width: float,
        y_for_lufs,
    ) -> list[tuple[float, float]]:
        if not values:
            return []
        last_index = max(1, len(values) - 1)
        return [
            (
                plot_left + ((float(index) / float(last_index)) * plot_width),
                y_for_lufs(float(value)),
            )
            for index, value in enumerate(values)
        ]

    @staticmethod
    def _format_loudness_scan_label(window_seconds: object, hop_seconds: object) -> str:
        window = parse_optional_float(window_seconds)
        hop = parse_optional_float(hop_seconds)
        if window is None or hop is None:
            return "LUFS scan"
        return f"{window:g}s / {hop:g}s scan"

    @staticmethod
    def _parse_loudness_window_curve(value: object) -> list[tuple[float, float, float]]:
        if not isinstance(value, (list, tuple)):
            return []

        parsed: list[tuple[float, float, float]] = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                start_sec = parse_optional_float(item[0])
                end_sec = parse_optional_float(item[1])
                lufs = parse_optional_float(item[2])
            elif isinstance(item, dict):
                start_sec = parse_optional_float(item.get("start_sec"))
                end_sec = parse_optional_float(item.get("end_sec"))
                lufs = parse_optional_float(item.get("lufs"))
            else:
                continue

            if (
                start_sec is None
                or end_sec is None
                or lufs is None
                or end_sec <= start_sec
            ):
                continue

            parsed.append((start_sec, end_sec, lufs))

        return parsed

    @staticmethod
    def _interpolate_loudness_curve(
        window_curve: list[tuple[float, float, float]],
        duration: float,
        point_count: int,
    ) -> list[float]:
        if not window_curve or duration <= 0.0 or point_count <= 0:
            return []

        import numpy as np

        centers = np.array(
            [(start + end) / 2.0 for start, end, _ in window_curve],
            dtype=np.float64,
        )
        values = np.array([lufs for _, _, lufs in window_curve], dtype=np.float64)
        if centers.size == 0 or values.size == 0:
            return []

        order = np.argsort(centers)
        centers = centers[order]
        values = values[order]

        if centers.size == 1:
            return [float(values[0])] * point_count

        sample_times = (np.arange(point_count, dtype=np.float64) + 0.5) * (duration / float(point_count))
        interpolated = np.interp(sample_times, centers, values)
        return [float(value) for value in interpolated.tolist()]

    @staticmethod
    def _format_time_short(seconds: object) -> str:
        try:
            total_seconds = max(0, int(round(float(seconds))))
        except Exception:
            return "--:--"
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def _show_waveform_preview(self, preview: dict[str, object]) -> None:
        self._current_waveform_data = preview
        self.var_waveform_title.set(str(preview.get("filename", "") or "Selected track"))
        self.var_waveform_stats.set("")
        try:
            self._show_output_tab("Waveform")
        except Exception:
            pass
        self._draw_waveform_canvas()

    def _show_waveform_error(self, message: str, filename: str) -> None:
        self._waveform_item_id = None
        self._current_waveform_data = None
        self.var_waveform_title.set(filename)
        self.var_waveform_stats.set(f"Waveform preview failed: {message}")
        self._draw_waveform_canvas(message="Waveform preview failed")

    def _clear_waveform_preview(self, message: str = "Select an analyzed track to preview its waveform.") -> None:
        if not hasattr(self, "waveform_canvas"):
            return
        self._waveform_request_id += 1
        self._cancel_waveform_preview_work()
        self._waveform_item_id = None
        self._current_waveform_data = None
        self.var_waveform_title.set(message)
        self.var_waveform_stats.set("")
        self._draw_waveform_canvas(message=message)

    def _format_waveform_loudest_tooltip(self, preview: dict[str, object]) -> str:
        start = self._optional_float(preview.get("loudest_section_start_sec"))
        end = self._optional_float(preview.get("loudest_section_end_sec"))
        if start is not None and end is not None and end > start:
            range_text = f"{self._format_time_short(start)}–{self._format_time_short(end)}"
        else:
            range_text = "time range unavailable"

        lufs_text = self._format_lufs(preview.get("loudest_section_lufs"))
        scan_window = self._optional_float(preview.get("loudness_scan_window_seconds"))
        scan_hop = self._optional_float(preview.get("loudness_scan_hop_seconds"))
        scan_note = ""
        if scan_window is not None and scan_hop is not None:
            scan_note = (
                f"\nFull-track {scan_window:g}s window / {scan_hop:g}s hop scan; "
                "highlight marks the loudest window."
            )
        return (
            f"Loudest section ({range_text}).\n"
            f"DropGain measured {lufs_text} integrated LUFS here to set gain."
            f"{scan_note}"
        )

    def _hide_waveform_hover_tip(self) -> None:
        if self._waveform_tip_after_id is not None:
            try:
                self.waveform_canvas.after_cancel(self._waveform_tip_after_id)
            except Exception:
                pass
            self._waveform_tip_after_id = None
        self._waveform_tip_pending_text = ""
        window = self._waveform_tip_window
        self._waveform_tip_window = None
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    def _show_waveform_hover_tip(self, root_x: int, root_y: int, text: str) -> None:
        if self._waveform_tip_window is not None:
            return

        window = tk.Toplevel(self.waveform_canvas)
        window.withdraw()
        window.overrideredirect(True)
        try:
            window.attributes("-topmost", True)
        except Exception:
            pass

        label = make_tooltip_label(
            window,
            text,
            wraplength=320,
        )
        label.pack()
        self._waveform_tip_window = window
        position_tooltip_window(window, root_x, root_y)
        window.deiconify()

    def _schedule_waveform_hover_tip(self, event: tk.Event, text: str) -> None:
        if self._waveform_tip_window is not None:
            return
        if text == self._waveform_tip_pending_text and self._waveform_tip_after_id is not None:
            return
        if self._waveform_tip_after_id is not None:
            try:
                self.waveform_canvas.after_cancel(self._waveform_tip_after_id)
            except Exception:
                pass
            self._waveform_tip_after_id = None
        self._waveform_tip_pending_text = text
        root_x = int(event.x_root)
        root_y = int(event.y_root)

        def show() -> None:
            self._waveform_tip_after_id = None
            region = self._waveform_loudest_region
            if region is None or not self._waveform_loudest_tooltip_text:
                return
            x1, y1, x2, y2 = region
            try:
                local_x = self.waveform_canvas.winfo_pointerx() - self.waveform_canvas.winfo_rootx()
                local_y = self.waveform_canvas.winfo_pointery() - self.waveform_canvas.winfo_rooty()
            except Exception:
                return
            if not (x1 <= local_x <= x2 and y1 <= local_y <= y2):
                return
            self._show_waveform_hover_tip(root_x, root_y, self._waveform_loudest_tooltip_text)

        try:
            self._waveform_tip_after_id = self.waveform_canvas.after(550, show)
        except Exception:
            self._waveform_tip_after_id = None

    def _on_waveform_canvas_configure(self, event: tk.Event) -> None:
        if int(getattr(event, "width", 0) or 0) < 64 or int(getattr(event, "height", 0) or 0) < 48:
            return
        if self._waveform_configure_after_id is not None:
            try:
                self.waveform_canvas.after_cancel(self._waveform_configure_after_id)
            except Exception:
                pass
        self._waveform_configure_after_id = self.waveform_canvas.after(50, self._draw_waveform_canvas)

    def _waveform_plot_size(self) -> tuple[int, int]:
        self.update_idletasks()
        canvas = self.waveform_canvas
        panel = self.waveform_panel
        width = int(canvas.winfo_width())
        height = int(canvas.winfo_height())
        if width < 64:
            width = max(WAVEFORM_MIN_WIDTH, int(panel.winfo_width()))
        if height < 48:
            height = max(WAVEFORM_MIN_HEIGHT, OUTPUT_CONTENT_HEIGHT)
        return max(WAVEFORM_MIN_WIDTH, width), max(WAVEFORM_MIN_HEIGHT, height)

    def _waveform_ui_scale(self) -> float:
        try:
            if hasattr(self, "waveform_canvas") and self.waveform_canvas.winfo_exists():
                return ui_scale_for(self.waveform_canvas)
        except Exception:
            pass
        return self._ui_scale()

    def _waveform_font_px(self, base: int) -> int:
        return max(base, int(round(base * self._waveform_ui_scale())))

    def _waveform_pil_font(self, base: int, *, bold: bool = False) -> ImageFont.ImageFont:
        return self._pil_font(self._waveform_font_px(base), bold=bold)

    def _draw_waveform_text_badge(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        font: ImageFont.ImageFont,
        *,
        text_color: tuple[int, int, int, int],
        anchor: str = "lm",
        ui_scale: float = 1.0,
    ) -> None:
        x, y = xy
        bbox = draw.textbbox((x, y), text, font=font, anchor=anchor)
        pad_x = 5.0 * ui_scale
        pad_y = 3.0 * ui_scale
        radius = max(3.0, 4.0 * ui_scale)
        draw.rounded_rectangle(
            (bbox[0] - pad_x, bbox[1] - pad_y, bbox[2] + pad_x, bbox[3] + pad_y),
            radius=radius,
            fill=self._hex_rgba(BG_CARD, WAVEFORM_TEXT_BADGE_ALPHA),
        )
        draw.text((x, y), text, fill=text_color, font=font, anchor=anchor)

    def _draw_waveform_message_image(self, width: int, height: int, text: str) -> Image.Image:
        img = Image.new("RGBA", (width, height), self._hex_rgba(LOG_BG))
        draw = ImageDraw.Draw(img)
        margin = 8.0
        draw_signal_grid(
            draw,
            margin,
            margin,
            width - margin,
            height - margin,
            spacing=float(SIGNAL_GRID_SPACING),
            color=ACCENT,
            alpha=SIGNAL_GRID_ALPHA,
        )
        draw_scanlines(
            draw,
            margin,
            margin,
            width - margin,
            height - margin,
            spacing=float(SIGNAL_SCANLINE_SPACING),
            alpha=SIGNAL_SCANLINE_ALPHA,
        )
        draw_corner_brackets(
            draw,
            margin,
            margin,
            width - margin,
            height - margin,
            color=ACCENT,
            alpha=SIGNAL_BRACKET_ALPHA,
            arm=10.0,
            width=1,
        )
        font = self._waveform_pil_font(WAVEFORM_FONT_MESSAGE_BASE)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        draw.text(
            ((width - text_width) / 2.0, (height - text_height) / 2.0),
            text,
            fill=self._hex_rgba(FG_MUTED),
            font=font,
        )
        return img

    def _draw_waveform_text_1x(
        self,
        draw: ImageDraw.ImageDraw,
        width: int,
        height: int,
        *,
        preview: dict[str, object],
        layout: dict[str, object],
    ) -> None:
        plot_left = float(layout["plot_left"])
        plot_right = float(layout["plot_right"])
        plot_top = float(layout["plot_top"])
        plot_bottom = float(layout["plot_bottom"])
        target_band = layout.get("target_band")
        highlight = layout.get("highlight")
        target_low = layout.get("target_low")
        target_high = layout.get("target_high")
        ui_scale = float(layout.get("ui_scale") or 1.0)

        if target_band is not None and target_low is not None and target_high is not None:
            band_top, band_bottom = target_band  # type: ignore[misc]
            label_font = self._waveform_pil_font(WAVEFORM_FONT_LABEL_BASE)
            self._draw_waveform_text_badge(
                draw,
                (plot_left + 8 * ui_scale, (band_top + band_bottom) / 2.0),
                "target LUFS",
                label_font,
                text_color=self._hex_rgba(ICE_SOFT),
                anchor="lm",
                ui_scale=ui_scale,
            )
            edge_font = self._waveform_pil_font(WAVEFORM_FONT_VALUE_BASE)
            draw.text(
                (plot_right - 8 * ui_scale, band_top - 2 * ui_scale),
                f"{max(float(target_low), float(target_high)):.1f}",
                fill=self._hex_rgba(ICE_SOFT),
                font=edge_font,
                anchor="rb",
            )
            draw.text(
                (plot_right - 8 * ui_scale, band_bottom + 2 * ui_scale),
                f"{min(float(target_low), float(target_high)):.1f}",
                fill=self._hex_rgba(ICE_SOFT),
                font=edge_font,
                anchor="rt",
            )

        legend_font = self._waveform_pil_font(WAVEFORM_FONT_VALUE_BASE)
        scan_curve = self._parse_lufs_curve_values(preview.get("loudness_curve_lufs"))
        if scan_curve:
            self._draw_waveform_text_badge(
                draw,
                (plot_left + 8 * ui_scale, plot_bottom - 8 * ui_scale),
                self._format_loudness_scan_label(
                    preview.get("loudness_scan_window_seconds"),
                    preview.get("loudness_scan_hop_seconds"),
                ),
                legend_font,
                text_color=self._hex_rgba(ICE_SOFT),
                anchor="lb",
                ui_scale=ui_scale,
            )

        projected_curve = self._parse_lufs_curve_values(preview.get("projected_loudness_curve_lufs"))
        y_for_lufs = layout.get("y_for_lufs")
        plot_width = float(layout.get("plot_width") or 0.0)
        if projected_curve and callable(y_for_lufs) and plot_width > 0.0:
            projected_points = self._lufs_curve_points(
                projected_curve,
                plot_left,
                plot_width,
                y_for_lufs,
            )
            if len(projected_points) >= 2:
                last_x, last_y = projected_points[-1]
                projected_font = self._waveform_pil_font(WAVEFORM_FONT_VALUE_BASE)
                projected_xy = (
                    min(plot_right - 8 * ui_scale, last_x),
                    max(plot_top + 10 * ui_scale, min(plot_bottom - 10 * ui_scale, last_y)),
                )
                self._draw_waveform_text_badge(
                    draw,
                    projected_xy,
                    "projected",
                    projected_font,
                    text_color=self._hex_rgba(WAVEFORM_CURVE),
                    anchor="rm",
                    ui_scale=ui_scale,
                )

        if highlight is not None:
            x1, x2 = highlight  # type: ignore[misc]
            drop_lufs = self._format_lufs(preview.get("loudest_section_lufs"))
            pill_text = f"DROP · {drop_lufs}"
            pill_font = self._waveform_pil_font(WAVEFORM_FONT_PILL_BASE, bold=True)
            pill_bbox = draw.textbbox((0, 0), pill_text, font=pill_font)
            pill_pad_x = 10 * ui_scale
            pill_pad_y = 4 * ui_scale
            pill_width = (pill_bbox[2] - pill_bbox[0]) + pill_pad_x
            pill_height = (pill_bbox[3] - pill_bbox[1]) + pill_pad_y
            pill_center_x = (float(x1) + float(x2)) / 2.0
            pill_left = pill_center_x - (pill_width / 2.0)
            pill_top = plot_top + 10 * ui_scale
            pill_right = pill_left + pill_width
            pill_bottom = pill_top + pill_height
            draw.rounded_rectangle(
                (pill_left, pill_top, pill_right, pill_bottom),
                radius=6 * ui_scale,
                fill=self._hex_rgba(WAVEFORM_MARKER, 200),
            )
            draw.text(
                (pill_center_x, (pill_top + pill_bottom) / 2.0),
                pill_text,
                fill=self._hex_rgba(BUTTON_TEXT_DARK),
                font=pill_font,
                anchor="mm",
            )

    def _on_waveform_canvas_motion(self, event: tk.Event) -> None:
        region = self._waveform_loudest_region
        text = self._waveform_loudest_tooltip_text
        if (
            region is not None
            and text
            and region[0] <= event.x <= region[2]
            and region[1] <= event.y <= region[3]
        ):
            self._schedule_waveform_hover_tip(event, text)
            return
        self._hide_waveform_hover_tip()

    def _on_waveform_canvas_leave(self, _event: tk.Event) -> None:
        self._hide_waveform_hover_tip()

    @staticmethod
    def _compute_frequency_band_envelopes(audio_blocks: Any) -> tuple[Any, Any, Any]:
        import numpy as np

        if audio_blocks.size == 0:
            empty = np.zeros(0, dtype=np.float64)
            return empty, empty, empty

        block_size = int(audio_blocks.shape[1])
        window = np.hanning(block_size).astype(np.float64)
        windowed = audio_blocks.astype(np.float64) * window
        spectra = np.abs(np.fft.rfft(windowed, axis=1))
        freqs = np.fft.rfftfreq(block_size, d=1.0 / float(WAVEFORM_PREVIEW_SAMPLE_RATE))

        low_mask = (freqs >= 20.0) & (freqs < 250.0)
        mid_mask = (freqs >= 250.0) & (freqs < 2000.0)
        high_mask = freqs >= 2000.0
        if not np.any(low_mask):
            low_mask = freqs < 250.0
        if not np.any(mid_mask):
            mid_mask = (freqs >= 250.0) & (freqs < max(2000.0, float(freqs.max()) * 0.5))
        if not np.any(high_mask):
            high_mask = freqs >= 2000.0

        band_low = np.sum(np.square(spectra[:, low_mask]), axis=1)
        band_mid = np.sum(np.square(spectra[:, mid_mask]), axis=1)
        band_high = np.sum(np.square(spectra[:, high_mask]), axis=1)

        def normalize_band(values: np.ndarray) -> np.ndarray:
            peak = float(values.max(initial=0.0))
            if peak > 0.0 and math.isfinite(peak):
                return values / peak
            return values

        return normalize_band(band_low), normalize_band(band_mid), normalize_band(band_high)

    def _blend_band_color(self, low_weight: float, mid_weight: float, high_weight: float, *, alpha: int = 215) -> tuple[int, int, int, int]:
        other_weight = mid_weight + high_weight
        total = low_weight + other_weight
        if total <= 1.0e-9:
            low_frac = 0.5
        else:
            low_frac = low_weight / total

        low_rgb = self._hex_rgba(WAVEFORM_BAND_LOW)
        other_rgb = self._hex_rgba(WAVEFORM_BAND_OTHER)
        rest_frac = 1.0 - low_frac
        red = int(low_rgb[0] * low_frac + other_rgb[0] * rest_frac)
        green = int(low_rgb[1] * low_frac + other_rgb[1] * rest_frac)
        blue = int(low_rgb[2] * low_frac + other_rgb[2] * rest_frac)
        return (red, green, blue, alpha)

    def _draw_frequency_waveform_body(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        band_low: list[float],
        band_mid: list[float],
        band_high: list[float],
        envelope: list[float],
        plot_left: float,
        plot_width: float,
        plot_top: float,
        plot_bottom: float,
    ) -> None:
        count = min(len(band_low), len(band_mid), len(band_high), len(envelope))
        if count < 1:
            return

        mid_y = plot_top + ((plot_bottom - plot_top) / 2.0)
        amplitude = max(8.0, (plot_bottom - plot_top) * 0.45)
        last_index = max(1, count - 1)
        plot_right = plot_left + plot_width

        for index in range(count):
            x1 = plot_left + ((float(index) / float(last_index)) * plot_width)
            if index + 1 < count:
                x2 = plot_left + ((float(index + 1) / float(last_index)) * plot_width)
            else:
                x2 = plot_right

            color = self._blend_band_color(band_low[index], band_mid[index], band_high[index])
            height_value = max(0.0, min(1.0, float(envelope[index])))
            y_top = max(plot_top, mid_y - (height_value * amplitude))
            y_bottom = min(plot_bottom, mid_y + (height_value * amplitude))
            if x2 > x1 and y_bottom > y_top:
                draw.rectangle((x1, y_top, x2, y_bottom), fill=color)

    @staticmethod
    def _hex_rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
        value = color.lstrip("#")
        if len(value) != 6:
            return (30, 30, 30, alpha)
        return (
            int(value[0:2], 16),
            int(value[2:4], 16),
            int(value[4:6], 16),
            alpha,
        )

    @staticmethod
    def _pil_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
        candidates = (
            ("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")
        )
        for name in candidates:
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _waveform_symmetric_polygon(
        envelope: list[float],
        plot_left: float,
        plot_width: float,
        plot_top: float,
        plot_bottom: float,
        *,
        amplitude_scale: float = 0.45,
    ) -> list[tuple[float, float]]:
        if not envelope:
            return []
        mid_y = plot_top + ((plot_bottom - plot_top) / 2.0)
        amplitude = max(8.0, (plot_bottom - plot_top) * amplitude_scale)
        last_index = max(1, len(envelope) - 1)
        points: list[tuple[float, float]] = []
        for index, value in enumerate(envelope):
            x = plot_left + ((float(index) / float(last_index)) * plot_width)
            clamped = max(0.0, min(1.0, float(value)))
            y_top = max(plot_top, mid_y - (clamped * amplitude))
            points.append((x, y_top))
        for index in range(len(envelope) - 1, -1, -1):
            value = envelope[index]
            x = plot_left + ((float(index) / float(last_index)) * plot_width)
            clamped = max(0.0, min(1.0, float(value)))
            y_bottom = min(plot_bottom, mid_y + (clamped * amplitude))
            points.append((x, y_bottom))
        return points

    def _render_waveform_pil_image(
        self,
        width: int,
        height: int,
        *,
        preview: dict[str, object] | None,
        message: str | None,
    ) -> Image.Image:
        width = max(WAVEFORM_MIN_WIDTH, int(width))
        height = max(WAVEFORM_MIN_HEIGHT, int(height))

        if message is not None:
            return self._draw_waveform_message_image(width, height, message)
        if not preview:
            return self._draw_waveform_message_image(width, height, "Select a row after analysis.")

        envelope_raw = preview.get("envelope") or []
        band_low_raw = preview.get("band_low") or []
        band_mid_raw = preview.get("band_mid") or []
        band_high_raw = preview.get("band_high") or []
        try:
            envelope = [max(0.0, min(1.0, float(value))) for value in envelope_raw]  # type: ignore[union-attr]
            band_low = [max(0.0, float(value)) for value in band_low_raw]  # type: ignore[union-attr]
            band_mid = [max(0.0, float(value)) for value in band_mid_raw]  # type: ignore[union-attr]
            band_high = [max(0.0, float(value)) for value in band_high_raw]  # type: ignore[union-attr]
        except Exception:
            envelope = []
            band_low = []
            band_mid = []
            band_high = []

        if not envelope:
            return self._draw_waveform_message_image(width, height, "No waveform data available.")

        duration = self._optional_float(preview.get("duration_sec")) or 0.0
        start = self._optional_float(preview.get("loudest_section_start_sec"))
        end = self._optional_float(preview.get("loudest_section_end_sec"))
        target_low = self._optional_float(preview.get("target_low_lufs"))
        target_high = self._optional_float(preview.get("target_high_lufs"))
        ui_scale = self._waveform_ui_scale()

        scale = WAVEFORM_SUPERSAMPLE
        render_width = width * scale
        render_height = height * scale
        img = Image.new("RGBA", (render_width, render_height), self._hex_rgba(LOG_BG))
        draw = ImageDraw.Draw(img)

        def s(value: float) -> float:
            return value * scale

        plot_pad_x = float(SPACE_1)
        plot_left_1x = plot_pad_x
        plot_right_1x = float(width) - plot_pad_x
        plot_width_1x = max(1.0, plot_right_1x - plot_left_1x)
        plot_left = s(plot_left_1x)
        plot_right = s(plot_right_1x)
        plot_width = max(1.0, plot_right - plot_left)

        plot_top_1x = 2.0
        plot_bottom_1x = max(plot_top_1x + 24.0, float(height) - 2.0)
        plot_height_1x = max(1.0, plot_bottom_1x - plot_top_1x)
        plot_top = s(plot_top_1x)
        plot_bottom = max(plot_top + s(24), float(render_height) - s(2.0))
        plot_height = max(1.0, plot_bottom - plot_top)

        loudness_scan_curve = self._parse_lufs_curve_values(preview.get("loudness_curve_lufs"))
        projected_loudness_curve = self._parse_lufs_curve_values(
            preview.get("projected_loudness_curve_lufs")
        )
        loudness_curve = projected_loudness_curve or loudness_scan_curve

        def percentile(values: list[float], fraction: float) -> float:
            if not values:
                return 0.0
            values_sorted = sorted(values)
            index = int(round((len(values_sorted) - 1) * max(0.0, min(1.0, fraction))))
            return values_sorted[index]

        lufs_min: float | None = None
        lufs_max: float | None = None
        bounds_values: list[float] = []
        bounds_values.extend(loudness_scan_curve)
        bounds_values.extend(projected_loudness_curve)
        if bounds_values:
            useful_curve = [value for value in bounds_values if value > -60.0]
            if not useful_curve:
                useful_curve = bounds_values
            bounds = [percentile(useful_curve, 0.05), percentile(useful_curve, 0.95)]
            if target_low is not None:
                bounds.append(float(target_low))
            if target_high is not None:
                bounds.append(float(target_high))
            lufs_min = min(bounds)
            lufs_max = max(bounds)
            if lufs_max - lufs_min < 3.0:
                center = (lufs_max + lufs_min) / 2.0
                lufs_min = center - 1.5
                lufs_max = center + 1.5
            else:
                pad = min(4.0, max(0.8, (lufs_max - lufs_min) * 0.12))
                lufs_min -= pad
                lufs_max += pad

        def y_for_lufs(value: float) -> float:
            if lufs_min is None or lufs_max is None or abs(lufs_max - lufs_min) < 1.0e-9:
                return plot_bottom
            ratio = (value - lufs_min) / (lufs_max - lufs_min)
            y = plot_bottom - (ratio * plot_height)
            return max(plot_top + 1.0, min(plot_bottom - 1.0, y))

        def y_for_lufs_1x(value: float) -> float:
            if lufs_min is None or lufs_max is None or abs(lufs_max - lufs_min) < 1.0e-9:
                return plot_bottom_1x
            ratio = (value - lufs_min) / (lufs_max - lufs_min)
            y = plot_bottom_1x - (ratio * plot_height_1x)
            return max(plot_top_1x + 1.0, min(plot_bottom_1x - 1.0, y))

        target_band: tuple[float, float] | None = None
        target_band_1x: tuple[float, float] | None = None
        highlight: tuple[float, float] | None = None
        highlight_1x: tuple[float, float] | None = None
        energy_width = max(2, int(round(2.0 * scale)))
        marker_width = max(1, int(round(1.0 * scale)))

        draw.rectangle((plot_left, plot_top, plot_right, plot_bottom), fill=self._hex_rgba(WAVEFORM_BG))
        draw_signal_grid(
            draw,
            plot_left,
            plot_top,
            plot_right,
            plot_bottom,
            spacing=s(float(SIGNAL_GRID_SPACING)),
            color=ACCENT,
            alpha=SIGNAL_GRID_ALPHA,
        )

        if target_low is not None and target_high is not None and lufs_min is not None and lufs_max is not None:
            low = min(float(target_low), float(target_high))
            high = max(float(target_low), float(target_high))
            y_low = y_for_lufs(low)
            y_high = y_for_lufs(high)
            band_top = min(y_low, y_high)
            band_bottom = max(y_low, y_high)
            target_band = (band_top, band_bottom)
            y_low_1x = y_for_lufs_1x(low)
            y_high_1x = y_for_lufs_1x(high)
            target_band_1x = (min(y_low_1x, y_high_1x), max(y_low_1x, y_high_1x))
            draw.rectangle(
                (plot_left, band_top, plot_right, band_bottom),
                fill=self._hex_rgba(WAVEFORM_TARGET_BAND),
            )

        if duration > 0.0 and start is not None and end is not None and end > start:
            x1 = plot_left + max(0.0, min(plot_width, (start / duration) * plot_width))
            x2 = plot_left + max(0.0, min(plot_width, (end / duration) * plot_width))
            if x2 > x1:
                highlight = (x1, x2)
                x1_1x = plot_left_1x + max(0.0, min(plot_width_1x, (start / duration) * plot_width_1x))
                x2_1x = plot_left_1x + max(0.0, min(plot_width_1x, (end / duration) * plot_width_1x))
                highlight_1x = (x1_1x, x2_1x)
                draw.rectangle(
                    (x1, plot_top, x2, plot_bottom),
                    fill=self._hex_rgba(WAVEFORM_DROP_BG, 200),
                )

        if target_band is not None:
            band_top, band_bottom = target_band
            dash = int(round(4 * scale))
            for y_line in (band_top, band_bottom):
                x_pos = plot_left
                while x_pos < plot_right:
                    draw.line(
                        (x_pos, y_line, min(plot_right, x_pos + dash), y_line),
                        fill=self._hex_rgba(ICE_SOFT),
                        width=max(1, scale),
                    )
                    x_pos += dash * 2

        if lufs_min is not None and lufs_max is not None:
            tick_values = [lufs_max, (lufs_max + lufs_min) / 2.0, lufs_min]
            for tick_value in tick_values:
                y_tick = y_for_lufs(tick_value)
                draw.line(
                    (plot_left, y_tick, plot_right, y_tick),
                    fill=self._hex_rgba(WAVEFORM_GRID, 120),
                    width=max(1, scale),
                )

        mid_y = plot_top + (plot_height / 2.0)
        draw.line(
            (plot_left, mid_y, plot_right, mid_y),
            fill=self._hex_rgba(WAVEFORM_GRID),
            width=max(1, scale),
        )

        if band_low and band_mid and band_high and len(band_low) == len(envelope):
            self._draw_frequency_waveform_body(
                draw,
                band_low=band_low,
                band_mid=band_mid,
                band_high=band_high,
                envelope=envelope,
                plot_left=plot_left,
                plot_width=plot_width,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
            )
        else:
            peak_polygon = self._waveform_symmetric_polygon(
                envelope,
                plot_left,
                plot_width,
                plot_top,
                plot_bottom,
            )
            if len(peak_polygon) >= 3:
                draw.polygon(peak_polygon, fill=self._hex_rgba(WAVEFORM_PEAK_FILL, 150))

        scan_points = self._lufs_curve_points(
            loudness_scan_curve,
            plot_left,
            plot_width,
            y_for_lufs,
        )
        projected_points = self._lufs_curve_points(
            projected_loudness_curve,
            plot_left,
            plot_width,
            y_for_lufs,
        )
        curve_points = projected_points if projected_points else scan_points

        peak_control = self._optional_float(preview.get("estimated_peak_control_db"))
        processing_engine = str(preview.get("processing_engine") or "")
        show_limiter_overshoot = (
            peak_control is not None
            and peak_control > 0.01
            and is_limiter_processing_engine(processing_engine)
            and target_low is not None
            and target_high is not None
            and target_band is not None
            and len(curve_points) >= 2
        )
        if show_limiter_overshoot:
            target_high_lufs = max(float(target_low), float(target_high))
            band_top_y = target_band[0]
            for index, (x_left, curve_y) in enumerate(curve_points):
                lufs_value = loudness_curve[index]
                if lufs_value <= target_high_lufs + 0.02:
                    continue
                if index + 1 < len(curve_points):
                    x_right = curve_points[index + 1][0]
                else:
                    x_right = plot_right
                if curve_y < band_top_y - 0.5:
                    draw.rectangle(
                        (x_left, curve_y, x_right, band_top_y),
                        fill=self._hex_rgba(WAVEFORM_LIMITER_ZONE, 180),
                    )

        if len(scan_points) >= 2:
            draw.line(
                scan_points,
                fill=self._hex_rgba(ICE_SOFT),
                width=energy_width,
                joint="curve",
            )
        if len(projected_points) >= 2:
            draw.line(
                projected_points,
                fill=self._hex_rgba(WAVEFORM_CURVE),
                width=energy_width,
                joint="curve",
            )

        if highlight is not None:
            x1, x2 = highlight
            bracket_y = plot_top + s(6)
            bracket_arm = s(8)
            draw.line((x1, bracket_y, x2, bracket_y), fill=self._hex_rgba(WAVEFORM_MARKER), width=marker_width)
            draw.line((x1, plot_top, x1, bracket_y + bracket_arm), fill=self._hex_rgba(WAVEFORM_MARKER), width=marker_width)
            draw.line((x2, plot_top, x2, bracket_y + bracket_arm), fill=self._hex_rgba(WAVEFORM_MARKER), width=marker_width)

            self._waveform_loudest_region = (
                int(highlight_1x[0]),
                int(plot_top_1x),
                int(highlight_1x[1]),
                int(plot_bottom_1x),
            )
            self._waveform_loudest_tooltip_text = self._format_waveform_loudest_tooltip(preview)

        draw_inset_frame(
            draw,
            plot_left,
            plot_top,
            plot_right,
            plot_bottom,
            border=BORDER_COLOR,
            highlight=SIGNAL_FRAME_HIGHLIGHT,
            shadow=SIGNAL_FRAME_SHADOW,
            width=max(1, scale),
        )
        draw_corner_brackets(
            draw,
            plot_left,
            plot_top,
            plot_right,
            plot_bottom,
            color=ACCENT,
            alpha=SIGNAL_BRACKET_ALPHA,
            arm=s(10.0),
            width=max(1, scale),
        )
        draw_scanlines(
            draw,
            plot_left,
            plot_top,
            plot_right,
            plot_bottom,
            spacing=s(float(SIGNAL_SCANLINE_SPACING)),
            alpha=SIGNAL_SCANLINE_ALPHA,
        )

        img = img.resize((width, height), Image.Resampling.LANCZOS)
        text_draw = ImageDraw.Draw(img)
        layout = {
            "plot_left": plot_left_1x,
            "plot_right": plot_right_1x,
            "plot_width": plot_width_1x,
            "plot_top": plot_top_1x,
            "plot_bottom": plot_bottom_1x,
            "target_band": target_band_1x,
            "highlight": highlight_1x,
            "target_low": target_low,
            "target_high": target_high,
            "lufs_min": lufs_min,
            "lufs_max": lufs_max,
            "y_for_lufs": y_for_lufs_1x,
            "ui_scale": ui_scale,
        }
        self._draw_waveform_text_1x(
            text_draw,
            width,
            height,
            preview=preview,
            layout=layout,
        )
        return img

    def _draw_waveform_canvas(self, message: str | None = None) -> None:
        if not hasattr(self, "waveform_canvas"):
            return

        canvas = self.waveform_canvas
        self._hide_waveform_hover_tip()
        if message is None:
            self._waveform_loudest_region = None
            self._waveform_loudest_tooltip_text = ""

        width, height = self._waveform_plot_size()
        preview = None if message is not None else self._current_waveform_data

        try:
            pil_image = self._render_waveform_pil_image(
                width,
                height,
                preview=preview if isinstance(preview, dict) else None,
                message=message,
            )
            self._waveform_pil_image = pil_image
            self._waveform_photo = ImageTk.PhotoImage(pil_image)
            canvas.delete("all")
            canvas.create_image(0, 0, anchor="nw", image=self._waveform_photo)
        except Exception as exc:
            self._logger.exception("Waveform render failed: %s", exc)
            try:
                fallback = self._draw_waveform_message_image(width, height, "Waveform preview failed")
                self._waveform_photo = ImageTk.PhotoImage(fallback)
                canvas.delete("all")
                canvas.create_image(0, 0, anchor="nw", image=self._waveform_photo)
            except Exception:
                pass
