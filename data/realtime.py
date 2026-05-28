"""实时行情接口（腾讯财经）"""

import requests
from data.fetcher import is_hk_stock
from utils.logger import get_logger

logger = get_logger("realtime")

_QT_URL = "https://qt.gtimg.cn/q="


def _code_to_tencent(code: str, stock_type: str | None = None) -> str:
    """转为腾讯行情代码格式（完整版接口，含 PE/PB 等估值字段）"""
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


def get_realtime_quotes(items: list[tuple[str, str | None]]) -> dict[tuple[str, str], dict]:
    """批量获取实时行情

    Args:
        items: [(code, stock_type)] 列表，stock_type 为 "stock"/"index"/None。
               同一 code 不同 type 可并存（如 000001 既是平安银行也是上证指数）。

    Returns:
        {(code, type): {"name", "price", "change", "change_pct", "pe"}}，type 缺省为 "stock"。
        pe 字段为腾讯返回的市盈率（A股动态/港股 TTM），无值或解析失败时为 None。
        指数也会返回 pe，但口径不明，调用方自行决定是否展示。
    """
    if not items:
        return {}

    norm = [(code, stype or "stock") for code, stype in items]
    tencent_codes = [_code_to_tencent(c, t) for c, t in norm]
    query = ",".join(tencent_codes)

    try:
        resp = requests.get(_QT_URL + query, timeout=5)
        resp.encoding = "gbk"
        text = resp.text
    except Exception as e:
        logger.warning(f"Realtime quote failed: {e}")
        return {}

    results = {}
    for (code, stype), tc in zip(norm, tencent_codes):
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
                        }
                    except (ValueError, IndexError):
                        pass
                break

    return results
