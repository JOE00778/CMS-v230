from __future__ import annotations

import csv
import io
import zipfile

import pymupdf as fitz
import pytest

from shared import ecms_label_splitter as splitter


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


def make_ambiguous_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=512, height=747)
    page.insert_text((360, 275), "6102821704186")
    page.insert_text((360, 295), "6102821704187")
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
        (
            "6102821704186 6102821704187",
            ("6102821704186", "6102821704187"),
        ),
        ("61O28217O4186", ()),
    ],
)
def test_find_tracking_candidates_is_strict(text, expected):
    assert splitter.find_tracking_candidates(text) == expected


def test_fixed_region_ignores_13_digit_number_elsewhere():
    result = splitter.split_label_pdf(
        make_pdf(["6102821704186"], outside_number="9999999999999"),
        ocr=False,
    )
    assert result.results[0].tracking_number == "6102821704186"
    assert result.results[0].recognition_method == "text"


def test_rejects_invalid_pdf():
    with pytest.raises(splitter.LabelPdfError, match="无法读取 PDF"):
        splitter.split_label_pdf(b"not a pdf", ocr=False)


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
    result = splitter.split_label_pdf(make_pdf([None]), ocr=ocr)
    row = result.results[0]
    assert row.status == "success"
    assert row.tracking_number == "6102821704186"
    assert row.recognition_method == "ocr"
    assert ocr.calls == 1


def test_text_success_does_not_call_ocr():
    ocr = FakeOCR(["9999999999999"])
    result = splitter.split_label_pdf(make_pdf(["6102821704186"]), ocr=ocr)
    assert result.results[0].tracking_number == "6102821704186"
    assert ocr.calls == 0


def test_ocr_does_not_guess_letters_as_digits():
    result = splitter.split_label_pdf(
        make_pdf([None]),
        ocr=FakeOCR(["61O28217O4186"]),
    )
    assert result.results[0].status == "unrecognized"


def test_multiple_ocr_candidates_are_ambiguous():
    ocr = FakeOCR(["6102821704186", "6102821704187"])
    result = splitter.split_label_pdf(make_pdf([None]), ocr=ocr)
    assert result.results[0].status == "ambiguous"


def test_missing_local_ocr_is_reported_without_page_content(monkeypatch):
    monkeypatch.setattr(splitter, "_load_local_ocr", lambda: None, raising=False)
    result = splitter.split_label_pdf(make_pdf([None]), ocr=None)
    row = result.results[0]
    assert row.status == "unrecognized"
    assert row.reason == "本地 OCR 不可用"


def read_zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def test_builds_one_page_pdfs_and_manifest_without_losing_pages():
    source = make_pdf(["6102821704186", "6102821704187", None])
    result = splitter.split_label_pdf(source, ocr=False)
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
        assert {row["status"] for row in rows} == {"success", "unrecognized"}


def test_duplicate_tracking_number_never_overwrites_first_page():
    result = splitter.split_label_pdf(
        make_pdf(["6102821704186", "6102821704186"]),
        ocr=False,
    )
    assert result.success_count == 1
    assert result.duplicate_count == 1
    with read_zip(result.zip_bytes) as archive:
        assert "6102821704186.pdf" in archive.namelist()
        assert "识别失败/重复单号-第002页.pdf" in archive.namelist()


def test_result_counts_cover_every_input_page():
    result = splitter.split_label_pdf(
        make_pdf(["6102821704186", None, "6102821704186"]),
        ocr=False,
    )
    assert (
        result.success_count + result.failed_count + result.duplicate_count
        == result.total_pages
    )


def test_ambiguous_page_is_preserved_in_failure_folder():
    result = splitter.split_label_pdf(
        make_ambiguous_pdf(),
        ocr=False,
    )
    assert result.results[0].status == "ambiguous"
    with read_zip(result.zip_bytes) as archive:
        assert "识别失败/第001页.pdf" in archive.namelist()
