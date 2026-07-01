"""DropGain Library Tuning page."""

from __future__ import annotations

import copy
import tkinter as tk
from typing import TYPE_CHECKING, Any

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from gui_theme import (
    ACCENT,
    BG_FIELD,
    BG_MAIN,
    BG_PANEL,
    BORDER_COLOR,
    FG_MAIN,
    FG_MUTED,
    ICE,
    ICE_FILL,
    ICE_SOFT,
    LOG_BG,
    METRIC_BG,
    METRIC_CHIP_CORNER_RADIUS,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_LABEL,
    TYPE_MICRO,
    WARN_FG,
)
from gui_utils import ui_scale_for
from analysis import (
    NORMALIZATION_MODE_LIMITER_ASSISTED,
    PEAK_CONTROL_SEVERITY_HEAVY,
    parse_float_or_default,
)
from jobs import DropGainSettings, recompute_row_decision
from optimizer import (
    LibraryProfile,
    RecommendedSettings,
    build_library_profile,
    format_peak_control_diagnostics,
    limiter_bucket_counts,
    lufs_distribution_curve,
    peak_control_stats,
    recommend_settings,
)

if TYPE_CHECKING:
    from gui_tk import App

CHART_MIN_HEIGHT = 160
CHART_MIN_WIDTH = 520
CHART_FONT_TITLE = 13
CHART_FONT_SUBTITLE = 11
CHART_FONT_AXIS = 11
CHART_FONT_TICK = 10
CHART_FONT_BAR_LABEL = 14
CHART_BAR_VALUE_HEIGHT_RATIO = 0.72
CHART_TARGET_BAND_ALPHA = 52
CHART_LEGEND_FONT = 12


class LibraryTuningPage(ctk.CTkFrame):
    """Library analysis, recommendations, preview, and charts."""

    def __init__(self, app: App, master: Any) -> None:
        super().__init__(master, fg_color=BG_MAIN, corner_radius=0)
        self._app = app
        self._profile: LibraryProfile | None = None
        self._recommendation: RecommendedSettings | None = None
        self._preview_rows: list[dict[str, object]] = []
        self._preview_after_id: str | None = None
        self._chart_after_id: str | None = None
        self._canvas_photos: dict[tk.Canvas, ImageTk.PhotoImage] = {}
        self._active_evidence_tab = "distribution"
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._build(app)

    def _build(self, app: App) -> None:
        command = ctk.CTkFrame(self, fg_color=BG_MAIN, corner_radius=0)
        command.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 6))
        command.grid_columnconfigure(1, weight=1)

        app._label(command, text="Library folder", color=FG_MUTED, bg=BG_MAIN, size=TYPE_BODY).grid(
            row=0, column=0, sticky="w", padx=(0, 10)
        )
        app.entry_lt_folder = ctk.CTkEntry(
            command,
            textvariable=app.var_folder,
            fg_color=BG_FIELD,
            border_color=BORDER_COLOR,
            text_color=FG_MAIN,
            font=app._font(TYPE_BODY),
            height=34,
        )
        app.entry_lt_folder.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        app.btn_lt_browse = app._button(command, text="Browse", command=app._pick_folder)
        app.btn_lt_browse.grid(row=0, column=2, sticky="e")

        settings_line = ctk.CTkFrame(command, fg_color="transparent")
        settings_line.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        app._label(
            settings_line,
            text="ACTIVE SETTINGS",
            color=FG_MUTED,
            bg=BG_MAIN,
            size=TYPE_MICRO,
            weight="bold",
        ).pack(side="left", padx=(0, 12))
        app.lbl_lt_settings_summary = app._label(
            settings_line,
            textvariable=app.var_process_settings_summary,
            color=FG_MUTED,
            bg=BG_MAIN,
            size=TYPE_MICRO,
            wraplength=480,
            justify="left",
            anchor="w",
        )
        app.lbl_lt_settings_summary.pack(side="left", fill="x", expand=True)

        def _on_lt_settings_configure(event: tk.Event) -> None:
            if event.widget is not command:
                return
            app.lbl_lt_settings_summary.configure(
                wraplength=max(int(event.width) - 108, 160)
            )

        command.bind("<Configure>", _on_lt_settings_configure, add="+")

        actions = ctk.CTkFrame(command, fg_color="transparent")
        actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        app.btn_lt_analyze = app._button(
            actions,
            text="Analyze Library",
            command=app._start_analyze_only,
            accent=True,
        )
        app.btn_lt_analyze.pack(side="left", padx=(0, 8))
        app.btn_lt_apply = app._button(
            actions,
            text="Apply Target",
            command=self._apply_recommended,
            state="disabled",
        )
        app.btn_lt_apply.pack(side="left", padx=(0, 8))
        app.btn_lt_apply_budget = app._button(
            actions,
            text="Apply Budget",
            command=self._apply_preview_budget,
            state="disabled",
        )
        app.btn_lt_apply_budget.pack(side="left")

        progress_frame = ctk.CTkFrame(command, fg_color=BG_MAIN, corner_radius=0)
        progress_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        progress_frame.grid_columnconfigure(0, weight=1)
        app.progress_lt = ctk.CTkProgressBar(
            progress_frame,
            fg_color=BG_FIELD,
            progress_color=ICE_FILL,
            border_color=BORDER_COLOR,
            border_width=1,
            height=8,
        )
        app.progress_lt.grid(row=0, column=0, sticky="ew")
        app.progress_lt.set(0)
        app._label(
            progress_frame,
            textvariable=app.var_status,
            color=FG_MUTED,
            bg=BG_MAIN,
            size=TYPE_CAPTION,
        ).grid(row=1, column=0, sticky="w", pady=(5, 0))

        self._metric_vars: dict[str, tk.StringVar] = {}
        metrics_section = ctk.CTkFrame(self, fg_color="transparent")
        metrics_section.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 8))
        app._label(
            metrics_section,
            text="At analyzed settings",
            color=FG_MUTED,
            bg=BG_MAIN,
            size=TYPE_MICRO,
        ).pack(anchor="w", pady=(0, 4))
        metrics_row = ctk.CTkFrame(metrics_section, fg_color="transparent")
        metrics_row.pack(fill="x", expand=True)
        metric_specs = (
            ("tracks", "Tracks scanned"),
            ("format_mix", "MP3 / lossless"),
            ("median_lufs", "Median drop LUFS"),
            ("median_gain", "Median gain"),
            ("range_lufs", "Middle 80% range"),
            ("would_render", "Would render"),
            ("heavy_limiter", "Heavy limiting risk"),
        )
        for index, (key, label) in enumerate(metric_specs):
            var = tk.StringVar(value="—")
            self._metric_vars[key] = var
            card = ctk.CTkFrame(
                metrics_row,
                fg_color=METRIC_BG,
                border_color=BORDER_COLOR,
                border_width=1,
                corner_radius=METRIC_CHIP_CORNER_RADIUS,
            )
            card.grid(row=0, column=index, padx=(0 if index == 0 else 6, 0), sticky="nsew")
            metrics_row.grid_columnconfigure(index, weight=1)
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=10, pady=8)
            app._label(inner, text=label, color=FG_MUTED, bg=METRIC_BG, size=TYPE_CAPTION).pack(anchor="w")
            app._label(
                inner,
                textvariable=var,
                color=FG_MAIN,
                bg=METRIC_BG,
                size=TYPE_BODY,
                weight="bold",
            ).pack(anchor="w", pady=(2, 0))

        rec_card = app._card(self, BG_PANEL, (12, 10))
        rec_card.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 8))
        rec_inner = app._inner(rec_card)
        rec_inner.grid_columnconfigure(1, weight=1)
        app._label(rec_inner, text="Recommended settings", bg=BG_PANEL, size=TYPE_BODY, weight="bold").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        self.var_recommendation = tk.StringVar(value="Analyze a library to see recommendations.")
        app._label(
            rec_inner,
            textvariable=self.var_recommendation,
            bg=BG_PANEL,
            size=TYPE_LABEL,
            justify="left",
            anchor="nw",
        ).grid(row=1, column=0, columnspan=2, sticky="ew")

        self.var_recommendation_diff = tk.StringVar(value="")
        app._label(
            rec_inner,
            textvariable=self.var_recommendation_diff,
            color=FG_MUTED,
            bg=BG_PANEL,
            size=TYPE_CAPTION,
            justify="left",
            anchor="nw",
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        app._label(
            rec_inner,
            text="What-if preview (not applied):",
            color=FG_MUTED,
            bg=BG_PANEL,
            size=TYPE_CAPTION,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 4))

        preview_target_row = ctk.CTkFrame(rec_inner, fg_color="transparent")
        preview_target_row.grid(row=4, column=0, columnspan=2, sticky="ew")
        app._label(
            preview_target_row,
            text="Target:",
            color=FG_MUTED,
            bg=BG_PANEL,
            size=TYPE_CAPTION,
        ).pack(side="left", padx=(0, 8))
        self.var_preview_target = tk.DoubleVar(value=-7.5)
        self.preview_slider = ctk.CTkSlider(
            preview_target_row,
            from_=-14.0,
            to=-4.0,
            number_of_steps=100,
            variable=self.var_preview_target,
            command=self._on_preview_slider,
            width=220,
        )
        self.preview_slider.pack(side="left", padx=(0, 8))
        self.lbl_preview_target = app._label(
            preview_target_row,
            text="-7.5 LUFS (1 dB band)",
            bg=BG_PANEL,
            size=TYPE_LABEL,
            weight="bold",
        )
        self.lbl_preview_target.pack(side="left")

        self._limiter_preview_row = ctk.CTkFrame(rec_inner, fg_color="transparent")
        self._limiter_preview_row.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        app._label(
            self._limiter_preview_row,
            text="Limiter budget:",
            color=FG_MUTED,
            bg=BG_PANEL,
            size=TYPE_CAPTION,
        ).pack(side="left", padx=(0, 8))
        self.var_preview_budget = tk.DoubleVar(value=3.0)
        self.preview_budget_slider = ctk.CTkSlider(
            self._limiter_preview_row,
            from_=0.0,
            to=20.0,
            number_of_steps=200,
            variable=self.var_preview_budget,
            command=self._on_preview_slider,
            width=220,
        )
        self.preview_budget_slider.pack(side="left", padx=(0, 8))
        self.lbl_preview_budget = app._label(
            self._limiter_preview_row,
            text="3.0 dB",
            bg=BG_PANEL,
            size=TYPE_LABEL,
            weight="bold",
        )
        self.lbl_preview_budget.pack(side="left")

        self.var_preview_diagnostics = tk.StringVar(value="")
        app._label(
            rec_inner,
            textvariable=self.var_preview_diagnostics,
            color=FG_MUTED,
            bg=BG_PANEL,
            size=TYPE_CAPTION,
            justify="left",
            anchor="nw",
            wraplength=720,
        ).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.var_preview_counts = tk.StringVar(value="")
        app._label(
            rec_inner,
            textvariable=self.var_preview_counts,
            color=FG_MUTED,
            bg=BG_PANEL,
            size=TYPE_CAPTION,
            justify="left",
            anchor="nw",
        ).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        evidence_card = app._card(self, BG_PANEL, (10, 10))
        evidence_card.grid(row=3, column=0, sticky="nsew", padx=18, pady=(0, 14))
        evidence = app._inner(evidence_card)
        evidence.grid_columnconfigure(0, weight=1)
        evidence.grid_rowconfigure(1, weight=1)

        tab_header = ctk.CTkFrame(evidence, fg_color="transparent")
        tab_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._evidence_tab_buttons: dict[str, ctk.CTkButton] = {}
        for index, (tab_id, label) in enumerate(
            (
                ("distribution", "Distribution"),
                ("render_impact", "Render Impact"),
            )
        ):
            btn = ctk.CTkButton(
                tab_header,
                text=label,
                height=26,
                command=lambda tid=tab_id: self._show_evidence_tab(tid),
                **app._tab_button_style(active=tab_id == "distribution"),
            )
            btn.grid(row=0, column=index, padx=(0 if index == 0 else 4, 0))
            self._evidence_tab_buttons[tab_id] = btn

        self.evidence_content = ctk.CTkFrame(evidence, fg_color=LOG_BG, corner_radius=6)
        self.evidence_content.grid(row=1, column=0, sticky="nsew")
        self.evidence_content.grid_columnconfigure(0, weight=1)
        self.evidence_content.grid_rowconfigure(0, weight=1)

        self.panel_distribution = ctk.CTkFrame(self.evidence_content, fg_color=LOG_BG)
        self.panel_distribution.grid(row=0, column=0, sticky="nsew")
        self.panel_distribution.grid_columnconfigure(0, weight=1)
        self.panel_distribution.grid_rowconfigure(0, weight=1)
        self.hist_canvas = tk.Canvas(
            self.panel_distribution,
            bg=LOG_BG,
            highlightthickness=0,
            bd=0,
        )
        self.hist_canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.hist_canvas.bind("<Configure>", self._on_chart_canvas_configure, add="+")

        self.panel_render_impact = ctk.CTkFrame(self.evidence_content, fg_color=LOG_BG)
        self.panel_render_impact.grid_columnconfigure(0, weight=1)
        self.panel_render_impact.grid_rowconfigure(0, weight=1)
        self.impact_canvas = tk.Canvas(
            self.panel_render_impact,
            bg=LOG_BG,
            highlightthickness=0,
            bd=0,
        )
        self.impact_canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.impact_canvas.bind("<Configure>", self._on_chart_canvas_configure, add="+")

        self._show_evidence_tab("distribution")

    def _show_evidence_tab(self, tab_id: str) -> None:
        if tab_id == self._active_evidence_tab:
            return

        for key, btn in self._evidence_tab_buttons.items():
            btn.configure(**self._app._tab_button_style(active=key == tab_id))

        def swap() -> None:
            self._active_evidence_tab = tab_id
            for panel in (
                self.panel_distribution,
                self.panel_render_impact,
            ):
                panel.grid_remove()
            mapping = {
                "distribution": self.panel_distribution,
                "render_impact": self.panel_render_impact,
            }
            mapping[tab_id].grid(row=0, column=0, sticky="nsew")

        self._app._fade_panel_swap(
            self.evidence_content,
            swap,
            before_reveal=self._settle_charts_for_reveal,
            settle_ms=90,
        )

    def refresh_from_app(self) -> None:
        self._refresh_profile()

    def settle_layout_for_reveal(self) -> None:
        """Settle Library Tuning metrics and charts while the page is covered."""
        self.refresh_from_app()
        self.update_idletasks()
        self._settle_charts_for_reveal()
        self.update_idletasks()

    def _settle_charts_for_reveal(self) -> None:
        if self._chart_after_id is not None:
            try:
                self.after_cancel(self._chart_after_id)
            except Exception:
                pass
            self._chart_after_id = None
        self.update_idletasks()
        self._redraw_charts()

    def _apply_recommended(self) -> None:
        if self._recommendation is None:
            return
        app = self._app
        app.var_target_low.set(self._recommendation.target_low_lufs)
        app.var_target_high.set(self._recommendation.target_high_lufs)
        app._refresh_settings_summary()

    def _apply_preview_budget(self) -> None:
        app = self._app
        app.var_max_reduction.set(round(float(self.var_preview_budget.get()), 1))
        self._update_apply_budget_state()

    def _is_limiter_assisted(self) -> bool:
        settings = self._app._current_dropgain_settings()
        if settings is None:
            return True
        return settings.normalization_mode == NORMALIZATION_MODE_LIMITER_ASSISTED

    def _update_limiter_preview_visibility(self) -> None:
        if self._is_limiter_assisted():
            self._limiter_preview_row.grid()
        else:
            self._limiter_preview_row.grid_remove()
            self.var_preview_diagnostics.set("")

    def _update_apply_budget_state(self) -> None:
        app = self._app
        if not self._app._analyzed_rows or not self._is_limiter_assisted():
            app._apply_action_button_state(app.btn_lt_apply_budget, "disabled")
            return
        try:
            preview_budget = float(self.var_preview_budget.get())
            current_budget = float(app.var_max_reduction.get())
        except (tk.TclError, TypeError, ValueError):
            app._apply_action_button_state(app.btn_lt_apply_budget, "disabled")
            return
        if abs(preview_budget - current_budget) < 0.05:
            app._apply_action_button_state(app.btn_lt_apply_budget, "disabled")
        else:
            app._apply_action_button_state(app.btn_lt_apply_budget, "normal")

    @staticmethod
    def _format_preview_budget_label(budget_db: float) -> str:
        return f"{budget_db:.1f} dB"

    @staticmethod
    def _format_preview_target_label(center: float) -> str:
        return f"{center:.1f} LUFS (1 dB band)"

    def _on_preview_slider(self, _value: object = None) -> None:
        if self._preview_after_id is not None:
            self.after_cancel(self._preview_after_id)
        self._preview_after_id = self.after(120, self._apply_preview_target)

    def _apply_preview_target(self) -> None:
        self._preview_after_id = None
        rows = self._app._analyzed_rows
        if not rows:
            self.var_preview_counts.set("")
            self.var_preview_diagnostics.set("")
            self._update_apply_budget_state()
            return

        center = float(self.var_preview_target.get())
        self.lbl_preview_target.configure(text=self._format_preview_target_label(center))
        target_low = round(center - 0.5, 1)
        target_high = round(center + 0.5, 1)

        settings = self._app._current_dropgain_settings()
        if settings is None:
            return

        preview_budget = max(0.0, float(self.var_preview_budget.get()))
        self.lbl_preview_budget.configure(text=self._format_preview_budget_label(preview_budget))

        preview_settings = DropGainSettings(
            folder=settings.folder,
            csv_path=settings.csv_path,
            target_low_lufs=target_low,
            target_high_lufs=target_high,
            window_seconds=settings.window_seconds,
            hop_seconds=settings.hop_seconds,
            max_reduction_db=preview_budget,
            peak_ceiling_dbfs=settings.peak_ceiling_dbfs,
            normalization_mode=settings.normalization_mode,
            analysis_workers=settings.analysis_workers,
            render_workers=settings.render_workers,
            analyze_only=True,
            write_csv=settings.write_csv,
            mp3_threshold=settings.mp3_threshold,
            lossless_threshold=settings.lossless_threshold,
            output_format_mode=settings.output_format_mode,
            allow_risky_true_peak_boost=settings.allow_risky_true_peak_boost,
            apply_render_gain_threshold=settings.apply_render_gain_threshold,
        )

        self._preview_rows = []
        would_render = 0
        heavy = 0
        for source in rows:
            row = copy.deepcopy(source)
            recompute_row_decision(
                preview_settings,
                row,  # type: ignore[arg-type]
                apply_gain_threshold=preview_settings.apply_render_gain_threshold,
            )
            self._preview_rows.append(row)
            if row.get("processing_status") == "analyzed_would_process":
                would_render += 1
            peak_control = parse_float_or_default(row.get("estimated_peak_control_db", ""), 0.0)
            uses_limiter = "Pro-L" in str(row.get("processing_engine", ""))
            if (
                str(row.get("peak_control_severity", "")) == PEAK_CONTROL_SEVERITY_HEAVY
                and peak_control > 0.01
                and uses_limiter
            ):
                heavy += 1

        if self._is_limiter_assisted():
            stats = peak_control_stats(self._preview_rows)  # type: ignore[arg-type]
            self.var_preview_diagnostics.set(
                format_peak_control_diagnostics(stats, preview_budget)
            )
            self.var_preview_counts.set(
                f"At {center:.1f} LUFS with {preview_budget:.1f} dB budget: "
                f"{would_render} would render, {heavy} heavy limiter risk"
            )
        else:
            self.var_preview_diagnostics.set("")
            self.var_preview_counts.set(
                f"At {center:.1f} LUFS: {would_render} would render"
            )

        self._update_apply_budget_state()
        self._draw_render_impact(self._preview_rows)
        self._draw_distribution(self._app._analyzed_rows)

    def _refresh_profile(self) -> None:
        rows = self._app._analyzed_rows
        if not rows:
            self._profile = None
            self._recommendation = None
            self._preview_rows = []
            for var in self._metric_vars.values():
                var.set("—")
            self.var_recommendation.set("Analyze a library to see recommendations.")
            self.var_recommendation_diff.set("")
            self.var_preview_counts.set("")
            self.var_preview_diagnostics.set("")
            self._app._apply_action_button_state(self._app.btn_lt_apply, "disabled")
            self._app._apply_action_button_state(self._app.btn_lt_apply_budget, "disabled")
            self._update_limiter_preview_visibility()
            return

        self._profile = build_library_profile(rows)  # type: ignore[arg-type]
        profile = self._profile
        self._metric_vars["tracks"].set(str(profile.track_count))
        self._metric_vars["format_mix"].set(f"{profile.mp3_count} / {profile.lossless_count}")
        self._metric_vars["median_lufs"].set(
            f"{profile.median_loudest_lufs:.1f} LUFS" if profile.median_loudest_lufs is not None else "—"
        )
        self._metric_vars["median_gain"].set(
            f"{profile.median_suggested_gain_db:+.2f} dB"
            if profile.median_suggested_gain_db is not None
            else "—"
        )
        if profile.p10_loudest_lufs is not None and profile.p90_loudest_lufs is not None:
            self._metric_vars["range_lufs"].set(
                f"{profile.p10_loudest_lufs:.1f} to {profile.p90_loudest_lufs:.1f} LUFS"
            )
        else:
            self._metric_vars["range_lufs"].set("—")
        self._metric_vars["would_render"].set(str(profile.would_render_count))
        self._metric_vars["heavy_limiter"].set(str(profile.heavy_limiter_count))

        settings = self._app._current_dropgain_settings()
        if settings is not None:
            self._recommendation = recommend_settings(rows, settings)  # type: ignore[arg-type]
            rec = self._recommendation
            self.var_preview_target.set((rec.target_low_lufs + rec.target_high_lufs) / 2.0)
            self.lbl_preview_target.configure(
                text=self._format_preview_target_label(
                    (rec.target_low_lufs + rec.target_high_lufs) / 2.0
                )
            )
            preview_budget = max(0.0, float(settings.max_reduction_db))
            self.var_preview_budget.set(preview_budget)
            self.lbl_preview_budget.configure(text=self._format_preview_budget_label(preview_budget))
            self.var_recommendation.set(self._format_recommendation(rec))
            self.var_recommendation_diff.set(self._format_diff(rec, settings))
            self._app._apply_action_button_state(self._app.btn_lt_apply, "normal")
        else:
            self._recommendation = None
            self.var_recommendation.set("Settings unavailable.")
            self._app._apply_action_button_state(self._app.btn_lt_apply, "disabled")

        self._update_limiter_preview_visibility()
        self._draw_distribution(rows)
        self._draw_render_impact(rows)
        self._apply_preview_target()

    @staticmethod
    def _format_recommendation(rec: RecommendedSettings) -> str:
        lines = [
            f"Target loudest section:  {rec.target_low_lufs:.1f} to {rec.target_high_lufs:.1f} LUFS",
            f"Confidence: {rec.confidence}",
        ]
        if rec.notes:
            lines.append(f"Reason: {rec.notes[0]}")
        return "\n".join(lines)

    @staticmethod
    def _format_diff(rec: RecommendedSettings, current: DropGainSettings) -> str:
        diffs: list[str] = []
        if abs(rec.target_low_lufs - current.target_low_lufs) > 0.05:
            diffs.append(
                f"Target low: current {current.target_low_lufs:.1f} → recommended {rec.target_low_lufs:.1f} LUFS"
            )
        else:
            diffs.append("Target low: same")
        if abs(rec.target_high_lufs - current.target_high_lufs) > 0.05:
            diffs.append(
                f"Target high: current {current.target_high_lufs:.1f} → recommended {rec.target_high_lufs:.1f} LUFS"
            )
        else:
            diffs.append("Target high: same")
        return "Recommended differs from current:\n" + "\n".join(diffs)

    def _on_chart_canvas_configure(self, event: tk.Event) -> None:
        if event.width < 20 or event.height < 80:
            return
        if self._chart_after_id is not None:
            self.after_cancel(self._chart_after_id)
        self._chart_after_id = self.after(120, self._redraw_charts)

    def _redraw_charts(self) -> None:
        self._chart_after_id = None
        rows = self._app._analyzed_rows
        if not rows:
            return
        if self._active_evidence_tab == "distribution":
            self._draw_distribution(rows)
        elif self._active_evidence_tab == "render_impact":
            preview = self._preview_rows or rows
            self._draw_render_impact(preview)

    @staticmethod
    def _hex_rgba(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
        value = hex_color.lstrip("#")
        return (
            int(value[0:2], 16),
            int(value[2:4], 16),
            int(value[4:6], 16),
            alpha,
        )

    def _chart_ui_scale(self, canvas: tk.Canvas) -> float:
        return ui_scale_for(canvas)

    def _chart_canvas_size(self, canvas: tk.Canvas) -> tuple[int, int, float]:
        canvas.update_idletasks()
        scale = self._chart_ui_scale(canvas)
        width = max(int(CHART_MIN_WIDTH * scale), canvas.winfo_width() or 0)
        height = max(int(CHART_MIN_HEIGHT * scale), canvas.winfo_height() or 0)
        return width, height, scale

    def _chart_font(self, canvas: tk.Canvas, base: int, *, bold: bool = False):
        scale = self._chart_ui_scale(canvas)
        return self._app._pil_font(max(base, int(round(base * scale))), bold=bold)

    @staticmethod
    def _pil_text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    @staticmethod
    def _pil_text_top_y(
        draw: ImageDraw.ImageDraw,
        text: str,
        font,
        box_top: float,
        box_bottom: float,
    ) -> float:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_h = bbox[3] - bbox[1]
        box_mid = (box_top + box_bottom) / 2.0
        return box_mid - text_h / 2.0 - bbox[1]

    def _bar_count_font(
        self,
        draw: ImageDraw.ImageDraw,
        value_text: str,
        *,
        bar_height: float,
        bar_width: float,
        value_pad_x: int,
    ):
        target_size = max(10, int(bar_height * CHART_BAR_VALUE_HEIGHT_RATIO))
        for size in range(target_size, 9, -1):
            font = self._app._pil_font(size, bold=True)
            text_w = self._pil_text_width(draw, value_text, font)
            if bar_width >= text_w + value_pad_x * 2:
                return font
        return self._app._pil_font(10, bold=True)

    @staticmethod
    def _lufs_to_x(
        lufs: float,
        plot_left: float,
        plot_width: float,
        lo: float,
        hi: float,
    ) -> float:
        if hi <= lo:
            return plot_left + plot_width / 2.0
        ratio = (lufs - lo) / (hi - lo)
        return plot_left + max(0.0, min(1.0, ratio)) * plot_width

    @staticmethod
    def _interp_curve_y(lufs_values: list[float], density: list[float], lufs: float) -> float:
        if not lufs_values or not density:
            return 0.0
        if lufs <= lufs_values[0]:
            return density[0]
        if lufs >= lufs_values[-1]:
            return density[-1]
        for index in range(len(lufs_values) - 1):
            left = lufs_values[index]
            right = lufs_values[index + 1]
            if left <= lufs <= right:
                span = right - left
                if span <= 0:
                    return density[index]
                t = (lufs - left) / span
                return density[index] + (density[index + 1] - density[index]) * t
        return 0.0

    def _draw_distribution_legend(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        plot_left: float,
        plot_top: float,
        plot_width: float,
        plot_bottom: float,
        scale: float,
        font,
        show_target_band: bool,
        show_median: bool,
        show_preview: bool,
    ) -> None:
        if not any((show_target_band, show_median, show_preview)):
            return

        swatch_w = int(14 * scale)
        swatch_h = int(8 * scale)
        gap = int(5 * scale)
        item_gap = int(12 * scale)
        pad_x = int(6 * scale)
        pad_y = int(4 * scale)
        icon_text_gap = int(4 * scale)
        line_w = max(2, int(round(2 * scale)))

        items: list[tuple[str, str]] = []
        if show_target_band:
            items.append(("band", "Target band"))
        if show_median:
            items.append(("median", "Median"))
        if show_preview:
            items.append(("preview", "What-if"))

        item_widths: list[float] = []
        max_text_h = 0.0
        for kind, label in items:
            bbox = draw.textbbox((0, 0), label, font=font)
            text_w = bbox[2] - bbox[0]
            max_text_h = max(max_text_h, bbox[3] - bbox[1])
            icon_w = swatch_w if kind != "preview" else line_w
            item_widths.append(icon_w + icon_text_gap + text_w)

        content_h = max(swatch_h, max_text_h)
        total_w = sum(item_widths) + item_gap * max(0, len(items) - 1)
        legend_right = plot_left + plot_width - int(8 * scale)
        legend_left = legend_right - total_w - pad_x * 2
        legend_top = plot_top + int(6 * scale)
        legend_bottom = legend_top + content_h + pad_y * 2

        draw.rounded_rectangle(
            [
                legend_left,
                legend_top,
                legend_right,
                legend_bottom,
            ],
            radius=int(4 * scale),
            fill=self._hex_rgba(BG_FIELD, 220),
            outline=BORDER_COLOR,
            width=1,
        )

        x = legend_left + pad_x
        cy = legend_top + pad_y + content_h / 2.0
        for (kind, label), item_w in zip(items, item_widths):
            if kind == "band":
                draw.rectangle(
                    [x, cy - swatch_h / 2.0, x + swatch_w, cy + swatch_h / 2.0],
                    fill=self._hex_rgba(ACCENT, CHART_TARGET_BAND_ALPHA),
                    outline=self._hex_rgba(ACCENT, 140),
                    width=1,
                )
            elif kind == "median":
                radius = max(3, int(round(3 * scale)))
                draw.ellipse(
                    [x + swatch_w / 2.0 - radius, cy - radius, x + swatch_w / 2.0 + radius, cy + radius],
                    fill=ICE,
                    outline=FG_MAIN,
                    width=1,
                )
            else:
                draw.line(
                    [(x, cy - swatch_h / 2.0), (x, cy + swatch_h / 2.0)],
                    fill=WARN_FG,
                    width=line_w,
                )
            text_x = x + (swatch_w if kind != "preview" else line_w) + icon_text_gap
            text_bbox = draw.textbbox((0, 0), label, font=font)
            text_h = text_bbox[3] - text_bbox[1]
            draw.text((text_x, cy - text_h / 2.0), label, fill=FG_MUTED, font=font)
            x += item_w + item_gap

    def _draw_distribution_curve(
        self,
        canvas: tk.Canvas,
        lufs_values: list[float],
        density: list[float],
        lo: float,
        hi: float,
        *,
        title: str,
        subtitle: str,
        target_low: float | None = None,
        target_high: float | None = None,
        preview_lufs: float | None = None,
        median_lufs: float | None = None,
    ) -> None:
        canvas.delete("all")
        width, image_height, scale = self._chart_canvas_size(canvas)
        header = int(52 * scale)
        footer = int(30 * scale)
        title_font = self._chart_font(canvas, CHART_FONT_TITLE, bold=True)
        subtitle_font = self._chart_font(canvas, CHART_FONT_SUBTITLE)
        tick_font = self._chart_font(canvas, CHART_FONT_TICK)

        if not lufs_values or not density or hi <= lo:
            canvas.create_text(
                width // 2,
                image_height // 2,
                text="No data",
                fill=FG_MUTED,
                anchor="center",
                font=self._app._font(TYPE_CAPTION),
            )
            return

        image = Image.new("RGBA", (width, image_height), LOG_BG)
        draw = ImageDraw.Draw(image)
        title_x = int(12 * scale)
        title_y = int(8 * scale)
        draw.text((title_x, title_y), title, fill=FG_MAIN, font=title_font)
        if subtitle:
            title_bbox = draw.textbbox((title_x, title_y), title, font=title_font)
            subtitle_y = title_bbox[3] + int(4 * scale)
            draw.text(
                (title_x, subtitle_y),
                subtitle,
                fill=FG_MUTED,
                font=subtitle_font,
            )

        margin_left = int(12 * scale)
        margin_right = int(12 * scale)
        plot_left = margin_left
        plot_width = width - margin_left - margin_right
        plot_top = header
        plot_bottom = image_height - footer
        usable_height = max(1.0, plot_bottom - plot_top)

        draw.rounded_rectangle(
            [plot_left, plot_top, plot_left + plot_width, plot_bottom],
            radius=int(6 * scale),
            fill=self._hex_rgba(BG_FIELD, 255),
            outline=BORDER_COLOR,
            width=1,
        )

        for fraction in (0.25, 0.5, 0.75):
            y = plot_bottom - usable_height * fraction
            draw.line(
                [(plot_left + int(8 * scale), y), (plot_left + plot_width - int(8 * scale), y)],
                fill=self._hex_rgba(BORDER_COLOR, 120),
                width=1,
            )

        if target_low is not None and target_high is not None:
            band_x0 = self._lufs_to_x(min(target_low, target_high), plot_left, plot_width, lo, hi)
            band_x1 = self._lufs_to_x(max(target_low, target_high), plot_left, plot_width, lo, hi)
            draw.rectangle(
                [band_x0, plot_top, band_x1, plot_bottom],
                fill=self._hex_rgba(ACCENT, CHART_TARGET_BAND_ALPHA),
            )
            edge_width = max(1, int(round(1 * scale)))
            for edge_x in (band_x0, band_x1):
                draw.line(
                    [(edge_x, plot_top), (edge_x, plot_bottom)],
                    fill=self._hex_rgba(ACCENT, 140),
                    width=edge_width,
                )

        curve_points: list[tuple[float, float]] = []
        for lufs, amount in zip(lufs_values, density):
            x = self._lufs_to_x(lufs, plot_left, plot_width, lo, hi)
            y = plot_bottom - amount * usable_height
            curve_points.append((x, y))

        if len(curve_points) >= 2:
            draw.line(curve_points, fill=ICE_SOFT, width=max(2, int(round(2.5 * scale))))

        if median_lufs is not None and lo <= median_lufs <= hi:
            mx = self._lufs_to_x(median_lufs, plot_left, plot_width, lo, hi)
            my = plot_bottom - self._interp_curve_y(lufs_values, density, median_lufs) * usable_height
            radius = max(3, int(round(3.5 * scale)))
            draw.ellipse(
                [mx - radius, my - radius, mx + radius, my + radius],
                fill=ICE,
                outline=FG_MAIN,
                width=1,
            )

        if preview_lufs is not None and lo <= preview_lufs <= hi:
            px = self._lufs_to_x(preview_lufs, plot_left, plot_width, lo, hi)
            draw.line(
                [(px, plot_top + int(6 * scale)), (px, plot_bottom - int(4 * scale))],
                fill=WARN_FG,
                width=max(2, int(round(2 * scale))),
            )
            preview_label = f"{preview_lufs:.1f}"
            label_font = self._chart_font(canvas, CHART_FONT_TICK, bold=True)
            label_bbox = draw.textbbox((0, 0), preview_label, font=label_font)
            pill_pad_x = int(6 * scale)
            pill_pad_y = int(3 * scale)
            pill_w = (label_bbox[2] - label_bbox[0]) + pill_pad_x * 2
            pill_h = (label_bbox[3] - label_bbox[1]) + pill_pad_y * 2
            pill_left = max(plot_left, min(plot_left + plot_width - pill_w, px - pill_w / 2.0))
            pill_top = plot_top + int(5 * scale)
            pill_right = pill_left + pill_w
            pill_bottom = pill_top + pill_h
            draw.rounded_rectangle(
                [pill_left, pill_top, pill_right, pill_bottom],
                radius=int(5 * scale),
                fill=self._hex_rgba(WARN_FG, 210),
            )
            draw.text(
                ((pill_left + pill_right) / 2.0, (pill_top + pill_bottom) / 2.0),
                preview_label,
                fill=BG_MAIN,
                font=label_font,
                anchor="mm",
            )

        self._draw_distribution_legend(
            draw,
            plot_left=float(plot_left),
            plot_top=float(plot_top),
            plot_width=float(plot_width),
            plot_bottom=float(plot_bottom),
            scale=scale,
            font=self._chart_font(canvas, CHART_LEGEND_FONT),
            show_target_band=target_low is not None and target_high is not None,
            show_median=median_lufs is not None and lo <= median_lufs <= hi,
            show_preview=preview_lufs is not None and lo <= preview_lufs <= hi,
        )

        tick_values = [lo + (hi - lo) * index / 4.0 for index in range(5)]
        for lufs in tick_values:
            x = self._lufs_to_x(lufs, plot_left, plot_width, lo, hi)
            draw.line(
                [(x, plot_bottom), (x, plot_bottom + int(5 * scale))],
                fill=BORDER_COLOR,
                width=1,
            )
            label = f"{lufs:.0f}"
            bbox = draw.textbbox((0, 0), label, font=tick_font)
            text_w = bbox[2] - bbox[0]
            draw.text((x - text_w / 2.0, plot_bottom + int(7 * scale)), label, fill=FG_MUTED, font=tick_font)

        self._canvas_photos[canvas] = ImageTk.PhotoImage(image)
        canvas.create_image(0, 0, anchor="nw", image=self._canvas_photos[canvas])

    def _draw_bar_chart(
        self,
        canvas: tk.Canvas,
        labels: list[str],
        values: list[int],
        *,
        title: str,
    ) -> None:
        canvas.delete("all")
        width, image_height, scale = self._chart_canvas_size(canvas)
        title_font = self._chart_font(canvas, CHART_FONT_TITLE, bold=True)
        label_font = self._chart_font(canvas, CHART_FONT_BAR_LABEL)

        if not values or sum(values) == 0:
            canvas.create_text(
                width // 2,
                image_height // 2,
                text="No data",
                fill=FG_MUTED,
                anchor="center",
                font=self._app._font(TYPE_CAPTION),
            )
            return

        image = Image.new("RGBA", (width, image_height), LOG_BG)
        draw = ImageDraw.Draw(image)
        draw.text((int(10 * scale), int(6 * scale)), title, fill=FG_MAIN, font=title_font)
        label_col_right = max(
            draw.textbbox((0, 0), label, font=label_font)[2]
            for label in labels
        ) + int(10 * scale)
        max_val = max(values) or 1
        margin_left = label_col_right + int(10 * scale)
        margin_right = int(14 * scale)
        plot_width = width - margin_left - margin_right
        plot_top = int(28 * scale)
        plot_bottom = image_height - int(12 * scale)
        row_height = max(int(32 * scale), (plot_bottom - plot_top) / len(labels))
        value_pad_x = int(10 * scale)
        for index, (label, value) in enumerate(zip(labels, values)):
            row_top = plot_top + index * row_height
            row_bottom = row_top + row_height
            bar_pad_y = int(5 * scale)
            y0 = row_top + bar_pad_y
            y1 = row_bottom - bar_pad_y
            bar_height = y1 - y0
            bar_width = int((value / max_val) * plot_width)
            bar_right = margin_left + bar_width
            label_w = self._pil_text_width(draw, label, label_font)
            draw.text(
                (label_col_right - label_w, self._pil_text_top_y(draw, label, label_font, y0, y1)),
                label,
                fill=FG_MUTED,
                font=label_font,
            )
            if value > 0:
                draw.rectangle([margin_left, y0, bar_right, y1], fill=ICE_FILL)
                value_text = str(value)
                value_font = self._bar_count_font(
                    draw,
                    value_text,
                    bar_height=bar_height,
                    bar_width=bar_width,
                    value_pad_x=value_pad_x,
                )
                text_w = self._pil_text_width(draw, value_text, value_font)
                value_y = self._pil_text_top_y(draw, value_text, value_font, y0, y1)
                if bar_width >= text_w + value_pad_x * 2:
                    draw.text(
                        (bar_right - value_pad_x - text_w, value_y),
                        value_text,
                        fill=FG_MAIN,
                        font=value_font,
                    )
                else:
                    draw.text(
                        (bar_right + int(6 * scale), value_y),
                        value_text,
                        fill=FG_MAIN,
                        font=value_font,
                    )
            else:
                zero_text = "0"
                zero_font = self._bar_count_font(
                    draw,
                    zero_text,
                    bar_height=bar_height,
                    bar_width=plot_width,
                    value_pad_x=value_pad_x,
                )
                draw.text(
                    (margin_left + int(6 * scale), self._pil_text_top_y(draw, zero_text, zero_font, y0, y1)),
                    zero_text,
                    fill=FG_MUTED,
                    font=zero_font,
                )

        self._canvas_photos[canvas] = ImageTk.PhotoImage(image)
        canvas.create_image(0, 0, anchor="nw", image=self._canvas_photos[canvas])

    def _distribution_markers(self) -> tuple[float | None, float | None, float | None, float | None]:
        target_low = target_high = preview_lufs = median_lufs = None
        settings = self._app._current_dropgain_settings()
        if settings is not None:
            target_low = settings.target_low_lufs
            target_high = settings.target_high_lufs
        try:
            preview_lufs = float(self.var_preview_target.get())
        except (tk.TclError, TypeError, ValueError):
            preview_lufs = None
        if self._profile is not None:
            median_lufs = self._profile.median_loudest_lufs
        return target_low, target_high, preview_lufs, median_lufs

    def _distribution_subtitle(self, track_count: int) -> str:
        profile = self._profile
        if profile is None:
            return f"{track_count} tracks"
        parts = [f"{track_count} tracks"]
        if profile.median_loudest_lufs is not None:
            parts.append(f"median {profile.median_loudest_lufs:.1f} LUFS")
        if profile.p10_loudest_lufs is not None and profile.p90_loudest_lufs is not None:
            parts.append(
                f"p10–p90 {profile.p10_loudest_lufs:.1f} to {profile.p90_loudest_lufs:.1f}"
            )
        return " · ".join(parts)

    def _draw_distribution(self, rows: list[dict[str, object]]) -> None:
        from analysis import parse_optional_float

        loudest = [
            v
            for row in rows
            if (v := parse_optional_float(row.get("loudest_section_lufs", ""))) is not None
        ]
        lufs_values, density, lo, hi = lufs_distribution_curve(loudest)
        target_low, target_high, preview_lufs, median_lufs = self._distribution_markers()
        self._draw_distribution_curve(
            self.hist_canvas,
            lufs_values,
            density,
            lo,
            hi,
            title="Loudest-section distribution",
            subtitle=self._distribution_subtitle(len(loudest)),
            target_low=target_low,
            target_high=target_high,
            preview_lufs=preview_lufs,
            median_lufs=median_lufs,
        )

    def _draw_render_impact(self, rows: list[dict[str, object]]) -> None:
        buckets = limiter_bucket_counts(rows)  # type: ignore[arg-type]
        labels = ["clean", "light", "moderate", "heavy"]
        values = [buckets[label] for label in labels]
        self._draw_bar_chart(
            self.impact_canvas,
            labels,
            values,
            title="Limiter reduction buckets",
        )
