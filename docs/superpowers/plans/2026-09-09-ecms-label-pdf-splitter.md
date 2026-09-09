# ECMS Label PDF Splitter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CMS tool that splits a one-label-per-page ECMS PDF into single-page PDFs named with each label's 13-digit tracking number and downloads every page in one ZIP.

**Architecture:** Keep PDF processing in a Streamlit-independent `shared` module. Use PyMuPDF to read the fixed label-number region, copy original pages, and render only that region when local RapidOCR is needed; use a thin Streamlit page for upload, status display, and ZIP download.

**Tech Stack:** Python 3.11, Streamlit, PyMuPDF, RapidOCR ONNX Runtime, pytest, standard-library `csv` and `zipfile`

---

## File map

- Create `shared/ecms_label_splitter.py`: validate PDFs, extract tracking numbers, copy pages, build the manifest, and create the ZIP.
- Create `tests/test_ecms_label_splitter.py`: synthetic-PDF unit tests for text extraction, OCR fallback, failures, duplicates, page preservation, and ZIP completeness.
- Create `pages/42_✂️_面单合集拆分.py`: upload and process one PDF, show metrics and exceptions, and offer the ZIP download.
- Modify `shared/i18n.py`: register the page in the Tools group and add Japanese UI translations.
- Modify `requirements.txt`: add the production PyMuPDF dependency.
- Modify `pyproject.toml`: add the same dependency to the local project definition.

The implementation must not modify `pages/41_📮_ECMS发货.py`, write uploaded content to disk or a database, or call an external OCR service.

### Task 1: Declare and verify the PDF dependency

**Files:**
- Modify: `requirements.txt:17-19`
- Modify: `pyproject.toml:6-13`

- [ ] **Step 1: Add PyMuPDF to both dependency manifests**

Add this production entry after `requests` in `requirements.txt`:

```text
# Page 42 ECMS 面单合集拆分 · PDF 文字提取、固定区域渲染和原页复制
PyMuPDF>=1.24
```

Add the same package to `[project].dependencies` in `pyproject.toml`:

```toml
    "PyMuPDF>=1.24",
```

- [ ] **Step 2: Synchronize the local environment**

Run:

```bash
uv sync --all-extras
```

Expected: exit code 0 and PyMuPDF is installed without removing pytest.

- [ ] **Step 3: Verify the import under the project Python**

Run:

```bash
uv run python -c "import pymupdf; print(pymupdf.__version__)"
```

Expected: exit code 0 and a version at or above 1.24.

- [ ] **Step 4: Commit the dependency declaration**

```bash
git add requirements.txt pyproject.toml uv.lock
git commit -m "➕ chore: add PDF processing dependency"
```

If `uv sync` does not change `uv.lock`, omit it from `git add`. Do not stage unrelated files.

### Task 2: Build strict fixed-region text recognition

**Files:**
- Create: `shared/ecms_label_splitter.py`
- Create: `tests/test_ecms_label_splitter.py`

- [ ] **Step 1: Write failing tests for strict candidate parsing and fixed-region text extraction**

Create the test helpers and initial tests:

```python
from __future__ import annotations

import pymupdf as fitz
import pytest

from shared.ecms_label_splitter import (
    LABEL_NUMBER_REGION,
    LabelPdfError,
    find_tracking_candidates,
    split_label_pdf,
)


def make_pdf(numbers: list[str | None], *, outside_number: str | None = None) -> bytes:
    doc = fitz.open()
    for number in numbers:
        page = doc.new_page(width=512, height=747)
        page.insert_text((20, 40), "ECMS TEST LABEL")
        if outside_number:
            page.insert_text((20, 120), outside_number)
        if number:
            page.insert_text((375, 282), number)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6102821704186", ("6102821704186",)),
        ("x6102821704186y", ("6102821704186",)),
        ("610282170418", ()),
        ("61028217041867", ()),
        ("6102821704186 6102821704187", ("6102821704186", "6102821704187")),
        ("61O28217O4186", ()),
    ],
)
def test_find_tracking_candidates_is_strict(text, expected):
    assert find_tracking_candidates(text) == expected


def test_fixed_region_ignores_13_digit_number_elsewhere():
    result = split_label_pdf(
        make_pdf(["6102821704186"], outside_number="9999999999999"),
        ocr=False,
    )
    assert result.results[0].tracking_number == "6102821704186"
    assert result.results[0].recognition_method == "text"


def test_rejects_invalid_pdf():
    with pytest.raises(LabelPdfError, match="无法读取 PDF"):
        split_label_pdf(b"not a pdf", ocr=False)
```

The synthetic page uses the same 512 × 747 proportions as the supplied screenshot. `LABEL_NUMBER_REGION` remains a relative rectangle so the production code works with other page dimensions.

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run pytest tests/test_ecms_label_splitter.py -v
```

Expected: collection fails because `shared.ecms_label_splitter` does not exist.

- [ ] **Step 3: Add the result types, validation error, and strict text extractor**

Create `shared/ecms_label_splitter.py` with these public types and helpers:

```python
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Callable, Literal

import pymupdf as fitz

LABEL_NUMBER_REGION = (0.68, 0.34, 0.99, 0.41)
TRACKING_RE = re.compile(r"(?<!\d)\d{13}(?!\d)")

Status = Literal["success", "unrecognized", "ambiguous", "duplicate"]


class LabelPdfError(ValueError):
    """Raised when the uploaded batch cannot be processed safely."""


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
        return sum(item.status in {"unrecognized", "ambiguous"} for item in self.results)


def find_tracking_candidates(text: str) -> tuple[str, ...]:
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
```

Add a temporary batch entry point so the text tests can run before OCR and ZIP behavior are implemented:

```python
def split_label_pdf(pdf_bytes: bytes, *, ocr=True) -> SplitResult:
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
            rows.append(PageResult(index + 1, status, number, "text" if number else ""))
        return SplitResult(doc.page_count, tuple(rows), b"")
    finally:
        doc.close()
```

- [ ] **Step 4: Run the focused text tests**

Run:

```bash
uv run pytest tests/test_ecms_label_splitter.py -v
```

Expected: all initial tests pass.

- [ ] **Step 5: Commit strict fixed-region text recognition**

```bash
git add shared/ecms_label_splitter.py tests/test_ecms_label_splitter.py
git commit -m "👔 feat: extract ECMS tracking numbers from PDF"
```

### Task 3: Add local OCR fallback without guessing digits

**Files:**
- Modify: `shared/ecms_label_splitter.py`
- Modify: `tests/test_ecms_label_splitter.py`

- [ ] **Step 1: Write failing OCR fallback tests**

Append:

```python
class FakeOCR:
    def __init__(self, lines):
        self.lines = lines
        self.calls = 0

    def __call__(self, image_bytes):
        self.calls += 1
        return [
            [[[0, 0], [10, 0], [10, 10], [0, 10]], text, 0.99]
            for text in self.lines
        ], None


def test_ocr_fallback_recognizes_image_only_page():
    ocr = FakeOCR(["6102821704186"])
    result = split_label_pdf(make_pdf([None]), ocr=ocr)
    row = result.results[0]
    assert row.status == "success"
    assert row.tracking_number == "6102821704186"
    assert row.recognition_method == "ocr"
    assert ocr.calls == 1


def test_text_success_does_not_call_ocr():
    ocr = FakeOCR(["9999999999999"])
    result = split_label_pdf(make_pdf(["6102821704186"]), ocr=ocr)
    assert result.results[0].tracking_number == "6102821704186"
    assert ocr.calls == 0


def test_ocr_does_not_guess_letters_as_digits():
    result = split_label_pdf(make_pdf([None]), ocr=FakeOCR(["61O28217O4186"]))
    assert result.results[0].status == "unrecognized"


def test_multiple_ocr_candidates_are_ambiguous():
    ocr = FakeOCR(["6102821704186", "6102821704187"])
    result = split_label_pdf(make_pdf([None]), ocr=ocr)
    assert result.results[0].status == "ambiguous"
```

- [ ] **Step 2: Run the OCR tests to verify they fail**

Run:

```bash
uv run pytest tests/test_ecms_label_splitter.py -v
```

Expected: OCR-specific assertions fail because the temporary entry point does not call OCR.

- [ ] **Step 3: Implement lazy local OCR and one-page recognition**

Add:

```python
OcrCallable = Callable[[bytes], tuple[object, object]]


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
        reason = "本地 OCR 不可用" if engine is None else "固定区域未找到唯一的 13 位单号"
        status: Status = "ambiguous" if len(text_candidates) > 1 else "unrecognized"
        return status, "", "", reason

    ocr_candidates = _ocr_candidates(page, engine)
    if len(ocr_candidates) == 1:
        return "success", ocr_candidates[0], "ocr", ""
    if len(ocr_candidates) > 1:
        return "ambiguous", "", "ocr", "固定区域识别到多个 13 位单号"
    return "unrecognized", "", "ocr", "固定区域未识别到 13 位单号"
```

Change `split_label_pdf` to call `_recognize_page` for each page. Preserve the current PDF validation and `finally: doc.close()` block.

- [ ] **Step 4: Run the focused suite**

Run:

```bash
uv run pytest tests/test_ecms_label_splitter.py -v
```

Expected: text and OCR tests pass.

- [ ] **Step 5: Commit the OCR fallback**

```bash
git add shared/ecms_label_splitter.py tests/test_ecms_label_splitter.py
git commit -m "🦺 feat: add local OCR fallback for ECMS labels"
```

### Task 4: Preserve every page and build the downloadable ZIP

**Files:**
- Modify: `shared/ecms_label_splitter.py`
- Modify: `tests/test_ecms_label_splitter.py`

- [ ] **Step 1: Write failing page-preservation, duplicate, and manifest tests**

Append:

```python
import csv
import io
import zipfile


def read_zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def test_builds_one_page_pdfs_and_manifest_without_losing_pages():
    source = make_pdf(["6102821704186", "6102821704187", None])
    result = split_label_pdf(source, ocr=False)
    assert result.total_pages == 3
    assert result.success_count == 2
    assert result.failed_count == 1
    assert result.duplicate_count == 0

    with read_zip(result.zip_bytes) as archive:
        names = set(archive.namelist())
        assert names == {
            "6102821704186.pdf",
            "6102821704187.pdf",
            "识别失败/第003页.pdf",
            "处理结果.csv",
        }
        for name in names - {"处理结果.csv"}:
            page_doc = fitz.open(stream=archive.read(name), filetype="pdf")
            assert page_doc.page_count == 1
            assert page_doc[0].rect == fitz.Rect(0, 0, 512, 747)
            page_doc.close()
        manifest = archive.read("处理结果.csv").decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(manifest)))
        assert len(rows) == 3


def test_duplicate_tracking_number_never_overwrites_first_page():
    result = split_label_pdf(
        make_pdf(["6102821704186", "6102821704186"]),
        ocr=False,
    )
    assert result.success_count == 1
    assert result.duplicate_count == 1
    with read_zip(result.zip_bytes) as archive:
        assert "6102821704186.pdf" in archive.namelist()
        assert "识别失败/重复单号-第002页.pdf" in archive.namelist()


def test_result_counts_cover_every_input_page():
    result = split_label_pdf(
        make_pdf(["6102821704186", None, "6102821704186"]),
        ocr=False,
    )
    assert (
        result.success_count + result.failed_count + result.duplicate_count
        == result.total_pages
    )
```

- [ ] **Step 2: Run the tests to verify ZIP behavior fails**

Run:

```bash
uv run pytest tests/test_ecms_label_splitter.py -v
```

Expected: new tests fail because `zip_bytes` is empty and duplicate handling is absent.

- [ ] **Step 3: Implement original-page copying, duplicate handling, and CSV output**

Add these helpers:

```python
MANIFEST_FIELDS = (
    "page_number",
    "status",
    "tracking_number",
    "recognition_method",
    "output_path",
    "reason",
)


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
```

Replace the batch loop with logic equivalent to:

```python
seen: set[str] = set()
results: list[PageResult] = []
page_files: list[tuple[str, bytes]] = []

for index, page in enumerate(doc):
    page_number = index + 1
    status, number, method, reason = _recognize_page(page, ocr)
    if status == "success" and number in seen:
        status = "duplicate"
        reason = "单号与前面的成功页面重复"

    if status == "success":
        seen.add(number)
        output_path = f"{number}.pdf"
    elif status == "duplicate":
        output_path = f"识别失败/重复单号-第{page_number:03d}页.pdf"
    else:
        output_path = f"识别失败/第{page_number:03d}页.pdf"

    result = PageResult(
        page_number=page_number,
        status=status,
        tracking_number=number,
        recognition_method=method,
        output_path=output_path,
        reason=reason,
    )
    results.append(result)
    page_files.append((output_path, _single_page_pdf(doc, index)))

zip_buffer = io.BytesIO()
with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
    for path, data in page_files:
        archive.writestr(path, data)
    archive.writestr("处理结果.csv", _manifest_bytes(results))

return SplitResult(doc.page_count, tuple(results), zip_buffer.getvalue())
```

Catch page-specific recognition exceptions inside the loop, convert them to `unrecognized` with the generic reason `该页处理失败`, and continue. Do not include exception text when it could contain OCR or PDF content. PDF-level open, encryption, and zero-page errors must still raise `LabelPdfError` before the loop.

- [ ] **Step 4: Run the complete splitter test suite**

Run:

```bash
uv run pytest tests/test_ecms_label_splitter.py -v
```

Expected: all splitter tests pass; the printed count will be recorded in the implementation report.

- [ ] **Step 5: Commit lossless ZIP generation**

```bash
git add shared/ecms_label_splitter.py tests/test_ecms_label_splitter.py
git commit -m "✨ feat: package split ECMS labels for download"
```

### Task 5: Add the Streamlit tool page and navigation

**Files:**
- Create: `pages/42_✂️_面单合集拆分.py`
- Modify: `shared/i18n.py:692-699`
- Modify: `shared/i18n.py:812-830`
- Modify: `tests/test_ecms_label_splitter.py`

- [ ] **Step 1: Write failing static navigation and privacy tests**

Append:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = ROOT / "pages" / "42_✂️_面单合集拆分.py"
I18N_PATH = ROOT / "shared" / "i18n.py"


def test_page_is_registered_in_tools_navigation():
    nav = I18N_PATH.read_text(encoding="utf-8")
    assert '("pages/42_✂️_面单合集拆分.py", "✂️ 面单合集拆分")' in nav


def test_page_keeps_processing_local_and_in_memory():
    source = PAGE_PATH.read_text(encoding="utf-8")
    assert "split_label_pdf(uploaded.getvalue())" in source
    assert "st.download_button" in source
    assert "requests." not in source
    assert ".write_bytes(" not in source
    assert "open(" not in source
```

- [ ] **Step 2: Run the static tests to verify they fail**

Run:

```bash
uv run pytest tests/test_ecms_label_splitter.py::test_page_is_registered_in_tools_navigation tests/test_ecms_label_splitter.py::test_page_keeps_processing_local_and_in_memory -v
```

Expected: FAIL because the page and navigation entry do not exist.

- [ ] **Step 3: Create the Streamlit page**

Create `pages/42_✂️_面单合集拆分.py` with this flow:

```python
"""模块 #42 ECMS 面单合集拆分工具。"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

st.set_page_config(page_title="面单合集拆分", page_icon="✂️", layout="wide")

from shared.auth import require_admin  # noqa: E402
from shared.ecms_label_splitter import LabelPdfError, split_label_pdf  # noqa: E402
from shared.i18n import lang_selector, t  # noqa: E402
from shared.theme import inject_theme  # noqa: E402

require_admin()
inject_theme()
lang_selector()

st.title(t("✂️ 面单合集拆分"))
st.caption(t("上传一页一张的 ECMS 面单 PDF，按每页 13 位单号拆分并打包下载"))
st.info(t("面单只在 CMS 内存中处理，不上传外部服务，也不写入数据库或服务器文件。"))

uploaded = st.file_uploader(
    t("选择 ECMS 面单合集 PDF"),
    type=["pdf"],
    accept_multiple_files=False,
    key="ecms_label_pdf",
)

if uploaded is not None:
    st.caption(f"{uploaded.name} · {len(uploaded.getvalue()):,} bytes")

if st.button(
    t("开始拆分"),
    type="primary",
    disabled=uploaded is None,
    use_container_width=True,
):
    try:
        with st.spinner(t("正在逐页识别并生成 ZIP…")):
            st.session_state["ecms_label_split_result"] = split_label_pdf(uploaded.getvalue())
            st.session_state["ecms_label_split_source"] = uploaded.name
    except LabelPdfError as exc:
        st.session_state.pop("ecms_label_split_result", None)
        st.error(str(exc))
    except Exception:
        st.session_state.pop("ecms_label_split_result", None)
        st.error(t("处理失败，请确认 PDF 未损坏后重试。"))

result = st.session_state.get("ecms_label_split_result")
source_name = st.session_state.get("ecms_label_split_source")
if result is not None and uploaded is not None and source_name == uploaded.name:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(t("总页数"), result.total_pages)
    c2.metric(t("成功"), result.success_count)
    c3.metric(t("识别失败"), result.failed_count)
    c4.metric(t("重复单号"), result.duplicate_count)

    exceptions = [row for row in result.results if row.status != "success"]
    if exceptions:
        st.warning(t("部分页面需要人工检查；异常页面已保留在 ZIP 的“识别失败”目录。"))
        st.dataframe(
            pd.DataFrame(
                {
                    t("页码"): [row.page_number for row in exceptions],
                    t("状态"): [row.status for row in exceptions],
                    t("单号"): [row.tracking_number for row in exceptions],
                    t("原因"): [row.reason for row in exceptions],
                }
            ),
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.success(t("全部页面识别成功。"))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    st.download_button(
        t("📦 整批下载 ZIP"),
        data=result.zip_bytes,
        file_name=f"面单拆分-{stamp}.zip",
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )
```

Do not display OCR text, customer fields, or page previews.

- [ ] **Step 4: Register navigation and Japanese translations**

Add this entry to the `🛠️ 工具` list in `shared/i18n.py`:

```python
        ("pages/42_✂️_面单合集拆分.py", "✂️ 面单合集拆分"),
```

Add these Chinese-key/Japanese-value pairs to the translation list:

```python
    ("✂️ 面单合集拆分", "✂️ 送り状一括分割"),
    ("面单合集拆分", "送り状一括分割"),
    ("上传一页一张的 ECMS 面单 PDF，按每页 13 位单号拆分并打包下载",
     "1ページ1枚のECMS送り状PDFを、各ページの13桁番号で分割してZIPダウンロード"),
    ("面单只在 CMS 内存中处理，不上传外部服务，也不写入数据库或服务器文件。",
     "送り状はCMSメモリ内のみで処理し、外部サービス、DB、サーバーファイルへ保存しません。"),
    ("选择 ECMS 面单合集 PDF", "ECMS送り状一括PDFを選択"),
    ("开始拆分", "分割開始"),
    ("正在逐页识别并生成 ZIP…", "ページごとに認識してZIPを生成しています…"),
    ("处理失败，请确认 PDF 未损坏后重试。",
     "処理に失敗しました。PDFが破損していないことを確認して再試行してください。"),
    ("总页数", "総ページ数"),
    ("识别失败", "認識失敗"),
    ("重复单号", "重複番号"),
    ("部分页面需要人工检查；异常页面已保留在 ZIP 的“识别失败”目录。",
     "一部ページは手動確認が必要です。異常ページはZIPの「認識失敗」フォルダに保存しました。"),
    ("页码", "ページ"),
    ("单号", "送り状番号"),
    ("原因", "理由"),
    ("全部页面识别成功。", "全ページの認識に成功しました。"),
    ("📦 整批下载 ZIP", "📦 ZIPを一括ダウンロード"),
```

- [ ] **Step 5: Run static, syntax, and focused tests**

Run:

```bash
uv run python -c "import ast; ast.parse(open('pages/42_✂️_面单合集拆分.py', encoding='utf-8').read())"
uv run pytest tests/test_ecms_label_splitter.py -v
```

Expected: Python 3.11 syntax check exits 0 and every splitter test passes.

- [ ] **Step 6: Commit the CMS page and navigation**

```bash
git add pages/42_✂️_面单合集拆分.py shared/i18n.py tests/test_ecms_label_splitter.py
git commit -m "🚸 feat: add ECMS label splitter tool page"
```

### Task 6: Run regression and actual-file acceptance

**Files:**
- Verify only unless a defect is found in files owned by Tasks 1-5.

- [ ] **Step 1: Run formatting and placeholder checks**

Run:

```bash
git diff --check HEAD~4..HEAD
rg -n "pass$|NotImplemented" shared/ecms_label_splitter.py pages/42_✂️_面单合集拆分.py tests/test_ecms_label_splitter.py
```

Expected: `git diff --check` exits 0 and `rg` finds no implementation placeholders.

- [ ] **Step 2: Run the full CMS test suite**

Run:

```bash
uv run pytest -q
```

Expected: exit code 0. Record exact `passed`, `failed`, and `skipped` counts; do not summarize partial success as fully green.

- [ ] **Step 3: Start CMS locally and smoke-test the page**

Run:

```bash
uv run streamlit run cms.py
```

Verify in the browser:

- “✂️ 面单合集拆分” appears under “🛠️ 工具”.
- A synthetic PDF uploads and produces a ZIP.
- Metrics equal the manifest counts.
- ZIP downloads and opens.
- Each PDF in the ZIP has one page at the original size.
- The page does not display names, telephone numbers, addresses, or OCR text.

- [ ] **Step 4: Validate the real 0909 PDF when the user provides it**

Process the original PDF through the local page. Verify:

- Input page count equals manifest row count.
- The sample label becomes `6102821704186.pdf`.
- A representative sample of output filenames matches the visible 13-digit number.
- Page size, direction, sharpness, and print scale match the source.
- ZIP opens without errors.

If the original PDF remains unavailable, report `代码测试完成，真实文件待验`. Do not claim production readiness from the screenshot or synthetic fixtures.

- [ ] **Step 5: Report local completion and stop before external changes**

Report:

- commits created;
- exact test counts;
- actual-PDF acceptance status;
- changed files;
- known failure counts or skipped checks;
- `push=0`, `deploy=0`, and `online_verify=0`.

Do not push GitHub, connect to Motokawa, rebuild the production container, restart CMS, or change online state without a new explicit authorization.
