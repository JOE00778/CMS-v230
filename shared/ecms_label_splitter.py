"""Split one-label-per-page ECMS PDFs without storing customer data."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import pymupdf as fitz

LABEL_NUMBER_REGION = (0.68, 0.34, 0.99, 0.41)
TRACKING_RE = re.compile(r"(?<!\d)\d{13}(?!\d)")

Status = Literal["success", "unrecognized", "ambiguous", "duplicate"]


class LabelPdfError(ValueError):
    """Raised when an uploaded PDF cannot be processed safely."""


@dataclass(frozen=True)
class PageResult:
    page_number: int
    status: Status
    tracking_number: str = ""
    recognition_method: str = ""
    output_path: str = ""
    reason: str = ""


@dataclass(frozen=True)
class SplitResult:
    total_pages: int
    results: tuple[PageResult, ...]
    zip_bytes: bytes

    @property
    def success_count(self) -> int:
        return sum(item.status == "success" for item in self.results)

    @property
    def duplicate_count(self) -> int:
        return sum(item.status == "duplicate" for item in self.results)

    @property
    def failed_count(self) -> int:
        return sum(
            item.status in {"unrecognized", "ambiguous"} for item in self.results
        )


def find_tracking_candidates(text: str) -> tuple[str, ...]:
    """Return unique strict 13-digit candidates in deterministic order."""
    return tuple(sorted(set(TRACKING_RE.findall(text or ""))))


def _clip_for(page: fitz.Page) -> fitz.Rect:
    x0, y0, x1, y1 = LABEL_NUMBER_REGION
    rect = page.rect
    return fitz.Rect(
        rect.x0 + rect.width * x0,
        rect.y0 + rect.height * y0,
        rect.x0 + rect.width * x1,
        rect.y0 + rect.height * y1,
    )


def _text_candidates(page: fitz.Page) -> tuple[str, ...]:
    return find_tracking_candidates(page.get_text("text", clip=_clip_for(page)))


def split_label_pdf(pdf_bytes: bytes, *, ocr=True) -> SplitResult:
    """Read strict tracking numbers from the fixed region of every PDF page."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise LabelPdfError("无法读取 PDF：文件可能已损坏或不是 PDF") from exc

    try:
        if doc.needs_pass:
            raise LabelPdfError("无法处理加密 PDF")
        if doc.page_count == 0:
            raise LabelPdfError("PDF 没有页面")

        rows = []
        for index, page in enumerate(doc):
            candidates = _text_candidates(page)
            number = candidates[0] if len(candidates) == 1 else ""
            status: Status = "success" if number else "unrecognized"
            rows.append(
                PageResult(
                    page_number=index + 1,
                    status=status,
                    tracking_number=number,
                    recognition_method="text" if number else "",
                )
            )
        return SplitResult(doc.page_count, tuple(rows), b"")
    finally:
        doc.close()
