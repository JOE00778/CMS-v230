"""Split one-label-per-page ECMS PDFs without storing customer data."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal

import pymupdf as fitz

LABEL_NUMBER_REGION = (0.68, 0.34, 0.99, 0.41)
TRACKING_RE = re.compile(r"(?<!\d)\d{13}(?!\d)")

Status = Literal["success", "unrecognized", "ambiguous", "duplicate"]
OcrCallable = Callable[[bytes], tuple[object, object]]


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


def _load_local_ocr() -> OcrCallable | None:
    try:
        from rapidocr_onnxruntime import RapidOCR

        return RapidOCR()
    except Exception:
        return None


def _ocr_candidates(page: fitz.Page, ocr: OcrCallable) -> tuple[str, ...]:
    pix = page.get_pixmap(
        matrix=fitz.Matrix(4, 4),
        clip=_clip_for(page),
        alpha=False,
    )
    result, _ = ocr(pix.tobytes("png"))
    texts = []
    for entry in result or []:
        if len(entry) >= 2:
            texts.append(str(entry[1]))
    return find_tracking_candidates("\n".join(texts))


def _recognize_page(
    page: fitz.Page,
    ocr: OcrCallable | Literal[False] | None,
) -> tuple[Status, str, str, str]:
    text_candidates = _text_candidates(page)
    if len(text_candidates) == 1:
        return "success", text_candidates[0], "text", ""

    engine = _load_local_ocr() if ocr is None else ocr
    if engine is False or engine is None:
        status: Status = "ambiguous" if len(text_candidates) > 1 else "unrecognized"
        reason = (
            "本地 OCR 不可用"
            if engine is None
            else "固定区域未找到唯一的 13 位单号"
        )
        return status, "", "", reason

    ocr_candidates = _ocr_candidates(page, engine)
    if len(ocr_candidates) == 1:
        return "success", ocr_candidates[0], "ocr", ""
    if len(ocr_candidates) > 1:
        return "ambiguous", "", "ocr", "固定区域识别到多个 13 位单号"
    return "unrecognized", "", "ocr", "固定区域未识别到 13 位单号"


def split_label_pdf(
    pdf_bytes: bytes,
    *,
    ocr: OcrCallable | Literal[False] | None = None,
) -> SplitResult:
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
            status, number, method, reason = _recognize_page(page, ocr)
            rows.append(
                PageResult(
                    page_number=index + 1,
                    status=status,
                    tracking_number=number,
                    recognition_method=method,
                    reason=reason,
                )
            )
        return SplitResult(doc.page_count, tuple(rows), b"")
    finally:
        doc.close()
