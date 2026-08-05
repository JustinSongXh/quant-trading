"""板块内选股：两级漏斗 + 确定性排序（issue #35）

给定一个板块，产出板块内的候选股排名 + 每只的确定性依据。零 token：技术/缠论信号、
基本面 flags、相对板块超额收益全是已有的纯计算，排序分是写死的加权公式。

## 为什么必须是漏斗

成分股动辄上百只（实测 银行 84 家、电网设备 140 家）。对每只取日K + 财报再排序，
在一次交互里做不完——总览页扫 36 个标的就要几十秒。所以：

- **粗筛（免费）**：成分股接口自带 `现价 / 涨跌幅`，按涨跌幅降序取前 N
  （`config/settings.py::SECTOR_PICK_TOP_N`，页面可临时改）。
- **细算（贵）**：只对这 N 只算技术 + 缠论信号（复用 `strategy/signals.py::build_signals`）、
  基本面 flags（复用 `analysis/fundamental.py::summarize`）、相对板块超额收益。

**不含 Kronos**：单只 CPU 上约 40 秒，20 只就是 13 分钟。`PICK_SIGNALS` 写死不含它。

粗筛用的涨跌幅来自成分股缓存（7 天 TTL + 过期回落，见 `data/sector_cons.py`），所以它
可能是几天前的快照——返回值里的 `fetched_at` / `stale` 必须显示到页面上，否则「按涨跌幅
筛出的前 20 只」会被读成今天的涨幅榜。

**不许静默截断**：返回值同时给 `total_constituents` 与 `computed`，页面照原样写出来
（#30 的教训——被静默丢掉的覆盖面会被读成「全都算过了」）。

## 排序分（确定性）

    排序分 = w_tech · z(技术信号) + w_excess · z(相对板块超额收益) + w_fund · 基本面加减分

前两项在**候选集内**做 z-score（复用 `analysis/rotation.py::zscore`，ddof=1、有效值
不足 2 个给 0）。权重取 `config/settings.py::SECTOR_PICK_WEIGHTS`，运行时读。

缠论信号照样算、照样返回、页面照样显示，但**不进排序分**——issue 定的公式只有三项。
它是给人看的旁证，不是权重项。

不产生 signal、不进 `fusion`、不影响回测与仓位。
"""

import pandas as pd

import config.settings as cfg
from analysis.fundamental import summarize
from analysis.rotation import zscore
from config.pool import get_sector_info
from data.fundamentals import get_fundamentals
from data.kline import get_kline
from data.sector_cons import get_sector_constituents, unavailable_reason
from strategy.signals import build_signals
from utils.logger import get_logger

logger = get_logger("sector_pick")

DEFAULT_TOP_N = 20
# 相对板块强弱的窗口（交易日）：个股 N 日累计收益 − 该板块指数同期累计收益
PICK_WINDOW = 20
# 取日K的日历天数：技术指标要 ~150 天才收敛（见 SIGNAL_LOOKBACK["technical"]），
# 缠论同量级，PICK_WINDOW 只有 20 个交易日，取 200 天两者都够
KLINE_DAYS = 200

# 细算跑哪些信号源：**刻意不含 kronos**（单只 CPU 上约 40 秒）
PICK_SIGNALS = ("technical", "chanlun")

W_TECH, W_EXCESS, W_FUND = "technical", "excess", "fundamental"

FLAG_UNIT = 0.5   # 每个正向 flag +0.5、每个负向 flag −0.5

# 基本面 flags → 加减分的**写死映射表**。key 是 analysis/fundamental.py::_flags 模板里
# 最稳定的那一段（那些标签带季数/年数，不能整串匹配），顺序即优先级，第一个命中的赢。
# 「同比下滑」必须排在「恶化」之前：`连续3季营收同比下滑` 与 `营收同比连续3季恶化` 会
# 同时出现，两者都是 −0.5，但前者不含「恶化」，漏了就少扣一次。
# 认不出的 flag 给 0 分并记日志——新增标签宁可不计分，也不要按猜的方向算进排序。
_FLAG_RULES = (
    ("同比下滑", -FLAG_UNIT),        # 连续N季营收/净利同比下滑
    ("改善", +FLAG_UNIT),            # 营收/净利同比连续N季改善
    ("恶化", -FLAG_UNIT),            # 营收/净利同比连续N季恶化
    ("最新单季净利润为负", -FLAG_UNIT),
    ("上升通道", +FLAG_UNIT),        # ROE 处于上升通道
    ("下降通道", -FLAG_UNIT),        # ROE 处于下降通道
    ("走强", +FLAG_UNIT),            # 销售净利率走强
    ("走弱", -FLAG_UNIT),            # 销售净利率走弱
    ("年新高", -FLAG_UNIT),          # 资产负债率创N年新高（杠杆变高是负向）
    ("年新低", +FLAG_UNIT),          # 资产负债率创N年新低
)

NOTE_NO_FUND = "基本面取不到（非 A 股或接口失败），基本面项按 0 计"
NOTE_NO_SECTOR_RET = "板块指数日K取不到，超额收益缺失、该项按 0 计"


def flag_score(flags: list[str] | None) -> tuple[float, list[str]]:
    """flags → (加减分, 认不出的 flag 列表)。flags 为 None（取不到基本面）时给 0 分。"""
    if not flags:
        return 0.0, []
    score, unknown = 0.0, []
    for flag in flags:
        for key, delta in _FLAG_RULES:
            if key in flag:
                score += delta
                break
        else:
            unknown.append(flag)
            logger.info(f"未识别的基本面 flag「{flag}」，按 0 分计（见 _FLAG_RULES）")
    return round(score, 3), unknown


def _cum_return(closes: pd.Series, window: int) -> float | None:
    """近 window 个交易日的累计收益（%）。不足 window+1 根 K 线返回 None。"""
    s = pd.Series(closes).dropna()
    if len(s) < window + 1:
        return None
    first, last = float(s.iloc[-(window + 1)]), float(s.iloc[-1])
    if first == 0:
        return None
    return round((last / first - 1) * 100, 3)


def _coarse_screen(constituents: list[dict], top_n: int) -> list[dict]:
    """粗筛：按涨跌幅降序取前 top_n。涨跌幅缺失（停牌/限流时接口给 '-'）的排最后——
    缺失不代表跌，但也没有理由让它挤掉有数据的标的。"""
    ordered = sorted(
        constituents,
        key=lambda c: (c.get("change_pct") is None,
                       -(c.get("change_pct") or 0.0), c.get("code") or ""),
    )
    return ordered[:top_n]


def _sector_return(sector_code: str, window: int,
                   force_refresh: bool = False) -> float | None:
    """板块指数近 window 交易日累计收益（%），取不到返回 None（超额项随之缺失）。"""
    df = get_kline(sector_code, int(window * 1.5) + 40, stock_type="sector",
                   force_refresh=force_refresh)
    if df is None or df.empty or "close" not in df.columns:
        return None
    return _cum_return(df["close"], window)


def _compute_one(item: dict, sector_ret: float | None, window: int,
                 force_refresh: bool) -> tuple[dict | None, str | None]:
    """细算一只：(候选行, 失败原因)。日K取不到/不足 → (None, 原因)，由调用方进 warnings。"""
    code, name = item["code"], item.get("name") or item["code"]
    df = get_kline(code, days=KLINE_DAYS, stock_type="stock",
                   force_refresh=force_refresh)
    if df is None or df.empty:
        return None, f"{name}({code}) 日K取数失败，已跳过"
    if len(df) < window + 1:
        return None, f"{name}({code}) 日K只有 {len(df)} 根，不足 {window + 1} 根，已跳过"

    try:
        sig = build_signals(df, symbol=code, enabled_signals=list(PICK_SIGNALS))
    except Exception as e:
        return None, f"{name}({code}) 信号计算失败（{e}），已跳过"
    last = sig.iloc[-1]
    tech = float(last.get("technical_signal") or 0.0)
    chan = float(last.get("chanlun_signal") or 0.0)

    stock_ret = _cum_return(df["close"], window)
    excess = None if (stock_ret is None or sector_ret is None) else \
        round(stock_ret - sector_ret, 3)

    notes = []
    if sector_ret is None:
        notes.append(NOTE_NO_SECTOR_RET)
    flags = None
    try:
        fund = get_fundamentals(code)
        if fund:
            flags = summarize(fund).get("flags") or []
    except Exception as e:
        logger.info(f"{code} 基本面汇总失败: {e}")
    if flags is None:
        notes.append(NOTE_NO_FUND)
    fund_score, unknown = flag_score(flags)
    if unknown:
        notes.append(f"未计分的基本面标签：{'、'.join(unknown)}")

    return {
        "code": code, "name": name,
        "price": item.get("price"), "change_pct": item.get("change_pct"),
        "technical": round(tech, 3), "chanlun": round(chan, 3),
        "stock_return": stock_ret, "excess": excess,
        "fund_flags": flags, "fund_score": fund_score,
        "note": "；".join(notes),
    }, None


def pick(sector_code: str, kind: str | None = None, top_n: int | None = None,
         window: int = PICK_WINDOW, force_refresh: bool = False,
         force_cons: bool = False, sector_name: str | None = None,
         progress_cb=None) -> dict:
    """板块内选股：成分股 → 粗筛 top_n → 细算 → 确定性排序。

    Args:
        sector_code: 板块代码（THS，股票池里配置的那个）
        kind: industry / concept；None 时从股票池配置推断
        sector_name: THS 板块名；None 时从股票池配置取。池外板块两者都取不到 →
            直接返回不可用（成分股要靠**名字**匹配东财板块，没有名字无从下手）
        top_n: 细算只数，None 时取 `SECTOR_PICK_TOP_N`
        window: 相对板块强弱的窗口（交易日）
        force_refresh: 忽略日K缓存重新取（贵，页面不用）
        force_cons: 只强制重取**成分股**（页面的「重试取数」按钮用——限流是窗口性的，
            多试一轮常能撞上；日K缓存照旧复用，不然一次重试要重下 20 只的日K）
        progress_cb: `(current, total, message)` 细算阶段逐只回调，给页面进度条

    Returns:
        ```
        {"available": bool, "sector", "sector_code", "kind",
         "em_code", "em_name", "match_level",
         "total_constituents": int, "computed": int, "top_n": int,
         "stale": bool, "fetched_at": str|None, "sector_return": float|None,
         "weights": {...}, "candidates": [...], "warnings": [...]}
        ```
        `available=False` 表示这个板块不支持选股（成分股取不到 / 未匹配到东财板块），
        原因在 `warnings[0]`，页面照它禁用入口并把话说清楚。

        `candidates` 每项：代码/名称/现价/涨跌幅（粗筛用的快照值）、技术信号、缠论信号、
        `window` 日收益、相对板块超额收益、基本面 flags 与加减分、三个 z 分项、排序分
        `score`、`note`（缺项说明）。按 `score` 降序。
    """
    info = get_sector_info(sector_code)
    resolved_name = sector_name or (info[0] if info else None)
    kind = kind or (info[1] if info else None)
    top_n = int(top_n or getattr(cfg, "SECTOR_PICK_TOP_N", DEFAULT_TOP_N))

    base = {"available": False, "sector": resolved_name or sector_code,
            "sector_code": sector_code,
            "kind": kind, "em_code": None, "em_name": None, "match_level": None,
            "em_kind": None, "cross_kind": False,
            "total_constituents": 0, "computed": 0, "top_n": top_n,
            "stale": False, "fetched_at": None, "sector_return": None,
            "weights": {}, "candidates": [], "warnings": []}

    if not resolved_name or not kind:
        # 池外板块没有名称/类型配置，名称匹配无从下手（同 fetch_daily_kline 的板块分支）。
        # 这条路要单独说清楚，否则提示会变成「东财名录里没有 881160」，让人以为是名称
        # 匹配的问题、跑去补 alias
        base["warnings"] = [f"板块 {sector_code} 不在股票池配置里（缺名称/类型），无法取成分股。"
                            f"先到「股票池管理」页把它加入股票池"]
        return base

    cons = get_sector_constituents(resolved_name, kind,
                                   force_refresh=force_refresh or force_cons)
    if not cons or not cons.get("constituents"):
        base["warnings"] = [unavailable_reason(resolved_name, kind)]
        return base

    constituents = cons["constituents"]
    picked = _coarse_screen(constituents, top_n)
    sector_ret = _sector_return(sector_code, window, force_refresh=force_refresh)

    rows, warnings = [], []
    total = len(picked)
    for i, item in enumerate(picked, start=1):
        if progress_cb:
            progress_cb(i, total, f"{item.get('name') or item['code']}")
        row, err = _compute_one(item, sector_ret, window, force_refresh)
        if err:
            warnings.append(err)
        else:
            rows.append(row)

    if sector_ret is None:
        warnings.append("板块指数日K取不到，「相对板块超额收益」这一列缺失、按 0 计入排序分")

    # z-score 在**候选集内**算（不是全成分股，也不是全市场）
    z_tech = zscore([r["technical"] for r in rows])
    z_excess = zscore([r["excess"] for r in rows])
    weights = getattr(cfg, "SECTOR_PICK_WEIGHTS", {})
    w_tech = float(weights.get(W_TECH, 1.0))
    w_excess = float(weights.get(W_EXCESS, 1.0))
    w_fund = float(weights.get(W_FUND, 1.0))
    for r, zt, ze in zip(rows, z_tech, z_excess):
        r["z_technical"] = zt
        r["z_excess"] = ze
        # 缺失的 z 分项按 0 计（该行 note 里已写明缺什么），基本面同理
        r["score"] = round(w_tech * (zt or 0.0) + w_excess * (ze or 0.0)
                           + w_fund * r["fund_score"], 3)
    rows.sort(key=lambda r: r["score"], reverse=True)

    if cons.get("stale"):
        warnings.append(f"成分股用的是过期缓存（{cons.get('fetched_at')}，东财接口限流），"
                        f"粗筛依据的涨跌幅也是那一刻的快照")

    base.update({
        "available": True,
        "em_code": cons.get("em_code"), "em_name": cons.get("em_name"),
        "match_level": cons.get("match_level"),
        # 东财把这个板块归在哪一类（#38：两家 kind 口径不一致时页面要能说明）
        "em_kind": cons.get("em_kind"), "cross_kind": bool(cons.get("cross_kind")),
        "total_constituents": len(constituents), "computed": len(rows),
        "stale": bool(cons.get("stale")), "fetched_at": cons.get("fetched_at"),
        "sector_return": sector_ret,
        "weights": {W_TECH: w_tech, W_EXCESS: w_excess, W_FUND: w_fund},
        "candidates": rows, "warnings": warnings,
    })
    return base
