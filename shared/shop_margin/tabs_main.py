"""店铺毛利 · 分页拆分（Boss 2026-11-20）：本文件由 shared/shop_margin/common.py 的全局变量驱动，
经 runpy 在页面内执行。勿直接 import。"""
# ruff: noqa: F821
_owner_tab = "👤 担当者別" if get_lang() == "ja" else "👤 店铺负责人"
_ad_tab_lbl = "📝 広告費" if get_lang() == "ja" else "📝 广告费"
_perf_tab_lbl = "📋 運営業績" if get_lang() == "ja" else "📋 运营绩效"
(tab_day, tab_owner, tab_shop, tab_market, tab_sku, tab_alert) = st.tabs(
    [t("📈 月内日次推移"), _owner_tab, t("🏪 店舗別"), t("🌐 市場別"),
     t("🏆 TOP SKU"), t("⚠️ 价格预警")]
)

# ============================================================
# Tab 0：月内日次推移（曲線図）← Boss 依頼の核心
# ============================================================
with tab_day:
    # 按下单日（Boss 2026-11-20）：NST は開票日計上で日曜に開票が無い → 下単日へ按分。
    #   Shopee/Coupang は下単日あり、その他は開票日のまま。上月下単分は本月日次から除外し注記。
    from shared.order_date_daily import SHARES_SQL, redistribute
    _shop_day = df.groupby(["shop", "sale_date"], as_index=False).agg(
        qty=("qty_sold", "sum"), revenue=("revenue", "sum"),
        defined_cost=("defined_cost", "sum"), gross_profit=("gross_profit", "sum"))
    _shares, _sh_err = _query(SHARES_SQL, (ym,))
    if _sh_err:
        st.error(("下単日按分の取得に失敗（開票日のまま表示）: " if get_lang() == "ja"
                  else "下单日分摊取数失败（按开票日显示）: ") + _sh_err)
    daily, _prior_rev = redistribute(
        _shop_day, _shares if _shares is not None else pd.DataFrame(
            columns=["shop", "inv_date", "order_date", "amt"]), ym)
    daily["sale_date"] = pd.to_datetime(daily["sale_date"]).dt.date
    daily = daily[daily["sale_date"] <= _cutoff]
    for _c in ("revenue", "defined_cost", "gross_profit"):
        daily[_c] = daily[_c].map(_rhu)
    daily["gross_margin"] = (
        daily["gross_profit"] / daily["revenue"].where(daily["revenue"] != 0)
    ).fillna(0) * 100

    # ── 日次広告費（Shopee Ads 消耗 · JPY）+ 日次CM(推定)（Boss 2026-09-08）──
    #   広告費は真の日次値（自動源）。手数料/取消控除は月次概念のため、
    #   日次CM は推定式: 日粗利 − 日広告費 − 日収益×当月手数料率。
    #   手入力広告費（Coupang 等·月次）は日次に按分しない（月次 CM のみ計上）。
    _adly, _ = _query(
        "SELECT a.perf_date AS sale_date, trim(m.nst_shop) AS shop, "
        "upper(left(a.country, 2)) AS cc, sum(a.expense) AS amt "
        "FROM shopee.ads_daily a "
        "JOIN shopee.v_nst_shop_map m ON m.shop_key = a.country "
        "WHERE to_char(a.perf_date, 'YYYY-MM') = ? "
        "GROUP BY 1, 2, 3", (ym,))
    if _adly is not None and not _adly.empty:
        _adly = _adly[_adly["shop"].isin(set(df["shop"].unique()))].copy()
    if _adly is not None and not _adly.empty:
        _adly["amt"] = pd.to_numeric(_adly["amt"], errors="coerce").fillna(0.0)
        _r2 = {cc: nst_monthly_rates(conn, COUNTRY_TO_CURRENCY.get(cc, cc), [ym])[ym]
               for cc in _adly["cc"].unique()}
        _adly["ad"] = [a * _r2.get(cc, 0.0)
                       for a, cc in zip(_adly["amt"], _adly["cc"])]
        daily = daily.merge(
            _adly.groupby("sale_date", as_index=False).agg(ad=("ad", "sum")),
            on="sale_date", how="left")
    if "ad" not in daily.columns:
        daily["ad"] = 0.0
    daily["ad"] = daily["ad"].fillna(0.0).map(_rhu)
    _fee_rate_day = (tot_fee_k / tot_r) if tot_r else 0.0
    daily["cm_est"] = (daily["gross_profit"] - daily["ad"]
                       - (daily["revenue"] * _fee_rate_day).map(_rhu))

    # 前日データ = 昨日(今日-1)の実績。当月以外/欠測時は「昨日以前の最新日」に回退
    _yest = _today - dt.timedelta(days=1)
    _elig = daily[daily["sale_date"] <= _yest]
    _src = _elig if not _elig.empty else daily
    _last = _src.iloc[-1]
    _prev = _src.iloc[-2] if len(_src) >= 2 else None
    _ld = _last["sale_date"]
    _hdr = "前日データ" if get_lang() == "ja" else "前日数据"
    _ld_md = f"{_ld.month}月{_ld.day}日"
    st.markdown(f"**{_hdr}** · {_ld_md}")
    if _prev is not None:
        _pdt = _prev["sale_date"]
        st.caption(
            f"増減は前々日（{_pdt.month}月{_pdt.day}日）比"
            if get_lang() == "ja"
            else f"涨跌为环比前前日（{_pdt.month}月{_pdt.day}日）"
        )

    def _delta(key, pct=False):
        if _prev is None:
            return None
        cur, prev = float(_last[key]), float(_prev[key])
        diff = cur - prev
        if pct:  # 粗利率は百分点差
            return f"{diff:+.2f}pt"
        rate = (diff / prev * 100) if prev else 0.0
        return f"{diff:+,.0f} ({rate:+.1f}%)"

    # ⚠️ 列変数に `pa` を使うな — shared.price_alert の import 別名を遮蔽して
    #    価格預警タブが落ちる（2026-09-08 実際に発生）
    pq, pr, pg, pm, p_ad, p_cm = st.columns(6)
    pq.metric(_col("qty"), f"{int(_last['qty']):,}", _delta("qty"))
    pr.metric(_col("revenue"), f"¥{_last['revenue']:,.0f}", _delta("revenue"))
    pg.metric(_col("gross_profit"), f"¥{_last['gross_profit']:,.0f}", _delta("gross_profit"))
    pm.metric(_col("gross_margin"), f"{_last['gross_margin']:.2f}%", _delta("gross_margin", pct=True))
    p_ad.metric(_col("ad"), f"¥{_last['ad']:,.0f}", _delta("ad"))
    p_cm.metric(_col("cm_est"), f"¥{_last['cm_est']:,.0f}", _delta("cm_est"))

    # x 軸 = 日付。月選択済みなので "N日" 形式で簡潔表示（chart 用に datetime 化）
    chart_src = daily.copy()
    chart_src["sale_date"] = pd.to_datetime(chart_src["sale_date"])
    chart_src["margin_disp"] = chart_src["gross_margin"].apply(lambda x: f"{x:.2f}%")
    _x = alt.X("sale_date:T", title=None,
               axis=alt.Axis(format="%-d日", labelAngle=0, tickCount="day"))
    _date_tip = alt.Tooltip("sale_date:T", title=_col("sale_date"), format="%Y-%m-%d")

    # 曲線図：日次 総収益 / 粗利（日付軸どこに hover しても両方表示）
    rev_lbl, gp_lbl = _col("revenue"), _col("gross_profit")
    _tip = [_date_tip,
            alt.Tooltip("revenue:Q", title=rev_lbl, format=",.0f"),
            alt.Tooltip("gross_profit:Q", title=gp_lbl, format=",.0f"),
            alt.Tooltip("margin_disp:N", title=_col("gross_margin"))]
    mg_lbl = _col("gross_margin")
    _nearest = alt.selection_point(nearest=True, on="pointerover",
                                   fields=["sale_date"], empty=False)
    _base = alt.Chart(chart_src).encode(x=_x)
    _amt_lbl = "金額" if get_lang() == "ja" else "金额"
    # 金額（左軸）：総収益 / 粗利（軸タイトルは layer 先頭の _l_rev が保持）
    _l_rev = _base.mark_line(point=True).encode(
        y=alt.Y("revenue:Q", title=_amt_lbl), color=alt.datum(rev_lbl),
    )
    _l_gp = _base.mark_line(point=True).encode(
        y=alt.Y("gross_profit:Q", title=None), color=alt.datum(gp_lbl),
    )
    # 粗利率（右軸・破線）
    _l_mg = _base.mark_line(point=True, strokeDash=[5, 3]).encode(
        y=alt.Y("gross_margin:Q", title=mg_lbl,
                scale=alt.Scale(domain=[50, 70]),
                axis=alt.Axis(orient="right", format=".0f")),
        color=alt.datum(mg_lbl),
    )
    # 透明な縦ルールで日付軸全体を hover 対象に → 最近傍の日に縦線+全値 tooltip
    _rule = _base.mark_rule(color="#888").encode(
        opacity=alt.condition(_nearest, alt.value(0.35), alt.value(0)),
        tooltip=_tip,
    ).add_params(_nearest)
    _money = alt.layer(_l_rev, _l_gp)   # 左軸共有
    line_chart = (
        alt.layer(_money, _l_mg, _rule)
        .resolve_scale(y="independent", color="shared")
        .properties(height=320)
        .configure_legend(orient="top", title=None)
    )
    st.altair_chart(line_chart, use_container_width=True)

    # 销量条形图
    qty_lbl = _col("qty")
    bar_chart = alt.Chart(chart_src).mark_bar(size=22).encode(
        x=_x,
        y=alt.Y("qty:Q", title=qty_lbl),
        tooltip=[_date_tip, alt.Tooltip("qty:Q", title=qty_lbl, format=",.0f")],
    ).properties(height=220).configure_axis(
        labelFontSize=_CHART_LABEL_FS, titleFontSize=_CHART_TITLE_FS
    )
    st.altair_chart(bar_chart, use_container_width=True)

    # 明细表（日付倒序·最近日在上。daily 本体は昇順維持＝前日指標/曲線図が依存）
    day_cols = ("sale_date", "qty", "revenue", "defined_cost",
                "gross_profit", "gross_margin", "ad", "cm_est",
                "n_shop")
    html_table(_disp(daily.sort_values("sale_date", ascending=False), day_cols))
    st.caption((f"日付=下単日（Shopee/Coupang の注文日で按分・その他は開票日）· "
                f"原価/粗利は店舗×開票日の粗利率で按分した推定 · "
                f"前月下単・当月開票 ¥{_prior_rev:,.0f} は日次に含まず（月次 KPI には含む）· 直近数日は未開票分が後から加算される")
               if get_lang() == "ja" else
               (f"日期=下单日（Shopee/Coupang 按订单下单日分摊·其他按开票日）· "
                f"原价/毛利按店铺×开票日毛利率分摊（估算）· "
                f"上月下单本月开票 ¥{_prior_rev:,.0f} 未计入每日（月度 KPI 已含）· 最近几天的订单开票后才会陆续补上"))
    st.caption(("広告費=Shopee Ads 日次消耗（円換算·手入力の月次分は含まず）· "
                "CM(日次推定)=日粗利 − 日広告費 − 日収益×当月手数料率"
                "（手数料/取消控除は月次概念のため率で按分した推定値·月次 CM が正）")
               if get_lang() == "ja" else
               "广告费=Shopee Ads 日消耗（日元换算·不含手工录入的月度部分）· "
               "CM(日估)=日毛利 − 日广告费 − 日收益×当月手续费率"
               "（手续费/取消冲减是月度概念，按率摊算的估值·以月度 CM 为准）")

# ============================================================
# Tab：担当者別（日本店=対象外 は除外）
# ============================================================
with tab_owner:
    _od = df   # df は既に「担当者あり」のみに絞り込み済（上流でグローバル除外）
    if _od.empty:
        st.info(t("この条件のデータがありません"))
    else:
        # ① 负责人汇总（総数据总览）+ 柱状图
        #   销售数量 / SKU数 列は Boss 2026-09-07 指示で削除 · CM 4 列を追加
        g = _od.groupby("owner", as_index=False).agg(
            revenue=("revenue", "sum"),
            defined_cost=("defined_cost", "sum"),
            gross_profit=("gross_profit", "sum"),
            n_shop=("shop", "nunique"),
        )
        g = _ns_round_money(g)
        g["gross_margin"] = (
            g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
        ).fillna(0) * 100
        g = _with_cm(g, "owner")
        g = g.sort_values("gross_profit", ascending=False)
        owner_cols = ("owner", "revenue", "cancel_est", "revenue_adj",
                      "defined_cost", "gross_profit", "gross_margin",
                      "fee", "ad", "cm", "cm_rate",
                      "fee_match", "payout_rate", "n_shop")
        html_table(_disp(g, owner_cols, mom_prev=_prev_owner, dim="owner"))
        _cm_note(key="owner")
        st.altair_chart(_hbar(g, "owner"), use_container_width=True)

        st.divider()

        # ② 各负责人ごと：名称(総計) + 配下の店舗別明細
        _gi = g.set_index("owner")
        for _ow in g["owner"].tolist():   # gross_profit 降順
            sub = _od[_od["owner"] == _ow]
            _r = _gi.loc[_ow]
            st.markdown(
                f"**👤 {_ow}** &nbsp;｜&nbsp; {_col('n_shop')}: {int(_r['n_shop'])} ｜ "
                f"{_col('revenue')}: ¥{_r['revenue']:,.0f} ｜ "
                f"{_col('gross_profit')}: ¥{_r['gross_profit']:,.0f} ｜ "
                f"{_col('gross_margin')}: {_r['gross_margin']:.2f}% ｜ "
                f"{_col('cm')}: ¥{_r['cm']:,.0f} ｜ {_col('cm_rate')}: {_r['cm_rate']:.2f}%"
            )
            sg = sub.groupby("shop", as_index=False).agg(
                revenue=("revenue", "sum"),
                defined_cost=("defined_cost", "sum"),
                gross_profit=("gross_profit", "sum"),
            )
            sg = _ns_round_money(sg)
            sg["gross_margin"] = (
                sg["gross_profit"] / sg["revenue"].where(sg["revenue"] != 0)
            ).fillna(0) * 100
            sg = _with_cm(sg, "shop")
            sg = sg.sort_values("gross_profit", ascending=False)
            sg_cols = ("shop", "revenue", "cancel_est", "revenue_adj",
                       "defined_cost", "gross_profit", "gross_margin",
                       "fee", "ad", "cm", "cm_rate",
                       "fee_match", "payout_rate")
            html_table(_disp(sg, sg_cols, mom_prev=_prev_shop, dim="shop"))

    # ============================================================
    # ⚙️ 担当者の設定・変更（Boss 2026-08-27 依頼 · 担当者タブ最下部）
    #   - 発効は「システム現在月」から。過去月の集計は一切動かない。
    #   - 担当者を空にすると、その月からこのページの全計算から外れる。
    # ============================================================
    st.divider()
    st.markdown("##### " + ("⚙️ 担当者の設定・変更" if get_lang() == "ja"
                            else "⚙️ 店铺负责人 设定 / 更改"))
    _set_ym = _today.strftime("%Y-%m")     # 発効月 = システム現在月（ページの対象月ではない）
    st.caption(
        (f"設定は **{_set_ym}** 以降にのみ発効し、過去月の数字は変わりません。"
         "担当者を空欄にすると、その月からこのページの全集計（総収益 KPI・市場統計・"
         "日次推移・TOP SKU・環比）から外れます。"
         if get_lang() == "ja" else
         f"设定自 **{_set_ym}** 起生效，之前月份的数据不受影响。"
         "负责人留空 = 从该月起，该店铺不计入本页任何计算"
         "（总收益 KPI / 市场统计 / 日次推移 / TOP SKU / 环比）。")
    )

    if _owner_schema_err:
        st.info(t("⚠️ 担当者設定テーブル（ops.shop_owner）が使えません（PG 未接続？）"
                  "— 現在はコードのハードコード基線で動作中。")
                + f"\n\n{_owner_schema_err}")
    else:
        with st.expander(("担当者を設定・変更する" if get_lang() == "ja"
                          else "设定 / 更改店铺负责人"), expanded=False):
            _rec = _read_w("SELECT shop, effective_ym, owner FROM " + _OWNER_TBL)
            _omap_now = load_owner_map(conn_w, _set_ym)

            # 対象店舗 = 直近12ヶ月に売上のある店 ∪ 現マッピングに出てくる店
            _sh12, _ = _query(
                "SELECT DISTINCT trim(shop) AS shop FROM nst.sales_daily "
                "WHERE sale_date >= ?",
                ((_today - dt.timedelta(days=365)).isoformat(),))
            _shops = set(_omap_now)
            if _sh12 is not None and not _sh12.empty:
                _shops |= {str(x).strip() for x in _sh12["shop"] if str(x).strip()}

            # 発効月の表示（テーブルに履歴が無ければ「基線」）
            _eff = {}
            if not _rec.empty:
                _r2 = _rec.copy()
                _r2["shop"] = _r2["shop"].astype(str).str.strip()
                _r2 = _r2[_r2["effective_ym"].astype(str) <= _set_ym]
                if not _r2.empty:
                    _eff = (_r2.sort_values("effective_ym")
                            .drop_duplicates("shop", keep="last")
                            .set_index("shop")["effective_ym"].to_dict())
            _base_lbl = "基線（コード既定）" if get_lang() == "ja" else "基线（代码默认）"

            _cur_tbl = pd.DataFrame([
                {"shop": _s,
                 "owner": _omap_now.get(_s, OWNER_NONE),
                 "eff": _eff.get(_s, _base_lbl)}
                for _s in sorted(_shops)
            ])
            _C_SHOP = _col("shop")
            _C_OWNER = _col("owner")
            _C_EFF = "発効開始月" if get_lang() == "ja" else "生效起始月"
            # 列名は固定キー（言語を切り替えても編集を保つ・shared/editor_state）。表示名は column_config の label

            _edited = lang_stable_editor(
                _cur_tbl, hide_index=True, use_container_width=True,
                num_rows="fixed", key="owner_editor", reset=tuple(_cur_tbl["shop"]),   # 行の識別（言語で変わらない）
                column_config={
                    "shop": st.column_config.TextColumn(_C_SHOP, disabled=True),
                    "owner": st.column_config.TextColumn(
                        _C_OWNER,
                        help=("空欄 = 担当者なし（集計対象外）" if get_lang() == "ja"
                              else "留空 = 无负责人（不计入任何计算）")),
                    "eff": st.column_config.TextColumn(_C_EFF, disabled=True),
                },
            )

            def _cell(v) -> str:
                """data_editor のセル → 文字列。空欄は NaN/None で返ってくる。"""
                return "" if v is None or pd.isna(v) else str(v).strip()

            _diff = [
                (_cell(r["shop"]), _cell(r["owner"]))
                for _, r in _edited.iterrows()
                if _cell(r["owner"])
                != str(_omap_now.get(_cell(r["shop"]), OWNER_NONE)).strip()
            ]
            if _diff:
                st.warning(
                    (f"{len(_diff)} 件の変更（{_set_ym} から発効）: " if get_lang() == "ja"
                     else f"{len(_diff)} 处变更（自 {_set_ym} 起生效）：")
                    + "、".join(
                        f"{_s} → {_o or ('担当者なし' if get_lang() == 'ja' else '无负责人')}"
                        for _s, _o in _diff)
                )
            if st.button(("💾 変更を保存" if get_lang() == "ja" else "💾 保存变更"),
                         type="primary", disabled=not _diff, key="owner_save"):
                _ok = _fail = 0
                _errs = []
                for _s, _o in _diff:
                    try:
                        conn_w.execute(
                            "INSERT INTO " + _OWNER_TBL + " "
                            "(shop, effective_ym, owner, updated_at) "
                            "VALUES (?,?,?,NOW()) "
                            "ON CONFLICT (shop, effective_ym) DO UPDATE SET "
                            "  owner = EXCLUDED.owner, updated_at = NOW()",
                            (_s, _set_ym, _o or None))
                        _ok += 1
                    except Exception as _e:  # noqa: BLE001
                        _fail += 1
                        _errs.append(f"{_s}: {_e}")
                if _fail:
                    conn_w.rollback()
                    # 監査 L2: 1 件でも失敗すると整批 rollback → ok 件数を出すと「その分は保存済み」と誤読される
                    st.error(t("保存失败，本批全部未保存（已回滚）")
                             + f"（0/{len(_diff)}）— " + " / ".join(_errs))
                else:
                    conn_w.commit()
                    st.success(f"ok={_ok} fail=0 · {_set_ym} " +
                               ("から発効" if get_lang() == "ja" else "起生效"))
                    st.rerun()

            # 変更履歴（過去分の追跡用 · 消さない）
            _hist = _read_w(
                "SELECT effective_ym, shop, owner, updated_at FROM " + _OWNER_TBL +
                " ORDER BY effective_ym DESC, shop")
            if not _hist.empty:
                st.markdown("###### " + ("変更履歴" if get_lang() == "ja" else "变更历史"))
                _h = _hist.copy()
                _h["owner"] = _h["owner"].fillna("").replace(
                    "", "—（担当者なし）" if get_lang() == "ja" else "—（无负责人）")
                _h["updated_at"] = pd.to_datetime(
                    _h["updated_at"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
                _h.columns = [_C_EFF, _C_SHOP, _C_OWNER,
                              "更新時刻" if get_lang() == "ja" else "更新时间"]
                html_table(_h)

# ============================================================
# Tab 1：店舗別
# ============================================================
with tab_shop:
    # 销售数量 列は Boss 2026-09-07 指示で削除 · CM 4 列を追加
    g = df.groupby(["shop", "owner"], as_index=False).agg(
        revenue=("revenue", "sum"),
        defined_cost=("defined_cost", "sum"),
        gross_profit=("gross_profit", "sum"),
    )
    g = _ns_round_money(g)
    g["gross_margin"] = (
        g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
    ).fillna(0) * 100
    g = _with_cm(g, "shop")
    g = g.sort_values("gross_profit", ascending=False)

    # 列順は尹雪莉さん 2026-08-03 依頼: owner を最終列へ · SKU数 は表から外す
    shop_cols = ("shop", "revenue", "cancel_est", "revenue_adj",
                 "defined_cost", "gross_profit", "gross_margin",
                 "fee", "ad", "cm", "cm_rate",
                 "fee_match", "payout_rate", "owner")
    _shop_disp = _disp(g, shop_cols, mom_prev=_prev_shop, dim="shop")
    html_table(_shop_disp)
    _cm_note(key="shop")
    st.download_button(
        "⬇️ CSV ダウンロード" if get_lang() == "ja" else "⬇️ 下载 CSV",
        _shop_disp.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"店舗別粗利_{ym}.csv", mime="text/csv", key="dl_shop_tab",
    )
    st.altair_chart(_hbar(g, "shop"), use_container_width=True)

    st.divider()
    # ── 単店の日次 粗利率トレンド（Boss 2026-06-22 依頼）──
    #   月/市場フィルタは df 段階で適用済 · 店舗だけ選んで日次 粗利率曲線を見る。
    _sub_hdr = ("🏪 単店の日次 粗利率トレンド" if get_lang() == "ja"
                else "🏪 单店日次毛利率趋势")
    st.markdown("##### " + _sub_hdr)
    _shop_opts = g["shop"].tolist()  # gross_profit 降順（g は上で sort 済）
    sel_shop = st.selectbox(_col("shop"), _shop_opts, key="shop_daily_sel")
    _sdd = (df[df["shop"] == sel_shop]
            .groupby("sale_date", as_index=False)
            .agg(qty=("qty_sold", "sum"), revenue=("revenue", "sum"),
                 gross_profit=("gross_profit", "sum"))
            .sort_values("sale_date"))
    _sdd["gross_margin"] = (
        _sdd["gross_profit"] / _sdd["revenue"].where(_sdd["revenue"] != 0)
    ).fillna(0) * 100
    if _sdd.empty:
        st.info(t("この条件のデータがありません"))
    else:
        _sc = _sdd.copy()
        _sc["sale_date"] = pd.to_datetime(_sc["sale_date"])
        _sc["margin_disp"] = _sc["gross_margin"].apply(lambda x: f"{x:.2f}%")
        _sx = alt.X("sale_date:T", title=None,
                    axis=alt.Axis(format="%-d日", labelAngle=0, tickCount="day"))
        _mg_lbl = _col("gross_margin")
        _stip = [
            alt.Tooltip("sale_date:T", title=_col("sale_date"), format="%Y-%m-%d"),
            alt.Tooltip("revenue:Q", title=_col("revenue"), format=",.0f"),
            alt.Tooltip("gross_profit:Q", title=_col("gross_profit"), format=",.0f"),
            alt.Tooltip("margin_disp:N", title=_mg_lbl),
            alt.Tooltip("qty:Q", title=_col("qty"), format=",.0f"),
        ]
        # 月内日次推移 tab と同じ「透明縦ルール＋最近傍 hover」方式で線上以外でも tooltip。
        _sbase = alt.Chart(_sc).encode(x=_sx)
        _snear = alt.selection_point(nearest=True, on="pointerover",
                                     fields=["sale_date"], empty=False)
        _sline = _sbase.mark_line(point=True, color="#4F46E5").encode(
            y=alt.Y("gross_margin:Q", title=_mg_lbl,
                    scale=alt.Scale(zero=False)),  # 単店は 50-70% 外も有り得る→自動缩放
        )
        _srule = _sbase.mark_rule(color="#888").encode(
            opacity=alt.condition(_snear, alt.value(0.35), alt.value(0)),
            tooltip=_stip,
        ).add_params(_snear)
        _schart = (alt.layer(_sline, _srule).properties(height=300)
                   .configure_axis(labelFontSize=_CHART_LABEL_FS,
                                   titleFontSize=_CHART_TITLE_FS))
        st.altair_chart(_schart, use_container_width=True)

# ============================================================
# Tab 2：市場別
# ============================================================
with tab_market:
    # 销售数量 / SKU数 は市場別のみ残す（Boss 2026-09-07）· CM 4 列を追加
    g = df.groupby("market", as_index=False).agg(
        qty=("qty_sold", "sum"),
        revenue=("revenue", "sum"),
        defined_cost=("defined_cost", "sum"),
        gross_profit=("gross_profit", "sum"),
        n_shop=("shop", "nunique"),
        n_sku=("item_internal_id", "nunique"),
    )
    g = _ns_round_money(g)
    g["gross_margin"] = (
        g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
    ).fillna(0) * 100
    g = _with_cm(g, "market")
    g = g.sort_values("gross_profit", ascending=False)

    mkt_cols = ("market", "qty", "revenue", "cancel_est", "revenue_adj",
                "defined_cost", "gross_profit", "gross_margin",
                "fee", "ad", "cm", "cm_rate",
                "fee_match", "payout_rate", "n_shop", "n_sku")
    html_table(_disp(g, mkt_cols))
    _cm_note(key="market")
    st.altair_chart(_hbar(g, "market"), use_container_width=True)

    # ── 🚫 担当者なし（上の市場統計・KPI には入っていない分）──
    #    Boss 2026-08-27:「単独で無担当ブロックを設けて統一計算」
    #    市場で割らず一本にまとめる。上の市場フィルタも掛けない。
    st.divider()
    _nh = ("🚫 担当者なし（上の集計・KPI には含まれない）" if get_lang() == "ja"
           else "🚫 无负责人（不含在上方统计与 KPI 内）")
    st.markdown("##### " + _nh)
    if _df_noowner.empty:
        st.caption(t("全店舗に担当者が設定されています。"))
    else:
        _nr = _rhu(_df_noowner["revenue"].sum())
        _nc = _rhu(_df_noowner["defined_cost"].sum())
        _ng = _nr - _nc
        _nm = (_ng / _nr * 100) if _nr else 0
        _n1, _n2, _n3, _n4, _n5 = st.columns(5)
        _n1.metric(_col("n_shop"), f"{_df_noowner['shop'].nunique()}")
        _n2.metric(_col("revenue"), f"¥{_nr:,.0f}")
        _n3.metric(_col("defined_cost"), f"¥{_nc:,.0f}")
        _n4.metric(_col("gross_profit"), f"¥{_ng:,.0f}")
        _n5.metric(_col("gross_margin"), f"{_nm:.2f}%")
        _ng2 = _df_noowner.groupby(["shop", "market"], as_index=False).agg(
            qty=("qty_sold", "sum"),
            revenue=("revenue", "sum"),
            defined_cost=("defined_cost", "sum"),
            gross_profit=("gross_profit", "sum"),
            n_sku=("item_internal_id", "nunique"),
        )
        _ng2 = _ns_round_money(_ng2)
        _ng2["gross_margin"] = (
            _ng2["gross_profit"] / _ng2["revenue"].where(_ng2["revenue"] != 0)
        ).fillna(0) * 100
        _ng2 = _ng2.sort_values("gross_profit", ascending=False)
        html_table(_disp(_ng2, ("shop", "market", "qty", "revenue", "defined_cost",
                                "gross_profit", "gross_margin", "n_sku")))
        st.caption(
            ("担当者を割り当てると、翌計算からは上の市場統計・KPI に入ります"
             "（発効はシステム現在月から）。設定は「👤 担当者別」タブ最下部。"
             if get_lang() == "ja" else
             "给这些店设定负责人后，就会计入上方的市场统计与 KPI"
             "（自系统当前月起生效）。设定入口在「👤 店铺负责人」tab 最下面。")
        )

# ============================================================
# Tab 3：TOP SKU
# ============================================================
with tab_sku:
    n_top = st.slider(t("Top N"), 10, 100, 30, 10)
    g = df.groupby(["item_internal_id", "display_name", "maker", "item_rank"],
                   as_index=False, dropna=False).agg(
        qty=("qty_sold", "sum"),
        revenue=("revenue", "sum"),
        gross_profit=("gross_profit", "sum"),
    )
    g = _ns_round_money(g)
    g["gross_margin"] = (
        g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
    ).fillna(0) * 100
    g = g.sort_values("gross_profit", ascending=False).head(n_top)

    sku_cols = ("display_name", "maker", "item_rank", "qty",
                "revenue", "gross_profit", "gross_margin")
    html_table(_disp(g, sku_cols))

# ============================================================
# Tab 5：价格预警（当日 vs 近7日平均 · 参数可调 · Boss 2026-06-22）
#   判定ロジックは shared/price_alert.py（純関数·tests/test_price_alert.py）
# ============================================================
with tab_alert:
    # key は中文に統一（旧版は 1 文字列に中日混在で、どちらの言語でも読めなかった）
    st.caption(t(
        "把各店「当日」(最新确认日) 与 近7日平均 对比，检出毛利异常 · "
        "🔴=规则1/2/3(毛利侵蚀 / 降价冲量 / 破红线) · 🟡=规则4(收益涨了但毛利率跌) · 阈值可在下方调整"
    ))
    _pc1, _pc2, _pc3, _pc4 = st.columns(4)
    _p_dev = _pc1.number_input(t("①跌破近7日均(pp)"), 0.0, 50.0, 5.0, 0.5, key="pa_dev")
    _p_qty = _pc2.number_input(t("②销量上涨(%)"), 0.0, 2000.0, 100.0, 10.0, key="pa_qty")
    _p_mgn = _pc3.number_input(t("②毛利率下降(pp)"), 0.0, 50.0, 5.0, 0.5, key="pa_mgn")
    _p_red = _pc4.number_input(t("③红线毛利率(%)"), 0.0, 100.0, 45.0, 1.0, key="pa_red")
    _params = pa.AlertParams(dev_pp=_p_dev, qty_up_pct=_p_qty,
                             mgn_drop_pp=_p_mgn, red_line_pct=_p_red)

    # 当日 = 当前 df 内最新确认日 · 近7日は月境界をまたぐため sales_daily を直接クエリ
    _cur_day = pd.to_datetime(df["sale_date"]).max().date()
    _win_start = _cur_day - dt.timedelta(days=7)
    _aw, _ae = _query(
        "SELECT trim(s.shop) AS shop, s.sale_date, s.qty_sold, s.revenue, s.gross_profit "
        "FROM nst.sales_daily s WHERE s.sale_date >= ? AND s.sale_date <= ?",
        (_win_start.isoformat(), _cur_day.isoformat()),
    )
    if _ae:
        st.error(_ae)
    elif _aw is None or _aw.empty:
        st.info(t("この条件のデータがありません"))
    else:
        for _c in ("qty_sold", "revenue", "gross_profit"):
            _aw[_c] = _aw[_c].astype(float)
        _aw["sale_date"] = pd.to_datetime(_aw["sale_date"]).dt.date
        _aw = add_market_column(_aw, store_col="shop")
        _aw = add_owner_column(_aw, shop_col="shop", owner_map=_omap)
        _aw = _aw[_aw["owner"].map(has_owner)]
        if mk:
            _aw = _aw[_aw["market"].isin(mk)]
        if pf:
            _aw = _aw[_aw["shop"].map(_classify_platform).isin(pf)]
        _owner_map = (_aw.drop_duplicates("shop").set_index("shop")["owner"]
                      if not _aw.empty else {})
        _dd = _aw.groupby(["shop", "sale_date"], as_index=False).agg(
            qty=("qty_sold", "sum"), revenue=("revenue", "sum"),
            gross_profit=("gross_profit", "sum"))
        _dd["margin"] = (_dd["gross_profit"]
                         / _dd["revenue"].where(_dd["revenue"] != 0)).fillna(0) * 100

        # 発火した規則名。t() を通していなかったため ja 表示でも中文が出ていた
        _RULE_LBL = {pa.R1: t("①跌破7日均"), pa.R2: t("②降价冲量"),
                     pa.R3: t("③破红线"), pa.R4: t("④收益涨毛利没跟")}
        _EMO = {pa.ALERT_RED: "🔴", pa.ALERT_YELLOW: "🟡", pa.ALERT_OK: "✅"}
        _rows = []
        for _shop, _g in _dd.groupby("shop"):
            _cur = _g[_g["sale_date"] == _cur_day]
            if _cur.empty:
                continue  # 当日に売上の無い店舗は判定対象外
            _cur = _cur.iloc[0]
            _base = _g[_g["sale_date"] < _cur_day]
            _hb = not _base.empty
            _am = float(_base["margin"].mean()) if _hb else None
            _aq = float(_base["qty"].mean()) if _hb else None
            _ar = float(_base["revenue"].mean()) if _hb else None
            _status, _fired = pa.evaluate(
                cur_margin=float(_cur["margin"]), avg7_margin=_am,
                cur_qty=float(_cur["qty"]), avg7_qty=_aq,
                cur_rev=float(_cur["revenue"]), avg7_rev=_ar,
                params=_params, has_baseline=_hb)
            _rows.append({
                "_sev": pa.severity(_status), "_st": _status,
                "shop": _shop, "owner": _owner_map.get(_shop, ""),
                "cur_m": float(_cur["margin"]), "avg_m": _am,
                "cur_q": float(_cur["qty"]), "avg_q": _aq,
                "cur_r": float(_cur["revenue"]), "avg_r": _ar,
                "fired": "、".join(_RULE_LBL[r] for r in _fired),
            })
        _adf = pd.DataFrame(_rows)
        if _adf.empty:
            st.info(t("当日に売上のある店舗がありません"))
        else:
            _ka, _kb, _kc, _kd = st.columns(4)
            _ka.metric(t("当日"), f"{_cur_day.month}月{_cur_day.day}日")
            _kb.metric("🔴 " + t("严重"), f"{int((_adf['_st'] == pa.ALERT_RED).sum())}")
            _kc.metric("🟡 " + t("注意"), f"{int((_adf['_st'] == pa.ALERT_YELLOW).sum())}")
            _kd.metric("✅ " + t("正常"), f"{int((_adf['_st'] == pa.ALERT_OK).sum())}")
            _only = st.checkbox(t("只看预警(隐藏正常)"), value=True, key="pa_only")
            _v = _adf.sort_values(["_sev", "cur_m"], ascending=[False, True])
            if _only:
                _v = _v[_v["_st"] != pa.ALERT_OK]
            if _v.empty:
                st.success(t("✅ 当日 全店毛利正常，无预警"))
            else:
                def _f_pct(x):
                    return "—" if x is None or pd.isna(x) else f"{x:.2f}%"

                def _f_yen(x):
                    return "—" if x is None or pd.isna(x) else f"¥{x:,.0f}"

                def _f_num(x):
                    return "—" if x is None or pd.isna(x) else f"{x:,.0f}"

                _disp_alert = pd.DataFrame({
                    t("状态"): [_EMO[s] for s in _v["_st"]],
                    _col("shop"): _v["shop"].tolist(),
                    _col("owner"): _v["owner"].tolist(),
                    t("当日毛利率"): [_f_pct(x) for x in _v["cur_m"]],
                    t("近7日均毛利率"): [_f_pct(x) for x in _v["avg_m"]],
                    t("当日销量"): [_f_num(x) for x in _v["cur_q"]],
                    t("近7日均销量"): [_f_num(x) for x in _v["avg_q"]],
                    t("当日总收益"): [_f_yen(x) for x in _v["cur_r"]],
                    t("近7日均收益"): [_f_yen(x) for x in _v["avg_r"]],
                    t("触发规则"): _v["fired"].tolist(),
                })
                html_table(_disp_alert)

st.divider()
st.caption(
    t("対象月") + f"：{ym} · " + t("市場") + f"：{mk} · "
    + t("表示行（明細）: ") + f"{len(df):,}"
)
