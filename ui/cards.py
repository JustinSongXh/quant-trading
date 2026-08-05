"""单股票页的情绪卡与基本面卡（issue #42 从 app.py 拆出；#2 / #33）

「新闻列表」页也用这里的新闻渲染与取数包装。
"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
from data.fetcher import is_hk_stock


SENTIMENT_ROWS = [
    ("announcement", "公告", "#7B1FA2"),
    ("news", "新闻", "#2196F3"),
    ("guba", "股吧", "#FF6F00"),
]


SOURCE_BY_PREFIX = {"announcement": "cninfo", "news": "em_news", "guba": "em_guba"}


@st.cache_data(ttl=1800, show_spinner=False)
def get_sentiment_data(symbol, stock_type, day):
    """过去 10 个交易日的分源情绪（缓存：同股同日不重复下载/打分）。day 用于按日失效。"""
    from analysis.sentiment import analyze_sentiment
    return analyze_sentiment(symbol, stock_type=stock_type)


@st.cache_data(ttl=1800, show_spinner=False)
def get_news_items(symbol, stock_type, day):
    """窗口内全部新闻条目（按 published_at 倒序），供明细列表展示。day 用于按日失效。

    依赖 get_sentiment_data 已先行采集+打分，这里只读缓存。
    """
    from data.cache import load_news_items
    from data.news import DEFAULT_WINDOW, trading_window_start
    return load_news_items(symbol, since=trading_window_start(DEFAULT_WINDOW))


def _sentiment_badge(score):
    """单条情绪得分 → emoji 标记（-1/0/+1 三档）"""
    if score is None or pd.isna(score):
        return "⚪"
    if score > 0.3:
        return "🟢"
    if score < -0.3:
        return "🔴"
    return "⚪"


def render_news_items(items, source_filter=None, limit=None):
    """渲染一组新闻条目：每条可展开看摘要 + 跳原文链接"""
    from data.news import news_detail_url

    if source_filter:
        items = items[items["source"] == source_filter]
    if limit:
        items = items.head(limit)
    if items.empty:
        st.caption("窗口内暂无数据")
        return
    for r in items.itertuples():
        ts = pd.to_datetime(r.published_at).strftime("%m-%d %H:%M")
        title = r.title or "(无标题)"
        with st.expander(f"{_sentiment_badge(r.sentiment_score)}  {ts}　{title}"):
            if r.content:
                st.write(r.content)
            else:
                st.caption("（该来源未抓取正文，点击下方链接查看原文）")
            url = news_detail_url(
                r.source, r.stock_code, r.external_id, pd.to_datetime(r.published_at)
            )
            if url:
                st.markdown(f"[🔗 查看原文]({url})")


def render_sentiment_card(symbol, stock_type):
    """单股票分析页：过去 10 个交易日情绪卡片（公告/新闻/股吧三行得分曲线+条数）"""
    from datetime import date

    st.markdown("---")
    st.subheader("📰 过去 10 个交易日情绪")
    st.caption("本地金融情绪模型（中文 BERT，3 分类 → -1~+1）对公告/新闻/股吧分别打分，仅作参考")
    try:
        with st.spinner("采集新闻并打分中（首次加载模型较慢）…"):
            df = get_sentiment_data(symbol, stock_type, date.today().isoformat())
    except Exception as e:
        st.info(f"情绪数据暂不可用：{e}")
        return
    if df is None or df.empty:
        st.info("窗口内暂无可用新闻/公告/股吧数据")
        return

    # 概览：各源均值 + 总条数
    cols = st.columns(3)
    for col, (pref, label, _) in zip(cols, SENTIMENT_ROWS):
        cnt = int(df[f"{pref}_count"].sum())
        if cnt:
            avg = (df[f"{pref}_score"] * df[f"{pref}_count"]).sum() / cnt
            col.metric(f"{label}情绪", f"{avg:+.2f}", f"{cnt} 条")
        else:
            col.metric(f"{label}情绪", "—", "0 条")

    # 三行得分曲线（marker 大小随条数）
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
        subplot_titles=[f"{label}情绪" for _, label, _ in SENTIMENT_ROWS],
    )
    for i, (pref, label, color) in enumerate(SENTIMENT_ROWS, start=1):
        sc = df[f"{pref}_score"]
        cnt = df[f"{pref}_count"]
        mask = cnt > 0
        fig.add_trace(go.Scatter(
            x=df.index[mask], y=sc[mask], mode="lines+markers", name=label,
            line=dict(color=color, width=1.5),
            marker=dict(size=[max(6, min(22, 6 + c)) for c in cnt[mask]], color=color),
            customdata=cnt[mask],
            hovertemplate=f"{label} %{{x}}<br>得分 %{{y:.2f}}<br>条数 %{{customdata}}<extra></extra>",
        ), row=i, col=1)
        fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=i, col=1)
        fig.update_yaxes(range=[-1.1, 1.1], row=i, col=1)
    fig.update_layout(height=480, showlegend=False, margin=dict(l=50, r=20, t=40, b=20))
    st.plotly_chart(fig, use_container_width=True)

    # 各源最新 10 条新闻明细
    items = get_news_items(symbol, stock_type, date.today().isoformat())
    st.markdown("##### 📋 最新新闻明细（每源最新 10 条）")
    tabs = st.tabs([label for _, label, _ in SENTIMENT_ROWS])
    for tab, (pref, label, _) in zip(tabs, SENTIMENT_ROWS):
        with tab:
            render_news_items(items, source_filter=SOURCE_BY_PREFIX[pref], limit=10)

    if st.button("📰 查看全部新闻列表 →", key=f"news_more_{symbol}"):
        st.session_state["page"] = "新闻列表"
        st.session_state["news_symbol"] = symbol
        st.session_state["news_jump"] = True
        st.rerun()


# ========== 基本面卡片（issue #33） ==========

@st.cache_data(ttl=1800, show_spinner=False)
def get_fundamentals_data(symbol, stock_type, day):
    """基本面原始数据（底下还有 24h 文件缓存，这层只是省掉重复解析）。day 用于按日失效。"""
    from data.fundamentals import get_fundamentals
    return get_fundamentals(symbol, stock_type=stock_type)


def _fmt_money(v):
    """173964000000.0 → '1739.64亿'"""
    if v is None:
        return "—"
    for unit, scale in (("万亿", 1e12), ("亿", 1e8), ("万", 1e4)):
        if abs(v) >= scale:
            return f"{v / scale:.2f}{unit}"
    return f"{v:.2f}"


def _fmt_pct(v):
    return "—" if v is None else f"{v:.2f}%"


def _flag_color(flag):
    """标签着色：利空红、利好绿，其余灰"""
    if any(k in flag for k in ("下滑", "恶化", "走弱", "下降", "为负", "新高")):
        return "#c0392b"
    if any(k in flag for k in ("改善", "走强", "上升", "新低")):
        return "#27ae60"
    return "#7f8c8d"


def render_fundamental_card(symbol, stock_type):
    """单股票分析页：基本面卡片（确定性层，零 token 成本，不产生任何交易信号）"""
    from datetime import date

    from analysis.fundamental import summarize

    st.markdown("---")
    st.subheader("📊 基本面")
    st.caption("同花顺财报摘要：单季度近 12 季判趋势、年度近 5 年看水平；仅作参考，不参与信号与回测")

    if stock_type in ("index", "sector") or is_hk_stock(symbol):
        st.info("基本面数据仅支持 A 股")
        return
    try:
        with st.spinner("拉取财报摘要中…"):
            fund = get_fundamentals_data(symbol, stock_type, date.today().isoformat())
    except Exception as e:
        st.info(f"基本面数据暂不可用：{e}")
        return
    if not fund:
        st.info("基本面数据暂不可用")
        return
    try:
        s = summarize(fund)
    except Exception as e:
        st.info(f"基本面数据暂不可用：{e}")
        return

    latest = s["latest"]
    st.markdown(f"##### 最新单季（{s['period'] or '—'}）")
    cols = st.columns(6)
    cols[0].metric("营业总收入", _fmt_money(latest["营业总收入"]),
                   _fmt_pct(latest["营业总收入同比增长率"]))
    cols[1].metric("净利润", _fmt_money(latest["净利润"]),
                   _fmt_pct(latest["净利润同比增长率"]))
    cols[2].metric("销售净利率", _fmt_pct(latest["销售净利率"]))
    cols[3].metric("净资产收益率", _fmt_pct(latest["净资产收益率"]),
                   f"近 8 季均值 {_fmt_pct(s['roe_level']['avg8'])}", delta_color="off")
    pct = s["leverage"]["percentile"]
    pct_text = "—" if pct is None else f"{pct:.0f}%"
    cols[4].metric("资产负债率", _fmt_pct(latest["资产负债率"]),
                   f"近 {s['leverage']['years']} 年分位 {pct_text}", delta_color="off")
    cols[5].metric("营收同比趋势", s["revenue_trend"]["direction"],
                   f"连续 {s['revenue_trend']['quarters']} 季", delta_color="off")

    if s["flags"]:
        badges = "".join(
            f'<span style="display:inline-block;background:{_flag_color(f)};color:#fff;'
            f'border-radius:10px;padding:2px 10px;margin:2px 6px 2px 0;font-size:13px">{f}</span>'
            for f in s["flags"]
        )
        st.markdown(badges, unsafe_allow_html=True)

    # 近 12 季单季同比（营收 + 净利）
    rev = s["revenue_trend"]["series"]
    prof = s["profit_trend"]["series"]
    if rev or prof:
        fig = go.Figure()
        for ser, label, color in ((rev, "营收同比", "#2196F3"), (prof, "净利同比", "#FF6F00")):
            if ser:
                fig.add_trace(go.Scatter(
                    x=[r["报告期"] for r in ser], y=[r["同比"] for r in ser],
                    mode="lines+markers", name=label, line=dict(color=color, width=1.8),
                    hovertemplate=f"{label} %{{x}}<br>%{{y:.2f}}%<extra></extra>",
                ))
        fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5)
        fig.update_layout(height=300, margin=dict(l=60, r=20, t=30, b=20),
                          yaxis_title="单季同比 (%)",
                          legend=dict(orientation="h", yanchor="bottom", y=1.01))
        st.plotly_chart(fig, use_container_width=True)

    # 近 5 年年度指标
    a_rows = fund["annual"]["rows"]
    if a_rows:
        st.markdown("##### 近 5 年年度指标")
        table = [{
            "年度": r["报告期"],
            "营业总收入": _fmt_money(r["营业总收入"]),
            "营收同比": _fmt_pct(r["营业总收入同比增长率"]),
            "净利润": _fmt_money(r["净利润"]),
            "净利同比": _fmt_pct(r["净利润同比增长率"]),
            "销售净利率": _fmt_pct(r["销售净利率"]),
            "净资产收益率": _fmt_pct(r["净资产收益率"]),
            "资产负债率": _fmt_pct(r["资产负债率"]),
        } for r in reversed(a_rows)]
        st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)

