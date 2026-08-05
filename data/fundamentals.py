"""基本面数据层（issue #33）

数据源 `ak.stock_financial_abstract_ths`——与行情/板块一致选同花顺（走 10jqka 域名，
独立于常被限流的东财 push2，#28/#32 都验证过这点）。

取两个口径，各有用途：

- `按单季度` 近 12 季：判断趋势拐点、「连续几季改善/恶化」
- `按年度`   近 5 年 ：看长期水平

**不用 `按报告期`**：那是累计值（Q1 / H1 / 9M / FY），季度之间不可比——浦发 2026Q1
营收 465.73 亿 vs 2025FY 1739.64 亿，拿相邻两行比趋势会得出荒谬结论。累计口径下
`同比增长率` 列本身是对的，但拐点判断必须用单季度。

仅 A 股。港股基本面接口是 `stock_financial_hk_analysis_indicator_em`，另开 issue；
指数/板块没有财报，直接返回 None 且不打接口。
"""

import json
import os

from datetime import datetime

from data.fetcher import is_hk_stock, is_index, is_sector
# 24h mtime 缓存判定沿用统一的那套惯例，不另造机制（#33；#41 起在 data/file_cache.py）
from data.file_cache import is_fresh_hours
from utils.logger import get_logger

logger = get_logger("fundamentals")

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache", "fundamentals")
# 财报按季度更新，24h 足够新；沿用与股票池/板块列表相同的 mtime 口径
_CACHE_MAX_AGE_HOURS = 24

QUARTER_INDICATOR = "按单季度"
ANNUAL_INDICATOR = "按年度"
QUARTERS = 12  # 近 12 季单季
YEARS = 5      # 近 5 年年度

# 字段白名单。窄一点是刻意的：这些字段将来要喂给对话层的模型，上下文越窄越不容易
# 跑偏；每股资本公积金、保守速动比率之类对判断没贡献，接口另外 12 列一概不要。
PERIOD = "报告期"
REVENUE = "营业总收入"
REVENUE_YOY = "营业总收入同比增长率"
PROFIT = "净利润"
PROFIT_YOY = "净利润同比增长率"
NET_MARGIN = "销售净利率"
ROE = "净资产收益率"
LEVERAGE = "资产负债率"

FIELDS = (PERIOD, REVENUE, REVENUE_YOY, PROFIT, PROFIT_YOY,
          NET_MARGIN, ROE, LEVERAGE)

_UNITS = {"万亿": 1e12, "亿": 1e8, "万": 1e4}
_NULLS = ("", "--", "-", "—", "nan", "None", "false", "true")


def parse_value(raw) -> float | None:
    """接口返回的是给人看的字符串，统一转 float。

    - `'1739.64亿'` → `173964000000.0`（支持 万 / 亿 / 万亿）
    - `'10.52%'`    → `10.52`（单位是 %，不除 100）
    - `False` / `''` / `'--'` / `None` → `None`
      实测银行股的 `流动比率` 就是布尔 `False`，不是字符串；白名单外的列虽然不取，
      白名单内的列同样会出现空值（早年报表缺项），一律归 None。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace(",", "")
    if s.lower() in _NULLS:
        return None
    mult = 1.0
    if s.endswith("%"):
        s = s[:-1]
    else:
        for suffix, m in _UNITS.items():  # 万亿 排在 万 之前，先长后短
            if s.endswith(suffix):
                s, mult = s[: -len(suffix)], m
                break
    try:
        # 乘完要 round：42.16 * 1e4 是 421599.99999999994，直接留着会污染下游展示
        return round(float(s) * mult, 2)
    except ValueError:
        return None


def _rows_from_df(df, limit: int) -> list[dict]:
    """按白名单裁列 + 取最近 limit 行，时间升序（接口本身升序，最新在末尾）。"""
    rows = []
    for _, r in df.tail(limit).iterrows():
        row = {PERIOD: str(r[PERIOD]).strip()}
        for col in FIELDS[1:]:
            row[col] = parse_value(r[col]) if col in df.columns else None
        rows.append(row)
    return rows


def _fetch(symbol: str) -> dict | None:
    """打两次接口（单季度 + 年度），任一为空即视为取数失败。"""
    import akshare as ak

    series = {}
    for key, indicator, limit in (("quarterly", QUARTER_INDICATOR, QUARTERS),
                                  ("annual", ANNUAL_INDICATOR, YEARS)):
        try:
            df = ak.stock_financial_abstract_ths(symbol=symbol, indicator=indicator)
        except Exception as e:
            logger.warning(f"取 {symbol} {indicator} 基本面失败: {e}")
            return None
        if df is None or df.empty or PERIOD not in df.columns:
            logger.warning(f"取 {symbol} {indicator} 基本面返回空表")
            return None
        series[key] = {"indicator": indicator, "rows": _rows_from_df(df, limit)}

    return {
        "symbol": symbol,
        "quarterly": series["quarterly"],
        "annual": series["annual"],
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def get_fundamentals(symbol: str, stock_type: str | None = None,
                     force_refresh: bool = False) -> dict | None:
    """A 股基本面（单季度近 12 季 + 年度近 5 年），带 24h 文件缓存。

    返回 None 的情形（都不打接口 / 不抛给页面）：
    - 非 A 股个股（港股 / 指数 / 板块 / 代码不是 6 位数字）
    - 接口异常或返回空表
    """
    if is_sector(symbol, stock_type) or is_index(symbol, stock_type) \
            or is_hk_stock(symbol) or not (len(symbol) == 6 and symbol.isdigit()):
        logger.info(f"{symbol} 非 A 股个股，跳过基本面（本接口只有 A 股）")
        return None

    path = os.path.join(_CACHE_DIR, f"{symbol}.json")
    if not force_refresh and is_fresh_hours(path, _CACHE_MAX_AGE_HOURS):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # 缓存写坏了不该让整张卡片打不开
            logger.warning(f"读 {symbol} 基本面缓存失败，改打接口: {e}")

    fund = _fetch(symbol)
    if fund is None:
        # 取数失败时旧缓存比空白有用（财报按季更新，过期一两天无妨）
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fund, f, ensure_ascii=False, indent=2)
    return fund
