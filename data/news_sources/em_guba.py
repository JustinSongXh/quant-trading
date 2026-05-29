"""东方财富股吧帖子采集（list 页内嵌 article_list JSON）

参考 zcyeee/EastMoney_Crawler。列表页 HTML 内含 ``var article_list={...};``，
其中 ``re`` 为帖子数组。仅 A 股股吧。external_id 取 post_id，正文不抓（title 即帖子标题）。
"""

import json
import re
from datetime import datetime

import requests

from utils.logger import get_logger

logger = get_logger("news.em_guba")

SOURCE = "em_guba"
_HEADERS = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_LIST_RE = re.compile(r"article_list=(\{.*?\});")


def _parse_time(s: str, ref: datetime) -> datetime | None:
    """解析帖子时间。东财有时省略年份（"MM-DD HH:MM"），按参考时间补全。"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    for fmt in ("%m-%d %H:%M:%S", "%m-%d %H:%M"):
        try:
            d = datetime.strptime(s, fmt).replace(year=ref.year)
            if d > ref:  # 跨年：补成上一年
                d = d.replace(year=ref.year - 1)
            return d
        except ValueError:
            pass
    return None


def fetch(symbol: str, start: datetime, end: datetime, max_pages: int = 3) -> list[dict]:
    """拉取 symbol 股吧帖子，仅返回发布时间落在 [start, end] 内的条目"""
    items = []
    seen = set()
    for page in range(1, max_pages + 1):
        slug = symbol if page == 1 else f"{symbol}_{page}"
        url = f"http://guba.eastmoney.com/list,{slug}.html"
        try:
            resp = requests.get(url, timeout=15, headers=_HEADERS)
            m = _LIST_RE.search(resp.text)
            if not m:
                break
            posts = json.loads(m.group(1)).get("re") or []
        except Exception as e:
            logger.warning(f"  {symbol} em_guba 第 {page} 页拉取失败: {e}")
            break
        if not posts:
            break

        for p in posts:
            pid = p.get("post_id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            pub = _parse_time(
                p.get("post_publish_time") or p.get("post_last_time"), end
            )
            if pub is None or pub < start or pub > end:
                continue
            items.append(
                {
                    "source": SOURCE,
                    "external_id": str(pid),
                    "stock_code": symbol,
                    "title": (p.get("post_title") or "").strip(),
                    "content": "",
                    "published_at": pub,
                }
            )
    return items
