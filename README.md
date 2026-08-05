# A股港股量化分析系统

A 股 + 港股的本地量化分析：取数 → 确定性计算（技术指标 / 缠论 / Kronos 预测 / 新闻情绪 / 基本面 / 板块相关性 / 板块轮动 / 板块内选股）→ Streamlit 看板 + 回测 + 盘后扫描。

**主体是零 token 的纯计算**：排序、加权、阈值判定全是写死的公式，改权重就是改 `config/settings.py`。模型只出现在两处可插拔面板里（自然语言生成策略、对话式分析助手），各自的 SDK 按需安装、不进基础镜像。

## 快速开始

只通过 Docker 运行：

```bash
docker compose up -d                                    # 起容器，访问 http://localhost:8501
curl -s -o /dev/null -w '%{http_code}\n' \
  http://localhost:8501/_stcore/health                  # 健康检查，期望 200
docker logs --tail 50 -f quant-app                      # 看日志
docker exec quant-app python tests/test_integration.py  # 容器内跑脚本/测试
```

代码目录以 volume 挂进容器，改完即时生效，不用重建镜像。

> ⚠️ **别在宿主机直接跑 python**（宿主是 2.x 且没有项目依赖）。另外**镜像重建当前是坏的**（pypi 拉大 wheel 会被掐断），日常用 `docker exec quant-app pip install <pkg>` 绕开——症状、成因与解法见 [CLAUDE.md](CLAUDE.md) 第 2、4 节。

## 页面

侧边栏 7 页（实现在 `ui/pages/`，路由表在 `app.py`）：

| 页面 | 一句话 |
|---|---|
| 全局信号总览 | 股票池所有标的的当前推荐一览，可按类型限定扫描范围 |
| 单股票分析 | K 线 + 指标 + 信号 + 回测 + 情绪卡 + 基本面卡 |
| 新闻列表 | 池内标的的新闻/公告/股吧，带情绪分 |
| 股票池管理 | 搜索全市场增删个股/指数/板块，支持批量加板块 |
| 板块分析 | 板块相关性、板块轮动、板块内选股三个视图 |
| 自定义策略 | 挂载自定义交易策略，并可用自然语言生成新策略 |
| AI 分析助手 | 把上面这些确定性函数包装成工具，用对话调用 |

每页的入口、依赖的计算、以及**代价与边界**（哪些操作慢、覆盖面到哪）见 [docs/pages.md](docs/pages.md)。

## 命令行入口

```bash
docker exec quant-app python main.py         # 批量回测股票池
docker exec quant-app python scanner.py      # 盘后扫描，打印当日有信号的标的
docker exec quant-app python purge_news.py   # 手工清理窗口外新闻
```

三个都**没有配 crontab**，目前是手动跑。app 进程内另有三个定时任务（每日 03:00 清新闻、03:20 刷板块成分股缓存、交易日 15:40 落板块资金流快照），见 `ui/jobs.py`。

## 数据来源

| 数据 | 来源 |
|---|---|
| A 股日K | AKShare（优先）→ baostock（兜底） |
| 港股行情 | 腾讯 |
| 个股/指数实时快照 | 腾讯（可批量） |
| 板块指数日K、板块快照、财报摘要、板块资金流 | 同花顺（经 AKShare） |
| 板块成分股 | 东方财富（手写 HTTP，含限流降级） |
| 新闻 / 股吧 / 公告 | 东方财富 / 东方财富 / 巨潮资讯 |

各源的模块、缓存位置与 TTL、以及限流现状见 [docs/data-sources.md](docs/data-sources.md)。

## 代码结构

| 顶层包 | 职责 |
|---|---|
| `config/` | 参数常量（`settings.py`）与股票池读写（`pool.py`） |
| `data/` | 取数与缓存，唯一实现「缓存新鲜度 / 回落 / 限流重试」的层 |
| `analysis/` | 纯计算：技术、缠论、Kronos、情绪、基本面、相关性、轮动、板块选股、对话工具 |
| `strategy/` | 信号聚合 → 决策 → 仓位管理，以及自然语言生成策略的流水线 |
| `backtest/` | 回测引擎、模拟券商、绩效指标 |
| `ui/` | Streamlit 界面：`ui/pages/` 一页一模块，`ui/shared.py` 与 `ui/cards.py` 为共用件 |
| `utils/` | 日志、插件发现 |
| `tests/` | 一个功能切片一个文件，全部 mock、不打网络 |

分层与依赖方向、模块清单、页面到模块的映射见 [docs/architecture.md](docs/architecture.md)。

## 策略与规则

信号是**单源选择 + 阈值判定**（技术 / 缠论 / Kronos 三选一，`|信号| < 阈值` 视为观望），**不是多源加权融合**。技术信号自身由 7 个分量加权、再乘成交量确认系数；A 股按 T+1、港股按 T+0 回测。

完整的分量权重、缠论买卖点强度、仓位分级、费率与涨跌停规则见 [docs/strategies.md](docs/strategies.md)——那份以代码为准，改代码就要改它（有测试守着两边一致）。

## 配置

| 在哪 | 是什么 |
|---|---|
| `config/stocks.json` | 自选股票池（个股/指数/板块）。**不入 git**，每台机器不同；界面「股票池管理」页可增删 |
| `.env` | 凭证（`ANTHROPIC_API_KEY`），模板见 `.env.example` |
| `config/settings.py` | 全部参数常量：指标周期、信号阈值、仓位分级、回测费率、各类权重 |

## 开发

开发约定（容器内跑测试、依赖怎么装、issue 先定稿再实现、提交规范、哪些文件不入 git）见 [CLAUDE.md](CLAUDE.md)。

每个功能的完整设计、实测数据与踩坑记录在 [GitHub issue](https://github.com/JustinSongXh/quant-trading/issues) 里——**issue 从不关闭**，等于这个项目的日志。
