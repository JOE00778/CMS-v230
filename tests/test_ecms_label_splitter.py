from __future__ import annotations

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
