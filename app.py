"""Streamlit 入口：页面配置 + 移动端适配 + 进程内定时任务 + 侧边栏路由。

页面实现在 `ui/pages/*.py`，跨页共用的展示助手在 `ui/shared.py`、卡片在 `ui/cards.py`
（issue #42 从这个文件拆出去的，拆之前它是 2724 行）。

**这个文件仍然是唯一的入口**（`streamlit run app.py`，测试里也是
`AppTest.from_file("app.py")`），所以顶层照旧有副作用：`set_page_config`、起调度器、
执行路由。**不要 import 它**——要用页面里的东西就直接 import 那个页面模块（那些模块
被设计成 import 无副作用）。
"""

import streamlit as st

from ui.jobs import start_background_jobs
from ui.pages.chat import page_chat
from ui.pages.custom_strategy import page_custom_strategy
from ui.pages.detail import page_detail
from ui.pages.manage import page_manage
from ui.pages.news_list import page_news_list
from ui.pages.overview import page_overview
from ui.pages.sector import page_sector_analysis

st.set_page_config(page_title="A股港股量化分析小程序", layout="wide")

start_background_jobs()

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


# ========== 页面路由 ==========

# 顺序即侧边栏顺序，键即显示名（改名会让老 session 里存的值失效，见 _LEGACY_PAGES）
PAGES = {
    "全局信号总览": page_overview,
    "单股票分析": page_detail,
    "新闻列表": page_news_list,
    "股票池管理": page_manage,
    "板块分析": page_sector_analysis,
    "自定义策略": page_custom_strategy,
    "AI 分析助手": page_chat,
}

# #37 前是两页（板块相关性 / 板块轮动）；老 session 里存的旧值映射到合并后的页，
# 否则重新打开会莫名落回第一页
_LEGACY_PAGES = {"板块相关性": "板块分析", "板块轮动": "板块分析"}

pages = list(PAGES)
default_page = st.session_state.get("page", pages[0])
default_page = _LEGACY_PAGES.get(default_page, default_page)
default_idx = pages.index(default_page) if default_page in pages else 0

page = st.sidebar.radio("页面", pages, index=default_idx)
st.session_state["page"] = page

PAGES[page]()
