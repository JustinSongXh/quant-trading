"""issue #35：板块内选股（两级漏斗 + 确定性排序）的测试（mock 取数，不打网络）

覆盖 issue 列的 6 项：84 只 → 粗筛 20 只的漏斗计数、top_n 可配置、排序分的权重来源与
候选集内 z-score、基本面取不到时该项为 0 且该行有标注、个股日K失败只跳过那一只、
**不调用 Kronos**（最后一项用真的 build_signals 跑，把 kronos 模块的入口换成会记账的
假函数——只断言 PICK_SIGNALS 常量不含 kronos 是自欺，得断言那条路真的没被走过）。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd

import analysis.sector_pick as sp
import config.settings as cfg

BARS = 220


def _kline(daily_pct, bars=BARS, start=100.0):
    """恒定日收益率的 OHLCV（真 build_signals 也能吃）"""
    idx = pd.date_range("2026-01-01", periods=bars, freq="D")
    close = pd.Series([start * (1 + daily_pct) ** i for i in range(bars)], index=idx)
    return pd.DataFrame({"open": close * 0.995, "high": close * 1.01,
                         "low": close * 0.99, "close": close,
                         "volume": [1_000_000 + 1000 * i for i in range(bars)]},
                        index=idx)


def _cons_payload(n, stale=False, prefix=600000):
    """n 只成分股，涨跌幅递减（code 越小涨得越多，粗筛该取前几只）"""
    return {
        "sector": "消费电子", "em_code": "BK1036", "em_name": "消费电子",
        "match_level": "exact", "fetched_at": "2026-08-04 03:20", "stale": stale,
        "constituents": [{"code": f"{prefix + i}", "name": f"股票{i}",
                          "price": 10.0 + i, "change_pct": round(9.0 - i * 0.1, 2)}
                         for i in range(n)],
    }


class _SignalSpy:
    """假 build_signals：按代码给定的技术/缠论值造一张只有最后一行有用的表"""

    def __init__(self, tech_by_code=None, chan_by_code=None):
        self.tech = tech_by_code or {}
        self.chan = chan_by_code or {}
        self.calls = []

    def __call__(self, kline_df, symbol="stock", sentiment_scores=None,
                 enabled_signals=None, progress_cb=None):
        self.calls.append({"symbol": symbol, "enabled_signals": enabled_signals})
        out = kline_df.copy()
        out["technical_signal"] = self.tech.get(symbol, 0.1)
        out["chanlun_signal"] = self.chan.get(symbol, 0.0)
        return out


def _patch(cons=None, klines=None, sector_kline=None, signals=None,
           fundamentals=None, default_kline=None):
    """替掉 sector_pick 里的取数依赖（都是模块级 import，直接赋属性即可）

    Returns: (还原函数, signal spy)
    """
    saved = {k: getattr(sp, k) for k in
             ("get_sector_constituents", "get_kline",
              "build_signals", "get_fundamentals", "summarize", "unavailable_reason",
              "get_sector_info")}
    klines = klines or {}
    base = default_kline if default_kline is not None else _kline(0.001)
    spy = signals or _SignalSpy()

    # 板块名/类型来自股票池配置，而 config/stocks.json 是本机文件（不入 git）——测试不能
    # 依赖它，这里直接给定
    sp.get_sector_info = lambda code: ("消费电子", "industry")
    sp.get_sector_constituents = lambda name, kind, force_refresh=False, attempts=1: cons
    # #41 起个股/板块日K都走 data/kline.py::get_kline，只差 stock_type——测试也只剩
    # 一个替身，按 stock_type 分派（原先是 _get_stock_kline / _get_sector_kline 两个）
    def _kline_stub(code, days=sp.KLINE_DAYS, stock_type=None, force_refresh=False):
        if stock_type == "sector":
            return sector_kline if sector_kline is not None else _kline(0.0005)
        return klines.get(code, base)

    sp.get_kline = _kline_stub
    sp.build_signals = spy
    sp.get_fundamentals = lambda code, *a, **k: (fundamentals or {}).get(code)
    sp.summarize = lambda fund: fund       # 测试里 fundamentals 直接给 {"flags": [...]}
    sp.unavailable_reason = lambda name, kind: "未匹配到东财板块（测试）"

    def restore():
        for k, v in saved.items():
            setattr(sp, k, v)

    return restore, spy


# ========== 1. 漏斗计数 ==========

def test_funnel_84_to_20():
    """84 只成分股 → 粗筛 20 只 → total_constituents=84, computed=20，且不许静默截断"""
    restore, spy = _patch(cons=_cons_payload(84))
    try:
        res = sp.pick("881124", "industry")
    finally:
        restore()
    assert res["available"] is True
    assert res["total_constituents"] == 84
    assert res["top_n"] == cfg.SECTOR_PICK_TOP_N == 20
    assert res["computed"] == 20 == len(res["candidates"])
    assert len(spy.calls) == 20, "细算只该跑粗筛出来的那 20 只"
    # 粗筛按涨跌幅降序：涨得最多的 20 只（code 600000~600019）
    picked = {c["code"] for c in res["candidates"]}
    assert picked == {str(600000 + i) for i in range(20)}
    print(f"[OK] 漏斗 {res['total_constituents']} → {res['computed']}")


def test_top_n_is_configurable():
    restore, _ = _patch(cons=_cons_payload(84))
    try:
        res5 = sp.pick("881124", "industry", top_n=5)
        res40 = sp.pick("881124", "industry", top_n=40)
    finally:
        restore()
    assert (res5["computed"], res5["top_n"]) == (5, 5)
    assert (res40["computed"], res40["top_n"]) == (40, 40)
    assert res5["total_constituents"] == res40["total_constituents"] == 84


def test_top_n_larger_than_constituents():
    """N 大于成分股数就是全算，computed 不能超过实际只数"""
    restore, _ = _patch(cons=_cons_payload(3))
    try:
        res = sp.pick("881124", "industry", top_n=20)
    finally:
        restore()
    assert res["total_constituents"] == 3 and res["computed"] == 3


def test_coarse_screen_puts_missing_change_pct_last():
    """涨跌幅缺失（停牌/限流给 '-'）的排最后：缺失不代表跌，但也不该挤掉有数据的"""
    cons = _cons_payload(3)
    cons["constituents"].append({"code": "600099", "name": "停牌股",
                                 "price": None, "change_pct": None})
    restore, _ = _patch(cons=cons)
    try:
        res = sp.pick("881124", "industry", top_n=3)
    finally:
        restore()
    assert "600099" not in {c["code"] for c in res["candidates"]}


# ========== 2. 排序分：权重 + 候选集内 z-score ==========

def test_score_uses_weights_from_settings():
    """权重从 SECTOR_PICK_WEIGHTS 读（运行时），改了立刻生效"""
    cons = _cons_payload(3)
    techs = {"600000": 0.9, "600001": 0.0, "600002": -0.9}
    restore, _ = _patch(cons=cons, signals=_SignalSpy(tech_by_code=techs),
                        klines={c: _kline(0.001) for c in techs})
    old = cfg.SECTOR_PICK_WEIGHTS
    try:
        # 只留技术项：排序分应当等于 z(技术信号)
        cfg.SECTOR_PICK_WEIGHTS = {"technical": 1.0, "excess": 0.0, "fundamental": 0.0}
        res = sp.pick("881124", "industry", top_n=3)
        by_code = {c["code"]: c for c in res["candidates"]}
        for code in techs:
            assert by_code[code]["score"] == by_code[code]["z_technical"]
        # 技术项权重翻倍 → 排序分翻倍
        cfg.SECTOR_PICK_WEIGHTS = {"technical": 2.0, "excess": 0.0, "fundamental": 0.0}
        res2 = sp.pick("881124", "industry", top_n=3)
        by_code2 = {c["code"]: c for c in res2["candidates"]}
        for code in techs:
            assert abs(by_code2[code]["score"] - 2 * by_code[code]["score"]) < 1e-6
        assert res2["weights"] == {"technical": 2.0, "excess": 0.0, "fundamental": 0.0}
    finally:
        cfg.SECTOR_PICK_WEIGHTS = old
        restore()


def test_zscore_is_computed_within_candidate_set():
    """z-score 只在候选集内算：84 只里筛出 3 只，z 就该按这 3 只的均值/标准差算"""
    techs = {"600000": 1.0, "600001": 0.0, "600002": -1.0}
    restore, _ = _patch(cons=_cons_payload(84), signals=_SignalSpy(tech_by_code=techs))
    try:
        res = sp.pick("881124", "industry", top_n=3)
    finally:
        restore()
    got = {c["code"]: c["z_technical"] for c in res["candidates"]}
    expect = pd.Series([1.0, 0.0, -1.0])
    mu, sd = expect.mean(), expect.std()      # ddof=1，同 rotation.zscore
    assert abs(got["600000"] - (1.0 - mu) / sd) < 1e-3
    assert abs(got["600002"] - (-1.0 - mu) / sd) < 1e-3
    # 其余 81 只（默认技术值 0.1）没参与统计
    assert res["computed"] == 3


def test_ranking_is_by_score_desc():
    techs = {"600000": -0.9, "600001": 0.9, "600002": 0.0}
    restore, _ = _patch(cons=_cons_payload(3), signals=_SignalSpy(tech_by_code=techs))
    old = cfg.SECTOR_PICK_WEIGHTS
    try:
        cfg.SECTOR_PICK_WEIGHTS = {"technical": 1.0, "excess": 0.0, "fundamental": 0.0}
        res = sp.pick("881124", "industry", top_n=3)
    finally:
        cfg.SECTOR_PICK_WEIGHTS = old
        restore()
    scores = [c["score"] for c in res["candidates"]]
    assert scores == sorted(scores, reverse=True)
    assert res["candidates"][0]["code"] == "600001"


# ========== 3. 相对板块超额收益 ==========

def test_excess_is_stock_minus_sector_return():
    stock, sector = _kline(0.002), _kline(0.001)
    restore, _ = _patch(cons=_cons_payload(1), klines={"600000": stock},
                        sector_kline=sector)
    try:
        res = sp.pick("881124", "industry", top_n=1, window=20)
    finally:
        restore()
    c = res["candidates"][0]
    exp_stock = ((1 + 0.002) ** 20 - 1) * 100
    exp_sector = ((1 + 0.001) ** 20 - 1) * 100
    assert abs(c["stock_return"] - exp_stock) < 1e-2
    assert abs(res["sector_return"] - exp_sector) < 1e-2
    assert abs(c["excess"] - (exp_stock - exp_sector)) < 1e-2


def test_missing_sector_index_degrades_without_crash():
    """板块指数取不到 → 超额为 None、该项按 0 计、每行标注 + 顶层 warning（不静默）"""
    restore, _ = _patch(cons=_cons_payload(2), sector_kline=pd.DataFrame())
    try:
        res = sp.pick("881124", "industry", top_n=2)
    finally:
        restore()
    assert res["sector_return"] is None
    for c in res["candidates"]:
        assert c["excess"] is None
        assert sp.NOTE_NO_SECTOR_RET in c["note"]
        assert c["score"] is not None
    assert any("超额" in w for w in res["warnings"])


# ========== 4. 基本面加减分 ==========

def test_fundamental_flag_score_mapping():
    """flags → 加减分：正向 +0.5、负向 −0.5，认不出的给 0 并报上来"""
    assert sp.flag_score(["ROE 处于上升通道", "销售净利率走强"]) == (1.0, [])
    assert sp.flag_score(["最新单季净利润为负"]) == (-0.5, [])
    assert sp.flag_score(["连续3季营收同比下滑", "营收同比连续3季恶化"]) == (-1.0, [])
    assert sp.flag_score(["净利同比连续4季改善"]) == (0.5, [])
    assert sp.flag_score(["资产负债率创5年新高"]) == (-0.5, [])
    assert sp.flag_score(["资产负债率创5年新低"]) == (0.5, [])
    assert sp.flag_score([]) == (0.0, [])
    assert sp.flag_score(None) == (0.0, [])         # 取不到基本面
    score, unknown = sp.flag_score(["某个新标签"])
    assert score == 0.0 and unknown == ["某个新标签"]


def test_fundamentals_unavailable_scores_zero_and_annotates_row():
    """取不到基本面 → 该项 0 分 + 该行标注（不静默当 0）；有基本面的照常加减分"""
    restore, _ = _patch(cons=_cons_payload(2),
                        fundamentals={"600000": {"flags": ["ROE 处于上升通道"]}})
    old = cfg.SECTOR_PICK_WEIGHTS
    try:
        cfg.SECTOR_PICK_WEIGHTS = {"technical": 0.0, "excess": 0.0, "fundamental": 1.0}
        res = sp.pick("881124", "industry", top_n=2)
    finally:
        cfg.SECTOR_PICK_WEIGHTS = old
        restore()
    by_code = {c["code"]: c for c in res["candidates"]}
    assert by_code["600000"]["fund_score"] == 0.5
    assert by_code["600000"]["score"] == 0.5
    assert sp.NOTE_NO_FUND not in by_code["600000"]["note"]
    assert by_code["600001"]["fund_flags"] is None
    assert by_code["600001"]["fund_score"] == 0.0
    assert sp.NOTE_NO_FUND in by_code["600001"]["note"]
    assert by_code["600001"]["score"] == 0.0


def test_fundamental_exception_does_not_break_pick():
    restore, _ = _patch(cons=_cons_payload(2))
    sp.get_fundamentals = lambda code, *a, **k: (_ for _ in ()).throw(RuntimeError("接口挂了"))
    try:
        res = sp.pick("881124", "industry", top_n=2)
    finally:
        restore()
    assert res["computed"] == 2
    assert all(sp.NOTE_NO_FUND in c["note"] for c in res["candidates"])


# ========== 5. 单只失败只跳过那一只 ==========

def test_failed_kline_skips_only_that_stock():
    restore, _ = _patch(cons=_cons_payload(3),
                        klines={"600001": pd.DataFrame()})     # 这只取不到日K
    try:
        res = sp.pick("881124", "industry", top_n=3)
    finally:
        restore()
    assert res["computed"] == 2
    assert {c["code"] for c in res["candidates"]} == {"600000", "600002"}
    assert any("600001" in w and "跳过" in w for w in res["warnings"])
    assert res["total_constituents"] == 3, "被跳过的只数不影响成分股总数的口径"


def test_short_kline_is_skipped_with_reason():
    restore, _ = _patch(cons=_cons_payload(2),
                        klines={"600001": _kline(0.001, bars=10)})
    try:
        res = sp.pick("881124", "industry", top_n=2, window=20)
    finally:
        restore()
    assert res["computed"] == 1
    assert any("不足" in w and "600001" in w for w in res["warnings"])


def test_signal_exception_skips_only_that_stock():
    def boom(kline_df, symbol="stock", **kw):
        if symbol == "600001":
            raise RuntimeError("缠论炸了")
        out = kline_df.copy()
        out["technical_signal"], out["chanlun_signal"] = 0.2, 0.0
        return out

    restore, _ = _patch(cons=_cons_payload(3), signals=boom)
    try:
        res = sp.pick("881124", "industry", top_n=3)
    finally:
        restore()
    assert res["computed"] == 2
    assert any("600001" in w and "信号计算失败" in w for w in res["warnings"])


def test_all_candidates_failing_returns_empty_not_crash():
    restore, _ = _patch(cons=_cons_payload(2), default_kline=pd.DataFrame())
    try:
        res = sp.pick("881124", "industry", top_n=2)
    finally:
        restore()
    assert res["available"] is True and res["candidates"] == []
    assert res["computed"] == 0 and len(res["warnings"]) >= 2


# ========== 6. 不调用 Kronos ==========

def test_kronos_is_never_invoked():
    """用**真的** build_signals 跑一遍，把 kronos 的两个入口换成会记账的假函数。

    只断言 PICK_SIGNALS 不含 kronos 是自欺（build_signals 内部可能另有路径），所以这里
    断言那条路真的没被走过——Kronos 单只 CPU 上约 40 秒，20 只就是十几分钟。
    """
    import analysis.kronos_pred as kp
    from strategy.signals import build_signals

    called = []
    saved = (kp.is_available, kp.generate_kronos_signal)
    kp.is_available = lambda *a, **k: called.append("is_available") or True
    kp.generate_kronos_signal = lambda *a, **k: called.append("generate")

    restore, _ = _patch(cons=_cons_payload(2))
    sp.build_signals = build_signals      # 换回真的
    try:
        res = sp.pick("881124", "industry", top_n=2)
    finally:
        restore()
        kp.is_available, kp.generate_kronos_signal = saved

    assert called == [], f"选股路径不该碰 Kronos，实际调用了 {called}"
    assert "kronos" not in sp.PICK_SIGNALS
    assert res["computed"] == 2
    for c in res["candidates"]:
        assert c["technical"] is not None and c["chanlun"] is not None


def test_enabled_signals_passed_to_build_signals():
    restore, spy = _patch(cons=_cons_payload(2))
    try:
        sp.pick("881124", "industry", top_n=2)
    finally:
        restore()
    for call in spy.calls:
        assert call["enabled_signals"] == list(sp.PICK_SIGNALS)
        assert "kronos" not in call["enabled_signals"]


# ========== 降级路径 ==========

def test_unavailable_sector_returns_reason_not_exception():
    """成分股取不到 / 未匹配到东财板块 → available=False + 一句能照着做的原因"""
    restore, _ = _patch(cons=None)
    try:
        res = sp.pick("881124", "industry")
    finally:
        restore()
    assert res["available"] is False
    assert res["candidates"] == [] and res["total_constituents"] == 0
    assert res["warnings"] == ["未匹配到东财板块（测试）"]


def test_sector_outside_pool_says_so_instead_of_blaming_alias():
    """池外板块没有名称/类型配置 → 单独一条提示，不能说成「东财名录里没有 881160」

    （那样会让人跑去补 alias，而真正该做的是把板块加进股票池——同 fetch_daily_kline
    的板块分支）
    """
    restore, _ = _patch(cons=_cons_payload(3))
    sp.get_sector_info = lambda code: None
    try:
        res = sp.pick("881160")
    finally:
        restore()
    assert res["available"] is False
    assert "股票池" in res["warnings"][0] and "881160" in res["warnings"][0]
    # 显式给名字/类型时照常能算（页面就是这么调的）
    restore, _ = _patch(cons=_cons_payload(3))
    sp.get_sector_info = lambda code: None
    try:
        res2 = sp.pick("881160", "industry", top_n=3, sector_name="光伏设备")
    finally:
        restore()
    assert res2["available"] is True and res2["sector"] == "光伏设备"


def test_stale_constituents_are_flagged_to_caller():
    restore, _ = _patch(cons=_cons_payload(3, stale=True))
    try:
        res = sp.pick("881124", "industry", top_n=3)
    finally:
        restore()
    assert res["stale"] is True and res["fetched_at"] == "2026-08-04 03:20"
    assert any("过期缓存" in w for w in res["warnings"])


def test_progress_cb_reports_every_candidate():
    seen = []
    restore, _ = _patch(cons=_cons_payload(84))
    try:
        sp.pick("881124", "industry", top_n=7,
                progress_cb=lambda i, total, msg: seen.append((i, total, msg)))
    finally:
        restore()
    assert [s[0] for s in seen] == list(range(1, 8))
    assert all(s[1] == 7 for s in seen)


def test_not_wired_into_signal_system():
    """选股是确定性参考层：不产生 signal、不进 fusion、不影响回测与仓位"""
    from strategy.fusion import SOURCE_COLUMNS
    from strategy.signals import SIGNAL_SOURCES

    for name in list(SIGNAL_SOURCES) + list(SOURCE_COLUMNS):
        assert "pick" not in str(name) and "sector_cons" not in str(name), name
    root = os.path.join(os.path.dirname(__file__), "..")
    for rel in ("strategy/signals.py", "strategy/fusion.py", "backtest/engine.py"):
        src = open(os.path.join(root, rel), encoding="utf-8").read()
        assert "sector_pick" not in src and "sector_cons" not in src, rel


# ========== 页面级：选股入口挂在热度分排名下方 ==========

def test_pick_panel_is_wired_into_hotness_tab():
    """源码自检：入口挂在热度分 tab 里、没有新增页面、夜间 job 已挂上（#35 第六、七节）"""
    root = os.path.join(os.path.dirname(__file__), "..")
    src = open(os.path.join(root, "app.py"), encoding="utf-8").read()
    assert "_render_sector_pick_panel(hot)" in src, "选股入口应挂在热度分排名下方"
    assert "def _render_sector_pick_panel" in src and "def _render_sector_pick_result" in src
    # #35 不新增页面：选股挂在「板块分析」页里，不许出现「板块选股」这类新页
    pages_line = next(ln for ln in src.splitlines() if ln.startswith("pages = ["))
    assert '"板块分析"' in pages_line, pages_line
    assert "选股" not in pages_line, f"#35 不该新增页面：{pages_line}"
    assert "refresh_pool_constituents" in src, "成分股夜间刷新 job 应挂在 APScheduler 上"


def test_page_renders_with_pick_panel():
    """headless 渲染「板块分析 → 轮动」：新面板不该抛异常，N 的默认值来自 settings"""
    from streamlit.testing.v1 import AppTest

    app = os.path.join(os.path.dirname(__file__), "..", "app.py")
    at = AppTest.from_file(app, default_timeout=900)
    at.session_state["page"] = "板块分析"
    at.run()
    assert not at.exception, at.exception
    view = next((r for r in at.radio if r.label == "视图"), None)
    if view is None:
        print("[SKIP] 股票池里没有板块，跳过页面渲染断言")
        return
    at2 = view.set_value("轮动").run()
    assert not at2.exception, at2.exception
    nums = [n for n in at2.number_input if n.label == "细算只数 N"]
    if not nums:
        # 热度分算不出来（板块日K/资金流都取不到）时面板不渲染，这条只做「不抛异常」的断言
        print("[SKIP] 热度分为空（取数失败），选股面板未渲染，仅确认无异常")
        return
    assert int(nums[0].value) == cfg.SECTOR_PICK_TOP_N

    # 点一次「选股」，把 pick 换成一份**故意难看**的结果：现价/涨跌幅/超额/flags 各种
    # None + 过期缓存 + warnings。候选表用 pandas Styler 染色格式化，None 混 float 正是
    # 这种页面最容易当场抛异常的地方，所以必须真渲染一遍
    canned = {
        "available": True, "sector": "测试板块", "sector_code": "881124",
        "kind": "industry", "em_code": "BK1036", "em_name": "消费电子",
        "match_level": "exact", "total_constituents": 84, "computed": 2, "top_n": 20,
        "stale": True, "fetched_at": "2026-07-20 03:20", "sector_return": None,
        "weights": {"technical": 1.0, "excess": 1.0, "fundamental": 1.0},
        "candidates": [
            {"code": "600000", "name": "有全套数据", "price": 12.3, "change_pct": 5.0,
             "technical": 0.2, "chanlun": 0.0, "stock_return": 3.3, "excess": None,
             "fund_flags": ["ROE 处于上升通道"], "fund_score": 0.5,
             "z_technical": 0.7, "z_excess": None, "score": 1.2, "note": sp.NOTE_NO_SECTOR_RET},
            {"code": "600001", "name": "什么都缺", "price": None, "change_pct": None,
             "technical": 0.0, "chanlun": 0.0, "stock_return": None, "excess": None,
             "fund_flags": None, "fund_score": 0.0,
             "z_technical": None, "z_excess": None, "score": 0.0,
             "note": sp.NOTE_NO_FUND},
        ],
        "warnings": ["某只股票日K取数失败，已跳过"],
    }
    saved_pick = sp.pick
    sp.pick = lambda *a, **k: canned
    try:
        btn = next(b for b in at2.button if str(b.proto.id).find("pick_btn_") >= 0
                   or b.label == "选股")
        at3 = btn.click().run()
    finally:
        sp.pick = saved_pick
    assert not at3.exception, at3.exception
    texts = " ".join(str(m.value) for m in at3.markdown) + " ".join(
        str(c.value) for c in at3.caption)
    assert "84" in texts and "已从" in texts, "必须写出「已从 84 只中筛出…」，不许静默截断"
    assert any("过期缓存" in str(w.value) for w in at3.warning), "过期缓存要显式告知"
    print(f"[OK] 轮动视图渲染出选股面板（默认 N={nums[0].value}），"
          f"候选表在 None 混 float 的降级数据上渲染无异常")


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"[OK] {name}")
    print(f"\n全部 {len(tests)} 项测试通过!")
