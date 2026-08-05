"""代码生成 provider 的自动发现与注册（#30）

发现机制本身在 `utils/plugin_registry.py`（#41 起三处拷贝合一）：扫目录、`_` 开头跳过、
key 必须等于文件名、单文件失败只记 warning。这里只把它绑到 `CodegenProvider` 上，并保持
对外函数名不变（页面、任务进程与测试都在用）。
"""

from pathlib import Path

from strategy.agent_gen.base import CodegenProvider
from utils.plugin_registry import PluginRegistry

_PROVIDER_DIR = Path(__file__).parent / "providers"

_REGISTRY = PluginRegistry(
    package="strategy.agent_gen.providers",
    directory=_PROVIDER_DIR,
    base_cls=CodegenProvider,
    label="codegen provider",
)


def refresh_providers() -> dict:
    """重新扫描 providers/（新增文件后调用）"""
    return _REGISTRY.refresh()


def list_providers(only_available: bool = False) -> list[dict]:
    """列出 provider 元数据（前端渲染选择器用），不可用的也列出并带原因"""
    return _REGISTRY.list_plugins(only_available=only_available)


def get_provider(key: str, **params) -> CodegenProvider:
    """按 key 构造 provider 实例，params 透传给构造函数"""
    return _REGISTRY.get(key, **params)


def default_provider_key() -> str | None:
    """默认 provider：`DEFAULT_CODEGEN_PROVIDER` 优先，不可用时退到第一个可用的"""
    from config.settings import DEFAULT_CODEGEN_PROVIDER

    return _REGISTRY.default_key(DEFAULT_CODEGEN_PROVIDER)
