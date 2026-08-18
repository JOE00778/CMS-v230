"""模块 #40 订单财务确定度 · 事业部利润与版块波动（JO 2026-07-30 依頼）.

JO:「赤伝の遅れは 1〜2 ヶ月あると分かっている。だから注文単位で財務データを
     整理して、どれが OK でどれが終わっていて、どれが動く可能性があるかを判断
     できるようにしてほしい」
    「先に全体を出して、確定できるものを確定させ、不確定なものは分けて個別に
     見る。事業部の利益計算と、どのセグメントがブレていてどこが改善したかの
     分析もしやすくなる」

3 タブ:
- 📊 総覧      三層（ok / closed / open）× 月 × プラットフォーム
- ❓ 不確定    open を 4 バケットに割る（データ欠損 / 未到期 / 赤伝待ち / 要監視）
- 💹 事業部    プラットフォーム・店舗・月の利益とブレ（変動係数 CV）

口径:
- 確定度は nst.v_order_finance_status（注文単位 · 5 条件のどれか 1 つでも開いていれば open）
- 利益は nst.v_shipped_settlement（出荷日基準 · page05 と同口径）
- ⚠️ open =「問題」ではない。出荷直後は必ず open（入金待ち）で、それは正常
"""
from __future__ import annotations

import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st

from shared.db import get_readonly_connection
from shared.i18n import lang_selector, t, get_lang

st.set_page_config(page_title=t("订单确定度"), page_icon="🧭", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme, html_table
require_password()
inject_theme()
lang_selector()
conn = get_readonly_connection()

_ja = get_lang() == "ja"


def _u(zh: str, ja: str) -> str:
    return ja if _ja else zh


st.title(t("🧭 订单确定度"))
st.caption(_u(
    "以【发货日】为基准，逐订单判断财务数字还会不会动 · "
    "ok=健全完结 / closed=带损失完结 / open=还会动（右侧列出开着哪个条件）· "
    "⚠️ open 不等于有问题：刚发货的单必然 open（等拨款），属正常",
    "【出荷日】基準で、注文ごとに金額がまだ動くかを判定 · "
    "ok=健全に完結 / closed=損失を伴い完結 / open=まだ動く（開いている条件を併記）· "
    "⚠️ open =「問題」ではない：出荷直後は必ず open（入金待ち）で正常"))


# ============================================================
# データ取得
# ============================================================
@st.cache_data(ttl=300, show_spinner=False)
def _ver() -> str:
    """キャッシュ版本 = NST 請求書 + Shopee 取得の最終時刻（page05 の _ship_ver と同じ）。"""
    vs = []
    for sql in ("SELECT max(pulled_at) FROM nst.sales_invoice",
                "SELECT max(finished_at) FROM shopee.pull_log"):
        try:
            row = get_readonly_connection().execute(sql).fetchone()
            if row and row[0]:
                vs.append(str(row[0]))
        except Exception:
            pass
    return "|".join(vs) or dt.datetime.now(
        dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H")


def _q(sql: str):
    try:
        from shared.cache import cached_df
        return cached_df(conn, sql, None, ver=_ver()), None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, str(e)


# ⚠️ v_order_finance_status は 10 万注文を毎回組み立てるので実測 ~12 秒かかる。
#    タブごとに投げず **1 回だけ全件取って** 以降は pandas で集計する
#    （cached_df がデータ同期まで保持するので、1 日 1 回だけ 12 秒）。
_DF, _ERR = _q(
    "SELECT ym, ship_date, trim(shop) AS shop, platform, order_no, nst_amount_jpy, "
    "is_paid_out, order_status, logistics_status, shipped_out, "
    "refund_status, credit_memos, finality, "
    "array_to_string(open_reasons, ' + ') AS open_reasons "
    "FROM nst.v_order_finance_status")

if _ERR:
    st.error(_u("订单确定度视图读取失败: ", "確定度ビュー取得エラー: ") + _ERR)
    st.stop()
if _DF is None or _DF.empty:
    st.info(_u("暂无数据", "データなし"))
    st.stop()

_DF["nst_amount_jpy"] = pd.to_numeric(_DF["nst_amount_jpy"], errors="coerce").fillna(0.0)
_LATEST_YM = _DF["ym"].max()
# 赤伝ラグとして許容する月数（JO 確認済み「退货的票是有滞后的，大概 1〜2 ヶ月」）。
# これより古い未起票は「待てば解消する」ではなく **経理の作業残** なので D に落とす。
_LAG_OK_YM = sorted(_DF["ym"].unique())[-3:]   # 当月 + 直近 2 ヶ月


def _bucket(r) -> str:
    """open の内訳を 4 つに割る（JO が対応要否を決めるための分類）。

    A〜C は放っておけば解消する。人が見るべきは D だけ。
    """
    if r["platform"] in ("Lazada", "Other"):
        # Lazada は注文明細 API が無い（構造的欠損）。入金の有無を判定できない
        return "A"
    if r["ym"] == _LATEST_YM:
        # 当月出荷 = 入金も返金期間もまだ走り切っていない
        return "B"
    if r["open_reasons"] == "赤伝未起票" and r["ym"] in _LAG_OK_YM:
        # ⚠️ 月齢の条件は必須。2026-07-31 に条件無しで書いていたため、
        #    3 月から 5 ヶ月放置されている 318 件（¥1,032,257）が
        #    「待てば解消する」側に隠れていた。実態は配達完了 + 部分返金済みで
        #    Shopee 側は控除済み、NST に赤伝が起票されていないだけ = 経理の作業残
        return "C"
    return "D"


_BK_LBL = {
    "A": _u("A. 数据缺口（无明细 API）", "A. 構造欠損（明細 API 無し）"),
    "B": _u("B. 未到期（当月发货）", "B. 未到期（当月出荷）"),
    "C": _u("C. 赤伝待起票（滞后 1~2 月内）", "C. 赤伝待ち（1〜2ヶ月ラグ内）"),
    "D": _u("D. 要盯（超期未处理）", "D. 要監視（期限超過）"),
}

_OPEN = _DF[_DF["finality"] == "open"].copy()
if not _OPEN.empty:
    _OPEN["bucket"] = _OPEN.apply(_bucket, axis=1)


def _yen(v) -> str:
    return f"¥{v:,.0f}"


def _pct(v) -> str:
    return f"{v:.1f}%"


def _dl(df: pd.DataFrame, name: str, key: str) -> None:
    st.download_button(_u("⬇ 下载 CSV", "⬇ CSV ダウンロード"),
                       df.to_csv(index=False).encode("utf-8-sig"),
                       file_name=name, mime="text/csv", key=key)


# ============================================================
# 要対応の定義 — 「誰が何をするか」で割る（JO 2026-07-31）
# ------------------------------------------------------------
# JO「需要人处理的，单独做个tab用来判断」
# バケット D（構造欠損でも当月でもラグ内でもない = 待っても解消しない）を
# open_reasons ごとに割り、対応先と作業内容を明示する。
# ⚠️ 1 注文が複数の理由を持ちうるので、区分の合計は D の件数を上回る。
#    「どれか 1 つでも残っていればその注文は閉じない」ので、重複計上が正しい。
# ============================================================
_ACTIONS = [
    ("赤伝未起票", _u("经理", "経理"),
     _u("在 NST 起赤伝冲销销售额", "NST に取消伝票（赤伝）を起票する"),
     _u("平台侧已扣款、NST 销售额还挂着 → 账面虚高",
        "プラットフォーム側は控除済みだが NST の売上が残っている → 帳簿が過大")),
    ("未入金", _u("运营", "運用"),
     _u("查平台后台这笔钱到底来没来", "プラットフォーム後台で入金の有無を確認する"),
     _u("过了正常拨款周期还没入金 → 可能漏拨或有争议",
        "通常の入金サイクルを過ぎている → 未拨款 or 係争の可能性")),
    ("賠償請求中", _u("运营", "運用"),
     _u("提交/跟进平台赔偿申请", "賠償申請を出す / 進捗を追う"),
     _u("丢件・破损等可向平台索赔，逾期不申请就拿不回来",
        "紛失・破損等は平台に請求できる。期限を過ぎると回収不能")),
    ("返金審査中", _u("运营", "運用"),
     _u("回应买家退款交涉", "返金交渉に応答する"),
     _u("平台在等卖家回应，不回应会自动判给买家",
        "平台が出品者の応答を待っている。無応答だと買い手勝ちで確定")),
]

_TODO = (_OPEN[_OPEN["bucket"] == "D"].copy()
         if not _OPEN.empty else _OPEN.copy())

tab1, tab0, tab2, tab3 = st.tabs([
    _u("📊 总览", "📊 総覧"),
    _u("🛠 需要人处理", "🛠 要対応"),
    _u("❓ 不确定明细", "❓ 不確定の内訳"),
    _u("💹 事业部利润与波动", "💹 事業部利益とブレ"),
])

# ============================================================
# 📊 総覧
# ============================================================
with tab1:
    _tot_n, _tot_a = len(_DF), _DF["nst_amount_jpy"].sum()
    g = _DF.groupby("finality").agg(orders=("order_no", "size"),
                                    amt=("nst_amount_jpy", "sum"))
    c = st.columns(4)
    c[0].metric(_u("订单总数", "注文総数"), f"{_tot_n:,}", help=_yen(_tot_a))
    for i, (k, zh, ja) in enumerate([("ok", "✅ 已确定（健全）", "✅ 確定（健全）"),
                                     ("open", "🔄 还会动", "🔄 まだ動く"),
                                     ("closed", "🔴 确定损失", "🔴 損失確定")]):
        n = int(g["orders"].get(k, 0))
        a = float(g["amt"].get(k, 0.0))
        c[i + 1].metric(_u(zh, ja), _yen(a),
                        f"{n:,} {_u('单', '件')} · {100.0 * a / _tot_a:.1f}%"
                        if _tot_a else None, delta_color="off")

    st.caption(_u(
        f"⚠️ 金额为 NST 请求书按订单摊分的**概算**（7/6 起为店铺合并票，1 票最多 253 单）。"
        f"精确的订单别金额看平台侧。数据范围 {_DF['ym'].min()} ~ {_LATEST_YM}",
        f"⚠️ 金額は NST 請求書を注文数で按分した**概算**（7/6 以降は店舗合併票 · 1 票最大 253 注文）。"
        f"厳密な注文別金額はプラットフォーム側を参照。データ範囲 {_DF['ym'].min()} 〜 {_LATEST_YM}"))

    st.subheader(_u("月 × 确定度", "月 × 確定度"))
    _m = _DF.pivot_table(index="ym", columns="finality", values="nst_amount_jpy",
                         aggfunc="sum", fill_value=0.0)
    _mn = _DF.pivot_table(index="ym", columns="finality", values="order_no",
                          aggfunc="size", fill_value=0)
    _rows = []
    for ym in _m.index:
        tot = _m.loc[ym].sum()
        _rows.append({
            _u("月", "月"): ym,
            _u("订单", "注文"): f"{int(_mn.loc[ym].sum()):,}",
            _u("✅ 已确定", "✅ 確定"): _yen(_m.loc[ym].get("ok", 0)),
            _u("🔄 还会动", "🔄 まだ動く"): _yen(_m.loc[ym].get("open", 0)),
            _u("🔴 损失", "🔴 損失"): _yen(_m.loc[ym].get("closed", 0)),
            _u("还会动占比", "まだ動く比率"): _pct(100.0 * _m.loc[ym].get("open", 0) / tot) if tot else "—",
        })
    html_table(pd.DataFrame(_rows))

    st.subheader(_u("平台 × 确定度", "プラットフォーム × 確定度"))
    _p = _DF.groupby(["platform", "finality"])["nst_amount_jpy"].sum().unstack(fill_value=0.0)
    _pn = _DF.groupby("platform")["order_no"].size()
    _rows = []
    for pf in _p.index:
        tot = _p.loc[pf].sum()
        _rows.append({
            _u("平台", "プラットフォーム"): pf,
            _u("订单", "注文"): f"{int(_pn[pf]):,}",
            _u("✅ 已确定", "✅ 確定"): _yen(_p.loc[pf].get("ok", 0)),
            _u("🔄 还会动", "🔄 まだ動く"): _yen(_p.loc[pf].get("open", 0)),
            _u("🔴 损失", "🔴 損失"): _yen(_p.loc[pf].get("closed", 0)),
            _u("还会动占比", "まだ動く比率"): _pct(100.0 * _p.loc[pf].get("open", 0) / tot) if tot else "—",
        })
    html_table(pd.DataFrame(_rows).sort_values(_u("平台", "プラットフォーム")))

    _dl(_DF, "order_finance_status.csv", "dl_all")

# ============================================================
# 🛠 要対応 — 待っても解消しないものだけ
# ============================================================
with tab0:
    if _TODO.empty:
        st.success(_u("没有需要人处理的订单 🎉", "要対応の注文はありません 🎉"))
    else:
        st.markdown(_u(
            "**这里只放「等下去不会自己好」的单。** 当月发货、赤伝滞后 1~2 月内、"
            "Lazada 无 API —— 这些都不在这里（去「不确定明细」看）。",
            "**ここは「待っても解消しない」ものだけ。** 当月出荷・赤伝ラグ 1〜2ヶ月内・"
            "Lazada（API 無し）は入れていない（「不確定の内訳」を参照）。"))

        c = st.columns(3)
        c[0].metric(_u("要处理订单", "要対応の注文"), f"{len(_TODO):,}")
        c[1].metric(_u("挂账金额", "帳簿に残る金額"),
                    _yen(_TODO["nst_amount_jpy"].sum()))
        c[2].metric(_u("最老一单", "最古"), str(_TODO["ship_date"].min()))

        for _reason, _who, _do, _why in _ACTIONS:
            _sub = _TODO[_TODO["open_reasons"].str.contains(_reason, regex=False)]
            if _sub.empty:
                continue
            _amt = _sub["nst_amount_jpy"].sum()
            _old = _sub["ship_date"].min()
            with st.expander(
                    f"【{_who}】{_do} — {len(_sub):,} "
                    + _u("单", "件") + f" {_yen(_amt)}"
                    + _u(f" · 最老 {_old}", f" · 最古 {_old}"),
                    expanded=True):
                st.caption(f"⚠️ {_why}")
                _g = (_sub.groupby(["shop"])
                      .agg(orders=("order_no", "size"),
                           amt=("nst_amount_jpy", "sum"),
                           oldest=("ship_date", "min"))
                      .reset_index().sort_values("amt", ascending=False))
                html_table(pd.DataFrame([{
                    _u("店铺", "店舗"): r["shop"],
                    _u("订单", "注文"): f"{int(r['orders']):,}",
                    _u("金额", "金額"): _yen(r["amt"]),
                    _u("最老", "最古"): str(r["oldest"]),
                } for _, r in _g.iterrows()]))
                # 判断に要る列だけ。実収の有無と物流ステータスで「金が来ないのか
                # 伝票が無いだけなのか」が分かる
                st.dataframe(
                    _sub[["ym", "ship_date", "shop", "order_no", "nst_amount_jpy",
                          "order_status", "logistics_status", "is_paid_out",
                          "refund_status", "credit_memos", "open_reasons"]]
                    .sort_values("nst_amount_jpy", ascending=False),
                    use_container_width=True, height=300)
                _dl(_sub, f"todo_{_reason}.csv", f"dl_todo_{_reason}")

        st.caption(_u(
            "※ 一单可能同时开着多个条件，所以各区分之和会大于上面的订单总数 —— "
            "只要还有一个没处理，这单就关不掉。",
            "※ 1 注文が複数の条件を持ちうるため、区分の合計は上の注文総数を上回ります。"
            "どれか 1 つでも残っていればその注文は閉じません。"))

# ============================================================
# ❓ 不確定の内訳
# ============================================================
with tab2:
    if _OPEN.empty:
        st.success(_u("没有不确定的订单", "不確定の注文なし"))
    else:
        st.markdown(_u(
            "**「还会动」不是一个问题池。** 下面把它割成 4 块 —— A/B/C 放着会自己消解，"
            "真正要人去盯的只有 **D**。",
            "**「まだ動く」は問題の山ではない。** 4 つに割ると、A/B/C は放置で解消し、"
            "人が見るべきは **D** だけ。"))

        b = _OPEN.groupby("bucket").agg(orders=("order_no", "size"),
                                        amt=("nst_amount_jpy", "sum")).reindex(
            ["A", "B", "C", "D"]).fillna(0)
        _tot = b["amt"].sum()
        html_table(pd.DataFrame([{
            _u("分类", "分類"): _BK_LBL[k],
            _u("订单", "注文"): f"{int(b.loc[k, 'orders']):,}",
            _u("金额", "金額"): _yen(b.loc[k, "amt"]),
            _u("占不确定", "不確定に占める"): _pct(100.0 * b.loc[k, "amt"] / _tot) if _tot else "—",
        } for k in ["A", "B", "C", "D"]]))

        st.subheader(_u("开着的条件（1 单可能开多个，故合计 > 订单数）",
                        "開いている条件（1 注文が複数持ちうるので合計 > 注文数）"))
        # ⚠️ regex=False 必須。pandas は長さ 2 以上の pat を既定で正規表現扱いし、
        #    " + " の "+" が量詞になって 1 件も分割されない（実測で踏んだ）
        _ex = _OPEN.assign(
            r=_OPEN["open_reasons"].str.split(" + ", regex=False)).explode("r")
        _r = _ex.groupby("r").agg(orders=("order_no", "size"),
                                  amt=("nst_amount_jpy", "sum")).sort_values("orders", ascending=False)
        html_table(pd.DataFrame([{
            _u("条件", "条件"): k,
            _u("订单", "注文"): f"{int(v['orders']):,}",
            _u("金额", "金額"): _yen(v["amt"]),
        } for k, v in _r.iterrows()]))

        st.subheader(_u("D · 要盯的订单（月 × 店铺）", "D · 要監視の注文（月 × 店舗）"))
        _d = _OPEN[_OPEN["bucket"] == "D"]
        if _d.empty:
            st.success(_u("没有要盯的订单", "要監視の注文なし"))
        else:
            # D は「入金が来ない」と「赤伝が起票されていない」の 2 種類が混ざる。
            # 対応先が違う（前者は運用/プラットフォーム、後者は経理）ので分けて出す
            _d_cred = _d[_d["open_reasons"].str.contains("赤伝未起票", regex=False)]
            if not _d_cred.empty:
                st.error(_u(
                    f"🧾 **赤伝が {_LAG_OK_YM[0]} より前から未起票**: "
                    f"{len(_d_cred):,} 单 {_yen(_d_cred['nst_amount_jpy'].sum())} · "
                    "多为「已送达 + 买家部分退款、Shopee 侧已扣款」但 NST 没开冲销票 · "
                    "**这批不会自己消解，要财务起票**（滞后 1~2 月内的在 C 类，不用管）",
                    f"🧾 **{_LAG_OK_YM[0]} より前の赤伝が未起票**: "
                    f"{len(_d_cred):,} 件 {_yen(_d_cred['nst_amount_jpy'].sum())} · "
                    "多くは「配達完了＋買い手の部分返金」で Shopee 側は控除済みだが "
                    "NST に取消伝票が無い状態 · "
                    "**待っても解消しない。経理の起票が要る**（1〜2ヶ月ラグ内は C 分類）"))

            _g = (_d.groupby(["ym", "shop"])
                  .agg(orders=("order_no", "size"), amt=("nst_amount_jpy", "sum"))
                  .reset_index().sort_values("amt", ascending=False))
            html_table(pd.DataFrame([{
                _u("月", "月"): r["ym"], _u("店铺", "店舗"): r["shop"],
                _u("订单", "注文"): f"{int(r['orders']):,}",
                _u("金额", "金額"): _yen(r["amt"]),
            } for _, r in _g.head(30).iterrows()]))
            st.caption(_u(
                "金额 ¥0 = 赤伝已冲销，账面已归零，只剩状态没关；金额 > 0 = 销售额还挂着，需确认能否收回。",
                "金額 ¥0 = 赤伝で相殺済み・帳簿はゼロ、ステータスが閉じていないだけ。"
                "金額 > 0 = 売上が残っており、回収可否の確認が要る。"))
            _dl(_d, "open_bucket_D.csv", "dl_d")

        st.subheader(_u("不确定订单明细", "不確定注文の明細"))
        _bk = st.multiselect(_u("分类筛选", "分類フィルタ"), ["A", "B", "C", "D"],
                             default=["D"], format_func=lambda k: _BK_LBL[k])
        _v = _OPEN[_OPEN["bucket"].isin(_bk)] if _bk else _OPEN
        st.dataframe(_v[["ym", "ship_date", "platform", "shop", "order_no",
                         "nst_amount_jpy", "open_reasons", "order_status",
                         "logistics_status", "is_paid_out"]],
                     use_container_width=True, height=380)
        _dl(_v, "open_orders.csv", "dl_open")

# ============================================================
# 💹 事業部利益とブレ
# ============================================================
with tab3:
    _S, _SE = _q(
        "SELECT ym, platform, trim(shop) AS shop, orders, sales_jpy, revenue_jpy, "
        "gross_profit_jpy, deduction_jpy FROM nst.v_shipped_settlement")
    if _SE:
        st.error(_u("结算视图读取失败: ", "決算ビュー取得エラー: ") + _SE)
    elif _S is None or _S.empty:
        st.info(_u("暂无数据", "データなし"))
    else:
        for c in ("sales_jpy", "revenue_jpy", "gross_profit_jpy", "deduction_jpy", "orders"):
            _S[c] = pd.to_numeric(_S[c], errors="coerce").fillna(0.0)

        st.subheader(_u("月 × 平台 · 利润", "月 × プラットフォーム · 利益"))
        _g = _S.groupby(["ym", "platform"]).sum(numeric_only=True).reset_index()
        _g["gm_pct"] = 100.0 * _g["gross_profit_jpy"] / _g["revenue_jpy"].replace(0, pd.NA)
        _g["ded_pct"] = 100.0 * _g["deduction_jpy"] / _g["sales_jpy"].replace(0, pd.NA)
        html_table(pd.DataFrame([{
            _u("月", "月"): r["ym"], _u("平台", "プラットフォーム"): r["platform"],
            _u("订单", "注文"): f"{int(r['orders']):,}",
            _u("总收益", "総収益"): _yen(r["revenue_jpy"]),
            _u("毛利", "粗利"): _yen(r["gross_profit_jpy"]),
            _u("毛利率", "粗利率"): _pct(r["gm_pct"]) if pd.notna(r["gm_pct"]) else "—",
            _u("扣减率", "控除率"): _pct(r["ded_pct"]) if pd.notna(r["ded_pct"]) else "—",
        } for _, r in _g.iterrows()]))

        st.altair_chart(
            alt.Chart(_g.dropna(subset=["gm_pct"])).mark_line(point=True).encode(
                x=alt.X("ym:N", title=_u("月", "月")),
                y=alt.Y("gm_pct:Q", title=_u("毛利率 %", "粗利率 %"),
                        scale=alt.Scale(zero=False)),
                color=alt.Color("platform:N", title=_u("平台", "プラットフォーム")),
                tooltip=["ym", "platform", alt.Tooltip("gm_pct:Q", format=".1f")],
            ).properties(height=280), use_container_width=True)

        st.subheader(_u("店铺别波动度（月次毛利率的变动系数 CV）",
                        "店舗別のブレ（月次粗利率の変動係数 CV）"))
        st.caption(_u(
            "CV = 标准差 ÷ 平均 × 100%。**CV 越小越稳定**。经验界：<5% 稳、5~10% 需留意、"
            ">10% 波动大。月数 < 3 的店铺不计（样本不足）。",
            "CV = 標準偏差 ÷ 平均 × 100%。**小さいほど安定**。目安: <5% 安定 / 5〜10% 要注意 / "
            ">10% ブレ大。月数 < 3 の店舗は除外（サンプル不足）。"))
        _m = _S.groupby(["shop", "platform", "ym"]).sum(numeric_only=True).reset_index()
        _m["gm"] = 100.0 * _m["gross_profit_jpy"] / _m["revenue_jpy"].replace(0, pd.NA)
        _m = _m.dropna(subset=["gm"])
        _k = _m.groupby(["shop", "platform"]).agg(
            months=("ym", "nunique"), revenue=("revenue_jpy", "sum"),
            gp=("gross_profit_jpy", "sum"), gm_avg=("gm", "mean"),
            gm_sd=("gm", "std"), gm_min=("gm", "min"), gm_max=("gm", "max"),
        ).reset_index()
        _k = _k[_k["months"] >= 3].copy()
        _k["cv"] = 100.0 * _k["gm_sd"] / _k["gm_avg"].abs().replace(0, pd.NA)
        # 直近月 − 初月 = 改善したのか悪化したのか（ブレの方向）
        _fst = _m.sort_values("ym").groupby("shop")["gm"].first()
        _lst = _m.sort_values("ym").groupby("shop")["gm"].last()
        _k["trend"] = _k["shop"].map(_lst - _fst)

        _sort = st.radio(_u("排序", "並び順"),
                         ["revenue", "cv", "trend"], horizontal=True,
                         format_func=lambda k: {
                             "revenue": _u("按规模", "規模順"),
                             "cv": _u("按波动大", "ブレ大きい順"),
                             "trend": _u("按改善幅度", "改善幅順")}[k])
        _k = _k.sort_values(_sort, ascending=False, na_position="last")

        html_table(pd.DataFrame([{
            _u("店铺", "店舗"): r["shop"], _u("平台", "プラットフォーム"): r["platform"],
            _u("月数", "月数"): int(r["months"]),
            _u("总收益", "総収益"): _yen(r["revenue"]),
            _u("毛利", "粗利"): _yen(r["gp"]),
            _u("平均毛利率", "平均粗利率"): _pct(r["gm_avg"]),
            _u("波动 CV", "ブレ CV"): _pct(r["cv"]) if pd.notna(r["cv"]) else "—",
            _u("区间", "レンジ"): f"{r['gm_min']:.1f}% ~ {r['gm_max']:.1f}%",
            _u("首月→末月", "初月→直近月"): f"{r['trend']:+.1f}pp" if pd.notna(r["trend"]) else "—",
        } for _, r in _k.iterrows()]))
        _dl(_k, "shop_volatility.csv", "dl_cv")
