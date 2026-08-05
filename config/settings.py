"""全局参数表：技术指标 / 回测 / 仓位 / 阈值 / 路径 / provider 默认值。

**只放常量与 .env 读取，不放有状态的读写**。股票池（自选清单）的加载与增删在
`config/pool.py`（#41 拆出）——它要读写 `config/stocks.json`，属于数据访问而不是配置。
"""

import os
from dotenv import load_dotenv

load_dotenv()

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

# ========== 板块内选股（issue #35）==========
# 两级漏斗的「细算」只数：成分股动辄上百只（实测 银行 84 家、电网设备 140 家），
# 每只要取日K + 算信号 + 取财报，一次交互里算不完。先用成分股接口自带的涨跌幅粗筛
# （免费），只对前 N 只做深度计算。页面上可临时改 N，且必须显示「已从 M 只中筛出 N 只」
# ——被静默丢掉的覆盖面会被读成「全都算过了」（#30 的教训）。
SECTOR_PICK_TOP_N = 20

# 候选股排序分 = technical · z(技术信号) + excess · z(相对板块超额收益)
#              + fundamental · 基本面加减分
# 前两项在**候选集内**做 z-score；基本面加减分是 flags 的正负标签各 ±0.5 累加
# （映射表写死在 analysis/sector_pick.py::_FLAG_RULES）。取不到基本面时该项为 0 并在
# 该行标注，不静默当 0。改这里的权重，下一次选股即生效。
SECTOR_PICK_WEIGHTS = {
    "technical": 1.0,
    "excess": 1.0,
    "fundamental": 1.0,
}

# ========== API 配置 ==========
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ========== AI 生成自定义策略 ==========
# 代码生成 provider（strategy/agent_gen/providers/ 下的文件名）。
# 该 provider 不可用时，前端自动退到第一个可用的，见 registry.default_provider_key()
DEFAULT_CODEGEN_PROVIDER = "claude_sdk"

# ========== AI 分析助手（issue #36）==========
# 对话 provider（analysis/chat/providers/ 下的文件名）。同上：不可用时前端自动退到
# 第一个可用的（analysis.chat.registry.default_provider_key）——前端默认项必须走那个
# 函数，否则这一行形同虚设（#30 的 4646dd4 就是这么踩的）。
DEFAULT_CHAT_PROVIDER = "claude_sdk"
