"""东方财富个股新闻采集（search-api jsonp 接口）

akshare 1.18 的 stock_news_em 因 pandas pyarrow 字符串后端不支持 \\u 转义正则而崩溃，
这里直连其底层同一接口自行解析，规避该 bug。

一条新闻可能同时命中多只股票（按代码作关键词检索），不同股票各落一行，
external_id 取新闻详情页 URL。
"""

import json
from datetime import datetime

from curl_cffi import requests as creq

from utils.logger import get_logger

logger = get_logger("news.em_news")

SOURCE = "em_news"
_URL = "https://search-api-web.eastmoney.com/search/jsonp"


def detail_url(stock_code: str, external_id: str, published_at=None) -> str:
    """external_id 即新闻详情页 URL，直接返回"""
    return external_id


def _clean(s: str) -> str:
    """去掉高亮标签、全角空格、换行"""
    if not s:
        return ""
    for token in ("<em>", "</em>", "　"):
        s = s.replace(token, "")
    return s.replace("\r\n", " ").replace("\n", " ").strip()


def fetch(symbol: str, start: datetime, end: datetime, page_size: int = 50) -> list[dict]:
    """拉取 symbol 相关新闻，仅返回 published_at 落在 [start, end] 内的条目"""
    inner = {
        "uid": "",
        "keyword": symbol,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": page_size,
                "preTag": "<em>",
                "postTag": "</em>",
            }
        },
    }
    params = {
        "cb": "jQuery_1",
        "param": json.dumps(inner, ensure_ascii=False),  # 保留中文
        "_": "1",
    }
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "referer": f"https://so.eastmoney.com/news/s?keyword={symbol}",
    }
    try:
        resp = creq.get(_URL, params=params, headers=headers, timeout=15)
        text = resp.text
        payload = json.loads(text[text.index("(") + 1 : -1])
        arr = payload.get("result", {}).get("cmsArticleWebOld") or []
    except Exception as e:
        logger.warning(f"  {symbol} em_news 拉取失败: {e}")
        return []

    items = []
    for a in arr:
        ts = a.get("date")
        try:
            pub = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if pub < start or pub > end:
            continue
        url = a.get("url") or ""
        if not url:
            continue
        items.append(
            {
                "source": SOURCE,
                "external_id": url,
                "stock_code": symbol,
                "title": _clean(a.get("title", "")),
                "content": _clean(a.get("content", "")),
                "published_at": pub,
            }
        )
    return items
