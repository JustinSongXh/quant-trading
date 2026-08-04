"""Claude Agent SDK 的对话 provider（issue #36）

走宿主的 Claude Code OAuth 登录态（容器挂载 /root/.claude），CLI 由 claude-agent-sdk
wheel 自带（`_bundled/claude`，不需要 Node）——与 #30 的 codegen provider 同一条路，
那边的踩坑记录见 `strategy/agent_gen/providers/claude_sdk.py`。

## 与 #30 的关键差别：这里模型有工具，但只有**我们的**工具

#30 是「模型只读仓库、代码走正文交付」，所以 `allowed_tools=[]` + `can_use_tool` 当唯一
关卡。这里反过来：模型必须能调 `tools.py` 里那十一个工具，而**一个内置工具都不能有**。

- 自有工具经进程内 MCP（`create_sdk_mcp_server`）注册，写进 `allowed_tools`
- 内置工具（Read/Write/Edit/Bash/WebFetch/…）写进 `disallowed_tools` 全禁
- `setting_sources=[]`：不加载 ~/.claude/settings.json 与 .claude/settings.local.json
  ——本仓库那份带着 `allow: ["Bash(docker exec *)"]`，allow 规则会在权限回调之前放行工具，
  等于把宿主 docker 交给模型（#30 的原始踩坑）
- `skills=[]`：别让本地 skills 混进来
- `system_prompt` 直接传字符串，**不套 claude_code preset**：那份 preset 是给写代码的
  agent 用的，会带进一堆与本场景无关的行为约定

工具名前缀 `mcp__quant__<tool>` 是 SDK 的拼法，实测确认过（见 tests/test_chat_provider.py
的配置自检；`python -m analysis.chat.providers.claude_sdk` 可以直接打印出来核对）。
"""

import asyncio
import contextlib
import json
import logging
import warnings
from pathlib import Path

from analysis.chat import tools as chat_tools
from analysis.chat.base import Answer, ChatProvider
from analysis.chat.prompt import build_system_prompt

logger = logging.getLogger(__name__)

CREDENTIALS = Path("/root/.claude/.credentials.json")

MCP_SERVER = "quant"
TOOL_PREFIX = f"mcp__{MCP_SERVER}__"

# 内置工具一律禁掉。**两层，缺一不可**（都是实测逼出来的，见下）：
#
# 第一层 `disallowed_tools`（这个列表）：把已知内置工具从「模型看得见的工具清单」里摘掉。
# 实测有效——不写它时 Read/Bash 会出现在 init 消息的 tools 里；写了就消失。
#
# 第二层 `can_use_tool`（_gate_tools）：**结构性**地拒绝一切不在 allowed_tools 里的调用。
# 为什么必须有：把 Read/Write/Bash/... 全 disallow 之后，实测 init 消息里**仍有 19 个**
# 编排类工具（CronCreate/CronDelete/CronList/DesignSync/EnterWorktree/ExitWorktree/
# Monitor/PushNotification/RemoteTrigger/ReportFindings/ScheduleWakeup/SendMessage/
# ShareOnboardingGuide/TaskCreate/TaskGet/TaskList/TaskUpdate/ToolSearch/Workflow），
# 而且模型真的调得动 ToolSearch。里面 CronCreate 能在宿主上排定时任务、Workflow 能拉起
# 一堆 subagent 烧额度、ShareOnboardingGuide 会往外发东西——一个解读助手不该有这些。
# 这些名字是当前 CLI 版本的清单，下个版本可能再多几个，所以只靠列表必然漏。
# allowed_tools 里的工具会在 can_use_tool 之前被放行（#30 踩过），那正好：自有工具直通，
# 其余无论叫什么都落到回调里被拒。
BUILTIN_TOOLS = (
    # 文件 / 执行 / 联网
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "BashOutput",
    "KillShell", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "Agent",
    "TodoWrite", "ExitPlanMode", "EnterPlanMode", "SlashCommand", "Skill", "Artifact",
    # 编排 / 对外动作（实测出现在 init 清单里的那 19 个）
    "CronCreate", "CronDelete", "CronList", "DesignSync", "EnterWorktree",
    "ExitWorktree", "Monitor", "PushNotification", "RemoteTrigger", "ReportFindings",
    "ScheduleWakeup", "SendMessage", "ShareOnboardingGuide", "TaskCreate", "TaskGet",
    "TaskList", "TaskUpdate", "TaskStop", "TaskOutput", "ToolSearch", "Workflow",
    "AskUserQuestion", "EndConversation",
)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
# 一个回合里模型可能要连着调好几个工具（先 list_sectors 再 hotness 再 sector_pick），
# 每次工具返回都算一轮，10 轮够用又不至于让一次提问漫无边际地跑下去
MAX_TURNS = 10


class ClaudeSdkChatProvider(ChatProvider):
    key = "claude_sdk"
    name = "Claude Agent SDK"
    description = ("用宿主的 Claude Code 登录态（订阅额度，非 API key）。"
                   "只能调本系统的确定性工具，没有 Read/Bash/联网等内置工具。")
    PARAMS = [
        # 与 #30 一致：effort 是离散档位，但 PARAMS schema 只支持数值滑块
        # （前端复用 _render_strategy_params），用 1~5 映射到 low..max
        {"key": "effort_level", "label": "推理投入（1=low 5=max）", "default": 2,
         "min": 1, "max": 5, "step": 1},
    ]
    REQUIRES = (("claude_agent_sdk", "claude-agent-sdk>=0.2.128"),)

    EFFORTS = {1: "low", 2: "medium", 3: "high", 4: "xhigh", 5: "max"}

    def __init__(self, effort_level: int = 2):
        super().__init__(effort_level=effort_level)
        self.effort = self.EFFORTS.get(int(effort_level), DEFAULT_EFFORT)

    # ---------- 可用性 ----------

    @classmethod
    def available(cls) -> tuple[bool, str]:
        ok, reason = cls.check_requirements()
        if not ok:
            return ok, reason
        # 不查 which("claude")：CLI 由 wheel 自带，PATH 里本就不该有（#30 test_codegen_deps）
        if not CREDENTIALS.exists():
            return False, (
                f"找不到 {CREDENTIALS}：需把宿主 /root/.claude 挂进容器且可写"
                "（token 要刷新），改 volumes 后须 docker compose up -d 重建"
            )
        return True, ""

    # ---------- 回合 ----------

    def ask(self, user_text: str, session: dict | None = None) -> Answer:
        session = session or {}
        texts, trace, new_session_id, error = asyncio.run(
            self._query(user_text, session.get("session_id"))
        )
        if error:
            raise RuntimeError(error)
        return Answer(texts=texts, trace=trace,
                      session={"session_id": new_session_id or session.get("session_id")})

    async def _query(self, user_text: str, session_id: str | None):
        from claude_agent_sdk import (
            AssistantMessage, ClaudeAgentOptions, CanUseToolShadowedWarning,
            ResultMessage, TextBlock, query,
        )

        # SDK 会提醒「allowed_tools 里的工具在 can_use_tool 之前就被放行」。这里**是有意
        # 的**：allowed_tools 只有自有的 mcp__quant__* 只读工具，它们本来就该直通；回调
        # 存在的意义是拦住其余一切。#30 那边同样一条警告是真问题（它当时把 Read 放进了
        # allowed_tools，等于对任意路径畅通），所以那边删掉了静音；两处的区别只在
        # allowed_tools 里装的是什么，而「只装自有工具」由测试守着
        # （test_chat_provider.py::test_allowed_tools_are_own_tools_only）。
        warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

        trace: list[dict] = []
        options = build_options(self.effort, session_id, trace)

        texts: list[str] = []
        new_session_id: str | None = None
        error: str | None = None

        # can_use_tool 要求 streaming 输入模式：字符串 prompt 会直接 ValueError
        # （claude_agent_sdk/_internal/client.py）。单条消息也得包成 async generator，
        # 且 **yield 完必须挂住**——生成器一结束 SDK 就关掉 stdin，而权限请求是 CLI
        # 反向发来、要走这条通道应答的；关早了模型只会看到 "Stream closed"，那是通道断了
        # 导致的被动拒绝，不是闸门在把关（#30 白干过几个提交）。
        stay_open = asyncio.Event()

        async def prompt_stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_text},
                "parent_tool_use_id": None,
                "session_id": session_id or "",
            }
            await stay_open.wait()

        stream = query(prompt=prompt_stream(), options=options)
        try:
            async for message in stream:
                if isinstance(message, AssistantMessage):
                    if getattr(message, "error", None):
                        error = _readable_error(message.error)
                        break
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            texts.append(block.text)
                    if getattr(message, "session_id", None):
                        new_session_id = message.session_id
                elif isinstance(message, ResultMessage):
                    new_session_id = message.session_id or new_session_id
                    if message.is_error:
                        error = _readable_error(message.subtype, message.errors)
                    # streaming 输入下 CLI 认为会话还可能继续，发完 ResultMessage 也不
                    # 退出，不 break 就会挂死在 async for 上（配合上面挂住的生成器）
                    break
        finally:
            stay_open.set()   # 先放开挂住的输入生成器，它才能正常收尾
            # 再显式关掉 SDK 的输出生成器：上面 break 出来时它悬在半路，留给
            # asyncio.run 的 shutdown_asyncgens 去收会打出
            # 「RuntimeError: aclose(): asynchronous generator is already running」
            # （实测每轮都有一行，功能不受影响，但那行足以让人以为调用失败了）
            with contextlib.suppress(Exception):
                await stream.aclose()

        if not texts and not error:
            error = f"模型没有返回任何文本（可能已达 max_turns={MAX_TURNS} 或被中断）"
        return texts, trace, new_session_id, error


def build_options(effort: str = DEFAULT_EFFORT, session_id: str | None = None,
                  trace: list[dict] | None = None):
    """组装 ClaudeAgentOptions。**单独抽出来是为了能被测试直接检查**——这几个字段就是
    这一片的安全配置，#30 的教训是「配置看着对、实际没生效」，所以它必须可断言。"""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        model=DEFAULT_MODEL,
        effort=effort,
        max_turns=MAX_TURNS,
        resume=session_id,
        # 纯字符串：不要套 claude_code preset（那是写代码 agent 的行为约定）
        system_prompt=build_system_prompt(),
        mcp_servers={MCP_SERVER: build_mcp_server(trace if trace is not None else [])},
        allowed_tools=list(allowed_tool_names()),
        disallowed_tools=list(BUILTIN_TOOLS),
        can_use_tool=_gate_tools,          # 第二层：一切非自有工具结构性拒绝
        # 见模块 docstring：本地 settings 里的 allow 规则会在回调之前放行工具
        setting_sources=[],
        skills=[],
    )


# ========== 工具适配：ToolSpec → SDK MCP ==========

def allowed_tool_names() -> tuple[str, ...]:
    """自有工具的完整名（`mcp__quant__<tool>`）。allowed_tools 只放这些。"""
    return tuple(f"{TOOL_PREFIX}{name}" for name in chat_tools.TOOL_NAMES)


def build_mcp_server(trace: list[dict]):
    """把 provider 无关的 ToolSpec 包成进程内 MCP server。

    `trace` 是本回合的轨迹收集器：**每次真实执行都在这里记一条**（工具名、参数、返回
    行数）。不从模型返回的 ToolUseBlock 里推——那是模型「说」它调了什么，轨迹要的是
    实际执行了什么（这正是对「解释不忠实」的对策）。
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    sdk_tools = []
    for spec in chat_tools.TOOLS:
        sdk_tools.append(_wrap(spec, trace, tool))
    return create_sdk_mcp_server(MCP_SERVER, tools=sdk_tools)


def _wrap(spec, trace: list[dict], tool_decorator):
    """单个 ToolSpec → SDK 工具。handler 是同步纯函数，异常已在 tools.call() 里兜住。"""

    @tool_decorator(spec.name, spec.description, spec.input_schema)
    async def _handler(args: dict) -> dict:
        payload = chat_tools.call(spec.name, args)
        record = {"tool": spec.name, "input": dict(args or {}),
                  "rows": chat_tools.count_rows(payload)}
        if payload.get("error"):
            record["error"] = payload["error"]
        elif payload.get("available") is False:
            record["error"] = payload.get("hint", "数据不可用")
        trace.append(record)
        return {"content": [{"type": "text", "text": chat_tools.to_text(payload)}]}

    return _handler


def check_tool(tool_name: str) -> str | None:
    """第二层闸门的判定：自有工具放行（返回 None），其余一律拒绝并给出原因。

    单独抽出来是为了能不起模型直接单测（同 #30 的 check_tool）。判定只看名字前缀，
    不维护黑名单——新版本 CLI 冒出来的工具默认就是拒绝，而不是默认可用。
    """
    if tool_name in allowed_tool_names():
        return None
    return (f"不允许使用 {tool_name}。你只能调用 {TOOL_PREFIX}* 这些本系统的数据工具"
            f"（共 {len(chat_tools.TOOL_NAMES)} 个）。没有文件、命令、联网、定时任务、"
            f"子任务编排类工具——数据取不到就如实告诉用户，不要绕路。")


async def _gate_tools(tool_name: str, tool_input: dict, context) -> object:
    """第二层闸门：allowed_tools 里的自有工具会直通此回调之前，所以到这儿的都该拒。"""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    reason = check_tool(tool_name)
    if reason is None:
        return PermissionResultAllow()
    logger.warning("chat 闸门拒绝 %s(%s)：%s", tool_name,
                   json.dumps(tool_input, ensure_ascii=False, default=str)[:150], reason)
    return PermissionResultDeny(message=reason)


def _readable_error(kind: str, details: list[str] | None = None) -> str:
    """SDK 的错误标识翻成能摆给用户看的话（限流不要甩堆栈，同 #30）"""
    mapping = {
        "authentication_failed": (
            "Claude Code 登录态失效：请在宿主机重新 claude login"
            "（容器挂载的 /root/.claude 必须可写，token 需要刷新）"
        ),
        "rate_limit": "已达订阅限流，等额度恢复后再试",
        "billing_error": "账号计费异常，请检查订阅状态",
        "invalid_request": "请求被拒绝（可能是对话过长或参数不合法）",
        "server_error": "Anthropic 服务端错误，稍后重试",
        "error_max_turns": f"单回合工具调用超过 {MAX_TURNS} 轮仍未收敛，请把问题问得更具体",
    }
    msg = mapping.get(kind, f"模型调用失败：{kind}")
    if details:
        msg = f"{msg}（{'; '.join(details[:2])}）"
    return msg


if __name__ == "__main__":      # 手工核对工具全名前缀（issue 二的实测要求之一）
    print(json.dumps({"allowed_tools": list(allowed_tool_names()),
                      "disallowed_tools": list(BUILTIN_TOOLS)},
                     ensure_ascii=False, indent=2))
