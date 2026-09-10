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
    for fn in ("delete_drafts", "set_sku_image", "set_spu_image", "set_sku_option", "missing_options",
               "list_shops", "list_shop_drafts", "get_shop_draft",
               "set_shop_status", "update_shop_text", "build_shop_images", "ImageProcessorClient"):
        assert re.search(rf"\b{fn}\b", SRC), fn
    assert "SHOPEE_LISTING_DIR" in COMPOSE and "/opt/shopee-listing:ro" in COMPOSE
    assert "GROQ_API_KEY" in COMPOSE


def test_tab1_one_line_one_spu_input():
    """Boss 2026-09-10：一行 = 一个 SPU，行内多个 JAN = 该 SPU 的多个 SKU；SPU 列可改即改分组。"""
    assert "st.file_uploader" not in SRC.split("with tab_review:")[0].split("with tab_auto:")[1]
    assert "_parse_jan_lines(" in SRC and "load_skus(conn, jans)" in SRC
    assert '"gen_names"' in SRC and "_default_spu_key(" in SRC
    assert "🔗" not in SRC and "gen_groups" not in SRC        # 勾选合并の 2 手順は廃止
    # 仍然走 run_pipeline --csv：把分组写成 SPU/SKU 两列临时 CSV，不另写一套入口
    assert '[{"SPU": k, "SKU": j} for k, js in spus.items() for j in js]' in SRC


def test_sku_option_name_editable_and_gates_approval():
    """Boss 2026-09-10「SKU的怎么做？」：多 SKU は規格名必須。空欄なら承認ボタンを塞ぐ。"""
    assert "set_sku_option(" in SRC and "missing_options(conn, sel)" in SRC
    assert 'label("option_name")' in SRC and '"option_name":' in COLS
    assert "or bool(missing_options(conn, sel))" in SRC      # 承認ボタンの disabled 条件
    assert "这些 JAN 还没有规格名，送不了上架后台：" in SRC


def test_tab1_runs_pipeline_in_background_and_supports_no_images():
    assert "def _spawn(" in SRC and "subprocess.Popen" in SRC
    assert '_spawn("run_pipeline.py", args, safe_batch)' in SRC
    assert '"--batch-id"' in SRC and '"--no-images"' in SRC and '"--mock-llm"' in SRC
    # DB URL はログに出さない
    assert "postgresql://***" in SRC


def test_run_status_auto_refreshes_and_has_terminal_states():
    """Boss 2026-09-10「已启动加一个状态变化，已完成，完成后自动变已完成」。"""
    assert "@st.fragment(run_every=" in SRC and "def _run_panel(" in SRC
    assert "def _job_state(" in SRC and 'startswith("ok=")' in SRC
    for st_key in ("running", "done", "failed", "stalled"):
        assert f'"{st_key}"' in SRC, st_key
    assert '_JOB_LABEL = {"running": "运行中", "done": "已完成", "failed": "失败"' in SRC
    assert "_STALE_SEC" in SRC              # 結果行が無いまま止まったら「中断」


def test_interrupted_run_can_be_resumed_per_spu():
    """2026-09-10 実障害：デプロイの docker restart が生成の子プロセスを殺し、pt-BR 群だけ書けて止まった。
    → 欠けた店舗版だけ localize.py で追いかける導線と、再起動注意の掲示を固定する。"""
    assert '_spawn("localize.py", ["--spu", sel, "--shops"' in SRC
    assert "▶ 补齐店铺版（{n} 家）" in SRC and "miss_shops" in SRC
    assert "生成期间不要重启 CMS 容器" in SRC


def test_review_writes_go_through_write_connection():
    assert "get_connection" in SRC, "写操作要拿写连接，不能用 get_readonly_connection"
    # 読みは只読接続 conn、書きは直前に get_connection()
    assert SRC.count("wc = get_connection()") >= 6


def test_list_layer_is_browse_only_and_no_reject():
    """Boss 2026-09-10「这一步的审批都删除掉，直接到单个SPU确认环节」→ 一覧に操作は無い。"""
    list_block = SRC.split("# ---------------- 详情层 ----------------")[0].split("with tab_review:")[1]
    for gone in ("CheckboxColumn", "bulk_set_status", "勾选后批量操作", "批准勾选", "删除勾选", "保存标题修改"):
        assert gone not in list_block, gone
    assert "st.dataframe(" in list_block and "ImageColumn" in list_block
    # Boss 2026-09-10「不需要已拒绝」：UI 无拒绝；不要的直接删
    assert "拒绝" not in SRC and '"rejected"' not in SRC.split("_STATUS_LABELS")[1].split("\n")[0]
    assert '["draft", "approved", "published", _STATUS_ALL]' in SRC
    # 削除は詳細だけ · 二次確認あり · published は消さない（delete_drafts 側で保証）
    assert "delete_drafts(wc, [sel])" in SRC and SRC.count('tt("确认删除")') == 1


def test_approve_is_worded_as_listing_backend_draft():
    """Boss 2026-09-10「最后批准改为上架后台（草稿）」。DB の値は approved のまま。"""
    assert "📤 上架后台（草稿）· 含勾选店铺" in SRC
    assert '"approved": "上架后台（草稿）"' in SRC
    assert 'set_status(wc, sel, "approved"' in SRC
    # ボタン文言に「批准」は残さない（Boss 用語＝上架后台（草稿））
    assert not re.findall(r'button\(tt\("[^"]*批准', SRC)
    assert "已批准" not in SRC


def test_mock_draft_cannot_be_approved():
    assert 'startswith("<MOCK>")' in SRC
    assert "disabled=is_mock" in SRC


def test_image_two_modes_and_templated_checkbox():
    """Boss 2026-09-10：一键自动出图 / 上传主图；上传原图 → ☑ 也走抠图套模板。"""
    assert "先不出图（之后在详情页手传）" in SRC and "自动出图" in SRC
    assert "原图 → 也走抠图套模板" in SRC and "manual_image(" in SRC and "templated=templated" in SRC
    # 换图后拼图重做
    assert "compose_spu(sel, good, overwrite=True)" in SRC


def test_shop_selection_is_a_country_column_checkbox_grid():
    """Boss 2026-09-10「用打勾选项形式…按照国家，按列分」：生成と上架の両方でグリッド。"""
    assert "def _shop_grid(" in SRC and "st.checkbox(k" in SRC
    assert '_shop_grid(gen_shops, "gen_shop")' in SRC          # 生成タブ
    assert '_shop_grid(shops, f"rv_pub_{sel}"' in SRC          # 待确认タブ
    assert "st.multiselect(" not in SRC                        # 旧 multiselect は廃止
    assert '"--shops"' in SRC and "localize_spu" not in SRC
    # 勾上 = approved（上架）／未勾 = rejected（剔掉）を 1 回で書く
    assert 'set_shop_status(wc, sel, k, "approved" if want else "rejected"' in SRC
    assert "build_shop_images(sel," in SRC


def test_detail_has_per_shop_version_viewer_and_no_shop_table():
    """Boss 2026-09-10「待确认中的店铺版整个删掉」+「旁边加一个店铺选择」。"""
    assert 'tt("看哪个版本（国家 · 店铺）")' in SRC and '_MASTER = "__master__"' in SRC
    for gone in ("🌏 店铺版", "lc_editor_", "打开一个店铺版", "shop_counts"):
        assert gone not in SRC, gone
    # 店舗版を選んだら保存はそのショップ行へ
    assert "update_shop_text(wc, sel, view," in SRC


def test_batch_id_is_date_plus_sequence():
    """Boss 2026-09-10「批次号简单些，日期加01 02 这种」。"""
    assert "def _next_batch_id(" in SRC and '%Y%m%d' in SRC and '{today}-{n:02d}' in SRC
    assert "_next_batch_id(conn)" in SRC and "ops-" not in SRC


def test_template_tab_uploads_via_sidecar_not_local_fs():
    """CMS は /data/whitebg を只読 mount。テンプレートも画像も sidecar 経由でしか書かない。"""
    assert "put_template(" in SRC and "delete_template(" in SRC and "list_templates()" in SRC
    assert "REFERENCE_DIR" not in SRC and ".write_bytes(" not in SRC
    assert "/data/whitebg:ro" in COMPOSE


# ---- 状态与文案按 UI 语言显示（Boss 2026-09-09「匹配为中文和日文UI」）----
def test_status_values_never_shown_raw():
    assert "_STATUS_LABELS" in SRC and "format_func=_st_label" in SRC
    assert '"status": _st_label(d["status"])' in SRC


def test_new_strings_have_japanese_and_english():
    page_en = re.search(r"_PAGE_STRINGS_EN: Dict\[str, str\] = \{(.*?)\n\}\n", SRC, re.S).group(1)
    for zh in ("🤖 生成母版", "🎨 主图模板", "上架后台（草稿）",
               "📤 上架后台（草稿）· 含勾选店铺", "出到哪些店铺（按国家分列）", "母版（英文）",
               "看哪个版本（国家 · 店铺）", "勾上 {n} 家", "🗑 删除", "确认删除",
               "原图 → 也走抠图套模板", "⬆️ 上传 / 替换模板", "⚠ 默认",
               "批次", "自动出图", "先不出图（之后在详情页手传）", "运行状态", "运行中", "已完成",
               "JAN（一行一个 SPU；同一行多个 JAN = 一个 SPU 的多个 SKU）", "💾 保存规格名", "多 SKU：",
               "这些 JAN 还没有规格名，送不了上架后台："):
        assert f'"{zh}":' in I18N, f"JA 缺 {zh}"
        assert f'"{zh}":' in page_en, f"EN 缺 {zh}"
    for zh, ja in [("草稿待确认", "確認待ち"), ("已发布", "公開済み"), ("✅ 待确认", "✅ 確認待ち"),
                   ("上架后台（草稿）", "出品バックエンド（下書き）")]:
        assert f'"{zh}": "{ja}"' in I18N, zh


def test_editor_columns_registered_in_i18n_columns():
    for col in ("thumb", "title_len", "batch_id", "shops_done", "shop_key", "shop_name", "lang", "template",
                "template_updated", "image_source", "ph_price", "spu_key", "status", "title", "sku_count", "image", "country_code",
                "sellable", "maker", "cost_jpy", "option_name"):
        assert f'"{col}":' in COLS, f"i18n_columns 缺列键 {col}"
    # 列表层/店铺层的 column_config 都用 label()，不写死中文
    assert SRC.count('label("') >= 20
