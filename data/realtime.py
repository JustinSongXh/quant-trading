"""实时行情接口（腾讯财经）"""

import requests
from data.fetcher import is_hk_stock
from utils.logger import get_logger

logger = get_logger("realtime")

_QT_URL = "https://qt.gtimg.cn/q="


def _code_to_tencent(code: str, stock_type: str | None = None) -> str:
    """转为腾讯行情代码格式"""
    if is_hk_stock(code):
        return f"s_hk{code}"
    if stock_type == "index":
        # 指数：000xxx -> s_sh000xxx, 399xxx -> s_sz399xxx
        if code.startswith("3"):
            return f"s_sz{code}"
        return f"s_sh{code}"
    if code.startswith(("6", "9")):
        return f"s_sh{code}"
    return f"s_sz{code}"


def get_realtime_quotes(codes: list[str], type_map: dict[str, str] | None = None) -> dict[str, dict]:
    """批量获取实时行情

    Args:
        codes: 股票代码列表，如 ["600519", "00700"]
        type_map: {code: type} 映射，用于区分同代码的指数和个股

    Returns:
        {code: {"name", "price", "change", "change_pct", "volume"}}
    """
    if not codes:
        return {}

    type_map = type_map or {}
    tencent_codes = [_code_to_tencent(c, type_map.get(c)) for c in codes]
    query = ",".join(tencent_codes)

    try:
        resp = requests.get(_QT_URL + query, timeout=5)
        resp.encoding = "gbk"
        text = resp.text
    except Exception as e:
        logger.warning(f"Realtime quote failed: {e}")
        return {}

    results = {}
    for code, tc in zip(codes, tencent_codes):
        key = f"v_{tc}"
        for line in text.split("\n"):
            if key in line:
                # v_s_sh600519="1~贵州茅台~600519~1342.25~-1.84~-0.14~48082~646834~~16808.60~GP-A~";
                parts = line.split("~")
                if len(parts) >= 6:
                    try:
                        results[code] = {
                            "name": parts[1],
                            "price": float(parts[3]),
                            "change": float(parts[4]),
                            "change_pct": float(parts[5]),
                        }
                    except (ValueError, IndexError):
                        pass
                break

    return results
