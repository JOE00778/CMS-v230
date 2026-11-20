"""店铺毛利 · 分页拆分（Boss 2026-11-20）：本文件由 shared/shop_margin/common.py 的全局变量驱动，
经 runpy 在页面内执行。勿直接 import。"""
# ruff: noqa: F821
from contextlib import nullcontext

@st.cache_data(ttl=300, show_spinner=False)
def _ship_ver() -> str:
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


tab_ff3 = nullcontext()

# ============================================================
# Tab 9：退款明细 / 返金詳細（出荷後に入金が立たなかった注文 · 在庫は FF-3 棚に仮置き）
#   JO 2026-07-30:「FF-3 は已发货未入金の注文に対応する在庫の仮置き棚。
#                   戻ってきたら通常倉庫へ、戻らなければ処理（全損）。
#                   Shopee は部分賠償なので賠償を引いた残りが全損。
#                   人が核対しやすい形に整理したリストが欲しい」
#   nst.v_ff3_reconcile は **1 行 = 注文 × SKU**。金額系（gmv/賠償/純損失）は
#   注文単位の値なので SKU 行のまま合計すると多重計上する → order_no で
#   畳んでから KPI と action 集計を出す。cost_total_jpy だけが SKU 単位。
# ============================================================
with tab_ff3:
    _fj = get_lang() == "ja"

    def _fl(zh: str, ja: str) -> str:
        return ja if _fj else zh

    st.caption(t(
        "FF-3 = 已发货但还没入金的订单，其对应库存的**临时暂存棚**。货已经出了日本，"
        "被取消/退款后现物不会自己回来，但 NST 上要做库存处理，所以先移到 FF-3 · "
        "之后由人逐单看：**回来了 → 退回正常仓库**；**回不来 → 处理（全损）** · "
        "Shopee 是**部分赔偿**，赔偿以外的残额才是全损 · "
        "赔偿要晚 6~8 个月才到账，所以「先去要赔偿」的单子别急着当全损处理"
    ))

    def _f3q(sql: str, ver: str):
        try:
            from shared.cache import cached_df
            return cached_df(conn, sql, None, ver=ver), None
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return None, str(e)

    _f3, _f3_err = _f3q(
        "SELECT ym, ship_date, trim(shop) AS shop, platform, country, order_no, invoice_no, "
        "item_internal_id, jan, display_name, qty, cost_estimate, "
        "cost_total_jpy, ff3_qty_now, order_status, logistics_status, "
        "shipped_out, cancel_reason, cancel_by, refund_reason, refund_status, "
        "refund_amount_local, needs_logistics, is_arrived_at_warehouse, "
        "return_tracking_number, gmv_local, received_local, gmv_jpy, "
        "compensation_local, compensation_modules, compensation_rate_pct, "
        "net_loss_jpy, action "
        f"FROM nst.v_ff3_reconcile WHERE ym = '{ym}'", _ship_ver())

    if _f3_err:
        st.error(t("退款明细视图未取得或连接错误: ") + _f3_err)
        st.info(t("需在元川 PG 执行 nst_api/sql/026_create_ff3_reconcile_view.sql"))
    elif _f3 is None or _f3.empty:
        st.info(t("当月无退款明细对象（出货后未入金的订单）"))
    else:
        F = _f3.copy()
        # PG の NUMERIC は decimal.Decimal で載る → float × Decimal の TypeError 防止
        for _c in ("qty", "cost_estimate", "cost_total_jpy", "ff3_qty_now",
                   "refund_amount_local", "gmv_local", "received_local",
                   "gmv_jpy", "compensation_local", "compensation_rate_pct",
                   "net_loss_jpy"):
            F[_c] = pd.to_numeric(F[_c], errors="coerce").astype(float).fillna(0)
        # action が NULL だと groupby が黙って行を落とし、集計の件数が対象注文数と
        # 合わなくなる（核対で最悪の壊れ方）→ 空文字にして「—」で見えるようにする
        F["action"] = F["action"].fillna("")

        # ビューに market 列は無い → 店舗名から分類（page 上部と同じ classify_market）
        F = add_market_column(F, store_col="shop")
        if mk:
            F = F[F["market"].isin(mk)]
        if pf:
            F = F[F["shop"].map(_classify_platform).isin(pf)]

        if F.empty:
            st.info(t("当前市场筛选下无数据"))
        else:
            # 注文単位に畳んだ表。金額 KPI と action 集計はこれを使う
            O = F.drop_duplicates(subset=["country", "order_no"])

            # 賠償が着金している注文だけ（賠償は 6〜8 ヶ月遅れて入るので、
            # 未着金を 0% として平均に混ぜると「Shopee はほぼ払わない」と誤読される）
            _wc = O[O["compensation_local"] > 0]

            k1, k2, k3, k4 = st.columns(4)
            k1.metric(_fl("📦 对象订单数", "📦 対象注文数"), f"{len(O):,}",
                      _fl(f"{len(F):,} 行（订单×SKU）", f"{len(F):,} 行（注文×SKU）"),
                      delta_color="off")
            k2.metric(_fl("🩸 净损失合计", "🩸 純損失 合計"),
                      f"¥{O['net_loss_jpy'].sum():,.0f}",
                      _fl("扣掉已到账赔偿后的残额", "着金済の賠償を引いた残額"),
                      delta_color="inverse")
            k3.metric(_fl("💰 赔偿已到账", "💰 賠償 着金済"),
                      f"¥{float(_wc['gmv_jpy'].sum() - _wc['net_loss_jpy'].sum()):,.0f}",
                      _fl(f"{len(_wc):,} / {len(O):,} 单 · 赔偿滞后 6~8 个月",
                          f"{len(_wc):,} / {len(O):,} 件 · 賠償は 6〜8ヶ月遅れ"),
                      delta_color="off")
            # ⚠️ 平均は **着金済の注文だけ** で取る。0% の未着金を分母に入れない
            k4.metric(_fl("📊 赔偿率（已到账单）", "📊 賠償率（着金済のみ）"),
                      f"{_wc['compensation_rate_pct'].mean():.1f}%" if len(_wc) else "—",
                      _fl("Shopee 是部分赔偿", "Shopee は部分賠償"),
                      delta_color="off")

            # 判定不能（物流データが無く shipped_out を判定できない注文）を必ず併記する。
            # ⚠️ これを隠すと、人は 673 件を「全部」だと読む。実測では判定不能が
            #    2,998 件あり、リストの 4 倍以上あった（2026-07-30 の監査で発覚）
            _und, _und_err = _f3q(
                "SELECT coalesce(sum(orders),0) AS orders, "
                "coalesce(sum(nst_amount_jpy),0) AS jpy "
                f"FROM nst.v_ff3_undecidable WHERE ym = '{ym}'", _ship_ver())
            if _und is not None and not _und.empty:
                _un = int(pd.to_numeric(_und["orders"], errors="coerce").fillna(0).iloc[0])
                if _un:
                    st.warning(_fl(
                        f"⚠️ 另有 {_un:,} 单**判定不能**，没进上面的清单 —— "
                        "这些单缺物流数据，无法判断货有没有出日本（不是「没出货」）。"
                        "定向物流回填跑完后会自动补进来。",
                        f"⚠️ 別に {_un:,} 件が**判定不能**でこのリストに入っていません —— "
                        "物流データが無く、日本を出たかを判定できない状態です"
                        "（「出荷していない」ではありません）。物流の取得後に流入します。"))

            # --- action 别汇总 = 人的作业队列（人的作业顺序に並べる）---
            _ACT = {
                "claim_first":     _fl("① 先去要赔偿（未申请/未到账）",
                                       "① まず賠償を請求（未申請/未着金）"),
                "await_return":    _fl("② 等返送到仓（用追踪号追）",
                                       "② 返送の到着待ち（追跡番号で追う）"),
                "return_to_stock": _fl("③ 退回正常仓库（已确认到仓）",
                                       "③ 通常倉庫へ戻す（入庫確認済）"),
                "writeoff":        _fl("④ 可以处理（丢件确定 or 赔偿已结）",
                                       "④ 処理してよい（紛失確定 or 賠償済）"),
                "monitor":         _fl("⑤ 观察（判断材料不足）",
                                       "⑤ 監視（判断材料が足りない）"),
            }
            _ORDER = list(_ACT.keys())

            st.markdown("**" + _fl("按建议动作分组（这就是作业队列）",
                                   "推奨アクション別（これが作業キュー）") + "**")
            _ag = (O.groupby("action", as_index=False)
                   .agg(orders=("order_no", "nunique"),
                        net_loss=("net_loss_jpy", "sum"),
                        rate=("compensation_rate_pct", "mean")))
            # SKU 行数と FF-3 に積まれる原価は SKU 単位の F 側から取る
            _as = (F.groupby("action", as_index=False)
                   .agg(cost=("cost_total_jpy", "sum")))
            _ag = _ag.merge(_as, on="action", how="left")
            # ⚠️ 0 件の action も行として出す。出さないと「該当なし」と
            #    「シグナルが来ていない」を人が区別できない（return_to_stock が該当 ·
            #    Shopee の入庫確認フラグが一度も真にならないため構造的に 0 件）
            _ag = (_ag.set_index("action").reindex(_ORDER)
                   .fillna(0).reset_index())
            _ag["_o"] = _ag["action"].map(lambda a: _ORDER.index(a))
            _ag = _ag.sort_values("_o")
            html_table(pd.DataFrame({
                _fl("建议动作", "推奨アクション"):
                    _ag["action"].map(lambda a: _ACT.get(a, a or "—")),
                _fl("订单数", "注文数"): _ag["orders"].map(lambda v: f"{int(v):,}"),
                _fl("净损失(¥)", "純損失(¥)"): _ag["net_loss"].map(lambda v: f"¥{v:,.0f}"),
                _fl("平均赔偿率", "平均賠償率"): _ag["rate"].map(lambda v: f"{v:.1f}%"),
                _fl("FF-3 原价(¥)", "FF-3 原価(¥)"):
                    _ag["cost"].fillna(0).map(lambda v: f"¥{v:,.0f}"),
            }))

            # --- FF-3 在庫の現況 vs ビューの原価合計 ---
            from shared.cache import data_version as _dv
            _inv, _inv_err = _f3q(
                "SELECT max(s.snapshot_date)::TEXT AS snap_date, "
                "count(DISTINCT s.item_internal_id) AS items, "
                "sum(s.qty_on_hand) AS qty, "
                "sum(s.qty_on_hand * COALESCE(im.cost_estimate, 0)) AS cost_jpy "
                "FROM nst.inventory_bin_snapshot s "
                "LEFT JOIN nst.item_master_raw im "
                "  ON im.internal_id = s.item_internal_id "
                "WHERE s.bin_number = 'FF-3' AND s.snapshot_date = "
                "  (SELECT max(snapshot_date) FROM nst.inventory_bin_snapshot)",
                _dv("inventory", "basic"))

            st.markdown("**" + _fl("FF-3 在库现况 ↔ 本月对象的原价",
                                   "FF-3 在庫の現況 ↔ 当月対象の原価") + "**")
            _view_cost = float(F["cost_total_jpy"].sum())
            if _inv_err or _inv is None or _inv.empty:
                st.caption(_fl("（FF-3 库存快照读取失败，只显示视图侧金额：",
                               "（FF-3 在庫スナップショットが読めません。ビュー側のみ表示：")
                           + f"¥{_view_cost:,.0f}）")
            else:
                _r = _inv.iloc[0]

                def _n1(col: str) -> float:
                    """1 行目を float 化。NULL/NaN は 0（`nan or 0` は nan が真値なので不可）。"""
                    return float(pd.to_numeric(_inv[col], errors="coerce")
                                 .fillna(0).iloc[0])

                _iq, _ic, _ii = _n1("qty"), _n1("cost_jpy"), int(_n1("items"))
                i1, i2, i3, i4 = st.columns(4)
                i1.metric(_fl("FF-3 品目数", "FF-3 品目数"), f"{_ii:,}",
                          str(_r["snap_date"] or "—"), delta_color="off")
                i2.metric(_fl("FF-3 个数", "FF-3 個数"), f"{_iq:,.0f}", None,
                          delta_color="off")
                i3.metric(_fl("FF-3 原价(¥)", "FF-3 原価(¥)"), f"¥{_ic:,.0f}", None,
                          delta_color="off")
                i4.metric(_fl("差额（FF-3 − 本月对象）", "差額（FF-3 − 当月対象）"),
                          f"¥{_ic - _view_cost:,.0f}",
                          f"{_fl('本月对象', '当月対象')} ¥{_view_cost:,.0f}",
                          delta_color="off")
                st.caption(t(
                    "差额叫**未说明额**，不是错 —— FF-3 是累计残高（往月未处理的还在棚上），"
                    "本月对象只有该月发货的单。实测差额的三个主因："
                    "① 本视图只覆盖 Shopee（Coupang/Lazada 没有 SKU 明细 API，进了 FF-3 也出不来）"
                    "② JAN 未匹配的行成本算 0（Shopee 侧 model_sku 为空或对不上 NST）"
                    "③ 物流/费用明细的拉取起点在 2026-05，更早的单判定不出来"
                ))

            # --- 明细（人工核对用）---
            st.caption(_fl(
                "⚠️ 「③ 回来了放回仓库」机器判不出来 —— Shopee 的入库确认字段"
                "（is_arrived_at_warehouse）实测 93 件全部返 false，一次都没变过。"
                "所以这一档永远 0 件，**不代表没有货回来**，要在 Shopee 后台目视确认。"
                "「② 等返送」里也只有一部分有追踪号，其余只能后台查。",
                "⚠️ 「③ 戻ってきたら在庫に戻す」は機械判定できません —— Shopee の入庫確認"
                "フィールド（is_arrived_at_warehouse）は実測 93 件すべて false で、"
                "一度も真になりません。この区分が 0 件でも**戻りが無いとは限らない**ので、"
                "Shopee 管理画面で目視してください。「② 返送待ち」も追跡番号があるのは一部です。"))

            st.markdown("**" + _fl("明细清单（人工核对）", "明細一覧（人手での照合用）") + "**")
            # ⚠️ options は **action コード**。ラベルを options にすると、言語を切り替えた
            #    とき session_state に旧言語の文字列が残り、Streamlit は例外を出さずに
            #    それを保持するので選択が空になり明細が消える（実測 · streamlit 1.57）
            _sel3 = st.multiselect(
                _fl("建议动作", "推奨アクション"), options=_ORDER,
                default=["claim_first", "await_return"],
                format_func=lambda a: _ACT[a], key="ff3_action")
            D = F[F["action"].isin(_sel3)].copy()
            if D.empty:
                st.caption(_fl("（该动作下没有订单）", "（該当するアクションの注文はありません）"))
            else:
                def _yn(v) -> str:
                    if v is None or (isinstance(v, float) and pd.isna(v)):
                        return "—"
                    return _fl("是", "はい") if bool(v) else _fl("否", "いいえ")

                D["action"] = D["action"].map(lambda a: _ACT.get(a, a or "—"))
                # ⚠️ 注文単位の金額を SKU 行に繰り返すと、operator が CSV を Excel で
                #    合計したとき純損失が 2.84 倍になる（実測 ¥708,156 → ¥2,010,650）。
                #    直前のコミット fd768f6 で直した「旧口径 2.85 倍」と同型の誤りを
                #    CSV 経由で再現させないため、2 行目以降は空欄にする。
                #    SKU 単位で足して良いのは qty / cost_total_jpy / ff3_qty_now だけ。
                _dup = D.duplicated(subset=["country", "order_no"])
                D.loc[_dup, ["gmv_local", "received_local", "compensation_local",
                             "compensation_rate_pct", "net_loss_jpy"]] = None
                for _b in ("shipped_out", "needs_logistics",
                           "is_arrived_at_warehouse"):
                    D[_b] = D[_b].map(_yn)
                _cols3 = {
                    "ship_date":               _fl("发货日", "出荷日"),
                    "action":                  _fl("建议动作", "推奨アクション"),
                    "platform":                _fl("平台", "プラットフォーム"),
                    "country":                 _fl("国家", "国"),
                    "shop":                    _fl("店铺", "店舗"),
                    "order_no":                _fl("订单号", "注文番号"),
                    "invoice_no":              _fl("NST 请求书", "NST 請求書"),
                    "jan":                     _fl("JAN", "JAN"),
                    "display_name":            _fl("商品名", "商品名"),
                    "qty":                     _fl("数量", "数量"),
                    "cost_total_jpy":          _fl("原价合计(¥)", "原価合計(¥)"),
                    "ff3_qty_now":             _fl("FF-3 现有数", "FF-3 現在数"),
                    "order_status":            _fl("订单状态", "注文状態"),
                    "logistics_status":        _fl("物流状态", "配送状態"),
                    "shipped_out":             _fl("已出日本", "日本を出た"),
                    "cancel_reason":           _fl("取消原因", "取消理由"),
                    "cancel_by":               _fl("取消方", "取消者"),
                    "refund_reason":           _fl("退款原因", "返金理由"),
                    "refund_status":           _fl("退款状态", "返金状態"),
                    "needs_logistics":         _fl("需返送", "返送が必要"),
                    "is_arrived_at_warehouse": _fl("已到仓", "倉庫着"),
                    "return_tracking_number":  _fl("返送追踪号", "返送追跡番号"),
                    "gmv_local":               _fl("订单金额(当地币)", "注文金額(現地)"),
                    "received_local":          _fl("实收(当地币)", "実収(現地)"),
                    "compensation_local":      _fl("赔偿(当地币)", "賠償(現地)"),
                    "compensation_rate_pct":   _fl("赔偿率(%)", "賠償率(%)"),
                    "net_loss_jpy":            _fl("净损失(¥)", "純損失(¥)"),
                }
                D = (D[list(_cols3)].rename(columns=_cols3)
                     .sort_values(_cols3["net_loss_jpy"], ascending=False))
                st.dataframe(D, use_container_width=True, height=460,
                             hide_index=True)
                st.download_button(
                    _fl("⬇️ 下载 CSV（退款明细）", "⬇️ CSV ダウンロード（返金詳細）"),
                    D.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"ff3_reconcile_{ym}.csv", mime="text/csv",
                    key="ff3_dl_detail")


st.divider()
st.caption(
    t("対象月") + f"：{ym} · " + t("市場") + f"：{mk} · "
    + t("表示行（明細）: ") + f"{len(df):,}"
)
