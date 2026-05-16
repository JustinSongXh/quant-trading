"""Streamlit 可视化：全局信号总览 + 单股票详细分析"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd

from config.settings import (
    load_stock_pool, get_stock_name, get_stock_type, add_stock, remove_stock,
    DEFAULT_SIGNAL_SOURCE, SIGNAL_THRESHOLD, SIGNAL_LOOKBACK,
)
from data.fetcher import fetch_daily_kline, is_hk_stock, is_index
from data.stock_list import search_stocks, get_all_stocks
from data.realtime import get_realtime_quotes
from data.mock import fetch_mock_kline
from data.cache import save_kline, load_kline, is_cache_fresh, is_during_trading
from strategy.signals import build_signals
from strategy.fusion import fuse_signals, SOURCE_COLUMNS
from backtest.engine import run_backtest
from backtest.metrics import calc_metrics
from analysis.chanlun import analyze as chanlun_analyze

st.set_page_config(page_title="A股港股量化分析小程序", layout="wide")

# 移动端适配 CSS
st.markdown("""
<style>
@media (max-width: 768px) {
    /* 侧边栏默认收起 */
    [data-testid="stSidebar"] { min-width: 0; width: 0; }
    /* 主内容区不留侧边栏间距 */
    .main .block-container { padding-left: 1rem; padding-right: 1rem; max-width: 100%; }
    /* 表格行可横向滚动 */
    [data-testid="stHorizontalBlock"] { overflow-x: auto; flex-wrap: nowrap !important; }
    [data-testid="stHorizontalBlock"] > div { min-width: 80px; flex-shrink: 0; }
    /* 标题字号缩小 */
    h1 { font-size: 1.5rem !important; }
    h2, h3 { font-size: 1.2rem !important; }
}
</style>
""", unsafe_allow_html=True)


# ========== 工具函数 ==========

def get_stock_data(code, days=365, force_refresh=False, stock_type=None):
    """获取股票数据，优先用新鲜缓存，过期或缓存天数不够则刷新"""
    # 指数用带 type 前缀的 cache key，避免和同代码个股冲突
    cache_key = f"idx_{code}" if stock_type == "index" else code
    if not force_refresh and is_cache_fresh(cache_key):
        df = load_kline(cache_key)
        # 缓存命中且行数足够才直接返回；否则走刷新分支补足天数
        if df is not None and len(df) >= days:
            return df
    try:
        df = fetch_daily_kline(code, days=days, stock_type=stock_type)
        save_kline(cache_key, df)
        return df
    except Exception:
        df = load_kline(cache_key)
        if df is not None:
            return df
        return fetch_mock_kline(code)


def get_recommendation(decision):
    """根据决策值返回推荐文案和颜色"""
    if decision == 1:
        return "买入", "#e74c3c"
    elif decision == -1:
        return "卖出", "#27ae60"
    return "观望", "#999999"


# ========== 全局信号总览页 ==========

SIGNAL_SOURCE_LABELS = {"technical": "技术指标", "chanlun": "缠论", "kronos": "Kronos 模型"}


def _scan_all_stocks(progress=None, source=DEFAULT_SIGNAL_SOURCE,
                      kronos_progress=None, force_refresh_data=False):
    """扫描股票池，计算指定 source 的信号；signal_df 按 source 缓存到 session_state"""
    stocks = load_stock_pool()
    all_codes = [s["code"] for s in stocks]
    type_map = {s["code"]: s.get("type", "stock") for s in stocks}
    realtime = get_realtime_quotes(all_codes, type_map=type_map)
    total = len(stocks)

    use_kronos = source == "kronos"
    sig_cache = st.session_state.setdefault("overview_signals", {})
    src_cache = sig_cache.setdefault(source, {})
    meta_cache = {}

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

            from datetime import date as _date
            last = signal_df.iloc[-1]
            if signal_df.index[-1].date() == _date.today() and len(signal_df) >= 2:
                prev_close = round(float(signal_df.iloc[-2]["close"]), 2)
                last_date = str(signal_df.index[-2].date())
            else:
                prev_close = round(float(last["close"]), 2)
                last_date = str(signal_df.index[-1].date())
            rt = realtime.get(code)
            cur_price = round(float(rt["price"]), 2) if rt else prev_close
            # 直接用现价/昨收算涨跌幅，避免非交易日 realtime 返回上一交易日快照导致
            # "现价==昨收 但涨跌幅≠0" 的不一致
            change_pct = round((cur_price - prev_close) / prev_close * 100, 2) if prev_close else 0.0
            market_label = "指数" if stype == "index" else ("港股" if market == "HK" else "A股")

            meta_cache[cache_id] = {
                "市场": market_label, "股票": f"{name}({code})",
                "昨收": prev_close, "现价": cur_price, "涨跌幅": change_pct,
                "日期": last_date, "_code": code, "_type": stype,
            }
        except Exception:
            market_label = "指数" if stype == "index" else ("港股" if market == "HK" else "A股")
            src_cache[cache_id] = None
            meta_cache[cache_id] = {
                "市场": market_label, "股票": f"{name}({code})",
                "昨收": "-", "现价": "-", "涨跌幅": "-",
                "日期": "-", "_code": code, "_type": stype,
            }

    st.session_state["overview_meta"] = meta_cache


def _build_overview_rows(source):
    """读取指定 source 的信号缓存，按阈值生成展示行"""
    sig_cache = st.session_state.get("overview_signals", {}).get(source, {})
    meta_cache = st.session_state.get("overview_meta", {})
    src_col = SOURCE_COLUMNS[source]

    rows = []
    for cache_id, meta in meta_cache.items():
        signal_df = sig_cache.get(cache_id)
        if signal_df is None:
            rows.append({**meta, "信号值": "-", "推荐": "数据异常", "仓位建议": ""})
            continue

        fusion_result = fuse_signals(signal_df, source=source)
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

    # radio 切换：当前 source 没缓存就重算（实际是"选了就重新计算"）
    sig_cache = st.session_state.get("overview_signals", {}).get(source, {})
    need_scan = need_refresh or not sig_cache or "overview_meta" not in st.session_state

    if need_scan:
        progress = st.progress(0, text="正在扫描...")
        kronos_bar = st.empty() if source == "kronos" else None
        _scan_all_stocks(progress, source=source, kronos_progress=kronos_bar,
                          force_refresh_data=need_refresh)
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
    for market_label, market_key in [("指数", "指数"), ("A股", "A股"), ("港股", "港股")]:
        market_df = result_df[result_df["市场"] == market_key]
        if market_df.empty:
            continue

        st.markdown(f"### {market_label}")

        # 表头：盘中显示"当前价"，收盘后显示"收盘价"
        is_trading = a_trading if market_key == "A股" else hk_trading
        price_label = "当前价" if is_trading else "收盘价"

        # 昨收日期（从第一条数据取，所有股票同市场日期一样）
        sample = market_df.iloc[0] if len(market_df) > 0 else None
        prev_date = sample["日期"] if sample is not None else ""
        prev_label = f"昨收({prev_date[5:]})" if prev_date else "昨收"

        sig_label = f"{SIGNAL_SOURCE_LABELS[source]}信号"
        col_ratios = [2.5, 1.2, 1.2, 1, 1.4, 1, 1, 0.8]
        header_cols = st.columns(col_ratios)
        for col, title in zip(header_cols, ["股票", price_label, prev_label, "涨跌幅", sig_label, "推荐", "仓位", ""]):
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

            # 信号值带颜色（按 SIGNAL_THRESHOLD 区分有/无信号）
            sig_val = row["信号值"]
            if isinstance(sig_val, (int, float)):
                if sig_val >= SIGNAL_THRESHOLD:
                    s_color = "#e74c3c"
                elif sig_val <= -SIGNAL_THRESHOLD:
                    s_color = "#27ae60"
                else:
                    s_color = "#666"
                cols[4].markdown(f'<span style="color:{s_color};font-weight:bold">{sig_val:.3f}</span>', unsafe_allow_html=True)
            else:
                cols[4].write(sig_val)

            # 推荐带颜色
            rec = row["推荐"]
            r_color = "#e74c3c" if rec == "买入" else "#27ae60" if rec == "卖出" else "#999"
            cols[5].markdown(f'<span style="color:{r_color};font-weight:bold">{rec}</span>', unsafe_allow_html=True)

            # 仓位建议
            pos = row.get("仓位建议", "")
            if pos:
                cols[6].markdown(f'<span style="color:{r_color};font-weight:bold">{pos}</span>', unsafe_allow_html=True)
            else:
                cols[6].write("")

            # 查看详情按钮
            if cols[7].button("详情", key=f"btn_{row['_code']}_{row.get('_type', 'stock')}"):
                st.session_state["page"] = "单股票分析"
                st.session_state["selected_code"] = row["_code"]
                st.session_state["selected_type"] = row.get("_type", "stock")
                st.session_state["auto_analyze"] = True
                st.rerun()


# ========== 单股票详细分析页 ==========

def page_detail():
    st.title("A股港股量化分析小程序")

    # 侧边栏：按市场分组选股
    stocks = load_stock_pool()
    idx_stocks = [s for s in stocks if s.get("type") == "index"]
    a_stocks = [s for s in stocks if s.get("market", "A") == "A" and s.get("type") != "index"]
    hk_stocks = [s for s in stocks if s.get("market") == "HK"]

    # 从 session_state 读取跳转过来的股票
    preselected_code = st.session_state.get("selected_code", None)
    preselected_type = st.session_state.get("selected_type", None)
    auto_analyze = st.session_state.pop("auto_analyze", False)

    # 判断预选股票所在市场
    default_market = "A股"
    if preselected_type == "index":
        default_market = "指数"
    elif preselected_code and is_hk_stock(preselected_code):
        default_market = "港股"

    market_tabs = ["A股", "港股"]
    if idx_stocks:
        market_tabs = ["指数"] + market_tabs
    market_tab = st.sidebar.radio("市场", market_tabs, horizontal=True,
                                   index=market_tabs.index(default_market) if default_market in market_tabs else 0)
    if market_tab == "指数":
        pool = idx_stocks
    elif market_tab == "港股":
        pool = hk_stocks
    else:
        pool = a_stocks
    stock_options = {f"{s['name']}({s['code']})": s['code'] for s in pool}
    # 建立 label -> stock 的映射，用于获取 type
    stock_by_label = {f"{s['name']}({s['code']})": s for s in pool}

    if not stock_options:
        st.sidebar.warning(f"{market_tab}股票池为空")
        return

    # 预选股票在当前市场中的索引
    default_idx = 0
    if preselected_code:
        option_codes = list(stock_options.values())
        if preselected_code in option_codes:
            default_idx = option_codes.index(preselected_code)

    selected = st.sidebar.selectbox("选择股票", list(stock_options.keys()), index=default_idx)
    symbol = stock_options[selected]
    stype = stock_by_label.get(selected, {}).get("type", "stock")
    name = get_stock_name(symbol, stock_type=stype)

    time_range = st.sidebar.selectbox("回测时间范围", ["近3个月", "近6个月", "近1年", "近2年"], index=2)
    time_range_days = {"近3个月": 90, "近6个月": 180, "近1年": 365, "近2年": 730}[time_range]

    # 信号源单选
    st.sidebar.markdown("---")
    st.sidebar.markdown("**信号源**")
    sources = list(SIGNAL_SOURCE_LABELS.keys())
    default_src = st.session_state.get("dt_source", DEFAULT_SIGNAL_SOURCE)
    if default_src not in sources:
        default_src = DEFAULT_SIGNAL_SOURCE
    source = st.sidebar.radio(
        "信号源",
        options=sources,
        format_func=lambda k: SIGNAL_SOURCE_LABELS[k],
        index=sources.index(default_src),
        key="dt_source",
        label_visibility="collapsed",
    )
    st.sidebar.markdown("---")

    button_pressed = st.sidebar.button("刷新", type="primary")

    # 触发分析：按钮、首次自动、selection 变化都会重算
    key = (symbol, stype, time_range, source)
    last_key = st.session_state.get("detail_last_key")
    need_analyze = button_pressed or auto_analyze or last_key is None or last_key != key

    if need_analyze:
        kronos_progress = st.empty() if source == "kronos" else None
        def _detail_progress(cur, total, msg):
            if kronos_progress:
                if cur < total:
                    kronos_progress.progress(cur / total, text=msg)
                else:
                    kronos_progress.empty()

        with st.spinner(f"正在分析 {name}({symbol})..."):
            # 显示 time_range_days 天 + 该 source 所需 buffer，使每一显示日都有充足上下文
            buffer_days = SIGNAL_LOOKBACK[source]
            df = get_stock_data(symbol, days=time_range_days + buffer_days,
                                 force_refresh=button_pressed, stock_type=stype)

            # 全量信号 df（含 buffer，用于图表/缠论/当前信号展示）
            signal_df_full = build_signals(df, symbol=symbol, sentiment_scores=None,
                                            enabled_signals=[source],
                                            progress_cb=_detail_progress if kronos_progress else None)
            fusion_full = fuse_signals(signal_df_full, source=source)

            # 回测 df：严格裁到 time_range_days 范围
            cutoff = signal_df_full.index.max() - pd.Timedelta(days=time_range_days)
            signal_df_bt = signal_df_full.loc[signal_df_full.index >= cutoff]
            fusion_bt = fusion_full.loc[fusion_full.index >= cutoff]

            decisions = fusion_full["decision"]  # 用于图表展示决策点
            result = run_backtest(symbol, signal_df_bt, fusion_bt, stock_type=stype)
            metrics = calc_metrics(result["net_values"], result["initial_capital"])
            trades = result["trade_log"]
            chan_result = chanlun_analyze(df, symbol)

        st.session_state["detail_last_key"] = key
        st.session_state["detail_last_result"] = {
            "signal_df_full": signal_df_full, "fusion_full": fusion_full,
            "signal_df_bt": signal_df_bt, "fusion_bt": fusion_bt,
            "decisions": decisions, "result": result, "metrics": metrics,
            "trades": trades, "chan_result": chan_result,
        }

    # 渲染：用最近一次分析结果（key 不变则复用，避免每次 rerun 重算）
    cached = st.session_state.get("detail_last_result")
    if cached and st.session_state.get("detail_last_key") == key:
        # signal_df = 全量（含 buffer），用于图表/当前信号展示
        # signal_df_bt = 严格 time_range，用于回测和净值曲线
        signal_df = cached["signal_df_full"]
        fusion_result = cached["fusion_full"]
        signal_df_bt = cached["signal_df_bt"]
        decisions = cached["decisions"]
        result = cached["result"]
        metrics = cached["metrics"]
        trades = cached["trades"]
        chan_result = cached["chan_result"]

        # ========== 绩效指标 ==========
        st.subheader(f"{name}({symbol}) 回测结果（{time_range}）")
        cols = st.columns(6)
        labels = ["总收益", "年化收益", "最大回撤", "夏普比率", "胜率", "交易天数"]
        keys = ["total_return", "annual_return", "max_drawdown", "sharpe_ratio", "win_rate", "trading_days"]
        for col, label, key in zip(cols, labels, keys):
            val = metrics.get(key, 0)
            if key in ("total_return", "annual_return", "max_drawdown", "win_rate"):
                col.metric(label, f"{val}%")
            else:
                col.metric(label, val)

        # ========== 当前信号推荐 ==========
        st.subheader("当前信号推荐")
        last = signal_df.iloc[-1]
        last_date = str(signal_df.index[-1].date())
        last_close = last["close"]
        last_decision = decisions.iloc[-1]
        last_strength = fusion_result["strength"].iloc[-1]
        rec_text, rec_color = get_recommendation(last_decision)

        src_col = SOURCE_COLUMNS[source]
        signal_val = float(last.get(src_col, 0) or 0)
        signal_color = "#e74c3c" if signal_val >= SIGNAL_THRESHOLD else "#27ae60" if signal_val <= -SIGNAL_THRESHOLD else "#666"

        # 仓位建议
        pos_label = ""
        pos_pct = ""
        if rec_text == "买入":
            from config.settings import POSITION_SIZING
            for tier in POSITION_SIZING:
                if last_strength >= tier["min_strength"]:
                    pos_label = tier["label"]
                    pos_pct = f"{int(tier['position_pct'] * 100)}%"
                    break
        elif rec_text == "卖出":
            pos_label = "清仓"

        pos_display = f'<span style="font-size:16px;color:#666;margin-left:20px">仓位建议: </span><span style="font-size:20px;font-weight:bold;color:{rec_color}">{pos_label} ({pos_pct})</span>' if pos_label else ""

        st.markdown(
            f'<div style="background:#f8f9fa;border-radius:8px;padding:16px 24px;margin-bottom:16px">'
            f'<span style="font-size:16px;color:#666">{SIGNAL_SOURCE_LABELS[source]}（{last_date}）</span>'
            f'<span style="font-size:32px;font-weight:bold;color:{rec_color};margin-left:20px">{rec_text}</span>'
            f'<span style="font-size:16px;color:#666;margin-left:20px">收盘价 {last_close:.2f}</span>'
            f'<span style="font-size:16px;color:#666;margin-left:20px">信号值 </span>'
            f'<span style="font-size:20px;font-weight:bold;color:{signal_color}">{signal_val:.3f}</span>'
            f'<span style="font-size:13px;color:#999;margin-left:6px">阈值 ±{SIGNAL_THRESHOLD}</span>'
            f'{pos_display}'
            f'</div>',
            unsafe_allow_html=True,
        )

        # 各策略明细：按当前 source 分别显示
        if source == "technical":
            detail_cols = st.columns(5)

            macd_dif = last.get("macd_dif", 0)
            macd_dea = last.get("macd_dea", 0)
            macd_hist = last.get("macd_hist", 0)
            macd_rec = "多头（DIF>DEA）" if macd_dif > macd_dea else "空头（DIF<DEA）" if macd_dif < macd_dea else "中性"
            detail_cols[0].markdown(f"**MACD**\n\nDIF: {macd_dif:.3f}\n\nDEA: {macd_dea:.3f}\n\n柱: {macd_hist:.3f}\n\n判定: **{macd_rec}**")

            rsi_val = last.get("rsi", 50)
            rsi_rec = "超卖 → 买入机会" if rsi_val < 30 else "超买 → 卖出机会" if rsi_val > 70 else "偏弱" if rsi_val < 45 else "偏强" if rsi_val > 55 else "中性"
            detail_cols[1].markdown(f"**RSI({rsi_val:.1f})**\n\n判定: **{rsi_rec}**")

            kdj_k, kdj_d, kdj_j = last.get("kdj_k", 50), last.get("kdj_d", 50), last.get("kdj_j", 50)
            if kdj_k > kdj_d and kdj_j < 20: kdj_rec = "超卖金叉 → 强买"
            elif kdj_k > kdj_d: kdj_rec = "金叉 → 偏多"
            elif kdj_k < kdj_d and kdj_j > 80: kdj_rec = "超买死叉 → 强卖"
            elif kdj_k < kdj_d: kdj_rec = "死叉 → 偏空"
            else: kdj_rec = "中性"
            detail_cols[2].markdown(f"**KDJ**\n\nK: {kdj_k:.1f}  D: {kdj_d:.1f}  J: {kdj_j:.1f}\n\n判定: **{kdj_rec}**")

            boll_upper, boll_mid, boll_lower = last.get("boll_upper", 0), last.get("boll_mid", 0), last.get("boll_lower", 0)
            boll_rec = "触及上轨 → 卖出" if last_close >= boll_upper else "触及下轨 → 买入" if last_close <= boll_lower else "中轨上方 → 偏多" if last_close > boll_mid else "中轨下方 → 偏空"
            detail_cols[3].markdown(f"**布林带**\n\n上轨: {boll_upper:.2f}\n\n中轨: {boll_mid:.2f}\n\n下轨: {boll_lower:.2f}\n\n判定: **{boll_rec}**")

            st_dir = last.get("st_direction", 0)
            st_val = last.get("st_value", 0)
            st_rec = "多头趋势" if st_dir == 1 else "空头趋势" if st_dir == -1 else "未知"
            detail_cols[4].markdown(f"**SuperTrend**\n\n方向: {'多头' if st_dir == 1 else '空头'}\n\n轨道: {st_val:.2f}\n\n判定: **{st_rec}**")

        elif source == "chanlun":
            chan_buys_all = chan_result["buy_points"]
            chan_sells_all = chan_result["sell_points"]
            all_chan_points = [(bp["dt"], "买", bp["type"], bp["price"]) for bp in chan_buys_all] + \
                              [(sp["dt"], "卖", sp["type"], sp["price"]) for sp in chan_sells_all]
            all_chan_points.sort(key=lambda x: x[0])

            recent_buy = next((p for p in reversed(all_chan_points) if p[1] == "买"), None)
            recent_sell = next((p for p in reversed(all_chan_points) if p[1] == "卖"), None)

            chan_cols = st.columns(4)
            chan_cols[0].markdown(f"**分型 / 笔 / 中枢**\n\n分型: {len(chan_result['czsc'].fx_list)}\n\n笔: {len(chan_result['bi_list'])}\n\n中枢: {len(chan_result['zs_list'])}")
            chan_cols[1].markdown(f"**买卖点统计**\n\n买点: {len(chan_buys_all)}\n\n卖点: {len(chan_sells_all)}")
            if recent_buy:
                chan_cols[2].markdown(f"**最近买点**\n\n{recent_buy[2]} @ {recent_buy[3]:.2f}\n\n{str(recent_buy[0].date())}")
            else:
                chan_cols[2].markdown("**最近买点**\n\n无")
            if recent_sell:
                chan_cols[3].markdown(f"**最近卖点**\n\n{recent_sell[2]} @ {recent_sell[3]:.2f}\n\n{str(recent_sell[0].date())}")
            else:
                chan_cols[3].markdown("**最近卖点**\n\n无")

        elif source == "kronos":
            from analysis.kronos_pred import KRONOS_SATURATE_PCT
            kronos_cols = st.columns(3)
            pred_pct = signal_val * KRONOS_SATURATE_PCT * 100  # 反推预测涨跌幅（%）
            sat_pct = KRONOS_SATURATE_PCT * 100
            kronos_cols[0].markdown(f"**最新信号**\n\n值: **{signal_val:.3f}**\n\n阈值: ±{SIGNAL_THRESHOLD}")
            kronos_cols[1].markdown(f"**预测未来 5 日**\n\n累计涨跌 ≈ **{pred_pct:+.2f}%**\n\n（±{sat_pct:.0f}% 为满分）")
            kronos_cols[2].markdown(f"**预测频率**\n\n每 5 个交易日滚动\n\n上下文 400 日")

        st.markdown("---")

        # ========== 图表（动态行数，跟 source 走） ==========
        sig_panel_title = f"{SIGNAL_SOURCE_LABELS[source]}信号 + 决策"

        # 动态面板列表：(key, title, height, yaxis_title)
        panels = [
            ("kline", "K线 & 买卖点", 0.36, "价格"),
            ("volume", "成交量", 0.10, "成交量"),
        ]
        if source == "technical":
            panels.append(("macd", "MACD", 0.13, "MACD"))
            panels.append(("rsi_kdj", "RSI & KDJ", 0.13, "RSI/KDJ"))
        panels.append(("signal", sig_panel_title, 0.13, "信号值"))
        panels.append(("nv", "净值曲线", 0.15, "净值"))

        n_rows = len(panels)
        row_heights = [p[2] for p in panels]
        titles = [p[1] for p in panels]
        # 归一化 row_heights
        total_h = sum(row_heights)
        row_heights = [h / total_h for h in row_heights]
        panel_row = {p[0]: i + 1 for i, p in enumerate(panels)}

        fig = make_subplots(
            rows=n_rows, cols=1, shared_xaxes=True, vertical_spacing=0.025,
            row_heights=row_heights, subplot_titles=titles,
        )

        # ---- K线 + 均线 + BOLL + SuperTrend ----
        r = panel_row["kline"]
        fig.add_trace(go.Candlestick(x=signal_df.index, open=signal_df["open"], high=signal_df["high"], low=signal_df["low"], close=signal_df["close"], name="K线", increasing_line_color="#e74c3c", decreasing_line_color="#27ae60"), row=r, col=1)

        if source == "technical":
            for ma_col, color in {"ma_5": "#2196F3", "ma_10": "#FF9800", "ma_20": "#E91E63", "ma_60": "#9C27B0"}.items():
                if ma_col in signal_df.columns:
                    fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df[ma_col], name=ma_col.upper(), line=dict(width=1, color=color)), row=r, col=1)
            if "boll_upper" in signal_df.columns:
                fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["boll_upper"], name="BOLL上轨", line=dict(width=1, color="rgba(150,150,150,0.5)", dash="dot")), row=r, col=1)
                fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["boll_lower"], name="BOLL下轨", line=dict(width=1, color="rgba(150,150,150,0.5)", dash="dot"), fill="tonexty", fillcolor="rgba(150,150,150,0.05)"), row=r, col=1)
            if "st_value" in signal_df.columns:
                fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["st_value"], name="SuperTrend", mode="lines", line=dict(width=2, color="#FF6F00", dash="dash")), row=r, col=1)

        # ---- 回测交易点 + 回测起点辅助线 ----
        buys = [t for t in trades if t["action"] == "BUY"]
        sells = [t for t in trades if t["action"] == "SELL"]
        if buys:
            fig.add_trace(go.Scatter(x=[t["date"] for t in buys], y=[t["price"] for t in buys], mode="markers+text", marker=dict(symbol="triangle-up", size=16, color="#e74c3c", line=dict(width=1, color="white")), text=["B"]*len(buys), textposition="bottom center", textfont=dict(size=10, color="#e74c3c"), name=f"买入({len(buys)}次)", hovertext=[f"买入 {t['shares']}股 @ {t['price']:.2f}" for t in buys], hoverinfo="text+x"), row=r, col=1)
        if sells:
            fig.add_trace(go.Scatter(x=[t["date"] for t in sells], y=[t["price"] for t in sells], mode="markers+text", marker=dict(symbol="triangle-down", size=16, color="#27ae60", line=dict(width=1, color="white")), text=["S"]*len(sells), textposition="top center", textfont=dict(size=10, color="#27ae60"), name=f"卖出({len(sells)}次)", hovertext=[f"卖出 {t['shares']}股 @ {t['price']:.2f}" for t in sells], hoverinfo="text+x"), row=r, col=1)

        # 回测起点辅助线（buffer 与 time_range 分界）
        # 用 Scatter 而非 add_vline：后者对 pd.Timestamp 做 +/- 时会撞 pandas 2.x 报错；
        # add_shape 配合 yref=paper + row/col 会污染 y 轴自动范围，导致 K 线被压扁。
        bt_start = signal_df_bt.index[0] if len(signal_df_bt) else None
        if bt_start is not None and bt_start > signal_df.index[0]:
            y_lo = float(signal_df["low"].min())
            y_hi = float(signal_df["high"].max())
            fig.add_trace(go.Scatter(
                x=[bt_start, bt_start], y=[y_lo, y_hi],
                mode="lines", line=dict(color="#888", width=1, dash="dash"),
                name="回测起点", hovertext="回测起点", hoverinfo="text+x",
                showlegend=True,
            ), row=r, col=1)

        # ---- 缠论买卖点（仅 chanlun 源） ----
        chan_buys = chan_result["buy_points"]
        chan_sells = chan_result["sell_points"]
        if source == "chanlun":
            if chan_buys:
                fig.add_trace(go.Scatter(x=[bp["dt"] for bp in chan_buys], y=[bp["price"] for bp in chan_buys], mode="markers", marker=dict(symbol="diamond", size=12, color="#FF6F00", line=dict(width=1, color="white")), name=f"缠论买点({len(chan_buys)})", hovertext=[f"{bp['type']}: {bp['reason']}" for bp in chan_buys], hoverinfo="text+x"), row=r, col=1)
            if chan_sells:
                fig.add_trace(go.Scatter(x=[sp["dt"] for sp in chan_sells], y=[sp["price"] for sp in chan_sells], mode="markers", marker=dict(symbol="diamond", size=12, color="#7B1FA2", line=dict(width=1, color="white")), name=f"缠论卖点({len(chan_sells)})", hovertext=[f"{sp['type']}: {sp['reason']}" for sp in chan_sells], hoverinfo="text+x"), row=r, col=1)

        # ---- 成交量 ----
        if "volume" in signal_df.columns:
            r_vol = panel_row["volume"]
            vol_colors = ["#e74c3c" if c >= o else "#27ae60" for c, o in zip(signal_df["close"], signal_df["open"])]
            fig.add_trace(go.Bar(x=signal_df.index, y=signal_df["volume"], marker_color=vol_colors, name="成交量", showlegend=False), row=r_vol, col=1)

        # ---- 技术指标面板（MACD / RSI / KDJ）：仅 technical 源 ----
        if source == "technical":
            r_macd = panel_row["macd"]
            if "macd_dif" in signal_df.columns:
                fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["macd_dif"], name="DIF", line=dict(color="#2196F3", width=1.5)), row=r_macd, col=1)
                fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["macd_dea"], name="DEA", line=dict(color="#FF9800", width=1.5)), row=r_macd, col=1)
                macd_colors = ["#e74c3c" if v >= 0 else "#27ae60" for v in signal_df["macd_hist"]]
                fig.add_trace(go.Bar(x=signal_df.index, y=signal_df["macd_hist"], marker_color=macd_colors, name="MACD柱", showlegend=False), row=r_macd, col=1)
                fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=r_macd, col=1)

            r_rk = panel_row["rsi_kdj"]
            if "rsi" in signal_df.columns:
                fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["rsi"], name="RSI", line=dict(color="#9C27B0", width=1.5)), row=r_rk, col=1)
                fig.add_hline(y=70, line_dash="dot", line_color="#e74c3c", line_width=0.5, row=r_rk, col=1, annotation_text="超买70", annotation_position="bottom right")
                fig.add_hline(y=30, line_dash="dot", line_color="#27ae60", line_width=0.5, row=r_rk, col=1, annotation_text="超卖30", annotation_position="top right")
            if "kdj_k" in signal_df.columns:
                fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["kdj_k"], name="KDJ-K", line=dict(color="#2196F3", width=1)), row=r_rk, col=1)
                fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["kdj_d"], name="KDJ-D", line=dict(color="#FF9800", width=1)), row=r_rk, col=1)
                fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["kdj_j"], name="KDJ-J", line=dict(color="#E91E63", width=1, dash="dot")), row=r_rk, col=1)

        # ---- 信号 + 决策面板 ----
        r_sig = panel_row["signal"]
        sig_series = signal_df[SOURCE_COLUMNS[source]]
        if source == "technical":
            fig.add_trace(go.Scatter(x=signal_df.index, y=sig_series, name=f"{SIGNAL_SOURCE_LABELS[source]}信号", line=dict(color="#2196F3", width=1.5), fill="tozeroy", fillcolor="rgba(33,150,243,0.1)"), row=r_sig, col=1)
        else:
            fig.add_trace(go.Scatter(x=signal_df.index, y=sig_series, name=f"{SIGNAL_SOURCE_LABELS[source]}信号", mode="markers+lines", line=dict(color="#FF6F00", width=1), marker=dict(size=4)), row=r_sig, col=1)
        decision_colors = ["#e74c3c" if d == 1 else "#27ae60" if d == -1 else "rgba(0,0,0,0)" for d in decisions]
        decision_sizes = [12 if d != 0 else 0 for d in decisions]
        fig.add_trace(go.Scatter(x=signal_df.index, y=decisions, name="决策", mode="markers", marker=dict(size=decision_sizes, color=decision_colors, symbol=["triangle-up" if d == 1 else "triangle-down" if d == -1 else "circle" for d in decisions])), row=r_sig, col=1)
        fig.add_hline(y=SIGNAL_THRESHOLD, line_dash="dot", line_color="#e74c3c", line_width=0.5, row=r_sig, col=1, annotation_text=f"+{SIGNAL_THRESHOLD}", annotation_position="top right")
        fig.add_hline(y=-SIGNAL_THRESHOLD, line_dash="dot", line_color="#27ae60", line_width=0.5, row=r_sig, col=1, annotation_text=f"-{SIGNAL_THRESHOLD}", annotation_position="bottom right")
        fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=r_sig, col=1)

        # ---- 净值曲线 ----
        r_nv = panel_row["nv"]
        nv = result["net_values"]
        if not nv.empty:
            fig.add_trace(go.Scatter(x=nv.index, y=nv["net_value"], name="净值", line=dict(color="#3498db", width=2), fill="tozeroy", fillcolor="rgba(52,152,219,0.1)"), row=r_nv, col=1)
            fig.add_hline(y=result["initial_capital"], line_dash="dash", line_color="gray", row=r_nv, col=1)

        # 总高度按面板数动态调整
        fig.update_layout(height=max(800, 220 * n_rows),
                           xaxis_rangeslider_visible=False,
                           legend=dict(orientation="h", yanchor="bottom", y=1.01, font=dict(size=10)),
                           margin=dict(l=60, r=20, t=40, b=20))
        for p in panels:
            fig.update_yaxes(title_text=p[3], row=panel_row[p[0]], col=1)
        for i in range(1, n_rows + 1):
            fig.update_xaxes(showticklabels=True, tickformat="%y/%m", dtick="M2", row=i, col=1)

        st.plotly_chart(fig, use_container_width=True)

        # ========== 缠论买卖点明细：仅 chanlun 源显示 ==========
        if source == "chanlun" and (chan_buys or chan_sells):
            st.subheader("缠论买卖点明细")
            points = []
            for bp in chan_buys:
                points.append({"时间": str(bp["dt"]), "类型": bp["type"], "方向": "买", "价格": f"{bp['price']:.2f}", "原因": bp["reason"]})
            for sp in chan_sells:
                points.append({"时间": str(sp["dt"]), "类型": sp["type"], "方向": "卖", "价格": f"{sp['price']:.2f}", "原因": sp["reason"]})
            st.dataframe(pd.DataFrame(points), use_container_width=True, hide_index=True)

        # ========== 交易明细表 ==========
        if trades:
            st.subheader("交易明细")
            trade_df = pd.DataFrame(trades)
            trade_df["action"] = trade_df["action"].map({"BUY": "买入", "SELL": "卖出"})
            trade_df.columns = trade_df.columns.map({"date": "日期", "action": "操作", "price": "价格", "shares": "数量", "cost": "总成本", "revenue": "净收入", "symbol": "代码"}.get)
            st.dataframe(trade_df, use_container_width=True, hide_index=True)
        else:
            st.info("回测期间无交易发生")


# ========== 股票池管理页 ==========

def page_manage():
    st.title("A股港股量化分析小程序")
    st.subheader("股票池管理")

    pool = load_stock_pool()
    pool_codes = {s["code"] for s in pool}

    col_left, col_right = st.columns([1, 1], gap="large")

    # ========== 左侧：当前股票池列表 ==========
    with col_left:
        st.markdown("### 当前股票池")

        idx_pool = [s for s in pool if s.get("type") == "index"]
        a_pool = [s for s in pool if s.get("market", "A") == "A" and s.get("type") != "index"]
        hk_pool = [s for s in pool if s.get("market") == "HK"]

        for label, sub_pool in [("指数", idx_pool), ("A股", a_pool), ("港股", hk_pool)]:
            if not sub_pool:
                continue
            st.markdown(f"**{label}**（{len(sub_pool)} 只）")
            for s in sub_pool:
                c1, c2 = st.columns([4, 1])
                c1.write(f"{s['name']}（{s['code']}）")
                stype = s.get("type", "stock")
                if c2.button("移除", key=f"del_{s['code']}_{stype}"):
                    remove_stock(s["code"], stock_type=stype)
                    st.rerun()

        # 手动输入添加
        st.markdown("---")
        st.markdown("**手动添加**")
        add_cols = st.columns([2, 2, 1, 1, 1])
        new_code = add_cols[0].text_input("代码", placeholder="600519 或 00700", key="manual_code")
        new_name = add_cols[1].text_input("名称", placeholder="贵州茅台", key="manual_name")
        new_market = add_cols[2].selectbox("市场", ["A", "HK"], key="manual_market")
        new_type = add_cols[3].selectbox("类型", ["stock", "index"], key="manual_type",
                                          format_func=lambda x: "个股" if x == "stock" else "指数")
        if add_cols[4].button("添加", key="manual_add"):
            if new_code and new_name:
                if add_stock(new_code.strip(), new_name.strip(), new_market, stock_type=new_type):
                    st.rerun()
                else:
                    st.warning(f"{new_code} 已在股票池中")
            else:
                st.warning("请填写代码和名称")

    # ========== 右侧：搜索全市场股票 ==========
    with col_right:
        st.markdown("### 搜索全市场股票")

        search_cols = st.columns([3, 1])
        keyword = search_cols[0].text_input("输入代码或名称", placeholder="如: 茅台、00700、银行", key="search_kw")
        market_filter = search_cols[1].selectbox("市场", ["全部", "A股", "港股"], key="search_market")

        if keyword:
            m = None
            if market_filter == "A股":
                m = "A"
            elif market_filter == "港股":
                m = "HK"

            results = search_stocks(keyword, market=m)

            if not results:
                st.info(f"未找到「{keyword}」相关股票。如果是首次使用，列表需加载约 10 秒。")
            else:
                st.markdown(f"找到 **{len(results)}** 只（最多 50 条）")

                # 表头
                h1, h2, h3, h4 = st.columns([2, 2, 1, 1])
                h1.markdown("**代码**")
                h2.markdown("**名称**")
                h3.markdown("**市场**")
                h4.markdown("**操作**")

                for s in results:
                    market_tag = "港股" if s.get("market") == "HK" else "A股"
                    in_pool = s["code"] in pool_codes
                    c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
                    c1.write(s["code"])
                    c2.write(s["name"])
                    c3.write(market_tag)
                    if in_pool:
                        c4.markdown('<span style="color:#27ae60">✓ 已添加</span>', unsafe_allow_html=True)
                    else:
                        if c4.button("添加", key=f"add_{s['code']}"):
                            add_stock(s["code"], s["name"], s.get("market", "A"))
                            st.rerun()
        else:
            st.info("输入关键词搜索 A 股（约 5200 只）或港股（30 只热门），回车确认。")


# ========== 页面路由 ==========

pages = ["全局信号总览", "单股票分析", "股票池管理"]
default_page = st.session_state.get("page", "全局信号总览")
default_idx = pages.index(default_page) if default_page in pages else 0

page = st.sidebar.radio("页面", pages, index=default_idx)
st.session_state["page"] = page

if page == "全局信号总览":
    page_overview()
elif page == "单股票分析":
    page_detail()
else:
    page_manage()
