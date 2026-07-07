"""模块 #33 网红工资 · PHライブ配信バイト料 自動計算.

Shopee ライブ配信の総エクスポート(全網紅混在)をアップロード → 直播名称で人ごとに振り分け →
労働時間(Σ时长) と 確定売上(Σ销售金额已确认订单) を集計 → 給与+階梯抽成 を算出。

計算ロジックは shared/influencer_wage.py（純関数·tests/test_influencer_wage.py）。
名册(人/时薪/收款人/匹配关键词)は data/files/influencer_roster.json に永続化（唯一可写挂载）。
レート等は当時状況で手动入力。Boss 2026-06-22 · フィリピンバイト料-*.xlsx「計算表」踏襲。
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from shared.i18n import lang_selector, t
from shared import influencer_wage as iw

st.set_page_config(page_title=t("网红工资"), page_icon="💸", layout="wide")
from shared.auth import require_password  # noqa: E402
require_password()
from shared.theme import inject_theme, html_table  # noqa: E402
inject_theme()
lang_selector()

st.title(t("💸 网红工资（PH直播バイト料 自動計算）"))
st.caption(t(
    "上传 Shopee 直播总导出（CSV/Excel）→ 按直播名称自动分到各网红（匹配不上的手动指派）→ "
    "算 給与(时长×时薪) + 阶梯抽成 → 出工资表。汇率等当時状況で手动。"
))

# ============================================================
# 名册（永続化：data/files/influencer_roster.json · 唯一可写挂载）
# ============================================================
_ROSTER_PATH = Path(__file__).resolve().parent.parent / "data" / "files" / "influencer_roster.json"
_DEFAULT_ROSTER = [
    {"name": "Bella", "wage": 2.0, "payee": "Anabel Magsipoc Malinog",
     "keywords": ["bella", "bell", "bel"]},
    {"name": "Charm", "wage": 1.5, "payee": "Charlime Villanueva",
     "keywords": ["charm"]},
]


def _load_roster() -> list[dict]:
    try:
        if _ROSTER_PATH.exists():
            data = json.loads(_ROSTER_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data
    except Exception:
        pass
    return [dict(r) for r in _DEFAULT_ROSTER]


def _save_roster(roster: list[dict]) -> None:
    _ROSTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ROSTER_PATH.write_text(json.dumps(roster, ensure_ascii=False, indent=2), encoding="utf-8")


roster = _load_roster()

_K_NAME, _K_WAGE = t("名前"), t("時給($/h)")
_K_PAYEE, _K_KW = t("振込先"), t("匹配关键词(逗号分隔)")
with st.expander(t("👥 网红名册（人 / 时薪 / 收款人 / 匹配关键词）· 可编辑后保存"), expanded=False):
    st.caption(t("匹配关键词：不区分大小写、按直播名称部分匹配（如 Bella 用 bella,bell,bel）。改完点保存。"))
    _redf = pd.DataFrame([{
        _K_NAME: r.get("name", ""), _K_WAGE: float(r.get("wage", 0) or 0),
        _K_PAYEE: r.get("payee", ""), _K_KW: ",".join(r.get("keywords", [])),
    } for r in roster])
    _edited = st.data_editor(_redf, num_rows="dynamic", use_container_width=True,
                             key="roster_editor")
    if st.button(t("💾 保存名册"), key="save_roster"):
        _new = []
        for _, _row in _edited.iterrows():
            _nm = str(_row[_K_NAME]).strip()
            if not _nm or _nm == "nan":
                continue
            _kws = [k.strip().lower() for k in str(_row[_K_KW]).split(",") if k.strip()]
            try:
                _wg = float(_row[_K_WAGE])
            except (ValueError, TypeError):
                _wg = 0.0
            _new.append({"name": _nm, "wage": _wg,
                         "payee": str(_row[_K_PAYEE]).strip(), "keywords": _kws})
        if _new:
            _save_roster(_new)
            roster = _new
            st.success(t("已保存名册（下次自动带出）"))
        else:
            st.warning(t("名册为空，未保存"))

# ============================================================
# 参数（当時状況で手动）
# ============================================================
st.markdown("##### " + t("⚙️ 参数（Rate 当時状況で手动）"))
_USD_JPY, _FEE = 155.0, 1.045  # 固定：USD→JPY / 含手续费倍率
_php_usd = st.number_input(t("Rate：peso/$（手动）"), 1.0, 500.0, 61.49, 0.01,
                           key="w_rate", format="%.2f")
with st.expander(t("阶梯抽成参数（默认 20万peso 阈值 · 1% / 1.5%）")):
    _tc1, _tc2, _tc3 = st.columns(3)
    _th = _tc1.number_input(t("阈值(peso)"), 0.0, 1e8, 200000.0, 10000.0, key="w_th")
    _r1 = _tc2.number_input(t("阈值以内 %"), 0.0, 100.0, 1.0, 0.1, key="w_r1") / 100.0
    _r2 = _tc3.number_input(t("阈值以上 %"), 0.0, 100.0, 1.5, 0.1, key="w_r2") / 100.0
_params = iw.WageParams(php_usd=_php_usd, usd_jpy=_USD_JPY, fee=_FEE,
                        tier_threshold=_th, tier1_rate=_r1, tier2_rate=_r2)

# ============================================================
# 上传 + 解析
# ============================================================
_NEED = ["直播名称", "时长", "销售金额(已确认订单)"]


def _norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=lambda c: str(c).strip())
    _miss = [c for c in _NEED if c not in df.columns]
    if _miss:
        raise ValueError(t("缺少必要列：") + "、".join(_miss)
                         + "｜" + t("实际列：") + "、".join(map(str, df.columns)))
    return df


def _read_upload(up) -> pd.DataFrame:
    _data = up.read()
    if up.name.lower().endswith(".csv"):
        for _enc in ("utf-8-sig", "utf-8", "cp932"):
            try:
                return _norm_cols(pd.read_csv(io.BytesIO(_data), encoding=_enc,
                                              dtype=str, keep_default_na=False))
            except UnicodeDecodeError:
                continue
        raise ValueError(t("CSV 编码解析失败（试过 utf-8/cp932）"))
    # Excel：表头可能不在首行（如 時間帯計算 sheet 表头在第3行）→ 自动找含「直播名称」的行
    _xls = pd.ExcelFile(io.BytesIO(_data))
    for _sh in _xls.sheet_names:
        _probe = pd.read_excel(_xls, sheet_name=_sh, header=None, dtype=str)
        for _hr in range(min(8, len(_probe))):
            if _probe.iloc[_hr].astype(str).str.contains("直播名称", na=False).any():
                return _norm_cols(pd.read_excel(_xls, sheet_name=_sh, header=_hr, dtype=str))
    raise ValueError(t("Excel 内に『直播名称』列が見つかりません"))


_up = st.file_uploader(t("📤 上传 Shopee 直播总导出（CSV / Excel）"),
                       type=["csv", "xlsx"], key="wage_upload")
if not _up:
    st.info(t("上传文件后，自动按直播名称分网红、汇总时长+已确认销售额、算工资。"))
    st.stop()

try:
    raw = _read_upload(_up)
except Exception as e:  # noqa: BLE001
    st.error(t("⚠️ 文件解析失败") + f"\n\n{e}")
    st.stop()

# ============================================================
# 匹配（自动）+ 解析数值
# ============================================================
_roster_kw = {r["name"]: r.get("keywords", []) for r in roster}
_wage_map = {r["name"]: float(r.get("wage", 0) or 0) for r in roster}
_payee_map = {r["name"]: r.get("payee", "") for r in roster}
_names = [r["name"] for r in roster]

raw["_who"] = raw["直播名称"].map(lambda x: iw.match_influencer(x, _roster_kw))
raw["_h"] = raw["时长"].map(iw.parse_duration_hours)
raw["_sales"] = raw["销售金额(已确认订单)"].map(iw.parse_peso)

# ============================================================
# 未匹配 → 下拉手动指派（失败要显式）
# ============================================================
_SKIP = t("（跳过·不计入）")
_unmatched = sorted(raw.loc[raw["_who"].isna(), "直播名称"].astype(str).unique().tolist())
_assign: dict[str, str] = {}
if _unmatched:
    st.markdown("##### ⚠️ " + t("未匹配直播（手动指派给网红）"))
    st.caption(t("这些直播没带可识别的网红标识。看下方每场明细判断是谁，再指派；不指派则不计入。"))
    _det_cols = [c for c in ["直播开始时间", "时长", "订单数(已确认订单)",
                             "销售金额(已确认订单)"] if c in raw.columns]
    for _i, _nm in enumerate(_unmatched):
        _sub = raw[raw["直播名称"].astype(str) == _nm]
        st.markdown(f"**{_nm}**　({len(_sub)} 场)")
        st.dataframe(_sub[_det_cols], hide_index=True, use_container_width=True)
        _sel = st.selectbox(t("↑ 指派给"), [_SKIP] + _names, key=f"assign_{_i}")
        if _sel != _SKIP:
            _assign[_nm] = _sel
    if _assign:
        raw["_who"] = [
            (_assign.get(str(_n), _w) if pd.isna(_w) else _w)
            for _n, _w in zip(raw["直播名称"], raw["_who"])
        ]

# ============================================================
# 聚合 + 计算
# ============================================================
_matched = raw.dropna(subset=["_who"])
_n_unassigned = int(raw["_who"].isna().sum())
if _matched.empty:
    st.warning(t("没有任何直播被归属到网红。请检查名册匹配关键词或手动指派。"))
    st.stop()

_rows = []
for _who, _g in _matched.groupby("_who"):
    _hours = float(_g["_h"].sum())
    _sales = float(_g["_sales"].sum())
    _res = iw.compute_wage(total_hours=_hours, sales_peso=_sales,
                           hourly_wage=_wage_map.get(_who, 0.0), params=_params)
    _rows.append({
        "who": _who, "payee": _payee_map.get(_who, ""), "sessions": len(_g),
        "hours": _hours, "sales": _sales, **_res,
    })
_rdf = pd.DataFrame(_rows).sort_values("total_fee_jpy", ascending=False)

# ============================================================
# 展示
# ============================================================
_k1, _k2, _k3, _k4 = st.columns(4)
_k1.metric(t("网红数"), f"{len(_rdf)}")
_k2.metric(t("已匹配场次"), f"{int(_matched.shape[0])} / {raw.shape[0]}")
_k3.metric(t("未指派场次"), f"{_n_unassigned}")
_k4.metric(t("含手续费 合计(¥)"), f"¥{_rdf['total_fee_jpy'].sum():,.0f}")
if _n_unassigned:
    st.caption("⚠️ " + t("有 {n} 场未指派、未计入工资").format(n=_n_unassigned))

# ---- 業績報告（直接表示·复制可）----
_month_lbl = ""
try:
    _month_lbl = f"{int(str(raw['数据期间'].dropna().iloc[0]).split('/')[0].strip())}月"
except Exception:
    _month_lbl = ""
_lines = [f"{_month_lbl}直播业绩", "", "ライブ実績："]
for _, _r in _rdf.iterrows():
    _full = _r["payee"] or _r["who"]
    _lines.append(f"{_full}（{_r['who']}）　ライブ時間：{_r['hours']:.2f}時間　"
                  f"売上：{int(round(_r['sales']))} peso")
_lines += ["", "給料振込："]
for _, _r in _rdf.iterrows():
    _full = _r["payee"] or _r["who"]
    _lines.append(f"{_full}（{_r['who']}）")
    _lines.append(f"　振込：＄{_r['total_fee_usd']:.0f}（日本円：¥{_r['total_fee_jpy']:,.0f}）")
st.markdown("##### " + t("📋 工资报告（可复制）"))
st.code("\n".join(_lines), language=None)

st.markdown("##### " + t("💰 工资汇总（明细）"))
_disp = pd.DataFrame({
    t("网红"): _rdf["who"].tolist(),
    t("振込先"): _rdf["payee"].tolist(),
    t("场次"): [f"{int(x)}" for x in _rdf["sessions"]],
    t("总时长(h)"): [f"{x:.2f}" for x in _rdf["hours"]],
    t("已确认销售额(₱)"): [f"₱{x:,.0f}" for x in _rdf["sales"]],
    t("給与($)"): [f"${x:,.0f}" for x in _rdf["salary_usd"]],
    t("奖励(₱)"): [f"₱{x:,.0f}" for x in _rdf["incentive_peso"]],
    t("奖励($)"): [f"${x:,.2f}" for x in _rdf["incentive_usd"]],
    t("Total($)"): [f"${x:,.2f}" for x in _rdf["total_usd"]],
    t("含手续费($)"): [f"${x:,.2f}" for x in _rdf["total_fee_usd"]],
    t("日本円(¥)"): [f"¥{x:,.0f}" for x in _rdf["total_jpy"]],
    t("含手续费(¥)"): [f"¥{x:,.0f}" for x in _rdf["total_fee_jpy"]],
})
html_table(_disp)

# 各人 計算表 風 明細（内容は日本語固定 · 工资报告と同様コピー転送前提 · Boss 2026-07-07）
st.markdown("##### " + t("🧾 各人明细"))
for _, _r in _rdf.iterrows():
    with st.expander(f"💸 {_r['who']} — ¥{_r['total_fee_jpy']:,.0f}（{_r['payee']}）"):
        st.markdown(
            f"- 総ライブ時間: **{_r['hours']:.2f} h** × 時給 ${_wage_map.get(_r['who'], 0):g}/h "
            f"→ 給与: **${_r['salary_usd']:,.0f}**\n"
            f"- 確定売上: **₱{_r['sales']:,.0f}** → 段階インセンティブ: "
            f"**₱{_r['incentive_peso']:,.0f}** ÷ {_php_usd} = **${_r['incentive_usd']:,.2f}**\n"
            f"- Total: **${_r['total_usd']:,.2f}** → 手数料込み(×{_FEE}): "
            f"**${_r['total_fee_usd']:,.2f}**\n"
            f"- 日本円(×{_USD_JPY:g}): **¥{_r['total_jpy']:,.0f}** ／ "
            f"手数料込み: **¥{_r['total_fee_jpy']:,.0f}**\n"
            f"- 配信回数: {int(_r['sessions'])} 回 ・ 1回あたり売上: "
            f"₱{(_r['sales'] / _r['sessions'] if _r['sessions'] else 0):,.0f}"
        )
