"""Split one-label-per-page ECMS PDFs without storing customer data.

2026-09-09 実測（0909面单合集.pdf · 24 頁）で判明した事実と口径:
  · ECMS 面单は**ベクター PDF でテキスト層あり**（スキャン画像ではない）。
    OCR は基本不要。旧版が 15/24 頁を落としたのは OCR の問題ではなく、
    単号を「厳密 13 桁」に固定していたため —— この号は**平台注文番号**で、
    Shopee は 13 桁・Coupang は 14 桁。KR 宛 15 頁（Coupang）が全滅していた。
  · 面单のバーコード（Code128 上下 2 本）は ECMS 運単号 `ECLBF...`（24/24 解読可）。
    ファイル名は平台注文番号で付ける（Boss 指定）が、運単号は manifest に
    併記し、単号が取れない頁のファイル名の兜底にも使う。

認識順（各頁）:
  1. 固定窓（右側 ZIP CODE 下）のテキスト層 → 13〜14 桁が唯一なら採用
  2. 全頁テキスト層（版式が動いた時の保険）
  3. バーコード（zxing-cpp · 任意依存 · 未インストールなら黙って跳ばす）→ 運単号
  4. OCR（rapidocr · 任意依存）→ テキスト層の無いスキャン面单の兜底
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Callable, Literal

import pymupdf as fitz

LABEL_NUMBER_REGION = (0.68, 0.34, 0.99, 0.41)
# 平台注文番号: Shopee 13 桁 / Coupang 14 桁（2026-09-09 実測）。
# 前後に数字が続くものは弾く（TEL/ZIP/運単号の数字部分は 3〜11 桁なので当たらない）
TRACKING_RE = re.compile(r"(?<!\d)\d{13,14}(?!\d)")
WAYBILL_RE = re.compile(r"^[A-Z]{2,6}\d{8,16}$")

Status = Literal["success", "unrecognized", "ambiguous", "duplicate"]
OcrCallable = Callable[[bytes], tuple[object, object]]
BarcodeCallable = Callable[[bytes], list[str]]
MANIFEST_FIELDS = (
    "page_number",
    "status",
    "tracking_number",
    "ecms_waybill",
    "recognition_method",
    "output_path",
    "reason",
)


class LabelPdfError(ValueError):
    """Raised when an uploaded PDF cannot be processed safely."""


@dataclass(frozen=True)
class PageResult:
    page_number: int
    status: Status
    tracking_number: str = ""
    ecms_waybill: str = ""
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
    """Return unique 13-14 digit candidates in deterministic order."""
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


def _text_candidates(page: fitz.Page, *, whole_page: bool = False) -> tuple[str, ...]:
    if whole_page:
        return find_tracking_candidates(page.get_text("text"))
    return find_tracking_candidates(page.get_text("text", clip=_clip_for(page)))


def _load_local_ocr() -> OcrCallable | None:
    try:
        from rapidocr_onnxruntime import RapidOCR

        return RapidOCR()
    except Exception:
        return None


def _load_barcode_reader() -> BarcodeCallable | None:
    """zxing-cpp があれば Code128 等を読む callable を返す。無ければ None。"""
    try:
        import zxingcpp
        from PIL import Image
    except Exception:
        return None

    def _read(png: bytes) -> list[str]:
        img = Image.open(io.BytesIO(png))
        return [str(r.text) for r in zxingcpp.read_barcodes(img) if r.text]

    return _read


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


def _barcode_waybill(page: fitz.Page, reader: BarcodeCallable) -> str:
    """頁内バーコードから ECMS 運単号（英字+数字）を 1 つ返す。無ければ空。"""
    pix = page.get_pixmap(dpi=200, alpha=False)
    values = {v.strip() for v in reader(pix.tobytes("png"))}
    waybills = sorted(v for v in values if WAYBILL_RE.match(v))
    return waybills[0] if len(waybills) == 1 else ""


def _recognize_page(
    page: fitz.Page,
    ocr: OcrCallable | Literal[False] | None,
    barcode: BarcodeCallable | Literal[False] | None,
) -> tuple[Status, str, str, str, str]:
    """→ (status, tracking_number, ecms_waybill, method, reason)"""
    # バーコード（運単号）は成功/失敗に関わらず manifest 用に読む
    reader = _load_barcode_reader() if barcode is None else barcode
    waybill = ""
    if reader:
        try:
            waybill = _barcode_waybill(page, reader)
        except Exception:
            waybill = ""

    # 1. 固定窓テキスト層
    win = _text_candidates(page)
    if len(win) == 1:
        return "success", win[0], waybill, "text", ""
    # 2. 全頁テキスト層（窓が外れた版式の保険）
    if len(win) == 0:
        whole = _text_candidates(page, whole_page=True)
        if len(whole) == 1:
            return "success", whole[0], waybill, "text_page", ""
        if len(whole) > 1:
            return "ambiguous", "", waybill, "text_page", "頁内に 13〜14 桁の番号が複数"
    elif len(win) > 1:
        return "ambiguous", "", waybill, "text", "固定窓に 13〜14 桁の番号が複数"

    # 3. OCR（テキスト層が無いスキャン面单の兜底）
    engine = _load_local_ocr() if ocr is None else ocr
    if engine is False or engine is None:
        reason = ("本地 OCR 不可用" if engine is None
                  else "文本层未找到 13~14 位单号")
        return "unrecognized", "", waybill, "", reason
    ocr_c = _ocr_candidates(page, engine)
    if len(ocr_c) == 1:
        return "success", ocr_c[0], waybill, "ocr", ""
    if len(ocr_c) > 1:
        return "ambiguous", "", waybill, "ocr", "OCR 识别到多个 13~14 位单号"
    return "unrecognized", "", waybill, "ocr", "文本层与 OCR 均未找到 13~14 位单号"


def _single_page_pdf(source: fitz.Document, page_index: int) -> bytes:
    output = fitz.open()
    try:
        output.insert_pdf(source, from_page=page_index, to_page=page_index)
        return output.tobytes(garbage=4, deflate=True)
    finally:
        output.close()


def _manifest_bytes(results: list[PageResult]) -> bytes:
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=MANIFEST_FIELDS)
    writer.writeheader()
    for item in results:
        writer.writerow({field: getattr(item, field) for field in MANIFEST_FIELDS})
    return text.getvalue().encode("utf-8-sig")


def split_label_pdf(
    pdf_bytes: bytes,
    *,
    ocr: OcrCallable | Literal[False] | None = None,
    barcode: BarcodeCallable | Literal[False] | None = None,
) -> SplitResult:
    """Read platform tracking numbers (13-14 digits) from every PDF page."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise LabelPdfError("无法读取 PDF：文件可能已损坏或不是 PDF") from exc

    try:
        if doc.needs_pass:
            raise LabelPdfError("无法处理加密 PDF")
        if doc.page_count == 0:
            raise LabelPdfError("PDF 没有页面")

        seen: set[str] = set()
        rows: list[PageResult] = []
        page_files: list[tuple[str, bytes]] = []
        for index, page in enumerate(doc):
            page_number = index + 1
            try:
                status, number, waybill, method, reason = _recognize_page(page, ocr, barcode)
            except Exception:
                status, number, waybill, method, reason = (
                    "unrecognized", "", "", "", "该页处理失败")

            if status == "success" and number in seen:
                status = "duplicate"
                reason = "单号与前面的成功页面重复"

            tag = f"-{waybill}" if waybill else ""
            if status == "success":
                seen.add(number)
                output_path = f"{number}.pdf"
            elif status == "duplicate":
                output_path = f"识别失败/重复单号-第{page_number:03d}页{tag}.pdf"
            else:
                # 単号が取れない頁は運単号を名前に残して人手で対応できるようにする
                output_path = f"识别失败/第{page_number:03d}页{tag}.pdf"

            rows.append(PageResult(
                page_number=page_number, status=status, tracking_number=number,
                ecms_waybill=waybill, recognition_method=method,
                output_path=output_path, reason=reason))
            try:
                page_files.append((output_path, _single_page_pdf(doc, index)))
            except Exception as exc:
                raise LabelPdfError(f"无法复制第 {page_number} 页") from exc

        zip_buffer = io.BytesIO()
        try:
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for path, data in page_files:
                    archive.writestr(path, data)
                archive.writestr("处理结果.csv", _manifest_bytes(rows))
        except Exception as exc:
            raise LabelPdfError("无法生成下载 ZIP") from exc

        return SplitResult(doc.page_count, tuple(rows), zip_buffer.getvalue())
    finally:
        doc.close()
