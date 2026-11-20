"""店铺毛利 · 分页拆分（Boss 2026-11-20）：本文件由 shared/shop_margin/common.py 的全局变量驱动，
经 runpy 在页面内执行。勿直接 import。"""
# ruff: noqa: F821
(tab_deduct, tab_payout, tab_loss, tab_ff3) = st.tabs(
    [t("🧾 店铺扣减"), t("💵 拨款明细"), t("🩸 未结算/损失"), t("💸 退款明细")]
)

# ============================================================
# Tab 6：店铺扣减（Coupang 결산 · coupang.settlement · Boss 2026-07-07 依頼）
#   Coupang Open API 지급내역を元川 PG に日次取込 → 扣減構成を可視化。
#   KRW 建て・Coupang 韓国店のみ → 上部の市場フィルタ / 対象月とは独立。
# ============================================================
with tab_deduct:

    # ============================================================
    # 出荷日基準の損益（市場 → 国 → 店舗）
    #   JO 2026-07-29:「NST の注文番号を基準に、入金も注文番号で突合する。
    #                   判断基準は出荷日の注文番号だけ」
    #   nst.v_shipped_settlement は NST 請求書の注文番号を展開して
    #   プラットフォーム側の手数料・入金を結んだもの。すべて出荷日で括る。
    # ============================================================
    _ja0 = get_lang() == "ja"

    def _u(zh: str, ja: str) -> str:
        return ja if _ja0 else zh

    st.caption(t(
        "以【发货日】为唯一基准 · NST 销售额（发货日）× 平台手续费/回款（按订单号匹配）· 日元 · "
        "「费用匹配率」= 该店发货订单中能在平台侧查到费用明细的比例，低于 100% 时扣减偏小、净利估偏高 · "
        "「回款率」= 已到账订单比例，发货后拨款需 1~2 周，故月内偏低属正常"
    ))

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

    def _shq(sql: str):
        try:
            from shared.cache import cached_df
            return cached_df(conn, sql, None, ver=_ship_ver()), None
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return None, str(e)

    _sh, _sh_err = _shq(
        "SELECT ym, market, platform, country, trim(shop) AS shop, invoices, orders, "
        "matched_orders, settled_orders, matched_pct, settled_pct, "
        "sales_jpy, deduction_jpy, shipping_jpy, payout_jpy, voucher_jpy, "
        "gross_profit_jpy, revenue_jpy, gross_margin_pct, "
        "deduction_pct_with_shipping, "
        "net_margin_est_pct "
        f"FROM nst.v_shipped_settlement WHERE ym = '{ym}'")

    if _sh_err:
        st.error(t("发货日基准视图未取得或连接错误: ") + _sh_err)
        st.info(t("需在元川 PG 执行 nst_api/sql/023,024 并跑 pull_sales_invoice"))
    elif _sh is None or _sh.empty:
        st.info(t("当月无发货数据 · 请先跑 nst_api.pull_sales_invoice"))
    else:
        d = _sh.copy()
        # PG の NUMERIC は decimal.Decimal で載るため演算前に float 化
        for c in ("invoices", "orders", "matched_orders", "settled_orders",
                  "matched_pct", "settled_pct", "sales_jpy", "revenue_jpy", "deduction_jpy",
                  "shipping_jpy", "payout_jpy", "voucher_jpy", "gross_profit_jpy",
                  "gross_margin_pct", "deduction_pct_with_shipping",
                  "net_margin_est_pct"):
            d[c] = pd.to_numeric(d[c], errors="coerce").astype(float).fillna(0)
        d["ded_total"] = d["deduction_jpy"] + d["shipping_jpy"]

        if mk:
            from shared.markets import MARKET_KOREA as _MKK, MARKET_SEA as _MKS
            _keep = set()
            if _MKS in mk:
                _keep.add("ASEAN")
            if _MKK in mk:
                _keep.add("KOREA")
            if _keep:
                d = d[d["market"].isin(_keep)]
        if pf:
            # ビューの platform 列（Shopee/Lazada/Coupang/Other）ではなく
            # ページ統一の店名解析（Amazon/Yahoo/Shopify も分ける）で絞る
            d = d[d["shop"].map(_classify_platform).isin(pf)]

        if d.empty:
            st.info(t("当前市场筛选下无数据"))
        else:
            def _y(v) -> str:
                return f"¥{v:,.0f}"

            def _pct(num, den) -> str:
                return f"{(num / den * 100):.1f}%" if den else "—"

            _tot = d[["sales_jpy", "revenue_jpy", "ded_total", "payout_jpy",
                      "gross_profit_jpy", "orders", "settled_orders"]].sum()
            # ⚠️ 率は **分子と分母を同じ表から** 取る。
            #    粗利は nst.sales_daily 由来 → 分母も sales_daily の revenue_jpy。
            #    控除は請求書額に対する割合 → 分母は sales_jpy（請求書由来）。
            #    混ぜると 2026-06 で粗利率が 59.57% → 60.77% に虚高した（JO 指摘）。
            _gm = _tot["gross_profit_jpy"] / _tot["revenue_jpy"] * 100 \
                if _tot["revenue_jpy"] else None
            _dr = _tot["ded_total"] / _tot["sales_jpy"] * 100 \
                if _tot["sales_jpy"] else None
            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric(_u("销售额（出荷日）", "売上（出荷日）"), _y(_tot["sales_jpy"]))
            k2.metric(_u("扣减合计", "控除合計"), _y(_tot["ded_total"]),
                      _pct(_tot["ded_total"], _tot["sales_jpy"]), delta_color="inverse")
            k3.metric(_u("粗利率", "粗利率"),
                      f"{_gm:.2f}%" if _gm is not None else "—",
                      _u("分母=NST 日次销售额", "分母=NST 日次売上"), delta_color="off")
            # 净利估 = 粗利率 − 控除率。それぞれ自分の分母で出した率を引き算する
            k4.metric(_u("净利估", "純利益(推定)"),
                      f"{_gm - _dr:.1f}%" if (_gm is not None and _dr is not None) else "—")
            # ============================================================
            # 入金率（JO 2026-07-31 依頼）
            #   分子 = その月に出荷した注文のうち、shopee.payout_escrow に
            #          order_sn が載っている件数（Coupang は決済予定日が到来したもの）
            #   分母 = その月に出荷した注文数（NST 請求書の注文番号 DISTINCT）
            #   → **注文数ベース**。金額ではない。「入金に含まれたか」だけを見るので、
            #      一部返金された注文も「入金済」に数える
            # ⚠️ Lazada / Other は明細 API が無く payout_escrow に載らないため
            #    **構造的に永久 0%**。分母に入れると全体が押し下がって誤読される
            #    （2026-07 実測: 全平台混合 38.4% vs 判定可能な範囲だけ 43.5%）。
            #    分母から外し、外した件数を明示する
            # ⚠️ 当月は出荷直後で低いのが正常（2026-07 は 43.5% · 過月は 96% 台）
            # ============================================================
            _paycap = d[~d["platform"].isin(("Lazada", "Other"))]
            _po_n = float(_paycap["orders"].sum())
            _po_s = float(_paycap["settled_orders"].sum())
            _po_x = int(_tot["orders"] - _po_n)     # 判定できない注文数
            k5.metric(_u("入款率", "入金率"), _pct(_po_s, _po_n),
                      _u(f"{_po_s:,.0f}/{_po_n:,.0f} 单"
                         + (f" · 除外 {_po_x:,}（无明细API）" if _po_x else ""),
                         f"{_po_s:,.0f}/{_po_n:,.0f} 件"
                         + (f" · 除外 {_po_x:,}（明細API無し）" if _po_x else "")),
                      delta_color="off")

            _base = float(_tot["revenue_jpy"]) or 1.0
            _gap = float(_tot["revenue_jpy"] - _tot["sales_jpy"])
            _gap_pct = 100.0 * _gap / _base
            if abs(_gap_pct) >= 0.5:
                st.warning(_u(
                    f"⚠️ 上方 KPI 的销售额（{_y(_tot['revenue_jpy'])}）与本 tab"
                    f"（{_y(_tot['sales_jpy'])}）差 {_y(_gap)}（{_gap_pct:+.2f}%）· "
                    "两边现在是**同一口径**（都取 NST · 发货日 tran_date 轴），本应一致 · "
                    "差这么多说明**有一侧数据陈旧**：日次（nst.sales_daily）只滚动更新"
                    "「当月+前月」，更早的月份停在最后一次拉取的快照，之后开的贷方票不会被吸收 · "
                    "补拉命令：`daily_pull --domains sales --since <月初>`",
                    f"⚠️ 上部 KPI の売上（{_y(_tot['revenue_jpy'])}）と本タブ"
                    f"（{_y(_tot['sales_jpy'])}）の差 {_y(_gap)}（{_gap_pct:+.2f}%）· "
                    "両者は**同一口径**（どちらも NST · 発送日 tran_date 軸）で一致するはずです · "
                    "これだけズレるのは**片側が古い**サイン：日次（nst.sales_daily）は"
                    "「当月＋前月」しか更新されず、それ以前の月は最後の取得時点で止まり、"
                    "その後に起票された貸方票が反映されません · "
                    "再取得: `daily_pull --domains sales --since <月初>`"))
            _DL = _u("⬇️ 下载 CSV", "⬇️ CSV ダウンロード")

            def _dl(df_, name, key):
                st.download_button(_DL, df_.to_csv(index=False).encode("utf-8-sig"),
                                   file_name=name, mime="text/csv", key=key)

            # ⚠️ 率の分母に使う列は必ず集計対象に入れる。revenue_jpy を入れ忘れて
            #    分組表が KeyError で落ちた（2026-07-30 · KPI だけ直して集計を忘れた）
            _AGG = {"orders": "sum", "sales_jpy": "sum", "revenue_jpy": "sum",
                    "ded_total": "sum", "payout_jpy": "sum", "voucher_jpy": "sum",
                    "gross_profit_jpy": "sum",
                    "matched_orders": "sum", "settled_orders": "sum"}

            # 参照する列が _AGG に無いと分組表だけが落ちる（KPI は d 全体を見るので通る）。
            # 落ちてから気付くのを避けるため、描画前に一度だけ突き合わせる。
            _need = {"orders", "sales_jpy", "revenue_jpy", "ded_total",
                     "gross_profit_jpy", "payout_jpy", "voucher_jpy",
                     "matched_orders", "settled_orders"}
            _missing = sorted(_need - set(_AGG))
            if _missing:
                st.error(_u(f"内部错误：分组聚合缺少列 {_missing}（_AGG 要和 _rows 对齐）",
                            f"内部エラー: 集計に列が不足 {_missing}（_AGG と _rows の不一致）"))

            def _rows(g, cols):
                out = []
                for _, r in g.iterrows():
                    row = {lbl: r[c] for c, lbl in cols}
                    row.update({
                        _u("订单数", "注文数"): f"{int(r['orders']):,}",
                        _u("销售额", "売上"): _y(r["sales_jpy"]),
                        _u("扣减合计", "控除合計"): _y(r["ded_total"]),
                        _u("扣减率", "控除率"): _pct(r["ded_total"], r["sales_jpy"]),
                        # 卖家券は 2026-08-03 に控除へ算入（JO 決定）。
                        # 控除率が 4〜5pp 上がった理由が表から分かるように内訳を出す
                        _u("其中卖家券", "うちセラークーポン"): _y(r["voucher_jpy"]),
                        # 分子(sales_daily)と分母(sales_daily)を揃える
                        _u("粗利率", "粗利率"): _pct(r["gross_profit_jpy"],
                                                   r["revenue_jpy"]),
                        _u("净利估", "純利(推定)"):
                            # 净利估 = 粗利率 − 控除率（各自の分母で出した率の差）
                            f"{r['gross_profit_jpy'] / r['revenue_jpy'] * 100 - r['ded_total'] / r['sales_jpy'] * 100:.1f}%"
                            if r["revenue_jpy"] and r["sales_jpy"] else "—",
                        _u("已回款", "入金済"): _y(r["payout_jpy"]),
                        _u("回款率", "入金済率"):
                            f"{(r['settled_orders'] / r['orders'] * 100):.0f}%" if r["orders"] else "—",
                        _u("费用匹配率", "突合率"):
                            f"{(r['matched_orders'] / r['orders'] * 100):.0f}%" if r["orders"] else "—",
                    })
                    out.append(row)
                return pd.DataFrame(out)

            st.markdown("**" + _u("平台", "プラットフォーム") + "**")
            _g1 = d.groupby("platform", as_index=False).agg(_AGG).sort_values("sales_jpy", ascending=False)
            html_table(_rows(_g1, [("platform", _u("平台", "プラットフォーム"))]))
            _dl(_g1, f"ship_platform_{ym}.csv", "sh_dl_m")

            st.markdown("**" + _u("国家", "国別") + "**")
            _g2 = (d.groupby(["platform", "country"], as_index=False).agg(_AGG)
                   .sort_values("sales_jpy", ascending=False))
            html_table(_rows(_g2, [("platform", _u("平台", "プラットフォーム")),
                                   ("country", _u("国家", "国"))]))
            _dl(_g2, f"ship_country_{ym}.csv", "sh_dl_c")

            st.markdown("**" + _u("店铺", "店舗") + "**")
            _g3 = d.sort_values("sales_jpy", ascending=False)
            html_table(_rows(_g3, [("platform", _u("平台", "プラットフォーム")),
                                   ("country", _u("国家", "国")),
                                   ("shop", _u("店铺", "店舗"))]))
            _dl(_g3, f"ship_shop_{ym}.csv", "sh_dl_s")


# ============================================================
# Tab 7：拨款明细（注文 1 件 = 1 行 · 官方「已完成拨款」相当）
# ============================================================
with tab_payout:
    _pj = get_lang() == "ja"

    def _pl(zh: str, ja: str) -> str:
        return ja if _pj else zh

    st.caption(t(
        "Shopee 各笔拨款包含的订单明细（现金主义）· 与 Shopee 后台「我的收入 > 已完成拨款 > 导出」同口径 · "
        "金额为当地币原值 · 费用为空表示该单明细尚未取得"
    ))

    @st.cache_data(ttl=300, show_spinner=False)
    def _po_ver() -> str:
        try:
            row = get_readonly_connection().execute(
                "SELECT max(finished_at) FROM shopee.pull_log").fetchone()
            if row and row[0]:
                return str(row[0])
        except Exception:
            pass
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H")

    def _poq(sql: str, params: tuple = ()):
        try:
            from shared.cache import cached_df
            return cached_df(conn, sql, params or None, ver=_po_ver()), None
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return None, str(e)

    _po_opt, _po_err = _poq(
        "SELECT DISTINCT payout_date, country_code, shop_key, shop_name "
        "FROM shopee.v_payout_order ORDER BY payout_date DESC, shop_key")

    if _po_err:
        st.error(t("Shopee 财务视图未取得或连接错误: ") + _po_err)
    elif _po_opt is None or _po_opt.empty:
        st.info(t("Shopee 拨款数据为空 · 请在 page 27 手动同步或等待每日自动拉取"))
    else:
        c1, c2 = st.columns([1, 2])
        _dates = _po_opt["payout_date"].dropna().unique().tolist()
        _sel_d = c1.selectbox(_pl("拨款日", "入金日"), _dates, key="po_date")
        _shops = _po_opt[_po_opt["payout_date"] == _sel_d]
        _shop_labels = {
            f"{r['country_code']} · {r['shop_name'] or r['shop_key']}": r["shop_key"]
            for _, r in _shops.iterrows()}
        _sel_s = c2.multiselect(_pl("店铺（空=全部）", "店舗（空=全部）"),
                                list(_shop_labels), key="po_shop")
        _keys = [_shop_labels[k] for k in _sel_s] or list(_shop_labels.values())

        _ph = ",".join(["%s"] * len(_keys))
        _po, _e2 = _poq(
            "SELECT country_code, shop_name, currency, payout_date, order_sn, "
            "order_create_time, buyer_payment_method, order_original_price, "
            "order_seller_discount, voucher_from_seller, buyer_paid_shipping_fee, "
            "shopee_shipping_rebate, actual_shipping_fee, commission_fee, "
            "service_fee, seller_transaction_fee, credit_card_transaction_fee, "
            "order_ams_commission_fee, seller_return_refund, "
            # 税・関税・GST と返品運賃（JO 2026-07-31「拨款明细和入金表格的项目对齐了吗？」）
            # ⚠️ これが無いと VN で帳尻が合わない（税は商品小計の約 9.9%）。
            #    PH は全国ゼロなので PH だけ見ていると気付けない
            "tax_total, reverse_shipping_fee, payout_amount, "
            "fee_missing FROM shopee.v_payout_order "
            f"WHERE payout_date = %s AND shop_key IN ({_ph}) ORDER BY order_sn",
            tuple([_sel_d] + _keys))

        if _e2:
            st.error(_e2)
        elif _po is None or _po.empty:
            st.info(t("该拨款日无明细"))
        else:
            _miss = int(_po["fee_missing"].sum())

            def _sum(col: str) -> float:
                return float(pd.to_numeric(_po[col], errors="coerce").fillna(0).sum())

            # 控除合計は **官方対账で認定した費目だけ**（reconcile_official.FEE_COLUMNS）。
            # credit_card_transaction_fee / seller_return_refund は控除ではないので入れない
            _ded = sum(abs(_sum(c)) for c in (
                "voucher_from_seller", "commission_fee", "service_fee",
                "seller_transaction_fee", "order_ams_commission_fee",
                "tax_total", "reverse_shipping_fee"))
            m1, m2, m3, m4 = st.columns(4)
            m1.metric(_pl("订单数", "注文数"), f"{len(_po):,}")
            m2.metric(_pl("拨款合计（当地币）", "入金合計（現地通貨）"),
                      f"{_sum('payout_amount'):,.0f}")
            m3.metric(_pl("扣减合计（当地币）", "控除合計（現地通貨）"), f"{_ded:,.0f}",
                      _pl(f"其中税 {_sum('tax_total'):,.0f}",
                          f"うち税 {_sum('tax_total'):,.0f}"), delta_color="off")
            # 取得できていない明細を必ず件数で見せる（黙って 0 円扱いしない）
            m4.metric(_pl("费用明细缺失", "費用内訳の欠落"), f"{_miss:,}",
                      delta=None if not _miss else _pl("需补拉", "要再取得"),
                      delta_color="inverse")

            _view = _po.drop(columns=["fee_missing"]).copy()
            # Decimal のままだと Arrow 変換や書式化で落ちるため float 化
            for _c in _view.columns:
                if _c not in ("country_code", "shop_name", "currency", "payout_date",
                              "order_sn", "buyer_payment_method", "order_create_time"):
                    _view[_c] = pd.to_numeric(_view[_c], errors="coerce").astype(float)
            st.dataframe(_view, use_container_width=True, height=460,
                         hide_index=True)
            st.download_button(
                _pl("⬇️ 下载 CSV（本次拨款全明细）", "⬇️ CSV ダウンロード"),
                _view.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"shopee_payout_{_sel_d}.csv", mime="text/csv",
                key="po_dl")


# ============================================================
# Tab 8：未结算/损失（発送したのに入金が立たない注文）
#   JO 2026-07-29:「0 で計上されているのは返金や紛失で結算されなかった可能性。
#                   これは当社の純損失。KPI にして一覧も出す」
#   nst.v_shipped_order が注文 1 件 = 1 行で状態を 4 つに分類する:
#     settled 入金済 / pending 未入金(正常) / zero 実収≤0 / unmatched 明細に無い
# ============================================================
with tab_loss:
    _lj = get_lang() == "ja"

    def _ll(zh: str, ja: str) -> str:
        return ja if _lj else zh

    st.caption(t(
        "发货了但没收到钱的订单 · 以【发货日】为基准 · "
        "判定分界是**货有没有出日本**（物流状态到「已揽收」以后就算出了）· "
        "「损失」= 出货后被取消 / 退款 / 实收≤0 → 商品和运费都回不来，这才是真损失 · "
        "「出货前取消」= 还没揽收就取消 → 库存能回收，**不计入损失**，单独看数量就行 · "
        "「未匹配」= 平台明细里没有这个订单号 → 三种原因混在一起（拉取范围没覆盖 / 平台确认周期未到 / 该平台没接明细API），"
        "要先按平台排除才能谈损失 · 「金额缺失」= 匹配上了、既没取消也查不到金额 → 数据缺口，需补拉 · "
        "「未入金」是发货后正常的等待期，不算损失"
    ))

    def _lq(sql: str):
        try:
            from shared.cache import cached_df
            return cached_df(conn, sql, None, ver=_ship_ver()), None
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return None, str(e)

    _lo, _lo_err = _lq(
        "SELECT ym, ship_date, market, platform, country, trim(shop) AS shop, order_no, "
        "invoice_no, order_ym_prefix, settle_status, order_status, "
        "logistics_status, shipped_out, cancel_reason, refund_reason, "
        "refund_status, refund_amount_local, "
        "gmv_local, received_local, gmv_jpy, received_jpy, nst_amount_jpy "
        f"FROM nst.v_shipped_order WHERE ym = '{ym}'")

    if _lo_err:
        st.error(t("订单级视图未取得或连接错误: ") + _lo_err)
        st.info(t("需在元川 PG 执行 nst_api/sql/025_create_shipped_order_view.sql"))
    elif _lo is None or _lo.empty:
        st.info(t("当月无发货数据 · 请先跑 nst_api.pull_sales_invoice"))
    else:
        L = _lo.copy()
        for c in ("gmv_local", "received_local", "gmv_jpy", "received_jpy",
                  "nst_amount_jpy"):
            L[c] = pd.to_numeric(L[c], errors="coerce").astype(float).fillna(0)

        if mk:
            from shared.markets import MARKET_KOREA as _MK2, MARKET_SEA as _MS2
            _kp = set()
            if _MS2 in mk:
                _kp.add("ASEAN")
            if _MK2 in mk:
                _kp.add("KOREA")
            if _kp:
                L = L[L["market"].isin(_kp)]
        if pf:
            L = L[L["shop"].map(_classify_platform).isin(pf)]

    if _lo is not None and not _lo.empty and L.empty:
        # 市場フィルタで全部落ちた場合。この先の groupby / 除算が壊れる
        st.info(t("当前市场筛选下无数据"))

    if _lo is not None and not _lo.empty and not L.empty:
        # 損失は **出荷後のみ**（JO 2026-07-30）。出荷前の取消は在庫が戻るので
        # 金額を損失に積まない —— 積むと 7 月実測で 2.85 倍に膨らむ
        _LOSS_ST = ("loss",)
        L["loss_jpy"] = L.apply(
            lambda r: r["nst_amount_jpy"] if r["settle_status"] == "unmatched"
            else (r["gmv_jpy"] if r["settle_status"] in _LOSS_ST else 0.0), axis=1)

        # ⚠️ v_shipped_order は 1 行 = (請求書 × 注文)。ヘッダのコメントが言う
        # 「注文 1 件 = 1 行」は実装と合っていない。同じ注文が 正票 + 貸方票 で
        # 複数行になるため、**注文単位の値**（gmv_jpy）を行のまま集計すると二重計上になる。
        # 既知の罠で、026_create_ff3_reconcile_view.sql:53-55 と
        # 032_dedupe_shipped_settlement_orders.sql:5-10 に実測記録がある
        # （7月は 18,752 注文に対し 41,752 行）。FF-3 タブ(:2074 付近)は
        # drop_duplicates で回避済みだが、この損失タブだけ素の行で sum していた。
        #
        # 2026-09 実測: 損失 277 行 / 219 注文、金額 ¥1,117,313 → 注文単位 ¥887,839
        # （**+26% 過大**）。同じ agg の中で件数だけ nunique・金額だけ sum という
        # 内部矛盾も起きていた（:1865-1866）。
        #
        # gmv_jpy は請求書ごとに僅差があるので（実測差 22 円）、完全な注文額に近い
        # 最大値を採って注文単位へ畳む。
        _loss = (L[L["settle_status"] == "loss"]
                 .sort_values("gmv_jpy", ascending=False)
                 .drop_duplicates(subset=["shop", "order_no"]))
        _pre = L[L["settle_status"] == "preship_void"].drop_duplicates(
            subset=["shop", "order_no"])
        # ⚠️ _unm だけは **行のまま**。loss_jpy = nst_amount_jpy は請求書按分の値で、
        # 正票と貸方票が行レベルで相殺して初めて正味になる。畳むと逆に狂う。
        _unm = L[L["settle_status"] == "unmatched"]
        _noamt = L[L["settle_status"] == "no_amount"].drop_duplicates(
            subset=["shop", "order_no"])
        _risk = L[L["settle_status"].isin(_LOSS_ST + ("unmatched",))]
        _n_all = L["order_no"].nunique()

        k1, k2, k3, k4 = st.columns(4)
        k1.metric(_ll("🩸 损失（出货后）", "🩸 損失（出荷後）"),
                  f"{len(_loss):,}", f"¥{_loss['loss_jpy'].sum():,.0f}",
                  delta_color="inverse")
        k2.metric(_ll("↩️ 出货前取消（不算损失）", "↩️ 出荷前の取消（損失外）"),
                  f"{len(_pre):,}",
                  _ll("库存可回收", "在庫は戻る"), delta_color="off")
        k3.metric(_ll("损失率（对发货单）", "損失率（出荷注文比）"),
                  f"{(len(_loss) / _n_all * 100):.2f}%" if _n_all else "—",
                  f"{len(_loss):,} / {_n_all:,}", delta_color="off")
        k4.metric(_ll("已入金 / 发货订单", "入金済 / 出荷注文"),
                  f"{len(L[L['settle_status'] == 'settled']):,} / {_n_all:,}",
                  None, delta_color="off")

        if len(_noamt):
            st.caption(_ll(
                f"🧩 另有 {len(_noamt):,} 单匹配上了、但既没取消也查不到金额 → 数据缺口，"
                "补拉 escrow/logistics 明细后才能定性",
                f"🧩 他に {len(_noamt):,} 件、突合済みだが取消でも金額取得済でもない注文があります"
                "（データ欠落 · 明細の再取得が必要）"))

        # 未匹配は原因が 3 つ混在する。プラットフォーム別に出さないと誤読される
        if not _unm.empty:
            _up = (_unm.groupby("platform", as_index=False)
                   .agg(orders=("order_no", "nunique"), jpy=("loss_jpy", "sum")))
            _base = L.groupby("platform", as_index=False).agg(all_o=("order_no", "nunique"))
            _up = _up.merge(_base, on="platform", how="left")
            _lines = []
            for _, r in _up.sort_values("orders", ascending=False).iterrows():
                _rate = r["orders"] / r["all_o"] * 100 if r["all_o"] else 0
                if _rate > 95:
                    _why = _ll("该平台明细 API 未接入 → 全部未匹配是必然，不是损失",
                               "明細 API 未接続のため全件が未突合になります（損失ではありません）")
                elif r["platform"] == "Coupang":
                    _why = _ll("Coupang 的销售确认日在配送完成后才生成 → 本月发货的下月才确认，属结构性滞后",
                               "売上認識日は配送完了後に立つため、構造的なラグです")
                else:
                    _why = _ll("多为跨月下单，费用明细只拉了本月 → 回填上月即可消除",
                               "前月の注文が多く、費用明細を当月分しか取得していません → 前月を回填すれば解消します")
                _lines.append(f"- **{r['platform']}** {int(r['orders']):,} "
                              + _ll("单", "件") + f"（¥{r['jpy']:,.0f}）· {_why}")
            st.info(_ll("**未匹配 ≠ 损失。按平台拆开看原因：**\n",
                       "**未突合 ≠ 損失です。プラットフォーム別に原因を切り分けます：**\n")
                    + "\n".join(_lines))

        if _risk.empty:
            st.success(_ll("✅ 本月发货订单全部有结算记录，无损失候选",
                           "✅ 当月の出荷注文はすべて精算済みです"))
        else:
            _LDL = _ll("⬇️ 下载 CSV", "⬇️ CSV ダウンロード")

            # 集計表は **確定損失（取消 + 実収≤0）のみ**。unmatched を混ぜると
            # 未接続プラットフォームの件数に埋もれ、実際に金を落としている店舗が消える
            def _grp(keys, label_cols, key):
                g = (_loss.groupby(keys, as_index=False)
                     .agg(orders=("order_no", "nunique"),
                          loss_jpy=("loss_jpy", "sum"))
                     .sort_values("loss_jpy", ascending=False))
                if g.empty:
                    st.caption(_ll("（无确认损失订单）", "（損失確定の注文なし）"))
                    return
                # 分母は同じ切り口の全出荷注文（率で読めるように）
                base = (L.groupby(keys, as_index=False)
                        .agg(all_orders=("order_no", "nunique")))
                g = g.merge(base, on=keys, how="left")
                rows = []
                for _, r in g.iterrows():
                    row = {lbl: r[c] for c, lbl in label_cols}
                    row.update({
                        _ll("损失订单", "損失注文"): f"{int(r['orders']):,}",
                        _ll("发货订单", "出荷注文"): f"{int(r['all_orders']):,}",
                        _ll("发生率", "発生率"):
                            f"{(r['orders'] / r['all_orders'] * 100):.2f}%"
                            if r["all_orders"] else "—",
                        _ll("损失额(估算)", "損失額(推定)"): f"¥{r['loss_jpy']:,.0f}",
                    })
                    rows.append(row)
                # 画面と CSV の列見出しを揃える（英語の生列名で落とさない）
                _tbl = pd.DataFrame(rows)
                html_table(_tbl)
                st.download_button(_LDL, _tbl.to_csv(index=False).encode("utf-8-sig"),
                                   file_name=f"loss_{key}_{ym}.csv",
                                   mime="text/csv", key=f"loss_dl_{key}")

            # 損失の内訳。7 月実測では「送達失敗」1 項目に 63% が集中していた。
            # 店舗別より先にこれを出す —— 打つ手が変わるのは理由の側だから
            st.markdown("**" + _ll("损失原因构成（出货后）", "損失の内訳（出荷後）") + "**")
            _LS = {"LOGISTICS_DELIVERY_FAILED": _ll("送达失败", "配達失敗"),
                   "LOGISTICS_DELIVERY_DONE": _ll("已送达", "配達完了"),
                   "LOGISTICS_PICKUP_DONE": _ll("已揽收在途", "集荷済・輸送中"),
                   "LOGISTICS_LOST": _ll("丢件", "紛失")}
            _RS = {"ITEM_MISSING": _ll("少件", "商品不足"),
                   "WRONG_ITEM": _ll("发错货", "商品違い"),
                   "SPILLED_CONTENTS": _ll("内容物漏出", "内容物漏れ"),
                   "NOT_RECEIPT": _ll("买家称未收到", "未受領の申告"),
                   "DAMAGED_OTHERS": _ll("破损", "破損"),
                   "FUNCTIONAL_DMG": _ll("功能损坏", "機能不良"),
                   "SUSPICIOUS_PARCEL": _ll("包裹异常", "不審な小包"),
                   "EXPIRED_PRODUCT": _ll("过期品", "期限切れ"),
                   "SLIGHT_SCRATCH_DENTS": _ll("轻微划痕/凹陷", "軽微な傷・凹み"),
                   "CHANGE_MIND": _ll("买家改变主意", "気が変わった"),
                   "EXPECTATION_FAILED": _ll("与预期不符", "期待と違う")}
            _cz = _loss.copy()
            _cz["_ls"] = _cz["logistics_status"].map(
                lambda x: _LS.get(x, x or "—"))
            _cz["_rs"] = _cz["refund_reason"].map(
                lambda x: _RS.get(x, x) if x else _ll("无退款申请", "返金申請なし"))
            _cg = (_cz.groupby(["_ls", "_rs"], as_index=False)
                   .agg(orders=("order_no", "nunique"), jpy=("loss_jpy", "sum"))
                   .sort_values("jpy", ascending=False))
            _tot = _cg["jpy"].sum()
            # 画面と CSV は同じ表から作る（片方だけ翻訳すると CSV が _ls/_rs/jpy になる）
            _rt = pd.DataFrame({
                _ll("物流状态", "配送状態"): _cg["_ls"],
                _ll("退款原因", "返金理由"): _cg["_rs"],
                _ll("订单", "注文"): _cg["orders"].map(lambda v: f"{int(v):,}"),
                _ll("损失额", "損失額"): _cg["jpy"].map(lambda v: f"¥{v:,.0f}"),
                _ll("占比", "構成比"): _cg["jpy"].map(
                    lambda v: f"{v / _tot * 100:.1f}%" if _tot else "—"),
            })
            html_table(_rt)
            st.download_button(
                _LDL, _rt.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"loss_reason_{ym}.csv", mime="text/csv",
                key="loss_dl_reason")

            st.markdown("**" + _ll("确认损失 · 平台", "損失確定 · プラットフォーム") + "**")
            _grp(["platform"], [("platform", _ll("平台", "プラットフォーム"))], "platform")

            st.markdown("**" + _ll("确认损失 · 国家", "損失確定 · 国別") + "**")
            _grp(["platform", "country"],
                 [("platform", _ll("平台", "プラットフォーム")),
                  ("country", _ll("国家", "国"))], "country")

            st.markdown("**" + _ll("确认损失 · 店铺", "損失確定 · 店舗") + "**")
            _grp(["platform", "country", "shop"],
                 [("platform", _ll("平台", "プラットフォーム")),
                  ("country", _ll("国家", "国")),
                  ("shop", _ll("店铺", "店舗"))], "shop")

            st.markdown("**" + _ll("明细清单", "明細一覧") + "**")
            _STATUS = {"loss": _ll("损失（出货后）", "損失（出荷後）"),
                       "preship_void": _ll("出货前取消（不算损失）", "出荷前の取消（損失外）"),
                       "unmatched": _ll("未匹配（平台无此单）", "未突合"),
                       "no_amount": _ll("金额缺失（数据缺口）", "金額欠落")}
            # options は状態コード（ラベルを options にすると言語切替で選択が消える）
            _sel = st.multiselect(
                _ll("状态", "状態"), options=list(_STATUS.keys()),
                default=["loss"], format_func=lambda k: _STATUS[k],
                key="loss_status")
            _det = L[L["settle_status"].isin(_sel)].copy()
            _det["settle_status"] = _det["settle_status"].map(_STATUS)
            _det["logistics_status"] = _det["logistics_status"].map(
                lambda x: _LS.get(x, x or "—"))
            _det["refund_reason"] = _det["refund_reason"].map(
                lambda x: _RS.get(x, x) if x else "—")
            _det = (_det[["ship_date", "platform", "country", "shop", "order_no",
                          "invoice_no", "settle_status", "logistics_status",
                          "refund_reason", "cancel_reason", "gmv_local",
                          "received_local", "loss_jpy"]]
                    .rename(columns={
                        "ship_date": _ll("发货日", "出荷日"),
                        "platform": _ll("平台", "プラットフォーム"),
                        "country": _ll("国家", "国"),
                        "shop": _ll("店铺", "店舗"),
                        "order_no": _ll("订单号", "注文番号"),
                        "invoice_no": _ll("NST 请求书", "NST 請求書"),
                        "settle_status": _ll("状态", "状態"),
                        "logistics_status": _ll("物流状态", "配送状態"),
                        "refund_reason": _ll("退款原因", "返金理由"),
                        "cancel_reason": _ll("取消原因", "取消理由"),
                        "gmv_local": _ll("订单金额(当地币)", "注文金額(現地)"),
                        "received_local": _ll("实收(当地币)", "実収(現地)"),
                        "loss_jpy": _ll("损失额(估算 ¥)", "損失額(推定 ¥)")})
                    .sort_values(_ll("损失额(估算 ¥)", "損失額(推定 ¥)"),
                                 ascending=False))
            st.dataframe(_det, use_container_width=True, height=460,
                         hide_index=True)
            st.download_button(
                _ll("⬇️ 下载 CSV（损失订单全明细）", "⬇️ CSV ダウンロード"),
                _det.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"loss_orders_{ym}.csv", mime="text/csv",
                key="loss_dl_detail")


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
