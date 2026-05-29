"""多源新闻/公告/股吧拉取器

每个模块导出：
- ``SOURCE``：源标识（写入 news_items.source）
- ``fetch(symbol, start, end) -> list[dict]``：拉取 [start, end] 内的条目，
  每项含 source / external_id / stock_code / title / content / published_at。
"""
