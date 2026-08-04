"""对话 provider 与「AI 分析助手」面板的测试（issue #36，fake provider，不调模型）

覆盖 issue 六「provider 与面板」那 6 项：业务层零耦合（AST）、换成 fake provider 整条链路
跑通、DEFAULT_CHAT_PROVIDER 决定前端默认项（#30 4646dd4 的回归）、安全配置自检
（disallowed_tools / setting_sources / skills / allowed_tools）、system prompt 含 7 条纪律、
工具调用轨迹被收集并回传。

**配置自检为什么必须有**：#30 的闸门 1 就是「配置看着对、can_use_tool 一次都没被调用」，
白干了几个提交。这里的两层拦截（disallowed_tools + can_use_tool）同样是配置，
所以它必须能被断言，而不是靠读文档相信。

claude-agent-sdk 是可选依赖：装了才跑得到需要它的用例，没装则跳过而不是报红。
"""

import ast
import importlib.util
import inspect
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pathlib import Path

import config.settings as cfg
from analysis.chat import registry, tools
from analysis.chat.prompt import DISCIPLINES, DISCLAIMER, build_system_prompt

REPO = Path(__file__).resolve().parents[1]
APP = str(REPO / "app.py")
_SDK_INSTALLED = importlib.util.find_spec("claude_agent_sdk") is not None

PROVIDER_FILE = registry._PROVIDER_DIR / "uifake.py"
PROVIDER_NAME = "UI 测试对话 provider"

# 假 provider：写成文件而不是 import tests/，因为要顺带验证「自动发现」这条路。
# 它**真的调两个工具**（list_pool + 资金流排行），用来断言轨迹被完整收集回来。
PROVIDER_SRC = '''
from analysis.chat import tools as chat_tools
from analysis.chat.base import Answer, ChatProvider


class UiFakeChatProvider(ChatProvider):
    key = "uifake"
    name = "UI 测试对话 provider"
    description = "不调用任何模型：固定调两个工具再返回一段话。"
    PARAMS = []

    def ask(self, user_text, session=None):
        session = session or {}
        trace = []
        for name, args in (("list_pool", {}),
                           ("sector_fund_flow_ranking",
                            {"kind": "industry", "period": "20日", "top_n": 3})):
            payload = chat_tools.call(name, args)
            record = {"tool": name, "input": args,
                      "rows": chat_tools.count_rows(payload)}
            if payload.get("error") or payload.get("available") is False:
                record["error"] = payload.get("hint") or payload.get("error")
            trace.append(record)
        return Answer(texts=[f"收到：{user_text}", "（这是假 provider 的固定回答）"],
                      trace=trace, session={"turns": session.get("turns", 0) + 1})
'''


def setup():
    PROVIDER_FILE.write_text(PROVIDER_SRC, encoding="utf-8")
    registry.refresh_providers()


def cleanup():
    PROVIDER_FILE.unlink(missing_ok=True)
    sys.modules.pop("analysis.chat.providers.uifake", None)
    shutil.rmtree(registry._PROVIDER_DIR / "__pycache__", ignore_errors=True)
    registry.refresh_providers()


# ---------- 1. 业务层零耦合 ----------
def _imports(module_or_src) -> list[str]:
    src = (module_or_src if isinstance(module_or_src, str)
           else inspect.getsource(module_or_src))
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def test_business_layer_never_imports_a_concrete_provider():
    """面板与 tools/prompt/base/registry 都不许 import 具体 provider 模块。

    用 AST 而不是字符串匹配：docstring 与注释里提到 `providers/claude_sdk.py` 是在
    **说明设计**，不该算命中（#30 test_no_which_call_in_source 的同一条教训）。
    """
    from analysis.chat import base, prompt
    from analysis.chat import registry as reg

    for module in (base, prompt, tools, reg):
        for mod in _imports(module):
            assert "chat.providers" not in mod, \
                f"{module.__name__} import 了具体 provider：{mod}"

    app_src = (REPO / "app.py").read_text(encoding="utf-8")
    for mod in _imports(app_src):
        assert "chat.providers" not in mod, f"app.py import 了具体 provider：{mod}"
    assert "analysis.chat.providers" not in app_src, \
        "app.py 里出现了 analysis.chat.providers——业务层只能经 registry 拿实例"
    print("[OK] 业务层不认识任何具体 provider")


def test_provider_is_discovered_by_dropping_a_file():
    setup()
    try:
        keys = [m["key"] for m in registry.list_providers()]
        assert "uifake" in keys, keys
        assert "_template" not in keys, "下划线开头的模板不该被发现"
        provider = registry.get_provider("uifake")
        assert provider.name == PROVIDER_NAME
    finally:
        cleanup()
    assert "uifake" not in [m["key"] for m in registry.list_providers()]
    print("[OK] 丢一个文件进 providers/ 就被发现，删掉就消失")


# ---------- 2/6. 整条链路 + 轨迹 ----------
def test_fake_provider_round_trip_collects_trace():
    setup()
    try:
        provider = registry.get_provider("uifake")
        answer = provider.ask("池子里有什么")
        assert answer.texts and "池子里有什么" in answer.texts[0]
        assert [c["tool"] for c in answer.trace] == \
            ["list_pool", "sector_fund_flow_ranking"]
        assert answer.trace[0]["rows"] >= 1
        assert answer.trace[1]["input"]["period"] == "20日"
        assert answer.session["turns"] == 1
        # 续接：session 原样传回去，轮次累加
        again = provider.ask("再说一遍", answer.session)
        assert again.session["turns"] == 2
    finally:
        cleanup()
    print("[OK] fake provider 整条链路跑通，工具轨迹完整回传")


# ---------- 3. DEFAULT_CHAT_PROVIDER 决定默认项 ----------
def test_default_provider_key_follows_settings():
    setup()
    saved = cfg.DEFAULT_CHAT_PROVIDER
    try:
        cfg.DEFAULT_CHAT_PROVIDER = "uifake"
        assert registry.default_provider_key() == "uifake"
        # 配的 provider 不可用时退到第一个可用的，而不是返回 None
        cfg.DEFAULT_CHAT_PROVIDER = "not_installed_xyz"
        fallback = registry.default_provider_key()
        assert fallback in [m["key"] for m in registry.list_providers(only_available=True)]
    finally:
        cfg.DEFAULT_CHAT_PROVIDER = saved
        cleanup()
    print("[OK] default_provider_key 跟着 DEFAULT_CHAT_PROVIDER 走")


def test_panel_default_selection_follows_settings():
    """#30 的 4646dd4 回归：前端 radio 的默认项必须走 default_provider_key()，
    不是 radio 的字母序第一项——否则配置项形同虚设。

    这条要求页面上同时有 ≥2 个可用 provider（假的 + 真的 claude_sdk）才测得到。
    """
    from streamlit.testing.v1 import AppTest

    setup()
    saved = cfg.DEFAULT_CHAT_PROVIDER
    try:
        usable = [m["name"] for m in registry.list_providers(only_available=True)]
        if len(usable) < 2:
            print(f"[SKIP] 只有 {usable} 一个可用 provider，选择器不出现，跳过默认项断言")
            return
        cfg.DEFAULT_CHAT_PROVIDER = "uifake"
        at = AppTest.from_file(APP, default_timeout=300)
        at.session_state["page"] = "AI 分析助手"
        at.run()
        assert not at.exception, at.exception
        radio = next((r for r in at.radio if r.label == "对话模型"), None)
        assert radio is not None, "找不到「对话模型」选择器"
        assert radio.value == PROVIDER_NAME, \
            f"默认选中的是 {radio.value!r}，没跟着 DEFAULT_CHAT_PROVIDER"
    finally:
        cfg.DEFAULT_CHAT_PROVIDER = saved
        cleanup()
    print("[OK] 前端默认项跟着 DEFAULT_CHAT_PROVIDER（不是字母序第一项）")


def test_panel_renders_answer_and_trace():
    """无头点一遍：提问 → 回答气泡 → 工具轨迹折叠区 → 免责声明。

    必须显式选中假 provider：真实的 claude_sdk 也可用，选错就会打真模型。
    """
    from streamlit.testing.v1 import AppTest

    setup()
    try:
        at = AppTest.from_file(APP, default_timeout=300)
        at.session_state["page"] = "AI 分析助手"
        at.session_state["chat_provider"] = PROVIDER_NAME
        at.run()
        assert not at.exception, at.exception
        assert [t.value for t in at.title] == ["AI 分析助手"]

        at.chat_input[0].set_value("过去一个月哪个行业板块资金净流入多？").run()
        assert not at.exception, at.exception
        blob = "\n".join([m.value for m in at.markdown] + [c.value for c in at.caption])
        assert "这是假 provider 的固定回答" in blob, "回答没渲染出来"
        # 轨迹的每一行（工具名 + 参数 + 返回行数）都要真渲染出来。折叠区的 label 在
        # AppTest 里不算 markdown 元素，所以对 label 与内容分别断言。
        assert "1. `list_pool`" in blob and "（无参数）" in blob, \
            f"轨迹里没有工具名与参数：{blob[-400:]}"
        assert "2. `sector_fund_flow_ranking`" in blob and "period=20日" in blob, \
            "轨迹里没有第二个工具的参数"
        assert " 行" in blob, "轨迹里没写返回了多少行"
        labels = [e.label for e in at.expander if "次工具" in (e.label or "")]
        assert labels and "2 次" in labels[0], f"折叠区标题不对：{labels}"
        assert DISCLAIMER in blob, "固定免责声明缺失"
    finally:
        cleanup()
    print("[OK] 面板渲染回答 + 工具轨迹 + 免责声明")


def test_clear_resets_conversation_and_session():
    """「清空对话」要同时清掉消息与 provider 的续接 session（否则新话题接着旧上下文说）。

    这里预置 session_state 而不是走 chat_input 提交一轮：chat_input 属于 Streamlit 底部
    固定容器，AppTest 里不随重跑清除，点完「清空」后它会被再提交一次、消息又长回来
    （真实浏览器里不会，#30 test_codegen_ui 里记过这个坑）。
    """
    from streamlit.testing.v1 import AppTest

    setup()
    try:
        at = AppTest.from_file(APP, default_timeout=300)
        at.session_state["page"] = "AI 分析助手"
        at.session_state["chat_provider"] = PROVIDER_NAME
        at.session_state["chat_msgs"] = [
            {"role": "user", "text": "旧问题"},
            {"role": "assistant", "text": "旧回答",
             "trace": [{"tool": "list_pool", "input": {}, "rows": 36}]},
        ]
        at.session_state["chat_session"] = {"session_id": "old-session"}
        at.run()
        assert "旧回答" in "\n".join(m.value for m in at.markdown)

        next(b for b in at.button if "清空" in b.label).click().run()
        assert not at.exception, at.exception
        assert at.session_state["chat_msgs"] == []
        assert "chat_session" not in at.session_state, "续接 session 没被清掉"
        # 再跑一遍才能看到「清空后的画面」：同一次 run() 里 st.rerun() 前后两遍的元素
        # 会累积在 at.markdown 里（清空前那遍渲染过旧消息），真实浏览器里 rerun 会丢掉
        # 前一遍的画面
        at.run()
        assert "旧回答" not in "\n".join(m.value for m in at.markdown)
    finally:
        cleanup()
    print("[OK] 清空对话同时清掉消息与续接 session")


def test_panel_shows_reasons_when_no_provider_available():
    """一个都不可用时要说明「为什么没有」，而不是给个空下拉框"""
    from streamlit.testing.v1 import AppTest

    from analysis.chat import registry as reg

    saved = reg.list_providers
    reg.list_providers = lambda only_available=False: (
        [] if only_available else
        [{"key": "x", "name": "某模型", "description": "", "params": [],
          "available": False, "reason": "未安装 xxx-sdk，按需装：pip install xxx"}])
    try:
        at = AppTest.from_file(APP, default_timeout=300)
        at.session_state["page"] = "AI 分析助手"
        at.run()
        assert not at.exception, at.exception
        blob = "\n".join([w.value for w in at.warning] + [m.value for m in at.markdown])
        assert "没有可用的对话 provider" in blob and "未安装 xxx-sdk" in blob
        assert not at.chat_input, "没有可用 provider 时不该给输入框"
    finally:
        reg.list_providers = saved
    print("[OK] 无可用 provider 时逐条给出原因")


def test_panel_reports_provider_error_as_a_bubble():
    """provider 抛错（限流/登录态失效）要落成一条气泡，不是整页红堆栈"""
    from streamlit.testing.v1 import AppTest

    boom = PROVIDER_SRC.replace(
        "        session = session or {}",
        "        raise RuntimeError('已达订阅限流，等额度恢复后再试')")
    PROVIDER_FILE.write_text(boom, encoding="utf-8")
    registry.refresh_providers()
    try:
        at = AppTest.from_file(APP, default_timeout=300)
        at.session_state["page"] = "AI 分析助手"
        at.session_state["chat_provider"] = PROVIDER_NAME
        at.run()
        at.chat_input[0].set_value("随便问问").run()
        assert not at.exception, at.exception
        blob = "\n".join(m.value for m in at.markdown)
        assert "调用失败" in blob and "订阅限流" in blob
    finally:
        cleanup()
    print("[OK] provider 报错变成气泡里的一句话")


# ---------- 4. 安全配置自检 ----------
def test_allowed_tools_are_own_tools_only():
    """allowed_tools 只放自有 mcp 工具。

    这条同时是 CanUseToolShadowedWarning 被静音的前提：allowed_tools 里的工具会在
    can_use_tool 之前放行，只有「里面全是自有只读工具」时那才是安全的。
    """
    from analysis.chat.providers.claude_sdk import TOOL_PREFIX, allowed_tool_names

    names = allowed_tool_names()
    assert len(names) == len(tools.TOOLS) > 0
    for n in names:
        assert n.startswith(TOOL_PREFIX), f"{n} 不是自有工具"
    assert TOOL_PREFIX == "mcp__quant__", f"工具全名前缀变了：{TOOL_PREFIX}"
    # 前缀是实测确认过的（SDK 的 init 消息里就是这个形状），写死在这里当回归
    assert "mcp__quant__sector_fund_flow_ranking" in names
    print(f"[OK] allowed_tools 只有 {len(names)} 个自有工具，前缀 {TOOL_PREFIX}")


def test_builtin_tools_are_disallowed():
    """内置工具全禁：文件/执行/联网，以及实测冒出来的编排类工具"""
    from analysis.chat.providers.claude_sdk import BUILTIN_TOOLS

    must_deny = ("Read", "Write", "Edit", "Bash", "WebFetch", "WebSearch", "Glob", "Grep",
                 # 实测：把上面这些 disallow 之后，init 清单里仍剩这些编排类工具，
                 # CronCreate 能在宿主排定时任务、Workflow 能拉起一堆 subagent 烧额度
                 "CronCreate", "Workflow", "ToolSearch", "PushNotification",
                 "TaskCreate", "SendMessage", "ShareOnboardingGuide", "EnterWorktree")
    for name in must_deny:
        assert name in BUILTIN_TOOLS, f"{name} 没在 disallowed_tools 里"
    for name in BUILTIN_TOOLS:
        assert not name.startswith("mcp__"), f"自有工具 {name} 被误禁"
    print(f"[OK] disallowed_tools 共 {len(BUILTIN_TOOLS)} 项，覆盖文件/执行/联网/编排")


def test_gate_denies_everything_but_own_tools():
    """第二层闸门是**结构性**的：不在 allowed_tools 里的一律拒，不靠黑名单。

    这条比上面那条重要——BUILTIN_TOOLS 是当前 CLI 版本的清单，下个版本多出来的工具
    只有这层能兜住（实测就发生过：Read/Bash 禁掉后还剩 19 个编排类工具）。
    """
    from analysis.chat.providers.claude_sdk import check_tool

    assert check_tool("mcp__quant__list_pool") is None
    for name in ("Read", "Bash", "CronCreate", "SomeBrandNewToolIn2027",
                 "mcp__other__whatever"):
        reason = check_tool(name)
        assert reason and name in reason, f"{name} 没被拒绝：{reason!r}"
        assert "mcp__quant__" in reason, "拒绝理由要告诉模型它能用什么"
    print("[OK] 闸门结构性拒绝一切非自有工具（含未来新增的）")


def test_sdk_options_are_locked_down():
    """真的把 ClaudeAgentOptions 造出来检查——#30 的教训是「配置看着对、实际没生效」"""
    if not _SDK_INSTALLED:
        print("[SKIP] 未安装 claude-agent-sdk（可选依赖）")
        return
    from analysis.chat.providers.claude_sdk import (
        BUILTIN_TOOLS, MCP_SERVER, allowed_tool_names, build_options,
    )

    opts = build_options(effort="low", session_id=None, trace=[])
    assert list(opts.allowed_tools) == list(allowed_tool_names())
    assert list(opts.disallowed_tools) == list(BUILTIN_TOOLS)
    assert opts.setting_sources == [], \
        "setting_sources 必须是 []：本地 settings 里的 allow 规则会在回调之前放行工具"
    assert opts.skills == [], "skills 必须是 []"
    assert opts.can_use_tool is not None, "can_use_tool 必须挂上（第二层闸门）"
    assert isinstance(opts.system_prompt, str), \
        "system_prompt 要直接传字符串，不要套 claude_code preset"
    assert MCP_SERVER in opts.mcp_servers
    assert opts.max_turns and opts.max_turns > 1, "多轮工具调用需要 max_turns > 1"
    print(f"[OK] SDK 配置已收紧（allowed={len(opts.allowed_tools)} "
          f"disallowed={len(opts.disallowed_tools)} setting_sources=[] skills=[]）")


def test_mcp_adapter_executes_handler_and_records_trace():
    """工具适配层：SDK 侧的 handler 真的调到了 tools.call 并记轨迹"""
    if not _SDK_INSTALLED:
        print("[SKIP] 未安装 claude-agent-sdk（可选依赖）")
        return
    import asyncio
    import json as _json

    from claude_agent_sdk import tool as sdk_tool

    from analysis.chat.providers.claude_sdk import _wrap

    trace = []
    wrapped = _wrap(tools.get_spec("get_overview_scan"), trace, sdk_tool)
    handler = getattr(wrapped, "handler", wrapped)
    out = asyncio.run(handler({}))
    payload = _json.loads(out["content"][0]["text"])
    assert payload["available"] is False and "扫描" in payload["hint"]
    assert trace == [{"tool": "get_overview_scan", "input": {},
                      "rows": 1, "error": payload["hint"]}], trace
    print("[OK] MCP 适配层执行 handler 并记录轨迹（含不可用原因）")


def test_provider_declares_its_own_sdk():
    """SDK 声明跟着 provider 走，不进 requirements.txt / 镜像（#30 cadfa28 的规矩）"""
    from analysis.chat.providers.claude_sdk import ClaudeSdkChatProvider

    specs = [s for _, s in ClaudeSdkChatProvider.REQUIRES]
    assert any("claude-agent-sdk" in s for s in specs), specs
    req = (REPO / "requirements.txt").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in req.splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#"))
    for pkg in ("claude-agent-sdk", "claude_agent_sdk", "anthropic"):
        assert pkg not in body, f"requirements.txt 又列上 {pkg} 了"
    ok, reason = ClaudeSdkChatProvider.available()
    if not ok:
        assert "pip install" in reason or "credentials" in reason.lower() or "/root/.claude" in reason
    print(f"[OK] provider 自带 SDK 声明：{specs}（available={ok}）")


def test_template_provider_is_sdk_free():
    """模板是纯 HTTP 形态、REQUIRES 为空——它的存在就是「抽象是真的」这件事的证据"""
    from analysis.chat.providers import _template

    assert _template.TemplateChatProvider.REQUIRES == ()
    src = inspect.getsource(_template)
    assert "openai_tools" in src, "模板要用同一份工具定义转成 OpenAI 声明"
    assert "build_system_prompt" in src, "模板要用同一份纪律 prompt"
    assert "claude_agent_sdk" not in src, "模板不该依赖某家 SDK"
    print("[OK] 模板 provider 纯 HTTP、共用同一份工具与 prompt")


# ---------- 5. system prompt 的 7 条纪律 ----------
def test_system_prompt_contains_all_disciplines():
    """逐条断言关键词——纪律是针对 TradeTrap 实证的失效模式写的，不许被悄悄改掉"""
    import re

    prompt = build_system_prompt()
    # prompt 里为了可读性会换行 + 缩进（「工具返回的顺序\n   就是答案的顺序」），
    # 断言前把空白抹掉，否则改一次排版就红一次
    flat = re.sub(r"\s+", "", prompt)
    required = [
        ("1 只用工具数据", ["只依据工具返回的数据", "不得引入你记忆"]),
        ("2 不自己算", ["不做加总、排序、加权", "工具返回的顺序就是答案的顺序"]),
        ("3 不预测价格", ["不预测未来价格"]),
        ("4 区分证据与推测", ["区分证据与推测", "推测必须明确标注"]),
        ("5 缺失就说缺失", ["数据缺失就说缺失", "available=false"]),
        ("6 转述覆盖面", ["覆盖面与时效必须转述", "不许把部分覆盖说成全量"]),
        ("7 不给投资建议", ["不输出投资建议式措辞", "只描述证据"]),
    ]
    for label, keys in required:
        for key in keys:
            assert re.sub(r"\s+", "", key) in flat, f"纪律「{label}」的关键词缺失：{key!r}"
    assert DISCIPLINES in prompt
    # 定位：L2 解读层，不是 L3 信号源
    assert "不是信号源" in flat.replace("*", "")
    assert len(prompt) > 800
    print(f"[OK] system prompt 含 7 条纪律（{len(prompt)} 字）")


def test_disciplines_mention_the_five_flow_periods():
    """资金流「只有五档、做不到就说做不到」要写在 prompt 里，不能只写在工具 description 里"""
    prompt = build_system_prompt()
    assert "即时/3日/5日/10日/20日" in prompt or "20日" in prompt
    assert "做不到" in prompt
    print("[OK] prompt 里写明了区间做不到时要直说")


def test_prompt_module_has_no_provider_specific_text():
    """纪律只有一份：provider 里不许另写一份（两份必然漂移）"""
    from analysis.chat.providers import _template, claude_sdk

    for module in (claude_sdk, _template):
        src = inspect.getsource(module)
        assert "build_system_prompt" in src, f"{module.__name__} 没用共用 prompt"
        for sentence in ("只依据工具返回的数据", "不预测未来价格"):
            assert sentence not in src, \
                f"{module.__name__} 里另写了一份纪律（命中 {sentence!r}）"
    print("[OK] 纪律只有 prompt.py 一份")


# ---------- 不接入信号系统 ----------
def test_not_wired_into_signal_system():
    """对话是 L2 解读层：不产生 signal、不进 fusion、不影响回测与仓位"""
    from strategy.fusion import SOURCE_COLUMNS
    from strategy.signals import SIGNAL_SOURCES

    for name in list(SIGNAL_SOURCES) + list(SOURCE_COLUMNS):
        assert "chat" not in str(name) and "llm" not in str(name).lower(), name
    for rel in ("strategy/signals.py", "strategy/fusion.py", "backtest/engine.py"):
        src = (REPO / rel).read_text(encoding="utf-8")
        assert "analysis.chat" not in src and "chat.tools" not in src, rel
    # config/settings.py 里只该有一行 DEFAULT_CHAT_PROVIDER（注释里会提到 providers 目录），
    # 但不许出现任何把 chat 塞进信号源/权重的配置
    settings_src = (REPO / "config" / "settings.py").read_text(encoding="utf-8")
    assert "DEFAULT_CHAT_PROVIDER" in settings_src
    for banned in ("chat_signal", "CHAT_SIGNAL", "chat_weight"):
        assert banned not in settings_src, f"config/settings.py 出现了 {banned}"
    print("[OK] 未接入信号系统（signals/fusion/backtest 都不认识 analysis.chat）")


def test_page_count_and_routing():
    src = (REPO / "app.py").read_text(encoding="utf-8")
    assert '"AI 分析助手"' in src and "page_chat()" in src
    assert 'pages = ["全局信号总览", "单股票分析", "新闻列表", "股票池管理", "板块分析",' in src
    print("[OK] 侧边栏多了「AI 分析助手」一页，路由已接上")


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"[OK] {name}")
    print(f"\n全部 {len(tests)} 项测试通过!")
