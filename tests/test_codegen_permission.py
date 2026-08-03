"""闸门 1（工具权限）测试：不起模型，直接测判定逻辑

背景：`allowed_tools` 里的工具会在 `can_use_tool` **之前**被自动放行。最初把
Read/Grep/Glob 放进了 allowed_tools，结果 Read 对任意绝对路径畅通——实测能读到
/etc/hostname，也就意味着能读容器里挂着的 /root/.claude：OAuth 凭证 + 宿主 5 个项目
的 Claude Code 会话记录（25 个 jsonl / 7.8MB）。一句「读一下那个文件」就能打进聊天框。

现在 allowed_tools 留空、回调是唯一关卡。这个文件守住那条边界。
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.agent_gen.providers.claude_sdk import (
    DENIED_IN_REPO, READ_ONLY_TOOLS, REPO_ROOT, check_tool,
)


def allowed(tool: str, **kw):
    reason = check_tool(tool, kw)
    assert reason is None, f"{tool}({kw}) 应放行，却被拒：{reason}"


def denied(tool: str, **kw) -> str:
    reason = check_tool(tool, kw)
    assert reason is not None, f"{tool}({kw}) 应被拒，却放行了"
    return reason


# ---------- 1. 只读工具白名单 ----------
def test_only_read_tools_allowed():
    for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "BashOutput",
                 "WebFetch", "WebSearch", "Task", "TodoWrite", "KillShell"):
        reason = denied(tool, file_path=str(REPO_ROOT / "app.py"))
        assert "只有 Read / Grep / Glob" in reason, reason
    for tool in READ_ONLY_TOOLS:
        allowed(tool, path=str(REPO_ROOT))
    print(f"[OK] 只放行 {list(READ_ONLY_TOOLS)}，其余 11 类工具全拒")


# ---------- 2. 仓库内放行 ----------
def test_repo_paths_allowed():
    allowed("Read", file_path=str(REPO_ROOT / "strategy" / "trade_strategy.py"))
    allowed("Read", file_path=str(REPO_ROOT / "backtest" / "broker.py"))
    allowed("Grep", pattern="def on_bar", path=str(REPO_ROOT / "strategy"))
    allowed("Glob", pattern="**/*.py", path=str(REPO_ROOT))
    # 相对路径按仓库根解析
    allowed("Read", file_path="strategy/trade_strategy.py")
    # Grep/Glob 省略 path 时以 cwd（= 仓库）为根
    allowed("Grep", pattern="TradeStrategy")
    allowed("Glob", pattern="strategy/**/*.py")
    print("[OK] 仓库内路径放行（含相对路径与省略 path）")


# ---------- 3. 越界一律拒 ----------
def test_outside_repo_denied():
    for p in ("/etc/hostname", "/etc/passwd", "/proc/self/environ",
              "/root/.claude/.credentials.json", "/root/.claude/projects",
              "/root/.claude/settings.json", "/root/.bashrc", "/tmp", "/"):
        reason = denied("Read", file_path=p)
        assert "仓库之外" in reason, f"{p} 的拒绝理由不对：{reason}"
    # Grep / Glob 的 path 同样受限——否则能直接扫会话记录
    assert "仓库之外" in denied("Grep", pattern="accessToken", path="/root/.claude")
    assert "仓库之外" in denied("Glob", pattern="**/*.jsonl", path="/root/.claude/projects")
    print("[OK] 仓库外路径全拒（含宿主 /root/.claude 的凭证与会话记录）")


def test_traversal_and_symlink_denied():
    """resolve() 会展开 .. 与符号链接，两种绕法都得堵住"""
    assert "仓库之外" in denied("Read", file_path=str(REPO_ROOT / ".." / ".." / "etc" / "passwd"))
    assert "仓库之外" in denied("Read", file_path="../../etc/passwd")
    assert "仓库之外" in denied("Read", file_path=f"{REPO_ROOT}/strategy/../../etc/hostname")

    link = REPO_ROOT / "_perm_test_link"
    try:
        link.symlink_to("/root/.claude")
        reason = denied("Read", file_path=str(link / ".credentials.json"))
        assert "仓库之外" in reason, reason
        assert "仓库之外" in denied("Glob", pattern="**/*", path=str(link))
        print("[OK] `..` 穿越与仓库内指向外部的软链都被拦下")
    finally:
        link.unlink(missing_ok=True)


# ---------- 4. 仓库内的敏感文件 ----------
def test_denied_in_repo():
    for rel in DENIED_IN_REPO:
        reason = denied("Read", file_path=str(REPO_ROOT / rel))
        assert rel in reason, reason
    # 目录形式的条目要连子路径一起拦
    assert "本机/个人配置" in denied("Read",
                                     file_path=str(REPO_ROOT / ".claude" / "settings.local.json"))
    # 前缀相同但不是同一个东西的，不能误伤
    allowed("Read", file_path=str(REPO_ROOT / "config" / "settings.py"))
    allowed("Glob", pattern="*.py", path=str(REPO_ROOT / "config"))
    print(f"[OK] 仓库内敏感项被拒（{list(DENIED_IN_REPO)}），同前缀的正常文件不误伤")


# ---------- 5. 多路径参数 ----------
def test_all_path_args_checked():
    """任何一个路径参数越界都要拒，不能只看第一个"""
    assert "仓库之外" in denied("Read", notebook_path="/root/.claude/x.ipynb")
    assert "仓库之外" in denied("Grep", pattern="x", path="/etc")
    # 合法 path + 其它无关参数共存时不受影响
    allowed("Grep", pattern="x", path=str(REPO_ROOT), glob="*.py", output_mode="content")
    print("[OK] 所有路径参数逐个校验")


# ---------- 6. 配置自身的合理性 ----------
def test_allowed_tools_left_empty():
    """allowed_tools 必须留空，否则里面的工具会绕过本回调——这就是原来的洞"""
    import inspect

    from strategy.agent_gen.providers import claude_sdk

    src = inspect.getsource(claude_sdk.ClaudeSdkProvider._query)
    assert "allowed_tools=[]" in src, \
        "allowed_tools 不为空：里面的工具会在 can_use_tool 之前被放行，闸门1 出现旁路"
    assert "can_use_tool=_gate_tools" in src
    assert "setting_sources=[]" in src, \
        "setting_sources 非空会加载本地 settings 的 allow 规则，同样绕过回调"
    print("[OK] allowed_tools 留空 + setting_sources 留空：回调是唯一关卡，无旁路")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
