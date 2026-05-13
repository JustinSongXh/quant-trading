"""全市场股票列表获取与缓存"""

import os
import json
from datetime import datetime, timedelta
from utils.logger import get_logger

logger = get_logger("stock_list")

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
_A_STOCK_CACHE = os.path.join(_CACHE_DIR, "a_stocks.json")
_HK_STOCK_CACHE = os.path.join(_CACHE_DIR, "hk_stocks.json")
_CACHE_MAX_AGE_HOURS = 24


def _ensure_dir():
    os.makedirs(_CACHE_DIR, exist_ok=True)


def _is_cache_valid(path: str) -> bool:
    """缓存文件存在且不超过 24 小时"""
    if not os.path.exists(path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return datetime.now() - mtime < timedelta(hours=_CACHE_MAX_AGE_HOURS)


def _load_cache(path: str) -> list[dict] | None:
    if _is_cache_valid(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_cache(path: str, data: list[dict]):
    _ensure_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ========== A 股 ==========

def _fetch_a_stocks_from_baostock() -> list[dict]:
    """从 baostock 获取全量 A 股列表"""
    import baostock as bs
    bs.login()
    rs = bs.query_stock_basic()
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    bs.logout()

    stocks = []
    for r in rows:
        code_full, name, ipo_date, out_date, _, status = r
        if status != "1":
            continue
        if out_date:  # 已退市
            continue
        code = code_full[3:]  # 去掉 sh./sz.
        prefix = code[:3]
        if prefix in ("600", "601", "603", "605", "000", "001", "002", "003", "300", "688"):
            stocks.append({"code": code, "name": name, "market": "A"})

    logger.info(f"Fetched {len(stocks)} active A-stocks from baostock")
    return stocks


def get_a_stock_list(force_refresh: bool = False) -> list[dict]:
    """获取 A 股全量列表，优先读缓存"""
    if not force_refresh:
        cached = _load_cache(_A_STOCK_CACHE)
        if cached:
            return cached

    stocks = _fetch_a_stocks_from_baostock()
    _save_cache(_A_STOCK_CACHE, stocks)
    return stocks


# ========== 港股 ==========

# 港股热门列表（恒生指数成分股 + 热门科技股）
_HK_BUILTIN = [
    {"code": "00700", "name": "腾讯控股"},
    {"code": "09988", "name": "阿里巴巴-W"},
    {"code": "03690", "name": "美团-W"},
    {"code": "01810", "name": "小米集团-W"},
    {"code": "09888", "name": "百度集团-SW"},
    {"code": "09618", "name": "京东集团-SW"},
    {"code": "01024", "name": "快手-W"},
    {"code": "09961", "name": "携程集团-S"},
    {"code": "03888", "name": "金山软件"},
    {"code": "09626", "name": "哔哩哔哩-SW"},
    {"code": "00005", "name": "汇丰控股"},
    {"code": "00941", "name": "中国移动"},
    {"code": "00388", "name": "香港交易所"},
    {"code": "02318", "name": "中国平安"},
    {"code": "01299", "name": "友邦保险"},
    {"code": "00883", "name": "中国海洋石油"},
    {"code": "02269", "name": "药明生物"},
    {"code": "01211", "name": "比亚迪股份"},
    {"code": "02020", "name": "安踏体育"},
    {"code": "00027", "name": "银河娱乐"},
    {"code": "00175", "name": "吉利汽车"},
    {"code": "06098", "name": "碧桂园服务"},
    {"code": "00772", "name": "阅文集团"},
    {"code": "06618", "name": "京东健康"},
    {"code": "02015", "name": "理想汽车-W"},
    {"code": "09866", "name": "蔚来-SW"},
    {"code": "09868", "name": "小鹏汽车-W"},
    {"code": "00981", "name": "中芯国际"},
    {"code": "02382", "name": "舜宇光学科技"},
    {"code": "00241", "name": "阿里健康"},
]


def get_hk_stock_list(force_refresh: bool = False) -> list[dict]:
    """获取港股列表，优先读缓存"""
    if not force_refresh:
        cached = _load_cache(_HK_STOCK_CACHE)
        if cached:
            return cached

    # 当前使用内置列表，后续可扩展从接口获取
    stocks = [{"code": s["code"], "name": s["name"], "market": "HK"} for s in _HK_BUILTIN]
    _save_cache(_HK_STOCK_CACHE, stocks)
    logger.info(f"Loaded {len(stocks)} HK stocks (builtin list)")
    return stocks


# ========== 统一入口 ==========

def get_all_stocks(force_refresh: bool = False) -> list[dict]:
    """获取全市场股票列表（A股 + 港股）"""
    a_stocks = get_a_stock_list(force_refresh)
    hk_stocks = get_hk_stock_list(force_refresh)
    return a_stocks + hk_stocks


def search_stocks(keyword: str, market: str | None = None) -> list[dict]:
    """搜索股票，支持代码或名称模糊匹配

    Args:
        keyword: 搜索关键词（代码或名称）
        market: 限定市场，"A" / "HK" / None(全部)
    """
    all_stocks = get_all_stocks()
    keyword = keyword.strip().upper()

    results = []
    for s in all_stocks:
        if market and s.get("market") != market:
            continue
        if keyword in s["code"].upper() or keyword in s["name"].upper():
            results.append(s)

    return results[:50]  # 最多返回 50 条


def refresh_stock_lists():
    """强制刷新全市场列表（供定时任务调用）"""
    logger.info("Refreshing stock lists...")
    a = get_a_stock_list(force_refresh=True)
    hk = get_hk_stock_list(force_refresh=True)
    logger.info(f"Refresh done: {len(a)} A-stocks, {len(hk)} HK-stocks")
