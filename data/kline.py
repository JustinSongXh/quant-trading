"""日K的取数与缓存编排（issue #41）

「缓存新鲜就用 → 否则取数落盘 → 失败回落过期缓存」这一套原先在**四处**各写一遍：
`app.py::get_stock_data`、`analysis/sector_pick.py::_get_stock_kline`、
`analysis/correlation.py::_get_sector_kline`，以及 `correlation._get_index_returns`
里内联的那一段。cache key 的前缀规则也跟着抄了四份。

副本存在的原因写在它们自己的 docstring 里——「逻辑同 app.get_stock_data，但不依赖
app.py（避免导入触发 Streamlit 脚本执行）」。既然这套逻辑属于数据层，就放在数据层：
`analysis/*` 只该说「要一份日K」，不该各自实现新鲜度判断与回落策略。

`cache` 与 `fetcher` **按模块引用而不是 from-import 具体函数**：这样测试只要替
`data.fetcher.fetch_daily_kline` 一处，经过本模块的所有取数路径都被拦住（from-import
会在导入时把函数对象绑死，替源模块无效——这个坑见 CLAUDE.md 第 3 节）。
"""

import pandas as pd

from data import cache, fetcher
from utils.logger import get_logger

logger = get_logger("kline")

# 缓存新鲜时还要求缓存覆盖的**日历天跨度**够长，差这么多天以内算够
_SPAN_TOLERANCE_DAYS = 7


def cache_key(code: str, stock_type: str | None = None) -> str:
    """指数/板块用带类型前缀的 cache key，避免与同代码个股在 DuckDB 里撞表。"""
    if stock_type == "index":
        return f"idx_{code}"
    if stock_type == "sector":
        return f"sec_{code}"
    return code


def get_kline(code: str, days: int = 365, stock_type: str | None = None,
              force_refresh: bool = False) -> pd.DataFrame:
    """日K，优先用新鲜缓存，过期或缓存天数不够则刷新。

    Args:
        code: 股票/指数/板块代码
        days: 需要的日历天跨度
        stock_type: stock（默认）/ index / sector，只影响 cache key 与取数分支
        force_refresh: 跳过缓存新鲜度判断，直接取数

    Returns:
        日K DataFrame（index 为日期）；数据源全失败且无任何缓存时返回**空 DataFrame**，
        由调用方决定如何提示用户。
    """
    key = cache_key(code, stock_type)
    if not force_refresh and cache.is_cache_fresh(key):
        df = cache.load_kline(key)
        # 按日历天跨度比，而非行数——交易日数永远 < 日历天数，用 len 比会永远判不足
        # 而反复重抓
        if df is not None and not df.empty and \
                (df.index.max() - df.index.min()).days >= days - _SPAN_TOLERANCE_DAYS:
            return df
    try:
        df = fetcher.fetch_daily_kline(code, days=days, stock_type=stock_type)
        if df is not None and not df.empty:
            cache.save_kline(key, df)
            return df
    except Exception as e:
        logger.info(f"{code} 日K取数失败: {e}")
    # 取数失败或返回空：用任意已有缓存兜底（哪怕已过期）
    df = cache.load_kline(key)
    return df if df is not None else pd.DataFrame()
