"""page41 の商品主档インポートが weight_kg を実際に読んでいるかの回帰テスト。

2026-09-04 に踏んだ穴: shared/coupang_to_ecms_xlsx.py と shared/coupang_store.py は
`weight_kg` を正しく扱うのに、page41 の Excel アップロード欄がその列を**一度も読んで
いなかった**（`recs.append()` に `weight_kg` キー自体が無かった）。commit メッセージには
「产品重量から取る」と書いていたが、実装していなかった——コミットメッセージを実装の
証拠にしない。

ユニットテストで直接検証できないので（Streamlit の file_uploader を経由する UI コード）、
ソースの静的チェックで「weight_kg を _c() で読んで recs に積んでいる」ことを保証する。
"""
from __future__ import annotations

import pathlib

PAGE = pathlib.Path(__file__).resolve().parents[1] / "pages" / "41_📮_ECMS发货.py"
SRC = PAGE.read_text(encoding="utf-8")


def test_商品主档インポートが産品重量列を読む():
    assert '_c(row, "产品重量"' in SRC, (
        "商品主档アップロードが「产品重量」列を読んでいない —— "
        "毛重が永久に空になる（2026-09-04 の再発）")


def test_recsにweight_kgが積まれる():
    start = SRC.index('def _c(row, *names):', SRC.index('---- 商品主档 ----'))
    block = SRC[start:start + 1800]
    assert '"weight_kg": w' in block, (
        "recs.append() に weight_kg キーが無い —— upsert_products に重量が渡らない")
