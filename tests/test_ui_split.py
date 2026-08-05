"""issue #42：拆分后的 UI 结构自检（不打网络）

拆 app.py 的收益就两条，这个文件把它们钉住：

1. **入口是薄的**——`app.py` 只有配置 + CSS + 路由表，没有页面实现；
2. **页面模块 import 无副作用**——不取数、不起调度器。这是原先做不到的事：
   `import app` 会执行整个 Streamlit 脚本，拿真实股票池跑一遍总览页（实测 95 次
   THS 板块快照），所以 #41 之前数据层被迫抄了四份日K取数、测试被迫用 AST 从源码
   读常量。

顺带用 AppTest 把三个「打开即渲染」的页面各跑一遍（总览/板块/自定义策略/AI 助手
四页在各自的 issue 测试里已有用例）。
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd

ROOT = os.path.join(os.path.dirname(__file__), "..")
APP = os.path.join(ROOT, "app.py")
UI_MODULES = (
    "ui.shared", "ui.cards", "ui.jobs",
    "ui.pages.overview", "ui.pages.detail", "ui.pages.news_list",
    "ui.pages.manage", "ui.pages.sector", "ui.pages.custom_strategy",
    "ui.pages.chat",
)
APP_MAX_LINES = 100


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ---------- 1. 入口是薄的 ----------
def test_app_entry_is_thin():
    src = _src("app.py")
    n = len(src.splitlines())
    assert n <= APP_MAX_LINES, f"app.py 应只剩入口与路由，实际 {n} 行"
    assert "def page_" not in src, "页面实现不该回到 app.py"
    assert "PAGES = {" in src, "路由应是一张表，而不是 if/elif 链"
    assert "elif page ==" not in src


def test_routing_table_covers_every_page():
    src = _src("app.py")
    routes = src.split("PAGES = {", 1)[1].split("}", 1)[0]
    expected = ["全局信号总览", "单股票分析", "新闻列表", "股票池管理", "板块分析",
                "自定义策略", "AI 分析助手"]
    got = [name for name in expected if f'"{name}"' in routes]
    assert got == expected, f"路由表页面缺失或顺序变了：{got}"
    # 每个页面都由自己的模块提供
    for mod in ("overview", "detail", "news_list", "manage", "sector",
                "custom_strategy", "chat"):
        assert f"from ui.pages.{mod} import page_" in src, f"缺 ui.pages.{mod} 的 import"


# ---------- 2. import 无副作用 ----------
def test_ui_modules_import_without_touching_network_or_scheduler():
    """把取数与调度器换成「一调用就炸」，再重新 import 全部 UI 模块"""
    import importlib

    import data.fetcher as fetcher
    import data.realtime as realtime
    from apscheduler.schedulers import background

    def boom(*a, **k):
        raise AssertionError("import UI 模块时不该取数/起调度器")

    saved = (fetcher.fetch_daily_kline, realtime.get_realtime_quotes,
             background.BackgroundScheduler)
    for name in UI_MODULES:
        sys.modules.pop(name, None)
    fetcher.fetch_daily_kline = boom
    realtime.get_realtime_quotes = boom
    background.BackgroundScheduler = boom
    try:
        for name in UI_MODULES:
            importlib.import_module(name)
    finally:
        (fetcher.fetch_daily_kline, realtime.get_realtime_quotes,
         background.BackgroundScheduler) = saved


def test_page_constants_are_importable():
    """常量直接 import 就能读——#42 前测试得用 AST 从 app.py 源码里挖"""
    from ui.pages.overview import COMPACT_ROWS, OVERVIEW_TYPES
    from ui.pages.sector import CELL_TEXT_MAX, HEATMAP_MAX

    assert COMPACT_ROWS == 20 and HEATMAP_MAX == 25 and CELL_TEXT_MAX == 15
    assert OVERVIEW_TYPES == ("index", "sector", "stock")


def test_shared_helpers_have_public_names():
    """跨模块用的东西不许是 _ 开头（#41 定的规则，#42 拆分时一并落实）"""
    import ui.cards as cards
    import ui.shared as shared

    for name in ("get_stock_data", "market_label", "get_recommendation",
                 "signed_color", "render_strategy_params", "SIGNAL_SOURCE_LABELS"):
        assert hasattr(shared, name), f"ui.shared 缺 {name}"
    for name in ("render_news_items", "render_sentiment_card",
                 "render_fundamental_card", "SENTIMENT_ROWS", "SOURCE_BY_PREFIX"):
        assert hasattr(cards, name), f"ui.cards 缺 {name}"


# ---------- 3. 页面能渲染 ----------
class _Env:
    """股票池指到临时文件、DuckDB 指到临时库、取数全 mock（同 test_overview_scale）"""

    def __enter__(self):
        import config.pool as pool
        import data.cache as cache
        import data.fetcher as fetcher
        import data.realtime as realtime

        self.tmp = tempfile.mkdtemp(prefix="ui_split_")
        self.saved = (pool._STOCKS_FILE, cache.CACHE_DB_PATH,
                      fetcher.fetch_daily_kline, realtime.get_realtime_quotes)
        pool._STOCKS_FILE = os.path.join(self.tmp, "stocks.json")
        with open(pool._STOCKS_FILE, "w", encoding="utf-8") as f:
            json.dump([{"code": "600519", "name": "贵州茅台", "market": "A"}], f,
                      ensure_ascii=False)
        cache.CACHE_DB_PATH = os.path.join(self.tmp, "market.duckdb")

        idx = pd.date_range("2026-01-01", periods=200, freq="D", name="date")
        close = pd.Series([100.0 + i * 0.5 for i in range(200)], index=idx)
        df = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                           "close": close, "volume": [1_000_000] * 200}, index=idx)
        fetcher.fetch_daily_kline = lambda symbol, days=365, stock_type=None: df
        realtime.get_realtime_quotes = lambda items: {
            (c, t): {"price": 10.0, "prev_close": 9.9, "change_pct": 1.01}
            for c, t in items}
        return self

    def __exit__(self, *exc):
        import shutil

        import config.pool as pool
        import data.cache as cache
        import data.fetcher as fetcher
        import data.realtime as realtime

        (pool._STOCKS_FILE, cache.CACHE_DB_PATH,
         fetcher.fetch_daily_kline, realtime.get_realtime_quotes) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def _render(page: str):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(APP, default_timeout=600)
    at.session_state["page"] = page
    at.run()
    assert not at.exception, f"{page} 渲染异常：{at.exception}"
    return at


def test_light_pages_render_after_split():
    """「打开即渲染」的三页各跑一遍（总览/板块/自定义策略/AI 助手在各自 issue 的用例里）"""
    with _Env():
        for page in ("新闻列表", "股票池管理", "单股票分析"):
            at = _render(page)
            labels = [r.label for r in at.radio]
            assert "页面" in labels, f"{page} 缺侧边栏路由"


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"[OK] {name}")
    print(f"\n全部 {len(tests)} 项测试通过!")
