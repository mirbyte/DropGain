"""DropGain preferences page."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import tkinter as tk

import customtkinter as ctk

from analysis import (
    LOSSLESS_MIN_ABS_GAIN_DB,
    MAX_LOUD_SECTION_HOP_SECONDS,
    MAX_ANALYSIS_WORKER_THREADS,
    MAX_LOUD_SECTION_WINDOW_SECONDS,
    MAX_BASS_MAX_BOOST_REDUCTION_DB,
    MAX_BASS_PENALTY_THRESHOLD_DB,
    MIN_LOUD_SECTION_HOP_SECONDS,
    MIN_ANALYSIS_WORKER_THREADS,
    MIN_LOUD_SECTION_WINDOW_SECONDS,
    MIN_BASS_MAX_BOOST_REDUCTION_DB,
    MIN_BASS_PENALTY_THRESHOLD_DB,
    LIMITER_ENGINE_CHOICES,
    MP3_MIN_ABS_GAIN_DB,
    NORMALIZATION_MODE_CHOICES,
    OUTPUT_FORMAT_MODE_CHOICES,
    PROCESSED_SUFFIX,
    default_csv_path,
    output_format_mode_tooltip,
)
from gui_theme import (
    ACCENT,
    ACCENT_HOVER,
    BG_CARD,
    BG_FIELD,
    BG_MAIN,
    BORDER_COLOR,
    BUTTON_CORNER_RADIUS,
    BUTTON_SECONDARY_ACTIVE,
    BUTTON_SECONDARY_BG,
    BUTTON_SECONDARY_HOVER,
    BUTTON_TEXT_DARK,
    CARD_CORNER_RADIUS,
    ERROR_FG,
    FG_MAIN,
    FG_MUTED,
    ICE_DIM,
    PAGE_PADX,
    PREFERENCES_LAYOUT_DEBOUNCE_MS,
    PREFERENCES_SINGLE_COLUMN_WIDTH,
    PREFERENCES_TWO_COLUMN_WIDTH,
    SECTION_GAP,
    SPACE_3,
    SUCCESS_FG,
    TYPE_BODY,
    TYPE_LABEL,
)
from gui_utils import apply_hand_cursor, logical_widget_width, pointer_inside_widget, wire_ctk_button_press

if TYPE_CHECKING:
    from gui_tk import App

# Slightly larger than the global type scale for readable settings on HiDPI displays.
SETTINGS_TITLE = 18
SETTINGS_HEAD = TYPE_BODY
SETTINGS_TEXT = TYPE_BODY
SETTINGS_HINT = TYPE_LABEL


class CTkNumberInput(ctk.CTkFrame):
    """Small CustomTkinter numeric input with minus/plus controls."""

    def __init__(
        self,
        master: Any,
        *,
        variable: tk.Variable,
        from_: float,
        to: float,
        increment: float,
        width: int = 76,
        integer: bool = False,
        entry_font: Any = None,
        button_font: Any = None,
        app: Any = None,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._variable = variable
        self._from = float(from_)
        self._to = float(to)
        self._increment = float(increment)
        self._integer = integer
        self._app = app

        self.btn_minus = ctk.CTkButton(
            self,
            text="−",
            width=26,
            height=30,
            fg_color=BUTTON_SECONDARY_BG,
            hover_color=BUTTON_SECONDARY_HOVER,
            border_width=0,
            text_color=FG_MAIN,
            font=button_font or ("Segoe UI", 13, "bold"),
            command=lambda: self._step(-self._increment),
            corner_radius=BUTTON_CORNER_RADIUS,
        )
        self.btn_minus.pack(side="left")

        self.entry = ctk.CTkEntry(
            self,
            textvariable=variable,
            width=width,
            height=30,
            fg_color=BG_FIELD,
            border_color=BORDER_COLOR,
            text_color=FG_MAIN,
            font=entry_font or ("Segoe UI", 12),
        )
        self.entry.pack(side="left", padx=4)

        self.btn_plus = ctk.CTkButton(
            self,
            text="+",
            width=26,
            height=30,
            fg_color=BUTTON_SECONDARY_BG,
            hover_color=BUTTON_SECONDARY_HOVER,
            border_width=0,
            text_color=FG_MAIN,
            font=button_font or ("Segoe UI", 13, "bold"),
            command=lambda: self._step(self._increment),
            corner_radius=BUTTON_CORNER_RADIUS,
        )
        self.btn_plus.pack(side="left")

        for step_button in (self.btn_minus, self.btn_plus):
            apply_hand_cursor(step_button)
            step_button._dropgain_accent = False  # type: ignore[attr-defined]
            if self._app is not None:
                self._app._wire_button_hover(step_button)

            def _restore_stepper(b: ctk.CTkButton = step_button) -> None:
                b.configure(fg_color=BUTTON_SECONDARY_BG, hover_color=BUTTON_SECONDARY_BG)
                tween = getattr(b, "_dropgain_button_tween", None)
                if tween is not None and pointer_inside_widget(b):
                    tween(True)

            wire_ctk_button_press(
                step_button,
                lambda: BUTTON_SECONDARY_ACTIVE,
                restore=_restore_stepper if self._app is not None else lambda b=step_button: b.configure(
                    fg_color=BUTTON_SECONDARY_BG,
                    hover_color=BUTTON_SECONDARY_HOVER,
                ),
            )

    def configure_state(self, state: str) -> None:
        cursor = "hand2" if state == "normal" else ""
        self.btn_minus.configure(state=state, cursor=cursor)
        self.btn_plus.configure(state=state, cursor=cursor)
        self.entry.configure(state=state)

    def _step(self, delta: float) -> None:
        try:
            current = float(self._variable.get())
        except Exception:
            current = self._from

        value = min(self._to, max(self._from, current + delta))
        if self._integer:
            self._variable.set(int(round(value)))
        else:
            self._variable.set(round(value, 3))


class PreferencesPage(ctk.CTkFrame):
    """Preferences page with grouped settings cards."""

    def __init__(self, app: App, master: Any) -> None:
        super().__init__(master, fg_color=BG_MAIN, corner_radius=0)
        self.app = app
        self._number_inputs: list[CTkNumberInput] = []
        self._setting_card_frames: list[ctk.CTkFrame] = []
        self._preferences_single_column = False
        self._layout_after_id: str | None = None
        self._controls_state: str | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        body = ctk.CTkFrame(self, fg_color=BG_MAIN, corner_radius=0)
        body.grid(row=0, column=0, sticky="nsew", padx=PAGE_PADX, pady=(SECTION_GAP, SECTION_GAP))
        body.grid_columnconfigure((0, 1), weight=1)
        self.body = body

        header = ctk.CTkFrame(body, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        app._label(
            header,
            text="Settings",
            bg=BG_MAIN,
            size=SETTINGS_TITLE,
            weight="bold",
            accent=True,
        ).grid(
            row=0, column=0, sticky="w"
        )

        self.lbl_system_check_status = app._label(
            header,
            text="",
            color=FG_MUTED,
            bg=BG_MAIN,
            size=SETTINGS_HINT,
            justify="left",
        )
        self.lbl_system_check_status.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.lbl_system_check_status.grid_remove()
        self.lbl_system_check_status.bind(
            "<Configure>",
            self._on_system_check_status_configure,
            add="+",
        )

        loudness_card = self._card(body, row=1, column=0)
        self._build_output_loudness_card(loudness_card)

        format_card = self._card(body, row=1, column=1)
        self._build_output_format_card(format_card)

        analysis_card = self._card(body, row=2, column=0)
        self._build_analysis_card(analysis_card)

        reporting_card = self._card(body, row=2, column=1)
        self._build_reporting_card(reporting_card)

        render_card = self._card(body, row=3, column=0)
        self._build_render_rules_card(render_card)

        self.bind("<Configure>", self._on_page_configure, add="+")
        self.after_idle(self.refresh_layout)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=1, column=0, sticky="ew", padx=PAGE_PADX, pady=(0, SPACE_3))
        footer_button_height = 40
        btn_reset_defaults = app._button(
            footer,
            text="Reset Defaults",
            command=app._reset_defaults,
        )
        btn_reset_defaults.configure(
            height=footer_button_height,
            corner_radius=BUTTON_CORNER_RADIUS,
        )
        btn_reset_defaults.pack(side="left")
        self.btn_system_check = app._button(
            footer,
            text="Check Limiter / System",
            command=app._run_system_check,
        )
        self.btn_system_check.configure(
            height=footer_button_height,
            corner_radius=BUTTON_CORNER_RADIUS,
        )
        self.btn_system_check.pack(side="left", padx=(8, 0))

        app.lbl_output_format_hint = self.lbl_output_format_hint
        app.mode_menu = self.mode_menu
        app.limiter_engine_menu = self.limiter_engine_menu
        app.output_format_menu = self.output_format_menu
        app.chk_allow_risky_true_peak_boost = self.chk_allow_risky_true_peak_boost
        app.chk_apply_render_gain_threshold = self.chk_apply_render_gain_threshold
        app.chk_write_csv = self.chk_write_csv
        app._number_inputs = self._number_inputs
        app._refresh_output_format_hint()
        app._refresh_settings_summary()

    def refresh_from_app(self) -> None:
        self.app._refresh_output_format_hint()
        self.app._refresh_settings_summary()

    def refresh_layout(self) -> None:
        self._schedule_preferences_layout()

    def settle_layout_for_reveal(self) -> None:
        """Cancel debounced relayout and settle Preferences while hidden."""
        if self._layout_after_id is not None:
            try:
                self.after_cancel(self._layout_after_id)
            except Exception:
                pass
            self._layout_after_id = None
        self.update_idletasks()
        self._update_preferences_layout(logical_widget_width(self))
        self.update_idletasks()

    def _schedule_preferences_layout(self) -> None:
        if self._layout_after_id is not None:
            try:
                self.after_cancel(self._layout_after_id)
            except Exception:
                pass
        self._layout_after_id = self.after(
            PREFERENCES_LAYOUT_DEBOUNCE_MS,
            self._apply_scheduled_preferences_layout,
        )

    def _apply_scheduled_preferences_layout(self) -> None:
        self._layout_after_id = None
        self._update_preferences_layout(logical_widget_width(self))

    def _on_page_configure(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._schedule_preferences_layout()

    def _update_preferences_layout(self, logical_w: int) -> None:
        if logical_w <= 0:
            return
        if self._preferences_single_column:
            single_column = logical_w < PREFERENCES_TWO_COLUMN_WIDTH
        else:
            single_column = logical_w < PREFERENCES_SINGLE_COLUMN_WIDTH
        if single_column == self._preferences_single_column:
            return
        self._preferences_single_column = single_column
        self._apply_preferences_layout(single_column)

    def _apply_preferences_layout(self, single_column: bool) -> None:
        body = self.body
        if single_column:
            # Clear stale columnspan after switching from two-column layout
            body.grid_columnconfigure(0, weight=1, minsize=0)
            body.grid_columnconfigure(1, weight=0, minsize=0)
            for index, outer in enumerate(self._setting_card_frames):
                outer.grid(
                    row=index + 1,
                    column=0,
                    columnspan=1,
                    sticky="nsew",
                    padx=0,
                    pady=(0, 14),
                )
        else:
            body.grid_columnconfigure(0, weight=1, minsize=0)
            body.grid_columnconfigure(1, weight=1, minsize=0)
            card_count = len(self._setting_card_frames)
            for index, outer in enumerate(self._setting_card_frames):
                row = (index // 2) + 1
                column = index % 2
                if index == card_count - 1 and card_count % 2 == 1:
                    outer.grid(
                        row=row,
                        column=0,
                        columnspan=2,
                        sticky="nsew",
                        padx=0,
                        pady=(0, 14),
                    )
                else:
                    outer.grid(
                        row=row,
                        column=column,
                        columnspan=1,
                        sticky="nsew",
                        padx=(0, 7) if column == 0 else (7, 0),
                        pady=(0, 14),
                    )

    def set_controls_state(self, state: str) -> None:
        # Avoid redundant reconfigure on +/- clicks (idle refresh retriggers this)
        if state == self._controls_state:
            return
        self._controls_state = state

        for number_input in self._number_inputs:
            number_input.configure_state(state)
        for widget in (
            self.mode_menu,
            self.limiter_engine_menu,
            self.output_format_menu,
            self.chk_allow_risky_true_peak_boost,
            self.chk_apply_render_gain_threshold,
            self.chk_write_csv,
            self.csv_display,
            self.btn_system_check,
        ):
            widget.configure(state=state)

    def _card(self, parent: Any, *, row: int, column: int) -> ctk.CTkFrame:
        padx = (0, 7) if column == 0 else (7, 0)
        outer = ctk.CTkFrame(
            parent,
            fg_color=BG_CARD,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=CARD_CORNER_RADIUS,
        )
        self._setting_card_frames.append(outer)
        outer.grid(row=row, column=column, sticky="nsew", padx=padx, pady=(0, 14))
        inner = ctk.CTkFrame(outer, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=14, pady=14)
        inner.grid_columnconfigure(0, weight=1)
        return inner

    def _subheading(self, parent: Any, row: int, text: str, *, columnspan: int = 1) -> None:
        self.app._label(
            parent,
            text=text,
            color=FG_MUTED,
            bg=BG_CARD,
            size=SETTINGS_HEAD,
            weight="bold",
        ).grid(row=row, column=0, columnspan=columnspan, sticky="w", pady=(0, 10))

    def _labeled_number(
        self,
        parent: Any,
        row: int,
        column: int,
        label: str,
        variable: tk.Variable,
        from_: float,
        to: float,
        increment: float,
        unit: str = "",
        *,
        integer: bool = False,
        width: int = 64,
        padx: tuple[int, int] = (0, 12),
        tooltip: str = "",
        columnspan: int = 1,
    ) -> None:
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=row, column=column, columnspan=columnspan, sticky="w", padx=padx, pady=(2, 4))
        self.app._label(cell, text=label, color=FG_MUTED, bg=BG_CARD, size=SETTINGS_TEXT).pack(side="left")
        number_input = CTkNumberInput(
            cell,
            variable=variable,
            from_=from_,
            to=to,
            increment=increment,
            integer=integer,
            width=width,
            entry_font=self.app._font(SETTINGS_TEXT),
            button_font=self.app._font(SETTINGS_TEXT, "bold"),
            app=self.app,
        )
        number_input.pack(side="left", padx=(7, 4))
        self._number_inputs.append(number_input)
        if unit:
            self.app._label(cell, text=unit, color=FG_MUTED, bg=BG_CARD, size=SETTINGS_TEXT).pack(side="left")
        if tooltip:
            self.app._add_tooltip(cell, tooltip)

    def _inline_number(
        self,
        parent: Any,
        variable: tk.Variable,
        from_: float,
        to: float,
        increment: float,
        unit: str = "",
        *,
        integer: bool = False,
        width: int = 64,
        tooltip: str = "",
    ) -> CTkNumberInput:
        wrapper = ctk.CTkFrame(parent, fg_color="transparent")
        wrapper.pack(side="left")
        number_input = CTkNumberInput(
            wrapper,
            variable=variable,
            from_=from_,
            to=to,
            increment=increment,
            integer=integer,
            width=width,
            entry_font=self.app._font(SETTINGS_TEXT),
            button_font=self.app._font(SETTINGS_TEXT, "bold"),
            app=self.app,
        )
        number_input.pack(side="left")
        self._number_inputs.append(number_input)
        if unit:
            self.app._label(wrapper, text=unit, color=FG_MUTED, bg=BG_CARD, size=SETTINGS_TEXT).pack(side="left", padx=(4, 0))
        if tooltip:
            self.app._add_tooltip(wrapper, tooltip)
        return number_input

    def _build_output_loudness_card(self, card: ctk.CTkFrame) -> None:
        self._subheading(card, 0, "OUTPUT LOUDNESS")

        loudness_row = ctk.CTkFrame(card, fg_color="transparent")
        loudness_row.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        loudness_row.grid_columnconfigure(0, weight=1)
        loudness_row.grid_columnconfigure(1, weight=0, minsize=188)

        target_cell = ctk.CTkFrame(loudness_row, fg_color="transparent")
        target_cell.grid(row=0, column=0, sticky="nw", padx=(0, 12))
        self.app._label(
            target_cell, text="Target loudest section", color=FG_MUTED, bg=BG_CARD, size=SETTINGS_TEXT
        ).pack(anchor="w", pady=(0, 8))
        target_inputs = ctk.CTkFrame(target_cell, fg_color="transparent")
        target_inputs.pack(anchor="w")
        self._inline_number(
            target_inputs,
            self.app.var_target_low,
            -20.0,
            0.0,
            0.1,
            tooltip=(
                "Lower edge of the acceptable loudness range for the loudest section. "
                "Tracks below this may be boosted so you do not need to ride trim during a set."
            ),
        )
        self.app._label(target_inputs, text="to", color=FG_MUTED, bg=BG_CARD, size=SETTINGS_TEXT).pack(side="left", padx=8)
        self._inline_number(
            target_inputs,
            self.app.var_target_high,
            -20.0,
            0.0,
            0.1,
            "LUFS",
            tooltip=(
                "Upper edge of the target loudness range. Tracks above this may be turned down. "
                "For loud EDM, keeping Low and High close gives a more consistent library."
            ),
        )

        self._labeled_number(
            loudness_row,
            0,
            1,
            "Peak ceiling",
            self.app.var_peak_ceiling,
            -12.0,
            0.0,
            0.1,
            "dBTP",
            padx=(0, 0),
            tooltip=(
                "True-peak safety ceiling for boosted or rendered files. -1.0 dBTP leaves practical headroom "
                "for club playback, limiters, and lossy encoding."
            ),
        )

        self.app._label(card, text="Processing mode", color=FG_MUTED, bg=BG_CARD, size=SETTINGS_TEXT).grid(
            row=2, column=0, sticky="w", pady=(0, 8)
        )
        mode_cell = ctk.CTkFrame(card, fg_color="transparent")
        mode_cell.grid(row=3, column=0, sticky="ew")
        mode_cell.grid_columnconfigure(0, weight=1)
        self.mode_menu = ctk.CTkOptionMenu(
            mode_cell,
            variable=self.app.var_normalization_mode,
            values=list(NORMALIZATION_MODE_CHOICES),
            fg_color=BG_FIELD,
            button_color=BG_CARD,
            button_hover_color=ICE_DIM,
            dropdown_fg_color=BG_FIELD,
            dropdown_hover_color=ICE_DIM,
            dropdown_text_color=FG_MAIN,
            text_color=FG_MAIN,
            font=self.app._font(TYPE_BODY),
            dropdown_font=self.app._font(TYPE_BODY),
            height=32,
        )
        self.mode_menu.grid(row=0, column=0, sticky="ew")
        self.app._add_tooltip(
            mode_cell,
            "Limiter-assisted can boost into a limiter plugin while respecting the true-peak ceiling. "
            "Clean gain only applies gain changes that stay under the ceiling without limiter help.",
        )

        self.app._label(card, text="Limiter engine", color=FG_MUTED, bg=BG_CARD, size=SETTINGS_TEXT).grid(
            row=4, column=0, sticky="w", pady=(12, 8)
        )
        limiter_engine_cell = ctk.CTkFrame(card, fg_color="transparent")
        limiter_engine_cell.grid(row=5, column=0, sticky="ew")
        limiter_engine_cell.grid_columnconfigure(0, weight=1)
        self.limiter_engine_menu = ctk.CTkOptionMenu(
            limiter_engine_cell,
            variable=self.app.var_limiter_engine,
            values=list(LIMITER_ENGINE_CHOICES),
            fg_color=BG_FIELD,
            button_color=BG_CARD,
            button_hover_color=ICE_DIM,
            dropdown_fg_color=BG_FIELD,
            dropdown_hover_color=ICE_DIM,
            dropdown_text_color=FG_MAIN,
            text_color=FG_MAIN,
            font=self.app._font(TYPE_BODY),
            dropdown_font=self.app._font(TYPE_BODY),
            height=32,
        )
        self.limiter_engine_menu.grid(row=0, column=0, sticky="ew")
        self.app._add_tooltip(
            limiter_engine_cell,
            "Which limiter plugin renders limiter-assisted rows.",
        )

    def _build_output_format_card(self, card: ctk.CTkFrame) -> None:
        self._subheading(card, 0, "OUTPUT FORMAT")

        self.output_format_menu = ctk.CTkOptionMenu(
            card,
            variable=self.app.var_output_format_mode,
            values=list(OUTPUT_FORMAT_MODE_CHOICES),
            command=self.app._on_output_format_mode_selected,
            fg_color=BG_FIELD,
            button_color=BG_CARD,
            button_hover_color=ICE_DIM,
            dropdown_fg_color=BG_FIELD,
            dropdown_hover_color=ICE_DIM,
            dropdown_text_color=FG_MAIN,
            text_color=FG_MAIN,
            font=self.app._font(TYPE_BODY),
            dropdown_font=self.app._font(TYPE_BODY),
            height=32,
        )
        self.output_format_menu.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.app._add_tooltip(card, output_format_mode_tooltip(), wraplength=320)

        self.lbl_output_format_hint = self.app._label(
            card,
            text="",
            color=FG_MUTED,
            bg=BG_CARD,
            size=SETTINGS_HINT,
            justify="left",
        )
        self.lbl_output_format_hint.grid(row=2, column=0, sticky="ew")
        self.lbl_output_format_hint.grid_remove()
        self._bind_card_wraplength(card, self.lbl_output_format_hint)

    def _build_analysis_card(self, card: ctk.CTkFrame) -> None:
        self._subheading(card, 0, "ANALYSIS")
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.grid(row=1, column=0, sticky="ew")
        grid.grid_columnconfigure((0, 1), weight=1)

        self._labeled_number(
            grid, 0, 0, "Window", self.app.var_window,
            MIN_LOUD_SECTION_WINDOW_SECONDS, MAX_LOUD_SECTION_WINDOW_SECONDS, 5.0, "sec",
            tooltip=(
                "Length of each segment used to find the loudest musical section. "
                "30 seconds is a good EDM default because it captures a drop or chorus instead of a tiny transient."
            ),
        )
        self._labeled_number(
            grid, 0, 1, "Hop", self.app.var_hop,
            MIN_LOUD_SECTION_HOP_SECONDS, MAX_LOUD_SECTION_HOP_SECONDS, 5.0, "sec",
            padx=(0, 0),
            tooltip=(
                "How far the analysis window moves each step. Lower values scan more precisely but take longer; "
                "higher values are faster but may miss the exact loudest drop."
            ),
        )
        self._labeled_number(
            grid, 1, 0, "Analysis workers", self.app.var_workers,
            MIN_ANALYSIS_WORKER_THREADS, MAX_ANALYSIS_WORKER_THREADS, 1.0, "",
            integer=True,
            padx=(0, 0),
            tooltip=(
                "Number of files analyzed at once. Higher can speed up scanning on strong CPUs, "
                "but can also make the computer feel busy. Limiter rendering stays single-threaded."
            ),
        )

    def _build_render_rules_card(self, card: ctk.CTkFrame) -> None:
        self._subheading(card, 0, "RENDER RULES")
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.grid(row=1, column=0, sticky="ew")
        grid.grid_columnconfigure((0, 1), weight=1)

        self.app._label(
            grid,
            text="Minimum gain change to render",
            color=FG_MUTED,
            bg=BG_CARD,
            size=SETTINGS_TEXT,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self._labeled_number(
            grid, 1, 0, "MP3", self.app.var_mp3_threshold, 0.0, 6.0, 0.05, "dB",
            tooltip=(
                "Minimum gain change before an MP3 is rendered. MP3 defaults higher because re-encoding is lossy."
            ),
        )
        self._labeled_number(
            grid, 1, 1, "Lossless", self.app.var_lossless_threshold, 0.0, 6.0, 0.05, "dB",
            padx=(0, 0),
            tooltip="Minimum gain change before FLAC, WAV, or AIFF files are rendered.",
        )

        self._labeled_number(
            grid, 2, 0, "Max limiter reduction", self.app.var_max_reduction, 0.0, 20.0, 0.1, "dB",
            tooltip=(
                "Maximum amount DropGain is allowed to turn down an overly loud track. "
                "This prevents extreme level changes from changing the intended energy of the master."
            ),
        )
        self._labeled_number(
            grid, 2, 1, "Max bass-aware trim", self.app.var_bass_max_reduction,
            MIN_BASS_MAX_BOOST_REDUCTION_DB, MAX_BASS_MAX_BOOST_REDUCTION_DB, 0.1, "dB",
            padx=(0, 0),
            tooltip=(
                "Maximum bass-aware gain trim on bass-heavy tracks. "
                "Reduces boosts, deepens cuts, or pulls down an otherwise in-target track slightly. "
                "Set to 0 to disable."
            ),
        )

        lbl_bass_trim_thresholds = self.app._label(
            grid,
            text="When to trim bass-heavy tracks",
            color=FG_MUTED,
            bg=BG_CARD,
            size=SETTINGS_TEXT,
        )
        lbl_bass_trim_thresholds.grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 6))
        self.app._add_tooltip(
            lbl_bass_trim_thresholds,
            (
                "DropGain measures how strong the low end is on each track's loudest section, "
                "compared to the mids (115-1000 Hz). See bass_strength_db and sub_strength_db in the results table. "
                "Bass-heavy tracks can feel louder than the LUFS meter suggests, so gain is pulled back slightly: "
                "less boost, a deeper cut, or a small pulldown even on a track that's already in the target LUFS "
                "band. These four values set when that pullback starts and when it reaches full strength. "
                "Max bass-aware trim sets how strong the pullback can get."
            ),
            wraplength=420,
        )

        self._labeled_number(
            grid, 4, 0, "Bass trim start", self.app.var_bass_penalty_start,
            MIN_BASS_PENALTY_THRESHOLD_DB, MAX_BASS_PENALTY_THRESHOLD_DB, 0.5, "dB",
            tooltip=(
                "How bass-heavy (45-115 Hz) a track must be before any bass-aware trim applies. "
                "Below this value: no change. Example at default +5 dB: bass strength +4 dB is left alone; "
                "+7 dB starts getting a small trim. Raise this if normal EDM bass keeps getting trimmed."
            ),
        )
        self._labeled_number(
            grid, 4, 1, "Bass trim full", self.app.var_bass_penalty_full,
            MIN_BASS_PENALTY_THRESHOLD_DB, MAX_BASS_PENALTY_THRESHOLD_DB, 0.5, "dB",
            padx=(0, 0),
            tooltip=(
                "How bass-heavy a track must be before bass-aware trim reaches Max bass-aware trim. "
                "Between Start and Full, trim grows smoothly. Example at defaults +5 / +17 dB: "
                "bass strength +11 dB gets about half the max trim. Lower Full to react sooner on "
                "moderately bass-heavy tracks."
            ),
        )
        self._labeled_number(
            grid, 5, 0, "Sub trim start", self.app.var_sub_penalty_start,
            MIN_BASS_PENALTY_THRESHOLD_DB, MAX_BASS_PENALTY_THRESHOLD_DB, 0.5, "dB",
            tooltip=(
                "Same as Bass trim start, but for sub-bass (20-45 Hz). Sub is usually weaker than mids, "
                "so this defaults higher than bass start. Below this value: sub contributes no trim."
            ),
        )
        self._labeled_number(
            grid, 5, 1, "Sub trim full", self.app.var_sub_penalty_full,
            MIN_BASS_PENALTY_THRESHOLD_DB, MAX_BASS_PENALTY_THRESHOLD_DB, 0.5, "dB",
            padx=(0, 0),
            tooltip=(
                "Same as Bass trim full, but for sub-bass. Bass and sub are checked separately; "
                "whichever asks for more trim wins."
            ),
        )

        self.chk_apply_render_gain_threshold = ctk.CTkCheckBox(
            grid,
            text="Apply gain thresholds when rendering",
            variable=self.app.var_apply_render_gain_threshold,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            border_color=BORDER_COLOR,
            text_color=FG_MAIN,
            checkmark_color=BUTTON_TEXT_DARK,
            font=self.app._font(SETTINGS_TEXT),
        )
        self.chk_apply_render_gain_threshold.grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.app._add_tooltip(
            self.chk_apply_render_gain_threshold,
            "Off by default for batch runs: render every track that passes safety checks. "
            "On: skip small gain-only changes using the MP3 and lossless thresholds above.",
        )

        self.chk_allow_risky_true_peak_boost = ctk.CTkCheckBox(
            grid,
            text="Allow boosting when true-peak measurement failed (risky)",
            variable=self.app.var_allow_risky_true_peak_boost,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            border_color=BORDER_COLOR,
            text_color=FG_MAIN,
            checkmark_color=BUTTON_TEXT_DARK,
            checkbox_width=18,
            checkbox_height=18,
            font=self.app._font(SETTINGS_TEXT),
        )
        self.chk_allow_risky_true_peak_boost.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.app._add_tooltip(
            self.chk_allow_risky_true_peak_boost,
            "Only use this when you trust the source file and accept the clipping risk.",
        )

    def _build_reporting_card(self, card: ctk.CTkFrame) -> None:
        self._subheading(card, 0, "REPORTING")

        self.lbl_reporting_hint = self.app._label(
            card,
            text=(
                f"By default, safe copies are saved beside originals with the {PROCESSED_SUFFIX} suffix. "
                "Use the output folder on the Process page to redirect copies elsewhere."
            ),
            color=FG_MUTED,
            bg=BG_CARD,
            size=SETTINGS_TEXT,
            justify="left",
        )
        self.lbl_reporting_hint.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self._bind_card_wraplength(card, self.lbl_reporting_hint)

        csv_cell = ctk.CTkFrame(card, fg_color="transparent")
        csv_cell.grid(row=2, column=0, sticky="ew")
        csv_cell.grid_columnconfigure(0, weight=1)

        self.chk_write_csv = ctk.CTkCheckBox(
            csv_cell,
            text="Write CSV report",
            variable=self.app.var_write_csv,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            border_color=BORDER_COLOR,
            text_color=FG_MAIN,
            checkmark_color=BUTTON_TEXT_DARK,
            checkbox_width=18,
            checkbox_height=18,
            font=self.app._font(SETTINGS_TEXT),
        )
        self.chk_write_csv.grid(row=0, column=0, sticky="w")
        self.app._add_tooltip(
            self.chk_write_csv,
            "Write a CSV report with the analysis and render decisions. Helpful for auditing a library, "
            "checking warnings, or reviewing which tracks DropGain would change.",
        )

        self.csv_display = ctk.CTkEntry(
            csv_cell,
            textvariable=self.app.var_csv,
            state="disabled",
            fg_color=BG_FIELD,
            border_color=BORDER_COLOR,
            text_color=FG_MAIN,
            font=self.app._font(TYPE_BODY),
        )
        self.csv_display.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.app._label(
            csv_cell,
            text=f"Default path: {default_csv_path(self.app.var_folder.get().strip())}",
            color=FG_MUTED,
            bg=BG_CARD,
            size=SETTINGS_HINT,
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))

    def _bind_card_wraplength(self, card: Any, label: ctk.CTkLabel) -> None:
        def on_configure(event: tk.Event) -> None:
            if event.widget is not card:
                return
            wrap = max(int(event.width) - 4, 0)
            if wrap <= 0:
                return
            last_wrap = getattr(label, "_dropgain_wraplength", None)
            if last_wrap is not None and abs(last_wrap - wrap) < 8:
                return
            label._dropgain_wraplength = wrap  # type: ignore[attr-defined]
            label.configure(wraplength=wrap)

        card.bind("<Configure>", on_configure, add="+")

    def _on_system_check_status_configure(self, event: tk.Event) -> None:
        if event.widget is not self.lbl_system_check_status:
            return
        wrap = max(int(event.width) - 4, 0)
        if wrap <= 0:
            return
        last_wrap = getattr(self.lbl_system_check_status, "_dropgain_wraplength", None)
        if last_wrap is not None and abs(last_wrap - wrap) < 8:
            return
        self.lbl_system_check_status._dropgain_wraplength = wrap  # type: ignore[attr-defined]
        self.lbl_system_check_status.configure(wraplength=wrap)

    def show_system_check_results(self, results: list[tuple[str, bool, str]]) -> None:
        lines: list[str] = []
        all_ok = True
        for name, ok, detail in results:
            if not ok:
                all_ok = False
            symbol = "✓" if ok else "✗"
            lines.append(f"{symbol} {name}: {detail}" if detail else f"{symbol} {name}")

        if all_ok:
            heading = "All checks passed"
            color = SUCCESS_FG
        else:
            heading = "Some checks failed"
            color = ERROR_FG

        self.lbl_system_check_status.configure(
            text=f"{heading}\n" + "\n".join(lines),
            text_color=color,
        )
        self.lbl_system_check_status.grid(row=1, column=0, sticky="ew", pady=(6, 8))
        self.lbl_system_check_status.update_idletasks()
        wrap = max(self.lbl_system_check_status.winfo_width() - 4, 0)
        if wrap > 0:
            self.lbl_system_check_status.configure(wraplength=wrap)
