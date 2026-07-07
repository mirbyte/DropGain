"""DropGain GUI theme: colors, typography, layout, and table definitions."""

from __future__ import annotations

# DropGain color palette
BG_MAIN = "#1e1e1e"
BG_CARD = "#262626"
BG_PANEL = "#212121"
FG_MAIN = "#f0f0f0"
FG_MUTED = "#909090"
ACCENT = "#b0dce8"
ACCENT_HOVER = "#8ec8d6"
ACCENT_SOFT = "#8ec8d6"
ACCENT_DIM = "#243338"
BG_FIELD = "#1a1a1a"
BORDER_COLOR = "#3e3e3e"
SELECTION_BG = "#243338"
TABLE_SELECTION_BG = "#304f58"
HEADER_BG = "#1a1a1a"
METRIC_BG = "#2a2a2a"
LOG_BG = BG_PANEL
ICE = "#b0dce8"
ICE_SOFT = "#8ec8d6"
ICE_FILL = "#6aadb8"
ICE_DIM = "#243338"
ERROR_FG = "#ff6b6b"
WARN_FG = "#d4b878"
SUCCESS_FG = "#87d6a3"
WAVEFORM_FILL = ICE_FILL
WAVEFORM_PEAK_FILL = "#3d6670"
WAVEFORM_RMS_FILL = ICE_FILL
WAVEFORM_BG = BG_FIELD
WAVEFORM_TARGET_BAND = "#1f363b"
WAVEFORM_DROP_BG = "#243338"
WAVEFORM_GRID = "#343434"
WAVEFORM_CURVE = "#ffffff"
WAVEFORM_MARKER = WARN_FG
WAVEFORM_LIMITER_ZONE = "#2a3236"
WAVEFORM_SUPERSAMPLE = 2
WAVEFORM_LUFS_SCALE_WIDTH = 42
WAVEFORM_MIN_WIDTH = 240
WAVEFORM_MIN_HEIGHT = 96
WAVEFORM_FONT_MESSAGE_BASE = 13
WAVEFORM_FONT_LABEL_BASE = 11
WAVEFORM_FONT_VALUE_BASE = 12
WAVEFORM_FONT_SCALE_BASE = 10
WAVEFORM_FONT_PILL_BASE = 10
WAVEFORM_TEXT_BADGE_ALPHA = 140
TABLE_ROW_ODD = "#1c1c1c"
TABLE_ROW_EVEN = "#242424"
BUTTON_TEXT_DARK = "#141414"
BUTTON_CORNER_RADIUS = 14
BUTTON_HEIGHT = 36
ACTION_BUTTON_CORNER_RADIUS = BUTTON_HEIGHT // 2
METRIC_CHIP_HEIGHT = 24
METRIC_CHIP_CORNER_RADIUS = METRIC_CHIP_HEIGHT // 2
METRIC_CHIP_PADX = 6
FOLDER_ENTRY_WIDTH = 280
PROCESS_ACTION_COLUMN_WIDTH = 208

# Spacing (4px base unit)
SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_6 = 24
PAGE_PADX = SPACE_4
PAGE_PADY_TOP = SPACE_3
SECTION_GAP = SPACE_2
CARD_PAD = 10

DASHBOARD_SECTION_GAP = SPACE_6
DASHBOARD_METRICS_TOP_PAD = SPACE_6
DASHBOARD_LAYOUT_COMPACT_WIDTH = 1120
DASHBOARD_LAYOUT_STACKED_WIDTH = 880
PREFERENCES_SINGLE_COLUMN_WIDTH = 900
PREFERENCES_TWO_COLUMN_WIDTH = 960
PREFERENCES_LAYOUT_DEBOUNCE_MS = 80
WINDOW_DESIGN_MIN_WIDTH = 1180
WINDOW_DESIGN_MIN_HEIGHT = 760
WINDOW_DESIGN_DEFAULT_WIDTH = 1240
WINDOW_DESIGN_DEFAULT_HEIGHT = 1500
WINDOW_DESIGN_MIN_WIDTH_FLOOR = 900
WINDOW_DESIGN_MIN_HEIGHT_FLOOR = 600
WINDOW_SCREEN_MARGIN_X = 40
WINDOW_SCREEN_MARGIN_Y = 80
BUTTON_DISABLED_FG = "#1a1a1a"
BUTTON_DISABLED_BORDER = "#2a2a2a"
BUTTON_DISABLED_TEXT = "#505050"
BUTTON_SECONDARY_BG = "#2e2e2e"
BUTTON_SECONDARY_HOVER = "#383838"
CARD_CORNER_RADIUS = 8

# Type scale (Segoe UI)
TYPE_DISPLAY = 28
TYPE_H1 = 15
TYPE_BODY = 13
TYPE_LABEL = 12
TYPE_CAPTION = 11
TYPE_MICRO = 10
TABLE_CELL_FONT_FAMILY = "Cascadia Mono"
TABLE_CELL_FONT_FALLBACKS = ("Consolas", "Courier New", "Segoe UI")
TABLE_HEADING_SIZE = TYPE_CAPTION
TABLE_CELL_SIZE = TYPE_CAPTION

METRIC_TILE_WIDTH = 170
METRIC_TILE_HEIGHT = 100
METRIC_TILE_GAP = 16
METRIC_TILE_CORNER_RADIUS = 16
METRIC_TILE_VALUE_SIZE = 42
METRIC_TILE_LABEL_SIZE = 11
PROCESS_SETTINGS_WRAP_WIDTH = 700
PROCESS_SETTINGS_ROW_PADY = 15
PROCESS_SETTINGS_VALUE_SEP = "   ·   "
PROCESS_DASHBOARD_PROGRESS_GAP = SPACE_1

TOOLTIP_PADX = 10
TOOLTIP_PADY = 8
TOOLTIP_OFFSET_X = 18
TOOLTIP_OFFSET_Y = 16
TOOLTIP_SCREEN_MARGIN = 12
TREEVIEW_ROW_HEIGHT = 30
TREEVIEW_HEADING_PAD = 22
TREEVIEW_CELL_PAD = 16
TREEVIEW_ROW_EXTRA_PAD = 5

# 2-tone waveform colors (warm bass, cool treble; aligned with accent palette)
WAVEFORM_BAND_LOW = "#a67d85"
WAVEFORM_BAND_OTHER = ICE_FILL

WAVEFORM_PREVIEW_SAMPLE_RATE = 8_000
WAVEFORM_PREVIEW_MAX_POINTS = 1_800
OUTPUT_LOG_LINES = 9
OUTPUT_LOG_LINE_PIXELS = 14
OUTPUT_LOG_PADDING = 16
WAVEFORM_CONTENT_HEIGHT = 168
OUTPUT_CONTENT_HEIGHT = max(
    (OUTPUT_LOG_LINES * OUTPUT_LOG_LINE_PIXELS) + OUTPUT_LOG_PADDING,
    WAVEFORM_CONTENT_HEIGHT,
)
WAVEFORM_LOUDNESS_SMOOTH_SECONDS = 5.0

RESULTS_TABLE_FILENAME_MIN = 380
RESULTS_TABLE_FILENAME_ABSOLUTE_MAX = 1140
RESULTS_TABLE_COLUMNS = (
    # column_id, heading, anchor, widest typical cell text, hover tooltip
    (
        "filename",
        "Name",
        "w",
        "",
        "Filename within the source folder.",
    ),
    (
        "gain",
        "Gain",
        "e",
        "+12.34 dB",
        "Suggested gain to bring the loudest section into your target loudness window.",
    ),
    (
        "current_lufs",
        "Current LUFS",
        "e",
        "-12.34 LUFS",
        "Measured LUFS of the loudest detected section in the source file, usually the drop or chorus.",
    ),
    (
        "projected_lufs",
        "Projected LUFS",
        "e",
        "-12.34 LUFS",
        "Estimated loudest-section LUFS after applying the suggested gain, before render or limiter.",
    ),
    (
        "true_peak",
        "TP",
        "e",
        "-12.34 dBTP",
        "Source true peak (dBTP) before any gain change.",
    ),
    (
        "projected_true_peak",
        "Projected TP",
        "e",
        "-12.34 dBTP",
        "Estimated true peak after the suggested gain, before any limiter.",
    ),
    (
        "limiting",
        "Peak control",
        "e",
        "1.47 dB",
        "Peak reduction FabFilter Pro-L would apply to stay under your ceiling. "
        "(clean) means no limiter is needed.",
    ),
    (
        "status",
        "Status",
        "center",
        "● Zero-gain skip",
        "Whether the track is ready to render, already in range, below the minimum "
        "change threshold, or needs manual review.",
    ),
    (
        "warnings",
        "Notes",
        "w",
        "",
        "Analysis notes, processing decisions, verification issues, and errors for this track.",
    ),
)
