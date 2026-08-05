"""单股票详细分析页（issue #42 从 app.py 拆出）"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
from data.fetcher import is_hk_stock
from data import realtime      # 按模块引用，测试替 data.realtime 才打得进来（见 ui/pages/overview.py 的说明）
from strategy.signals import build_signals
from backtest.engine import run_backtest
from backtest.metrics import calc_metrics
from analysis.chanlun import analyze as chanlun_analyze

from config.pool import load_stock_pool, get_stock_name
from config.settings import DEFAULT_SIGNAL_SOURCE, SIGNAL_THRESHOLD, SIGNAL_LOOKBACK
from strategy.decision import decide, SOURCE_COLUMNS

from ui.cards import render_fundamental_card, render_sentiment_card
from ui.shared import SIGNAL_SOURCE_LABELS, get_recommendation, get_stock_data


def page_detail():
    st.title("A股港股量化分析小程序")

    # 侧边栏：按市场分组选股
    stocks = load_stock_pool()
    idx_stocks = [s for s in stocks if s.get("type") == "index"]
    sector_stocks = [s for s in stocks if s.get("type") == "sector"]
    a_stocks = [s for s in stocks if s.get("market", "A") == "A" and s.get("type") not in ("index", "sector")]
    hk_stocks = [s for s in stocks if s.get("market") == "HK"]

    # 从 session_state 读取跳转过来的股票
    preselected_code = st.session_state.get("selected_code", None)
    preselected_type = st.session_state.get("selected_type", None)
    auto_analyze = st.session_state.pop("auto_analyze", False)

    # 判断预选股票所在市场
    default_market = "A股"
    if preselected_type == "sector":
        default_market = "板块"
    elif preselected_type == "index":
        default_market = "指数"
    elif preselected_code and is_hk_stock(preselected_code):
        default_market = "港股"

    market_tabs = ["A股", "港股"]
    if sector_stocks:
        market_tabs = ["板块"] + market_tabs
    if idx_stocks:
        market_tabs = ["指数"] + market_tabs
    market_tab = st.sidebar.radio("市场", market_tabs, horizontal=True,
                                   index=market_tabs.index(default_market) if default_market in market_tabs else 0)
    if market_tab == "指数":
        pool = idx_stocks
    elif market_tab == "板块":
        pool = sector_stocks
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

    # 信号阈值（"待生效"语义：改完点刷新才应用）
    # dt_buy_th / dt_sell_th 是当前生效值；dt_*_pending 是输入框 widget 值
    buy_th = st.session_state.setdefault("dt_buy_th", SIGNAL_THRESHOLD)
    sell_th = st.session_state.setdefault("dt_sell_th", -SIGNAL_THRESHOLD)
    st.session_state.setdefault("dt_buy_th_pending", buy_th)
    st.session_state.setdefault("dt_sell_th_pending", sell_th)

    st.sidebar.markdown("---")
    st.sidebar.markdown("**信号阈值**")
    st.sidebar.number_input(
        "买入阈值 (≥)", min_value=0.0, max_value=1.0, step=0.05,
        key="dt_buy_th_pending",
        help="信号值 ≥ 此阈值算买入；改完点「刷新」才生效",
    )
    st.sidebar.number_input(
        "卖出阈值 (≤)", min_value=-1.0, max_value=0.0, step=0.05,
        key="dt_sell_th_pending",
        help="信号值 ≤ 此阈值算卖出；改完点「刷新」才生效",
    )
    if (st.session_state["dt_buy_th_pending"] != buy_th
            or st.session_state["dt_sell_th_pending"] != sell_th):
        st.sidebar.caption(":orange[阈值已修改，点「刷新」生效]")

    st.sidebar.markdown("---")

    button_pressed = st.sidebar.button("刷新", type="primary")

    # 点刷新时提交待生效阈值为当前生效阈值（要在 key 计算之前完成提交）
    if button_pressed:
        st.session_state["dt_buy_th"] = st.session_state["dt_buy_th_pending"]
        st.session_state["dt_sell_th"] = st.session_state["dt_sell_th_pending"]
        buy_th = st.session_state["dt_buy_th"]
        sell_th = st.session_state["dt_sell_th"]

    # 触发分析：按钮、首次自动、selection 变化都会重算
    # 阈值并入 key，刷新后阈值变化触发重算（融合 decision 依赖阈值）
    key = (symbol, stype, time_range, source, buy_th, sell_th)
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

            if df is None or df.empty:
                st.error(f"无法获取 {name}({symbol}) 的行情数据：数据源临时不可用或该代码暂无历史K线。"
                         "可点击「刷新」重试，或确认代码是否正确。")
                st.session_state.pop("detail_last_key", None)
                st.session_state.pop("detail_last_result", None)
                st.stop()

            # 全量信号 df（含 buffer，用于图表/缠论/当前信号展示）
            signal_df_full = build_signals(df, symbol=symbol, sentiment_scores=None,
                                            enabled_signals=[source],
                                            progress_cb=_detail_progress if kronos_progress else None)
            fusion_full = decide(signal_df_full, source=source,
                                        buy_threshold=buy_th, sell_threshold=sell_th)

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
        signal_color = "#e74c3c" if signal_val >= buy_th else "#27ae60" if signal_val <= sell_th else "#666"

        # 市盈率（指数/板块/缺值显示 -；负 PE 即亏损股，原样展示）
        # 板块直接跳过取价：这里只用 pe，而板块的 pe 恒为 "-"，问一次要多打一次
        # THS 板块快照接口（每个板块一次请求，冷启动还要先拉概念名称表）
        rt_quote = (None if stype == "sector"
                    else realtime.get_realtime_quotes([(symbol, stype)]).get((symbol, stype)))
        pe_raw = rt_quote.get("pe") if rt_quote else None
        if stype in ("index", "sector") or pe_raw is None:
            pe_text = "-"
        else:
            pe_text = f"{float(pe_raw):.2f}"

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
            f'<span style="font-size:16px;color:#666;margin-left:20px">市盈率 </span>'
            f'<span style="font-size:20px;font-weight:bold;color:#333">{pe_text}</span>'
            f'<span style="font-size:16px;color:#666;margin-left:20px">信号值 </span>'
            f'<span style="font-size:20px;font-weight:bold;color:{signal_color}">{signal_val:.3f}</span>'
            f'<span style="font-size:13px;color:#999;margin-left:6px">阈值 买≥{buy_th:g} / 卖≤{sell_th:g}</span>'
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
            kronos_cols[0].markdown(f"**最新信号**\n\n值: **{signal_val:.3f}**\n\n阈值: 买≥{buy_th:g} / 卖≤{sell_th:g}")
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
        fig.add_hline(y=buy_th, line_dash="dot", line_color="#e74c3c", line_width=0.5, row=r_sig, col=1, annotation_text=f"买≥{buy_th:g}", annotation_position="top right")
        fig.add_hline(y=sell_th, line_dash="dot", line_color="#27ae60", line_width=0.5, row=r_sig, col=1, annotation_text=f"卖≤{sell_th:g}", annotation_position="bottom right")
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

        # ========== 过去 10 个交易日情绪（公告/新闻/股吧） ==========
        render_sentiment_card(symbol, stype)

        # ========== 基本面（确定性层，#33） ==========
        render_fundamental_card(symbol, stype)

