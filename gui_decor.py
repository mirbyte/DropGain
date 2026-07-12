"""PIL helpers for DropGain signal-console surface treatment."""

from __future__ import annotations

from PIL import ImageDraw


def parse_hex(hex_color: str) -> tuple[int, int, int]:
    text = hex_color.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def hex_rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    red, green, blue = parse_hex(hex_color)
    return red, green, blue, alpha


def draw_signal_grid(
    draw: ImageDraw.ImageDraw,
    left: float,
    top: float,
    right: float,
    bottom: float,
    *,
    spacing: float,
    color: str,
    alpha: int,
    major_every: int = 4,
) -> None:
    column = 0
    x = left
    while x <= right:
        line_alpha = alpha if column % major_every else min(255, alpha + 24)
        draw.line((x, top, x, bottom), fill=hex_rgba(color, line_alpha), width=1)
        x += spacing
        column += 1

    row = 0
    y = top
    while y <= bottom:
        line_alpha = alpha if row % major_every else min(255, alpha + 18)
        draw.line((left, y, right, y), fill=hex_rgba(color, line_alpha), width=1)
        y += spacing
        row += 1


def draw_scanlines(
    draw: ImageDraw.ImageDraw,
    left: float,
    top: float,
    right: float,
    bottom: float,
    *,
    spacing: float,
    alpha: int,
) -> None:
    y = top
    while y < bottom:
        draw.line((left, y, right, y), fill=(255, 255, 255, alpha), width=1)
        y += spacing


def draw_inset_frame(
    draw: ImageDraw.ImageDraw,
    left: float,
    top: float,
    right: float,
    bottom: float,
    *,
    border: str,
    highlight: str,
    shadow: str,
    width: int = 1,
) -> None:
    draw.rectangle((left, top, right, bottom), outline=hex_rgba(shadow), width=width)
    inset = max(1, width)
    draw.rectangle(
        (left + inset, top + inset, right - inset, bottom - inset),
        outline=hex_rgba(highlight, 72),
        width=1,
    )
    draw.rectangle((left, top, right, bottom), outline=hex_rgba(border), width=width)


def draw_corner_brackets(
    draw: ImageDraw.ImageDraw,
    left: float,
    top: float,
    right: float,
    bottom: float,
    *,
    color: str,
    alpha: int,
    arm: float,
    width: int = 1,
) -> None:
    fill = hex_rgba(color, alpha)
    draw.line((left, top + arm, left, top), fill=fill, width=width)
    draw.line((left, top, left + arm, top), fill=fill, width=width)
    draw.line((right - arm, top, right, top), fill=fill, width=width)
    draw.line((right, top, right, top + arm), fill=fill, width=width)
    draw.line((left, bottom - arm, left, bottom), fill=fill, width=width)
    draw.line((left, bottom, left + arm, bottom), fill=fill, width=width)
    draw.line((right - arm, bottom, right, bottom), fill=fill, width=width)
    draw.line((right, bottom - arm, right, bottom), fill=fill, width=width)
