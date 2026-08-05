"""JSON 文件缓存的新鲜度判定（issue #41）

同一套 mtime 惯例原先有三份：`data/stock_list.py::_is_cache_valid` 与
`data/sector_list.py::_is_cache_valid`（同名同逻辑的两份）、
`data/sector_cons.py::_is_fresh`（只是 TTL 按天给）。`data/fundamentals.py` 还从
`sector_list` import 了那个私有名来用。

惯例：**以文件 mtime 为缓存时间**，文件不存在算不新鲜。写入即刷新 mtime，所以不需要
额外的元数据文件。
"""

import os
from datetime import datetime, timedelta


def is_fresh(path: str, ttl: timedelta) -> bool:
    """缓存文件存在且 mtime 距今在 ttl 之内"""
    if not os.path.exists(path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return datetime.now() - mtime < ttl


def is_fresh_hours(path: str, hours: float) -> bool:
    return is_fresh(path, timedelta(hours=hours))


def is_fresh_days(path: str, days: float) -> bool:
    return is_fresh(path, timedelta(days=days))


def cache_mtime(path: str) -> datetime | None:
    """缓存文件的写入时间，不存在返回 None"""
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path))
