"""DuckDB 本地数据缓存（带时效校验）"""

import os
import json
import duckdb
import pandas as pd
from datetime import datetime, time
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


def _save_meta(symbol: str):
    """记录缓存写入时间"""
    _ensure_dir()
    with open(_meta_path(symbol), "w") as f:
        json.dump({"updated_at": datetime.now().isoformat()}, f)


def _load_meta(symbol: str) -> datetime | None:
    """读取缓存写入时间"""
    path = _meta_path(symbol)
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        data = json.load(f)
    return datetime.fromisoformat(data["updated_at"])


def is_cache_fresh(symbol: str) -> bool:
    """判断缓存是否为最新收盘数据

    规则：
    - A 股：当天 15:00 后写入的才有效
    - 港股：当天 16:00 后写入的才有效
    - 非交易日：上一个交易日收盘后的缓存有效
    """
    updated_at = _load_meta(symbol)
    if updated_at is None:
        return False

    now = datetime.now()
    close_time = _CLOSE_TIME_HK if is_hk_stock(symbol) else _CLOSE_TIME_A

    # 今天是否交易日（简单判断：周一到周五）
    today_is_trading_day = now.weekday() < 5

    if today_is_trading_day:
        if now.time() >= close_time:
            # 已收盘：缓存需要是今天收盘后写入的
            today_close = datetime.combine(now.date(), close_time)
            return updated_at >= today_close
        else:
            # 盘中：上一个交易日收盘后的缓存就够了（盘中不强制刷新）
            # 但如果缓存是今天盘中写入的，标记为不新鲜（下次会刷新）
            return updated_at.date() < now.date() or updated_at.time() >= close_time
    else:
        # 周末/节假日：只要不是太久以前的就行（3天内）
        age = now - updated_at
        return age.total_seconds() < 3 * 24 * 3600


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

    conn = duckdb.connect(CACHE_DB_PATH)
    table_name = f"kline_{symbol}"
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM save_df")
    conn.close()

    _save_meta(symbol)


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
