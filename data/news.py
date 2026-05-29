"""多源新闻/公告/股吧采集编排（issue #1）

维护"过去 N 个有效交易日"窗口的数据：增量拉取 → upsert 缓存 → 滚动清理 → 返回。
- 增量：每个源只抓 (缓存最新, 今天]，窗口内已缓存的不重拉；首次冷启动拉满窗口。
- 窗口：用 AKShare 交易日历定边界，窗口内的日历日（含周末）数据全部保留。
- 数据源见 data/news_sources/；情绪打分（sentiment_score）由 analysis/sentiment.py 填写。
"""

from datetime import datetime, time, timedelta

import pandas as pd

from data.cache import (
    get_latest_news_published_at,
    load_news_items,
    purge_news_outside_window,
    upsert_news_items,
)
from data.fetcher import is_hk_stock, is_index
from data.news_sources import cninfo, em_guba, em_news
from utils.logger import get_logger

logger = get_logger("news")

DEFAULT_WINDOW = 10


def _trading_window_start(lookback_trading_days: int) -> datetime:
    """过去 N 个有效交易日里最早一天的 00:00"""
    today = datetime.now().date()
    try:
        import akshare as ak

        cal = [d for d in ak.tool_trade_date_hist_sina()["trade_date"] if d <= today]
    except Exception as e:
        logger.warning(f"  交易日历加载失败，按自然日回退: {e}")
        cal = []
    if cal:
        idx = max(0, len(cal) - lookback_trading_days)
        start_date = cal[idx]
    else:
        # 兜底：交易日约占 70%，多回退一些保证覆盖
        start_date = today - timedelta(days=int(lookback_trading_days * 1.6) + 3)
    return datetime.combine(start_date, time.min)


def _sources_for(symbol: str, stock_type: str | None):
    """按品种选择数据源。
    - 指数：本期不采集
    - 港股：仅东财新闻（公告/股吧本期为 A 股）
    - A 股：公告 + 新闻 + 股吧
    """
    if is_index(symbol, stock_type):
        return []
    if is_hk_stock(symbol):
        return [em_news]
    return [cninfo, em_news, em_guba]


def fetch_stock_news(
    symbol: str,
    lookback_trading_days: int = DEFAULT_WINDOW,
    stock_type: str | None = None,
) -> pd.DataFrame:
    """从 DuckDB 缓存读取，缺失部分增量拉取后返回。

    返回列：source, external_id, stock_code, title, content,
    published_at, fetched_at, sentiment_score（按 published_at 倒序）。
    """
    window_start = _trading_window_start(lookback_trading_days)
    end = datetime.now()

    for src in _sources_for(symbol, stock_type):
        latest = get_latest_news_published_at(symbol, src.SOURCE)
        # 增量起点：缓存最新与窗口起点取较晚者；窗口外的不重拉
        start = max(window_start, latest) if latest else window_start
        try:
            items = src.fetch(symbol, start, end)
        except Exception as e:
            logger.warning(f"  {symbol} 源 {src.SOURCE} 拉取异常: {e}")
            continue
        items = [it for it in items if it["published_at"] >= window_start]
        inserted = upsert_news_items(items)
        logger.info(f"  {symbol} {src.SOURCE}: 抓到 {len(items)} 条，新增 {inserted} 条")

    purge_news_outside_window(window_start)
    return load_news_items(symbol, since=window_start)
