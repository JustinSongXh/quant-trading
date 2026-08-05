"""板块多时总览页与相关性视图的可用性（#28 / #29 的评论定稿，不打网络）

实测背景：#37 支持批量加板块后，池内板块到了 95 个（个股+指数 26 个），于是
- 总览页 95 行板块把个股挤出屏幕，且每行 9 个 columns + 1 按钮 ≈ 950 个 widget
- 相关性热力图 95×95 = 9025 格、高 3390px，字糊成一片
- 每次扫描多花约 70s（板块 0.74s/个 逐个快照，个股+指数 26 个一次批量 0.2s）

这个文件钉住三件事：**未选中的类型连行情都不请求**、**行数多的组只渲染一个表格**、
**板块多时热力图换成抱团分组**。前两条用 AppTest 真跑一遍页面（mock 掉取数），
第三条是纯函数 + 页面元素断言。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import tempfile

import pandas as pd

import config.pool as pool_cfg
import config.settings as cfg

APP = os.path.join(os.path.dirname(__file__), "..", "app.py")
BARS = 200


def _app_const(name: str):
    """页面模块里的常量。

    #42 之前这里得用 AST 从 `app.py` 源码里读——那时页面全在 app.py 里，而 `import app`
    会执行整个 Streamlit 脚本（顶层就有页面路由），代价是拿真实股票池跑一遍总览页。
    拆分之后页面模块 import 无副作用，直接读属性即可。
    """
    import ui.pages.overview as ov
    import ui.pages.sector as sec

    for module in (ov, sec):
        if hasattr(module, name):
            return getattr(module, name)
    raise AssertionError(f"ui/pages 里找不到常量 {name}")


def _kline(start=100.0, step=0.5):
    # index 必须叫 "date"：data/cache.py::save_kline 靠这个名字 reset_index，
    # 不命名的话它会 KeyError，而 data/kline.py::get_kline 用 except Exception 兜住后
    # 回落成空表——表现出来就是「收益率数据不足」，很难查
    idx = pd.date_range("2026-01-01", periods=BARS, freq="D", name="date")
    close = pd.Series([start + step * i for i in range(BARS)], index=idx)
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": [1_000_000] * BARS}, index=idx)


def _pool(n_sectors=30, n_stocks=3):
    """造一个「板块远多于个股」的池子（复现用户报的形状）"""
    out = [{"code": f"60000{i}", "name": f"股票{i}", "market": "A"} for i in range(n_stocks)]
    out.append({"code": "000300", "name": "沪深300", "market": "A", "type": "index"})
    for i in range(n_sectors):
        out.append({"code": f"8812{i:02d}", "name": f"板块{i}", "market": "A",
                    "type": "sector", "sector_kind": "industry"})
    return out


class _Env:
    """把股票池指到临时文件 + mock 掉所有取数（行情/日K/信号），记录被请求的行情项"""

    def __init__(self, n_sectors=30, n_stocks=3):
        self.n_sectors, self.n_stocks = n_sectors, n_stocks
        self.quote_calls = []

    def __enter__(self):
        # 千万不要在这里 `import app`：app.py 是 Streamlit 脚本，模块顶层就有页面路由，
        # 一 import 就会**拿真实股票池跑一遍总览页**（实测 95 个板块逐个打 THS 快照）。
        # 需要 app.py 里的常量就用 _app_const() 从源码读。
        self.tmp = tempfile.mkdtemp(prefix="ov_test_")
        self.saved_file = pool_cfg._STOCKS_FILE
        pool_cfg._STOCKS_FILE = os.path.join(self.tmp, "stocks.json")
        with open(pool_cfg._STOCKS_FILE, "w", encoding="utf-8") as f:
            json.dump(_pool(self.n_sectors, self.n_stocks), f, ensure_ascii=False)

        import data.cache as cache
        import data.fetcher as fetcher
        import data.realtime as realtime
        # DuckDB 指到临时库：页面会把取到的日K存缓存，不能把造的假行情写进
        # data/cache/market.duckdb（同 test_fund_flow 的做法）
        self.saved_db = cache.CACHE_DB_PATH
        cache.CACHE_DB_PATH = os.path.join(self.tmp, "market.duckdb")
        self.saved = {
            "quotes": realtime.get_realtime_quotes,
            "kline": fetcher.fetch_daily_kline,
            "fresh": None,
        }

        def fake_quotes(items):
            self.quote_calls.append(list(items))
            return {(c, t): {"price": 10.0, "prev_close": 9.9, "change_pct": 1.01}
                    for c, t in items}

        realtime.get_realtime_quotes = fake_quotes
        # 替 data.fetcher 这一枪就够了：#41 起所有日K都经 data/kline.py，而它是按模块
        # 引用 `fetcher.fetch_daily_kline` 的（不是 from-import 绑死函数对象），所以
        # 个股、指数、板块三条路径一起被拦住。#41 前 analysis.correlation 在自己模块
        # 顶层 from-import 了这个名字，必须再单独打一枪，否则板块日K会走真实网络。
        fetcher.fetch_daily_kline = lambda symbol, days=365, stock_type=None: _kline()
        return self

    def __exit__(self, *exc):
        import data.cache as cache
        import data.fetcher as fetcher
        import data.realtime as realtime
        pool_cfg._STOCKS_FILE = self.saved_file
        cache.CACHE_DB_PATH = self.saved_db
        realtime.get_realtime_quotes = self.saved["quotes"]
        fetcher.fetch_daily_kline = self.saved["kline"]
        return False

    def sector_items(self):
        """所有被请求过的行情项里，板块那些"""
        return [it for call in self.quote_calls for it in call if it[1] == "sector"]


def _open(env, types=None, signal_only=False, timeout=600):
    """打开总览页。types 用中文标签（与 multiselect 的选项一致）"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(APP, default_timeout=timeout)
    at.session_state["page"] = "全局信号总览"
    if types is not None:
        at.session_state["ov_types"] = types
    at.session_state["ov_signal_only"] = signal_only
    return at.run()


def _texts(at):
    return "\n".join([m.value for m in at.markdown] + [c.value for c in at.caption]
                     + [i.value for i in at.info] + [w.value for w in at.warning])


# ---------- 1. 未选中的类型既不扫描也不渲染 ----------
def test_unselected_types_are_neither_scanned_nor_rendered():
    with _Env(n_sectors=30) as env:
        at = _open(env, types=["个股（3）", "指数（1）"])
        assert not at.exception, at.exception
        assert env.sector_items() == [], \
            f"去掉「板块」后仍请求了 {len(env.sector_items())} 个板块行情"
        blob = _texts(at)
        assert "### 板块" not in blob, "板块分组不该渲染"
        assert "### A股" in blob and "### 指数" in blob
    print("[OK] 范围里去掉板块 → 不请求板块行情、不渲染板块分组")


def test_selecting_sectors_scans_them():
    with _Env(n_sectors=5) as env:
        at = _open(env, types=["个股（3）", "指数（1）", "板块（5）"])
        assert not at.exception, at.exception
        assert len(env.sector_items()) == 5, env.quote_calls
        assert "### 板块" in _texts(at)
    print("[OK] 勾上板块 → 板块被扫描并渲染")


def test_scope_change_triggers_rescan():
    """范围进指纹：勾上板块后必须重扫，否则页面上根本不出现板块"""
    with _Env(n_sectors=4) as env:
        at = _open(env, types=["个股（3）"])
        assert env.sector_items() == []
        ms = next(m for m in at.multiselect if m.label == "扫描范围")
        at2 = ms.set_value(["个股（3）", "板块（4）"]).run()
        assert not at2.exception, at2.exception
        assert len(env.sector_items()) == 4, "改了范围没有重扫"
        assert "### 板块" in _texts(at2)
    print("[OK] 扫描范围变化会触发重扫（指纹带上了范围）")


def test_empty_scope_is_handled():
    with _Env(n_sectors=3) as env:
        at = _open(env, types=[])
        assert not at.exception, at.exception
        assert "扫描范围是空的" in _texts(at)
        assert env.quote_calls == [], "范围为空时不该请求任何行情"
    print("[OK] 范围为空时给提示、不取数")


# ---------- 2. 行数多的组改紧凑表格 ----------
def test_large_group_renders_one_table_without_per_row_buttons():
    with _Env(n_sectors=30, n_stocks=3) as env:
        at = _open(env)
        assert not at.exception, at.exception
        # 30 > COMPACT_ROWS(20)：板块组走紧凑表格
        assert _app_const("COMPACT_ROWS") == 20
        assert len(at.dataframe) >= 1, "紧凑表格没渲染"
        # 逐行「详情」按钮只该属于小组（个股 3 + 指数 1 = 4 个）
        detail_btns = [b for b in at.button if b.label == "详情"]
        assert len(detail_btns) == 4, \
            f"逐行按钮应只剩小组的 4 个，实际 {len(detail_btns)} 个"
        assert any(b.label == "查看详情" for b in at.button), "紧凑表格缺「查看详情」入口"
        assert "已改用紧凑表格" in _texts(at)
    print("[OK] >20 行的组只渲染一个表格 + 一个详情入口，不再逐行按钮")


def test_small_group_keeps_per_row_buttons():
    with _Env(n_sectors=5, n_stocks=3) as env:
        at = _open(env)
        assert not at.exception, at.exception
        detail_btns = [b for b in at.button if b.label == "详情"]
        assert len(detail_btns) == 9, f"5 板块+3 个股+1 指数 = 9 行逐行按钮，实际 {len(detail_btns)}"
        assert "已改用紧凑表格" not in _texts(at)
    print("[OK] ≤20 行仍是逐行按钮版（体感不变）")


# ---------- 3. 组头摘要与「只看有信号的」 ----------
def test_group_header_has_summary_counts():
    with _Env(n_sectors=6, n_stocks=3) as env:
        at = _open(env)
        blob = _texts(at)
        assert "### 板块（6 个 ·" in blob, blob[:400]
        assert "买入" in blob and "卖出" in blob and "观望" in blob
    print("[OK] 组头带「N 个 · 买入 x · 卖出 y · 观望 z」摘要")


def test_signal_only_filter():
    """造的行情是单调上涨，技术信号恒定 → 要么全有信号要么全无，两种情况都不许崩"""
    with _Env(n_sectors=6, n_stocks=3) as env:
        at = _open(env, signal_only=True)
        assert not at.exception, at.exception
        blob = _texts(at)
        assert ("已筛出" in blob) or ("没有触发信号的标的" in blob), blob[:400]
    print("[OK]「只看有信号的」筛选生效且不崩")


# ---------- 4. 相关性：分组视图 ----------
def _corr_mat(n, blocks=2):
    """构造 blocks 个明显分离的簇：同簇内 0.9、跨簇 -0.4"""
    names = [f"板块{i}" for i in range(n)]
    data = []
    for i in range(n):
        row = []
        for j in range(n):
            if i == j:
                row.append(1.0)
            elif i % blocks == j % blocks:
                row.append(0.9)
            else:
                row.append(-0.4)
        data.append(row)
    return pd.DataFrame(data, index=names, columns=names)


def test_cluster_groups_splits_two_clusters():
    from analysis.correlation import cluster_groups

    mat = _corr_mat(8, blocks=2)
    groups = cluster_groups(mat, max_groups=2)
    assert len(groups) == 2, groups
    assert all(len(g["members"]) == 4 for g in groups), groups
    for g in groups:
        idx = {int(m.replace("板块", "")) % 2 for m in g["members"]}
        assert len(idx) == 1, f"一组里混了两个簇：{g['members']}"
        assert abs(g["avg_corr"] - 0.9) < 1e-6, g
    print(f"[OK] cluster_groups 正确切成 2 组，组内平均相关 {groups[0]['avg_corr']}")


def test_cluster_groups_sorted_by_cohesion_and_degrades():
    from analysis.correlation import cluster_groups

    mat = pd.DataFrame([[1.0, 0.3, -0.1], [0.3, 1.0, -0.2], [-0.1, -0.2, 1.0]],
                       index=list("ABC"), columns=list("ABC"))
    groups = cluster_groups(mat, max_groups=2)
    corrs = [g["avg_corr"] for g in groups if g["avg_corr"] is not None]
    assert corrs == sorted(corrs, reverse=True), groups
    # 单成员组的 avg_corr 必须是 None（给 0 会被读成「不相关」）
    assert any(g["avg_corr"] is None for g in groups if len(g["members"]) == 1)
    # N<3 与空矩阵都不许抛
    assert len(cluster_groups(mat.iloc[:2, :2])) == 1
    assert cluster_groups(pd.DataFrame()) == []
    print("[OK] 分组按抱团紧密度降序，单成员组 avg_corr 为 None，退化输入不抛异常")


def test_cluster_groups_survives_missing_scipy():
    """scipy 不可用 → 全员一组，不抛（与 cluster_order 同样的降级）"""
    import builtins

    import analysis.correlation as corr

    real_import = builtins.__import__

    def no_scipy(name, *a, **kw):
        if name.startswith("scipy"):
            raise ImportError("no scipy")
        return real_import(name, *a, **kw)

    builtins.__import__ = no_scipy
    try:
        groups = corr.cluster_groups(_corr_mat(6))
    finally:
        builtins.__import__ = real_import
    assert len(groups) == 1 and len(groups[0]["members"]) == 6
    print("[OK] scipy 缺失时退回全员一组")


def test_pairs_from_matrix_both_directions():
    from analysis.correlation import pairs_from_matrix

    mat = pd.DataFrame([[1.0, 0.8, -0.6], [0.8, 1.0, float("nan")], [-0.6, float("nan"), 1.0]],
                       index=list("ABC"), columns=list("ABC"))
    neg = pairs_from_matrix(mat, top_n=5, positive=False)
    pos = pairs_from_matrix(mat, top_n=5, positive=True)
    assert neg[0] == ("A", "C", -0.6) and pos[0] == ("A", "B", 0.8)
    assert len(neg) == len(pos) == 2, "NaN 的那一对必须被跳过"
    assert pairs_from_matrix(pd.DataFrame(), 5) == []
    print("[OK] pairs_from_matrix 正负两向排序正确、跳过 NaN")


def test_hedge_pairs_regression():
    """hedge_pairs 现在是 pairs_from_matrix 的薄封装，行为不能变"""
    import analysis.correlation as corr

    saved = corr.corr_matrix
    corr.corr_matrix = lambda codes, window=60, method="pearson", mode="raw": _corr_mat(4)
    try:
        pairs = corr.hedge_pairs(["a", "b", "c", "d"], top_n=3)
    finally:
        corr.corr_matrix = saved
    assert len(pairs) == 3
    assert all(p[2] == -0.4 for p in pairs), pairs
    assert pairs == sorted(pairs, key=lambda p: p[2]), "仍必须按系数升序（最负在前）"
    print("[OK] hedge_pairs 行为不变")


def test_correlation_page_switches_to_groups_when_many_sectors():
    """页面级：板块数 > HEATMAP_MAX 时默认给分组视图，不画全矩阵"""
    heatmap_max = _app_const("HEATMAP_MAX")
    with _Env(n_sectors=30, n_stocks=1) as env:
        at = _open(env, types=["个股（1）"])          # 先别扫板块，省时间
        assert not at.exception, at.exception
        radios = [r for r in at.sidebar.radio if r.label == "页面"]
        at2 = radios[0].set_value("板块分析").run() if radios else at
        assert not at2.exception, at2.exception
        blob = _texts(at2)
        assert heatmap_max == 25
        assert f"当前 30 个板块（> {heatmap_max}）" in blob, blob[:600]
        assert "抱团分组" in blob
        assert any(m.label and "画哪一组" in m.label for m in at2.selectbox), \
            "缺「画哪一组的矩阵」选择器"
    print("[OK] 30 个板块时相关性页默认走分组视图")


def test_correlation_page_keeps_full_matrix_when_few_sectors():
    with _Env(n_sectors=6, n_stocks=1) as env:
        at = _open(env, types=["个股（1）"])
        radios = [r for r in at.sidebar.radio if r.label == "页面"]
        at2 = radios[0].set_value("板块分析").run() if radios else at
        assert not at2.exception, at2.exception
        blob = _texts(at2)
        # 判据用分组视图**独有的控件与文案**，不要用「抱团分组」这个词——相关对排行 tab 的
        # 说明里也提到它，拿词做否定断言会假阳性
        assert not any(s.label and "画哪一组" in s.label for s in at2.selectbox), \
            "6 个板块不该出现分组选择器"
        assert "全矩阵有" not in blob, "6 个板块不该给「板块太多」的提示"
        assert "红=正相关" in blob, "全矩阵的说明没出现"
    print("[OK] 6 个板块时仍画全矩阵（行为不变）")


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"[OK] {name}")
    print(f"\n全部 {len(tests)} 项测试通过!")
