"""DuckDB 本地数据缓存（带时效校验）"""

import os
import json
import duckdb
import pandas as pd
from datetime import datetime, time, date, timedelta
from config.settings import CACHE_DB_PATH
from data.fetcher import is_hk_stock

_META_DIR = os.path.join(os.path.dirname(CACHE_DB_PATH), "meta")

# A 股收盘 15:00，港股收盘 16:00
_CLOSE_TIME_A = time(15, 0)
_CLOSE_TIME_HK = time(16, 0)


def _ensure_dir():
    os.makedirs(os.path.dirname(CACHE_DB_PATH), exist_ok=True)
    os.makedirs(_META_DIR, exist_ok=True)


def _meta_path(symbol: str) -> str:
    return os.path.join(_META_DIR, f"{symbol}.json")


def _save_meta(symbol: str, last_bar_date: str | None):
    """记录缓存写入时间与缓存中最新一根 K 线的日期"""
    _ensure_dir()
    payload = {"updated_at": datetime.now().isoformat()}
    if last_bar_date:
        payload["last_bar_date"] = last_bar_date
    with open(_meta_path(symbol), "w") as f:
        json.dump(payload, f)


def _load_meta(symbol: str) -> dict | None:
    """读取 meta（updated_at + last_bar_date）"""
    path = _meta_path(symbol)
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def _prev_trading_day(d: date) -> date:
    """上一个交易日（仅按周末判断，不含节假日）"""
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _expected_last_bar_date(symbol: str) -> date:
    """缓存里最新的 K 线日期满足 >= 此值才算够新

    - 今天是交易日且已收盘：期望今天
    - 今天是交易日但盘中：期望上一交易日（盘中不强制刷新）
    - 今天非交易日：期望上一交易日
    """
    now = datetime.now()
    close_time = _CLOSE_TIME_HK if is_hk_stock(symbol) else _CLOSE_TIME_A
    today = now.date()
    if today.weekday() < 5 and now.time() >= close_time:
        return today
    return _prev_trading_day(today)


def is_cache_fresh(symbol: str) -> bool:
    """判断缓存是否覆盖到最新应有的交易日

    依据缓存数据本身的最后一根 K 线日期判断，而不是 meta 的写入时间——
    避免在 T-1 盘中（彼时 T-1 当日 K 线还没出来）抓到的旧数据被一直当成"新鲜"。
    """
    meta = _load_meta(symbol)
    if not meta or "last_bar_date" not in meta:
        return False
    try:
        last_bar = date.fromisoformat(meta["last_bar_date"][:10])
    except ValueError:
        return False
    return last_bar >= _expected_last_bar_date(symbol)


def is_during_trading(symbol: str) -> bool:
    """当前是否在交易时间内"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    close_time = _CLOSE_TIME_HK if is_hk_stock(symbol) else _CLOSE_TIME_A
    open_time = time(9, 30)
    return open_time <= now.time() <= close_time


def save_kline(symbol: str, df: pd.DataFrame):
    """将K线数据缓存到 DuckDB"""
    _ensure_dir()
    save_df = df.copy()
    if save_df.index.name == "date":
        save_df = save_df.reset_index()
    save_df["date"] = save_df["date"].astype(str)

    last_bar_date = max(save_df["date"])[:10] if not save_df.empty else None

    conn = duckdb.connect(CACHE_DB_PATH)
    table_name = f"kline_{symbol}"
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM save_df")
    conn.close()

    _save_meta(symbol, last_bar_date)


def load_kline(symbol: str) -> pd.DataFrame | None:
    """从缓存加载K线数据"""
    _ensure_dir()
    conn = duckdb.connect(CACHE_DB_PATH)
    table_name = f"kline_{symbol}"
    try:
        df = conn.execute(f"SELECT * FROM {table_name}").fetchdf()
        conn.close()
        if df.empty:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        return df
    except duckdb.CatalogException:
        conn.close()
        return None
