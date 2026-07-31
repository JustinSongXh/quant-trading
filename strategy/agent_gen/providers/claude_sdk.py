"""Claude Agent SDK provider（#30）

走宿主的 Claude Code OAuth 登录态（容器挂载 /root/.claude），经由镜像内的 claude
CLI。裸 OAuth token 直打 /v1/messages 稳定 429，只在 CLI 客户端路径下被接受——
见 #30 第一条评论，那条路已排除，不要再加回来当 fallback。

无状态多轮：每回合一次 query()，session_id 存进 Turn.session，下一轮用 resume 续上。
**不用 ClaudeSDKClient 长连接**——Streamlit 每次交互重跑整个脚本，长连接会与 rerun
打架（#27 已在情绪模型上踩过多线程竞态）。
"""

import asyncio
import json
import logging
import shutil
from pathlib import Path

from strategy.agent_gen.base import CodegenProvider, Turn
from strategy.agent_gen.prompt import build_system_prompt

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
CREDENTIALS = Path("/root/.claude/.credentials.json")

# 允许的只读工具。写操作一律不给——provider 不碰文件系统，落盘由 orchestrator 经
# store 做。注意这三个**不放进 allowed_tools**：allowed_tools 里的工具会在
# can_use_tool 之前被自动放行，那样 Read 就对任意绝对路径畅通了。实测能读到
# /etc/hostname，也就意味着能读容器里挂着的 /root/.claude（凭证 + 宿主 5 个项目的
# 会话记录）。所以让回调成为唯一关卡，在这里逐次校验路径。
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")

# 各工具里代表路径的入参名。Read 用 file_path，Grep/Glob 用 path（可省略，
# 省略时以 cwd 为根，也就是仓库本身）。
PATH_ARGS = ("file_path", "path", "notebook_path")

# 仓库内也不给读的：都是不进 git 的本机/个人配置
DENIED_IN_REPO = (
    ".env",              # PUSHPLUS_TOKEN / ANTHROPIC_API_KEY
    "config/stocks.json",  # 个人自选股
    ".claude",           # 本机 Claude Code 配置与权限规则
)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
MAX_TURNS = 15


class ClaudeSdkProvider(CodegenProvider):
    key = "claude_sdk"
    name = "Claude Agent SDK"
    description = (
        "用宿主的 Claude Code 登录态（订阅额度，非 API key）。"
        "能只读地 Read / Grep 本仓库，照着真实接口和既有策略的风格写。"
    )
    PARAMS = [
        # effort 是离散档位，但 PARAMS schema 只支持数值滑块（前端复用
        # _render_strategy_params）。用 1~5 映射到 low..max，避免为一个参数
        # 去改前端的渲染器。
        {"key": "effort_level", "label": "推理投入（1=low 5=max）", "default": 4,
         "min": 1, "max": 5, "step": 1},
    ]

    EFFORTS = {1: "low", 2: "medium", 3: "high", 4: "xhigh", 5: "max"}

    def __init__(self, effort_level: int = 4):
        super().__init__(effort_level=effort_level)
        self.effort = self.EFFORTS.get(int(effort_level), DEFAULT_EFFORT)

    # ---------- 可用性 ----------

    @classmethod
    def available(cls) -> tuple[bool, str]:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            return False, "未安装 claude-agent-sdk（requirements.txt 已列，需重建镜像）"
        if shutil.which("claude") is None:
            return False, "PATH 里找不到 claude CLI（镜像需装 @anthropic-ai/claude-code）"
        if not CREDENTIALS.exists():
            return False, (
                f"找不到 {CREDENTIALS}：需把宿主 /root/.claude 挂进容器且可写"
                "（token 要刷新），改 volumes 后须 docker compose up -d 重建"
            )
        return True, ""

    # ---------- 回合 ----------

    def start(self, user_text: str) -> Turn:
        return self._run(user_text, session_id=None)

    def resume(self, session: dict, user_text: str) -> Turn:
        return self._run(user_text, session_id=session.get("session_id"))

    def _run(self, user_text: str, session_id: str | None) -> Turn:
        """跑一个回合。Streamlit 是同步的，每回合用 asyncio.run 包一次。"""
        texts, new_session_id, error = asyncio.run(self._query(user_text, session_id))

        if error:
            raise RuntimeError(error)

        session = {"session_id": new_session_id or session_id}
        return self.to_turn(texts, session)

    async def _query(self, user_text: str, session_id: str | None):
        from claude_agent_sdk import (
            AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query,
        )

        options = ClaudeAgentOptions(
            cwd=str(REPO_ROOT),
            model=DEFAULT_MODEL,
            effort=self.effort,
            max_turns=MAX_TURNS,
            resume=session_id,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": build_system_prompt(),
            },
            # 刻意留空：任何工具调用都必须过 can_use_tool，没有旁路
            allowed_tools=[],
            can_use_tool=_gate_tools,
            # 闸门 1 的关键一环：不加载任何本地 settings。
            # setting_sources=None（默认）会读 ~/.claude/settings.json 与
            # <cwd>/.claude/settings.local.json —— 本仓库那份就带着
            # allow: ["Bash(docker exec *)"]，而 allow 规则在 can_use_tool
            # 之前放行工具，等于把宿主 docker 交给模型。空列表 = SDK 隔离模式。
            # 代价：不加载 CLAUDE.md（本来也不需要，契约由 prompt.py 提供）。
            setting_sources=[],
            # 已经显式给了 allowed_tools + can_use_tool，不要再让本地 skills 混进来
            skills=[],
        )

        texts: list[str] = []
        new_session_id: str | None = None
        error: str | None = None

        # 流式 prompt：can_use_tool 回调要求 streaming mode，字符串 prompt 会直接
        # ValueError（claude_agent_sdk/_internal/client.py:105）。单条消息也得包成
        # async generator。
        #
        # yield 完必须挂住不返回：生成器一结束，SDK 就关掉 stdin，而 can_use_tool 的
        # 控制请求是 CLI 反向发来、要走这条通道应答的。关早了模型侧只会看到
        # "Tool permission request failed: AbortError: Stream closed"——工具确实没跑成，
        # 但那是通道断了导致的被动拒绝，不是闸门在把关（回调压根没被调用）。
        # 整轮结束后 async for 退出，生成器随之被关闭回收。
        stay_open = asyncio.Event()

        async def prompt_stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_text},
                "parent_tool_use_id": None,
                "session_id": session_id or "",
            }
            await stay_open.wait()

        try:
            async for message in query(prompt=prompt_stream(), options=options):
                if isinstance(message, AssistantMessage):
                    if message.error:
                        error = _readable_error(message.error)
                        break
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            texts.append(block.text)
                    if message.session_id:
                        new_session_id = message.session_id
                elif isinstance(message, ResultMessage):
                    new_session_id = message.session_id or new_session_id
                    if message.is_error:
                        error = _readable_error(message.subtype, message.errors)
                    # 一轮到此结束，必须主动 break：streaming 输入模式下 CLI 认为
                    # 会话还可能继续，发完 ResultMessage 也不退出，不 break 就会
                    # 一直挂在 async for 上（配合上面挂住的生成器直接死等）。
                    break
        finally:
            stay_open.set()   # 放开挂住的生成器，让它正常收尾

        if not texts and not error:
            error = "模型没有返回任何文本（可能已达 max_turns 或被中断）"
        return texts, new_session_id, error


def check_tool(tool_name: str, tool_input: dict) -> str | None:
    """闸门 1 的判定逻辑：放行返回 None，拒绝返回给模型看的原因。

    单独抽出来是为了能不起模型直接单测（见 tests/test_codegen_permission.py）。

    两条规则：
    1. 只有 Read / Grep / Glob 三个只读工具可用，其余（Write/Edit/Bash/WebFetch…）全拒
    2. 这三个的路径参数必须落在本仓库内，且不在 DENIED_IN_REPO 里

    第 2 条是必需的：容器挂了宿主的 /root/.claude，里面是 OAuth 凭证和 5 个项目的
    Claude Code 会话记录。不限路径的话，一句「读一下那个文件」就能把它们打进聊天框。
    """
    if tool_name not in READ_ONLY_TOOLS:
        return (f"不允许使用 {tool_name}。你只有 Read / Grep / Glob 三个只读工具。"
                "策略代码请写在回复正文的 python 代码块里交付，"
                "不要尝试写文件或执行命令。")

    for arg in PATH_ARGS:
        raw = tool_input.get(arg)
        if raw in (None, ""):
            continue          # Grep/Glob 省略 path 时以 cwd（仓库）为根，放行
        reason = _check_path(str(raw))
        if reason is not None:
            return reason
    return None


def _check_path(raw: str) -> str | None:
    """路径必须解析后仍在仓库内。resolve() 会展开 .. 与符号链接，
    所以 `../../etc/passwd` 和「仓库里指向外面的软链」都会被拦下。"""
    try:
        target = Path(raw)
        if not target.is_absolute():
            target = REPO_ROOT / target
        target = target.resolve()
    except (OSError, RuntimeError) as e:
        return f"路径 {raw} 无法解析（{type(e).__name__}），已拒绝。"

    if not (target == REPO_ROOT or target.is_relative_to(REPO_ROOT)):
        return (f"不允许访问仓库之外的路径（{raw}）。你只能读 {REPO_ROOT} 里的文件——"
                "本机还挂载着与本任务无关的私有数据。")

    rel = target.relative_to(REPO_ROOT).as_posix() if target != REPO_ROOT else ""
    for denied in DENIED_IN_REPO:
        if rel == denied or rel.startswith(denied + "/"):
            return (f"不允许访问 {rel}（本机/个人配置，与生成策略无关）。"
                    "策略契约所需的接口在 strategy/trade_strategy.py 和 backtest/broker.py 里。")
    return None


async def _gate_tools(tool_name: str, tool_input: dict, context) -> object:
    """闸门 1：唯一的工具关卡（allowed_tools 留空，没有旁路）"""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    reason = check_tool(tool_name, tool_input)
    if reason is None:
        return PermissionResultAllow()

    logger.warning("闸门1 拒绝 %s(%s)：%s",
                   tool_name, json.dumps(tool_input, ensure_ascii=False)[:150], reason)
    return PermissionResultDeny(message=reason)


def _readable_error(kind: str, details: list[str] | None = None) -> str:
    """把 SDK 的错误标识翻成能摆给用户看的话（issue 十一.3：限流不要甩堆栈）"""
    mapping = {
        "authentication_failed": (
            "Claude Code 登录态失效：请在宿主机重新 claude login"
            "（容器挂载的 /root/.claude 必须可写，token 需要刷新）"
        ),
        "rate_limit": "已达订阅限流（Max 5x），等额度恢复后再试",
        "billing_error": "账号计费异常，请检查订阅状态",
        "invalid_request": "请求被拒绝（可能是 prompt 过长或参数不合法）",
        "server_error": "Anthropic 服务端错误，稍后重试",
        "error_max_turns": f"单回合工具调用超过 {MAX_TURNS} 轮仍未收敛",
    }
    msg = mapping.get(kind, f"模型调用失败：{kind}")
    if details:
        msg = f"{msg}（{'; '.join(details[:2])}）"
    return msg


# 曾经这里有个 CanUseToolShadowedWarning 的静音 filter：当时 allowed_tools 列了
# Read/Grep/Glob，SDK 会提醒「这三个不走 can_use_tool」，我把它当成有意设计静音掉了。
# 那条警告是对的——它正是「Read 对任意路径畅通」这个洞的提示。现在 allowed_tools
# 留空、回调是唯一关卡，警告不再产生，filter 也就不需要了。
