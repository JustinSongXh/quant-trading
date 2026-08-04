"""对话 provider 的契约（issue #36）

与 `CodegenProvider`（#30）刻意同构：元数据 + PARAMS + REQUIRES + 注册表 + 自动发现 +
数据驱动前端。加一家新模型 = 往 `providers/` 丢一个文件，别处零改动。

⚠️ 核心约束：**provider 只做「把话和工具交给模型、把回答和轨迹拿回来」**。

- 工具集是 provider 无关的纯数据（`tools.py::TOOLS`），各 provider 自己适配成本家格式
- 工具 handler 一律在这一侧执行，模型拿不到任何绕过 handler 的口子：
  不给内置工具（没有 Read/Bash/Write），不加载本地 settings，不给 skills
- 纪律 prompt 只有一份（`prompt.py`），provider 不许另写

`check_requirements` 与 `CodegenProvider` 的那份是同一段逻辑（find_spec 探测，不 import）。
刻意各自留一份而不抽公共基类：两个 provider 体系除了「可插拔」这个 idiom 之外没有任何
共同语义，为 15 行去建一个跨包基类会让 chat 依赖 strategy.agent_gen，那是更差的耦合。
"""

import importlib.util
from dataclasses import dataclass, field


@dataclass
class Answer:
    """一个回合的产出

    texts: 面向用户的文本，前端按序渲染成气泡
    trace: 工具调用轨迹 `[{"tool", "input", "rows", "error"}]`——**不是可选的 UI 装饰**，
        它是对「解释与实际过程不一致」这一失效模式的实际对策：用户能看到模型到底查了
        什么，而不是只看它说了什么
    session: provider 私有的续接状态（如 session_id / history），调用方原样存回
    """
    texts: list[str]
    trace: list[dict] = field(default_factory=list)
    session: dict = field(default_factory=dict)


class ChatProvider:
    """对话 provider 基类"""

    key = "base"              # 注册名，必须等于文件名
    name = ""                 # 前端显示名
    description = ""          # 前端说明
    PARAMS: list[dict] = []   # 与 TradeStrategy.PARAMS 同 schema，复用前端渲染器

    # 该 provider 需要的第三方 SDK：((import 名, pip 规格), ...)
    # **不进 requirements.txt、不进镜像**（#30 cadfa28 定的规矩）：每家 SDK 只服务它
    # 自己，焊进基础镜像等于让所有部署都背上用不到的包。没装就在选择器里显示成
    # 不可用 + 一行可直接复制的 pip 命令。纯 HTTP 的 provider 留空。
    REQUIRES: tuple[tuple[str, str], ...] = ()

    def __init__(self, **params):
        self.params = params

    @classmethod
    def check_requirements(cls) -> tuple[bool, str]:
        """REQUIRES 里的模块是否都装了，返回 (齐全, 缺失说明)。

        用 find_spec 而不是 import：只探测存在性，不承担 import 副作用
        （claude-agent-sdk 一 import 就去找 CLI），选择器渲染要快。
        """
        missing = []
        for mod, spec in cls.REQUIRES:
            try:
                found = importlib.util.find_spec(mod) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                missing.append(spec)
        if not missing:
            return True, ""
        pkgs = " ".join(f"'{s}'" for s in missing)
        return False, (
            f"未安装 {'、'.join(missing)}——本项目刻意不把各家 SDK 放进镜像，"
            f"按需装：docker exec quant-app pip install {pkgs}"
        )

    @classmethod
    def available(cls) -> tuple[bool, str]:
        """依赖与凭证是否就绪，返回 (可用, 不可用原因)。前端据此过滤 + 提示。

        要查凭证的子类覆写它，并在开头先调 check_requirements()——SDK 都没装时
        不该先报「找不到凭证」。
        """
        return cls.check_requirements()

    def ask(self, user_text: str, session: dict | None = None) -> Answer:
        """跑一个回合。session 为空即新会话，否则在其上续接。

        实现要求：
        - system prompt 用 `prompt.build_system_prompt()`，不要自己另写纪律
        - 工具用 `tools.TOOLS`，handler 只经 `tools.call()` 调用（异常在那儿被兜住）
        - 每次工具执行都要往 trace 里追加一条，包含参数与返回行数
        - 模型侧的错误翻成人能读的一句话再 raise RuntimeError，不要甩 SDK 堆栈
        """
        raise NotImplementedError
