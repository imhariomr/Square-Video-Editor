"""Renders the text layer (either a solid black caption band, or a
semi-transparent overlay box) as a PNG using Pillow. ffmpeg then just
overlays this pre-rendered image onto the video — all font/wrapping/
alignment logic lives here in one place, in normal Python, instead of
fighting ffmpeg's drawtext filter (which can't auto-wrap text).
"""
import os
import textwrap

from PIL import Image, ImageDraw, ImageFont

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_BOLD = os.path.join(FONTS_DIR, "Poppins-Bold.ttf")
FONT_REGULAR = os.path.join(FONTS_DIR, "Poppins-Regular.ttf")

MIN_FONT_SIZE = 18
MAX_FONT_SIZE = 96
LINE_SPACING = 1.3


def get_font(bold, size):
    path = FONT_BOLD if bold else FONT_REGULAR
    return ImageFont.truetype(path, size)


def _line_width(draw, line, font):
    bbox = draw.textbbox((0, 0), line, font=font)
    return bbox[2] - bbox[0]


def wrap_text(text, font, max_width, draw):
    """Wraps text to fit max_width (pixels), respecting the user's own
    line breaks and greedily packing words onto each line."""
    lines = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            lines.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if _line_width(draw, candidate, font) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _even(n):
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


def render_band_image(text, canvas_w, font_size, bold, align, padding_x=48, padding_y=28):
    """A solid black band, full canvas width, sized to exactly fit the
    wrapped text (matches the reference screenshot's top caption bar)."""
    font = get_font(bold, font_size)
    probe = Image.new("RGBA", (canvas_w, 10))
    draw = ImageDraw.Draw(probe)

    max_text_width = canvas_w - 2 * padding_x
    lines = wrap_text(text or " ", font, max_text_width, draw)

    line_height = int(font_size * LINE_SPACING)
    band_h = _even(padding_y * 2 + line_height * len(lines))

    img = Image.new("RGBA", (canvas_w, band_h), (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)

    total_text_h = line_height * len(lines)
    y = (band_h - total_text_h) / 2 + (line_height - font_size) / 2
    for line in lines:
        w = _line_width(draw, line, font)
        if align == "left":
            x = padding_x
        elif align == "right":
            x = canvas_w - padding_x - w
        else:
            x = (canvas_w - w) / 2
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height

    return img, band_h


def render_overlay_image(text, canvas_w, font_size, bold, align, max_width_ratio=0.86,
                          padding_x=32, padding_y=20, box_opacity=140, box_radius=18):
    """A semi-transparent rounded box with white text, sized to fit the
    wrapped text — meant to float on top of the video (center/bottom
    caption style) rather than reserve a dedicated black band."""
    font = get_font(bold, font_size)
    probe = Image.new("RGBA", (canvas_w, 10))
    draw = ImageDraw.Draw(probe)

    max_text_width = int(canvas_w * max_width_ratio) - 2 * padding_x
    lines = wrap_text(text or " ", font, max_text_width, draw)

    line_height = int(font_size * LINE_SPACING)
    content_w = max(_line_width(draw, line, font) for line in lines) if lines else 0
    box_w = _even(min(content_w + 2 * padding_x, canvas_w * max_width_ratio))
    box_h = _even(padding_y * 2 + line_height * len(lines))

    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, box_w - 1, box_h - 1], radius=box_radius,
                            fill=(0, 0, 0, box_opacity))

    total_text_h = line_height * len(lines)
    y = (box_h - total_text_h) / 2 + (line_height - font_size) / 2
    for line in lines:
        w = _line_width(draw, line, font)
        if align == "left":
            x = padding_x
        elif align == "right":
            x = box_w - padding_x - w
        else:
            x = (box_w - w) / 2
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height

    return img, box_w, box_h


SUBTITLE_PRESETS = {"none", "yellow"}


def render_subtitle_image(text, canvas_w, font_size, preset="none", max_width_ratio=0.86):
    """Renders one auto-generated subtitle line, styled by preset:

    - "none": the same semi-transparent black box + white text as the
      manual floating-box caption (reuses render_overlay_image as-is).
    - "yellow": dark-yellow, not-quite-bold text with a thin black outline
      directly over the video, no background box — the classic burned-in
      subtitle look.
    """
    if preset == "yellow":
        return _render_subtitle_outline(text, canvas_w, font_size, max_width_ratio)
    return render_overlay_image(text, canvas_w, font_size, True, "center", max_width_ratio)


def _render_subtitle_outline(text, canvas_w, font_size, max_width_ratio):
    font = get_font(False, font_size)
    probe = Image.new("RGBA", (canvas_w, 10))
    draw = ImageDraw.Draw(probe)

    stroke_w = max(2, font_size // 16)
    max_text_width = int(canvas_w * max_width_ratio) - 2 * stroke_w
    lines = wrap_text(text or " ", font, max_text_width, draw)

    line_height = int(font_size * LINE_SPACING)
    content_w = max(_line_width(draw, line, font) for line in lines) if lines else 0
    img_w = _even(min(content_w, canvas_w * max_width_ratio) + 4 * stroke_w)
    img_h = _even(line_height * len(lines) + 2 * stroke_w)

    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    total_text_h = line_height * len(lines)
    y = stroke_w + (img_h - 2 * stroke_w - total_text_h) / 2 + (line_height - font_size) / 2
    for line in lines:
        w = _line_width(draw, line, font)
        x = (img_w - w) / 2
        draw.text((x, y), line, font=font, fill=(196, 156, 0, 255),
                   stroke_width=stroke_w, stroke_fill=(0, 0, 0, 255))
        y += line_height

    return img, img_w, img_h


def render_watermark_image(text, font_size=26, bold=True, shadow_offset=2, shadow_opacity=170,
                            padding=6, opacity=1.0):
    """Small bold white text with a subtle drop shadow (so it stays legible
    over any video background, light or dark) — a fixed brand/handle
    watermark burned into every export, sized to exactly fit the text
    itself rather than a full-width band.

    `opacity` (0-1) scales the whole rendered layer's alpha channel
    uniformly *after* drawing, so the text and its shadow fade together
    rather than the text turning gray — same effect as CSS `opacity` on an
    element with a text-shadow.
    """
    font = get_font(bold, font_size)
    probe = Image.new("RGBA", (10, 10))
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    img_w = _even(text_w + 2 * padding + shadow_offset)
    img_h = _even(text_h + 2 * padding + shadow_offset)
    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    x = padding - bbox[0]
    y = padding - bbox[1]
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=(0, 0, 0, shadow_opacity))
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))

    if opacity < 1.0:
        alpha = img.getchannel("A").point(lambda a: int(a * opacity))
        img.putalpha(alpha)

    return img, img_w, img_h
