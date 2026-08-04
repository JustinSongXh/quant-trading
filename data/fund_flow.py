"""板块资金流数据层（issue #34）

数据源 `ak.stock_fund_flow_industry` / `ak.stock_fund_flow_concept`（THS）。**一次调用
返回全部板块且已按净额排好序**——聚合在数据源侧就算完了，不需要像板块日K那样逐个板块
打接口（实测行业 90 行、概念 386 行）。

档位只有五档：`即时` / `3日` / `5日` / `10日` / `20日`。**这是硬边界**：
「过去一个月」映射到 `20日`（20 个交易日），「过去 45 天」「7月15日到8月1日」这类区间
**做不到**，不能拿 20 日的数据硬答（前端也写了这句话）。东财那个能给逐日历史的
`stock_sector_fund_flow_hist` 实测连不上（`RemoteDisconnected`），就是 #28 记录的老问题。

两处 akshare 的坑（都实测过，归一化必须处理）：

1. **`即时` 与 `N日排行` 列不同**：`即时` 有 `行业-涨跌幅 / 领涨股 / 领涨股-涨跌幅 /
   当前价`；`N日排行` 没有这些，涨跌幅那列叫 `阶段涨跌幅` 且**是带百分号的字符串**
   （`'13.67%'`），而 `即时` 的 `行业-涨跌幅` 是裸 float（`4.41`）。按列名硬取会在
   换档位时 KeyError，不做解析则 `N日` 档的涨跌幅是字符串、排序按字典序。
2. **概念侧的板块名列也叫 `行业`**（不是 `概念`）。按 `概念` 取会 KeyError。

资金单位是**亿元**，原样保留不换算成元（页面和后续对话层都按亿读更自然），字段名
`inflow / outflow / net` 的单位写在 `SCHEMA_UNITS` 里。
"""

import threading
import time

from datetime import date

# 百分号字符串 → float（'13.67%' → 13.67，单位保留 %），#33 已经有一份，不重复实现
from data.fundamentals import parse_value
from utils.logger import get_logger

logger = get_logger("fund_flow")

LIVE_PERIOD = "即时"
RANK_PERIODS = ("3日", "5日", "10日", "20日")
VALID_PERIODS = (LIVE_PERIOD,) + RANK_PERIODS
VALID_KINDS = ("industry", "concept")

# 落盘积累用的档位（见 snapshot_fund_flow）
SNAPSHOT_PERIODS = (LIVE_PERIOD, "20日")

# 进程内 TTL 缓存，写法同 data/realtime.py::get_sector_snapshot：
# `即时` 盘中分钟级变化，60 秒够用；`N日排行` 是按日聚合，盘中变化有限给 30 分钟。
_TTL_LIVE_SEC = 60
_TTL_RANK_SEC = 30 * 60
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
# Streamlit 每次 rerun 在独立线程执行，缓存读写要串行化（#27 在情绪模型上踩过）
_lock = threading.RLock()

# 板块名列：概念侧实测也叫 `行业`
_COL_SECTOR = "行业"
_COL_INDEX = "行业指数"
_COL_PCT_LIVE = "行业-涨跌幅"     # 即时档，裸 float
_COL_PCT_RANK = "阶段涨跌幅"      # N日档，'13.67%' 字符串
_COL_COUNT = "公司家数"
_COL_LEADER = "领涨股"
_COL_LEADER_PCT = "领涨股-涨跌幅"

SCHEMA_UNITS = {
    "sector": "板块名（THS 口径，与板块日K/实时快照同源）",
    "sector_index": "板块指数点位",
    "change_pct": "涨跌幅 %（即时档=当日，N日档=区间累计）",
    "inflow": "流入资金（亿元）",
    "outflow": "流出资金（亿元）",
    "net": "净额（亿元，流入-流出）",
    "company_count": "成分股家数",
    "leader": "领涨股（仅 即时 档有，N日档为 None）",
    "leader_change_pct": "领涨股涨跌幅 %（仅 即时 档有）",
}


def _amount(raw) -> float | None:
    """资金 / 指数点位列的数值化。

    这里**刻意不复用 `parse_value` 的万/亿后缀换算**：这几列的单位固定是亿元，真出现
    带单位后缀说明接口口径变了，那时给 None（页面显示「—」）比静默放大 1e8 安全得多。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return round(float(raw), 4)
    s = str(raw).strip().replace(",", "")
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def _count(raw) -> int | None:
    v = _amount(raw)
    return None if v is None else int(v)


def _ak_symbol(period: str) -> str:
    """档位 → akshare 的 symbol 参数：`即时` 原样，其余是 `20日排行` 这种拼法。"""
    return LIVE_PERIOD if period == LIVE_PERIOD else f"{period}排行"


def _check_args(kind: str, period: str):
    if kind not in VALID_KINDS:
        raise ValueError(f"kind 只能是 {VALID_KINDS}，收到 {kind!r}")
    if period not in VALID_PERIODS:
        raise ValueError(
            f"period 只有 {'/'.join(VALID_PERIODS)} 五档，收到 {period!r}"
            "——数据源没有任意区间，不能拿 20日 的数据当「过去 45 天」用"
        )


def _ttl_for(period: str) -> int:
    return _TTL_LIVE_SEC if period == LIVE_PERIOD else _TTL_RANK_SEC


def _normalize(row: dict, period: str) -> dict | None:
    """接口一行 → 统一 schema。两种列结构归一化成同一份 key（缺的填 None）。"""
    name = row.get(_COL_SECTOR)
    if name is None or str(name).strip() in ("", "nan"):
        return None
    live = period == LIVE_PERIOD
    return {
        "sector": str(name).strip(),
        "sector_index": _amount(row.get(_COL_INDEX)),
        # 即时档是 float、N日档是 '13.67%' 字符串，parse_value 两种都吃
        "change_pct": parse_value(row.get(_COL_PCT_LIVE if live else _COL_PCT_RANK)),
        "inflow": _amount(row.get("流入资金")),
        "outflow": _amount(row.get("流出资金")),
        "net": _amount(row.get("净额")),
        "company_count": _count(row.get(_COL_COUNT)),
        "leader": (str(row[_COL_LEADER]).strip()
                   if live and row.get(_COL_LEADER) is not None else None),
        "leader_change_pct": parse_value(row.get(_COL_LEADER_PCT)) if live else None,
    }


def _fetch(kind: str, period: str) -> list[dict] | None:
    """真正打接口，不带缓存。失败 → None + warning（不抛给页面）。

    **同名板块去重（保留首次出现）**：akshare 内部是分页抓的（概念侧 8 页、行业侧 2 页），
    盘中翻页期间行情在动，跨页边界的板块会重复出现——实测 2026-08-04 10:20 行业 `即时`
    返回 90 行只有 89 个板块（`建筑材料` 两条：点位 5309.25/5305.02、涨跌 0.5%/0.42%、
    家数与领涨股完全一样，就是同一个板块的两个瞬间），概念 `即时` 337 行只有 320 个板块。
    不去重的话：`net_by_sector` 建 dict 是「后者胜」、快照表主键冲突是「前者胜」，同一份
    数据两条路给出不同的值。同理**板块总数每次调用都会变**（概念侧实测 320~387），
    代码和测试都不要写死条数；分页也可能让某个板块整批缺失，所以热度分 join 不上是
    生产上真会发生的事，不是只存在于测试里的分支。
    """
    import akshare as ak

    fn = (ak.stock_fund_flow_industry if kind == "industry"
          else ak.stock_fund_flow_concept)
    try:
        df = fn(symbol=_ak_symbol(period))
    except Exception as e:
        logger.warning(f"取 {kind} {period} 资金流失败: {type(e).__name__}: {e}")
        return None
    if df is None or df.empty or _COL_SECTOR not in df.columns:
        logger.warning(f"取 {kind} {period} 资金流返回空表或缺 {_COL_SECTOR} 列")
        return None

    rows, seen = [], set()
    dups = []
    for _, r in df.iterrows():
        item = _normalize(r.to_dict(), period)
        if not item:
            continue
        # 同名板块要去重，保留先出现的那条（见函数 docstring 里的分页说明）
        if item["sector"] in seen:
            dups.append(item["sector"])
            continue
        seen.add(item["sector"])
        rows.append(item)
    if dups:
        logger.info(f"{kind} {period} 资金流有 {len(dups)} 个板块重复（分页期间行情在动），"
                    f"保留首次出现的那条：{dups[:5]}{'…' if len(dups) > 5 else ''}")
    if not rows:
        logger.warning(f"取 {kind} {period} 资金流解析后为空")
        return None
    return rows


def sector_fund_flow_ranking(kind: str = "industry", period: str = LIVE_PERIOD,
                             force_refresh: bool = False) -> list[dict] | None:
    """全市场板块资金流排行（接口自带排序，原样保留顺序）。

    Args:
        kind: `industry`（90 个行业）/ `concept`（全部概念，数量随行情变，不要写死）
        period: 五档之一 `即时`/`3日`/`5日`/`10日`/`20日`。**没有任意区间**，
                传别的值直接 ValueError——静默用 20日 顶替是错答案。

    Returns:
        `[{sector, sector_index, change_pct, inflow, outflow, net, company_count,
           leader, leader_change_pct}]`，字段单位见 `SCHEMA_UNITS`；
        取数失败返回 None（不抛给页面）。`即时` 档 60s / `N日` 档 30min 进程内缓存，
        **失败不进缓存**，下次访问会重试。
    """
    _check_args(kind, period)

    key = (kind, period)
    now = time.monotonic()
    if not force_refresh:
        with _lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < _ttl_for(period):
                return hit[1]

    rows = _fetch(kind, period)
    if rows is None:
        return None

    with _lock:
        _cache[key] = (time.monotonic(), rows)
    return rows


def net_by_sector(kind: str, period: str = "20日") -> dict[str, float]:
    """`{板块名: 净额(亿元)}`，供轮动热度分按板块名 join。取数失败返回空 dict。"""
    rows = sector_fund_flow_ranking(kind, period)
    if not rows:
        return {}
    return {r["sector"]: r["net"] for r in rows if r.get("net") is not None}


def snapshot_fund_flow(today: date | None = None) -> int:
    """收盘后把当日 `即时` + `20日` 两档的行业/概念排行落盘，返回新增行数。

    **本 issue 里没有任何功能消费这张表**——写它的唯一理由是：资金流没有逐日历史接口，
    这种数据只能靠时间积累，今天不开始，将来任何时候想用都得再等几个月。

    只在交易日落盘：非交易日 THS 返回的是上一交易日的数据，按今天的日期写进去就是
    重复行（闸门同 data/realtime.py 里给板块 bar 用的那个）。同一
    `(date, kind, period, sector)` 重复写入由主键挡掉，重跑安全。
    """
    from data.cache import upsert_fund_flow_snapshot
    from data.realtime import market_traded_today

    day = today or date.today()
    if not market_traded_today(day):
        logger.info(f"{day} 非交易日（或行情闸门取数失败），跳过资金流快照落盘")
        return 0

    inserted = 0
    for kind in VALID_KINDS:
        for period in SNAPSHOT_PERIODS:
            rows = sector_fund_flow_ranking(kind, period, force_refresh=True)
            if not rows:
                logger.warning(f"{day} {kind} {period} 资金流取数失败，本档跳过")
                continue
            inserted += upsert_fund_flow_snapshot(day, kind, period, rows)
    logger.info(f"{day} 资金流快照落盘 {inserted} 行")
    return inserted
