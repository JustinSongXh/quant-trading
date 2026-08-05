"""全局信号总览页（issue #42 从 app.py 拆出）

`_scan_all_stocks` 目前仍住在这里：它拿 `st.session_state` 当扫描结果的缓存，
搬到 `analysis/` 需要先把缓存改成显式参数，那是有行为风险的重新设计（见 #42 边界）。

实时行情**按模块引用** `realtime.get_realtime_quotes`，不 from-import 那个函数：
页面模块只被 import 一次并常驻 `sys.modules`（拆分前的 `app.py` 是每次 rerun 重新执行
整个脚本、import 会重新绑），from-import 会把函数对象绑死，导致测试替 `data.realtime`
的替身打不进来——#42 拆分时这条真的把 `test_scope_change_triggers_rescan` 打红过。
"""

import streamlit as st
import pandas as pd
from data import realtime
from data.cache import is_during_trading
from strategy.signals import build_signals

from config.pool import load_stock_pool
from config.settings import DEFAULT_SIGNAL_SOURCE, SIGNAL_THRESHOLD, SIGNAL_LOOKBACK
from strategy.decision import decide, SOURCE_COLUMNS

from ui.shared import (
    SIGNAL_SOURCE_LABELS, get_recommendation, get_stock_data, market_label,
    signed_color,
)


def _overview_meta_row(code, name, stype, market, signal_df, rt):
    """总览页一行的展示字段（纯函数，便于测试）

    Args:
        signal_df: 带 close 列、DatetimeIndex 的信号表
        rt: 该标的的实时快照（get_realtime_quotes 的一项），取不到时为 None

    板块与个股走**同一条口径**（#32）：现价来自实时快照、昨收来自已收盘的最后一根
    bar、涨跌幅由这两者相除。板块此前单独用「日K最后两根收盘价」算，盘中日K还没有
    当日 bar，算出来的是上一交易日的涨跌（实测 2026-08-03 盘中消费电子显示 +3.49%
    ——那是 07-31 的涨幅，当日实为 +0.10%）。
    """
    from datetime import date as _date

    n = len(signal_df)
    # 收盘后 fetch 会补上当日 bar（板块经 _today_sector_bar_from_ths、个股经
    # _today_bar_from_realtime）；盘中当日 bar 会被剔除。
    has_today_bar = n >= 2 and signal_df.index[-1].date() == _date.today()

    # 昨收 = 最后一根**已收盘**的 bar，「日期」跟着昨收走——表头按 f"昨收({日期})"
    # 渲染，取成现价那根就会张冠李戴（板块此前就是这样，显示「昨收(07-31)」而那一
    # 格的值其实是 07-30 收盘）。
    prev_i = -2 if has_today_bar else -1
    prev_close = round(float(signal_df.iloc[prev_i]["close"]), 2)
    last_date = str(signal_df.index[prev_i].date())

    if rt and rt.get("price"):
        cur_price = round(float(rt["price"]), 2)
        # 板块快照自带昨收，用它而不是日K那根：两者同源才不会出现
        # 「现价==昨收 但涨跌幅≠0」的不一致（#26 在个股上踩过）
        snap_prev = rt.get("prev_close")
        if snap_prev:
            prev_close = round(float(snap_prev), 2)
    elif stype == "sector" and n >= 2:
        # 板块快照取不到，回落到日K最后两根收盘价——即改动前的行为，盘中会滞后到
        # 上一交易日，但比显示 0.00% 有信息量。日期仍跟着昨收那根走。
        cur_price = round(float(signal_df.iloc[-1]["close"]), 2)
        prev_close = round(float(signal_df.iloc[-2]["close"]), 2)
        last_date = str(signal_df.index[-2].date())
    else:
        # 个股/指数取不到实时价：现价按昨收显示，涨跌幅 0（与改动前一致）
        cur_price = prev_close

    change_pct = round((cur_price - prev_close) / prev_close * 100, 2) if prev_close else 0.0

    # 指数/板块 PE 无意义或口径不明，不展示
    pe_raw = rt.get("pe") if rt else None
    if stype in ("index", "sector") or pe_raw is None:
        pe_val = "-"
    else:
        pe_val = round(float(pe_raw), 2)

    return {
        "市场": market_label(stype, market), "股票": f"{name}({code})",
        "昨收": prev_close, "现价": cur_price, "涨跌幅": change_pct,
        "市盈率": pe_val,
        "日期": last_date, "_code": code, "_type": stype,
    }


# 总览页的扫描/显示范围（#28 评论）：类型 → 中文名。港股/A股 都归在「个股」里，


OVERVIEW_TYPE_LABELS = {"index": "指数", "sector": "板块", "stock": "个股"}
OVERVIEW_TYPES = tuple(OVERVIEW_TYPE_LABELS)
# 分组行数超过这个数就改用紧凑表格（一个 dataframe），不再逐行渲染 9 列 + 按钮
COMPACT_ROWS = 20


def _render_overview_compact(market_df, price_label, prev_label, sig_label, market_key):
    """行数多的分组：一个 dataframe + 一个「看详情」选择器（#28 评论）

    逐行版每行 9 个 st.columns + 1 个按钮，95 行 ≈ 950 个 widget，翻页本身就卡；
    紧凑版是 1 个 dataframe + 2 个 widget。代价是没有逐行按钮，所以下面配一个选择器
    ——「详情」这个入口不能因为改表格就消失。
    """
    show = pd.DataFrame({
        "标的": market_df["股票"],
        price_label: market_df["现价"],
        prev_label: market_df["昨收"],
        "涨跌幅%": market_df["涨跌幅"],
        "市盈率": market_df["市盈率"],
        sig_label: market_df["信号值"],
        "推荐": market_df["推荐"],
        "仓位": market_df["仓位建议"],
    })
    # 数值列可能混着 "-"（取数失败的行），先转数字再格式化，否则 Styler 会整列当字符串
    for col in ("涨跌幅%", sig_label):
        show[col] = pd.to_numeric(show[col], errors="coerce")
    st.dataframe(
        show.style.format({"涨跌幅%": "{:+.2f}", sig_label: "{:+.3f}"}, na_rep="—")
            .map(signed_color, subset=["涨跌幅%", sig_label])
            .map(lambda v: "color:#e74c3c;font-weight:bold" if v == "买入"
                 else "color:#27ae60;font-weight:bold" if v == "卖出" else "color:#999",
                 subset=["推荐"]),
        hide_index=True, use_container_width=True,
        height=min(560, 36 * len(show) + 40),
    )
    labels = list(market_df["股票"])
    c1, c2 = st.columns([3, 1])
    with c1:
        chosen = st.selectbox("看某个标的的详情", labels, key=f"ov_pick_{market_key}",
                              label_visibility="collapsed")
    row = market_df[market_df["股票"] == chosen].iloc[0]
    if c2.button("查看详情", key=f"ov_pick_btn_{market_key}"):
        st.session_state["page"] = "单股票分析"
        st.session_state["selected_code"] = row["_code"]
        st.session_state["selected_type"] = row.get("_type", "stock")
        st.session_state["auto_analyze"] = True
        st.rerun()
    st.caption(f"这一组超过 {COMPACT_ROWS} 个，已改用紧凑表格（逐行按钮会让页面卡）。"
               "要看某个标的用上面的选择器。")


def _scan_all_stocks(progress=None, source=DEFAULT_SIGNAL_SOURCE,
                      kronos_progress=None, force_refresh_data=False, types=None):
    """扫描股票池，计算指定 source 的信号；signal_df 按 source 缓存到 session_state

    `types`：只扫这些标的类型（`{"index","sector","stock"}` 的子集），None = 全扫。
    未选中的类型**连行情都不请求**——板块没有批量行情接口（#32），95 个板块逐个 THS 快照
    实测 0.74s/个 ≈ 70s，而个股+指数 26 个一次批量只要 0.2s。只想看个股的人不该等这一分多钟。
    """
    stocks = [s for s in load_stock_pool()
              if types is None or s.get("type", "stock") in types]
    quote_items = [(s["code"], s.get("type", "stock")) for s in stocks]
    quotes = realtime.get_realtime_quotes(quote_items)
    total = len(stocks)

    use_kronos = source == "kronos"
    sig_cache = st.session_state.setdefault("overview_signals", {})
    src_cache = sig_cache.setdefault(source, {})
    meta_cache = {}
    if not stocks:
        st.session_state["overview_meta"] = meta_cache
        return

    for i, stock in enumerate(stocks):
        code, name = stock["code"], stock["name"]
        if progress:
            kronos_hint = "（含 Kronos 预测）" if use_kronos else ""
            progress.progress((i + 1) / total, text=f"扫描 {name}({code}){kronos_hint}...")
        market = stock.get("market", "A")
        stype = stock.get("type", "stock")
        cache_id = f"{code}_{stype}"

        def _kronos_cb(cur, tot, msg):
            if kronos_progress:
                if cur < tot:
                    kronos_progress.progress(cur / tot, text=f"{name}: {msg}")
                else:
                    kronos_progress.empty()

        try:
            # 总览页只看最后一天信号，按 source 所需 buffer 拉数据
            df = get_stock_data(code, days=SIGNAL_LOOKBACK[source],
                                 force_refresh=force_refresh_data, stock_type=stype)
            signal_df = build_signals(df, symbol=code, sentiment_scores=None,
                                       enabled_signals=[source],
                                       progress_cb=_kronos_cb if use_kronos else None)
            src_cache[cache_id] = signal_df

            meta_cache[cache_id] = _overview_meta_row(
                code, name, stype, market, signal_df, quotes.get((code, stype)))
        except Exception:
            src_cache[cache_id] = None
            meta_cache[cache_id] = {
                "市场": market_label(stype, market), "股票": f"{name}({code})",
                "昨收": "-", "现价": "-", "涨跌幅": "-",
                "市盈率": "-",
                "日期": "-", "_code": code, "_type": stype,
            }

    st.session_state["overview_meta"] = meta_cache


def _build_overview_rows(source, buy_threshold=SIGNAL_THRESHOLD, sell_threshold=None):
    """读取指定 source 的信号缓存，按阈值生成展示行"""
    if sell_threshold is None:
        sell_threshold = -buy_threshold
    sig_cache = st.session_state.get("overview_signals", {}).get(source, {})
    meta_cache = st.session_state.get("overview_meta", {})
    src_col = SOURCE_COLUMNS[source]

    rows = []
    for cache_id, meta in meta_cache.items():
        signal_df = sig_cache.get(cache_id)
        if signal_df is None:
            rows.append({**meta, "信号值": "-", "推荐": "数据异常", "仓位建议": ""})
            continue

        fusion_result = decide(signal_df, source=source,
                                      buy_threshold=buy_threshold,
                                      sell_threshold=sell_threshold)
        last = signal_df.iloc[-1]
        last_strength = fusion_result["strength"].iloc[-1]
        rec_text, _ = get_recommendation(fusion_result["decision"].iloc[-1])

        from config.settings import POSITION_SIZING
        pos_label = ""
        if rec_text == "买入":
            for tier in POSITION_SIZING:
                if last_strength >= tier["min_strength"]:
                    pos_label = tier["label"]
                    break
        elif rec_text == "卖出":
            pos_label = "清仓"

        sig_val = round(float(last.get(src_col, 0) or 0), 3)
        rows.append({
            **meta,
            "信号值": sig_val,
            "推荐": rec_text,
            "仓位建议": pos_label,
        })
    return rows


def page_overview():
    st.title("A股港股量化分析小程序")
    st.subheader("全局信号总览")

    # 盘中判断
    a_trading = is_during_trading("000001")
    hk_trading = is_during_trading("00700")
    if a_trading or hk_trading:
        st.warning("当前为交易时段，数据为盘中快照，非收盘价。收盘后刷新页面获取最终数据。")

    # 信号源单选（总览页不含 Kronos：单只滚动预测在 CPU 上 ~40 秒，全池扫描太慢）
    st.sidebar.markdown("**信号源**")
    sources = [s for s in SIGNAL_SOURCE_LABELS.keys() if s != "kronos"]
    default_src = st.session_state.get("ov_source", DEFAULT_SIGNAL_SOURCE)
    if default_src not in sources:
        default_src = sources[0]
    source = st.sidebar.radio(
        "信号源",
        options=sources,
        format_func=lambda k: SIGNAL_SOURCE_LABELS[k],
        index=sources.index(default_src),
        key="ov_source",
        label_visibility="collapsed",
    )
    st.sidebar.caption("Kronos 在单股票分析页可选")
    st.sidebar.markdown("---")
    need_refresh = st.sidebar.button("刷新数据", type="primary")

    # ---- 扫描/显示范围（#28 评论）：未选中的类型既不扫描也不渲染 ----
    pool = load_stock_pool()
    counts = {}
    for s in pool:
        counts[s.get("type", "stock")] = counts.get(s.get("type", "stock"), 0) + 1
    labels = [f"{OVERVIEW_TYPE_LABELS[t]}（{counts.get(t, 0)}）" for t in OVERVIEW_TYPES]
    label_to_type = dict(zip(labels, OVERVIEW_TYPES))
    sel_labels = st.multiselect("扫描范围", labels, default=labels, key="ov_types",
                                help="去掉的类型既不扫描也不显示——收起来并不省时间，"
                                     "Streamlit 折叠区里的内容照样会渲染。")
    types = {label_to_type[l] for l in sel_labels if l in label_to_type}
    n_sector = counts.get("sector", 0) if "sector" in types else 0
    st.caption(f"个股/指数走一次批量行情（实测 26 个 0.2s）；**板块没有批量接口**"
               f"（#32），逐个 THS 快照实测 0.74s/个"
               + (f"，当前选了 {n_sector} 个板块 ≈ {n_sector * 0.74:.0f}s。"
                  if n_sector else "，当前未选板块。")
               + "只想看个股时把「板块」去掉，扫描会快一分多钟。")
    if not types:
        st.info("扫描范围是空的，请至少选一个类型。")
        return

    only_signal = st.checkbox("只看有信号的（|信号| ≥ 阈值）", value=False, key="ov_signal_only")

    # 股票池指纹：管理页加/删股票后回到总览，靠它触发自动重扫。
    # **范围也要进指纹**：否则勾上「板块」后不会重扫，页面上根本不出现板块。
    pool_fp = (tuple(sorted((s["code"], s.get("type", "stock")) for s in pool)),
               tuple(sorted(types)))
    pool_changed = st.session_state.get("overview_pool_fp") != pool_fp

    # radio 切换：当前 source 没缓存就重算（实际是"选了就重新计算"）
    sig_cache = st.session_state.get("overview_signals", {}).get(source, {})
    need_scan = (need_refresh or pool_changed or not sig_cache
                 or "overview_meta" not in st.session_state)

    if need_scan:
        progress = st.progress(0, text="正在扫描...")
        kronos_bar = st.empty() if source == "kronos" else None
        _scan_all_stocks(progress, source=source, kronos_progress=kronos_bar,
                          force_refresh_data=need_refresh, types=types)
        st.session_state["overview_pool_fp"] = pool_fp
        progress.empty()
        if kronos_bar:
            kronos_bar.empty()

    rows = _build_overview_rows(source)

    st.caption(f"当前信号源: **{SIGNAL_SOURCE_LABELS[source]}**（触发阈值 |signal| ≥ {SIGNAL_THRESHOLD}）")

    if not rows:
        st.warning("股票池为空")
        return

    result_df = pd.DataFrame(rows)

    # 分市场显示，每行带「查看详情」按钮
    for market_label, market_key in [("指数", "指数"), ("板块", "板块"), ("A股", "A股"), ("港股", "港股")]:
        market_df = result_df[result_df["市场"] == market_key]
        if market_df.empty:
            continue

        # 组头摘要：不展开也知道这组里有没有值得看的东西
        total_n = len(market_df)
        buy_n = int((market_df["推荐"] == "买入").sum())
        sell_n = int((market_df["推荐"] == "卖出").sum())
        summary = f"{total_n} 个 · 买入 {buy_n} · 卖出 {sell_n} · 观望 {total_n - buy_n - sell_n}"
        if only_signal:
            market_df = market_df[market_df["推荐"].isin(["买入", "卖出"])]
            summary += f" · **已筛出 {len(market_df)} 个有信号**"
        st.markdown(f"### {market_label}（{summary}）")
        if market_df.empty:
            st.caption("这一组没有触发信号的标的（去掉上方「只看有信号的」可看全部）。")
            continue

        # 表头：盘中显示"当前价"，收盘后显示"收盘价"（板块跟随 A 股时段）
        is_trading = hk_trading if market_key == "港股" else a_trading
        price_label = "当前价" if is_trading else "收盘价"

        # 昨收日期（从第一条数据取，所有股票同市场日期一样）
        sample = market_df.iloc[0] if len(market_df) > 0 else None
        prev_date = sample["日期"] if sample is not None else ""
        prev_label = f"昨收({prev_date[5:]})" if prev_date else "昨收"

        sig_label = f"{SIGNAL_SOURCE_LABELS[source]}信号"

        # 行数多的组改用紧凑表格：逐行版每行 9 个 columns + 1 个按钮，95 行 ≈ 950 个
        # widget，光渲染就卡（#28 评论）。≤20 行仍走逐行版，体感不变。
        if len(market_df) > COMPACT_ROWS:
            _render_overview_compact(market_df, price_label, prev_label, sig_label,
                                     market_key)
            continue

        col_ratios = [2.5, 1.2, 1.2, 1, 1, 1.4, 1, 1, 0.8]
        header_cols = st.columns(col_ratios)
        for col, title in zip(header_cols, ["股票", price_label, prev_label, "涨跌幅", "市盈率", sig_label, "推荐", "仓位", ""]):
            col.markdown(f"**{title}**")

        # 数据行
        for _, row in market_df.iterrows():
            cols = st.columns(col_ratios)
            cols[0].write(row["股票"])
            cols[1].write(row["现价"])
            cols[2].write(row["昨收"])

            # 涨跌幅带颜色
            chg = row["涨跌幅"]
            if isinstance(chg, (int, float)):
                chg_color = "#e74c3c" if chg > 0 else "#27ae60" if chg < 0 else "#666"
                chg_prefix = "+" if chg > 0 else ""
                cols[3].markdown(f'<span style="color:{chg_color};font-weight:bold">{chg_prefix}{chg}%</span>', unsafe_allow_html=True)
            else:
                cols[3].write(chg)

            # 市盈率
            cols[4].write(row.get("市盈率", "-"))

            # 信号值带颜色（按 SIGNAL_THRESHOLD 区分有/无信号）
            sig_val = row["信号值"]
            if isinstance(sig_val, (int, float)):
                if sig_val >= SIGNAL_THRESHOLD:
                    s_color = "#e74c3c"
                elif sig_val <= -SIGNAL_THRESHOLD:
                    s_color = "#27ae60"
                else:
                    s_color = "#666"
                cols[5].markdown(f'<span style="color:{s_color};font-weight:bold">{sig_val:.3f}</span>', unsafe_allow_html=True)
            else:
                cols[5].write(sig_val)

            # 推荐带颜色
            rec = row["推荐"]
            r_color = "#e74c3c" if rec == "买入" else "#27ae60" if rec == "卖出" else "#999"
            cols[6].markdown(f'<span style="color:{r_color};font-weight:bold">{rec}</span>', unsafe_allow_html=True)

            # 仓位建议
            pos = row.get("仓位建议", "")
            if pos:
                cols[7].markdown(f'<span style="color:{r_color};font-weight:bold">{pos}</span>', unsafe_allow_html=True)
            else:
                cols[7].write("")

            # 查看详情按钮
            if cols[8].button("详情", key=f"btn_{row['_code']}_{row.get('_type', 'stock')}"):
                st.session_state["page"] = "单股票分析"
                st.session_state["selected_code"] = row["_code"]
                st.session_state["selected_type"] = row.get("_type", "stock")
                st.session_state["auto_analyze"] = True
                st.rerun()

