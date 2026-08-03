"""实时行情接口

个股/指数走腾讯财经（一次请求批量取），板块走同花顺（THS）实时快照。

板块为什么不能跟着走腾讯：板块代码没有固定规律，概念板块的 3xxxxx 段与创业板股票
号直接相撞——`300008`（新能源汽车概念）拼出来是 `sz300008`，那是天海防务；`300733`
（锂电池概念）是西菱动力。腾讯照样返回数据，key 还是 sector 的，肉眼看不出错（#32）。
"""

import threading
import time

import requests
from data.fetcher import is_hk_stock
from utils.logger import get_logger

logger = get_logger("realtime")

_QT_URL = "https://qt.gtimg.cn/q="

# 板块快照 TTL：THS 的板块 info 接口只能逐个板块调，股票池里十来个板块就是十来次
# 请求。总览页每次重扫都要现价，60 秒内复用同一份快照足够（盘中分钟级粒度）。
_SECTOR_TTL_SEC = 60
_sector_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
# Streamlit 每次 rerun 在独立线程执行，缓存读写要串行化（#27 在情绪模型上踩过）
_sector_lock = threading.RLock()


def _code_to_tencent(code: str, stock_type: str | None = None) -> str:
    """转为腾讯行情代码格式（完整版接口，含 PE/PB 等估值字段）"""
    if stock_type == "sector":
        # 板块永远不该走到这里：拼出来的代码会撞上真实股票并静默返回错值。
        # 分流在 get_realtime_quotes 里做，漏了要当场炸而不是悄悄给错数据。
        raise ValueError(
            f"板块 {code} 不能转腾讯代码：板块走 THS 快照（get_sector_snapshot）"
        )
    if is_hk_stock(code):
        return f"hk{code}"
    if stock_type == "index":
        # 指数：000xxx -> sh000xxx, 399xxx -> sz399xxx
        if code.startswith("3"):
            return f"sz{code}"
        return f"sh{code}"
    if code.startswith(("6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _safe_float(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _parse_quote_date(s):
    """腾讯行情时间字段（parts[30]，形如 20260608161427）→ 'YYYY-MM-DD'，失败返回 None"""
    if s and len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def market_traded_today(today_date) -> bool:
    """今天 A 股是否已开盘（交易日闸门）。

    THS 板块快照没有日期字段，工作日节假日会返回上一交易日的数据，误当今日会造出
    幻影 bar 污染日K/回测。用腾讯上证指数（000001）快照的交易日期做闸门：仅当其
    date==今日（今天确实开过盘）才算数。取价失败按"未开盘"处理（宁缺勿错）。

    只有**写进日K**才需要这道闸门；展示层不需要——非交易日的快照就是上一交易日的
    真实收盘，正是要显示的东西，与个股周末的表现一致。
    """
    q = get_realtime_quotes([("000001", "index")]).get(("000001", "index"))
    return bool(q) and q.get("date") == today_date.isoformat()


# ========== 板块实时快照（同花顺） ==========

def get_sector_snapshot(name: str, kind: str) -> dict | None:
    """THS 板块实时快照。name 为板块中文名，kind 为 industry/concept。

    数据源：行业 stock_board_industry_info_ths / 概念 stock_board_concept_info_ths，
    返回「项目/值」表，含 今开/昨收/最高/最低/成交量(万手)/板块涨幅。点位与板块指数
    EOD 同标度（实测快照「昨收」与日K上一根收盘价一致到小数点后两位）。

    概念侧没有一次性带涨跌幅的接口——stock_board_concept_summary_ths 只有驱动事件/
    龙头股/成分股数量；行业侧 summary 有涨跌幅但没点位。所以两侧都逐个 info。

    Returns:
        {"price", "prev_close", "change_pct", "open", "high", "low", "volume"}，
        取数失败或字段缺失返回 None（调用方回落到日K口径）。结果带 60 秒 TTL 缓存。
    """
    cache_key = (name, kind)
    now = time.monotonic()
    with _sector_lock:
        hit = _sector_cache.get(cache_key)
        if hit and now - hit[0] < _SECTOR_TTL_SEC:
            return hit[1]

    snap = _fetch_sector_snapshot(name, kind)

    with _sector_lock:
        _sector_cache[cache_key] = (time.monotonic(), snap)
    return snap


def _fetch_sector_snapshot(name: str, kind: str) -> dict | None:
    """真正打 THS 接口，不带缓存"""
    import akshare as ak

    try:
        fn = (ak.stock_board_industry_info_ths if kind == "industry"
              else ak.stock_board_concept_info_ths)
        raw = fn(symbol=name)
        kv = dict(zip(raw["项目"], raw["值"]))
    except Exception as e:
        logger.warning(f"THS 板块实时快照失败 {name}({kind}): {type(e).__name__}: {e}")
        return None

    prev_close = _safe_float(kv.get("昨收"))
    pct = _safe_float(str(kv.get("板块涨幅", "")).replace("%", ""))
    if prev_close is None or pct is None:
        logger.warning(f"THS 板块实时快照缺字段 {name}({kind}): {kv}")
        return None

    price = round(prev_close * (1 + pct / 100), 3)
    vol_wan = _safe_float(kv.get("成交量(万手)"))
    return {
        "price": price,
        "prev_close": prev_close,
        "change_pct": pct,
        "open": _safe_float(kv.get("今开")) or price,
        "high": _safe_float(kv.get("最高")) or price,
        "low": _safe_float(kv.get("最低")) or price,
        "volume": round(vol_wan * 10000) if vol_wan is not None else 0,
    }


def _sector_quotes(items: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """板块项 → 与腾讯同构的 quote dict。名称/类型从股票池配置取。"""
    from config.settings import get_sector_info
    from datetime import date

    if not items:
        return {}

    # 闸门只问一次：所有板块共用同一个「今天开没开盘」的答案
    today = date.today()
    traded = market_traded_today(today)

    out = {}
    for code, stype in items:
        info = get_sector_info(code)
        if not info:
            logger.warning(f"板块 {code} 缺少名称/类型配置，跳过实时快照")
            continue
        name, kind = info
        snap = get_sector_snapshot(name, kind)
        if not snap:
            continue
        out[(code, stype)] = {
            "name": name,
            "price": snap["price"],
            "change": round(snap["price"] - snap["prev_close"], 3),
            "change_pct": snap["change_pct"],
            "prev_close": snap["prev_close"],
            "pe": None,                             # 板块无 PE
            "date": today.isoformat() if traded else None,
            "open": snap["open"],
            "high": snap["high"],
            "low": snap["low"],
            "volume": snap["volume"],
        }
    return out


def get_realtime_quotes(items: list[tuple[str, str | None]]) -> dict[tuple[str, str], dict]:
    """批量获取实时行情

    Args:
        items: [(code, stock_type)] 列表，stock_type 为 "stock"/"index"/"sector"/None。
               同一 code 不同 type 可并存（如 000001 既是平安银行也是上证指数）。
               混合列表按 type 自动分流：sector 走 THS 快照，其余走腾讯批量。

    Returns:
        {(code, type): {"name", "price", "change", "change_pct", "pe",
                        "date", "open", "high", "low", "volume"}}，type 缺省为 "stock"。
        pe 字段为腾讯返回的市盈率（A股动态/港股 TTM），无值或解析失败时为 None；
        板块恒为 None。
        date 为该快照的交易日（'YYYY-MM-DD'）；非交易日腾讯会返回上一交易日快照，
        调用方需用 date 校验是否为当日，避免把旧快照误当今日数据。板块快照本身没有
        日期字段，date 填的是 market_traded_today() 的闸门结果（今日 / None）。
        板块另有 prev_close（快照自带的昨收），个股/指数没有这个字段。
        指数也会返回 pe，但口径不明，调用方自行决定是否展示。
        取不到的项不出现在返回字典里，调用方需按 key 缺失处理。
    """
    if not items:
        return {}

    norm = [(code, stype or "stock") for code, stype in items]
    sectors = [(c, t) for c, t in norm if t == "sector"]
    others = [(c, t) for c, t in norm if t != "sector"]

    results = _sector_quotes(sectors)
    if not others:
        return results

    tencent_codes = [_code_to_tencent(c, t) for c, t in others]
    query = ",".join(tencent_codes)

    try:
        resp = requests.get(_QT_URL + query, timeout=5)
        resp.encoding = "gbk"
        text = resp.text
    except Exception as e:
        logger.warning(f"Realtime quote failed: {e}")
        return results          # 腾讯挂了不连累已取到的板块快照

    # 注意配 others 而不是 norm——板块已经在上面处理过，不在 tencent_codes 里
    for (code, stype), tc in zip(others, tencent_codes):
        key = f"v_{tc}"
        for line in text.split("\n"):
            if key in line:
                # 完整版字段（~ 分割）：1=name 3=price 31=change 32=change_pct 39=PE
                parts = line.split("~")
                if len(parts) >= 33:
                    try:
                        results[(code, stype)] = {
                            "name": parts[1],
                            "price": float(parts[3]),
                            "change": float(parts[31]),
                            "change_pct": float(parts[32]),
                            "pe": _safe_float(parts[39]) if len(parts) > 39 else None,
                            # 当日 K 线补全用：日期 + OHLCV（最高/最低在更靠后字段）
                            "date": _parse_quote_date(parts[30]) if len(parts) > 30 else None,
                            "open": _safe_float(parts[5]),
                            "high": _safe_float(parts[33]) if len(parts) > 33 else None,
                            "low": _safe_float(parts[34]) if len(parts) > 34 else None,
                            "volume": _safe_float(parts[6]),
                        }
                    except (ValueError, IndexError):
                        pass
                break

    return results
