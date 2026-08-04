"""对话助手的工具集（issue #36）

**这一片不新增任何计算**：每个工具都只是 #33/#34/#35 与既有取数层的薄包装。模型只做
意图路由、跨源综合、追问；排序/聚合/加权一律由这里调的 Python 函数完成。

## 为什么工具定义是 provider 无关的纯数据

issue 给的示例用 SDK 的 `@tool` 装饰器，但同一份工具集还要给**纯 HTTP**（OpenAI 兼容
function calling）的 provider 用——被 SDK 装饰过的对象喂不进 HTTP 请求体。所以这里只声明
`ToolSpec(name, description, input_schema, handler)` 这种纯数据 + 同步函数，由各 provider
自己适配：`providers/claude_sdk.py` 把它们包成 `tool()` + `create_sdk_mcp_server()`，
`providers/_template.py` 把同一份 spec 转成 OpenAI 的 `tools=[...]`。抽象是不是真的，
就看这两条路能不能共用一份工具定义。

## description 与 input_schema 是这一片的质量核心

模型**靠 description 选工具、靠 input_schema 传参**。两条硬要求（测试守着）：

1. **边界写进 enum，不止写在文字里。** 资金流只有五个固定档位，写成 enum 后模型在
   schema 层就传不出 `"45日"`；只在文字里叮嘱是会被绕过的。
2. **每个 description 都要说清「能给什么 / 代价多大 / 边界在哪」。** 尤其 C 类要写明
   「慢」和「只覆盖 top N」，否则模型会把「算了 20 只」说成「板块全扫了一遍」。

## 三类数据，三种对待

- **A 类**（数据源侧已算好的榜单）：资金流排行一次调用返回全部板块且已排序，对话里现取。
- **B 类**（单标的明细）：命中缓存就是本地读（日K 在 DuckDB、财报 24h、新闻已落盘），
  首次取数几秒，可接受。
- **C 类**（跨标的重计算）：`sector_pick` 要对 N 只算信号、`get_overview_scan` 涉及全池
  几十个标的。**不在对话回合里现算**——`get_overview_scan` 只读缓存、缺失就回
  `available=false` + 提示，`sector_pick` 有 `top_n ≤ 30` 硬上限且必须带覆盖面回话。
"""

import json
import math
from dataclasses import dataclass
from typing import Callable

from utils.logger import get_logger

logger = get_logger("chat_tools")

# sector_pick 的硬上限：对话里现算 20 只已经要几秒到几十秒，30 只是能忍的天花板
PICK_TOP_N_MAX = 30
PICK_TOP_N_DEFAULT = 20
# 单个工具最多回多少行：token 成本与「够用」的折中，超出时截断并显式报总数
MAX_ROWS = 50
NEWS_ITEMS = 10          # 新闻只回最近 N 条标题，全文不进上下文
SIGNAL_TAIL = 10         # 信号序列只回最近 N 天

# 资金流五档写进 enum（data/fund_flow.py::VALID_PERIODS 的同一份定义，运行时校验）
FLOW_PERIODS = ["即时", "3日", "5日", "10日", "20日"]
SECTOR_KINDS = ["industry", "concept"]
# Kronos **不进工具集**：单只 CPU 上约 40 秒（#35 的同一条理由）
SIGNAL_SOURCES = ["technical", "chanlun"]
MOMENTUM_WINDOWS = [5, 20, 60]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具：名字 + 给模型看的说明 + JSON Schema + 同步 handler。

    handler 收 dict 参数、返回 **JSON 可序列化的 dict**（provider 负责序列化与包装）。
    handler 里不许写文件、不许改状态——工具是只读的（测试用 AST 守着）。
    """
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], dict]
    data_class: str = "B"        # A/B/C，见模块 docstring


# ========== 通用小工具 ==========

def _clean(v):
    """NaN / numpy 标量 → JSON 能吃的值。NaN 一律成 None（不能当 0，会被读成「算出来是 0」）"""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, float):
        return round(v, 4)
    item = getattr(v, "item", None)      # numpy / pandas 标量
    if callable(item):
        try:
            return _clean(item())
        except Exception:
            pass
    return str(v)


def _rows_from_df(df, index_name: str, limit: int = MAX_ROWS) -> tuple[list[dict], int]:
    """DataFrame → records（含 index 列），返回 (行, 总行数)。截断由调用方写进回答里。"""
    if df is None or getattr(df, "empty", True):
        return [], 0
    total = len(df)
    out = []
    for idx, row in df.head(limit).iterrows():
        item = {index_name: str(idx)}
        item.update({str(k): _clean(v) for k, v in row.items()})
        out.append(item)
    return out, total


def _truncated(rows: list[dict], total: int) -> dict:
    """统一的截断说明字段：模型必须能看出「这不是全部」"""
    out = {"returned": len(rows), "total": total}
    if total > len(rows):
        out["truncated"] = f"只返回了前 {len(rows)} 行（共 {total} 行），回答时要说明这一点"
    return out


def _name_from_cached_lists(symbol: str) -> str | None:
    """池外标的的名字：**只读**已下载过的全量清单，不触发取数。

    过期的名字也照用（股票改名是年度级的事），但绝不为了一个名字去打接口——
    对话里问一句「600519 怎么样」不该顺手拉一份 5000 行的股票清单。
    """
    from data import stock_list as sl

    for path in (sl._A_STOCK_CACHE, sl._HK_STOCK_CACHE):
        for row in (sl._load_cache(path) or []):
            if row.get("code") == symbol and row.get("name"):
                return row["name"]
    return None


def _resolve(symbol: str) -> tuple[str, str]:
    """(标的类型, 显示名)。池内标的按池里的配置，池外查已缓存的清单，都没有就用代码。"""
    from config.settings import get_stock_name, get_stock_type

    stype = get_stock_type(symbol)
    name = get_stock_name(symbol, stype)
    if name == symbol and stype not in ("sector", "index"):
        name = _name_from_cached_lists(symbol) or symbol
    return stype, name


def _kline(symbol: str, days: int, stype: str):
    """日K：板块走 #29 的板块管线，个股/指数走 #35 的个股管线（都带缓存，口径与页面一致）"""
    if stype == "sector":
        from analysis.correlation import _get_sector_kline

        return _get_sector_kline(symbol, days)
    from analysis.sector_pick import _get_stock_kline

    return _get_stock_kline(symbol, days=days, stock_type=stype)


def _pool_sectors() -> list[dict]:
    from config.settings import load_stock_pool

    return [s for s in load_stock_pool() if s.get("type") == "sector"]


# ========== 工具实现 ==========

def _list_pool(args: dict) -> dict:
    from config.settings import load_stock_pool

    pool = load_stock_pool()
    rows = [{"code": s["code"], "name": s["name"],
             "type": s.get("type", "stock"), "market": s.get("market", "A"),
             **({"sector_kind": s["sector_kind"]} if s.get("sector_kind") else {})}
            for s in pool]
    return {"items": rows[:MAX_ROWS], **_truncated(rows[:MAX_ROWS], len(rows))}


def _list_sectors(args: dict) -> dict:
    """池内板块（可直接用于其他板块工具）+ 可选的全量板块搜索（只在缓存已存在时）"""
    from data.sector_list import search_sectors, sector_list_available

    pool = [{"code": s["code"], "name": s["name"],
             "kind": s.get("sector_kind", "industry")} for s in _pool_sectors()]
    out = {"pool_sectors": pool, "pool_count": len(pool),
           "note": "板块动量/热度/选股这些工具只覆盖池内板块——板块日K要逐个取，"
                   "全市场 900 多个板块算不动。资金流排行是全市场的，不受此限。"}

    keyword = (args.get("keyword") or "").strip()
    if not keyword:
        return out
    if not sector_list_available():
        out["search"] = {"available": False,
                         "hint": "全量板块清单还没加载过（需在「股票池管理」页加载一次），"
                                 "现在只能用池内板块回答"}
        return out
    try:
        hits = search_sectors(keyword)[:MAX_ROWS]
    except Exception as e:
        out["search"] = {"available": False, "hint": f"板块清单搜索失败：{e}"}
        return out
    out["search"] = {"available": True, "keyword": keyword, "items": hits,
                     "note": "搜到的板块若不在 pool_sectors 里，动量/热度/选股工具用不了它，"
                             "需要用户先把它加进股票池"}
    return out


def _get_kline_summary(args: dict) -> dict:
    symbol = str(args["symbol"]).strip()
    days = int(args.get("days") or 120)
    stype, name = _resolve(symbol)
    df = _kline(symbol, days, stype)
    if df is None or df.empty or "close" not in df.columns:
        return {"available": False, "symbol": symbol, "name": name,
                "hint": f"{symbol} 日K取不到（数据源失败或代码不存在），无法据此回答"}

    close = df["close"].dropna()
    first, last = float(close.iloc[0]), float(close.iloc[-1])
    ret = (last / first - 1) * 100 if first else None
    pct = close.pct_change().dropna()
    return {
        "available": True, "symbol": symbol, "name": name, "type": stype,
        "bars": len(close),
        "start_date": str(df.index.min().date()), "end_date": str(df.index.max().date()),
        "first_close": round(first, 3), "last_close": round(last, 3),
        "period_return_pct": _clean(ret),
        "high": _clean(float(df["high"].max()) if "high" in df else close.max()),
        "low": _clean(float(df["low"].min()) if "low" in df else close.min()),
        "max_close": round(float(close.max()), 3), "min_close": round(float(close.min()), 3),
        "daily_vol_pct": _clean(float(pct.std()) * 100 if len(pct) > 1 else None),
        "avg_volume": _clean(float(df["volume"].mean()) if "volume" in df else None),
        "note": "区间收益按首尾收盘价算（前复权）；盘中当日 bar 已被剔除，"
                "end_date 是最后一个已收盘交易日",
    }


def _get_signals(args: dict) -> dict:
    from config.settings import SIGNAL_LOOKBACK, SIGNAL_THRESHOLD
    from strategy.fusion import SOURCE_COLUMNS, fuse_signals
    from strategy.signals import build_signals

    symbol = str(args["symbol"]).strip()
    source = args.get("source") or "technical"
    if source not in SIGNAL_SOURCES:
        return {"error": f"source 只能是 {SIGNAL_SOURCES}（Kronos 不在工具集里：单只约 40 秒）"}
    stype, name = _resolve(symbol)
    df = _kline(symbol, SIGNAL_LOOKBACK.get(source, 150), stype)
    if df is None or df.empty:
        return {"available": False, "symbol": symbol, "name": name,
                "hint": f"{symbol} 日K取不到，信号算不出来"}

    sig = build_signals(df, symbol=symbol, enabled_signals=[source])
    fused = fuse_signals(sig, source=source)
    col = SOURCE_COLUMNS[source]
    tail = sig.tail(SIGNAL_TAIL)
    series = [{"date": str(i.date()), "signal": _clean(r[col]),
               "decision": int(fused.loc[i, "decision"]),
               "strength": _clean(fused.loc[i, "strength"])}
              for i, r in tail.iterrows()]
    latest = series[-1] if series else {}
    return {
        "available": True, "symbol": symbol, "name": name, "source": source,
        "threshold": SIGNAL_THRESHOLD, "latest": latest, "recent": series,
        "note": f"decision 1=买入 / -1=卖出 / 0=观望，由 |signal| 与阈值 {SIGNAL_THRESHOLD} "
                f"比较得出（strategy/fusion.py 的确定性规则）。这是既有信号源的输出，"
                f"不要据此给出投资建议，也不要自己改阈值重判。",
    }


def _get_fundamentals(args: dict) -> dict:
    from analysis.fundamental import summarize
    from data.fundamentals import get_fundamentals

    symbol = str(args["symbol"]).strip()
    stype, name = _resolve(symbol)
    try:
        fund = get_fundamentals(symbol)
    except Exception as e:
        return {"available": False, "symbol": symbol, "name": name,
                "hint": f"基本面取数失败：{e}"}
    if not fund:
        return {"available": False, "symbol": symbol, "name": name,
                "hint": "取不到基本面（本接口只有 A 股个股：港股/指数/板块都没有）"}
    try:
        s = summarize(fund)
    except ValueError as e:      # 口径不对宁可报错也不能拿累计口径算趋势
        return {"available": False, "symbol": symbol, "name": name, "hint": str(e)}
    return {
        "available": True, "symbol": symbol, "name": name,
        "period": s.get("period"), "latest": {k: _clean(v) for k, v in
                                              (s.get("latest") or {}).items()},
        "revenue_trend": s.get("revenue_trend"), "profit_trend": s.get("profit_trend"),
        "roe_level": s.get("roe_level"), "margin_trend": s.get("margin_trend"),
        "leverage": {k: _clean(v) for k, v in (s.get("leverage") or {}).items()},
        "flags": s.get("flags") or [],
        "note": "flags 是确定性标签（analysis/fundamental.py 的写死规则），"
                "营收/净利看单季同比、ROE/净利率看近4季均值 vs 前4季均值。"
                "period 是最新报告期——它可能已经过了几个月，回答时要带上这个日期。",
    }


def _get_news(args: dict) -> dict:
    from data.news import fetch_stock_news

    symbol = str(args["symbol"]).strip()
    days = int(args.get("days") or 10)
    stype, name = _resolve(symbol)
    try:
        df = fetch_stock_news(symbol, lookback_trading_days=days, stock_type=stype)
    except Exception as e:
        return {"available": False, "symbol": symbol, "name": name,
                "hint": f"新闻取数失败：{e}"}
    if df is None or df.empty:
        return {"available": True, "symbol": symbol, "name": name, "count": 0,
                "items": [], "note": f"近 {days} 个交易日窗口内没有抓到该标的的新闻"}

    scores = df["sentiment_score"].dropna() if "sentiment_score" in df else []
    items = [{"published_at": str(r.get("published_at")), "source": r.get("source"),
              "title": r.get("title"), "sentiment": _clean(r.get("sentiment_score"))}
             for _, r in df.head(NEWS_ITEMS).iterrows()]
    return {
        "available": True, "symbol": symbol, "name": name, "count": len(df),
        "scored_count": int(len(scores)),
        "avg_sentiment": _clean(float(scores.mean()) if len(scores) else None),
        "items": items,
        "note": f"只返回最近 {len(items)} 条标题（共 {len(df)} 条），正文不进上下文。"
                f"sentiment 是已落盘的情绪分（越高越正面），没打分的条目为 null——"
                f"平均分只统计打过分的那些。",
    }


def _fund_flow_ranking(args: dict) -> dict:
    from data.fund_flow import sector_fund_flow_ranking

    kind, period = args["kind"], args["period"]
    order = args.get("order") or "inflow"
    top_n = min(int(args.get("top_n") or 20), MAX_ROWS)
    try:
        rows = sector_fund_flow_ranking(kind, period)
    except ValueError as e:      # 非法档位在 schema 层就该被拦住，这里是双保险
        return {"available": False, "hint": str(e)}
    if not rows:
        return {"available": False, "kind": kind, "period": period,
                "hint": "资金流数据暂不可用（THS 接口取数失败），稍后再试"}

    ordered = sorted(rows, key=lambda r: (r.get("net") is None, r.get("net") or 0.0),
                     reverse=(order == "inflow"))
    picked = [{"sector": r["sector"], "net_yi": _clean(r.get("net")),
               "inflow_yi": _clean(r.get("inflow")), "outflow_yi": _clean(r.get("outflow")),
               "change_pct": _clean(r.get("change_pct")),
               "company_count": r.get("company_count")}
              for r in ordered[:top_n]]
    return {
        "available": True, "kind": kind, "period": period, "order": order,
        "items": picked, **_truncated(picked, len(rows)),
        "unit": "亿元",
        "note": f"数据源只有 {'/'.join(FLOW_PERIODS)} 五档，**不支持任意日期区间**。"
                f"排序由数据源与本工具完成（按净额），你不要再自己加总或重排。"
                f"接口内部分页抓取，板块条数每次会有出入、个别板块可能整批缺失。",
    }


def _sector_momentum(args: dict) -> dict:
    from analysis.rotation import momentum_table, rotation_signals

    windows = args.get("windows") or MOMENTUM_WINDOWS
    windows = tuple(sorted({int(w) for w in windows if int(w) in MOMENTUM_WINDOWS}))
    if not windows:
        return {"error": f"windows 只能取 {MOMENTUM_WINDOWS} 的子集"}
    sectors = _pool_sectors()
    if not sectors:
        return {"available": False,
                "hint": "股票池里没有板块，动量算不了（让用户先在「股票池管理」页加板块）"}

    mom = momentum_table([s["code"] for s in sectors], windows=windows)
    rows, total = _rows_from_df(mom, "sector")
    out = {"available": not mom.empty, "windows": list(windows), "items": rows,
           **_truncated(rows, total),
           "index_available": bool(mom.attrs.get("index_available", True)),
           "note": "超额收益 = 板块累计收益 − 沪深300 同窗口累计收益（单位 %）；排名 1 = "
                   "该窗口超额最高，**只在池内板块之间排**。指标为 null 的板块是日K不足，"
                   "不参与排名。index_available=false 时「超额」列实际是绝对收益（已降级），"
                   "必须在回答里说明。"}
    if 5 in windows and 20 in windows:
        out["rotation_labels"] = rotation_signals(mom)
        out["labels_note"] = ("标签由 analysis/rotation.py 的确定性规则给出（阈值=池内 1/3 "
                              "名次，一个板块只给一个标签），不要自己另行判断谁在轮动。")
    return out


def _sector_hotness(args: dict) -> dict:
    from analysis.rotation import hotness

    sectors = _pool_sectors()
    if not sectors:
        return {"available": False,
                "hint": "股票池里没有板块，热度分算不了（让用户先加板块）"}
    hot = hotness([s["code"] for s in sectors])
    rows, total = _rows_from_df(hot, "sector")
    return {
        "available": not hot.empty, "items": rows, **_truncated(rows, total),
        "weights": hot.attrs.get("weights", {}),
        "note": "「热门」= 确定性公式 热度分 = w1·z(20日超额) + w2·z(20日资金净额)，两项都在"
                "池内做 z-score，权重来自 config/settings.py::SECTOR_HOTNESS_WEIGHTS。"
                "**这就是本系统对「热门」的定义**，不要换成你自己的标准，也不要自己加权。"
                "热度分为 null 表示某个分项缺失（不是 0 分），备注列写了原因，要转达。",
    }


def _sector_pick(args: dict) -> dict:
    from analysis.sector_pick import pick

    code = str(args["sector_code"]).strip()
    kind = args.get("kind")
    top_n = min(int(args.get("top_n") or PICK_TOP_N_DEFAULT), PICK_TOP_N_MAX)
    res = pick(code, kind, top_n=top_n)
    if not res.get("available"):
        return {"available": False, "sector": res.get("sector"), "sector_code": code,
                "hint": (res.get("warnings") or ["成分股不可用"])[0]}

    items = [{
        "code": c["code"], "name": c["name"],
        "change_pct_snapshot": _clean(c.get("change_pct")),
        "technical": _clean(c.get("technical")), "chanlun": _clean(c.get("chanlun")),
        "return_pct": _clean(c.get("stock_return")), "excess_pct": _clean(c.get("excess")),
        "fund_score": _clean(c.get("fund_score")), "fund_flags": c.get("fund_flags"),
        "score": _clean(c.get("score")), "note": c.get("note") or "",
    } for c in res["candidates"][:MAX_ROWS]]
    return {
        "available": True, "sector": res["sector"], "sector_code": code,
        "kind": res["kind"],
        # 跨 kind 命中时把东财那边的归类也带上（#38），否则模型会照着 kind 说错话
        "em_board": (f"{res['em_name']}({res['em_code']})"
                     + (f"，东财归类为 {res.get('em_kind')} 板块（与本系统的 "
                        f"{res['kind']} 口径不同，属正常）" if res.get("cross_kind") else "")),
        "total_constituents": res["total_constituents"], "computed": res["computed"],
        "top_n": res["top_n"], "stale": res["stale"], "fetched_at": res["fetched_at"],
        "sector_return_pct": _clean(res.get("sector_return")),
        "weights": res.get("weights", {}), "items": items,
        "warnings": res.get("warnings", []),
        "note": f"**覆盖面必须转述**：共 {res['total_constituents']} 只成分股，先按涨跌幅"
                f"粗筛 {res['top_n']} 只、实际算成 {res['computed']} 只——不是全都算过了。"
                f"排序分是确定性加权（z(技术)+z(超额)+基本面加减分，权重见 weights），"
                f"顺序就是答案的顺序，不要自己重排。change_pct_snapshot 是成分股缓存写入"
                f"那一刻（fetched_at）的涨跌幅，不是实时值。stale=true 表示用的是过期缓存。",
    }


def _get_overview_scan(args: dict) -> dict:
    """**只读** st.session_state 里总览页扫描的结果，缓存缺失就如实说，绝不现算。

    全池几十个标的扫一遍要几十秒（每只取日K + 算信号），放进对话回合里等于让用户
    干等，而且他很可能只是随口一问。
    """
    meta = None
    try:
        import streamlit as st

        meta = st.session_state.get("overview_meta")
    except Exception as e:          # 不在 Streamlit 脚本上下文里（测试/离线）
        logger.info(f"读不到 st.session_state（{type(e).__name__}），按缓存缺失处理")
    if not meta:
        return {"available": False,
                "hint": "总览页还没扫描过（本轮会话没有缓存）。请让用户先打开「全局信号总览」"
                        "页扫一遍，再回来问——这里刻意不现算：全池几十个标的要几十秒。"}
    rows = [{k: _clean(v) for k, v in row.items() if not k.startswith("_")}
            for row in list(meta.values())[:MAX_ROWS]]
    return {"available": True, "items": rows, **_truncated(rows, len(meta)),
            "note": "这是总览页上一次扫描的快照（用户点扫描时的时点），不是此刻的实时行情；"
                    "回答时说明它可能不是最新的。"}


# ========== 工具清单 ==========

def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


_SYMBOL = {"type": "string",
           "description": "标的代码，如 600519（A股）/ 00700（港股）/ 000300（指数）/ "
                          "881279（同花顺板块代码，用 list_sectors 查）"}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_pool",
        data_class="A",
        description="列出用户股票池里的全部标的（个股/板块/指数）及其代码、类型、市场。"
                    "代价：本地读 config，毫秒级。**先调它**——用户说「我的持仓」「池子里」"
                    "「这几个票」时指的就是这份清单；其他工具都要传代码，代码从这里拿。"
                    "边界：这只是自选清单，**不含持仓数量与成本**（系统里没有真实仓位），"
                    "也不是全市场清单；超过 50 条会截断并在 truncated 里说明。",
        input_schema=_obj({}, []),
        handler=_list_pool,
    ),
    ToolSpec(
        name="list_sectors",
        data_class="A",
        description="列出股票池里配置的板块（含 industry/concept 类型），可选按关键词搜全量"
                    "板块清单。代价：池内清单是本地读；关键词搜索只在全量清单已缓存时可用，"
                    "没缓存就返回提示、不会去打接口。边界：板块动量/热度/选股只能用池内板块"
                    "（板块日K要逐个取），资金流排行则是全市场。",
        input_schema=_obj({"keyword": {"type": "string",
                                       "description": "可选，板块名或代码的关键词"}}, []),
        handler=_list_sectors,
    ),
    ToolSpec(
        name="get_kline_summary",
        description="单个标的的日K摘要统计：区间涨跌幅、最高/最低、日波动率、均量、起止日期。"
                    "代价：命中缓存是本地读（毫秒），未命中要打行情接口（几秒）。"
                    "边界：只给摘要，不返回逐日 K 线（几百行进上下文没意义）；"
                    "不预测未来价格。",
        input_schema=_obj({
            "symbol": _SYMBOL,
            "days": {"type": "integer", "minimum": 30, "maximum": 500, "default": 120,
                     "description": "回溯日历天数（不是交易日），默认 120"},
        }, ["symbol"]),
        handler=_get_kline_summary,
    ),
    ToolSpec(
        name="get_signals",
        description="单个标的的既有信号源输出：最近 10 天的信号值 + decision(1买/-1卖/0观望) "
                    "+ strength，阈值来自配置。代价：命中日K缓存约 1 秒，未命中几秒；"
                    "缠论比技术指标略慢。边界：只有 technical / chanlun 两个源——"
                    "**Kronos 不在工具集里**（单只 CPU 上约 40 秒）；这是既有系统的信号，"
                    "不是你的判断，也不构成投资建议。",
        input_schema=_obj({
            "symbol": _SYMBOL,
            "source": {"type": "string", "enum": SIGNAL_SOURCES, "default": "technical"},
        }, ["symbol"]),
        handler=_get_signals,
    ),
    ToolSpec(
        name="get_fundamentals",
        description="单只 A 股的基本面确定性判断：最新单季营收/净利/ROE/净利率/资产负债率、"
                    "趋势方向与连续季数、以及一组确定性 flags。代价：24 小时文件缓存，"
                    "命中是本地读，未命中要打接口（几秒）。边界：**只有 A 股个股**"
                    "（港股/指数/板块一律 available=false）；报告期可能已过几个月，"
                    "回答必须带上 period。",
        input_schema=_obj({"symbol": _SYMBOL}, ["symbol"]),
        handler=_get_fundamentals,
    ),
    ToolSpec(
        name="get_news",
        description="单个标的近 N 个交易日的新闻：条数、平均情绪分、最近 10 条标题+时间+情绪分。"
                    "代价：已落盘的部分是本地读，增量抓取要打新闻源（几秒）。"
                    "边界：只回标题不回正文；情绪分是已落盘的确定性打分，没打分的为 null，"
                    "平均分只算打过分的；窗口外的旧新闻已被定时清理，问不到。",
        input_schema=_obj({
            "symbol": _SYMBOL,
            "days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10,
                     "description": "回溯交易日数，默认 10"},
        }, ["symbol"]),
        handler=_get_news,
    ),
    ToolSpec(
        name="sector_fund_flow_ranking",
        data_class="A",
        description="板块资金流排行榜（已按净额排序，一次返回全市场板块，单位：亿元）。"
                    "代价：一次接口调用、秒级，即时档 60 秒缓存、N日档 30 分钟缓存。"
                    "⚠️ **只有五个固定档位，不支持任意日期区间**：用户问「过去一个月」"
                    "用 20日（20 个交易日）；问「过去 45 天」「7月15日到8月1日」这类"
                    "**请直接说明做不到**，不要拿 20日 的数据当答案。排序已由工具完成，"
                    "不要自己加总或重排。",
        input_schema=_obj({
            "kind": {"type": "string", "enum": SECTOR_KINDS,
                     "description": "industry=行业板块，concept=概念板块"},
            "period": {"type": "string", "enum": FLOW_PERIODS,
                       "description": "只有这五档；「过去一个月」→ 20日"},
            "order": {"type": "string", "enum": ["inflow", "outflow"], "default": "inflow",
                      "description": "inflow=净流入最多的在前，outflow=净流出最多的在前"},
            "top_n": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS, "default": 20},
        }, ["kind", "period"]),
        handler=_fund_flow_ranking,
    ),
    ToolSpec(
        name="sector_momentum",
        description="池内板块的动量矩阵：各窗口的绝对收益、相对沪深300 的超额收益、池内排名；"
                    "同时选上 5 日与 20 日时附带确定性轮动标签（持续走强/新晋走强/转弱/"
                    "持续弱势）。代价：**每个板块要取一次板块日K**，首次约 1 秒/个"
                    "（10 个板块约 10 秒），之后命中 DuckDB 缓存。边界：只覆盖股票池里的"
                    "板块；窗口只有 5/20/60 交易日三档。",
        input_schema=_obj({
            "windows": {"type": "array", "default": MOMENTUM_WINDOWS,
                        "items": {"type": "integer", "enum": MOMENTUM_WINDOWS},
                        "description": "动量窗口（交易日），只有 5/20/60 三档可选"},
        }, []),
        handler=_sector_momentum,
    ),
    ToolSpec(
        name="sector_hotness",
        description="池内板块的「热度分」排名——本系统对「热门板块」的**确定性定义**："
                    "热度分 = w1·z(20日超额收益) + w2·z(20日资金净额)，两项都在池内做 "
                    "z-score，权重在配置里。代价：要取各板块日K + 一次资金流排行，"
                    "首次十几秒、之后走缓存。边界：只覆盖池内板块；某分项缺失时热度分为 "
                    "null（不是 0 分）并给出原因，必须转达。用户问「哪个板块热」就用它，"
                    "不要自己定义热门。",
        input_schema=_obj({}, []),
        handler=_sector_hotness,
    ),
    ToolSpec(
        name="sector_pick",
        data_class="C",
        description="板块内选股：取该板块成分股 → 按涨跌幅粗筛 top_n 只 → 对这几只算技术/"
                    "缠论信号、基本面 flags、相对板块超额收益 → 确定性加权排序。"
                    "⚠️ **这是最慢的工具**：要对 top_n 只逐个算信号与财报，20 只约 5~30 秒；"
                    "成分股取不到（东财限流）或板块名匹配不上时会直接返回 available=false。"
                    "边界：top_n 上限 30；返回的 total_constituents/computed 说明覆盖面，"
                    "**回答时必须说明「从 M 只里筛了 N 只算」**，不能说成全扫过了。",
        input_schema=_obj({
            "sector_code": {"type": "string",
                            "description": "同花顺板块代码（用 list_sectors 查，如 881279）"},
            "kind": {"type": "string", "enum": SECTOR_KINDS,
                     "description": "可选，缺省按股票池配置"},
            "top_n": {"type": "integer", "minimum": 1, "maximum": PICK_TOP_N_MAX,
                      "default": PICK_TOP_N_DEFAULT,
                      "description": f"细算只数，上限 {PICK_TOP_N_MAX}（再多一个回合等不完）"},
        }, ["sector_code"]),
        handler=_sector_pick,
    ),
    ToolSpec(
        name="get_overview_scan",
        data_class="C",
        description="读「全局信号总览」页上一次扫描的结果（全池标的的现价/涨跌幅/信号）。"
                    "代价：**只读缓存，零耗时**；没有缓存时返回 available=false + 提示，"
                    "**不会现算**（全池几十个标的要几十秒）。边界：是快照不是实时行情，"
                    "回答要说明时效；缓存按浏览器会话存在，用户没扫过就是没有。",
        input_schema=_obj({}, []),
        handler=_get_overview_scan,
    ),
)

TOOL_NAMES = tuple(spec.name for spec in TOOLS)
_BY_NAME = {spec.name: spec for spec in TOOLS}


def get_spec(name: str) -> ToolSpec:
    if name not in _BY_NAME:
        raise KeyError(f"未知工具 {name!r}，可用：{list(TOOL_NAMES)}")
    return _BY_NAME[name]


def count_rows(payload: dict) -> int:
    """工具返回值里的「行数」，用于前端轨迹展示（「返回了多少行」）。

    优先用 payload 自己报的 returned/count，其次数第一个列表字段的长度。
    """
    if not isinstance(payload, dict):
        return 1
    for key in ("returned", "count", "computed"):
        if isinstance(payload.get(key), int):
            return payload[key]
    for value in payload.values():
        if isinstance(value, list):
            return len(value)
    return 1


def call(name: str, args: dict | None = None) -> dict:
    """执行一个工具。**异常不外抛**——工具挂了要变成模型能读懂、能转达的一句话，
    否则整个回合以堆栈收场，用户只看到「出错了」。"""
    spec = get_spec(name)
    args = dict(args or {})
    try:
        payload = spec.handler(args)
    except Exception as e:
        logger.warning(f"工具 {name}({args}) 执行失败: {type(e).__name__}: {e}")
        return {"available": False, "error": f"{type(e).__name__}: {e}",
                "hint": f"工具 {name} 执行失败，请如实告知用户这项数据取不到"}
    if not isinstance(payload, dict):
        return {"available": False, "error": f"工具 {name} 返回了 {type(payload).__name__}"}
    return payload


def to_text(payload: dict) -> str:
    """工具返回值 → 喂给模型的文本（JSON）"""
    return json.dumps(payload, ensure_ascii=False, default=str)


def openai_tools() -> list[dict]:
    """同一份 spec 转成 OpenAI 兼容的 function calling 声明（给纯 HTTP 的 provider 用）。

    这个函数存在的意义就是证明工具定义没有绑在某家 SDK 上：claude_sdk 那边转成
    create_sdk_mcp_server，这边转成请求体里的 tools 字段，handler 是同一份。
    """
    return [{"type": "function",
             "function": {"name": s.name, "description": s.description,
                          "parameters": s.input_schema}} for s in TOOLS]
