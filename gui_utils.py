"""DropGain GUI utilities: DPI scaling, tooltips, and thread-safe logging."""

from __future__ import annotations

import logging
import os
import queue
import sys
import time
from collections.abc import Callable
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from gui_theme import (
    ACCENT,
    BG_FIELD,
    BG_MAIN,
    BORDER_COLOR,
    FG_MAIN,
    TOOLTIP_OFFSET_X,
    TOOLTIP_OFFSET_Y,
    TOOLTIP_PADX,
    TOOLTIP_PADY,
    TOOLTIP_SCREEN_MARGIN,
    TREEVIEW_ROW_EXTRA_PAD,
    TREEVIEW_ROW_HEIGHT,
    TYPE_MICRO,
    BRAND_DISPLAY_FONT_CANDIDATES,
    METRIC_TILE_VALUE_FONT_CANDIDATES,
    TABLE_CELL_FONT_FAMILY,
    TABLE_CELL_FONT_FALLBACKS,
    UI_ACCENT_FONT_CANDIDATES,
    UI_BODY_FONT_CANDIDATES,
    WINDOW_DESIGN_DEFAULT_HEIGHT,
    WINDOW_DESIGN_DEFAULT_WIDTH,
    WINDOW_DESIGN_MIN_HEIGHT,
    WINDOW_DESIGN_MIN_HEIGHT_FLOOR,
    WINDOW_DESIGN_MIN_WIDTH,
    WINDOW_DESIGN_MIN_WIDTH_FLOOR,
    WINDOW_SCREEN_MARGIN_X,
    WINDOW_SCREEN_MARGIN_Y,
)


def telemetry_caption(text: str) -> str:
    """Prefix instrument-panel readouts with a console-style marker."""
    stripped = text.strip()
    if stripped.startswith("// "):
        return stripped
    return f"// {stripped}"


def telemetry_plain(text: str) -> str:
    """Strip a console-style telemetry marker when present."""
    stripped = text.strip()
    if stripped.startswith("// "):
        return stripped[3:].strip()
    return stripped


def _fonts_directory() -> Path:
    """Return fonts next to this module, or next to the frozen executable."""
    beside_module = Path(__file__).resolve().parent / "fonts"
    if beside_module.is_dir():
        return beside_module
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "fonts"
    return beside_module


def _register_font_file(root: tk.Misc, path: Path) -> tuple[str, ...]:
    resolved = path.resolve()
    if not resolved.is_file():
        return ()

    before = {str(name).lower() for name in root.tk.call("font", "families")}

    if os.name == "nt":
        try:
            import ctypes

            added = ctypes.windll.gdi32.AddFontResourceExW(str(resolved), 0x10, 0)
            if added == 0:
                ctypes.windll.gdi32.AddFontResourceExW(str(resolved), 0, 0)
        except Exception:
            return ()
    else:
        try:
            internal_name = f"dropgain_{abs(hash(str(resolved))) & 0xFFFFFFFF:08x}"
            root.tk.call("font", "create", internal_name, "-file", str(resolved))
        except Exception:
            return ()

    available = {str(name).lower(): str(name) for name in root.tk.call("font", "families")}
    return tuple(available[key] for key in available if key not in before)


def register_app_fonts(root: tk.Misc) -> tuple[str, ...]:
    """Load bundled/optional fonts from fonts/ (including subfolders) for this process."""
    fonts_dir = _fonts_directory()
    if not fonts_dir.is_dir():
        return ()

    registered: list[str] = []
    paths = sorted(fonts_dir.rglob("*.ttf")) + sorted(fonts_dir.rglob("*.otf"))
    for path in paths:
        for family in _register_font_file(root, path):
            if family not in registered:
                registered.append(family)
    return tuple(registered)


def resolve_font_family(
    root: tk.Misc,
    registered: tuple[str, ...],
    candidates: tuple[str, ...],
) -> str:
    available = {name.lower(): name for name in root.tk.call("font", "families")}
    registered_lower = {name.lower() for name in registered}
    for candidate in candidates:
        key = candidate.lower()
        if key in registered_lower:
            return available[key]
    for candidate in candidates:
        resolved = available.get(candidate.lower())
        if resolved:
            return resolved
    if candidates == UI_BODY_FONT_CANDIDATES:
        return "Segoe UI" if os.name == "nt" else "Helvetica"
    return resolve_body_font_family(root, registered)


def resolve_body_font_family(root: tk.Misc, registered: tuple[str, ...]) -> str:
    return resolve_font_family(root, registered, UI_BODY_FONT_CANDIDATES)


def resolve_table_cell_family(root: tk.Misc, registered: tuple[str, ...]) -> str:
    return resolve_font_family(
        root,
        registered,
        (TABLE_CELL_FONT_FAMILY, *TABLE_CELL_FONT_FALLBACKS),
    )


def resolve_brand_display_family(root: tk.Misc, registered: tuple[str, ...]) -> str:
    return resolve_font_family(root, registered, BRAND_DISPLAY_FONT_CANDIDATES)


def resolve_metric_value_family(root: tk.Misc, registered: tuple[str, ...]) -> str:
    return resolve_font_family(root, registered, METRIC_TILE_VALUE_FONT_CANDIDATES)


def resolve_ui_accent_family(root: tk.Misc, registered: tuple[str, ...]) -> str:
    return resolve_font_family(root, registered, UI_ACCENT_FONT_CANDIDATES)


def enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return

    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def enable_macos_ctk_scaling(root: tk.Misc) -> None:
    """Match CustomTkinter widget scale to Retina logical DPI on macOS."""
    if sys.platform != "darwin":
        return

    scale = ui_scale_for(root)
    if scale <= 1.05:
        return

    try:
        import customtkinter as ctk

        ctk.set_widget_scaling(scale)
        ctk.set_window_scaling(scale)
    except Exception:
        pass


def ui_scale_for(widget: tk.Misc) -> float:
    try:
        return max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except Exception:
        return 1.0


def scaled_px_from_float(scale: float, value: float | int) -> int:
    return max(1, int(round(float(value) * scale)))


def scaled_px(widget: tk.Misc, value: float | int) -> int:
    return scaled_px_from_float(ui_scale_for(widget), value)


def logical_screen_size(widget: tk.Misc) -> tuple[int, int]:
    scale = ui_scale_for(widget)
    return (
        max(1, int(widget.winfo_screenwidth() / scale)),
        max(1, int(widget.winfo_screenheight() / scale)),
    )


def logical_widget_width(widget: tk.Misc) -> int:
    try:
        width = int(widget.winfo_width())
        if width <= 1:
            return 0
        return max(1, int(width / ui_scale_for(widget)))
    except Exception:
        return 0


def fit_window_bounds(
    widget: tk.Misc,
    *,
    default_width: int = WINDOW_DESIGN_DEFAULT_WIDTH,
    default_height: int = WINDOW_DESIGN_DEFAULT_HEIGHT,
    design_min_width: int = WINDOW_DESIGN_MIN_WIDTH,
    design_min_height: int = WINDOW_DESIGN_MIN_HEIGHT,
) -> tuple[int, int, int, int]:
    """Return width, height, min_width, min_height in CTk logical units."""
    logical_w, logical_h = logical_screen_size(widget)
    max_w = max(WINDOW_DESIGN_MIN_WIDTH_FLOOR, logical_w - WINDOW_SCREEN_MARGIN_X)
    max_h = max(WINDOW_DESIGN_MIN_HEIGHT_FLOOR, logical_h - WINDOW_SCREEN_MARGIN_Y)
    min_w = min(design_min_width, max_w)
    min_h = min(design_min_height, max_h)
    width = min(default_width, max_w)
    height = min(default_height, max(640, max_h))
    return width, height, min_w, min_h


def treeview_rowheight_px(cell_linespace: int, ui_scale: float) -> int:
    """Device-pixel row height; cell_linespace already reflects monitor DPI."""
    return max(
        scaled_px_from_float(ui_scale, TREEVIEW_ROW_HEIGHT),
        cell_linespace + TREEVIEW_ROW_EXTRA_PAD,
    )


def treeview_column_width_px(text_width: int, pad: int) -> int:
    """Device-pixel column width; text_width from font.measure() is already DPI-aware."""
    return text_width + pad


def make_tooltip_label(
    parent: tk.Misc,
    text: str,
    *,
    wraplength: int,
) -> tk.Label:
    return tk.Label(
        parent,
        text=text,
        justify="left",
        anchor="w",
        wraplength=wraplength,
        background=BG_FIELD,
        foreground=FG_MAIN,
        activebackground=BG_FIELD,
        activeforeground=FG_MAIN,
        relief="solid",
        borderwidth=1,
        padx=TOOLTIP_PADX,
        pady=TOOLTIP_PADY,
        font=(resolve_body_font_family(parent, ()), TYPE_MICRO),
    )


def position_tooltip_window(
    window: tk.Toplevel,
    root_x: int,
    root_y: int,
) -> None:
    x = root_x + TOOLTIP_OFFSET_X
    y = root_y + TOOLTIP_OFFSET_Y
    try:
        window.update_idletasks()
        width = window.winfo_reqwidth()
        height = window.winfo_reqheight()
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        x = min(x, max(0, screen_width - width - TOOLTIP_SCREEN_MARGIN))
        y = min(y, max(0, screen_height - height - TOOLTIP_SCREEN_MARGIN))
        window.geometry(f"+{x}+{y}")
    except Exception:
        pass


class GuiQueueLogHandler(logging.Handler):
    """Forward log records to the Tkinter UI queue for thread-safe display."""

    def __init__(self, ui_queue: queue.Queue[tuple[str, object]]) -> None:
        super().__init__()
        self._ui_queue = ui_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            if not message.endswith("\n"):
                message += "\n"

            if record.levelno >= logging.ERROR:
                tag = "error"
            elif record.levelno >= logging.WARNING:
                tag = "warn"
            else:
                tag = None

            self._ui_queue.put(("log_message", (message, tag)))
        except Exception:
            self.handleError(record)


class DropGainTooltip:
    """Small delayed tooltip for Tkinter/CustomTkinter widgets."""

    def __init__(
        self,
        widget: tk.Widget,
        text: str,
        *,
        delay_ms: int = 550,
        wraplength: int = 390,
    ) -> None:
        self.widget = widget
        self.text = text.strip()
        self.delay_ms = delay_ms
        self.wraplength = wraplength
        self._after_id: str | None = None
        self._tip_window: tk.Toplevel | None = None
        self._last_root_x = 0
        self._last_root_y = 0
        self._bind_recursive(widget)

    def _bind_recursive(self, widget: tk.Widget) -> None:
        for sequence, callback in (
            ("<Enter>", self._on_enter),
            ("<Motion>", self._on_motion),
            ("<Leave>", self._on_leave),
            ("<ButtonPress>", self._on_leave),
        ):
            try:
                widget.bind(sequence, callback, add=True)
            except Exception:
                pass

        try:
            children = widget.winfo_children()
        except Exception:
            children = []

        for child in children:
            self._bind_recursive(child)

    def _on_enter(self, event: tk.Event) -> None:
        self._remember_pointer(event)
        self._schedule()

    def _on_motion(self, event: tk.Event) -> None:
        self._remember_pointer(event)
        if self._tip_window is not None:
            self._position_window()

    def _on_leave(self, _event: tk.Event | None = None) -> None:
        self._cancel_schedule()
        try:
            self.widget.after(80, self._hide_if_pointer_left)
        except Exception:
            self._hide()

    def _remember_pointer(self, event: tk.Event) -> None:
        try:
            self._last_root_x = int(event.x_root)
            self._last_root_y = int(event.y_root)
        except Exception:
            try:
                self._last_root_x = self.widget.winfo_pointerx()
                self._last_root_y = self.widget.winfo_pointery()
            except Exception:
                pass

    def _schedule(self) -> None:
        if not self.text or self._tip_window is not None:
            return
        self._cancel_schedule()
        try:
            self._after_id = self.widget.after(self.delay_ms, self._show)
        except Exception:
            self._after_id = None

    def _cancel_schedule(self) -> None:
        if self._after_id is None:
            return
        try:
            self.widget.after_cancel(self._after_id)
        except Exception:
            pass
        self._after_id = None

    def _pointer_inside_widget(self) -> bool:
        try:
            x = self.widget.winfo_pointerx()
            y = self.widget.winfo_pointery()
            left = self.widget.winfo_rootx()
            top = self.widget.winfo_rooty()
            right = left + self.widget.winfo_width()
            bottom = top + self.widget.winfo_height()
            return left <= x <= right and top <= y <= bottom
        except Exception:
            return False

    def _hide_if_pointer_left(self) -> None:
        if not self._pointer_inside_widget():
            self._hide()

    def _show(self) -> None:
        self._after_id = None
        if self._tip_window is not None or not self.text:
            return

        try:
            if not self.widget.winfo_exists() or not self._pointer_inside_widget():
                return
        except Exception:
            return

        window = tk.Toplevel(self.widget)
        window.withdraw()
        window.overrideredirect(True)
        try:
            window.attributes("-topmost", True)
        except Exception:
            pass

        label = make_tooltip_label(
            window,
            self.text,
            wraplength=self.wraplength,
        )
        label.pack()
        self._tip_window = window
        self._position_window()
        window.deiconify()

    def _position_window(self) -> None:
        window = self._tip_window
        if window is None:
            return

        position_tooltip_window(window, self._last_root_x, self._last_root_y)

    def _hide(self) -> None:
        window = self._tip_window
        self._tip_window = None
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass


class TreeviewHeadingTooltip:
    """Delayed hover tooltips for ttk.Treeview column headings."""

    def __init__(
        self,
        treeview: ttk.Treeview,
        column_tooltips: dict[str, str],
        *,
        delay_ms: int = 550,
        wraplength: int = 390,
    ) -> None:
        self.treeview = treeview
        self.column_tooltips = {
            column_id: text.strip()
            for column_id, text in column_tooltips.items()
            if text.strip()
        }
        self.delay_ms = delay_ms
        self.wraplength = wraplength
        self._after_id: str | None = None
        self._tip_window: tk.Toplevel | None = None
        self._active_column: str | None = None
        self._pending_text = ""
        self._last_root_x = 0
        self._last_root_y = 0
        self._column_index_map = self._build_column_index_map()

        treeview.bind("<Motion>", self._on_motion, add=True)
        treeview.bind("<Leave>", self._on_leave, add=True)
        treeview.bind("<ButtonPress>", self._on_leave, add=True)

    def _build_column_index_map(self) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for index, column_id in enumerate(self.treeview["columns"], start=1):
            mapping[index] = str(column_id)
        return mapping

    def _column_at(self, event: tk.Event) -> str | None:
        try:
            if self.treeview.identify_region(event.x, event.y) != "heading":
                return None
            column = self.treeview.identify_column(event.x)
        except Exception:
            return None
        if not column.startswith("#"):
            return None
        try:
            column_index = int(column[1:])
        except ValueError:
            return None
        return self._column_index_map.get(column_index)

    def _on_motion(self, event: tk.Event) -> None:
        self._last_root_x = int(event.x_root)
        self._last_root_y = int(event.y_root)
        column_id = self._column_at(event)
        if column_id is None:
            self._clear_active()
            return

        tooltip_text = self.column_tooltips.get(column_id, "")
        if not tooltip_text:
            self._clear_active()
            return

        if column_id == self._active_column:
            if self._tip_window is not None:
                self._position_window()
            return

        self._hide()
        self._active_column = column_id
        self._pending_text = tooltip_text
        self._schedule()

    def _on_leave(self, _event: tk.Event | None = None) -> None:
        self._clear_active()

    def _schedule(self) -> None:
        if not self._pending_text or self._tip_window is not None:
            return
        self._cancel_schedule()
        try:
            self._after_id = self.treeview.after(self.delay_ms, self._show)
        except Exception:
            self._after_id = None

    def _cancel_schedule(self) -> None:
        if self._after_id is None:
            return
        try:
            self.treeview.after_cancel(self._after_id)
        except Exception:
            pass
        self._after_id = None

    def _clear_active(self) -> None:
        self._active_column = None
        self._pending_text = ""
        self._cancel_schedule()
        self._hide()

    def _show(self) -> None:
        self._after_id = None
        if self._tip_window is not None or not self._pending_text:
            return

        try:
            if not self.treeview.winfo_exists():
                return
        except Exception:
            return

        window = tk.Toplevel(self.treeview)
        window.withdraw()
        window.overrideredirect(True)
        try:
            window.attributes("-topmost", True)
        except Exception:
            pass

        label = make_tooltip_label(
            window,
            self._pending_text,
            wraplength=self.wraplength,
        )
        label.pack()
        self._tip_window = window
        self._position_window()
        window.deiconify()

    def _position_window(self) -> None:
        window = self._tip_window
        if window is None:
            return

        position_tooltip_window(window, self._last_root_x, self._last_root_y)

    def _hide(self) -> None:
        window = self._tip_window
        self._tip_window = None
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass


_ALPHA_SUPPORT: bool | None = None


def _toplevel_alpha_supported(root: tk.Misc) -> bool:
    global _ALPHA_SUPPORT
    if _ALPHA_SUPPORT is not None:
        return _ALPHA_SUPPORT
    try:
        probe = tk.Toplevel(root)
        probe.withdraw()
        probe.attributes("-alpha", 0.5)
        probe.destroy()
        _ALPHA_SUPPORT = True
    except Exception:
        _ALPHA_SUPPORT = False
    return _ALPHA_SUPPORT


class ContentFadeTransition:
    """Full-page overlay that masks navigation until the target page has settled."""

    PEAK_ALPHA = 1.0
    STEP_MS = 14
    GLITCH_STEPS = 24
    FADE_IN_STEPS = 4
    FADE_OUT_STEPS = 12
    SETTLE_MS = 150
    LINE_FADE_IN_SEC = 0.09
    EDGE_BLEED = 3
    FINAL_UPDATE_PASSES = 3
    GLITCH_FRAGMENTS = (
        # x_frac, y_frac, w_frac, h_frac, direction, color, start
        (0.14, 0.18, 0.050, 0.007, -1, BORDER_COLOR, 0.00),
        (0.68, 0.14, 0.038, 0.006, 1, ACCENT, 0.05),
        (0.42, 0.31, 0.044, 0.006, 1, ACCENT, 0.09),
        (0.24, 0.47, 0.032, 0.005, -1, BORDER_COLOR, 0.14),
        (0.76, 0.39, 0.041, 0.007, -1, ACCENT, 0.19),
        (0.52, 0.56, 0.036, 0.006, 1, BORDER_COLOR, 0.24),
        (0.18, 0.63, 0.047, 0.008, 1, ACCENT, 0.29),
        (0.84, 0.58, 0.030, 0.005, -1, BORDER_COLOR, 0.34),
        (0.36, 0.72, 0.040, 0.006, -1, ACCENT, 0.39),
        (0.58, 0.81, 0.034, 0.006, 1, BORDER_COLOR, 0.44),
    )

    def __init__(self, root: tk.Misc) -> None:
        self._root = root
        self._active = False
        self._overlay: tk.Toplevel | None = None
        self._curtain_canvas: tk.Canvas | None = None
        self._curtain_size: tuple[int, int] = (0, 0)
        self._fragment_items: list[int] = []
        self._swapped = False
        self._anim_after_id: str | None = None
        self._build_overlay_shell()

    @property
    def active(self) -> bool:
        return self._active

    @staticmethod
    def _ease_in_out_cubic(value: float) -> float:
        value = max(0.0, min(1.0, value))
        if value < 0.5:
            return 4.0 * value * value * value
        return 1.0 - pow(-2.0 * value + 2.0, 3.0) / 2.0

    @staticmethod
    def _ease_out_cubic(value: float) -> float:
        value = max(0.0, min(1.0, value))
        return 1.0 - (1.0 - value) ** 3

    @staticmethod
    def _mix_toward_bg(color: str, opacity: float) -> str:
        opacity = max(0.0, min(1.0, opacity))
        if opacity >= 1.0:
            return color
        if opacity <= 0.0:
            return BG_MAIN
        from_rgb = tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))
        bg_rgb = tuple(int(BG_MAIN[i : i + 2], 16) for i in (1, 3, 5))
        return "#" + "".join(
            f"{int(round(b + (f - b) * opacity)):02x}" for f, b in zip(from_rgb, bg_rgb)
        )

    @staticmethod
    def _band_window(progress: float, start: float, duration: float) -> float:
        if duration <= 0.0:
            return 0.0
        return max(0.0, min(1.0, (progress - start) / duration))

    def run(
        self,
        container: tk.Widget,
        swap: Callable[[], None],
        *,
        before_reveal: Callable[[], None] | None = None,
        on_complete: Callable[[], None] | None = None,
        settle_ms: int | None = None,
    ) -> None:
        if self._active or not _toplevel_alpha_supported(self._root):
            self._finish_after_swap(swap, before_reveal, on_complete)
            return

        self._active = True
        self._swapped = False
        self._ensure_overlay(container)
        tick_sec = self.STEP_MS / 1000.0
        fade_in_sec = self.FADE_IN_STEPS * tick_sec
        glitch_sec = self.GLITCH_STEPS * tick_sec
        fade_out_sec = self.FADE_OUT_STEPS * tick_sec
        hold_steps = max(6, (self.SETTLE_MS if settle_ms is None else max(0, int(settle_ms))) // self.STEP_MS)
        hold_sec = hold_steps * tick_sec
        motion_sec = glitch_sec + hold_sec + fade_out_sec
        phase = "cover"
        started_at = time.perf_counter()

        def abort_if_overlay_gone() -> bool:
            if self._overlay_alive():
                return False
            if not self._swapped:
                self._finish_after_swap(swap, before_reveal, on_complete)
            else:
                self._active = False
            return True

        def animate() -> None:
            nonlocal phase, started_at
            self._anim_after_id = None
            if not self._active or abort_if_overlay_gone():
                return

            elapsed = time.perf_counter() - started_at

            if phase == "cover":
                if elapsed < fade_in_sec:
                    self._set_overlay_alpha((elapsed / fade_in_sec) * self.PEAK_ALPHA)
                    self._anim_after_id = self._root.after(self.STEP_MS, animate)
                    return
                self._set_overlay_alpha(self.PEAK_ALPHA)
                phase = "swap"
                # Yield so the opaque curtain can paint before layout work blocks.
                self._anim_after_id = self._root.after(1, animate)
                return

            if phase == "swap":
                if not self._swapped:
                    self._swapped = True
                    try:
                        swap()
                        self._run_before_reveal(container, before_reveal)
                    except Exception:
                        self._release_overlay()
                        self._active = False
                        raise
                phase = "motion"
                started_at = time.perf_counter()
                elapsed = 0.0

            if elapsed < glitch_sec:
                progress = min(1.0, elapsed / glitch_sec) if glitch_sec > 0 else 1.0
                appear = self._ease_out_cubic(
                    min(1.0, elapsed / self.LINE_FADE_IN_SEC) if self.LINE_FADE_IN_SEC > 0 else 1.0
                )
                self._draw_glitch_frame(progress, 1.0, opacity=appear, hold_idle=True)
            elif elapsed < glitch_sec + hold_sec:
                hold_progress = (elapsed - glitch_sec) / hold_sec if hold_sec > 0 else 1.0
                self._draw_glitch_frame(1.0, max(0.0, 1.0 - hold_progress))
            else:
                self._hide_glitch_fragments()
                fade_progress = (
                    (elapsed - glitch_sec - hold_sec) / fade_out_sec if fade_out_sec > 0 else 1.0
                )
                self._set_overlay_alpha(max(0.0, (1.0 - fade_progress) * self.PEAK_ALPHA))

            if elapsed >= motion_sec:
                try:
                    self._flush_pending_ui(container, include_timers=True, passes=self.FINAL_UPDATE_PASSES)
                finally:
                    self._release_overlay()
                    self._active = False
                    if on_complete is not None:
                        on_complete()
                return

            self._anim_after_id = self._root.after(self.STEP_MS, animate)

        self._anim_after_id = self._root.after(1, animate)

    def _init_glitch_fragments(self) -> None:
        canvas = self._curtain_canvas
        if canvas is None or self._fragment_items:
            return
        for *_rest, color, _start in self.GLITCH_FRAGMENTS:
            item = canvas.create_rectangle(0, 0, 0, 0, fill=color, outline="", state="hidden")
            self._fragment_items.append(item)

    def _hide_glitch_fragments(self) -> None:
        canvas = self._curtain_canvas
        if canvas is None:
            return
        for item in self._fragment_items:
            canvas.itemconfigure(item, state="hidden")

    def _draw_glitch_frame(
        self,
        progress: float,
        intensity: float,
        *,
        opacity: float = 1.0,
        hold_idle: bool = False,
    ) -> None:
        canvas = self._curtain_canvas
        if canvas is None:
            return

        width, height = self._curtain_size
        if width < 1 or height < 1:
            return

        self._init_glitch_fragments()
        if intensity <= 0.0 or opacity <= 0.0:
            self._hide_glitch_fragments()
            return

        max_shift = width * 0.04 * intensity
        fragment_duration = 0.58

        for item, (x_frac, y_frac, w_frac, h_frac, direction, color, start) in zip(
            self._fragment_items,
            self.GLITCH_FRAGMENTS,
            strict=True,
        ):
            local_t = self._band_window(progress, start, fragment_duration)
            if local_t <= 0.0:
                if not hold_idle:
                    canvas.itemconfigure(item, state="hidden")
                    continue
                local_t = 0.0

            eased = self._ease_in_out_cubic(local_t)
            offset_x = direction * max_shift * (eased * 2.0 - 1.0)
            frag_w = max(8.0, width * w_frac)
            frag_h = max(1.0, height * h_frac)
            x1 = width * x_frac + offset_x
            y1 = height * y_frac
            canvas.coords(item, x1, y1, x1 + frag_w, y1 + frag_h)
            canvas.itemconfigure(item, fill=self._mix_toward_bg(color, opacity), state="normal")

    def _finish_after_swap(
        self,
        swap: Callable[[], None],
        before_reveal: Callable[[], None] | None,
        on_complete: Callable[[], None] | None,
    ) -> None:
        try:
            swap()
            self._run_before_reveal(None, before_reveal)
        finally:
            self._release_overlay()
            self._active = False
            if on_complete is not None:
                on_complete()

    def _set_overlay_alpha(self, value: float) -> None:
        overlay = self._overlay
        if overlay is None:
            return
        try:
            overlay.attributes("-alpha", max(0.0, min(self.PEAK_ALPHA, value)))
        except Exception:
            pass

    def _cancel_anim(self) -> None:
        if self._anim_after_id is None:
            return
        try:
            self._root.after_cancel(self._anim_after_id)
        except Exception:
            pass
        self._anim_after_id = None

    def _run_before_reveal(
        self,
        container: tk.Widget | None,
        before_reveal: Callable[[], None] | None,
    ) -> None:
        if before_reveal is not None:
            before_reveal()
        self._flush_pending_ui(container, include_timers=False, passes=2)

    def _flush_pending_ui(
        self,
        container: tk.Widget | None,
        *,
        include_timers: bool,
        passes: int,
    ) -> None:
        """Flush geometry, drawing, and optionally due Tk timers while covered."""
        for _ in range(max(1, int(passes))):
            try:
                self._root.update_idletasks()
                if self._overlay is not None and container is not None:
                    self._position_overlay(self._overlay, container)
                if include_timers:
                    # update() is intentionally limited to the final covered
                    # phase. It lets due after()/after_idle callbacks finish
                    # before the curtain is removed, avoiding visible settling.
                    self._root.update()
                else:
                    self._root.update_idletasks()
            except Exception:
                return

    def _overlay_alive(self) -> bool:
        overlay = self._overlay
        if overlay is None:
            return False
        try:
            return bool(overlay.winfo_exists())
        except tk.TclError:
            return False

    def _build_overlay_shell(self) -> None:
        if self._overlay_alive():
            return
        self._drop_overlay()
        try:
            overlay = tk.Toplevel(self._root)
            overlay.withdraw()
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            overlay.attributes("-alpha", 0.0)
            overlay.configure(bg=BG_MAIN)
            self._curtain_canvas = tk.Canvas(
                overlay,
                highlightthickness=0,
                bd=0,
                bg=BG_MAIN,
            )
            self._curtain_canvas.pack(fill="both", expand=True)
            self._overlay = overlay
            self._fragment_items = []
            self._init_glitch_fragments()
        except Exception:
            self._drop_overlay()

    def _ensure_overlay(self, container: tk.Widget) -> None:
        if not self._overlay_alive():
            self._build_overlay_shell()
        self._show_overlay(container)

    def _show_overlay(self, container: tk.Widget) -> None:
        overlay = self._overlay
        if overlay is None:
            return
        self._hide_glitch_fragments()
        self._set_overlay_alpha(0.0)
        try:
            overlay.withdraw()
        except tk.TclError:
            pass
        container.update_idletasks()
        self._position_overlay(overlay, container)
        overlay.deiconify()
        try:
            overlay.lift()
        except Exception:
            pass

    def _position_overlay(self, overlay: tk.Toplevel, container: tk.Widget) -> None:
        try:
            bleed = self.EDGE_BLEED
            x = container.winfo_rootx() - bleed
            y = container.winfo_rooty() - bleed
            w = max(container.winfo_width(), 1) + bleed * 2
            h = max(container.winfo_height(), 1) + bleed * 2
            overlay.geometry(f"{w}x{h}+{x}+{y}")
            self._curtain_size = (w, h)
            if self._curtain_canvas is not None:
                self._curtain_canvas.configure(width=w, height=h)
        except Exception:
            pass

    def _release_overlay(self) -> None:
        self._cancel_anim()
        if not self._overlay_alive():
            self._drop_overlay()
            return
        self._hide_glitch_fragments()
        self._set_overlay_alpha(0.0)
        overlay = self._overlay
        if overlay is None:
            return
        try:
            overlay.withdraw()
        except tk.TclError:
            self._drop_overlay()

    def _drop_overlay(self) -> None:
        self._cancel_anim()
        overlay = self._overlay
        self._overlay = None
        self._curtain_canvas = None
        self._curtain_size = (0, 0)
        self._fragment_items = []
        if overlay is not None:
            try:
                overlay.destroy()
            except Exception:
                pass


def apply_hand_cursor(widget: tk.Widget) -> None:
    """Use a pointer cursor for clickable widgets."""
    try:
        widget.configure(cursor="hand2")
    except Exception:
        pass


def pointer_inside_widget(widget: tk.Widget) -> bool:
    """Return True if the screen pointer is currently inside the widget bounds."""
    try:
        x = widget.winfo_pointerx()
        y = widget.winfo_pointery()
        left = widget.winfo_rootx()
        top = widget.winfo_rooty()
        right = left + widget.winfo_width()
        bottom = top + widget.winfo_height()
        return left <= x <= right and top <= y <= bottom
    except Exception:
        return False


def wire_ctk_button_press(
    button: tk.Widget,
    press_fg: Callable[[], str],
    *,
    restore: Callable[[], None] | None = None,
) -> None:
    """Briefly darken a CTkButton while the primary mouse button is held."""
    if getattr(button, "_dropgain_press_wired", False):
        return
    button._dropgain_press_wired = True  # type: ignore[attr-defined]

    def _restore(_event: tk.Event | None = None) -> None:
        if not getattr(button, "_dropgain_pressing", False):
            return
        button._dropgain_pressing = False  # type: ignore[attr-defined]
        if restore is not None:
            restore()
            return
        saved = getattr(button, "_dropgain_saved_fg", None)
        if saved is not None:
            button.configure(fg_color=saved)

    def _on_press(_event: tk.Event) -> None:
        try:
            if str(button.cget("state")) == "disabled":
                return
        except Exception:
            return
        button._dropgain_pressing = True  # type: ignore[attr-defined]
        try:
            button._dropgain_saved_fg = button.cget("fg_color")  # type: ignore[attr-defined]
            button.configure(fg_color=press_fg())
        except Exception:
            button._dropgain_pressing = False  # type: ignore[attr-defined]

    button.bind("<ButtonPress-1>", _on_press, add="+")
    button.bind("<ButtonRelease-1>", _restore, add="+")
    button.bind("<Leave>", _restore, add="+")
