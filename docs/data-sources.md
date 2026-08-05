# 数据来源与缓存

一条铁律：**取数与缓存只在 `data/` 里实现**。`analysis/` 要数据就调 `data/*`，不自己写新鲜度判断与回落策略（这套逻辑曾经在四处各抄一遍，见 issue #41）。

## 源 → 模块

| 数据 | 源 | 模块 | 说明 |
|---|---|---|---|
| A 股日K | AKShare → baostock | `data/fetcher.py` | AKShare 优先，失败兜底 baostock |
| 港股日K/行情 | 腾讯 | `data/fetcher.py` | |
| 个股/指数实时快照 | 腾讯 `qt.gtimg.cn` | `data/realtime.py` | **可批量**，几十个一次约 0.2s |
| 板块实时快照 | 同花顺（经 AKShare） | `data/realtime.py` | **只能逐个**，约 0.74s/个，带 60 秒内存 TTL |
| 板块指数日K | 同花顺 | `data/fetcher.py` | |
| 板块清单（行业/概念） | 同花顺 | `data/sector_list.py` | 走 10jqka 域名，独立于常被限流的东财 push2 |
| 财报摘要 | 同花顺 | `data/fundamentals.py` | 只有 A 股个股 |
| 板块资金流 | 同花顺 | `data/fund_flow.py` | 全市场一次返回且已排序；五档：即时 / 3日 / 5日 / 10日 / 20日 |
| 板块成分股 | 东方财富 | `data/sector_cons.py` | 手写 HTTP，见下面的限流一节 |
| 个股新闻 | 东方财富 | `data/news_sources/em_news.py` | |
| 股吧帖子 | 东方财富 | `data/news_sources/em_guba.py` | |
| 公告 | 巨潮资讯 | `data/news_sources/cninfo.py` | |

## 缓存

两种介质，各有明确分工：

**DuckDB**（`data/cache/market.duckdb`，`data/cache.py`）
- 日K：一个标的一张表，cache key 带类型前缀（`idx_` 指数 / `sec_` 板块 / 裸代码个股）。取数编排统一在 `data/kline.py::get_kline`：缓存新鲜且**日历天跨度**够就用缓存，否则取数落盘，失败回落任意已有缓存（哪怕过期），全空返回空表。
- 新闻/公告/股吧：`news_items` 表，含情绪分字段。
- 板块资金流快照：`sector_fund_flow_snapshot` 表，每交易日 15:40 落一份（资金流没有逐日历史接口，只能自己攒）。

**JSON 文件**（`data/cache/`，TTL 判定统一在 `data/file_cache.py`，以文件 mtime 为缓存时间）

| 内容 | TTL | 常量 |
|---|---|---|
| 全市场股票清单 | 24h | `data/stock_list.py::_CACHE_MAX_AGE_HOURS` |
| 板块清单 | 24h | `data/sector_list.py::_CACHE_MAX_AGE_HOURS` |
| 财报摘要（每股一份） | 24h | `data/fundamentals.py::_CACHE_MAX_AGE_HOURS` |
| 东财板块名录（名称→BK 代码） | 30 天 | `data/sector_cons.py::BOARD_MAP_TTL_DAYS`（板块增删是季度级） |
| 板块成分股 | 7 天 | `data/sector_cons.py::CONS_TTL_DAYS`（成分调整是月度级） |
| 新闻保留窗口 | 过去 10 个交易日 | `data/news.py::DEFAULT_WINDOW` |

日K新鲜度不是简单的"多久没取"，而是围绕**交易日与盘中/收盘口径**判定的（见 `data/cache.py::is_cache_fresh` / `is_during_trading`）——这块反复踩过坑，改动前先看 issue #11 → #5 → #26 那一串。

## 限流现状

**东方财富的成分股接口是窗口性限流**（2026-08-04 实测）：同一分钟内 22 个 `(分片, 协议)` 组合只有 2 个返回 200，其余当场断连；两分钟后全挂，再过 40 秒又活。因此这一层的写法是：

- 分片 × 协议两个维度都轮换，并记住上次可用的组合优先试；
- 翻页（接口 `pz` 硬上限 100，`data.total` 给真实条数）；任一页最终失败 → 整批作废（半截名录会让名称匹配莫名失败）；
- 交互路径只轮一圈就返回（不让页面为限流卡几十秒），夜间 job 用更多重试轮数；
- **缓存不是优化而是前提**：取不到就用过期缓存并标 `stale` + 缓存日期。因此每晚 03:20 有一个 job 预热池内板块的成分股。

**板块没有批量行情接口**（issue #32 的结论）：逐个快照约 0.74s/个，涉及大量板块的功能必须把这个代价摆到用户面前，而不是让人干等。

**AKShare 是事实上的单点**：板块日K、板块快照、财报、资金流、板块清单全走它包装的同花顺接口。它一次接口改名能同时打掉多个功能，且只在运行时才发现。

## 新增取数时

1. 放在 `data/` 下，日K一律经 `get_kline`，JSON 缓存一律经 `file_cache`；
2. 返回值带 `fetched_at` 与 `stale`，并一路传到页面显示——"过期的数据远好过没有"的前提是用户知道它过期了；
3. 只算了一部分就同时返回总数与实算数，不许静默截断；
4. 不要自己发明重试，照 `data/sector_cons.py` 的既有写法；
5. 取数函数**按模块引用**（`from data import fetcher` 然后 `fetcher.fetch_daily_kline(...)`），否则测试替不掉——理由见 [CLAUDE.md](../CLAUDE.md) 第 3 节。
