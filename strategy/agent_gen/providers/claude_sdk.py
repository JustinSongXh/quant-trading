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

# 只读工具。写操作一律不给——provider 不碰文件系统，落盘由 orchestrator 经 store 做。
READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]

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
            allowed_tools=READ_ONLY_TOOLS,
            can_use_tool=_deny_all_writes,
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
        async def prompt_stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_text},
                "parent_tool_use_id": None,
                "session_id": session_id or "",
            }

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

        if not texts and not error:
            error = "模型没有返回任何文本（可能已达 max_turns 或被中断）"
        return texts, new_session_id, error


async def _deny_all_writes(tool_name: str, tool_input: dict, context) -> object:
    """闸门 1：拒绝一切非只读工具。

    provider 只返回代码文本，落盘全部由 store.py 做，路径由我们拼。所以这里不需要
    路径白名单——直接拒绝写操作，攻击面最小。

    注意 allowed_tools 里的工具（Read/Grep/Glob）会在本回调之前被自动放行，
    因此这里实际只会收到白名单外的调用。这正是想要的分工，SDK 会为此发一条
    CanUseToolShadowedWarning（见 _install_warning_filter）。
    """
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    if tool_name in READ_ONLY_TOOLS:
        return PermissionResultAllow()

    logger.info("闸门1 拒绝工具调用：%s(%s)", tool_name, json.dumps(tool_input)[:120])
    return PermissionResultDeny(
        message=(
            f"不允许使用 {tool_name}。你只有 Read / Grep / Glob 三个只读工具。"
            "策略代码请写在回复正文的 python 代码块里交付，不要尝试写文件或执行命令。"
        )
    )


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


def _install_warning_filter() -> None:
    """静音 CanUseToolShadowedWarning。

    allowed_tools 列了 Read/Grep/Glob，SDK 会提醒「这三个不会走 can_use_tool」——
    对我们是有意设计（只读走快路径，回调兜住其余），不是配置失误。不静音会在
    Streamlit 日志里反复刷。
    """
    import warnings

    try:
        from claude_agent_sdk import CanUseToolShadowedWarning
    except ImportError:
        return
    warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)


_install_warning_filter()
