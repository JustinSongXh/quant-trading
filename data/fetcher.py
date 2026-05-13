"""AKShare 行情与财务数据采集"""

import akshare as ak
import pandas as pd
from config.settings import DEFAULT_LOOKBACK_DAYS
from datetime import datetime, timedelta


def fetch_daily_kline(symbol: str, days: int = DEFAULT_LOOKBACK_DAYS) -> pd.DataFrame:
    """获取个股日K线数据（前复权）"""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start,
        end_date=end,
        adjust="qfq",
    )
    df.columns = ["date", "open", "close", "high", "low", "volume", "turnover",
                  "amplitude", "pct_change", "change", "turnover_rate"]
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df


def fetch_stock_info(symbol: str) -> dict:
    """获取个股基本信息（名称、行业、板块类型等）"""
    prefix = symbol[:3]
    if prefix in ("600", "601", "603", "605"):
        board = "main_board"
    elif prefix == "300":
        board = "gem"
    elif prefix == "688":
        board = "star"
    elif prefix in ("000", "001", "002"):
        board = "main_board"
    else:
        board = "main_board"
    return {"symbol": symbol, "board": board}
