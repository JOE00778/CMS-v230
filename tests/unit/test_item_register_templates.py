"""商品登録 ZIP（NST csv / JD xlsx）のテンプレ生成の回帰テスト。

現場から同じ不具合が二人ずつ挙がっていた（2026-08-18 修正）:
  #27 隋艶偉さん / #30 川崎さん … NST CSV から「サポート提供」列が消えて取込エラー
  #28 隋艶偉さん / #31 川崎さん … JD シート 1 列目（貨主ID）に値が入って取込エラー

どちらも「テンプレ原文の列名に前後空白がある」「既定値へフォールバックする」という
静かな挙動が原因で、動かしてみるまで気付けなかった。ここで固定する。
"""
import csv
import io

from data_warehouse.templates import jd_bm_item_master as JBM
from data_warehouse.templates import nst_item_master as TPL


def _header_and_rows(csv_bytes: bytes) -> tuple[list[str], list[list[str]]]:
    text = csv_bytes.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], rows[1:]


# ───────────────────── #27 / #30 サポート提供 ─────────────────────

def test_column_lookup_ignores_surrounding_spaces():
    """page15 は取込時に header を strip する。空白を落とした名前でも照合できること。"""
    assert "サポート提供" in TPL.VALID_COLUMNS
    assert "直送アイテム" in TPL.VALID_COLUMNS


def test_support_column_always_emitted_with_default_t():
    """入力に「サポート提供」が無くても、CSV には必ず出て既定値 T が入る（#30）。"""
    header, rows = _header_and_rows(
        TPL.build_nst_master_csv([{"型番": "A-1", "アイテム名": "テスト品"}],
                                 ["型番", "アイテム名"])
    )
    assert "サポート提供 " in header, header      # 出力はテンプレ原文（末尾空白つき）
    assert rows[0][header.index("サポート提供 ")] == "T"


def test_support_column_keeps_explicit_value():
    """明示的に値が入っていれば既定値で上書きしない。"""
    header, rows = _header_and_rows(
        TPL.build_nst_master_csv([{"型番": "A-1", "サポート提供": "F"}],
                                 ["型番", "サポート提供"])
    )
    assert rows[0][header.index("サポート提供 ")] == "F"


def test_stripped_column_name_is_accepted_and_written_back_as_template_original():
    """strip 済みの名前で渡しても弾かれず、ヘッダはテンプレ原文で出る。"""
    header, rows = _header_and_rows(
        TPL.build_nst_master_csv([{"型番": "A-1", "直送アイテム": "T"}],
                                 ["型番", "直送アイテム"])
    )
    assert " 直送アイテム　" in header, header
    assert rows[0][header.index(" 直送アイテム　")] == "T"


def test_duplicate_after_normalization_is_not_emitted_twice():
    """原文名と strip 名を両方渡しても列は 1 本だけ。"""
    header, _ = _header_and_rows(
        TPL.build_nst_master_csv([{"型番": "A-1"}], ["型番", "サポート提供", "サポート提供 "])
    )
    assert header.count("サポート提供 ") == 1


def test_unknown_column_still_rejected():
    """正規化してもテンプレに無い列は従来どおり弾く（黙って通さない）。"""
    try:
        TPL.build_nst_master_csv([{"型番": "A-1"}], ["存在しない列"])
    except ValueError as e:
        assert "存在しない列" in str(e)
    else:
        raise AssertionError("非テンプレ列が素通りした")


# ───────────────────── #28 / #31 JD 貨主ID ─────────────────────

def test_jd_owner_id_defaults_to_blank():
    """既定は空欄。単一貨主で 1 列目に値が入ると JD 取込がエラーになる（#31）。"""
    row = JBM.nst_to_jd_row({"JANコード": "4901234567890", "アイテム名": "テスト品"})
    assert row[0] == ""


def test_jd_owner_id_written_when_explicitly_given():
    """複数貨主になった時のために、明示指定は従来どおり 1 列目へ入る。"""
    row = JBM.nst_to_jd_row({"JANコード": "4901234567890", "アイテム名": "テスト品"},
                            jd_customer_code="KH20000009340")
    assert row[0] == "KH20000009340"
