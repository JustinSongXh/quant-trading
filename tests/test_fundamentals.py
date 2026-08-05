"""issue #33：基本面数据层 + 确定性判断 的测试（mock akshare，不打网络）

覆盖 issue 列的 10 项：字符串解析（万/亿/万亿、百分号、布尔 False）、8 字段白名单、
非 A 股不打接口、24h 文件缓存、取数失败不抛、summarize 的三种趋势 / 缺失季不重置 /
口径校验。
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta

import pandas as pd

import data.fundamentals as fd
from analysis.fundamental import CHOPPY, IMPROVING, WORSENING, summarize

# 接口真实返回的 20 列（实测 600000，值取自 2026-03-31 那行）
ALL_COLUMNS = [
    "报告期", "净利润", "净利润同比增长率", "扣非净利润", "扣非净利润同比增长率",
    "营业总收入", "营业总收入同比增长率", "基本每股收益", "每股净资产",
    "每股资本公积金", "每股未分配利润", "每股经营现金流", "销售净利率",
    "净资产收益率", "净资产收益率-摊薄", "流动比率", "速动比率", "保守速动比率",
    "产权比率", "资产负债率",
]


def _row(period, rev="465.73亿", rev_yoy="1.42%", profit="178.61亿",
         profit_yoy="1.49%", margin="39.01%", roe="2.32%", lev="91.84%"):
    """造一行接口原样返回的字符串数据（白名单外的列一律填上，验证会被裁掉）"""
    return {
        "报告期": period, "净利润": profit, "净利润同比增长率": profit_yoy,
        "扣非净利润": "179.03亿", "扣非净利润同比增长率": "1.83%",
        "营业总收入": rev, "营业总收入同比增长率": rev_yoy,
        "基本每股收益": "0.5200", "每股净资产": "22.63", "每股资本公积金": "4.04",
        "每股未分配利润": "8.05", "每股经营现金流": "2.75", "销售净利率": margin,
        "净资产收益率": roe, "净资产收益率-摊薄": "2.29%",
        # 实测银行股这三列是布尔 False，不是字符串
        "流动比率": False, "速动比率": False, "保守速动比率": False,
        "产权比率": "11.35", "资产负债率": lev,
    }


def _df(rows):
    return pd.DataFrame(rows, columns=ALL_COLUMNS)


def _install_fake_ak(quarter_rows, annual_rows, calls=None, exc=None, empty=False):
    """替掉 data.fundamentals 里 import 的 akshare（模块内 lazy import → 塞 sys.modules）"""
    class _FakeAk:
        @staticmethod
        def stock_financial_abstract_ths(symbol, indicator):
            if calls is not None:
                calls.append((symbol, indicator))
            if exc:
                raise exc
            if empty:
                return pd.DataFrame()
            return _df(quarter_rows if indicator == fd.QUARTER_INDICATOR else annual_rows)

    sys.modules["akshare"] = _FakeAk()
    return calls


def _clean_cache(symbol="600000"):
    path = os.path.join(fd._CACHE_DIR, f"{symbol}.json")
    if os.path.exists(path):
        os.remove(path)
    return path


# ---------- 1~3. 字符串解析 ----------
def test_parse_money_units():
    assert fd.parse_value("1739.64亿") == 173964000000.0
    assert fd.parse_value("42.16万") == 421600.0
    assert fd.parse_value("1.2万亿") == 1.2e12  # 万亿 不能被 万 抢先匹配
    assert fd.parse_value("-8.05亿") == -805000000.0
    assert fd.parse_value("11.35") == 11.35
    print("[OK] 万 / 亿 / 万亿 解析正确")


def test_parse_percent():
    assert fd.parse_value("10.52%") == 10.52  # 单位是 %，不除 100
    assert fd.parse_value("-28.28%") == -28.28
    print("[OK] 百分号解析为 float（单位 %）")


def test_parse_nulls():
    for raw in (False, True, "", "--", "-", None, "abc"):
        assert fd.parse_value(raw) is None, raw
    print("[OK] False / '' / '--' / None → None（银行股流动比率实测是布尔 False）")


# ---------- 4. 字段白名单 ----------
def test_only_whitelist_fields():
    _install_fake_ak([_row("2026-03-31")], [_row("2025")])
    _clean_cache()
    fund = fd.get_fundamentals("600000", "stock")
    row = fund["quarterly"]["rows"][0]
    assert set(row) == set(fd.FIELDS), set(row) ^ set(fd.FIELDS)
    assert len(fd.FIELDS) == 8
    for dropped in ("扣非净利润", "每股资本公积金", "保守速动比率", "净资产收益率-摊薄"):
        assert dropped not in row
    assert row["营业总收入"] == 46573000000.0 and row["资产负债率"] == 91.84
    print("[OK] 只保留 8 个白名单字段，其余 12 列不出现")


# ---------- 5. 非 A 股不打接口 ----------
def test_non_a_share_returns_none_without_calling_akshare():
    calls = _install_fake_ak([_row("2026-03-31")], [_row("2025")], calls=[])
    for symbol, stype in (("00700", "stock"), ("00700", None),   # 港股
                          ("000001", "index"), ("881124", "sector"),
                          ("BK0001", "stock")):                   # 非 6 位数字
        assert fd.get_fundamentals(symbol, stype) is None, (symbol, stype)
    assert calls == [], f"非 A 股不该打接口，实际调用 {calls}"
    print("[OK] 港股/指数/板块 → None，且未调用 akshare")


# ---------- 6. 24h 文件缓存 ----------
def test_file_cache_24h():
    calls = _install_fake_ak([_row("2026-03-31")], [_row("2025")], calls=[])
    path = _clean_cache()

    assert fd.get_fundamentals("600000", "stock") is not None
    assert len(calls) == 2, calls  # 单季度 + 年度
    assert os.path.exists(path)

    fd.get_fundamentals("600000", "stock")
    assert len(calls) == 2, f"命中缓存不该再打接口，实际 {calls}"

    # 把 mtime 推到 25 小时前 → 缓存过期，重新打接口
    stale = (datetime.now() - timedelta(hours=25)).timestamp()
    os.utime(path, (stale, stale))
    fd.get_fundamentals("600000", "stock")
    assert len(calls) == 4, f"缓存过期该重新打接口，实际 {calls}"
    print("[OK] 24h 文件缓存：命中不打接口，过期后重打")


# ---------- 7. 取数失败不抛给调用方 ----------
def test_fetch_failure_returns_none():
    _clean_cache()
    _install_fake_ak([], [], exc=RuntimeError("10jqka 限流"))
    assert fd.get_fundamentals("600000", "stock") is None

    _clean_cache()
    _install_fake_ak([], [], empty=True)
    assert fd.get_fundamentals("600000", "stock") is None
    print("[OK] 接口抛异常 / 返回空表 → None，不抛给页面")


def test_stale_cache_beats_nothing_on_failure():
    """有旧缓存时取数失败回落旧缓存（财报按季更新，过期一两天无妨）"""
    _install_fake_ak([_row("2026-03-31")], [_row("2025")])
    path = _clean_cache()
    fd.get_fundamentals("600000", "stock")
    stale = (datetime.now() - timedelta(hours=25)).timestamp()
    os.utime(path, (stale, stale))

    _install_fake_ak([], [], exc=RuntimeError("boom"))
    fund = fd.get_fundamentals("600000", "stock")
    assert fund is not None and fund["quarterly"]["rows"][0]["报告期"] == "2026-03-31"
    print("[OK] 取数失败回落过期缓存，不覆盖为空")


# ---------- 8~10. summarize ----------
def _fund(quarter_yoy, annual_lev=("90.0%", "90.5%", "91.0%", "91.5%", "91.8%"),
          margin=None, roe=None, profit_yoy=None):
    """按给定的营收同比序列造一份已解析的 fund dict（时间升序）"""
    n = len(quarter_yoy)
    periods = [f"20{20 + i // 4}-{[3, 6, 9, 12][i % 4]:02d}-30" for i in range(n)]
    q_rows = []
    for i, yoy in enumerate(quarter_yoy):
        q_rows.append({
            "报告期": periods[i],
            "营业总收入": 46573000000.0,
            "营业总收入同比增长率": yoy,
            "净利润": 17861000000.0,
            "净利润同比增长率": (profit_yoy[i] if profit_yoy else yoy),
            "销售净利率": (margin[i] if margin else 39.01),
            "净资产收益率": (roe[i] if roe else 2.32),
            "资产负债率": 91.84,
        })
    a_rows = [{"报告期": str(2021 + i), "营业总收入": 1.7e11,
               "营业总收入同比增长率": 1.88, "净利润": 5.0e10,
               "净利润同比增长率": 10.52, "销售净利率": 28.97,
               "净资产收益率": 6.76, "资产负债率": fd.parse_value(v)}
              for i, v in enumerate(annual_lev)]
    return {
        "symbol": "600000",
        "quarterly": {"indicator": fd.QUARTER_INDICATOR, "rows": q_rows},
        "annual": {"indicator": fd.ANNUAL_INDICATOR, "rows": a_rows},
    }


def test_summarize_three_trends():
    up = summarize(_fund([-5.0, -2.0, 1.0, 4.0]))["revenue_trend"]
    assert up["direction"] == IMPROVING and up["quarters"] == 4, up

    down = summarize(_fund([8.0, 5.0, 2.0, -1.0]))["revenue_trend"]
    assert down["direction"] == WORSENING and down["quarters"] == 4, down
    assert down["decline_quarters"] == 1  # 只有最新一季转负

    choppy = summarize(_fund([2.0, -1.0, 3.0, -2.0]))["revenue_trend"]
    assert choppy["direction"] == CHOPPY, choppy
    print("[OK] 连续改善 / 连续恶化 / 震荡 三种序列各判对")


def test_summarize_decline_flag():
    s = summarize(_fund([1.0, -0.5, -2.0, -3.5]))
    assert "连续3季营收同比下滑" in s["flags"], s["flags"]
    assert "营收同比连续4季恶化" in s["flags"], s["flags"]
    print("[OK] 连续 N 季同比下滑 / 恶化 标签")


def test_missing_quarter_does_not_reset_run():
    """中间季缺失（同比列为 None）时跳过该季，连续计数不重置"""
    fund = _fund([-5.0, -2.0, 1.0, 4.0])
    fund["quarterly"]["rows"][2]["营业总收入同比增长率"] = None
    tr = summarize(fund)["revenue_trend"]
    assert tr["direction"] == IMPROVING, tr
    assert tr["quarters"] == 3, tr  # -5 → -2 → 4，缺的那季跳过而非断开
    assert len(tr["series"]) == 3
    print("[OK] 缺失季跳过但不重置连续计数")


def test_summarize_rejects_cumulative_indicator():
    """喂 按报告期（累计值）进来必须拒绝：浦发 2026Q1 营收 465.73亿 vs 2025FY 1739.64亿"""
    fund = _fund([1.0, 2.0, 3.0, 4.0])
    fund["quarterly"]["indicator"] = "按报告期"
    try:
        summarize(fund)
        raise AssertionError("累计口径应当被拒绝")
    except ValueError as e:
        assert "按单季度" in str(e), e

    fund = _fund([1.0, 2.0, 3.0, 4.0])
    fund["annual"]["indicator"] = "按报告期"
    try:
        summarize(fund)
        raise AssertionError("年度口径错也应当被拒绝")
    except ValueError as e:
        assert "按年度" in str(e), e

    for bad in (None, {}):
        try:
            summarize(bad)
            raise AssertionError("空值应当被拒绝")
        except ValueError:
            pass
    print("[OK] 单季度/年度口径不混用：累计口径直接 ValueError")


def test_summarize_levels_use_year_over_year_windows():
    """ROE / 净利率是水平值有季节性，用近4季均值 vs 前4季均值，不逐季比"""
    # 前 4 季 ROE 均值 1.0，近 4 季均值 3.0 → 上升通道
    s = summarize(_fund([1.0] * 8, roe=[1.0, 1.0, 1.0, 1.0, 3.0, 3.0, 3.0, 3.0],
                        margin=[20.0] * 4 + [30.0] * 4))
    assert s["roe_level"]["direction"] == IMPROVING
    assert s["roe_level"]["avg8"] == 2.0 and s["roe_level"]["latest"] == 3.0
    assert s["margin_trend"]["direction"] == IMPROVING
    assert "ROE 处于上升通道" in s["flags"] and "销售净利率走强" in s["flags"]

    # 单季波动（Q4 低 Q1 高）但两个年度窗口均值一致 → 不该报趋势
    seasonal = [1.0, 2.0, 3.0, 0.5] * 2
    s2 = summarize(_fund([1.0] * 8, roe=seasonal, margin=seasonal))
    assert s2["roe_level"]["direction"] == CHOPPY, s2["roe_level"]
    assert s2["margin_trend"]["direction"] == CHOPPY
    print("[OK] ROE / 净利率用 4 季均值对比，季节性波动不误报趋势")


def test_leverage_position_and_flags():
    s = summarize(_fund([1.0] * 4, annual_lev=("90.0%", "90.5%", "91.0%", "91.5%", "91.8%")))
    lev = s["leverage"]
    assert lev["latest"] == 91.84 and lev["years"] == 5
    assert lev["annual_max"] == 91.8 and lev["percentile"] == 100.0
    assert "资产负债率创5年新高" in s["flags"], s["flags"]

    s2 = summarize(_fund([1.0] * 4, annual_lev=("95.0%", "94.0%", "93.0%", "92.5%", "92.0%")))
    assert s2["leverage"]["percentile"] == 0.0
    assert "资产负债率创5年新低" in s2["flags"], s2["flags"]
    print("[OK] 资产负债率的 5 年分位与新高/新低标签")


def test_summarize_survives_empty_rows():
    """全空行不该炸（取到表但白名单列全空的极端情形）"""
    fund = _fund([])
    fund["annual"]["rows"] = []
    s = summarize(fund)
    assert s["flags"] == [] and s["period"] is None
    assert s["revenue_trend"]["direction"] == CHOPPY
    assert s["leverage"]["latest"] is None and s["leverage"]["percentile"] is None
    print("[OK] 空数据不抛异常")


# ---------- 不接入信号系统 ----------
def test_not_wired_into_signal_system():
    """基本面是确定性参考层：不产生 signal、不进 fusion、不影响回测与仓位"""
    from strategy.decision import SOURCE_COLUMNS
    from strategy.signals import SIGNAL_SOURCES

    for name in list(SIGNAL_SOURCES) + list(SOURCE_COLUMNS):
        assert "fundamental" not in str(name), name
    for cols in SOURCE_COLUMNS.values():
        assert not any("fundamental" in str(c) for c in cols), cols

    src = open(os.path.join(os.path.dirname(__file__), "..", "strategy", "signals.py"),
               encoding="utf-8").read()
    assert "fundamental" not in src, "signals.py 不该引用基本面"
    print("[OK] 未接入信号系统（SIGNAL_SOURCES / SOURCE_COLUMNS 不含 fundamental）")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for fn in tests:
            fn()
        print(f"\n全部 {len(tests)} 项测试通过!")
    finally:
        _clean_cache()
