"""provider 自动发现与注册（#30）

与 strategy/trade_strategy.py 的 _discover_custom() 同一套写法：importlib 扫目录、
key 必须等于文件名、单文件失败只记 warning 不影响其余。刻意重复这个 idiom，
让代码库里只存在一种「可插拔」的写法。
"""

import importlib
import logging
import threading
from pathlib import Path

from strategy.agent_gen.base import CodegenProvider

logger = logging.getLogger(__name__)

_PROVIDER_DIR = Path(__file__).parent / "providers"
_cache: dict | None = None
_lock = threading.RLock()


def _discover() -> dict:
    """扫 providers/*.py（下划线开头的跳过：_template.py 是给人抄的空壳）"""
    found = {}
    if not _PROVIDER_DIR.is_dir():
        return found

    for path in sorted(_PROVIDER_DIR.glob("*.py")):
        stem = path.stem
        if stem.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"strategy.agent_gen.providers.{stem}")
            importlib.reload(module)
            classes = [
                obj for obj in vars(module).values()
                if isinstance(obj, type) and issubclass(obj, CodegenProvider)
                and obj is not CodegenProvider and obj.__module__ == module.__name__
            ]
            if len(classes) != 1:
                raise ValueError(f"应恰好包含 1 个 CodegenProvider 子类，实际 {len(classes)} 个")
            cls = classes[0]
            if cls.key != stem:
                raise ValueError(f"key={cls.key!r} 与文件名 {stem!r} 不一致")
            found[cls.key] = cls
        except Exception as e:
            logger.warning("跳过 provider %s：%s: %s", path.name, type(e).__name__, e)

    return found


def _registry() -> dict:
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache = _discover()
    return _cache


def refresh_providers() -> dict:
    """重新扫描 providers/（新增文件后调用）"""
    global _cache
    with _lock:
        _cache = _discover()
        return _cache


def list_providers(only_available: bool = False) -> list[dict]:
    """列出 provider 元数据（前端渲染选择器用）

    不可用的也列出来并带上原因——一个都没有时要能告诉用户「为什么没有」，
    而不是给一个空下拉框。
    """
    out = []
    for key, cls in _registry().items():
        try:
            ok, reason = cls.available()
        except Exception as e:  # available() 自身炸了不能拖垮选择器
            ok, reason = False, f"检测可用性时出错：{type(e).__name__}: {e}"
        if only_available and not ok:
            continue
        out.append({
            "key": key,
            "name": cls.name or key,
            "description": cls.description,
            "params": cls.PARAMS,
            "available": ok,
            "reason": reason,
        })
    return out


def get_provider(key: str, **params) -> CodegenProvider:
    """按 key 构造 provider 实例，params 透传给构造函数"""
    registry = _registry()
    if key not in registry:
        raise ValueError(f"unknown codegen provider: {key}")
    return registry[key](**params)


def default_provider_key() -> str | None:
    """默认 provider：配置项优先，不可用时退到第一个可用的"""
    from config.settings import DEFAULT_CODEGEN_PROVIDER

    usable = [p["key"] for p in list_providers(only_available=True)]
    if DEFAULT_CODEGEN_PROVIDER in usable:
        return DEFAULT_CODEGEN_PROVIDER
    return usable[0] if usable else None
