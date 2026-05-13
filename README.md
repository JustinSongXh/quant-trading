# A股量化交易系统

基于新闻情绪分析 + 技术指标的 A 股量化交易系统。

## 架构概览

```
数据采集 (AKShare)
    ├── 行情数据（日K线、分钟线）
    ├── 财务数据（基本面指标）
    └── 新闻数据（财经新闻、公告）
         │
    ┌────┴────┐
    ▼         ▼
技术分析    情绪分析
(pandas-ta) (Claude API)
    │         │
    └────┬────┘
         ▼
    信号融合 & 决策
         │
         ▼
    回测引擎（T+1、涨跌停、手续费）
         │
         ▼
    结果可视化 (Streamlit)
```

## 技术栈

| 组件 | 技术选型 |
|------|---------|
| 行情/财务/新闻数据 | AKShare |
| 本地数据缓存 | DuckDB |
| 技术指标计算 | pandas-ta |
| 新闻情绪分析 | Claude API (Haiku) |
| 回测引擎 | 自研（支持 A 股规则） |
| 可视化 | Streamlit |

## 项目结构

```
quant-trading/
├── config/              # 配置管理
│   └── settings.py      # 全局配置（股票池、参数、API key）
├── data/                # 数据采集与缓存
│   ├── fetcher.py       # AKShare 数据采集
│   ├── news.py          # 新闻数据采集
│   └── cache.py         # DuckDB 本地缓存
├── analysis/            # 分析模块
│   ├── technical.py     # 技术指标计算
│   └── sentiment.py     # 新闻情绪分析（Claude API）
├── strategy/            # 策略模块
│   ├── signals.py       # 信号生成
│   └── fusion.py        # 多信号融合决策
├── backtest/            # 回测引擎
│   ├── engine.py        # 回测核心逻辑
│   ├── broker.py        # 模拟券商（T+1、手续费、涨跌停）
│   └── metrics.py       # 绩效指标计算
├── utils/               # 工具函数
│   └── logger.py        # 日志
├── requirements.txt
└── main.py              # 入口
```

## 快速开始

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 复制配置
cp .env.example .env
# 编辑 .env 填入 ANTHROPIC_API_KEY（情绪分析模块需要）

# 运行
python main.py
```

## 模块说明

### 数据采集 (data/)
通过 AKShare 获取 A 股行情、财务、新闻数据，使用 DuckDB 做本地缓存避免重复请求。

### 技术分析 (analysis/technical.py)
计算核心技术指标：MA/EMA、MACD、RSI、KDJ、布林带，生成技术面交易信号。

### 情绪分析 (analysis/sentiment.py)
调用 Claude API 对财经新闻进行情绪打分（-1 到 +1），批量处理后生成情绪面信号。

### 信号融合 (strategy/fusion.py)
将技术信号和情绪信号按权重融合，输出最终的买入/卖出/持有决策。

### 回测引擎 (backtest/)
自研轻量回测引擎，支持 A 股特有规则：
- T+1 交易制度
- 涨跌停限制（主板 ±10%，创业板/科创板 ±20%）
- 手续费：佣金万2.5 + 印花税千1（卖出单边）
