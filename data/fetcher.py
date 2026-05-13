"""行情与财务数据采集（AKShare 优先，baostock 备选）"""

import pandas as pd
from config.settings import DEFAULT_LOOKBACK_DAYS
from datetime import datetime, timedelta
from utils.logger import get_logger

logger = get_logger("fetcher")


def _symbol_to_baostock(symbol: str) -> str:
    """将纯数字代码转为 baostock 格式：sh.600519 / sz.000858"""
    if symbol.startswith(("6", "9")):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


def _fetch_via_akshare(symbol: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_zh_a_hist(
        symbol=symbol, period="daily",
        start_date=start, end_date=end, adjust="qfq",
    )
    # 不硬编码列名，用 AKShare 返回的原始列名做映射
    col_map = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
               "最低": "low", "成交量": "volume", "成交额": "turnover",
               "振幅": "amplitude", "涨跌幅": "pct_change", "涨跌额": "change",
               "换手率": "turnover_rate"}
    df = df.rename(columns=col_map)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    # 只保留核心列
    core_cols = ["open", "close", "high", "low", "volume"]
    return df[[c for c in core_cols if c in df.columns]]


def _fetch_via_baostock(symbol: str, start: str, end: str) -> pd.DataFrame:
    import baostock as bs
    bs.login()
    bs_symbol = _symbol_to_baostock(symbol)
    start_fmt = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    end_fmt = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    rs = bs.query_history_k_data_plus(
        bs_symbol,
        "date,open,high,low,close,volume",
        start_date=start_fmt, end_date=end_fmt,
        frequency="d", adjustflag="2",  # 前复权
    )
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    bs.logout()

    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df


def fetch_daily_kline(symbol: str, days: int = DEFAULT_LOOKBACK_DAYS) -> pd.DataFrame:
    """获取个股日K线数据（前复权），AKShare 优先，失败则用 baostock"""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    try:
        df = _fetch_via_akshare(symbol, start, end)
        if not df.empty:
            logger.info(f"  {symbol}: fetched {len(df)} rows via AKShare")
            return df
    except Exception as e:
        logger.warning(f"  {symbol}: AKShare failed ({e}), trying baostock...")

    df = _fetch_via_baostock(symbol, start, end)
    logger.info(f"  {symbol}: fetched {len(df)} rows via baostock")
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
