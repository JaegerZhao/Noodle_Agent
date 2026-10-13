from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf as fitz

_FIGURE_RE = re.compile(
    r"^Figure\s+(?P<label>\d+(?:\.\d+)+(?:[a-z])?)\s*[:.]\s*(?P<caption>.+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FigureCandidate:
    page_number: int
    figure_label: str
    caption: str
    clip: fitz.Rect


def figure_candidates(page: fitz.Page) -> list[FigureCandidate]:
    """Locate textbook figures and return source-faithful page-render crops.

    Rendering the PDF region, rather than exporting image xrefs, keeps vector
    diagrams intact and deterministically merges figures split across xrefs.
    """

    text_blocks = [
        (fitz.Rect(block[:4]), " ".join(str(block[4]).split()))
        for block in page.get_text("blocks")
        if str(block[4]).strip()
    ]
    captions: list[tuple[fitz.Rect, str, str]] = []
    for index, (block_rect, text) in enumerate(text_blocks):
        match = _FIGURE_RE.match(text)
        if match:
            caption_rect = block_rect
            caption_parts = [text]
            previous_rect = block_rect
            for next_rect, next_text in text_blocks[index + 1 :]:
                gap = next_rect.y0 - previous_rect.y1
                if (
                    gap < -1
                    or gap > 6
                    or abs(next_rect.x0 - block_rect.x0) > 12
                    or _FIGURE_RE.match(next_text)
                ):
                    break
                caption_rect |= next_rect
                caption_parts.append(next_text)
                previous_rect = next_rect
            captions.append(
                (caption_rect, match.group("label"), " ".join(caption_parts))
            )
    if not captions:
        return []

    image_rects = [
        fitz.Rect(item["bbox"])
        for item in page.get_image_info(xrefs=True)
        if fitz.Rect(item["bbox"]).get_area() >= 400
    ]
    candidates: list[FigureCandidate] = []
    previous_caption_bottom = 36.0
    content_left = max(36.0, min(item[0].x0 for item in captions))
    content_right = min(page.rect.width - 36.0, max(item[0].x1 for item in captions))

    for caption_rect, label, caption in captions:
        nearby = [
            rect
            for rect in image_rects
            if rect.y0 >= previous_caption_bottom - 4
            and rect.y1 <= caption_rect.y0 + 8
            and caption_rect.y0 - rect.y1 <= 320
        ]
        if nearby:
            top = max(24.0, min(rect.y0 for rect in nearby) - 10)
            left = max(24.0, min(content_left, *(rect.x0 for rect in nearby)) - 8)
            right = min(
                page.rect.width - 24.0,
                max(content_right, *(rect.x1 for rect in nearby)) + 8,
            )
        else:
            top = max(36.0, previous_caption_bottom + 8, caption_rect.y0 - 300)
            left = content_left
            right = content_right
        bottom = min(page.rect.height - 24.0, caption_rect.y1 + 8)
        clip = fitz.Rect(left, top, right, bottom) & page.rect
        if clip.width >= 80 and clip.height >= 60:
            candidates.append(
                FigureCandidate(
                    page_number=page.number + 1,
                    figure_label=label,
                    caption=caption,
                    clip=clip,
                )
            )
        previous_caption_bottom = max(previous_caption_bottom, caption_rect.y1)
    return candidates


def render_figure(page: fitz.Page, candidate: FigureCandidate) -> bytes:
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(1.7, 1.7),
        clip=candidate.clip,
        alpha=False,
        annots=False,
    )
    return pixmap.tobytes("png")
