"""「丢一个文件进目录就注册一个插件」这套发现机制的公共实现（issue #41）

仓库里原本有三份几乎逐行相同的拷贝：`analysis/chat/registry.py`、
`strategy/agent_gen/registry.py`（两者连行号都对齐）、`strategy/trade_strategy.py::
_discover_custom()`。三处的注释都写着「刻意重复这个 idiom，让代码库里只存在一种可插拔
写法」——那个目标是对的，但实现同一个 idiom 的方式应该是**同一份代码**，而不是三份。

约定（三处共有，不可变）：

- 扫目录里的顶层 `*.py`，**下划线开头的跳过**（`_template.py` 是给人抄的空壳）；
- 每个文件里**恰好一个**目标基类的子类，且 `cls.key` 必须等于文件名（key 与文件名
  绑死，才能靠文件名推断插件、也才能在报错里指名道姓）；
- `importlib.reload()`：保存文件后立即生效，不用重启进程；
- **单个文件出错只记 warning 并跳过**——一个坏插件不能拖垮整站。

`PluginRegistry` 额外提供带锁的缓存与 `available()` 汇总，供两个 provider 注册表用；
只需要发现逻辑的（自定义策略）直接调 `discover_classes()`。
"""

import importlib
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def discover_classes(package: str, directory: Path, base_cls: type, label: str,
                     reject=None) -> dict:
    """扫 `directory/*.py` → `{cls.key: cls}`。

    Args:
        package: 目录对应的 import 包路径（如 `analysis.chat.providers`）
        directory: 要扫的目录
        base_cls: 目标基类，文件里必须恰好有一个它的子类
        label: 出错日志里的称呼（如 "chat provider" / "自定义策略"）
        reject: 可选 `(cls) -> str | None`，返回非空即视为该文件不合法（原因）。
            用于基类之外的额外规则，例如「key 不许与内置策略冲突」。

    Returns:
        合法插件的 `{key: 类}`；目录不存在时返回空 dict。
    """
    found = {}
    if not directory.is_dir():
        return found

    for path in sorted(directory.glob("*.py")):
        stem = path.stem
        if stem.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{package}.{stem}")
            importlib.reload(module)     # 保存后立即生效，无需重启进程
            classes = [
                obj for obj in vars(module).values()
                if isinstance(obj, type) and issubclass(obj, base_cls)
                and obj is not base_cls and obj.__module__ == module.__name__
            ]
            if len(classes) != 1:
                raise ValueError(
                    f"应恰好包含 1 个 {base_cls.__name__} 子类，实际 {len(classes)} 个")
            cls = classes[0]
            if cls.key != stem:
                raise ValueError(f"key={cls.key!r} 与文件名 {stem!r} 不一致")
            if reject is not None:
                reason = reject(cls)
                if reason:
                    raise ValueError(reason)
            found[cls.key] = cls
        except Exception as e:
            logger.warning("跳过%s %s：%s: %s", label, path.name, type(e).__name__, e)

    return found


class PluginRegistry:
    """带缓存的插件注册表（两个 provider 注册表共用）。

    缓存是惰性的并用 RLock 串行化：Streamlit 每次 rerun 在独立线程执行，并发 refresh
    会在 `importlib.reload()` 重新执行模块体的同时被另一线程读 `vars(module)`（#27
    的同类竞态）。RLock 而非 Lock——持锁的代码路径里可能再次触发 refresh。
    """

    def __init__(self, package: str, directory: Path, base_cls: type, label: str):
        self.package = package
        self.directory = directory
        self.base_cls = base_cls
        self.label = label
        self._cache: dict | None = None
        self._lock = threading.RLock()

    def discover(self) -> dict:
        return discover_classes(self.package, self.directory, self.base_cls, self.label)

    def registry(self) -> dict:
        if self._cache is None:
            with self._lock:
                if self._cache is None:
                    self._cache = self.discover()
        return self._cache

    def refresh(self) -> dict:
        """重新扫描目录（新增/删除文件后调用）"""
        with self._lock:
            self._cache = self.discover()
            return self._cache

    def list_plugins(self, only_available: bool = False) -> list[dict]:
        """列出元数据（前端渲染选择器用）

        不可用的也列出来并带上原因——一个都没有时要能告诉用户「为什么没有」，
        而不是给一个空下拉框。
        """
        out = []
        for key, cls in self.registry().items():
            try:
                ok, reason = cls.available()
            except Exception as e:   # available() 自身炸了不能拖垮选择器
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

    def get(self, key: str, **params):
        """按 key 构造实例，params 透传给构造函数"""
        registry = self.registry()
        if key not in registry:
            raise ValueError(f"unknown {self.label}: {key}")
        return registry[key](**params)

    def default_key(self, configured: str | None) -> str | None:
        """默认插件：配置项优先，不可用时退到第一个可用的。

        **前端 radio 的默认项必须走这个函数**——#30 踩过一次：默认项用了 radio 的
        字母序第一项，导致 DEFAULT_*_PROVIDER 配置形同虚设（4646dd4 修的）。
        """
        usable = [p["key"] for p in self.list_plugins(only_available=True)]
        if configured in usable:
            return configured
        return usable[0] if usable else None
