"""issue #34：板块轮动确定性计算的测试（mock 取数，不打网络）

覆盖 issue 列的 6 项：超额收益口径与沪深300 缺失时的降级、四种轮动标签、阈值边界
「达到即触发」、热度分的池内 z-score 与 join 不上给 None（不是 0）、权重从
SECTOR_HOTNESS_WEIGHTS 读、板块日K不足不崩。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd

import analysis.rotation as rot
import config.settings as cfg
from analysis.rotation import (
    LABEL_EMERGING, LABEL_FADING, LABEL_LAGGARD, LABEL_STRONG, col_excess,
    col_rank, col_ret, hotness, momentum_table, rotation_signals,
)

BARS = 200


def _kline(daily_pct, bars=BARS, start=100.0):
    """恒定日收益率的收盘价序列（pct_change 后每天都是 daily_pct）"""
    idx = pd.date_range("2026-01-01", periods=bars + 1, freq="D")
    closes = [start * (1 + daily_pct) ** i for i in range(bars + 1)]
    return pd.DataFrame({"close": closes}, index=idx)


def _cum(daily_pct, w):
    return ((1 + daily_pct) ** w - 1) * 100


def _patch(klines: dict, index_daily=None, nets=None, kinds=None):
    """替掉 rotation 模块里的取数依赖（都是模块级 import，直接赋属性即可）"""
    rot._get_sector_kline = lambda code, days, force_refresh=False: klines.get(code)
    rot._get_index_returns = lambda window, force_refresh=False: (
        None if index_daily is None
        else _kline(index_daily)["close"].pct_change().dropna()
    )
    rot.net_by_sector = lambda kind, period=rot.HOTNESS_FLOW_PERIOD: dict(
        (nets or {}).get(kind, {})
    )
    rot.get_sector_info = lambda code: (kinds or {}).get(code)
    rot.get_stock_name = lambda code, stock_type=None: (
        (kinds or {}).get(code, (code,))[0] if code in (kinds or {}) else code
    )


# ---------- 1. 超额收益口径 + 沪深300 缺失降级 ----------
def test_excess_is_sector_minus_hs300():
    _patch({"881121": _kline(0.01), "881124": _kline(-0.002)}, index_daily=0.005,
           kinds={"881121": ("半导体", "industry"), "881124": ("消费电子", "industry")})
    mom = momentum_table(["881121", "881124"], windows=(5, 20))
    assert mom.attrs["index_available"] is True
    assert list(mom.index) == ["半导体", "消费电子"]

    for w in (5, 20):
        # 值 round 到 3 位小数（见 _cum_return），比对留 1e-3 的容差
        assert abs(mom.loc["半导体", col_ret(w)] - _cum(0.01, w)) < 1e-3
        expect = _cum(0.01, w) - _cum(0.005, w)
        assert abs(mom.loc["半导体", col_excess(w)] - expect) < 1e-2, (w, mom)
        # 板块跑输大盘 → 超额为负
        assert mom.loc["消费电子", col_excess(w)] < 0
    # 排名按超额降序，1 = 最强
    assert mom.loc["半导体", col_rank(5)] == 1 and mom.loc["消费电子", col_rank(5)] == 2
    print("[OK] 超额收益 = 板块累计收益 − 沪深300 同窗口累计收益，排名 1 = 最强")


def test_falls_back_to_absolute_return_without_index():
    _patch({"881121": _kline(0.01)}, index_daily=None,
           kinds={"881121": ("半导体", "industry")})
    mom = momentum_table(["881121"], windows=(20,))
    assert mom.attrs["index_available"] is False, "取不到指数必须标注，否则「超额」列被误读"
    assert mom.loc["半导体", col_excess(20)] == mom.loc["半导体", col_ret(20)]
    print("[OK] 沪深300 取不到 → 超额退回绝对收益并置 index_available=False")


# ---------- 6. 板块日K不足不崩 ----------
def test_short_history_yields_none_not_crash():
    _patch({"881121": _kline(0.01), "300008": _kline(0.02, bars=10),
            "881279": None},                      # 取数彻底失败
           index_daily=0.005,
           kinds={"881121": ("半导体", "industry"), "300008": ("新能源汽车", "concept"),
                  "881279": ("光伏设备", "industry")})
    mom = momentum_table(["881121", "300008", "881279"], windows=(5, 20, 60))
    # 10 根 bar → 9 个收益：5 日算得出，20/60 日不够
    assert mom.loc["新能源汽车", col_ret(5)] is not None
    assert pd.isna(mom.loc["新能源汽车", col_ret(20)])
    assert pd.isna(mom.loc["新能源汽车", col_rank(60)])
    # 取数失败的板块保留行但全空（页面要能看出它被跳过，而不是消失）
    assert "光伏设备" in mom.index
    assert pd.isna(mom.loc["光伏设备", col_excess(5)])
    # 有效值仍照常排名
    assert mom.loc["半导体", col_rank(20)] == 1
    print("[OK] 日K不足/取数失败的板块指标为 None、不崩、不参与排名")


# ---------- 2. 四种标签 ----------
def _mom(rows, short=5, long=20):
    """直接造 momentum_table 形状的表（标签判定只读这四列）"""
    df = pd.DataFrame(rows).set_index("板块")
    return df.rename(columns={
        "es": col_excess(short), "el": col_excess(long),
        "rs": col_rank(short), "rl": col_rank(long),
    })


# n=9 → 池内 1/3 = 3.0 个名次，阈值刚好是整数，边界好验
NINE = [
    {"板块": "A", "es": 2.0, "el": 3.0, "rs": 1, "rl": 2},    # 持续走强
    {"板块": "B", "es": 4.0, "el": -1.0, "rs": 2, "rl": 6},   # 新晋走强（升 4 名）
    {"板块": "C", "es": -2.0, "el": 5.0, "rs": 8, "rl": 1},   # 转弱（降 7 名）
    {"板块": "D", "es": -3.0, "el": -4.0, "rs": 9, "rl": 9},  # 持续弱势
    {"板块": "E", "es": 1.0, "el": 2.0, "rs": 4, "rl": 5},    # 无标签（排名不在前 1/3）
    {"板块": "F", "es": 0.5, "el": 1.0, "rs": 5, "rl": 4},    # 无标签
    {"板块": "G", "es": -0.5, "el": 0.5, "rs": 6, "rl": 7},   # 转弱（长窗正短窗转负）
    {"板块": "H", "es": -1.0, "el": -2.0, "rs": 7, "rl": 8},  # 持续弱势
    {"板块": "I", "es": 3.0, "el": 1.0, "rs": 3, "rl": 3},    # 持续走强（排名=3 恰好前 1/3）
]


def test_four_labels():
    sigs = rotation_signals(_mom(NINE))
    got = {s["sector"]: s["label"] for s in sigs}
    assert got == {
        "A": LABEL_STRONG, "I": LABEL_STRONG,
        "B": LABEL_EMERGING,
        "C": LABEL_FADING, "G": LABEL_FADING,
        "D": LABEL_LAGGARD, "H": LABEL_LAGGARD,
    }, got
    assert "E" not in got and "F" not in got, "不满足任何判定的板块不该被硬贴标签"

    # 一个板块只出一个标签，且按 持续走强 > 新晋走强 > 转弱 > 持续弱势 排序
    assert len(sigs) == len(got)
    assert [s["label"] for s in sigs] == [
        LABEL_STRONG, LABEL_STRONG, LABEL_EMERGING,
        LABEL_FADING, LABEL_FADING, LABEL_LAGGARD, LABEL_LAGGARD,
    ], [s["label"] for s in sigs]
    b = next(s for s in sigs if s["sector"] == "B")
    assert b["rank_change"] == 4 and "升 4 名" in b["detail"]
    print("[OK] 持续走强 / 新晋走强 / 转弱 / 持续弱势 四种标签各判对，且互斥")


# ---------- 3. 边界：达到即触发 ----------
def test_threshold_boundary_triggers():
    rows = list(NINE)
    # 名次上升恰好 3（= 池内 1/3）→ 新晋走强 触发
    rows[4] = {"板块": "E", "es": 1.0, "el": -1.0, "rs": 4, "rl": 7}
    # 名次下降恰好 3 → 转弱 触发（长窗排名 4 不在前 1/3，不会先被判成持续走强）
    rows[5] = {"板块": "F", "es": 0.5, "el": 1.0, "rs": 7, "rl": 4}
    got = {s["sector"]: s["label"] for s in rotation_signals(_mom(rows))}
    assert got["E"] == LABEL_EMERGING, got
    assert got["F"] == LABEL_FADING, got
    assert got["I"] == LABEL_STRONG, "长窗排名 3 == 池内 1/3 也算前 1/3"

    # 差一名（上升 2 / 下降 2）就不触发
    rows[4] = {"板块": "E", "es": 1.0, "el": -1.0, "rs": 4, "rl": 6}
    rows[5] = {"板块": "F", "es": 0.5, "el": 1.0, "rs": 6, "rl": 4}
    got2 = {s["sector"]: s["label"] for s in rotation_signals(_mom(rows))}
    assert "E" not in got2 and "F" not in got2, got2
    print("[OK] 排名迁移刚好等于阈值时「达到即触发」，差一名不触发")


def test_signals_skip_missing_and_bad_input():
    rows = list(NINE)
    rows.append({"板块": "X", "es": None, "el": None, "rs": None, "rl": None})
    sigs = rotation_signals(_mom(rows))
    assert all(s["sector"] != "X" for s in sigs), "指标缺失的板块不该猜标签"
    assert rotation_signals(pd.DataFrame()) == []
    assert rotation_signals(_mom(NINE), short=99, long=100) == []  # 列不存在
    print("[OK] 指标缺失/列缺失时不猜标签，返回空而不抛")


# ---------- 4. 热度分：池内 z-score + join 不上给 None ----------
HOT_KINDS = {
    "881121": ("半导体", "industry"),
    "881124": ("消费电子", "industry"),
    "300008": ("新能源汽车", "concept"),
}


def _hot_setup(nets=None):
    _patch({"881121": _kline(0.01), "881124": _kline(0.002), "300008": _kline(-0.003)},
           index_daily=0.001,
           nets=nets if nets is not None else {
               "industry": {"半导体": 40.84, "消费电子": -5.0},
               "concept": {"新能源汽车": 12.0},
           },
           kinds=HOT_KINDS)


def _z(values):
    s = pd.Series(values, dtype="float64")
    return [(v - s.mean()) / s.std() for v in values]


def test_hotness_zscore_within_pool():
    _hot_setup()
    hot = hotness(list(HOT_KINDS))
    # 按热度分降序：半导体（涨幅+资金双高）→ 新能源汽车（资金 12 亿托住）→ 消费电子（资金净流出）
    assert list(hot.index) == ["半导体", "新能源汽车", "消费电子"], hot
    assert hot["热度分"].is_monotonic_decreasing

    ex_col = col_excess(rot.HOTNESS_WINDOW)
    excess = [hot.loc[n, ex_col] for n in ["半导体", "消费电子", "新能源汽车"]]
    nets = [40.84, -5.0, 12.0]
    for name, ze, zn in zip(["半导体", "消费电子", "新能源汽车"],
                            _z(excess), _z(nets)):
        assert abs(hot.loc[name, "z_超额"] - round(ze, 3)) < 1e-3, name
        assert abs(hot.loc[name, "z_资金"] - round(zn, 3)) < 1e-3, name
        assert abs(hot.loc[name, "热度分"] - round(ze + zn, 3)) < 1e-2, name
    assert (hot["备注"] == "").all()
    print("[OK] 热度分 = z(20日超额) + z(20日资金净额)，z 在池内计算")


def test_hotness_join_miss_is_none_not_zero():
    # 资金流排行里没有「新能源汽车」
    _hot_setup(nets={"industry": {"半导体": 40.84, "消费电子": -5.0}, "concept": {}})
    hot = hotness(list(HOT_KINDS))
    assert pd.isna(hot.loc["新能源汽车", "热度分"]), hot
    assert pd.isna(hot.loc["新能源汽车", "z_资金"]), "join 不上不能当 0（会被排到中间）"
    assert "资金流" in hot.loc["新能源汽车", "备注"]
    assert list(hot.index)[-1] == "新能源汽车", "热度分为空的排最后"
    # 其余板块照常算，且 z 只在有值的板块间算
    assert not pd.isna(hot.loc["半导体", "热度分"])
    assert abs(hot.loc["半导体", "z_资金"] - 0.707) < 0.01     # 两个值的 z 是 ±0.707
    print("[OK] 资金流 join 不上 → 热度分 None + 备注，不静默当 0")


# ---------- 5. 权重从 SECTOR_HOTNESS_WEIGHTS 读 ----------
def test_hotness_weights_from_settings():
    assert cfg.SECTOR_HOTNESS_WEIGHTS == {"excess_20d": 1.0, "net_20d": 1.0}
    original = cfg.SECTOR_HOTNESS_WEIGHTS
    try:
        _hot_setup()
        cfg.SECTOR_HOTNESS_WEIGHTS = {"excess_20d": 1.0, "net_20d": 0.0}
        only_excess = hotness(list(HOT_KINDS))
        for name in HOT_KINDS.values():
            n = name[0]
            assert abs(only_excess.loc[n, "热度分"] - only_excess.loc[n, "z_超额"]) < 1e-9, n

        cfg.SECTOR_HOTNESS_WEIGHTS = {"excess_20d": 0.0, "net_20d": 3.0}
        only_net = hotness(list(HOT_KINDS))
        for name in HOT_KINDS.values():
            n = name[0]
            assert abs(only_net.loc[n, "热度分"] - 3 * only_net.loc[n, "z_资金"]) < 1e-2, n
        assert only_net.attrs["weights"] == {"excess_20d": 0.0, "net_20d": 3.0}
        # 改权重会改排序：只看资金时 新能源汽车(12亿) 排在 消费电子(-5亿) 前面
        assert list(only_net.index).index("新能源汽车") < list(only_net.index).index("消费电子")
    finally:
        cfg.SECTOR_HOTNESS_WEIGHTS = original
    print("[OK] 热度分权重从 config/settings.py::SECTOR_HOTNESS_WEIGHTS 运行时读取")


def test_hotness_empty_pool():
    _hot_setup()
    assert hotness([]).empty
    assert momentum_table([]).empty and momentum_table(["881121"], windows=()).empty
    print("[OK] 空板块列表返回空表，不抛")


# ---------- 不接入信号系统 ----------
def test_not_wired_into_signal_system():
    """轮动是确定性参考层：不产生 signal、不进 fusion、不影响回测与仓位"""
    from strategy.fusion import SOURCE_COLUMNS
    from strategy.signals import SIGNAL_SOURCES

    for name in list(SIGNAL_SOURCES) + list(SOURCE_COLUMNS):
        assert "rotation" not in str(name) and "fund_flow" not in str(name), name
    src = open(os.path.join(os.path.dirname(__file__), "..", "strategy", "signals.py"),
               encoding="utf-8").read()
    assert "rotation" not in src and "fund_flow" not in src, "signals.py 不该引用轮动/资金流"
    print("[OK] 未接入信号系统（SIGNAL_SOURCES / SOURCE_COLUMNS 不含 rotation / fund_flow）")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
