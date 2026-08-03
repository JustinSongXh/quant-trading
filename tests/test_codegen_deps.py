"""代码生成 provider 的依赖边界（#30）

两条规矩，这个文件把它们钉住：

1. **各家模型 SDK 不进镜像、不进 requirements.txt。** 每家 SDK 只服务它自己那个
   provider，焊进基础依赖等于让所有部署都背上用不到的包。依赖声明跟着 provider
   文件走（`CodegenProvider.REQUIRES`），没装时选择器显示不可用 + 一行 pip 命令。

2. **不装 Node。** Dockerfile 里曾有一段 Node 20 + `npm i -g @anthropic-ai/claude-code`
   （346M，且走 npmmirror——本环境到 nodejs.org / registry.npmjs.org 会静默卡死）。
   那段是死重：claude-agent-sdk 的 linux-x64 wheel 自带 `_bundled/claude`，而 SDK 的
   `_find_cli()` **先查 bundled 再查 PATH**，npm 装的那个从来没被用过（bundled
   2.1.220 反而比 npm 装的 2.1.197 更新）。于是删掉那段，并去掉 `available()` 里的
   `which("claude")`——否则删了 Node 之后它会报「PATH 里找不到 claude CLI」把功能
   藏起来，而底下其实完全能跑。

claude-agent-sdk 是可选依赖，装了才跑得到第 3 组用例；没装时跳过而不是报红。
"""

import os
import subprocess
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SDK_INSTALLED = importlib.util.find_spec("claude_agent_sdk") is not None

# provider 声明过的 pip 包名，用于「不许出现在基础依赖里」的检查
_SDK_PKGS = ("claude-agent-sdk", "claude_agent_sdk", "anthropic")


# ---------- 1. 依赖声明跟着 provider 走 ----------
def test_provider_declares_its_own_sdk():
    """claude_sdk provider 必须自己声明 REQUIRES，而不是指望基础依赖装好"""
    from strategy.agent_gen.providers.claude_sdk import ClaudeSdkProvider

    specs = [spec for _, spec in ClaudeSdkProvider.REQUIRES]
    assert any("claude-agent-sdk" in s for s in specs), \
        f"REQUIRES 里没声明 claude-agent-sdk：{ClaudeSdkProvider.REQUIRES}"
    print(f"[OK] provider 自带依赖声明：{specs}")


def test_missing_sdk_reason_is_actionable():
    """依赖缺失时给的是能直接复制的 pip 命令，不是「需重建镜像」"""
    from strategy.agent_gen.base import CodegenProvider

    class _Fake(CodegenProvider):
        key = "_fake"
        REQUIRES = (("definitely_not_installed_xyz", "definitely-not-installed-xyz>=1.0"),)

    ok, reason = _Fake.available()
    assert ok is False, "缺依赖却报可用"
    assert "pip install" in reason, f"原因里没有可执行的补救命令：{reason}"
    assert "definitely-not-installed-xyz>=1.0" in reason, f"没带上具体包名：{reason}"
    assert "重建镜像" not in reason, f"不该再让人重建镜像：{reason}"
    print(f"[OK] 缺依赖时的提示可直接照做：{reason}")


def test_check_requirements_does_not_import():
    """探测用 find_spec，不真的 import——import claude-agent-sdk 会去找 CLI，拖慢选择器"""
    import ast
    import inspect

    from strategy.agent_gen import base

    src = inspect.getsource(base.CodegenProvider.check_requirements)
    tree = ast.parse(inspect.cleandoc(src))
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.Import, ast.ImportFrom)), \
            f"check_requirements 里有 import（第 {node.lineno} 行），应该只用 find_spec"
    assert "find_spec" in src, "没用 find_spec 探测"
    print("[OK] check_requirements 只探测不 import")


# ---------- 2. 基础依赖 / 镜像里不该有这些 ----------
def test_requirements_has_no_provider_sdk():
    """requirements.txt 不许列各家模型 SDK"""
    text = (REPO / "requirements.txt").read_text(encoding="utf-8")
    body = "\n".join(
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    )
    for pkg in _SDK_PKGS:
        assert pkg not in body, (
            f"requirements.txt 又列上 {pkg!r} 了：各家 SDK 只服务自己那个 provider，"
            f"应写进该 provider 的 REQUIRES，让用户按需装"
        )
    print("[OK] requirements.txt 不含模型 SDK")


def test_dockerfile_installs_no_node_or_sdk():
    """镜像里不该有 Node/npm，也不该 pip 装模型 SDK"""
    text = (REPO / "Dockerfile").read_text(encoding="utf-8")
    body = "\n".join(
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    )

    for pat in ("npm ", "node.tar.gz", "NODE_VERSION", "@anthropic-ai/claude-code"):
        assert pat not in body, (
            f"Dockerfile 又在装 Node 了（命中 {pat!r}）：claude-agent-sdk 自带 CLI，"
            f"这层只会让镜像大 346M 并把构建绑在 npmmirror 上"
        )
    for pkg in _SDK_PKGS:
        assert pkg not in body, f"Dockerfile 又在装 {pkg!r} 了：SDK 按需装，不进镜像"
    print("[OK] Dockerfile 不装 Node/npm，也不装模型 SDK")


# ---------- 3. 装了 SDK 时：CLI 来自 wheel，且不需要 Node ----------
def test_cli_comes_from_wheel():
    """_find_cli() 必须落在 site-packages 里的 _bundled/claude"""
    if not _SDK_INSTALLED:
        print("[SKIP] 未安装 claude-agent-sdk（可选依赖）")
        return

    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    transport = SubprocessCLITransport(prompt="x", options=ClaudeAgentOptions())
    cli = Path(transport._find_cli())

    import claude_agent_sdk
    expected = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    assert cli == expected, (
        f"CLI 不是 wheel 自带的那个，而是 {cli}。若 wheel 不再 bundle，"
        f"available() 需要重新加一层检查，Dockerfile 也得把 Node 装回来"
    )
    assert cli.is_file(), f"{cli} 不存在"
    print(f"[OK] CLI 来自 wheel：{cli}（{cli.stat().st_size // 1024 // 1024}M）")


def test_bundled_cli_needs_no_node():
    """清空 PATH 里的 node，bundled CLI 仍能跑——它是静态 ELF，不是 JS 脚本"""
    if not _SDK_INSTALLED:
        print("[SKIP] 未安装 claude-agent-sdk（可选依赖）")
        return

    import claude_agent_sdk
    cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"

    # PATH 只留 /bin:/usr/bin（Node 若装过，在 /usr/local/bin）
    proc = subprocess.run(
        [str(cli), "--version"],
        env={"HOME": os.path.expanduser("~"), "PATH": "/bin:/usr/bin"},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"没有 Node 时 CLI 跑不起来（rc={proc.returncode}）：{proc.stderr[:400]}"
    )
    assert "Claude Code" in proc.stdout, f"输出不像版本号：{proc.stdout[:200]}"
    print(f"[OK] 无 Node 环境下可执行：{proc.stdout.strip()}")


# ---------- 4. available() 不看 PATH ----------
def test_available_does_not_consult_path():
    """PATH 清空后 available() 不应因为「找不到 claude」而报不可用"""
    from strategy.agent_gen.providers.claude_sdk import ClaudeSdkProvider

    saved = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = ""
        ok, reason = ClaudeSdkProvider.available()
    finally:
        os.environ["PATH"] = saved

    # 没装 SDK / 凭证没挂时 ok 为 False 都合理，但理由不能是 PATH 或 CLI 找不到
    assert "PATH" not in reason, f"available() 仍在依赖 PATH：{reason}"
    assert "CLI" not in reason, f"available() 仍在找 CLI：{reason}"
    print(f"[OK] available() 与 PATH 无关（ok={ok} reason={reason or '无'}）")


def test_no_which_call_in_source():
    """源码自检：不许把 which() 调用加回来

    用 AST 而不是字符串匹配——注释里提到 which("claude") 是**在说明为什么不用它**，
    那不该算命中（第一版就这么误报了）。
    """
    import ast
    import inspect

    from strategy.agent_gen.providers import claude_sdk

    tree = ast.parse(inspect.getsource(claude_sdk))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        assert name != "which", (
            f"第 {node.lineno} 行有 which() 调用：CLI 由 wheel 自带、PATH 里本就没有，"
            f"这行会让面板把可用的功能藏起来"
        )
    print("[OK] 源码里没有 which() 调用")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
