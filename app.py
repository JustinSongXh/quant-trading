"""Streamlit 可视化：K线图 + 多策略信号 + 买卖点标注 + 回测绩效"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd

from config.settings import load_stock_pool, get_stock_name
from data.fetcher import fetch_daily_kline
from data.mock import fetch_mock_kline
from data.cache import save_kline, load_kline
from strategy.signals import build_signals
from strategy.fusion import fuse_signals
from backtest.engine import run_backtest
from backtest.metrics import calc_metrics
from analysis.chanlun import analyze as chanlun_analyze


st.set_page_config(page_title="A股量化回测系统", layout="wide")
st.title("A股量化回测系统")

# ========== 侧边栏 ==========
stocks = load_stock_pool()
stock_options = {f"{s['name']}({s['code']})": s['code'] for s in stocks}
selected = st.sidebar.selectbox("选择股票", list(stock_options.keys()))
symbol = stock_options[selected]
name = get_stock_name(symbol)

if st.sidebar.button("开始回测", type="primary"):
    with st.spinner(f"正在分析 {name}({symbol})..."):
        # 获取数据
        df = load_kline(symbol)
        if df is None:
            try:
                df = fetch_daily_kline(symbol)
                save_kline(symbol, df)
            except Exception:
                df = fetch_mock_kline(symbol)
                st.warning("无法连接数据源，使用模拟数据")

        # 构建信号 & 回测
        signal_df = build_signals(df, symbol=symbol, sentiment_scores=None)
        decisions = fuse_signals(signal_df)
        result = run_backtest(symbol, signal_df, decisions)
        metrics = calc_metrics(result["net_values"], result["initial_capital"])
        trades = result["trade_log"]

        # 缠论分析详情
        chan_result = chanlun_analyze(df, symbol)

    # ========== 绩效指标 ==========
    st.subheader(f"{name}({symbol}) 回测结果")
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

    # 综合推荐
    if last_decision == 1:
        rec_text, rec_color = "买入", "#e74c3c"
    elif last_decision == -1:
        rec_text, rec_color = "卖出", "#27ae60"
    else:
        rec_text, rec_color = "持有/观望", "#999999"

    st.markdown(
        f'<div style="background:#f8f9fa;border-radius:8px;padding:16px 24px;margin-bottom:16px">'
        f'<span style="font-size:16px;color:#666">综合推荐（{last_date}）</span>'
        f'<span style="font-size:32px;font-weight:bold;color:{rec_color};margin-left:20px">{rec_text}</span>'
        f'<span style="font-size:16px;color:#666;margin-left:20px">收盘价 {last_close:.2f}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # 各策略明细
    detail_cols = st.columns(5)

    # 1) MACD
    macd_dif = last.get("macd_dif", 0)
    macd_dea = last.get("macd_dea", 0)
    macd_hist = last.get("macd_hist", 0)
    if macd_dif > macd_dea:
        macd_rec = "多头（DIF>DEA）"
    elif macd_dif < macd_dea:
        macd_rec = "空头（DIF<DEA）"
    else:
        macd_rec = "中性"
    detail_cols[0].markdown(
        f"**MACD**\n\n"
        f"DIF: {macd_dif:.3f}\n\n"
        f"DEA: {macd_dea:.3f}\n\n"
        f"柱: {macd_hist:.3f}\n\n"
        f"判定: **{macd_rec}**"
    )

    # 2) RSI
    rsi_val = last.get("rsi", 50)
    if rsi_val < 30:
        rsi_rec = "超卖 → 买入机会"
    elif rsi_val > 70:
        rsi_rec = "超买 → 卖出机会"
    elif rsi_val < 45:
        rsi_rec = "偏弱"
    elif rsi_val > 55:
        rsi_rec = "偏强"
    else:
        rsi_rec = "中性"
    detail_cols[1].markdown(
        f"**RSI({last.get('rsi', 0):.1f})**\n\n"
        f"判定: **{rsi_rec}**"
    )

    # 3) KDJ
    kdj_k = last.get("kdj_k", 50)
    kdj_d = last.get("kdj_d", 50)
    kdj_j = last.get("kdj_j", 50)
    if kdj_k > kdj_d and kdj_j < 20:
        kdj_rec = "超卖金叉 → 强买"
    elif kdj_k > kdj_d:
        kdj_rec = "金叉 → 偏多"
    elif kdj_k < kdj_d and kdj_j > 80:
        kdj_rec = "超买死叉 → 强卖"
    elif kdj_k < kdj_d:
        kdj_rec = "死叉 → 偏空"
    else:
        kdj_rec = "中性"
    detail_cols[2].markdown(
        f"**KDJ**\n\n"
        f"K: {kdj_k:.1f}  D: {kdj_d:.1f}  J: {kdj_j:.1f}\n\n"
        f"判定: **{kdj_rec}**"
    )

    # 4) 布林带
    boll_upper = last.get("boll_upper", 0)
    boll_mid = last.get("boll_mid", 0)
    boll_lower = last.get("boll_lower", 0)
    if last_close >= boll_upper:
        boll_rec = "触及上轨 → 卖出"
    elif last_close <= boll_lower:
        boll_rec = "触及下轨 → 买入"
    elif last_close > boll_mid:
        boll_rec = "中轨上方 → 偏多"
    else:
        boll_rec = "中轨下方 → 偏空"
    detail_cols[3].markdown(
        f"**布林带**\n\n"
        f"上轨: {boll_upper:.2f}\n\n"
        f"中轨: {boll_mid:.2f}\n\n"
        f"下轨: {boll_lower:.2f}\n\n"
        f"判定: **{boll_rec}**"
    )

    # 5) 缠论
    chan_buys_all = chan_result["buy_points"]
    chan_sells_all = chan_result["sell_points"]
    # 找最近的缠论买卖点
    recent_chan = "无近期信号"
    all_chan_points = []
    for bp in chan_buys_all:
        all_chan_points.append((bp["dt"], "买", bp["type"], bp["reason"]))
    for sp in chan_sells_all:
        all_chan_points.append((sp["dt"], "卖", sp["type"], sp["reason"]))
    all_chan_points.sort(key=lambda x: x[0])
    if all_chan_points:
        latest = all_chan_points[-1]
        recent_chan = f"{latest[1]}({latest[2]}) @ {str(latest[0].date())}"
    detail_cols[4].markdown(
        f"**缠论**\n\n"
        f"笔: {len(chan_result['bi_list'])}  中枢: {len(chan_result['zs_list'])}\n\n"
        f"最近信号: **{recent_chan}**"
    )

    # 技术综合 + 缠论综合的信号数值
    st.markdown("---")
    sig_cols = st.columns(3)
    tech_val = last.get("technical_signal", 0)
    chan_val = last.get("chanlun_signal", 0)
    tech_color = "#e74c3c" if tech_val > 0 else "#27ae60" if tech_val < 0 else "#999"
    chan_color = "#e74c3c" if chan_val > 0 else "#27ae60" if chan_val < 0 else "#999"
    sig_cols[0].markdown(
        f'技术综合信号: <span style="font-size:24px;font-weight:bold;color:{tech_color}">{tech_val:.3f}</span>'
        f' <span style="color:#999">(-1 强卖 ~ +1 强买)</span>',
        unsafe_allow_html=True,
    )
    sig_cols[1].markdown(
        f'缠论信号: <span style="font-size:24px;font-weight:bold;color:{chan_color}">{chan_val:.3f}</span>'
        f' <span style="color:#999">(B1=1.0 S1=-1.0)</span>',
        unsafe_allow_html=True,
    )
    sig_cols[2].markdown(
        f'融合决策: <span style="font-size:24px;font-weight:bold;color:{rec_color}">{rec_text}</span>',
        unsafe_allow_html=True,
    )
    st.markdown("---")

    # ========== 主图：K线 + 买卖点 + 布林带 + 均线 ==========
    fig = make_subplots(
        rows=6, cols=1, shared_xaxes=True,
        vertical_spacing=0.025,
        row_heights=[0.35, 0.10, 0.12, 0.12, 0.12, 0.19],
        subplot_titles=[
            "K线 & 买卖点 & 布林带",
            "成交量 & 量比",
            "MACD",
            "RSI & KDJ",
            "信号总览（技术 / 缠论 / 融合决策）",
            "净值曲线",
        ],
    )

    # --- Row 1: K线 ---
    fig.add_trace(go.Candlestick(
        x=signal_df.index,
        open=signal_df["open"], high=signal_df["high"],
        low=signal_df["low"], close=signal_df["close"],
        name="K线",
        increasing_line_color="#e74c3c",
        decreasing_line_color="#27ae60",
    ), row=1, col=1)

    # 均线
    ma_colors = {"ma_5": "#2196F3", "ma_10": "#FF9800", "ma_20": "#E91E63", "ma_60": "#9C27B0"}
    for ma_col, color in ma_colors.items():
        if ma_col in signal_df.columns:
            fig.add_trace(go.Scatter(
                x=signal_df.index, y=signal_df[ma_col],
                name=ma_col.upper(), line=dict(width=1, color=color),
            ), row=1, col=1)

    # 布林带
    if "boll_upper" in signal_df.columns:
        fig.add_trace(go.Scatter(
            x=signal_df.index, y=signal_df["boll_upper"],
            name="BOLL上轨", line=dict(width=1, color="rgba(150,150,150,0.5)", dash="dot"),
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=signal_df.index, y=signal_df["boll_lower"],
            name="BOLL下轨", line=dict(width=1, color="rgba(150,150,150,0.5)", dash="dot"),
            fill="tonexty", fillcolor="rgba(150,150,150,0.05)",
        ), row=1, col=1)

    # 买卖点标注
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    if buys:
        fig.add_trace(go.Scatter(
            x=[t["date"] for t in buys], y=[t["price"] for t in buys],
            mode="markers+text",
            marker=dict(symbol="triangle-up", size=16, color="#e74c3c", line=dict(width=1, color="white")),
            text=["B"] * len(buys), textposition="bottom center", textfont=dict(size=10, color="#e74c3c"),
            name=f"买入({len(buys)}次)",
            hovertext=[f"买入 {t['shares']}股 @ {t['price']:.2f}" for t in buys],
            hoverinfo="text+x",
        ), row=1, col=1)
    if sells:
        fig.add_trace(go.Scatter(
            x=[t["date"] for t in sells], y=[t["price"] for t in sells],
            mode="markers+text",
            marker=dict(symbol="triangle-down", size=16, color="#27ae60", line=dict(width=1, color="white")),
            text=["S"] * len(sells), textposition="top center", textfont=dict(size=10, color="#27ae60"),
            name=f"卖出({len(sells)}次)",
            hovertext=[f"卖出 {t['shares']}股 @ {t['price']:.2f}" for t in sells],
            hoverinfo="text+x",
        ), row=1, col=1)

    # 缠论买卖点（用菱形标注在 K 线上）
    chan_buys = chan_result["buy_points"]
    chan_sells = chan_result["sell_points"]
    if chan_buys:
        fig.add_trace(go.Scatter(
            x=[bp["dt"] for bp in chan_buys], y=[bp["price"] for bp in chan_buys],
            mode="markers",
            marker=dict(symbol="diamond", size=12, color="#FF6F00", line=dict(width=1, color="white")),
            name=f"缠论买点({len(chan_buys)})",
            hovertext=[f"{bp['type']}: {bp['reason']}" for bp in chan_buys],
            hoverinfo="text+x",
        ), row=1, col=1)
    if chan_sells:
        fig.add_trace(go.Scatter(
            x=[sp["dt"] for sp in chan_sells], y=[sp["price"] for sp in chan_sells],
            mode="markers",
            marker=dict(symbol="diamond", size=12, color="#7B1FA2", line=dict(width=1, color="white")),
            name=f"缠论卖点({len(chan_sells)})",
            hovertext=[f"{sp['type']}: {sp['reason']}" for sp in chan_sells],
            hoverinfo="text+x",
        ), row=1, col=1)

    # --- Row 2: 成交量 + 量比 ---
    if "volume" in signal_df.columns:
        colors = ["#e74c3c" if c >= o else "#27ae60"
                  for c, o in zip(signal_df["close"], signal_df["open"])]
        fig.add_trace(go.Bar(
            x=signal_df.index, y=signal_df["volume"],
            marker_color=colors, name="成交量", showlegend=False,
        ), row=2, col=1)
    if "vol_ratio" in signal_df.columns:
        fig.add_trace(go.Scatter(
            x=signal_df.index, y=signal_df["vol_ratio"],
            name="量比", line=dict(color="#FF9800", width=1),
            yaxis="y2",
        ), row=2, col=1)

    # --- Row 3: MACD ---
    if "macd_dif" in signal_df.columns:
        fig.add_trace(go.Scatter(
            x=signal_df.index, y=signal_df["macd_dif"],
            name="DIF", line=dict(color="#2196F3", width=1.5),
        ), row=3, col=1)
        fig.add_trace(go.Scatter(
            x=signal_df.index, y=signal_df["macd_dea"],
            name="DEA", line=dict(color="#FF9800", width=1.5),
        ), row=3, col=1)
        macd_colors = ["#e74c3c" if v >= 0 else "#27ae60" for v in signal_df["macd_hist"]]
        fig.add_trace(go.Bar(
            x=signal_df.index, y=signal_df["macd_hist"],
            marker_color=macd_colors, name="MACD柱", showlegend=False,
        ), row=3, col=1)
        fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=3, col=1)

    # --- Row 4: RSI & KDJ ---
    if "rsi" in signal_df.columns:
        fig.add_trace(go.Scatter(
            x=signal_df.index, y=signal_df["rsi"],
            name="RSI", line=dict(color="#9C27B0", width=1.5),
        ), row=4, col=1)
        fig.add_hline(y=70, line_dash="dot", line_color="#e74c3c", line_width=0.5, row=4, col=1,
                      annotation_text="超买70", annotation_position="bottom right")
        fig.add_hline(y=30, line_dash="dot", line_color="#27ae60", line_width=0.5, row=4, col=1,
                      annotation_text="超卖30", annotation_position="top right")
    if "kdj_k" in signal_df.columns:
        fig.add_trace(go.Scatter(
            x=signal_df.index, y=signal_df["kdj_k"],
            name="KDJ-K", line=dict(color="#2196F3", width=1),
        ), row=4, col=1)
        fig.add_trace(go.Scatter(
            x=signal_df.index, y=signal_df["kdj_d"],
            name="KDJ-D", line=dict(color="#FF9800", width=1),
        ), row=4, col=1)
        fig.add_trace(go.Scatter(
            x=signal_df.index, y=signal_df["kdj_j"],
            name="KDJ-J", line=dict(color="#E91E63", width=1, dash="dot"),
        ), row=4, col=1)

    # --- Row 5: 信号总览 ---
    fig.add_trace(go.Scatter(
        x=signal_df.index, y=signal_df["technical_signal"],
        name="技术信号", line=dict(color="#2196F3", width=1.5),
        fill="tozeroy", fillcolor="rgba(33,150,243,0.1)",
    ), row=5, col=1)
    fig.add_trace(go.Scatter(
        x=signal_df.index, y=signal_df["chanlun_signal"],
        name="缠论信号", mode="markers+lines",
        line=dict(color="#FF6F00", width=1),
        marker=dict(size=4),
    ), row=5, col=1)
    # 融合决策
    decision_colors = ["#e74c3c" if d == 1 else "#27ae60" if d == -1 else "rgba(0,0,0,0)"
                       for d in decisions]
    decision_sizes = [12 if d != 0 else 0 for d in decisions]
    fig.add_trace(go.Scatter(
        x=signal_df.index, y=decisions,
        name="融合决策", mode="markers",
        marker=dict(size=decision_sizes, color=decision_colors,
                    symbol=["triangle-up" if d == 1 else "triangle-down" if d == -1 else "circle" for d in decisions]),
    ), row=5, col=1)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=5, col=1)

    # --- Row 6: 净值曲线 ---
    nv = result["net_values"]
    if not nv.empty:
        fig.add_trace(go.Scatter(
            x=nv.index, y=nv["net_value"],
            name="净值", line=dict(color="#3498db", width=2),
            fill="tozeroy", fillcolor="rgba(52,152,219,0.1)",
        ), row=6, col=1)
        fig.add_hline(y=result["initial_capital"], line_dash="dash",
                      line_color="gray", row=6, col=1)

    # 布局
    fig.update_layout(
        height=1400,
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, font=dict(size=10)),
        margin=dict(l=60, r=20, t=40, b=20),
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1)
    fig.update_yaxes(title_text="MACD", row=3, col=1)
    fig.update_yaxes(title_text="RSI/KDJ", row=4, col=1)
    fig.update_yaxes(title_text="信号值", row=5, col=1)
    fig.update_yaxes(title_text="净值", row=6, col=1)

    # 每个子图都显示时间轴刻度
    for i in range(1, 7):
        fig.update_xaxes(
            showticklabels=True,
            tickformat="%y/%m",
            dtick="M2",
            row=i, col=1,
        )

    st.plotly_chart(fig, use_container_width=True)

    # ========== 缠论分析摘要 ==========
    st.subheader("缠论分析")
    chan_cols = st.columns(4)
    chan_cols[0].metric("分型数", len(chan_result["czsc"].fx_list))
    chan_cols[1].metric("笔数", len(chan_result["bi_list"]))
    chan_cols[2].metric("中枢数", len(chan_result["zs_list"]))
    chan_cols[3].metric("买卖点", f"{len(chan_buys)}买 / {len(chan_sells)}卖")

    if chan_buys or chan_sells:
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
        trade_df.columns = trade_df.columns.map({
            "date": "日期", "action": "操作", "price": "价格",
            "shares": "数量", "cost": "总成本", "revenue": "净收入", "symbol": "代码",
        }.get)
        st.dataframe(trade_df, use_container_width=True, hide_index=True)
    else:
        st.info("回测期间无交易发生")
