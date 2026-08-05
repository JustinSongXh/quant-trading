"""新闻列表页（issue #42 从 app.py 拆出；#1 / #2）"""

import streamlit as st
from data.fetcher import is_hk_stock

from config.pool import load_stock_pool

from ui.cards import (
    SENTIMENT_ROWS, SOURCE_BY_PREFIX, get_news_items, get_sentiment_data,
    render_news_items,
)


def page_news_list():
    from datetime import date

    from data.news import DEFAULT_WINDOW

    st.title("📰 新闻列表")

    # 侧边栏选股（范围 = 股票池；指数/板块无新闻源，不列入）
    stocks = load_stock_pool()
    a_stocks = [s for s in stocks if s.get("market", "A") == "A" and s.get("type") not in ("index", "sector")]
    hk_stocks = [s for s in stocks if s.get("market") == "HK"]

    # 从单股票页跳转过来：一次性预选那只股票（写入 widget state，后续可自由改）
    jump = st.session_state.pop("news_jump", False)
    pre_code = st.session_state.get("news_symbol") if jump else None
    if pre_code:
        st.session_state["news_market"] = "港股" if is_hk_stock(pre_code) else "A股"

    market_tabs = ["A股", "港股"]
    market = st.sidebar.radio("市场", market_tabs, horizontal=True, key="news_market")
    pool = hk_stocks if market == "港股" else a_stocks
    if not pool:
        st.sidebar.warning(f"{market}股票池为空")
        return

    stock_options = {f"{s['name']}({s['code']})": s for s in pool}
    if pre_code:
        for label, s in stock_options.items():
            if s["code"] == pre_code:
                st.session_state["news_stock"] = label
                break
    # 切换市场后旧选项已不在当前池里：清掉残留值，避免 selectbox 因默认值越界报错
    if st.session_state.get("news_stock") not in stock_options:
        st.session_state.pop("news_stock", None)
    selected = st.sidebar.selectbox("选择股票", list(stock_options.keys()), key="news_stock")
    chosen = stock_options[selected]
    symbol = chosen["code"]
    stock_type = chosen.get("type", "stock")
    name = chosen["name"]

    st.caption(f"{name}（{symbol}）· 过去 {DEFAULT_WINDOW} 个交易日窗口 · 公告/新闻/股吧")
    if st.button("← 返回单股票分析"):
        st.session_state["page"] = "单股票分析"
        st.session_state["selected_code"] = symbol
        st.session_state["selected_type"] = stock_type
        st.session_state["auto_analyze"] = True
        st.rerun()

    try:
        with st.spinner("采集新闻并打分中（首次加载模型较慢）…"):
            get_sentiment_data(symbol, stock_type, date.today().isoformat())
            items = get_news_items(symbol, stock_type, date.today().isoformat())
    except Exception as e:
        st.info(f"新闻数据暂不可用：{e}")
        return
    if items is None or items.empty:
        st.info("窗口内暂无新闻/公告/股吧数据")
        return

    src_labels = {"全部": None}
    src_labels.update({label: SOURCE_BY_PREFIX[pref] for pref, label, _ in SENTIMENT_ROWS})
    choice = st.radio("来源", list(src_labels.keys()), horizontal=True)
    src = src_labels[choice]
    sub = items if src is None else items[items["source"] == src]
    st.caption(f"共 {len(sub)} 条")
    render_news_items(sub)

