"""page21 の静的回帰（UI 経由コードはテストで到達しないので文面で固定する）。

2026-09-07 の教訓：commit メッセージに書いたのに実装していなかった／市場リストが
2026-05-17 の Boss 校正（ID→BR）を 4 ヶ月追従していなかった。
2026-09-10 v3：N8N 版 Tab1 を新パイプライン入口へ、一覧一括承認、画像差し替え、店舗ローカライズ、
テンプレート管理。ロジックは workflow-automation/shopee-listing の draft_store / localize / image_pipeline に
あり（そちらは pytest で本物のテスト）。ここは「page が本当にそれを呼んでいるか」だけ固定する。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "21_🚀_Shopee上架.py"
SRC = PAGE.read_text(encoding="utf-8")
I18N = (ROOT / "shared" / "i18n.py").read_text(encoding="utf-8")
COLS = (ROOT / "shared" / "i18n_columns.py").read_text(encoding="utf-8")
COMPOSE = (ROOT / "deploy" / "windows" / "docker-compose.yml").read_text(encoding="utf-8")


def test_page_parses():
    ast.parse(SRC)


def test_market_order_matches_actual_7_countries():
    """実店は 7 国 = PH/MY/SG/TH/VN/TW/BR（ID なし・BR あり）。価格表の列順に使う。"""
    m = re.search(r'_MARKETS_ORDER = \[([^\]]+)\]', SRC)
    assert m
    assert re.findall(r'"([A-Z]{2})"', m.group(1)) == ["PH", "MY", "SG", "TH", "VN", "TW", "BR"]


def test_four_tabs_and_n8n_trigger_gone():
    assert 'tab_auto, tab_review, tab_refs, tab_history = st.tabs(' in SRC
    for name in ('"🤖 生成母版"', '"✅ 待确认"', '"🎨 主图模板"', '"📜 历史运行"'):
        assert name in SRC, name
    # 旧 N8N 触发（B1 の /api/sku/master は 2026-08-21 削除・リンク切れ）は page から消す
    assert "trigger_workflow" not in SRC and "shopee-mass-upload" not in SRC


def test_pipeline_code_imported_not_copied():
    """ロジックは workflow-automation 側。page は import するだけ（二重実装禁止）。"""
    assert "SHOPEE_LISTING_DIR" in SRC and "/opt/shopee-listing" in SRC
    for fn in ("bulk_set_status", "set_sku_image", "set_spu_image", "list_shops", "list_shop_drafts",
               "set_shop_status", "update_shop_text", "lang_for_shop", "build_shop_images", "ImageProcessorClient"):
        assert re.search(rf"\b{fn}\b", SRC), fn
    assert "SHOPEE_LISTING_DIR" in COMPOSE and "/opt/shopee-listing:ro" in COMPOSE
    assert "GROQ_API_KEY" in COMPOSE


def test_tab1_jan_input_and_spu_merge_no_csv_upload():
    """Boss 2026-09-10：CSV 上传改成直接输 JAN，勾选合并成 SPU。"""
    assert "st.file_uploader" not in SRC.split("with tab_review:")[0].split("with tab_auto:")[1]
    assert "_parse_jans(" in SRC and "load_skus(conn, jans)" in SRC
    assert "🔗 勾选的合并成一个 SPU" in SRC and "↩ 全部拆开" in SRC and '"gen_groups"' in SRC
    # 仍然走 run_pipeline --csv：把分组写成 SPU/SKU 两列临时 CSV，不另写一套入口
    assert '[{"SPU": k, "SKU": j} for k, js in spus.items() for j in js]' in SRC


def test_tab1_runs_pipeline_in_background_and_supports_no_images():
    assert "run_pipeline.py" in SRC and "subprocess.Popen" in SRC
    assert '"--batch-id"' in SRC and '"--no-images"' in SRC and '"--mock-llm"' in SRC
    # DB URL はログに出さない
    assert "postgresql://***" in SRC


def test_review_writes_go_through_write_connection():
    assert "get_connection" in SRC, "写操作要拿写连接，不能用 get_readonly_connection"
    # 読みは只読接続 conn、書きは直前に get_connection()
    assert SRC.count("wc = get_connection()") >= 8


def test_batch_actions_and_isolation():
    assert "st.data_editor(" in SRC and "CheckboxColumn" in SRC and "ImageColumn" in SRC
    assert 'bulk_set_status(wc, picked, "approved"' in SRC and 'bulk_set_status(wc, picked, "rejected"' in SRC
    assert "批量批准 ok={ok} fail={fail}" in SRC   # 逐项报数


def test_mock_draft_cannot_be_approved():
    assert 'startswith("<MOCK>")' in SRC
    assert "disabled=is_mock" in SRC


def test_image_two_modes_and_templated_checkbox():
    """Boss 2026-09-10：一键自动出图 / 上传主图；上传原图 → ☑ 也走抠图套模板。"""
    assert "先不出图（之后在详情页手传）" in SRC and "自动出图" in SRC
    assert "原图 → 也走抠图套模板" in SRC and "manual_image(" in SRC and "templated=templated" in SRC
    # 换图后拼图重做
    assert "compose_spu(sel, good, overwrite=True)" in SRC


def test_one_step_flow_shops_chosen_at_generation_and_single_approval():
    """Boss 2026-09-10「太繁琐」：生成时选店一口气出店铺版；审核一次批准级联。页面不再有单独的本地化按钮。"""
    assert '"--shops"' in SRC and 'key="gen_shops"' in SRC
    assert "localize_spu" not in SRC and "🌏 生成本地化版" not in SRC and "母版批准后才能出店铺版" not in SRC
    assert "✅ 批准（含全部店铺版）" in SRC
    # 店铺区只做剔店 / 恢复 / 改文案 / 重出图
    assert 'set_shop_status(wc, sel, k, "rejected"' in SRC and 'set_shop_status(wc, sel, k, "approved"' in SRC
    assert "build_shop_images(sel," in SRC


def test_template_tab_uploads_via_sidecar_not_local_fs():
    """CMS は /data/whitebg を只読 mount。テンプレートも画像も sidecar 経由でしか書かない。"""
    assert "put_template(" in SRC and "delete_template(" in SRC and "list_templates()" in SRC
    assert "REFERENCE_DIR" not in SRC and ".write_bytes(" not in SRC
    assert "/data/whitebg:ro" in COMPOSE


# ---- 状态与文案按 UI 语言显示（Boss 2026-09-09「匹配为中文和日文UI」）----
def test_status_values_never_shown_raw():
    assert "_STATUS_LABELS" in SRC and "format_func=_st_label" in SRC
    assert '"status": _st_label(d["status"])' in SRC and '"status": _st_label(r["status"])' in SRC


def test_new_strings_have_japanese_and_english():
    page_en = re.search(r"_PAGE_STRINGS_EN: Dict\[str, str\] = \{(.*?)\n\}\n", SRC, re.S).group(1)
    for zh in ("🤖 生成母版", "🎨 主图模板", "✅ 批准勾选", "❌ 拒绝勾选", "💾 保存标题修改", "🌏 店铺版", "店铺（默认全部）",
               "✅ 批准（含全部店铺版）", "❌ 剔掉勾选的店", "原图 → 也走抠图套模板", "⬆️ 上传 / 替换模板", "⚠ 默认",
               "批次", "自动出图", "先不出图（之后在详情页手传）", "JAN（一行一个，或空格/逗号分隔）", "🔗 勾选的合并成一个 SPU", "合并后的 SPU 名"):
        assert f'"{zh}":' in I18N, f"JA 缺 {zh}"
        assert f'"{zh}":' in page_en, f"EN 缺 {zh}"
    for zh, ja in [("草稿待确认", "確認待ち"), ("已批准", "承認済み"), ("已拒绝", "却下"), ("已发布", "公開済み"), ("✅ 待确认", "✅ 確認待ち")]:
        assert f'"{zh}": "{ja}"' in I18N, zh


def test_editor_columns_registered_in_i18n_columns():
    for col in ("select", "thumb", "title_len", "batch_id", "shops_done", "shop_key", "shop_name", "lang", "template",
                "template_updated", "image_source", "ph_price", "spu_key", "status", "title", "sku_count", "image", "country_code",
                "sellable", "maker", "cost_jpy"):
        assert f'"{col}":' in COLS, f"i18n_columns 缺列键 {col}"
    # 列表层/店铺层的 column_config 都用 label()，不写死中文
    assert SRC.count('label("') >= 20
