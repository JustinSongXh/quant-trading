"""Streamlit 可视化：全局信号总览 + 单股票详细分析"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd

from config.settings import load_stock_pool, get_stock_name, add_stock, remove_stock
from data.fetcher import fetch_daily_kline, is_hk_stock
from data.stock_list import search_stocks, get_all_stocks
from data.mock import fetch_mock_kline
from data.cache import save_kline, load_kline, is_cache_fresh, is_during_trading
from strategy.signals import build_signals
from strategy.fusion import fuse_signals
from backtest.engine import run_backtest
from backtest.metrics import calc_metrics
from analysis.chanlun import analyze as chanlun_analyze

st.set_page_config(page_title="A股港股量化分析系统", layout="wide")


# ========== 工具函数 ==========

def get_stock_data(code, days=365, force_refresh=False):
    """获取股票数据，优先用新鲜缓存，过期则刷新"""
    if not force_refresh and is_cache_fresh(code):
        df = load_kline(code)
        if df is not None and len(df) > 0:
            return df
    try:
        df = fetch_daily_kline(code, days=days)
        save_kline(code, df)
        return df
    except Exception:
        df = load_kline(code)
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

def page_overview():
    st.title("A股港股量化分析系统")
    st.subheader("全局信号总览")

    # 盘中提示
    if is_during_trading("000001") or is_during_trading("00700"):
        st.warning("当前为交易时段，数据可能为盘中快照，非收盘价。收盘后刷新页面获取最终数据。")

    stocks = load_stock_pool()
    a_stocks = [s for s in stocks if s.get("market", "A") == "A"]
    hk_stocks = [s for s in stocks if s.get("market") == "HK"]

    progress = st.progress(0, text="正在扫描...")
    rows = []
    total = len(stocks)

    for i, stock in enumerate(stocks):
        code, name = stock["code"], stock["name"]
        market = stock.get("market", "A")
        progress.progress((i + 1) / total, text=f"扫描 {name}({code})...")

        try:
            df = get_stock_data(code, days=400)
            signal_df = build_signals(df, symbol=code, sentiment_scores=None)
            decisions = fuse_signals(signal_df)

            last = signal_df.iloc[-1]
            last_date = str(signal_df.index[-1].date())
            rec_text, _ = get_recommendation(decisions.iloc[-1])

            rows.append({
                "市场": "港股" if market == "HK" else "A股",
                "股票": f"{name}({code})",
                "收盘价": round(last["close"], 2),
                "技术信号": round(last.get("technical_signal", 0), 3),
                "缠论信号": round(last.get("chanlun_signal", 0), 3),
                "综合推荐": rec_text,
                "日期": last_date,
                "_code": code,
            })
        except Exception as e:
            rows.append({
                "市场": "港股" if market == "HK" else "A股",
                "股票": f"{name}({code})",
                "收盘价": "-",
                "技术信号": "-",
                "缠论信号": "-",
                "综合推荐": "数据异常",
                "日期": "-",
                "_code": code,
            })

    progress.empty()

    if not rows:
        st.warning("股票池为空")
        return

    result_df = pd.DataFrame(rows)

    # 分市场显示，每行带「查看详情」按钮
    for market_label, market_key in [("A股", "A股"), ("港股", "港股")]:
        market_df = result_df[result_df["市场"] == market_key]
        if market_df.empty:
            continue

        st.markdown(f"### {market_label}")

        # 表头
        header_cols = st.columns([3, 1.5, 1.5, 1.5, 1.2, 1.5, 1])
        for col, title in zip(header_cols, ["股票", "收盘价", "技术信号", "缠论信号", "推荐", "日期", ""]):
            col.markdown(f"**{title}**")

        # 数据行
        for _, row in market_df.iterrows():
            cols = st.columns([3, 1.5, 1.5, 1.5, 1.2, 1.5, 1])
            cols[0].write(row["股票"])
            cols[1].write(row["收盘价"])

            # 技术信号带颜色
            tech = row["技术信号"]
            if isinstance(tech, (int, float)):
                t_color = "#e74c3c" if tech > 0.1 else "#27ae60" if tech < -0.1 else "#666"
                cols[2].markdown(f'<span style="color:{t_color}">{tech:.3f}</span>', unsafe_allow_html=True)
            else:
                cols[2].write(tech)

            # 缠论信号带颜色
            chan = row["缠论信号"]
            if isinstance(chan, (int, float)):
                c_color = "#e74c3c" if chan > 0.1 else "#27ae60" if chan < -0.1 else "#666"
                cols[3].markdown(f'<span style="color:{c_color}">{chan:.3f}</span>', unsafe_allow_html=True)
            else:
                cols[3].write(chan)

            # 推荐带颜色
            rec = row["综合推荐"]
            r_color = "#e74c3c" if rec == "买入" else "#27ae60" if rec == "卖出" else "#999"
            cols[4].markdown(f'<span style="color:{r_color};font-weight:bold">{rec}</span>', unsafe_allow_html=True)

            cols[5].write(row["日期"])

            # 查看详情按钮
            if cols[6].button("详情", key=f"btn_{row['_code']}"):
                st.session_state["page"] = "单股票分析"
                st.session_state["selected_code"] = row["_code"]
                st.session_state["auto_analyze"] = True
                st.rerun()


# ========== 单股票详细分析页 ==========

def page_detail():
    st.title("A股港股量化分析系统")

    # 侧边栏：按市场分组选股
    stocks = load_stock_pool()
    a_stocks = [s for s in stocks if s.get("market", "A") == "A"]
    hk_stocks = [s for s in stocks if s.get("market") == "HK"]

    # 从 session_state 读取跳转过来的股票
    preselected_code = st.session_state.get("selected_code", None)
    auto_analyze = st.session_state.pop("auto_analyze", False)

    # 按市场分组
    all_options = {f"{s['name']}({s['code']})": s['code'] for s in stocks}
    a_options = {f"{s['name']}({s['code']})": s['code'] for s in a_stocks}
    hk_options = {f"{s['name']}({s['code']})": s['code'] for s in hk_stocks}

    # 判断预选股票所在市场
    default_market = "A股"
    if preselected_code and is_hk_stock(preselected_code):
        default_market = "港股"

    market_tab = st.sidebar.radio("市场", ["A股", "港股"], horizontal=True,
                                   index=0 if default_market == "A股" else 1)
    pool = a_stocks if market_tab == "A股" else hk_stocks
    stock_options = {f"{s['name']}({s['code']})": s['code'] for s in pool}

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
    name = get_stock_name(symbol)

    time_range = st.sidebar.selectbox("回测时间范围", ["近3个月", "近6个月", "近1年", "近2年"], index=2)
    time_range_days = {"近3个月": 90, "近6个月": 180, "近1年": 365, "近2年": 730}[time_range]

    if st.sidebar.button("开始分析", type="primary") or auto_analyze:
        with st.spinner(f"正在分析 {name}({symbol})..."):
            df = get_stock_data(symbol, days=max(time_range_days + 60, 400), force_refresh=True)

            cutoff = df.index.max() - pd.Timedelta(days=time_range_days)
            df = df.loc[df.index >= cutoff]

            signal_df = build_signals(df, symbol=symbol, sentiment_scores=None)
            decisions = fuse_signals(signal_df)
            result = run_backtest(symbol, signal_df, decisions)
            metrics = calc_metrics(result["net_values"], result["initial_capital"])
            trades = result["trade_log"]
            chan_result = chanlun_analyze(df, symbol)

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
        rec_text, rec_color = get_recommendation(last_decision)

        st.markdown(
            f'<div style="background:#f8f9fa;border-radius:8px;padding:16px 24px;margin-bottom:16px">'
            f'<span style="font-size:16px;color:#666">综合推荐（{last_date}）</span>'
            f'<span style="font-size:32px;font-weight:bold;color:{rec_color};margin-left:20px">{rec_text}</span>'
            f'<span style="font-size:16px;color:#666;margin-left:20px">收盘价 {last_close:.2f}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # 各策略明细
        detail_cols = st.columns(6)

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

        chan_buys_all = chan_result["buy_points"]
        chan_sells_all = chan_result["sell_points"]
        all_chan_points = [(bp["dt"], "买", bp["type"]) for bp in chan_buys_all] + [(sp["dt"], "卖", sp["type"]) for sp in chan_sells_all]
        all_chan_points.sort(key=lambda x: x[0])
        recent_chan = f"{all_chan_points[-1][1]}({all_chan_points[-1][2]}) @ {str(all_chan_points[-1][0].date())}" if all_chan_points else "无近期信号"
        detail_cols[5].markdown(f"**缠论**\n\n笔: {len(chan_result['bi_list'])}  中枢: {len(chan_result['zs_list'])}\n\n最近信号: **{recent_chan}**")

        st.markdown("---")
        sig_cols = st.columns(3)
        tech_val = last.get("technical_signal", 0)
        chan_val = last.get("chanlun_signal", 0)
        tech_color = "#e74c3c" if tech_val > 0 else "#27ae60" if tech_val < 0 else "#999"
        chan_color = "#e74c3c" if chan_val > 0 else "#27ae60" if chan_val < 0 else "#999"
        sig_cols[0].markdown(f'技术综合信号: <span style="font-size:24px;font-weight:bold;color:{tech_color}">{tech_val:.3f}</span> <span style="color:#999">(-1 强卖 ~ +1 强买)</span>', unsafe_allow_html=True)
        sig_cols[1].markdown(f'缠论信号: <span style="font-size:24px;font-weight:bold;color:{chan_color}">{chan_val:.3f}</span> <span style="color:#999">(B1=1.0 S1=-1.0)</span>', unsafe_allow_html=True)
        sig_cols[2].markdown(f'融合决策: <span style="font-size:24px;font-weight:bold;color:{rec_color}">{rec_text}</span>', unsafe_allow_html=True)
        st.markdown("---")

        # ========== 图表 ==========
        fig = make_subplots(
            rows=6, cols=1, shared_xaxes=True, vertical_spacing=0.025,
            row_heights=[0.35, 0.10, 0.12, 0.12, 0.12, 0.19],
            subplot_titles=["K线 & 买卖点 & 布林带", "成交量 & 量比", "MACD", "RSI & KDJ", "信号总览（技术 / 缠论 / 融合决策）", "净值曲线"],
        )

        fig.add_trace(go.Candlestick(x=signal_df.index, open=signal_df["open"], high=signal_df["high"], low=signal_df["low"], close=signal_df["close"], name="K线", increasing_line_color="#e74c3c", decreasing_line_color="#27ae60"), row=1, col=1)

        for ma_col, color in {"ma_5": "#2196F3", "ma_10": "#FF9800", "ma_20": "#E91E63", "ma_60": "#9C27B0"}.items():
            if ma_col in signal_df.columns:
                fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df[ma_col], name=ma_col.upper(), line=dict(width=1, color=color)), row=1, col=1)

        if "boll_upper" in signal_df.columns:
            fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["boll_upper"], name="BOLL上轨", line=dict(width=1, color="rgba(150,150,150,0.5)", dash="dot")), row=1, col=1)
            fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["boll_lower"], name="BOLL下轨", line=dict(width=1, color="rgba(150,150,150,0.5)", dash="dot"), fill="tonexty", fillcolor="rgba(150,150,150,0.05)"), row=1, col=1)

        # SuperTrend 轨道
        if "st_value" in signal_df.columns:
            st_colors = ["#e74c3c" if d == 1 else "#27ae60" for d in signal_df["st_direction"]]
            fig.add_trace(go.Scatter(
                x=signal_df.index, y=signal_df["st_value"],
                name="SuperTrend", mode="lines",
                line=dict(width=2, color="#FF6F00", dash="dash"),
            ), row=1, col=1)

        buys = [t for t in trades if t["action"] == "BUY"]
        sells = [t for t in trades if t["action"] == "SELL"]
        if buys:
            fig.add_trace(go.Scatter(x=[t["date"] for t in buys], y=[t["price"] for t in buys], mode="markers+text", marker=dict(symbol="triangle-up", size=16, color="#e74c3c", line=dict(width=1, color="white")), text=["B"]*len(buys), textposition="bottom center", textfont=dict(size=10, color="#e74c3c"), name=f"买入({len(buys)}次)", hovertext=[f"买入 {t['shares']}股 @ {t['price']:.2f}" for t in buys], hoverinfo="text+x"), row=1, col=1)
        if sells:
            fig.add_trace(go.Scatter(x=[t["date"] for t in sells], y=[t["price"] for t in sells], mode="markers+text", marker=dict(symbol="triangle-down", size=16, color="#27ae60", line=dict(width=1, color="white")), text=["S"]*len(sells), textposition="top center", textfont=dict(size=10, color="#27ae60"), name=f"卖出({len(sells)}次)", hovertext=[f"卖出 {t['shares']}股 @ {t['price']:.2f}" for t in sells], hoverinfo="text+x"), row=1, col=1)

        chan_buys = chan_result["buy_points"]
        chan_sells = chan_result["sell_points"]
        if chan_buys:
            fig.add_trace(go.Scatter(x=[bp["dt"] for bp in chan_buys], y=[bp["price"] for bp in chan_buys], mode="markers", marker=dict(symbol="diamond", size=12, color="#FF6F00", line=dict(width=1, color="white")), name=f"缠论买点({len(chan_buys)})", hovertext=[f"{bp['type']}: {bp['reason']}" for bp in chan_buys], hoverinfo="text+x"), row=1, col=1)
        if chan_sells:
            fig.add_trace(go.Scatter(x=[sp["dt"] for sp in chan_sells], y=[sp["price"] for sp in chan_sells], mode="markers", marker=dict(symbol="diamond", size=12, color="#7B1FA2", line=dict(width=1, color="white")), name=f"缠论卖点({len(chan_sells)})", hovertext=[f"{sp['type']}: {sp['reason']}" for sp in chan_sells], hoverinfo="text+x"), row=1, col=1)

        if "volume" in signal_df.columns:
            vol_colors = ["#e74c3c" if c >= o else "#27ae60" for c, o in zip(signal_df["close"], signal_df["open"])]
            fig.add_trace(go.Bar(x=signal_df.index, y=signal_df["volume"], marker_color=vol_colors, name="成交量", showlegend=False), row=2, col=1)

        if "macd_dif" in signal_df.columns:
            fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["macd_dif"], name="DIF", line=dict(color="#2196F3", width=1.5)), row=3, col=1)
            fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["macd_dea"], name="DEA", line=dict(color="#FF9800", width=1.5)), row=3, col=1)
            macd_colors = ["#e74c3c" if v >= 0 else "#27ae60" for v in signal_df["macd_hist"]]
            fig.add_trace(go.Bar(x=signal_df.index, y=signal_df["macd_hist"], marker_color=macd_colors, name="MACD柱", showlegend=False), row=3, col=1)
            fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=3, col=1)

        if "rsi" in signal_df.columns:
            fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["rsi"], name="RSI", line=dict(color="#9C27B0", width=1.5)), row=4, col=1)
            fig.add_hline(y=70, line_dash="dot", line_color="#e74c3c", line_width=0.5, row=4, col=1, annotation_text="超买70", annotation_position="bottom right")
            fig.add_hline(y=30, line_dash="dot", line_color="#27ae60", line_width=0.5, row=4, col=1, annotation_text="超卖30", annotation_position="top right")
        if "kdj_k" in signal_df.columns:
            fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["kdj_k"], name="KDJ-K", line=dict(color="#2196F3", width=1)), row=4, col=1)
            fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["kdj_d"], name="KDJ-D", line=dict(color="#FF9800", width=1)), row=4, col=1)
            fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["kdj_j"], name="KDJ-J", line=dict(color="#E91E63", width=1, dash="dot")), row=4, col=1)

        fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["technical_signal"], name="技术信号", line=dict(color="#2196F3", width=1.5), fill="tozeroy", fillcolor="rgba(33,150,243,0.1)"), row=5, col=1)
        fig.add_trace(go.Scatter(x=signal_df.index, y=signal_df["chanlun_signal"], name="缠论信号", mode="markers+lines", line=dict(color="#FF6F00", width=1), marker=dict(size=4)), row=5, col=1)
        decision_colors = ["#e74c3c" if d == 1 else "#27ae60" if d == -1 else "rgba(0,0,0,0)" for d in decisions]
        decision_sizes = [12 if d != 0 else 0 for d in decisions]
        fig.add_trace(go.Scatter(x=signal_df.index, y=decisions, name="融合决策", mode="markers", marker=dict(size=decision_sizes, color=decision_colors, symbol=["triangle-up" if d == 1 else "triangle-down" if d == -1 else "circle" for d in decisions])), row=5, col=1)
        fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=5, col=1)

        nv = result["net_values"]
        if not nv.empty:
            fig.add_trace(go.Scatter(x=nv.index, y=nv["net_value"], name="净值", line=dict(color="#3498db", width=2), fill="tozeroy", fillcolor="rgba(52,152,219,0.1)"), row=6, col=1)
            fig.add_hline(y=result["initial_capital"], line_dash="dash", line_color="gray", row=6, col=1)

        fig.update_layout(height=1400, xaxis_rangeslider_visible=False, legend=dict(orientation="h", yanchor="bottom", y=1.01, font=dict(size=10)), margin=dict(l=60, r=20, t=40, b=20))
        for i, title in enumerate(["价格", "成交量", "MACD", "RSI/KDJ", "信号值", "净值"], 1):
            fig.update_yaxes(title_text=title, row=i, col=1)
        for i in range(1, 7):
            fig.update_xaxes(showticklabels=True, tickformat="%y/%m", dtick="M2", row=i, col=1)

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
            trade_df.columns = trade_df.columns.map({"date": "日期", "action": "操作", "price": "价格", "shares": "数量", "cost": "总成本", "revenue": "净收入", "symbol": "代码"}.get)
            st.dataframe(trade_df, use_container_width=True, hide_index=True)
        else:
            st.info("回测期间无交易发生")


# ========== 股票池管理页 ==========

def page_manage():
    st.title("A股港股量化分析系统")
    st.subheader("股票池管理")

    pool = load_stock_pool()
    pool_codes = {s["code"] for s in pool}

    col_left, col_right = st.columns([1, 1], gap="large")

    # ========== 左侧：当前股票池列表 ==========
    with col_left:
        st.markdown("### 当前股票池")

        a_pool = [s for s in pool if s.get("market", "A") == "A"]
        hk_pool = [s for s in pool if s.get("market") == "HK"]

        for label, sub_pool in [("A股", a_pool), ("港股", hk_pool)]:
            if not sub_pool:
                continue
            st.markdown(f"**{label}**（{len(sub_pool)} 只）")
            for s in sub_pool:
                c1, c2 = st.columns([4, 1])
                c1.write(f"{s['name']}（{s['code']}）")
                if c2.button("移除", key=f"del_{s['code']}"):
                    remove_stock(s["code"])
                    st.rerun()

        # 手动输入添加
        st.markdown("---")
        st.markdown("**手动添加**")
        add_cols = st.columns([2, 2, 1, 1])
        new_code = add_cols[0].text_input("代码", placeholder="600519 或 00700", key="manual_code")
        new_name = add_cols[1].text_input("名称", placeholder="贵州茅台", key="manual_name")
        new_market = add_cols[2].selectbox("市场", ["A", "HK"], key="manual_market")
        add_cols[3].write("")  # 占位对齐
        if add_cols[3].button("添加", key="manual_add"):
            if new_code and new_name:
                if add_stock(new_code.strip(), new_name.strip(), new_market):
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
