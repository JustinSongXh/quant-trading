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
    conn = duckdb.connect(CACHE_DB_PATH)
    table_name = f"kline_{symbol}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table_name} AS SELECT * FROM df WHERE 1=0")
    conn.execute(f"DELETE FROM {table_name}")
    conn.execute(f"INSERT INTO {table_name} SELECT * FROM df")
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
        df = df.set_index("date").sort_index()
        return df
    except duckdb.CatalogException:
        conn.close()
        return None
