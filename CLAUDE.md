# CLAUDE.md

给在这个仓库里干活的人和 AI 看的项目约定。**只写对任何人都成立的项目事实与流程**，个人偏好不进这里。

## 1. 项目是什么

A 股 + 港股量化分析系统：本地取数 → 确定性计算（技术指标 / 缠论 / Kronos 预测 / 新闻情绪 / 基本面 / 板块相关性 / 板块轮动 / 板块内选股）→ Streamlit 页面 + 回测 + 盘后扫描推送。

- **唯一在用的前端是 `app.py`**（Streamlit，7 页：全局信号总览 / 单股票分析 / 新闻列表 / 股票池管理 / 板块分析 / 自定义策略 / AI 分析助手）。
- **`api/server.py`（FastAPI）已弃用**——原本给微信小程序用，小程序不做了。讨论页面行为、信号链路、一致性时**只参照 `app.py`**，不要把它当另一套实现去对比或建议拉齐。
- 命令行入口：`main.py`（批量回测）、`scanner.py`（盘后扫描 + PushPlus 推送）、`purge_news.py`（清理窗口外新闻）。
- app 进程内还有三个 APScheduler 定时任务（03:00 清新闻、03:20 刷板块成分股缓存、15:40 板块资金流快照），见 `app.py::_start_background_jobs`。

## 2. 运行环境：只在 Docker 里跑

**禁止在宿主机直接 `python` / `python3`。** 宿主机的 python 是 2.x 且没有任何项目依赖（pandas / streamlit / akshare / torch 都在容器里），一跑就是语法或 ImportError，白费一轮调试。

| 资产 | 值 |
|---|---|
| 容器名 | `quant-app` |
| 镜像 | `quant-trading-app`（根目录 `Dockerfile` 构建） |
| Base | `python:3.12-slim` |
| 端口 | 8501（Streamlit）；8000 是已弃用的 FastAPI，compose 还启着 |
| 工作目录 | `/app`，宿主 `./` 通过 volume 挂进来——**改代码即时生效，不用重建** |
| 时区 | `Asia/Shanghai`（定时任务按本地时间触发） |
| Compose | `docker-compose.yml`（**不入 git**，见第 7 节） |

```bash
docker ps --filter name=quant-app                                    # 状态
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8501/_stcore/health   # 健康检查，期望 200
docker logs --tail 50 -f quant-app                                   # 日志
docker restart quant-app                                             # 改了模块结构/新增 import 后重启
docker exec quant-app python -c "..."                                # 临时验证
```

`docker restart` 会杀掉容器内进程——重启前确认没有正在跑的 `pip install` 或长任务。
数据缓存 `data/cache/market.duckdb` 通过挂载持久化，重启不丢。

## 3. 测试

**这个仓库不用 pytest / unittest，容器里也没装 pytest。** 每个测试文件是一个自带 runner 的普通脚本（末尾 `__main__` 里收集本文件所有 `test_*` 函数依次调用，全过就打印「全部 N 项测试通过!」）。

```bash
# 单个文件
docker exec quant-app python /app/tests/test_sector_cons.py

# 全量（27 个文件，当前应全绿）
for f in tests/test_*.py; do
  docker exec quant-app python /app/$f >/dev/null 2>&1 && echo "OK   $f" || echo "FAIL $f"
done
```

新增测试就照抄任一现有文件的骨架（顶部 `sys.path.insert(0, ...)` 指到仓库根 + 末尾的 runner）。

**三条铁律**（违反过、都疼过）：

1. **不打网络**：所有取数一律 mock。注意 `analysis/*` 有些模块在**顶层** `from data.fetcher import ...`，只 mock `data.fetcher` 不影响已绑定的引用，得直接打到调用方那个名字上。
2. **不碰生产数据**：DuckDB 指到临时库、股票池指到临时文件，别让测试写 `data/cache/market.duckdb` 或 `config/stocks.json`。
3. **不要 `import app`**：`app.py` 顶层就起调度器并执行页面路由，import 会真的拿**真实股票池**跑一遍总览页并打网络（实测触发 95 次板块快照请求）。要测页面用 `streamlit.testing.v1.AppTest`；只想读 `app.py` 里的模块级常量，按 `tests/test_overview_scale.py` 的做法用 AST 从源码读。

## 4. 装依赖

首选是改 `requirements.txt` 后重建镜像，**但当前重建是坏的**：

```
ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE
    unknown package:  Expected sha256 ...  Got <每次都不同>
```

`requirements.txt` 里没有任何 hash——这是 pip 拿**被截断的下载**去对索引登记的 hash。本环境到 pypi.org 取索引元数据约 7.2s/包（阿里云镜像 0.16s），100+ 依赖的长窗口里大 wheel 被限速到 50 kB/s 然后掐断（卡在 torch 的 CUDA 依赖 `nvidia_nvjitlink`）。**尚未采纳的解法**：给 Dockerfile 的 pip 层加 `-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --retries 10 --timeout 60`。

**日常绕法**（因为 `/app` 是挂载的，代码改动本来就不需要重建）：

```bash
docker exec quant-app pip install <pkg>     # 装在容器 fs 内，重启保留、重建丢失
```

同时把包写进 `requirements.txt` 兜底。

**可插拔组件的第三方 SDK 不进 `requirements.txt`、也不进 `Dockerfile`**：每家 SDK 只服务它自己那个 provider，焊进基础依赖等于让所有部署都背上用不到的包（`claude-agent-sdk` 一个 wheel 自带 262M 的 `_bundled/claude`），也和「加一个 provider = 丢一个文件、别处零改动」冲突。做法是基类上 `REQUIRES = ((import名, pip规格), ...)` + `check_requirements()`，用 `find_spec` 探测（不 `import`，避免副作用拖慢 UI），缺失时给一行可直接复制的 pip 命令——**不要提示「需重建镜像」**。样板见 `strategy/agent_gen/base.py`。

## 5. 开发流程

1. `git status` 确认工作区干净，脏的先处理，别把无关改动混进新需求；
2. **先搜历史 issue**：`gh issue list --state all --limit 60`。这个仓库的 issue **从不关闭**，等于一份项目日志，所以「归属正确」比「新建一条」有价值。某个功能当初是哪个 issue 加的，它的毛病往往就属于那一条；
3. **在 issue 里把设计做成定稿再动手**：细节自己用合理且可扩展的默认值定下来并说明取舍，**不留 open questions**；确实要拍板的核心歧义最多问一两个，别一次抛一堆选择题。较大/独立的需求开新 issue，小的功能增强直接归到相关旧 issue（`gh issue comment N -F file.md`）；
4. 实现，**在容器里测试**（第 3 节）；
5. 提交，message 末尾带 issue 号（第 6 节）。

**按需求原话的范围做。** 调研时顺手发现的 bug，在 issue 或回复里点出来**并附上实测代价**（如「95 个板块逐个 THS 快照 ≈ 70s」），让他据此决定要不要单开 issue——不要替他把「顺带做彻底」塞进当前这次改动。

## 6. 提交规范

- **身份**：`.git/config` 里刻意没有配 `user.name` / `user.email`（这台机器上还有另一份工作的身份，配进去会串）。每次提交显式传，不要改本地或全局配置：
  ```bash
  git -c user.name=... -c user.email=... commit -m "..."
  ```
  用哪个身份看仓库既有提交：`git log -1 --format='%an <%ae>'`。
- **不要 `Co-Authored-By`** 行。
- **message 一两句话**说清「改了什么 + 关键的一个坑」，**末尾带 `#N`**（多个用空格分隔，如 `... #28 #29`）。完整设计、实测数据、踩坑记录写到 **issue 评论**里，message 里指一句「详见 issue 评论 #N」即可。不要写几百字的长 message（git log 早期那些是反面教材）。
- **分支**：`feat/<slug>-<issue号>` 或 `fix/<slug>-<issue号>`，做完 merge 回 `main`，merge commit 形如 `Merge feat/sector-pick-35: ... #35`。纯清理类小改动可以直接落在 `main`（如 `37562fd`）。

## 7. 哪些文件不入 git

| 文件 | 原因 |
|---|---|
| `docker-compose.yml` | 含本机资源限制（`mem_limit`、日志上限），每台机器不一样；已 `git rm --cached` |
| `config/stocks.json` | 个人自选股票池/板块池，不是仓库通用配置 |
| `.env` | 密钥（`PUSHPLUS_TOKEN`、`ANTHROPIC_API_KEY`），模板见 `.env.example` |
| `data/cache/` | DuckDB + 各类 JSON 快照，全是本地产物 |
| `data/hf_cache/` | HuggingFace 权重缓存（Kronos） |
| `tests/kronos_repo/` | 第三方仓库 clone，自带 `.git`，误 add 会变成嵌套仓库 |
| `strategy/custom/_staging/`、`_rejected/` | AI 生成策略的中间产物；**已启用**的 `strategy/custom/*.py` 是正经产物，照常入 git |
| `.claude/settings.local.json` | 本机个人配置；而 `settings.json` / `agents/` / `commands/` / `skills/` 是项目级共享配置，照常提交 |

改了上面这些文件**不要建议提交**、也不要 `git add` 进来。确实要入 git 才用 `git add -f`。

另外，**个人账号信息（提交身份用的姓名/邮箱等）不要写进任何入 git 的文件**，只在命令行里用。

`.claude/` 那条的写法有个坑：必须写 `.claude/*` 而**不能**写 `.claude/`——整个目录被忽略时 git 根本不进目录，后面的 `!` 例外会全部失效（见 `37562fd` 与 #19 的评论）。

## 8. 代码约定

**分层与依赖方向**（目前是干净的，别破坏）：

- `data/` 只管取数与缓存，不做业务判断；
- `analysis/` 是纯计算、无副作用（同一份输入得同一份输出，不写文件不发请求）；
- `strategy/` → `backtest/` 单向依赖，不回头；
- `config/settings.py` 只依赖 stdlib + dotenv，任何模块都能 import 它而不产生循环。

**取回来的数据要带出处和新鲜度**：缓存类返回值必须给 `fetched_at` 与 `stale`，并且**一路传到页面上显示**。「过期的数据远好过没有」的前提是用户知道它过期了——参考 `data/sector_cons.py::get_sector_constituents`。

**不许静默截断**：凡是只算了一部分（top-N、跳过失败项、抽样），返回值要同时给总数与实算数（如 `total_constituents` / `computed`）并在页面照原样写出来。被静默丢掉的覆盖面会被读成「全都算过了」。

**东财 / 同花顺是窗口性限流源**：东财成分股接口实测同一分钟内 22 个 (分片, 协议) 组合只有 2 个通、两分钟后全挂、再过 40 秒又活。新增取数不要自己发明重试，照 `data/sector_cons.py` 的既有写法（分片×协议轮换 + 记住上次可用组合 + 退避 + 失败回落过期缓存）。板块**没有批量行情接口**（#32 的既定结论），逐个快照约 0.74s/个，涉及大量板块的功能必须把这个代价摆到用户面前。

**Streamlit 页面注意**：`st.expander` 折叠区里的内容照样会渲染、widget 一个不少，靠「收起来」并不能省渲染；真要省就在取数前过滤（见 `app.py::_scan_all_stocks` 的 `types` 参数）。
