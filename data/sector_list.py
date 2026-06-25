"""东财行业/概念板块列表获取与缓存（供管理页板块选择器）"""

import os
import json
from datetime import datetime, timedelta
from utils.logger import get_logger

logger = get_logger("sector_list")

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
_SECTOR_CACHE = os.path.join(_CACHE_DIR, "sectors.json")
_CACHE_MAX_AGE_HOURS = 24


def _is_cache_valid(path: str) -> bool:
    if not os.path.exists(path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return datetime.now() - mtime < timedelta(hours=_CACHE_MAX_AGE_HOURS)


def _fetch_sectors() -> list[dict]:
    """拉取行业 + 概念全部板块 → [{code, name, kind, market}]（数据源：同花顺 THS）

    THS 名称列表列为 name/code，走 10jqka 域名，独立于常被限流的东财 push2。
    """
    import akshare as ak
    out = []
    for kind, fn in (("industry", ak.stock_board_industry_name_ths),
                     ("concept", ak.stock_board_concept_name_ths)):
        try:
            df = fn()
            for _, r in df.iterrows():
                out.append({
                    "code": str(r["code"]),
                    "name": str(r["name"]),
                    "kind": kind,
                    "market": "A",
                })
        except Exception as e:
            logger.warning(f"Fetch {kind} sectors failed: {e}")
    logger.info(f"Fetched {len(out)} sectors (industry + concept)")
    return out


def get_sector_list(force_refresh: bool = False) -> list[dict]:
    """获取全部板块列表，优先读 24h 缓存。"""
    if not force_refresh and _is_cache_valid(_SECTOR_CACHE):
        with open(_SECTOR_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)

    sectors = _fetch_sectors()
    if sectors:  # 拉取失败（东财抽风）时不覆盖旧缓存
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_SECTOR_CACHE, "w", encoding="utf-8") as f:
            json.dump(sectors, f, ensure_ascii=False, indent=2)
    elif os.path.exists(_SECTOR_CACHE):
        with open(_SECTOR_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    return sectors


def sector_list_available() -> bool:
    """是否已有可用的板块列表缓存（供 UI 判断要不要提示加载）。"""
    return os.path.exists(_SECTOR_CACHE)


def search_sectors(keyword: str) -> list[dict]:
    """按名称或代码模糊搜索板块，最多返回 50 条。"""
    keyword = keyword.strip().upper()
    if not keyword:
        return []
    results = []
    for s in get_sector_list():
        if keyword in s["name"].upper() or keyword in s["code"].upper():
            results.append(s)
    return results[:50]
