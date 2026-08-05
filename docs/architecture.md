# 架构与模块

## 数据流

```
数据源  AKShare/baostock · 腾讯 · 同花顺 · 东方财富 · 巨潮资讯
            │
data/       取数 + 缓存（DuckDB 日K/新闻/资金流快照 + JSON 文件缓存）
            │   唯一实现「新鲜度判断 / 失败回落 / 限流重试」的层
            ▼
analysis/   纯计算（同一份输入得同一份输出，不写文件不发请求）
            技术指标 · 缠论 · Kronos · 新闻情绪 · 基本面
            板块相关性 · 板块轮动 · 板块内选股 · 对话工具集
            │
strategy/   信号聚合 → 单源决策（阈值） → 仓位管理
            │                                  └── agent_gen: 自然语言生成策略的流水线
            ├──────────────┬───────────────┐
            ▼              ▼               ▼
backtest/          ui/（Streamlit）    scanner.py / main.py
回测引擎            7 个页面            命令行入口
```

依赖方向是单向的：`ui → strategy/analysis → data → config`。`config/settings.py` 只依赖 stdlib + dotenv，任何模块都能 import 它而不产生循环。

## 顶层包

### `config/`
- `settings.py`：全部参数常量（指标周期、回测费率、仓位分级、信号阈值与 lookback、各类权重、provider 默认值）。**只放常量与 `.env` 读取**。
- `pool.py`：股票池（`config/stocks.json`）的加载与增删。它是有状态的数据访问，因此与参数表分开（issue #41）。
- `em_board_alias.json`：同花顺板块名 → 东方财富板块名/BK 代码的人工映射表。

### `data/`
取数与缓存，详见 [data-sources.md](data-sources.md)。关键模块：`kline.py`（日K取数编排，全仓库唯一一份）、`cache.py`（DuckDB + 新鲜度口径）、`file_cache.py`（JSON 缓存 TTL）、`fetcher.py` / `realtime.py` / `stock_list.py` / `sector_list.py` / `sector_cons.py` / `fundamentals.py` / `fund_flow.py` / `news.py` + `news_sources/`。

### `analysis/`
| 模块 | 职责 |
|---|---|
| `technical.py` | 7 个分量的技术指标信号 + 成交量确认 |
| `chanlun.py` | 基于 czsc 的笔/中枢/三类买卖点 |
| `kronos_pred.py` | Kronos 基础模型预测（CPU 约 40s/只，故只手动触发） |
| `sentiment.py` | 新闻情绪打分（本地 transformers 中文金融模型） |
| `fundamental.py` | 财报趋势判定 → flags |
| `correlation.py` | 板块相关矩阵、层次聚类分组、极值板块对 |
| `rotation.py` | 板块动量/超额/排名迁移标签、热度分 |
| `sector_pick.py` | 板块内选股的两级漏斗与确定性排序 |
| `chat/` | 对话助手：`base`（契约）、`registry`（provider 自动发现）、`prompt`、`tools`（工具集）、`providers/` |

### `strategy/`
- `signals.py`：调各分析模块，产出带各信号列的表。
- `decision.py`：**单源选择 + 阈值** → `decision` / `strength`。这个模块以前的名字暗示它做多源加权融合，而它从不做——issue #41 改成了现在的名字。
- `trade_strategy.py`：可插拔的"交易策略 / 仓位管理"层，内置两个，`custom/*.py` 自动发现。
- `agent_gen/`：自然语言生成策略的流水线——`orchestrator`（主循环）→ `extract`（判定交付还是澄清）→ `guard`（AST 静态校验）→ `smoke`（子进程冒烟回测）→ `store`（`_staging` → `custom` 落盘 / `_rejected` 归档），外加 `jobs`（子进程管理）、`registry`、`prompt`、`providers/`。

### `backtest/`
`engine.py`（主循环）、`broker.py`（A 股 T+1 / 港股 T+0、涨跌停、费率、手数）、`metrics.py`（收益、夏普、回撤等）。

### `ui/`
| 模块 | 内容 |
|---|---|
| `app.py`（仓库根） | 入口：页面配置、移动端 CSS、起定时任务、`PAGES` 路由表 |
| `ui/pages/*.py` | 一页一个模块，对外只暴露 `page_xxx()` |
| `ui/shared.py` | **两个以上页面**都在用的展示助手（日K入口、市场标签、推荐文案、涨跌配色、策略参数控件） |
| `ui/cards.py` | 情绪卡与基本面卡（单股票页与新闻页共用） |
| `ui/jobs.py` | 进程内 APScheduler 三个定时任务 |

`ui/**` 的模块保持 **import 无副作用**，因此可以被测试直接 import（`tests/test_ui_split.py` 守着这条）。页面细节见 [pages.md](pages.md)。

### `utils/`
`logger.py`；`plugin_registry.py`——"丢一个文件进目录就注册一个插件"的**唯一实现**，被 chat provider、codegen provider、自定义策略三处共用。

### `tests/`
一个功能切片一个文件，末尾自带 runner（不用 pytest）。全部 mock、不打网络、不碰生产 DuckDB。跑法见 [CLAUDE.md](../CLAUDE.md) 第 3 节。

## 页面 → 模块

| 页面 | 页面模块 | 主要依赖 |
|---|---|---|
| 全局信号总览 | `ui/pages/overview.py` | `data.kline` · `data.realtime` · `strategy.signals` · `strategy.decision` |
| 单股票分析 | `ui/pages/detail.py` | 同上 + `backtest` + `ui.cards` + `analysis.chanlun` / `kronos_pred` |
| 新闻列表 | `ui/pages/news_list.py` | `data.news` · `analysis.sentiment` · `ui.cards` |
| 股票池管理 | `ui/pages/manage.py` | `config.pool` · `data.stock_list` · `data.sector_list` |
| 板块分析 | `ui/pages/sector.py` | `analysis.correlation` · `analysis.rotation` · `analysis.sector_pick` · `data.fund_flow` |
| 自定义策略 | `ui/pages/custom_strategy.py` | `strategy.trade_strategy` · `strategy.agent_gen` |
| AI 分析助手 | `ui/pages/chat.py` | `analysis.chat`（工具集内部再调上面各处） |

## 三套可插拔机制（同一份实现）

`utils/plugin_registry.py` 提供：扫目录 → `_` 前缀跳过 → importlib 导入并 reload → 文件里恰好一个目标基类子类 → `cls.key` 必须等于文件名 → 单个文件失败只 warning。

| 用处 | 目录 | 基类 | 额外规则 |
|---|---|---|---|
| 对话 provider | `analysis/chat/providers/` | `ChatProvider` | 带 `available()` 汇总与默认项选择 |
| 代码生成 provider | `strategy/agent_gen/providers/` | `CodegenProvider` | 同上 |
| 自定义交易策略 | `strategy/custom/` | `TradeStrategy` | key 不许与内置策略冲突 |

各 provider 的第三方 SDK **不进 `requirements.txt`、也不进镜像**：依赖声明跟着 provider 文件走（`REQUIRES` + `check_requirements()`），缺失时界面给出可直接复制的 pip 命令。

## 进程内定时任务（`ui/jobs.py`）

| 时间 | 任务 | 为什么在这里 |
|---|---|---|
| 每日 03:00 | 删窗口外新闻 | 采集/打分只在访问时写入，删除统一由定时任务做 |
| 每日 03:20 | 刷池内板块的成分股缓存 | 东财限流是窗口性的，白天用户只肯等一轮，缓存得靠夜里攒 |
| 交易日 15:40 | 落板块资金流快照 | 资金流没有逐日历史接口，只能自己攒 |

它们**寄生在 app 进程里**（`@st.cache_resource` 保证一个进程只起一次），所以 app 不跑时不会执行；`scanner.py` 那条路则完全没有调度（见 README）。

## 已知的结构性问题

- 总览页的扫描编排 `_scan_all_stocks` 仍在 UI 层（它拿 `st.session_state` 当缓存）→ issue #43。
- `scanner.py` 里还有一份自写的日K取数，cache key 缺 `sec_` 前缀且每次强制取数 → issue #44。
- 定时任务与 `scanner.py` 是两套调度（进程内 vs 无），通知渠道未接 → issue #4。
