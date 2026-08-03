"""基本面确定性判断（issue #33）

纯函数，不碰网络，输入是 `data/fundamentals.py::get_fundamentals()` 的返回值。
算出趋势 / 水平 / 杠杆位置，以及一组确定性标签 `flags`——标签是给后续对话层用的：
模型不需要自己从二十几个数字里找规律，看标签就行。

两类指标用两种口径，原因是季节性：

- **营收 / 净利** 看 `同比增长率`。同比本身已消掉季节性，可以逐季直接比大小。
- **ROE / 销售净利率** 是水平值，单季有强季节性（浦发 2026Q1 ROE 2.32% vs
  2025Q4 1.09%，不是「翻倍改善」而是 Q1 与 Q4 不可比）。所以不逐季比，改用
  「近 4 季均值 vs 前 4 季均值」——两边各跨满一年，季节项相互抵消。

「连续 N 季」的定义（写死在代码里）：取单季同比增长率序列，**缺失季跳过但不重置
计数**——财报缺项很常见，一重置标签几乎永不触发。N 是这段连续区间里的季数，
所以「连续 3 季改善」= 最近 3 个有值的季度里同比逐季走高（2 次抬升）。
"""

from data.fundamentals import (
    ANNUAL_INDICATOR, LEVERAGE, NET_MARGIN, PERIOD, PROFIT, PROFIT_YOY,
    QUARTER_INDICATOR, REVENUE, REVENUE_YOY, ROE,
)

# 方向至少要 3 季（2 次同向变动）才叫趋势，否则是震荡
MIN_TREND_QUARTERS = 3
# 均值对比的免打扰区：ROE / 净利率 4 季均值差在此以内视为持平（单位 pp）
_ROE_EPS = 0.1
_MARGIN_EPS = 0.5
# 「连续 N 季同比下滑」至少 2 季才报
MIN_DECLINE_QUARTERS = 2

IMPROVING, WORSENING, CHOPPY = "改善", "恶化", "震荡"


def _series(rows: list[dict], field: str) -> list[tuple[str, float]]:
    """(报告期, 值) 序列，时间升序，**跳过缺失季**（不重置连续计数就靠这一步）。"""
    return [(r.get(PERIOD), r[field]) for r in rows
            if r.get(field) is not None]


def _run_from_latest(values: list[float], rising: bool) -> int:
    """从最新一期往回数单调区间的季数（严格单调，含最新那季本身）。"""
    if not values:
        return 0
    n = 1
    for i in range(len(values) - 1, 0, -1):
        step_up = values[i] > values[i - 1]
        if step_up != rising or values[i] == values[i - 1]:
            break
        n += 1
    return n


def _trend(rows: list[dict], field: str) -> dict:
    """同比增长率序列 → 方向 + 连续季数。"""
    ser = _series(rows, field)
    values = [v for _, v in ser]
    up = _run_from_latest(values, rising=True)
    down = _run_from_latest(values, rising=False)
    if up >= MIN_TREND_QUARTERS:
        direction, quarters = IMPROVING, up
    elif down >= MIN_TREND_QUARTERS:
        direction, quarters = WORSENING, down
    else:
        direction, quarters = CHOPPY, max(up, down)
    decline = 0  # 连续多少季同比为负（「下滑」≠「恶化」：前者看正负，后者看斜率）
    for _, v in reversed(ser):
        if v >= 0:
            break
        decline += 1
    return {
        "direction": direction,
        "quarters": quarters,
        "latest_yoy": values[-1] if values else None,
        "decline_quarters": decline,
        "series": [{"报告期": p, "同比": v} for p, v in ser],
    }


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _level(rows: list[dict], field: str, eps: float) -> dict:
    """水平型指标（ROE / 净利率）：最新值 + 近 8 季均值 + 近4季 vs 前4季。"""
    ser = _series(rows, field)
    values = [v for _, v in ser]
    recent4, prev4 = _avg(values[-4:]), _avg(values[-8:-4])
    direction = CHOPPY
    if recent4 is not None and prev4 is not None:
        if recent4 - prev4 > eps:
            direction = IMPROVING
        elif prev4 - recent4 > eps:
            direction = WORSENING
    return {
        "latest": values[-1] if values else None,
        "period": ser[-1][0] if ser else None,
        "avg8": _avg(values[-8:]),
        "recent4": recent4,
        "prev4": prev4,
        "direction": direction,
        "series": [{"报告期": p, field: v} for p, v in ser],
    }


def _leverage(q_rows: list[dict], a_rows: list[dict]) -> dict:
    """资产负债率：最新单季值 + 它在近 N 年年报里的位置。"""
    latest = next((r[LEVERAGE] for r in reversed(q_rows)
                   if r.get(LEVERAGE) is not None), None)
    annual = [(r.get(PERIOD), r[LEVERAGE]) for r in a_rows
              if r.get(LEVERAGE) is not None]
    values = [v for _, v in annual]
    pct = None
    if latest is not None and values:
        pct = round(sum(1 for v in values if v < latest) / len(values) * 100, 1)
    return {
        "latest": latest,
        "years": len(values),
        "annual_max": max(values) if values else None,
        "annual_min": min(values) if values else None,
        "percentile": pct,
        "annual": [{"报告期": p, LEVERAGE: v} for p, v in annual],
    }


def _flags(rev: dict, prof: dict, roe: dict, margin: dict, lev: dict,
           latest: dict) -> list[str]:
    flags = []
    for label, tr in (("营收", rev), ("净利", prof)):
        if tr["decline_quarters"] >= MIN_DECLINE_QUARTERS:
            flags.append(f"连续{tr['decline_quarters']}季{label}同比下滑")
        if tr["direction"] != CHOPPY:
            flags.append(f"{label}同比连续{tr['quarters']}季{tr['direction']}")
    if latest.get(PROFIT) is not None and latest[PROFIT] < 0:
        flags.append("最新单季净利润为负")
    if roe["direction"] == IMPROVING:
        flags.append("ROE 处于上升通道")
    elif roe["direction"] == WORSENING:
        flags.append("ROE 处于下降通道")
    if margin["direction"] == IMPROVING:
        flags.append("销售净利率走强")
    elif margin["direction"] == WORSENING:
        flags.append("销售净利率走弱")
    if lev["latest"] is not None and lev["years"]:
        if lev["latest"] > lev["annual_max"]:
            flags.append(f"资产负债率创{lev['years']}年新高")
        elif lev["latest"] < lev["annual_min"]:
            flags.append(f"资产负债率创{lev['years']}年新低")
    return flags


def summarize(fund: dict) -> dict:
    """基本面 dict → 确定性判断（趋势 / 水平 / 杠杆 / flags）。

    口径校验：`quarterly` 必须是 `按单季度`、`annual` 必须是 `按年度`。喂累计口径
    （`按报告期`）进来直接 ValueError——那种数据算出来的「趋势」是纯噪声，宁可炸掉
    也不能让它悄悄流到页面和模型上下文里。
    """
    if not fund:
        raise ValueError("summarize() 需要 get_fundamentals() 的返回值，收到空值")
    for key, expect in (("quarterly", QUARTER_INDICATOR), ("annual", ANNUAL_INDICATOR)):
        got = (fund.get(key) or {}).get("indicator")
        if got != expect:
            raise ValueError(
                f"{key} 口径应为 {expect}，收到 {got!r}——累计口径不可用于趋势判断"
            )

    q_rows = fund["quarterly"]["rows"]
    a_rows = fund["annual"]["rows"]
    latest = q_rows[-1] if q_rows else {}

    rev = _trend(q_rows, REVENUE_YOY)
    prof = _trend(q_rows, PROFIT_YOY)
    roe = _level(q_rows, ROE, _ROE_EPS)
    margin = _level(q_rows, NET_MARGIN, _MARGIN_EPS)
    lev = _leverage(q_rows, a_rows)

    return {
        "symbol": fund.get("symbol"),
        "period": latest.get(PERIOD),
        "latest": {k: latest.get(k) for k in
                   (REVENUE, REVENUE_YOY, PROFIT, PROFIT_YOY,
                    NET_MARGIN, ROE, LEVERAGE)},
        "revenue_trend": rev,
        "profit_trend": prof,
        "roe_level": roe,
        "margin_trend": margin,
        "leverage": lev,
        "flags": _flags(rev, prof, roe, margin, lev, latest),
    }
