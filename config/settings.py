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

def get_stock_name(code: str) -> str:
    """根据代码获取股票名称"""
    for s in load_stock_pool():
        if s["code"] == code:
            return s["name"]
    return code


def save_stock_pool(stocks: list[dict]):
    """保存股票池到 stocks.json"""
    with open(_STOCKS_FILE, "w", encoding="utf-8") as f:
        json.dump(stocks, f, ensure_ascii=False, indent=4)


def add_stock(code: str, name: str, market: str = "A"):
    """添加股票到股票池"""
    pool = load_stock_pool()
    if any(s["code"] == code for s in pool):
        return False  # 已存在
    pool.append({"code": code, "name": name, "market": market})
    save_stock_pool(pool)
    return True


def remove_stock(code: str):
    """从股票池删除股票"""
    pool = load_stock_pool()
    pool = [s for s in pool if s["code"] != code]
    save_stock_pool(pool)

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

# ========== 涨跌停规则 ==========
LIMIT_RULES = {
    "main_board": 0.10,    # 主板 ±10%
    "gem": 0.20,           # 创业板 ±20% (300xxx)
    "star": 0.20,          # 科创板 ±20% (688xxx)
}

# ========== 信号融合权重 ==========
SIGNAL_WEIGHTS = {
    "technical": 0.35,
    "chanlun": 0.35,
    "sentiment": 0.30,
}

# ========== 通知配置 ==========
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")

# ========== API 配置 ==========
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
