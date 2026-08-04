"""板块成分股数据层（issue #35）

**这是全仓库唯一用东方财富的地方**，而且只用于取成分股这一件事。行情/板块指数/资金流
一律仍走同花顺（#28/#32/#34 的既有选择不动）。原因很直接：THS 在 akshare 里**没有成分股
接口**（只有板块指数 `stock_board_*_index_ths` 与快照 `*_info_ths`），成分股只有东财有。

## 为什么绕过 akshare 自己发请求

`ak.stock_board_industry_cons_em(symbol="消费电子")` 实测**必挂**（`RemoteDisconnected`）：
它内部要先调 `stock_board_industry_name_em` 做名称→BK 代码映射，那一步打的是
`17.push2.eastmoney.com`。而底层 `clist` 接口本身是通的——所以这里手写 HTTP 调用，和
`data/realtime.py` 手写腾讯 `qt.gtimg.cn` 是同一个路子。

## 东财这条路必须按「随时取不到」设计（实测）

1. **限流是窗口性的，且分片 + 协议一起换才有意义**：2026-08-04 11:2x 实测 22 个
   `(分片, 协议)` 组合里只有 `23/http` 与 `99/https` 返回 200，其余 20 个全部当场
   `RemoteDisconnected`（0.05~0.16s 就断，所以轮完一圈很便宜）；两分钟后全挂，再过
   40 秒（第 5 轮重试）又活。issue 里只写了「分片轮换」，实测**协议也必须轮**。
2. **`pz` 有硬上限 100**：传 `pz=500` 照样只回 100 行，而 `data.total` 会告诉你真实
   条数（行业板块 496、概念板块 503）。不翻页就会静默只拿到前 100 个板块，名称匹配
   随之莫名失败。
3. 限流时偶尔返回 `HTTP 200` 但 `data` 为 `null` —— 当失败处理，不抛异常。

所以：**缓存不是优化，是这一层能用的前提**。板块名→BK 代码映射 30 天、成分股列表 7 天，
过期后取数失败**继续用过期缓存**并标 `stale=True` + 缓存日期——过期的成分股远好过没有。
"""

import json
import os

from datetime import datetime, timedelta

from utils.logger import get_logger

logger = get_logger("sector_cons")

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache", "sector_cons")
_ALIAS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "config", "em_board_alias.json")

# 东财 push2 分片 × 协议：两个维度都要轮（见模块 docstring 第 1 点）
_SHARDS = (23, 99, 1, 7, 13, 17, 29, 33, 50, 60, 82)
_SCHEMES = ("http", "https")
_COMBOS = [(shard, scheme) for shard in _SHARDS for scheme in _SCHEMES]

PAGE_SIZE = 100          # 接口硬上限，传更大照样只回 100 行
MAX_PAGES = 20           # 兜底：2000 行足够覆盖任何板块/名录
CONNECT_TIMEOUT, READ_TIMEOUT = 5, 12
RETRY_WAIT_SEC = 8       # 夜间 job 两轮重试之间的间隔（限流窗口是几十秒级别）
JOB_ATTEMPTS = 8         # 夜间 job 每个板块最多重试轮数

BOARD_MAP_TTL_DAYS = 30  # 板块增删是季度级
CONS_TTL_DAYS = 7        # 成分调整是月度级

# 东财 clist 的 fs 参数：t:2 行业板块、t:3 概念板块（与 akshare 的 name_em 系列一致）
_FS_BOARDS = {"industry": "m:90 t:2 f:!50", "concept": "m:90 t:3 f:!50"}
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://data.eastmoney.com/",
}

# 名称匹配只允许去掉这些尾缀（THS 的 `锂电池概念` ↔ 东财的 `锂电池`）。
# 刻意不做编辑距离/包含匹配：配错板块会静默返回一批完全不相干的股票。
_STRIP_SUFFIXES = ("概念", "板块", "行业")

MATCH_EXACT, MATCH_STRIPPED, MATCH_ALIAS = "exact", "stripped", "alias"

# 跨 kind 命中时 match_level 写成 `exact@concept` 这种形状（#38）：页面得能说出
# 「东财把它归在概念板块」，否则一个 industry 板块给出 concept 的成分股会像是配错了
CROSS_SEP = "@"
OTHER_KIND = {"industry": "concept", "concept": "industry"}

# 上一次成功的 (分片, 协议) 先试，省掉每次都从头轮 20 个必挂的组合
_last_good: tuple[int, str] | None = None


def _is_fresh(path: str, days: int) -> bool:
    """mtime 惯例同 data/sector_list.py::_is_cache_valid，只是 TTL 按天给"""
    if not os.path.exists(path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return datetime.now() - mtime < timedelta(days=days)


def _cache_date(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")


def _read_cache(path: str):
    if not os.path.exists(path):
        return None     # 没有缓存是正常状态（首次运行），不该记 WARNING
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"读缓存 {path} 失败: {e}")
        return None


def _write_cache(path: str, payload):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _combo_order() -> list[tuple[int, str]]:
    """上次成功的组合排最前，其余按固定顺序（可用组合会漂移，但重试代价很低）"""
    if _last_good is None:
        return list(_COMBOS)
    return [_last_good] + [c for c in _COMBOS if c != _last_good]


def _get_page(fs: str, fields: str, page: int) -> dict | None:
    """取一页。轮换 (分片, 协议) 直到有一个给出结构完整的 data，全挂返回 None。"""
    global _last_good
    import requests

    params = {
        "pn": page, "pz": PAGE_SIZE, "po": 1, "np": 1,
        "fltt": 2, "invt": 2, "fid": "f3", "fs": fs, "fields": fields,
    }
    for shard, scheme in _combo_order():
        url = f"{scheme}://{shard}.push2.eastmoney.com/api/qt/clist/get"
        try:
            resp = requests.get(url, params=params, headers=_HEADERS,
                                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            data = (resp.json() or {}).get("data")
        except Exception:
            continue        # 限流时当场 RemoteDisconnected，换下一个组合
        # 限流时也会出现 200 + data=null；当失败处理，不抛
        if not data or not data.get("diff"):
            continue
        _last_good = (shard, scheme)
        return data
    logger.warning(f"东财 clist 全部 {len(_COMBOS)} 个(分片,协议)组合均失败 fs={fs} page={page}")
    return None


def _get_page_retry(fs: str, fields: str, page: int, attempts: int,
                    wait: int) -> dict | None:
    """一页的重试：轮完 22 个组合还不行就隔 wait 秒再来一轮。

    **重试必须落在「那一页」上**：实测同一时刻 page1 挂而 page2~5 全通（可用窗口是
    按请求瞬间算的，不是按整批），所以「整批失败→整批重来」会白等——第二轮很可能又在
    另一页上挂掉。
    """
    import time

    for attempt in range(max(1, attempts)):
        data = _get_page(fs, fields, page)
        if data is not None:
            return data
        if attempt < attempts - 1:
            time.sleep(wait)
    return None


def _clist(fs: str, fields: str, attempts: int = 1,
           wait: int = RETRY_WAIT_SEC) -> list[dict] | None:
    """翻页取全部行（pz 硬上限 100，见模块 docstring 第 2 点）。

    `attempts=1`（默认）给交互路径：轮完 22 个组合还是不行就立刻返回 None，页面不该为
    限流窗口卡住几十秒。夜间 job / 用户点「重试」时传大一点的 attempts。
    **任一页最终失败 → 整批 None**：半截数据比没有更危险（名录缺页会让名称匹配莫名失败）。
    """
    first = _get_page_retry(fs, fields, 1, attempts, wait)
    if first is None:
        return None
    rows = list(first["diff"])
    total = int(first.get("total") or len(rows))
    pages = min(MAX_PAGES, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    for page in range(2, pages + 1):
        data = _get_page_retry(fs, fields, page, attempts, wait)
        if data is None:
            logger.warning(f"东财 clist 第 {page}/{pages} 页失败，放弃本次取数 fs={fs}")
            return None
        rows.extend(data["diff"])
    return rows


# ========== 板块名 → 东财 BK 代码 ==========

MAP_INTERACTIVE_ATTEMPTS = 2   # 名录 5 页，交互路径给一次重试；再多就该让页面上的「重试」按钮兜


def get_em_board_map(kind: str, force_refresh: bool = False,
                     attempts: int = MAP_INTERACTIVE_ATTEMPTS) -> dict | None:
    """`{东财板块名: BK代码}`，30 天文件缓存。取不到且无缓存返回 None。

    attempts > 1 时会隔 RETRY_WAIT_SEC 再试（夜间 job 用；交互路径保持 1 次即返回）。
    """
    if kind not in _FS_BOARDS:
        raise ValueError(f"kind 只能是 industry/concept，收到 {kind!r}")
    path = os.path.join(_CACHE_DIR, f"board_map_{kind}.json")
    if not force_refresh and _is_fresh(path, BOARD_MAP_TTL_DAYS):
        cached = _read_cache(path)
        if cached:
            return cached

    rows = _clist(_FS_BOARDS[kind], "f12,f14", attempts=attempts)
    if not rows:
        cached = _read_cache(path)      # 过期缓存也远好过没有
        if cached:
            logger.info(f"{kind} 板块名录取数失败，回落过期缓存（{_cache_date(path)}）")
        return cached
    mapping = {str(r["f14"]).strip(): str(r["f12"]).strip()
               for r in rows if r.get("f14") and r.get("f12")}
    _write_cache(path, mapping)
    logger.info(f"{kind} 东财板块名录 {len(mapping)} 个已缓存")
    return mapping


def _load_alias() -> dict:
    """人工映射表 config/em_board_alias.json：`{THS板块名: 东财板块名 或 BKxxxx}`

    以 `_` 开头的键是文件里的说明文字，不参与匹配。
    """
    if not os.path.exists(_ALIAS_FILE):
        return {}
    data = _read_cache(_ALIAS_FILE)
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if not k.startswith("_") and isinstance(v, str)}


def _suffix_variants(name: str) -> list[str]:
    """后缀归一的候选名（去后缀 / 加后缀），逐个仍走精确查表"""
    out = []
    for suffix in _STRIP_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            out.append(name[: -len(suffix)])
            break
    out.extend(name + suffix for suffix in _STRIP_SUFFIXES)
    return [c for c in out if c != name]


def match_em_board(ths_name: str, board_map: dict, kind: str | None = None,
                   cross: bool = False) -> dict | None:
    """THS 板块名 → **单个**东财名录里的板块，四级逐级降级，匹配不上返回 None。

    1. 精确匹配 THS 名 == 东财名
    2. 后缀归一后精确匹配：去掉尾部 `概念`/`板块`/`行业`（`锂电池概念` → `锂电池`），
       或补上这些尾缀（`储能` → `储能概念`）——实测两个方向都出现
    3. 查 `config/em_board_alias.json`（值可以是东财板块名，也可以直接是 `BKxxxx`）
    4. 全部失败 → None（页面提示把正确的东财板块名补进 alias 表）

    **不做编辑距离/包含匹配**：`电池` 包含在 `锂电池概念` 里，但那是两个不同板块，
    静默配错会返回一批完全不相干的股票。

    跨两个 kind 的名录轮询由 `resolve_em_board` 负责（#38），这里只管一份名录。
    `kind` / `cross` 只用于给返回值打标记，不影响匹配判定。

    Returns: `{"em_code", "em_name", "match_level", "em_kind", "cross_kind"}` 或 None
    """
    if not ths_name or not board_map:
        return None

    def hit(code, name, level):
        return {"em_code": code, "em_name": name,
                "match_level": f"{level}{CROSS_SEP}{kind}" if cross and kind else level,
                "em_kind": kind, "cross_kind": bool(cross)}

    if ths_name in board_map:
        return hit(board_map[ths_name], ths_name, MATCH_EXACT)

    # 后缀归一是**双向**的：THS 的 `锂电池概念` 对东财的 `锂电池`（去后缀），
    # 而 THS 的 `储能` 对东财的 `储能概念`（加后缀）——实测两个方向都出现。
    # 每个候选仍然是一次精确查表，不是模糊匹配。
    for cand in _suffix_variants(ths_name):
        if cand in board_map:
            return hit(board_map[cand], cand, MATCH_STRIPPED)

    alias = _load_alias().get(ths_name)
    if alias:
        if str(alias).upper().startswith("BK"):
            return hit(str(alias).upper(), ths_name, MATCH_ALIAS)
        if alias in board_map:
            return hit(board_map[alias], alias, MATCH_ALIAS)
        logger.info(f"alias 表里 {ths_name} → {alias}，但这份东财名录里没有 {alias}")
    return None


def _load_map(kind: str, attempts: int = 1, cached_only: bool = False) -> dict | None:
    """一份名录。`cached_only=True` 时只读缓存、绝不打接口（给页面画按钮用）。"""
    if cached_only:
        return _read_cache(os.path.join(_CACHE_DIR, f"board_map_{kind}.json"))
    return get_em_board_map(kind, attempts=attempts)


def _board_maps(kind: str, cached_only: bool = True) -> list[tuple[str, dict]]:
    """`[(kind, 名录)]`，本 kind 在前。**只给「只读缓存」的场景用**（判断有没有名录、
    收集候选提示）——读缓存是免费的，取数路径请用 `resolve_em_board` 的懒加载。"""
    out = []
    for k in (kind, OTHER_KIND.get(kind)):
        if not k:
            continue
        mapping = _load_map(k, cached_only=cached_only)
        if mapping:
            out.append((k, mapping))
    return out


def resolve_em_board(ths_name: str, kind: str, attempts: int = 1,
                     cached_only: bool = False) -> dict | None:
    """THS 板块名 → 东财板块，**两个 kind 的名录都查**（#38）。

    为什么必须跨 kind：两家的一级分类口径不对齐。实测东财**行业**名录里根本没有
    「汽车整车」（它那套是申万式细分：乘用车/商用车/综合乘用车…），而**概念**名录里
    有一个同名的 `汽车整车`(BK1029)。只查同 kind 的话，池内 `汽车整车`(industry) 会
    四级全落空，报「去补 alias」——而东财其实有同名板块，只是归在另一个桶里。

    顺序：本 kind 的三级 → 另一个 kind 的三级。本 kind 有同名就用本 kind 的
    （两边都有同名板块时不该跑到另一个桶里去取）。

    跨 kind 仍然只做**精确 / 后缀归一 / alias**，绝不因此放松成包含或编辑距离匹配：
    跨 kind 的精确同名远比同 kind 的模糊匹配安全——叫这个名字的板块就是这个板块，
    东财把它归在哪一类是它自己的分类问题。

    **另一份名录是懒加载的**：本 kind 命中就到此为止，不去取第二份。名录取数是要打
    限流接口的，绝大多数板块在本 kind 就能命中，眼前多取一份等于把限流暴露翻倍
    （而且第一次跑就会因为多打一枪把窗口用掉）。
    """
    for i, k in enumerate((kind, OTHER_KIND.get(kind))):
        if not k:
            continue
        mapping = _load_map(k, attempts=attempts, cached_only=cached_only)
        if not mapping:
            continue      # 这一份取不到（限流）不算失败，接着试另一份
        got = match_em_board(ths_name, mapping, kind=k, cross=(i > 0))
        if got:
            if got["cross_kind"]:
                logger.info(f"{ths_name}({kind}) 在东财 {k} 名录里命中 "
                            f"{got['em_name']}({got['em_code']})——两家分类口径不一致")
            return got
    return None


# ========== 成分股 ==========

def get_sector_constituents(sector_name: str, kind: str,
                            force_refresh: bool = False,
                            attempts: int = 1) -> dict | None:
    """板块成分股（东财），7 天文件缓存 + 过期回落。

    Args:
        sector_name: **THS 板块名**（股票池里配置的那个名字），内部做东财名称匹配
        kind: industry / concept

    Returns:
        ```
        {"sector": THS名, "em_code": "BK1037", "em_name": 东财名,
         "match_level": exact|stripped|alias,
         "constituents": [{"code","name","price","change_pct"}],
         "fetched_at": "2026-08-04 11:30", "stale": False}
        ```
        匹配不上东财板块、或取数失败且无任何缓存 → None（调用方据此禁用选股入口）。
        `stale=True` 表示这是过期缓存（取数失败时的回落），`fetched_at` 是缓存日期。

    signature 与 issue 里写的 `list[dict] | None` 不同：list 装不下 `stale` 与缓存
    日期，而这两个信息必须传到页面上（"过期的成分股远好过没有"的前提是用户知道它过期了）。
    """
    matched = resolve_em_board(sector_name, kind, attempts=attempts)
    if not matched:
        logger.info(f"{sector_name}({kind}) 未匹配到东财板块（两个名录都查过），跳过成分股"
                    f"（可在 config/em_board_alias.json 手工补）")
        return None

    em_code = matched["em_code"]
    path = os.path.join(_CACHE_DIR, f"cons_{em_code}.json")

    def _payload(cons, stale, when):
        return {"sector": sector_name, "em_code": em_code,
                "em_name": matched["em_name"], "match_level": matched["match_level"],
                # em_kind/cross_kind 要一路传到页面：跨 kind 命中时得说明「东财把它归在
                # 概念板块」，不然 industry 板块给出 concept 成分股会像是配错了（#38）
                "em_kind": matched.get("em_kind"),
                "cross_kind": bool(matched.get("cross_kind")),
                "constituents": cons, "fetched_at": when, "stale": stale}

    if not force_refresh and _is_fresh(path, CONS_TTL_DAYS):
        cached = _read_cache(path)
        if cached:
            return _payload(cached, False, _cache_date(path))

    rows = _clist(f"b:{em_code}", "f12,f14,f2,f3", attempts=attempts)
    if not rows:
        cached = _read_cache(path)
        if cached:
            logger.info(f"{sector_name} 成分股取数失败，回落过期缓存（{_cache_date(path)}）")
            return _payload(cached, True, _cache_date(path))
        return None

    cons = []
    for r in rows:
        code = str(r.get("f12") or "").strip()
        if not code:
            continue
        cons.append({
            "code": code,
            "name": str(r.get("f14") or "").strip(),
            # 限流/停牌时这两个字段会是 '-'，统一成 None 交给上层
            "price": _num(r.get("f2")),
            "change_pct": _num(r.get("f3")),
        })
    if not cons:
        return None
    _write_cache(path, cons)
    return _payload(cons, False, _cache_date(path))


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


SUGGEST_LIMIT = 6


def _kind_note(matched: dict) -> str:
    """跨 kind 命中时给页面一句解释；同 kind 返回空串（#38）"""
    if not matched.get("cross_kind"):
        return ""
    label = "概念" if matched.get("em_kind") == "concept" else "行业"
    return f"（东财把它归在{label}板块，两家分类口径不一致）"


def suggest_em_names(sector_name: str, board_map: dict,
                     limit: int = SUGGEST_LIMIT) -> list[str]:
    """匹配失败时给**人**看的候选东财板块名（共享至少 2 个连续汉字），按共享片段多的排前。

    **匹配逻辑永不使用这个函数**——它只进提示文案。区别在于谁来定夺：自动匹配一旦用上
    包含关系就会静默配错（`电池` vs `锂电池概念`），而这里是把候选摆出来让人挑一个写进
    alias 表。

    别指望它多聪明：它按二字词命中，语义近邻给不出（`汽车整车` 与 `乘用车`/`商用车`
    没有共同二字，实测候选里只有 `汽车*` 系列）。真正该先做的是跨 kind 精确匹配
    （`resolve_em_board`，#38）——`汽车整车` 本来就在东财**概念**名录里有同名板块，
    只有两边名录都没有同名时才轮到这些候选。
    """
    if not sector_name or not board_map:
        return []
    bigrams = {sector_name[i:i + 2] for i in range(len(sector_name) - 1)}
    # 「概念/板块/行业」这几个二字词本身没有区分度：不剔掉，「芯片概念」的候选会被
    # 5G概念/中药概念 这种同后缀板块灌满（实测），真正沾边的 存储芯片/国产芯片 反而挤不进来
    bigrams -= set(_STRIP_SUFFIXES)
    if not bigrams:
        return []
    scored = []
    for em_name in board_map:
        hits = sum(1 for b in bigrams if b in em_name)
        if hits:
            scored.append((-hits, len(em_name), em_name))
    return [name for _, _, name in sorted(scored)[:limit]]


def unavailable_reason(sector_name: str, kind: str) -> str:
    """`get_sector_constituents` 返回 None 时，给页面一句能照着做的原因。

    三种情形要分开说：名录取不到（等限流窗口/夜间 job）、名字没匹配上（去补 alias）、
    成分股取不到且无缓存（等限流窗口）。只读缓存，不额外打接口。

    「名字没匹配上」这条要说明**行业与概念两个名录都查过了**（#38）：只说「东财名录里
    没有」，而实际只查了一个桶，会把人引到 alias 表去补一个本来不需要的映射。
    """
    maps = _board_maps(kind, cached_only=True)
    if not maps:
        return ("东财板块名录还没取到（接口限流窗口），夜间 job 会重试；"
                "也可以点下面的「重试取数」多等一会儿")
    matched = resolve_em_board(sector_name, kind, cached_only=True)
    if not matched:
        checked = "、".join(k for k, _ in maps)
        reason = (f"未能匹配到东财板块：东财的 {checked} 名录里都没有「{sector_name}」，"
                  f"去/加「概念/板块/行业」后缀也没有同名板块。"
                  f"请在 config/em_board_alias.json 里加一行"
                  f'「"{sector_name}": "东财板块名或BK代码"」')
        hints = []
        for _, mapping in maps:
            for name in suggest_em_names(sector_name, mapping):
                if name not in hints:
                    hints.append(name)
        if hints:
            # 只是候选，**系统不会自动采用**——口径要人来定（乘用车 ≠ 汽车整车 ≠ 汽车）
            reason += (f"。名字沾边的东财板块（需人工确认，系统不会自动采用）："
                       f"{'、'.join(hints[:SUGGEST_LIMIT])}")
        return reason
    where = (f"（东财把它归在{'概念' if matched.get('em_kind') == 'concept' else '行业'}板块）"
             if matched.get("cross_kind") else "")
    return (f"已匹配东财 {matched['em_name']}({matched['em_code']}){where}，但成分股取数失败"
            f"且本地没有任何缓存（接口限流），夜间 job 会重试")


STATE_CACHED = "cached"          # 本地有成分股缓存，点选股立刻能用
STATE_NO_CACHE = "no_cache"      # 名字匹配上了但还没缓存成分股，点下去会现场取数（可能被限流）
STATE_UNMATCHED = "unmatched"    # 未匹配到东财板块 → 这个板块**不支持**选股，入口该禁用
STATE_NO_MAP = "no_map"          # 连东财名录都还没取到，匹配无从谈起，点下去会现场取


def cached_status(sector_name: str, kind: str) -> dict:
    """**只读缓存**判断选股入口该开还是该禁（不打任何接口）。

    页面渲染一行一个板块，不能为了画按钮去打十几次限流接口——所以这里只看本地缓存。
    区分四种状态（见 STATE_*）：只有 `unmatched` 是确定性的「不支持」（名字对不上，
    再等限流窗口也没用，得去补 alias），其余三种都允许点击现场取数——没有缓存不等于
    取不到，第一次用总得有人去打那一枪。

    Returns: `{"state", "reason", "count", "fetched_at", "stale", "em_code", "em_name",
    "em_kind", "cross_kind"}`
    """
    out = {"state": STATE_NO_MAP, "reason": "", "count": None,
           "fetched_at": None, "stale": False, "em_code": None, "em_name": None,
           "em_kind": None, "cross_kind": False}
    if not _board_maps(kind, cached_only=True):
        out["reason"] = "东财板块名录还没取到，点「选股」会现场尝试取数（可能撞上限流窗口）"
        return out

    matched = resolve_em_board(sector_name, kind, cached_only=True)
    if not matched:
        out["state"] = STATE_UNMATCHED
        out["reason"] = unavailable_reason(sector_name, kind)
        return out
    out["em_code"], out["em_name"] = matched["em_code"], matched["em_name"]
    out["em_kind"], out["cross_kind"] = matched.get("em_kind"), matched["cross_kind"]

    cons_path = os.path.join(_CACHE_DIR, f"cons_{matched['em_code']}.json")
    cached = _read_cache(cons_path) if os.path.exists(cons_path) else None
    if not cached:
        out["state"] = STATE_NO_CACHE
        out["reason"] = (f"已匹配东财 {matched['em_name']}({matched['em_code']})"
                         f"{_kind_note(matched)}，但还没有成分股缓存，"
                         f"点「选股」会现场取数（可能撞上限流窗口）")
        return out
    out.update({
        "state": STATE_CACHED, "count": len(cached),
        "fetched_at": _cache_date(cons_path),
        "stale": not _is_fresh(cons_path, CONS_TTL_DAYS),
    })
    return out


def refresh_pool_constituents() -> dict:
    """夜间 job：刷新股票池里所有板块的成分股（#35）。

    东财限流是窗口性的，一晚多试几次大概率撞上可用窗口；白天用户点「选股」时就直接命中
    缓存。返回 `{板块名: 成分股数 or None}` 便于日志核对。
    """
    from config.settings import load_stock_pool

    out = {}
    for s in load_stock_pool():
        if s.get("type") != "sector":
            continue
        got = get_sector_constituents(s["name"], s.get("sector_kind", "industry"),
                                      force_refresh=True, attempts=JOB_ATTEMPTS)
        out[s["name"]] = len(got["constituents"]) if got else None
    ok = sum(1 for v in out.values() if v)
    logger.info(f"成分股夜间刷新：{ok}/{len(out)} 个板块成功")
    return out
