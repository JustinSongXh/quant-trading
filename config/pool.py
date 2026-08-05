"""股票池（自选清单）的读写（issue #41 从 config/settings.py 拆出）

池子存在 `config/stocks.json`——**本机文件，不入 git**（每个人的自选不一样）。
条目形如 `{"code", "name", "market", "type"?, "sector_kind"?}`：个股不写 `type`，
指数写 `type="index"`，板块写 `type="sector"` + `sector_kind`（industry/concept，
决定取数走行业还是概念接口）。

为什么从 `settings.py` 搬出来：`settings.py` 是参数表（技术指标、权重、路径、阈值），
被每一层 import；而这里是**有状态的数据访问**（读文件、写文件）。两者混在一个模块里，
等于让"读一个常量"和"落盘改自选池"共用一个入口。

拆分时同时删掉了 `settings.STOCK_POOL`——它是 **import 时**算出来的代码列表，
池子改了同进程不更新（`main.py` 用的就是它，一个安静的陈旧读）。要代码列表就现取：
`[s["code"] for s in load_stock_pool()]`。
"""

import json
import os

_STOCKS_FILE = os.path.join(os.path.dirname(__file__), "stocks.json")

# stocks.json 不存在时的兜底池（首次启动/新机器）
_DEFAULT_POOL = [
    {"code": "600519", "name": "贵州茅台"},
    {"code": "000858", "name": "五粮液"},
    {"code": "300750", "name": "宁德时代"},
]


def load_stock_pool() -> list[dict]:
    """从 stocks.json 加载股票池，返回 [{"code": "600519", "name": "贵州茅台"}, ...]"""
    if os.path.exists(_STOCKS_FILE):
        with open(_STOCKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return list(_DEFAULT_POOL)


def save_stock_pool(stocks: list[dict]):
    """保存股票池到 stocks.json"""
    with open(_STOCKS_FILE, "w", encoding="utf-8") as f:
        json.dump(stocks, f, ensure_ascii=False, indent=4)


def get_stock_name(code: str, stock_type: str | None = None) -> str:
    """根据代码获取股票名称"""
    for s in load_stock_pool():
        if s["code"] == code:
            if stock_type and s.get("type", "stock") != stock_type:
                continue
            return s["name"]
    return code


def get_stock_type(code: str) -> str:
    """根据代码获取类型（stock/index/sector），默认 stock"""
    for s in load_stock_pool():
        if s["code"] == code and s.get("type") in ("index", "sector"):
            return s["type"]
    return "stock"


def get_sector_info(code: str) -> tuple[str, str] | None:
    """板块代码 → (板块名称, sector_kind)；sector_kind 为 industry/concept。非板块返回 None。

    板块取数需要中文名（akshare 的 key）与 kind（决定行业/概念接口）。
    """
    for s in load_stock_pool():
        if s["code"] == code and s.get("type") == "sector":
            return s["name"], s.get("sector_kind", "industry")
    return None


def _pool_entry(code: str, name: str, market: str = "A", stock_type: str = "stock",
                sector_kind: str | None = None) -> dict:
    """构造股票池条目（个股不写 type，指数/板块写 type，板块另写 sector_kind）"""
    entry = {"code": code, "name": name, "market": market}
    if stock_type == "index":
        entry["type"] = "index"
    elif stock_type == "sector":
        entry["type"] = "sector"
        entry["sector_kind"] = sector_kind or "industry"
    return entry


def add_stocks(entries: list[dict]) -> int:
    """批量添加，**一次 load + 一次 save**，返回真正新增的条数（#37）。

    entries 每项含 code / name，可选 market / type / sector_kind（与 add_stock 同义）。
    按 (code, type) 判重：已在池里的跳过，entries 自身重复也只进一条。
    一次加 90 个行业板块不该产生 90 次读写 json。
    """
    if not entries:
        return 0
    pool = load_stock_pool()
    have = {(s["code"], s.get("type", "stock")) for s in pool}
    added = []
    for e in entries:
        stype = e.get("type") or e.get("stock_type") or "stock"
        key = (e["code"], stype)
        if key in have:
            continue
        have.add(key)
        added.append(_pool_entry(e["code"], e.get("name") or e["code"],
                                 e.get("market", "A"), stype, e.get("sector_kind")))
    if not added:
        return 0
    save_stock_pool(pool + added)
    return len(added)


def remove_stocks(items: list[tuple[str, str]]) -> int:
    """批量移除，一次 load + 一次 save。items 为 [(code, stock_type)]，返回删除条数。

    删不存在的项不报错（返回数按实际删掉的算）。
    """
    if not items:
        return 0
    targets = {(code, stype or "stock") for code, stype in items}
    pool = load_stock_pool()
    kept = [s for s in pool if (s["code"], s.get("type", "stock")) not in targets]
    removed = len(pool) - len(kept)
    if removed:
        save_stock_pool(kept)
    return removed


def add_stock(code: str, name: str, market: str = "A", stock_type: str = "stock",
              sector_kind: str | None = None):
    """添加股票到股票池

    sector_kind 仅 stock_type=="sector" 时使用（industry/concept），决定取数走行业还是概念接口。
    """
    return add_stocks([{"code": code, "name": name, "market": market,
                        "type": stock_type, "sector_kind": sector_kind}]) == 1


def remove_stock(code: str, stock_type: str = "stock"):
    """从股票池删除股票"""
    remove_stocks([(code, stock_type)])
