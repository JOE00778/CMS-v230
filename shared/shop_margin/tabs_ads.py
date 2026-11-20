"""店铺毛利 · 分页拆分（Boss 2026-11-20）：本文件由 shared/shop_margin/common.py 的全局变量驱动，
经 runpy 在页面内执行。勿直接 import。"""
# ruff: noqa: F821
_owner_tab = "👤 担当者別" if get_lang() == "ja" else "👤 店铺负责人"
_ad_tab_lbl = "📝 広告費" if get_lang() == "ja" else "📝 广告费"
_perf_tab_lbl = "📋 運営業績" if get_lang() == "ja" else "📋 运营绩效"
(tab_ads, tab_perf) = st.tabs([_ad_tab_lbl, _perf_tab_lbl])

# ============================================================
# Tab：📝 広告費（韓国運営の手入力口 + 当月一覧 · Boss 2026-09-08）
#   自動 = Shopee Ads 日次消耗（毎日 05:50 取得）· 手入力 = 非 Shopee 渠道
#   （Coupang 広告は API 無しと実証済 → ここで毎月手入力）
# ============================================================
with tab_ads:
    st.caption(("広告費 = Shopee Ads 消耗（自動）+ 手入力（非Shopee渠道 · "
                "Coupang 広告は WING 広告後台の月次消耗をここで入力）· "
                "同一店×月に手入力があると自動値を上書き · 円換算は当月 NST 三金レート")
               if get_lang() == "ja" else
               "广告费 = Shopee Ads 消耗（自动）+ 手工录入（非 Shopee 渠道 · "
               "Coupang 广告请每月把 WING 广告后台的当月消耗填在这里）· "
               "同店同月有手工记录时覆盖自动值 · 日元换算按当月 NST 三金汇率")

    # ── 店舗別 日次明細（Shopee Ads · Boss 2026-09-08 依頼の表形式）──
    st.divider()
    _ja_ad = get_lang() == "ja"
    st.markdown("##### " + ("📅 店舗別 日次消耗明細（Shopee Ads · 現地通貨）"
                            if _ja_ad else
                            "📅 店铺日级消耗明细（Shopee Ads · 本币）"))
    _ads_shops, _ = _query(
        "SELECT trim(m.nst_shop) AS shop, sum(a.expense) AS exp "
        "FROM shopee.ads_daily a "
        "JOIN shopee.v_nst_shop_map m ON m.shop_key = a.country "
        "WHERE to_char(a.perf_date, 'YYYY-MM') = ? "
        "GROUP BY 1 HAVING sum(coalesce(a.expense, 0)) > 0 "
        "ORDER BY 2 DESC", (ym,))
    if _ads_shops is None or _ads_shops.empty:
        st.info(("この月の Ads 日次データがありません" if _ja_ad
                 else "该月无 Ads 日级数据"))
    else:
        _sel_ad_shop = st.selectbox(
            _col("shop"), _ads_shops["shop"].tolist(), key=f"ads_shop_{ym}")
        _adl, _ = _query(
            "SELECT a.perf_date, a.expense, a.clicks, a.impression, "
            "coalesce(a.direct_order, 0) + coalesce(a.broad_order, 0) AS ad_orders, "
            "coalesce(a.direct_gmv, 0) + coalesce(a.broad_gmv, 0) AS ad_gmv "
            "FROM shopee.ads_daily a "
            "JOIN shopee.v_nst_shop_map m ON m.shop_key = a.country "
            "WHERE trim(m.nst_shop) = ? AND to_char(a.perf_date, 'YYYY-MM') = ? "
            "ORDER BY a.perf_date DESC", (_sel_ad_shop, ym))
        if _adl is None or _adl.empty:
            st.info(t("この条件のデータがありません"))
        else:
            for _c in ("expense", "clicks", "impression", "ad_orders", "ad_gmv"):
                _adl[_c] = pd.to_numeric(_adl[_c], errors="coerce").fillna(0).astype(float)
            _adl["ctr"] = (_adl["clicks"]
                           / _adl["impression"].where(_adl["impression"] != 0)).fillna(0) * 100
            _adl["roas"] = (_adl["ad_gmv"]
                            / _adl["expense"].where(_adl["expense"] != 0)).fillna(0)
            # KPI 行（月合計）
            _te, _tc, _ti = (_adl["expense"].sum(), _adl["clicks"].sum(),
                             _adl["impression"].sum())
            _to, _tg = _adl["ad_orders"].sum(), _adl["ad_gmv"].sum()
            _k1, _k2, _k3, _k4, _k5, _k6 = st.columns(6)
            _k1.metric("消耗 計" if _ja_ad else "消耗 合计", f"{_te:,.2f}")
            _k2.metric("クリック" if _ja_ad else "点击", f"{_tc:,.0f}")
            _k3.metric("表示" if _ja_ad else "曝光", f"{_ti:,.0f}")
            _k4.metric("広告注文" if _ja_ad else "广告订单", f"{_to:,.0f}")
            _k5.metric(t("広告GMV"), f"{_tg:,.0f}")
            _k6.metric("ROAS", f"{(_tg / _te):,.2f}" if _te else "—")
            _ADL_COLS = (("日期", "消耗", "点击", "曝光", "CTR", "广告订单",
                          "广告GMV", "ROAS") if not _ja_ad else
                         ("日付", "消耗", "クリック", "表示", "CTR", "広告注文",
                          "広告GMV", "ROAS"))
            _adt2 = pd.DataFrame({
                _ADL_COLS[0]: _adl["perf_date"].astype(str),
                _ADL_COLS[1]: _adl["expense"].map(lambda v: f"{v:,.2f}"),
                _ADL_COLS[2]: _adl["clicks"].map(lambda v: f"{v:,.0f}"),
                _ADL_COLS[3]: _adl["impression"].map(lambda v: f"{v:,.0f}"),
                _ADL_COLS[4]: _adl["ctr"].map(lambda v: f"{v:.2f}%"),
                _ADL_COLS[5]: _adl["ad_orders"].map(lambda v: f"{v:,.0f}"),
                _ADL_COLS[6]: _adl["ad_gmv"].map(lambda v: f"{v:,.0f}"),
                _ADL_COLS[7]: _adl["roas"].map(lambda v: f"{v:,.2f}"),
            })
            html_table(_adt2)
            st.download_button(
                "⬇️ CSV ダウンロード" if _ja_ad else "⬇️ 下载 CSV",
                _adt2.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"広告費日次_{_sel_ad_shop}_{ym}.csv",
                mime="text/csv", key=f"dl_ads_{ym}")
            # 日次消耗の推移（棒グラフ）
            _adc = _adl.copy()
            _adc["perf_date"] = pd.to_datetime(_adc["perf_date"])
            st.altair_chart(
                alt.Chart(_adc).mark_bar(color="#4F46E5").encode(
                    x=alt.X("perf_date:T", title=None,
                            axis=alt.Axis(format="%-d日", labelAngle=0)),
                    y=alt.Y("expense:Q",
                            title="消耗" if _ja_ad else "消耗"),
                    tooltip=[
                        alt.Tooltip("perf_date:T", title=_ADL_COLS[0],
                                    format="%Y-%m-%d"),
                        alt.Tooltip("expense:Q", title=_ADL_COLS[1],
                                    format=",.2f"),
                        alt.Tooltip("clicks:Q", title=_ADL_COLS[2],
                                    format=",.0f"),
                        alt.Tooltip("impression:Q", title=_ADL_COLS[3],
                                    format=",.0f"),
                    ],
                ).properties(height=220)
                .configure_axis(labelFontSize=_CHART_LABEL_FS,
                                titleFontSize=_CHART_TITLE_FS),
                use_container_width=True)

    st.divider()
    # ── 当月一覧（自動 / 手入力 / 採用値）──
    _AD_L = (("店铺", "自动(Ads消耗)", "手工录入", "计入CM", "来源")
             if get_lang() != "ja" else
             ("店舗", "自動(Ads消耗)", "手入力", "CM計上", "採用"))
    if _ad_detail.empty:
        st.info(("この月の広告費データはまだありません（自動も手入力も無し）"
                 if get_lang() == "ja" else "该月尚无广告费数据（自动与手工均无）"))
    else:
        _adv = _ad_detail.copy()
        for _c in ("auto", "manual"):
            if _c not in _adv.columns:
                _adv[_c] = pd.NA
        _adv["used"] = _adv["manual"].where(_adv["manual"].notna(), _adv["auto"])
        _adv["src"] = [("手入力" if get_lang() == "ja" else "手工")
                       if pd.notna(m) else "Ads"
                       for m in _adv["manual"]]
        # 全 0 の行はノイズなので出さない（Boss 2026-09-08「乱」対策）
        _adv = _adv[_adv["used"].fillna(0) != 0]
        _adv = _adv.sort_values("used", ascending=False)

        def _yen(v):
            return f"¥{v:,.0f}" if pd.notna(v) else "—"

        if _adv.empty:
            st.info(("この月は広告費が全店 0 です" if get_lang() == "ja"
                     else "该月全部店铺广告费为 0"))
        else:
            _adt = pd.DataFrame({
                _AD_L[0]: _adv["shop"],
                _AD_L[1]: _adv["auto"].map(_yen),
                _AD_L[2]: _adv["manual"].map(_yen),
                _AD_L[3]: _adv["used"].map(_yen),
                _AD_L[4]: _adv["src"],
            })
            html_table(_adt)
            _t_used = float(_adv["used"].fillna(0).sum())
            st.caption((f"合計（CM 計上）: ¥{_t_used:,.0f}" if get_lang() == "ja"
                        else f"合计（计入 CM）: ¥{_t_used:,.0f}"))

    st.divider()
    # ============================================================
    # 📝 広告費の録入（対象月 × 店舗 · 手入力（非Shopee渠道）· Boss 2026-09-08 独立タブ化）
    #   保存 = 対象月の全行入替（この画面が唯一の書込口 · 行数は月数十行程度）
    # ============================================================
    st.markdown("##### " + ("📝 広告費の録入（対象月 = 上で選択中の月）"
                            if get_lang() == "ja"
                            else "📝 广告费录入（对象月 = 上方选中的月份）"))
    st.caption(("Shopee 店は Ads 消耗が自動計上されるため通常入力不要。"
                "手入力は非 Shopee 渠道（Coupang 広告等）や手動補正用 · "
                "同一店×月に手入力があると自動値を**上書き**します · "
                "外貨は当月 NST 三金レートで円換算 · 同一店舗複数行は合算")
               if get_lang() == "ja" else
               "Shopee 店的广告消耗已自动计入，通常无需录入。"
               "手工录入用于非 Shopee 渠道（如 Coupang 广告）或手动修正 · "
               "同店同月有手工记录时将**覆盖**自动值 · "
               "外币按当月 NST 三金汇率换算日元 · 同一店铺多行时合算")
    if _ad_schema_err:
        st.info(("⚠️ 広告費テーブル（" + _AD_TBL + "）が使えません（PG 未接続？）: "
                 if get_lang() == "ja" else
                 "⚠️ 广告费表（" + _AD_TBL + "）不可用（PG 未连接？）: ") + _ad_schema_err)
    else:
        with st.expander(("広告費を録入・変更する" if get_lang() == "ja"
                          else "录入 / 修改广告费"), expanded=False):
            # 店舗候補 = 直近12ヶ月に売上のある店舗（担当者設定と同じ範囲）
            _sh12a, _ = _query(
                "SELECT DISTINCT trim(shop) AS shop FROM nst.sales_daily "
                "WHERE sale_date >= ?",
                ((_today - dt.timedelta(days=365)).isoformat(),))
            _ad_shop_opts = (sorted({str(x).strip() for x in _sh12a["shop"]
                                     if str(x).strip()})
                             if _sh12a is not None and not _sh12a.empty else [])
            _ad_cur_rows = _read_w(
                f"SELECT shop, amount, currency, note FROM {_AD_TBL} "
                "WHERE ym = ? ORDER BY shop", (ym,))
            if _ad_cur_rows.empty:
                _ad_cur_rows = pd.DataFrame(
                    {"shop": pd.Series(dtype=str),
                     "amount": pd.Series(dtype=float),
                     "currency": pd.Series(dtype=str),
                     "note": pd.Series(dtype=str)})
            _ad_cur_rows["amount"] = pd.to_numeric(
                _ad_cur_rows["amount"], errors="coerce")
            _C_AMT = "金額（現地通貨）" if get_lang() == "ja" else "金额（本币）"
            _C_CUR = "通貨" if get_lang() == "ja" else "币种"
            _C_NOTE = "備考" if get_lang() == "ja" else "备注"
            _ad_edit = lang_stable_editor(
                _ad_cur_rows,
                num_rows="dynamic",
                use_container_width=True,
                key=f"ad_editor_{ym}",
                column_config={
                    "shop": st.column_config.SelectboxColumn(
                        _col("shop"), options=_ad_shop_opts, required=True),
                    "amount": st.column_config.NumberColumn(
                        _C_AMT, min_value=0.0, format="%.2f"),
                    "currency": st.column_config.SelectboxColumn(
                        _C_CUR, options=list(_AD_CURRENCIES), default="JPY"),
                    "note": st.column_config.TextColumn(_C_NOTE),
                },
            )
            if st.button(("💾 保存（" + ym + " の広告費を上書き）") if get_lang() == "ja"
                         else ("💾 保存（覆盖 " + ym + " 的广告费）"),
                         key=f"ad_save_{ym}"):
                _rows_ok, _rows_skip = [], 0
                _seen: dict[str, list] = {}
                _mixed: set[str] = set()
                for _, _r in _ad_edit.iterrows():
                    _s = str(_r.get("shop") or "").strip()
                    _a = pd.to_numeric(_r.get("amount"), errors="coerce")
                    _c = str(_r.get("currency") or "JPY").strip().upper() or "JPY"
                    _n = str(_r.get("note") or "").strip()
                    if not _s or pd.isna(_a) or float(_a) <= 0:
                        _rows_skip += 1
                        continue
                    # 同一店舗×通貨は合算。PK が ym×shop なので 1 店舗 1 通貨（違う通貨は前の行を黙って捨てていた・監査 M5 2026-11-17）
                    if _s in _seen and _seen[_s][1] != _c:
                        _mixed.add(_s)
                    elif _s in _seen:
                        _seen[_s][0] += float(_a)
                        if _n:
                            _seen[_s][2] = _n
                    else:
                        _seen[_s] = [float(_a), _c, _n]
                _rows_ok = [(ym, s, v[0], v[1], v[2] or None)
                            for s, v in _seen.items()]
                if _mixed:
                    st.error(("同じ店舗に通貨が 2 種類あります（1 店舗 1 通貨にしてください・保存していません）: "
                              if get_lang() == "ja" else "同一店铺填了两种币种（每个店铺只能一种币种・未保存）: ")
                             + "・".join(sorted(_mixed)))
                else:
                    try:
                        conn_w.execute(f"DELETE FROM {_AD_TBL} WHERE ym = ?", (ym,))
                        for _row in _rows_ok:
                            conn_w.execute(
                                f"INSERT INTO {_AD_TBL} (ym, shop, amount, currency, note) "
                                "VALUES (?, ?, ?, ?, ?)", _row)
                        conn_w.commit()
                        st.success((f"保存完了 ok={len(_rows_ok)} skipped={_rows_skip}"
                                    "（空店舗/金額0）" if get_lang() == "ja" else
                                    f"已保存 ok={len(_rows_ok)} skipped={_rows_skip}"
                                    "（店铺空/金额0）"))
                        st.rerun()
                    except Exception as _e:  # noqa: BLE001
                        try:
                            conn_w.rollback()
                        except Exception:
                            pass
                        st.error(("保存失敗: " if get_lang() == "ja" else "保存失败: ")
                                 + str(_e))


# ============================================================
# Tab 11：運営業績（JO 2026-09-14 · 財務 statement 表と同じ費目で店舗別に並べる）
#   運営業績額 = 粗利 − 控除合計 − JD 費用 − 広告費
#   控除合計   = 販売手数料 + サービス費 + 取引手数料 + AMS + セラークーポン
#                + 第三者物流費 + その他(税・返品運賃) − 買い手支払運費 − Shopee 運費回扣
#                （v_shipped_settlement の恒等式「売上 − 控除 + 物流純額 = 実収」と同じ括り）
#   別掲（引かない）: 返金額（Shopee seller_return_refund · 紅票が GMV を既に剥がすため
#                二重控除になる）、線下調整（NST 品目 31109「入金調整分」= 補償金等 ·
#                原価 0 で粗利を吊り上げるので売上/粗利から外して単列）。
#   軸: 売上/粗利=NST 出荷日 · 控除/返金=注文番号で出荷月へ · JD=請求月(≈出荷月)を
#       注文番号で店舗へ · 広告費=消耗月 · 線下調整=NST 計上月。
# ============================================================
_OFFLINE_ITEM = "31109"   # NST 非在庫品目「入金調整分」（補償金 / 入金調整）

with tab_perf:
    _ja_pf = get_lang() == "ja"
    st.caption(("運営業績額 = 粗利 − 控除合計 − JD 費用 − ECMS 輸送費 − 広告費 · "
                "控除は statement と同じ費目で明細表示（買い手支払運費・Shopee 運費回扣は控除を減らす側）· "
                "返金額と線下調整（NST「入金調整分」）は引かずに別掲 · "
                "線下調整は売上/粗利からも外してある")
               if _ja_pf else
               "运营绩效金额 = 毛利 − 扣减合计 − 京东费用 − ECMS运费 − 广告费 · "
               "扣减按 statement 同名费目逐项列出（买家支付运费、Shopee 运费回扣是减少扣减的项）· "
               "退款金额与线下调整（NST「入金調整分」）不扣、单列对照 · "
               "线下调整已从销售额/毛利中剔出")

    # --- 控除明細（月×店舗 · 円）---
    _pf_raw, _pf_err = _query(
        "SELECT trim(shop) AS shop, deduction_jpy, shipping_jpy, commission_jpy, "
        "service_jpy, transaction_jpy, ams_jpy, voucher_jpy, "
        "buyer_ship_jpy, rebate_jpy, refund_jpy "
        "FROM nst.v_shipped_settlement WHERE ym = ?", (ym,))
    if _pf_raw is None or _pf_raw.empty:
        if _pf_err:
            st.warning(("控除明細を取得できません（ビュー 033 未適用の可能性）: "
                        if _ja_pf else "无法取得扣减明细（视图 033 可能未应用）: ") + _pf_err)
        _pf_raw = pd.DataFrame(columns=["shop"])
    _pf_fee = pd.DataFrame({"shop": _pf_raw.get("shop", pd.Series(dtype=str))})
    for _src, _dst in (("commission_jpy", "commission"), ("service_jpy", "service"),
                       ("transaction_jpy", "transaction"), ("ams_jpy", "ams"),
                       ("voucher_jpy", "voucher"), ("shipping_jpy", "ship3pl"),
                       ("buyer_ship_jpy", "buyer_ship"), ("rebate_jpy", "rebate"),
                       ("refund_jpy", "refund"), ("deduction_jpy", "_ded")):
        _pf_fee[_dst] = pd.to_numeric(_pf_raw.get(_src), errors="coerce").fillna(0.0)
    # その他 = 控除合計(deduction) − 明細に出した 5 項（= 税・返品運賃 ± 丸め）
    _pf_fee["other_ded"] = (_pf_fee["_ded"] - _pf_fee["commission"] - _pf_fee["service"]
                            - _pf_fee["transaction"] - _pf_fee["ams"] - _pf_fee["voucher"])
    _pf_fee = _pf_fee.drop(columns=["_ded"])

    # --- JD 費用（請求月 = 対象月 · 注文番号 → NST 店舗 · 税抜）---
    #     logistics.cost_monthly は斑马店名なので使わず、明細行を注文番号で NST 店舗へ寄せる
    #     （2026-07 実測: Shopee PH ¥835,268 vs 斑马店名口径 ¥839,713 · 差 0.5% は NST 未突合）
    _jd_raw, _jd_err = _query(
        "SELECT trim(io.shop) AS shop, sum(r.amount_ex_tax) AS jd "
        "FROM logistics.cost_invoice_raw r "
        "LEFT JOIN logistics.order_shop_map mp ON mp.parcel_no = r.join_key "
        "JOIN LATERAL (SELECT si.shop FROM nst.invoice_order io "
        "  JOIN nst.sales_invoice si ON si.invoice_id = io.invoice_id "
        "  WHERE io.order_no = coalesce(mp.order_id, r.join_key) LIMIT 1) io ON TRUE "
        "WHERE r.year_month = ? GROUP BY 1", (ym,))
    if _jd_raw is None or _jd_raw.empty:
        if _jd_err:
            st.warning(("JD 費用を取得できません: " if _ja_pf else "无法取得京东费用: ") + _jd_err)
        _jd_raw = pd.DataFrame(columns=["shop", "jd"])
    _jd_raw["jd"] = pd.to_numeric(_jd_raw["jd"], errors="coerce").fillna(0.0)

    # --- ECMS 輸送費（請求月 = 対象月 · 注文番号 → NST 店舗 · 税抜）---
    #     ECMS の請求明細は注文番号を直接持っているので、JD のような
    #     「包裹番号 → 注文番号」の中間マッピングは要らない。
    #     logistics.cost_monthly ではなく明細から引くのは JD と同じ理由
    #     （店舗名の揺れを避け、NST の店舗帰属に揃えるため）。
    #     航空運賃は課税対象外なので、この金額はそのまま税抜として JD と足せる。
    #     ⚠️ 注文番号の末尾 '-1' '-2' は分納の通し番号（節税のため 1 注文を 2 回に
    #        分けて出す · Boss 2026-10-02）。NST は接尾辞なししか持たないので剥いで照合する。
    #     ⚠️ 請求書にしか無い費目（賠償など）は注文に紐づかないのでここには入らない。
    #        月次の全体像は page28「物流費用分析」で見ること。
    _ecms_raw, _ecms_err = _query(
        "SELECT trim(io.shop) AS shop, "
        "       sum(d.ecms_freight + d.ecms_fuel + d.ecms_permit + d.ecms_ese_care "
        "           + d.ecms_au_surcharge + d.ecms_other) AS ecms "
        "FROM logistics.ecms_invoice_detail d "
        "JOIN LATERAL (SELECT si.shop FROM nst.invoice_order io "
        "  JOIN nst.sales_invoice si ON si.invoice_id = io.invoice_id "
        "  WHERE io.order_no = regexp_replace(d.order_no, '-[0-9]+$', '') LIMIT 1) io ON TRUE "
        "WHERE d.year_month = ? GROUP BY 1", (ym,))
    if _ecms_raw is None or _ecms_raw.empty:
        if _ecms_err:
            st.warning(("ECMS 輸送費を取得できません: " if _ja_pf else "无法取得 ECMS 运费: ")
                       + _ecms_err)
        _ecms_raw = pd.DataFrame(columns=["shop", "ecms"])
    _ecms_raw["ecms"] = pd.to_numeric(_ecms_raw["ecms"], errors="coerce").fillna(0.0)

    _pf_shop = (_pf_fee.merge(_jd_raw, on="shop", how="outer")
                .merge(_ecms_raw, on="shop", how="outer")
                .merge(_ad_df, on="shop", how="outer"))
    _PF_NUM = ("commission", "service", "transaction", "ams", "voucher", "ship3pl",
               "buyer_ship", "rebate", "other_ded", "refund", "jd", "ecms", "ad")
    for _c in _PF_NUM:
        _pf_shop[_c] = pd.to_numeric(_pf_shop.get(_c), errors="coerce").fillna(0.0)

    # --- NST 売上/粗利: 線下調整（品目 31109）を外して別掲 ---
    _dfp = df.copy()
    _is_off = _dfp["item_internal_id"].astype(str).str.strip() == _OFFLINE_ITEM
    _dfp["offline"] = _dfp["revenue"].where(_is_off, 0.0)
    for _c in ("revenue", "defined_cost", "gross_profit", "qty_sold"):
        _dfp[_c] = _dfp[_c].where(~_is_off, 0.0)

    _dim_lbl = {"shop": _col("shop"), "owner": _col("owner"), "market": _col("market")}
    _pf_dim = st.radio(("集計軸" if _ja_pf else "汇总维度"),
                       ["shop", "owner", "market"], horizontal=True,
                       format_func=lambda k: _dim_lbl[k], key="perf_dim")

    def _perf_table(dim: str) -> pd.DataFrame:
        keys = ["shop", "owner"] if dim == "shop" else [dim]
        g = _dfp.groupby(keys, as_index=False).agg(
            qty=("qty_sold", "sum"), revenue=("revenue", "sum"),
            defined_cost=("defined_cost", "sum"), gross_profit=("gross_profit", "sum"),
            offline=("offline", "sum"))
        g = _ns_round_money(g)
        g["offline"] = g["offline"].map(_rhu)
        if dim == "shop":
            m = g.merge(_pf_shop, on="shop", how="left")
        else:
            _map = _dfp[["shop", dim]].drop_duplicates()
            _fa = (_map.merge(_pf_shop, on="shop", how="inner")
                   .groupby(dim, as_index=False).agg(**{c: (c, "sum") for c in _PF_NUM}))
            m = g.merge(_fa, on=dim, how="left")
        for _c in _PF_NUM:
            m[_c] = pd.to_numeric(m.get(_c), errors="coerce").fillna(0.0).map(_rhu)
        m["ded_total"] = (m["commission"] + m["service"] + m["transaction"] + m["ams"]
                          + m["voucher"] + m["ship3pl"] + m["other_ded"]
                          - m["buyer_ship"] - m["rebate"])
        m["shop_gp"] = m["gross_profit"] - m["ded_total"]
        # Boss 2026-10-02: ECMS 輸送費も業績から引く（韓国向けは ECMS で出している）
        m["perf"] = m["shop_gp"] - m["jd"] - m["ecms"] - m["ad"]
        m["gross_margin"] = (m["gross_profit"] / m["revenue"].where(m["revenue"] != 0)).fillna(0) * 100
        m["perf_rate"] = (m["perf"] / m["revenue"].where(m["revenue"] != 0)).fillna(0) * 100
        return m.sort_values("perf", ascending=False)

    _pt = _perf_table(_pf_dim)

    # KPI（対象店舗の合計 · 率は合算後）
    _tot = {c: float(_pt[c].sum()) for c in
            ("revenue", "gross_profit", "ded_total", "shop_gp", "jd", "ecms", "ad", "perf", "refund", "offline")}
    k1, k2, k3, k4, k5, k6, k7, k8 = st.columns(8)
    k1.metric(("売上(NST·調整除く)" if _ja_pf else "销售额(NST·剔调整)"), f"¥{_tot['revenue']:,.0f}")
    k2.metric(_col("gross_profit"), f"¥{_tot['gross_profit']:,.0f}",
              (f"{_tot['gross_profit'] / _tot['revenue'] * 100:.2f}%" if _tot["revenue"] else "—"),
              delta_color="off")
    k3.metric(_col("ded_total"), f"¥{_tot['ded_total']:,.0f}")
    k4.metric(_col("shop_gp"), f"¥{_tot['shop_gp']:,.0f}")
    k5.metric(_col("jd"), f"¥{_tot['jd']:,.0f}")
    k6.metric(_col("ecms"), f"¥{_tot['ecms']:,.0f}")
    k7.metric(_col("ad"), f"¥{_tot['ad']:,.0f}")
    k8.metric(_col("perf"), f"¥{_tot['perf']:,.0f}",
              (f"{_tot['perf'] / _tot['revenue'] * 100:.2f}%" if _tot["revenue"] else "—"),
              delta_color="off")
    st.caption((f"別掲: 返金額 ¥{_tot['refund']:,.0f} · 線下調整 ¥{_tot['offline']:,.0f}（いずれも業績から引いていない）"
                if _ja_pf else
                f"单列: 退款金额 ¥{_tot['refund']:,.0f} · 线下调整 ¥{_tot['offline']:,.0f}（均未从绩效中扣除）"))

    _head = ("shop", "owner") if _pf_dim == "shop" else (_pf_dim,)
    _perf_cols = _head + (
        "qty", "revenue", "defined_cost", "gross_profit", "gross_margin",
        "commission", "service", "transaction", "ams", "voucher", "ship3pl",
        "buyer_ship", "rebate", "other_ded", "ded_total",
        "shop_gp", "jd", "ecms", "ad", "perf", "perf_rate",
        "refund", "offline")
    _pf_disp = _disp(_pt, _perf_cols)
    html_table(_pf_disp)
    st.download_button(
        "⬇️ CSV ダウンロード" if _ja_pf else "⬇️ 下载 CSV",
        _pf_disp.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"運営業績_{_pf_dim}_{ym}.csv", mime="text/csv", key="dl_perf_tab")
    if _cov_pct is not None and _cov_pct < 100:
        st.caption((f"⚠️ 手数料明細カバレッジ {_cov_pct:.1f}%（未突合注文の控除は 0 扱い → 業績が高く出る）"
                    if _ja_pf else
                    f"⚠️ 手续费明细覆盖率 {_cov_pct:.1f}%（未匹配订单扣减按 0 → 绩效偏高）"))

st.divider()
st.caption(
    t("対象月") + f"：{ym} · " + t("市場") + f"：{mk} · "
    + t("表示行（明細）: ") + f"{len(df):,}"
)
