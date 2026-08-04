import os
import json
from dotenv import load_dotenv

load_dotenv()

# ========== 股票池（从 config/stocks.json 读取）==========
_STOCKS_FILE = os.path.join(os.path.dirname(__file__), "stocks.json")

def load_stock_pool() -> list[dict]:
    """从 stocks.json 加载股票池，返回 [{"code": "600519", "name": "贵州茅台"}, ...]"""
    if os.path.exists(_STOCKS_FILE):
        with open(_STOCKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return [
        {"code": "600519", "name": "贵州茅台"},
        {"code": "000858", "name": "五粮液"},
        {"code": "300750", "name": "宁德时代"},
    ]

# 兼容旧代码：纯代码列表
STOCK_POOL = [s["code"] for s in load_stock_pool()]

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

    _fetch_sector 需要板块中文名（akshare 取数 key）与 kind（决定行业/概念接口）。
    """
    for s in load_stock_pool():
        if s["code"] == code and s.get("type") == "sector":
            return s["name"], s.get("sector_kind", "industry")
    return None


def save_stock_pool(stocks: list[dict]):
    """保存股票池到 stocks.json"""
    with open(_STOCKS_FILE, "w", encoding="utf-8") as f:
        json.dump(stocks, f, ensure_ascii=False, indent=4)


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

# ========== 数据配置 ==========
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CACHE_DB_PATH = os.path.join(DATA_DIR, "cache", "market.duckdb")

# 默认获取最近 N 个交易日的数据
DEFAULT_LOOKBACK_DAYS = 365

# ========== 技术指标参数 ==========
TECHNICAL = {
    "ma_periods": [5, 10, 20, 60],
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "rsi_period": 14,
    "kdj_period": 9,
    "boll_period": 20,
    "supertrend": {"atr_period": 10, "multiplier": 3.0},
}

# ========== 回测参数 ==========
BACKTEST = {
    "initial_capital": 100_000,       # 初始资金
    "commission_rate": 0.00025,       # 佣金费率 万2.5
    "stamp_tax_rate": 0.001,          # 印花税 千1（卖出单边）
    "slippage": 0.002,                # 滑点 0.2%
    "max_position_pct": 0.3,          # 单只股票最大仓位 30%
}

# ========== 港股回测参数 ==========
BACKTEST_HK = {
    "initial_capital": 100_000,
    "commission_rate": 0.0005,          # 佣金费率 万5
    "min_commission": 50,               # 最低佣金 50 港元
    "stamp_tax_rate": 0.0013,           # 印花税 千1.3（买卖双边）
    "slippage": 0.002,
    "max_position_pct": 0.3,
    "lot_size": 100,                    # 默认每手100股（实际每只不同）
}

# ========== 动态仓位分级（按信号强度）==========
POSITION_SIZING = [
    {"min_strength": 0.7, "position_pct": 0.30, "label": "重仓"},  # 强信号
    {"min_strength": 0.3, "position_pct": 0.20, "label": "标准仓"},  # 中信号
    {"min_strength": 0.0, "position_pct": 0.10, "label": "轻仓"},  # 弱信号
]

# ========== 涨跌停规则 ==========
LIMIT_RULES = {
    "main_board": 0.10,    # 主板 ±10%
    "gem": 0.20,           # 创业板 ±20% (300xxx)
    "star": 0.20,          # 科创板 ±20% (688xxx)
    "index": 0.20,         # 指数无涨跌停，设大值兜底
    "sector": 0.20,        # 板块指数无涨跌停，设大值兜底
}

# ========== 信号源配置（单选模式）==========
# 可选: "technical" | "chanlun" | "kronos"
DEFAULT_SIGNAL_SOURCE = "technical"
# 触发买/卖的最小信号绝对值；|signal| < 阈值 视为观望
SIGNAL_THRESHOLD = 0.3
# 每个 source 计算一天信号所需的前置 K 线天数（buffer）
# 例：技术指标当天信号 = f(K线[当天-60, 当天])；要展示 N 天信号需 fetch N + buffer 天
SIGNAL_LOOKBACK = {
    "technical": 150,   # MACD slow EMA (span=26, adjust=False) 需 ~80 bar 收敛，60 自然日不够会误报金叉
    "chanlun": 150,     # 覆盖 B3 中枢逻辑
    "kronos": 400,      # 匹配 Kronos 模型自身上下文上限
}

# ========== 板块轮动热度分权重（issue #34）==========
# 热度分 = excess_20d · z(20日超额收益) + net_20d · z(20日资金净额)，两项都在池内做
# z-score。「热门」写成公式而不让模型判断：同一个问题问两次模型对「热门」的理解会飘。
# 想更看重资金还是更看重涨幅，直接改这里的权重，页面下一次计算就生效。
SECTOR_HOTNESS_WEIGHTS = {
    "excess_20d": 1.0,
    "net_20d": 1.0,
}

# ========== 通知配置 ==========
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")

# ========== API 配置 ==========
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ========== AI 生成自定义策略 ==========
# 代码生成 provider（strategy/agent_gen/providers/ 下的文件名）。
# 该 provider 不可用时，前端自动退到第一个可用的，见 registry.default_provider_key()
DEFAULT_CODEGEN_PROVIDER = "claude_sdk"
