"""DuckDB 本地数据缓存"""

import os
import duckdb
import pandas as pd
from config.settings import CACHE_DB_PATH


def _ensure_dir():
    os.makedirs(os.path.dirname(CACHE_DB_PATH), exist_ok=True)


def save_kline(symbol: str, df: pd.DataFrame):
    """将K线数据缓存到 DuckDB"""
    _ensure_dir()
    # 把 DatetimeIndex reset 成普通列再存，避免 DuckDB 丢失日期信息
    save_df = df.copy()
    if save_df.index.name == "date":
        save_df = save_df.reset_index()
    save_df["date"] = save_df["date"].astype(str)

    conn = duckdb.connect(CACHE_DB_PATH)
    table_name = f"kline_{symbol}"
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM save_df")
    conn.close()


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
