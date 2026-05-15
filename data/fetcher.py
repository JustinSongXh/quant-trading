"""行情数据采集（A股: AKShare/baostock, 港股: 腾讯财经）"""

import pandas as pd
import requests
from config.settings import DEFAULT_LOOKBACK_DAYS
from datetime import datetime, timedelta, time
from utils.logger import get_logger

logger = get_logger("fetcher")


def is_hk_stock(symbol: str) -> bool:
    """判断是否为港股代码（5位数字）"""
    return len(symbol) == 5 and symbol.isdigit()


def is_index(symbol: str, stock_type: str | None = None) -> bool:
    """判断是否为指数代码

    优先使用 stock_type 字段判断，否则按代码推断。
    """
    if stock_type == "index":
        return True
    if stock_type == "stock":
        return False
    # 常见指数代码段（无 type 字段时的兜底）
    return symbol in ("000001",) and False  # 有歧义，需要显式 type


# ========== A 股数据采集 ==========

def _symbol_to_baostock(symbol: str) -> str:
    if symbol.startswith(("6", "9")):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


def _fetch_via_akshare(symbol: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_zh_a_hist(
        symbol=symbol, period="daily",
        start_date=start, end_date=end, adjust="qfq",
    )
    col_map = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
               "最低": "low", "成交量": "volume", "成交额": "turnover",
               "振幅": "amplitude", "涨跌幅": "pct_change", "涨跌额": "change",
               "换手率": "turnover_rate"}
    df = df.rename(columns=col_map)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
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
        frequency="d", adjustflag="2",
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


def _fetch_a_stock(symbol: str, start: str, end: str) -> pd.DataFrame:
    """A 股：AKShare 优先，baostock 备选"""
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


# ========== 港股数据采集 ==========

def _fetch_hk_stock(symbol: str, start: str, end: str) -> pd.DataFrame:
    """港股：通过腾讯财经接口获取日K线（前复权）"""
    start_fmt = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    end_fmt = f"{end[:4]}-{end[4:6]}-{end[6:]}"

    url = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
    params = {"param": f"hk{symbol},day,{start_fmt},{end_fmt},500,qfq"}

    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()

    hk_key = f"hk{symbol}"
    hk_data = data.get("data", {}).get(hk_key, {})
    # 优先取前复权数据，没有则取普通日线
    klines = hk_data.get("qfqday") or hk_data.get("day") or []
    if not klines:
        raise ValueError(f"No HK data returned for {symbol}")

    # 腾讯格式: [date, open, close, high, low, volume, ...]
    rows = []
    for k in klines:
        rows.append({
            "date": k[0],
            "open": float(k[1]),
            "close": float(k[2]),
            "high": float(k[3]),
            "low": float(k[4]),
            "volume": float(k[5]),
        })

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    logger.info(f"  {symbol}: fetched {len(df)} rows via Tencent HK")
    return df


# ========== 指数数据采集 ==========

def _index_symbol_to_akshare(symbol: str) -> str:
    """指数代码转 AKShare 格式（sh000001 / sz399006）"""
    if symbol.startswith("0"):
        return f"sh{symbol}"
    elif symbol.startswith("3"):
        return f"sz{symbol}"
    return f"sh{symbol}"


def _fetch_index(symbol: str, start: str, end: str) -> pd.DataFrame:
    """通过 AKShare 获取指数日K线"""
    import akshare as ak
    ak_symbol = _index_symbol_to_akshare(symbol)
    start_fmt = f"{start[:4]}{start[4:6]}{start[6:]}"
    end_fmt = f"{end[:4]}{end[4:6]}{end[6:]}"
    df = ak.stock_zh_index_daily(symbol=ak_symbol)
    df = df.rename(columns={"date": "date", "open": "open", "close": "close",
                             "high": "high", "low": "low", "volume": "volume"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    # 按日期范围过滤
    df = df.loc[start_fmt:end_fmt]
    core_cols = ["open", "close", "high", "low", "volume"]
    df = df[[c for c in core_cols if c in df.columns]]
    logger.info(f"  {symbol}: fetched {len(df)} rows via AKShare index")
    return df


# ========== 统一入口 ==========

def fetch_daily_kline(symbol: str, days: int = DEFAULT_LOOKBACK_DAYS, stock_type: str | None = None) -> pd.DataFrame:
    """获取日K线数据（前复权），自动识别 A 股/港股/指数

    Args:
        symbol: 股票/指数代码
        days: 回溯天数
        stock_type: "stock" / "index" / None（自动推断）

    注意：盘中获取的当天数据不完整，自动剔除今天的行，只保留已收盘的数据。
    """
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    if is_index(symbol, stock_type):
        df = _fetch_index(symbol, start, end)
    elif is_hk_stock(symbol):
        df = _fetch_hk_stock(symbol, start, end)
    else:
        df = _fetch_a_stock(symbol, start, end)

    # 盘中剔除今天未收盘的数据；收盘后保留（数据已完整）
    now = datetime.now()
    today = pd.Timestamp(now.date())
    close_time = time(16, 10) if is_hk_stock(symbol) else time(15, 5)
    if now.weekday() < 5 and now.time() < close_time:
        df = df[df.index < today]

    return df


def fetch_stock_info(symbol: str, stock_type: str | None = None) -> dict:
    """获取个股基本信息（板块类型）"""
    if is_index(symbol, stock_type):
        return {"symbol": symbol, "board": "index", "market": "A", "type": "index"}

    if is_hk_stock(symbol):
        return {"symbol": symbol, "board": "hk", "market": "HK"}

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
    return {"symbol": symbol, "board": board, "market": "A"}
