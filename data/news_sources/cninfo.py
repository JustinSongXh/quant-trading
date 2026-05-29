"""巨潮资讯网公告采集（hisAnnouncement/query 接口）

参考 tr1s7an/CnInfoReports。code→orgId 映射来自 cninfo 公开的 szse_stock.json
（含沪深全市场 A 股），进程内缓存一次。column=szse 对沪深均可用。

本期仅采 A 股公告；港股（H 股）公告走 cninfo 的 hke 列、映射表也不同，列入后续。
公告正文为 PDF，本期不下载解析，content 留空，title 即公告标题。
"""

from datetime import datetime, timedelta

import requests

from utils.logger import get_logger

logger = get_logger("news.cninfo")

SOURCE = "cninfo"
_MAP_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_HEADERS = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_org_map: dict | None = None


def _load_org_map() -> dict:
    """加载并缓存 code→orgId 映射"""
    global _org_map
    if _org_map is None:
        try:
            data = requests.get(_MAP_URL, timeout=15, headers=_HEADERS).json()
            _org_map = {x["code"]: x["orgId"] for x in data.get("stockList", [])}
            logger.info(f"  cninfo 股票映射加载完成，共 {len(_org_map)} 只")
        except Exception as e:
            logger.warning(f"  cninfo 股票映射加载失败: {e}")
            _org_map = {}
    return _org_map


def detail_url(stock_code: str, external_id: str, published_at=None) -> str:
    """巨潮公告详情页。orgId 取自进程内缓存的映射表（采集时已加载）"""
    org = _load_org_map().get(stock_code, "")
    date_str = published_at.strftime("%Y-%m-%d") if published_at is not None else ""
    return (
        "http://www.cninfo.com.cn/new/disclosure/detail?"
        f"stockCode={stock_code}&announcementId={external_id}"
        f"&orgId={org}&announcementTime={date_str}"
    )


def fetch(
    symbol: str,
    start: datetime,
    end: datetime,
    page_size: int = 30,
    max_pages: int = 10,
) -> list[dict]:
    """拉取 symbol 在 [start, end] 内的公告"""
    org = _load_org_map().get(symbol)
    if not org:
        return []  # 非 A 股或不在映射表

    se_date = f"{start.strftime('%Y-%m-%d')}~{end.strftime('%Y-%m-%d')}"
    items = []
    for page in range(1, max_pages + 1):
        data = {
            "pageNum": page,
            "pageSize": page_size,
            "column": "szse",
            "tabName": "fulltext",
            "stock": f"{symbol},{org}",
            "seDate": se_date,
        }
        try:
            resp = requests.post(
                _QUERY_URL,
                data=data,
                timeout=15,
                headers={
                    **_HEADERS,
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                },
            )
            payload = resp.json()
        except Exception as e:
            logger.warning(f"  {symbol} cninfo 第 {page} 页拉取失败: {e}")
            break

        anns = payload.get("announcements") or []
        for a in anns:
            ms = a.get("announcementTime")
            aid = a.get("announcementId")
            if not ms or not aid:
                continue
            # announcementTime 是"北京零点"对应的 UTC 瞬时毫秒戳，直接 utcfromtimestamp
            # 会得到前一天 16:00。+8h 还原北京墙钟（公告日期），避免日期漂移、且不受容器 tz 影响。
            pub = datetime.utcfromtimestamp(ms / 1000) + timedelta(hours=8)
            items.append(
                {
                    "source": SOURCE,
                    "external_id": str(aid),
                    "stock_code": symbol,
                    "title": a.get("announcementTitle", "") or "",
                    "content": "",
                    "published_at": pub,
                }
            )
        if len(anns) < page_size:
            break
    return items
