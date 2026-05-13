# A股港股量化分析系统

多策略融合的 A 股 + 港股量化分析系统，集成技术指标分析、缠论买卖点识别、盘后信号扫描和可视化回测。

## 架构概览

```
数据采集
├── A股: AKShare / baostock
├── 港股: 腾讯财经接口
└── 本地缓存: DuckDB(K线) + JSON(股票列表)
         │
    ┌────┴──────────┐
    ▼                ▼
技术分析(6维度)     缠论分析(czsc)
MACD/RSI/KDJ/       笔/线段/中枢
MA交叉/MA排列/       三类买卖点
布林带/成交量确认
    │                │
    └───────┬────────┘
            ▼
     信号融合 & 决策 ←── 情绪分析(预留)
            │
     ┌──────┼──────┐
     ▼      ▼      ▼
   回测   盘后扫描  可视化看板
   引擎   微信推送  Streamlit
```

## 功能

- **A 股 + 港股**：支持沪深主板/创业板/科创板 + 港股，自动适配交易规则
- **多策略信号融合**：技术指标（6 维度加权）+ 缠论买卖点 + 情绪分析（预留）
- **历史回测**：自研引擎，A 股 T+1 / 港股 T+0，涨跌停，手续费
- **全局信号总览**：一张表查看所有股票的当前推荐，点击跳转详情
- **单股票详细分析**：K 线 + 布林带 + MACD + RSI/KDJ + 信号总览 + 净值曲线
- **当前信号推荐**：MACD / RSI / KDJ / 布林带 / 缠论各策略维度判定
- **股票池管理**：搜索全市场 5000+ 只 A 股和 30 只热门港股，一键增删
- **盘后扫描**：自动扫描股票池，发现信号可推送微信
- **回测时间可选**：近 3 个月 / 6 个月 / 1 年 / 2 年

## 技术栈

| 组件 | 技术选型 |
|------|---------|
| A 股数据 | AKShare（优先）/ baostock（备选） |
| 港股数据 | 腾讯财经接口 |
| 本地缓存 | DuckDB（K线）+ JSON（股票列表，24h 过期） |
| 技术指标 | 纯 pandas/numpy 自研（MA/MACD/RSI/KDJ/布林带/量比） |
| 缠论分析 | czsc（笔/中枢/三类买卖点） |
| 信号融合 | 多维度加权 + 连续确认 + 成交量验证 |
| 回测引擎 | 自研（A 股 T+1 / 港股 T+0，涨跌停，手续费） |
| 可视化 | Streamlit + Plotly |
| 微信推送 | PushPlus |
| 情绪分析 | Claude API Haiku（预留，待接入） |

## 项目结构

```
quant-trading/
├── config/
│   ├── settings.py          # 全局配置（参数、权重、API key）
│   └── stocks.json          # 股票池（A股+港股，可通过界面管理）
├── data/
│   ├── fetcher.py           # 数据采集（A股: AKShare/baostock, 港股: 腾讯）
│   ├── stock_list.py        # 全市场股票列表获取与缓存
│   ├── cache.py             # DuckDB 本地缓存
│   ├── mock.py              # Mock 数据（离线测试用）
│   └── news.py              # 新闻采集（预留）
├── analysis/
│   ├── technical.py         # 技术指标（6维度信号 + 成交量确认）
│   ├── chanlun.py           # 缠论分析（笔/中枢/三类买卖点）
│   └── sentiment.py         # 情绪分析（预留）
├── strategy/
│   ├── signals.py           # 信号生成（整合技术+缠论+情绪）
│   └── fusion.py            # 多信号融合决策
├── backtest/
│   ├── engine.py            # 回测核心逻辑
│   ├── broker.py            # 模拟券商（A股T+1/港股T+0，手续费）
│   └── metrics.py           # 绩效指标（收益率、夏普、回撤等）
├── notify/
│   └── wechat.py            # 微信推送（PushPlus）
├── utils/
│   └── logger.py            # 日志
├── main.py                  # 回测入口
├── scanner.py               # 盘后信号扫描
├── app.py                   # Streamlit 可视化看板（3 页面）
├── requirements.txt
├── .env.example
└── .gitignore
```

## 快速开始

```bash
# 克隆项目
git clone https://github.com/JustinSongXh/quant-trading.git
cd quant-trading

# 创建虚拟环境并安装依赖
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 复制配置文件
cp .env.example .env
```

## 使用方式

### 1. 可视化看板（推荐）

```bash
streamlit run app.py --server.port 8501
```

三个页面：
- **全局信号总览**：所有股票的当前推荐一览，点击「详情」跳转分析
- **单股票分析**：选股 → 选时间范围 → 点「开始分析」，查看 K 线图、策略信号、回测绩效、交易明细
- **股票池管理**：搜索全市场股票添加，或手动输入代码添加，一键移除

### 2. 命令行回测

```bash
python main.py
```

扫描 `config/stocks.json` 中的全部股票，输出每只的回测绩效。

### 3. 盘后信号扫描

```bash
python scanner.py
```

扫描全部股票池，打印当日有买卖信号的股票。配置 PushPlus token 后可推送微信。

### 4. 定时执行（crontab）

```bash
# 每个交易日 15:30 自动扫描
30 15 * * 1-5 cd /path/to/quant-trading && .venv/bin/python scanner.py >> /var/log/quant-scanner.log 2>&1
```

## 配置说明

### 股票池 (`config/stocks.json`)

```json
[
    {"code": "600519", "name": "贵州茅台", "market": "A"},
    {"code": "00700", "name": "腾讯控股", "market": "HK"}
]
```

可通过可视化看板的「股票池管理」页面增删，也可以直接编辑此文件。

### 策略参数 (`config/settings.py`)

```python
# 技术指标参数
TECHNICAL = {
    "ma_periods": [5, 10, 20, 60],
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "rsi_period": 14,
    "kdj_period": 9,
    "boll_period": 20,
}

# 信号融合权重
SIGNAL_WEIGHTS = {
    "technical": 0.35,
    "chanlun": 0.35,
    "sentiment": 0.30,  # 待接入
}
```

### 微信推送 (`.env`)

```
PUSHPLUS_TOKEN=你的token
```

在 [pushplus.plus](https://www.pushplus.plus/) 微信扫码获取 token。

## 策略说明

### 技术信号（6 维度加权）

| 维度 | 权重 | 买入信号 | 卖出信号 |
|------|------|---------|---------|
| MACD | 25% | DIF 上穿 DEA（金叉） | DIF 下穿 DEA（死叉） |
| RSI | 10% | RSI 从超卖区回升（上穿30） | RSI 从超买区回落（下穿70） |
| KDJ | 15% | K 上穿 D，J<20 加强 | K 下穿 D，J>80 加强 |
| MA交叉 | 15% | MA5 上穿 MA20 | MA5 下穿 MA20 |
| MA排列 | 15% | 多头排列（短>中>长） | 空头排列（短<中<长） |
| 布林带 | 20% | 价格触及下轨反弹 | 价格触及上轨回落 |

最终信号乘以**成交量确认**系数（放量增强、缩量削弱）。

### 缠论信号

基于 czsc 库自动识别分型、笔、中枢，生成三类买卖点：

| 买卖点 | 信号强度 | 含义 |
|--------|---------|------|
| B1 / S1 | 1.0 | 趋势背驰（反转信号） |
| B2 / S2 | 0.7 | 回调不破前低/高（趋势确认） |
| B3 / S3 | 0.8 | 中枢突破回踩（趋势加速） |

### 融合决策

```
综合分数 = 技术信号 × 0.5 + 缠论信号 × 0.5（情绪未接入时）
```

- 缠论 B1/S1 强信号可直接触发交易
- 其余信号需连续 2 天同向确认才触发

### A 股 vs 港股交易规则

| | A 股 | 港股 |
|---|------|------|
| T+N | T+1 | T+0 |
| 涨跌停 | 主板 ±10%，创业板/科创板 ±20% | 无限制 |
| 佣金 | 万 2.5 | 万 5 |
| 印花税 | 千 1（卖出单边） | 千 1.3（双边） |
