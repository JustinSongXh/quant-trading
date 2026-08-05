"""对话 provider 的自动发现与注册（issue #36）

发现机制本身在 `utils/plugin_registry.py`（#41 起三处拷贝合一）：扫目录、`_` 开头跳过、
key 必须等于文件名、单文件失败只记 warning。这里只把它绑到 `ChatProvider` 上，并保持
对外函数名不变（页面与测试都在用）。
"""

from pathlib import Path

from analysis.chat.base import ChatProvider
from utils.plugin_registry import PluginRegistry

_PROVIDER_DIR = Path(__file__).parent / "providers"

_REGISTRY = PluginRegistry(
    package="analysis.chat.providers",
    directory=_PROVIDER_DIR,
    base_cls=ChatProvider,
    label="chat provider",
)


def refresh_providers() -> dict:
    """重新扫描 providers/（新增文件后调用）"""
    return _REGISTRY.refresh()


def list_providers(only_available: bool = False) -> list[dict]:
    """列出 provider 元数据（前端渲染选择器用），不可用的也列出并带原因"""
    return _REGISTRY.list_plugins(only_available=only_available)


def get_provider(key: str, **params) -> ChatProvider:
    """按 key 构造 provider 实例，params 透传给构造函数"""
    return _REGISTRY.get(key, **params)


def default_provider_key() -> str | None:
    """默认 provider：`DEFAULT_CHAT_PROVIDER` 优先，不可用时退到第一个可用的"""
    from config.settings import DEFAULT_CHAT_PROVIDER

    return _REGISTRY.default_key(DEFAULT_CHAT_PROVIDER)
