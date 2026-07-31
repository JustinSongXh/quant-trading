"""代码生成 provider 的契约（#30）

刻意与 TradeStrategy 同构：元数据 + PARAMS + 注册表 + 自动发现 + 数据驱动前端。
加一个新 provider = 往 providers/ 丢一个文件，别处零改动。

⚠️ 核心约束：provider 不碰文件系统。

任何 provider 都只返回代码文本，落盘一律由 orchestrator 经 store.py 完成。即便
底层 SDK 自带 Write 工具（如 Claude Agent SDK），也只授予只读工具。这样：

- 「自带文件工具的 agent」和「纯文本 LLM」输出形状完全一致
- 闸门 1 从「路径白名单」退化成「拒绝一切写」，攻击面更小
- 回灌逻辑共用一份

自带文件工具的 provider 保留的真实优势仍在：能 Read/Grep 真实仓库学接口风格。
"""

from dataclasses import dataclass, field
from typing import Literal

from strategy.agent_gen.extract import extract as default_extract


@dataclass
class Turn:
    """provider 的一次回合产出

    kind="ask"     还需用户回应，不进闸门，直接回前端渲染成气泡
    kind="deliver" 交付代码，进三道闸门
    """
    kind: Literal["ask", "deliver"]
    texts: list[str]                    # 面向用户的文本，前端按序渲染
    session: dict = field(default_factory=dict)   # provider 私有续接状态，调用方原样存回
    code: str | None = None             # kind="deliver" 时的策略源码
    key: str | None = None              # 策略 key（= 落盘后的文件名）


class CodegenProvider:
    """代码生成 provider 基类"""

    key = "base"              # 注册名，必须等于文件名
    name = ""                 # 前端显示名
    description = ""          # 前端说明
    PARAMS: list[dict] = []   # 与 TradeStrategy.PARAMS 同 schema，复用 _render_strategy_params

    def __init__(self, **params):
        """params 来自前端按 PARAMS 渲染的表单；子类可改成显式关键字参数"""
        self.params = params

    @classmethod
    def available(cls) -> tuple[bool, str]:
        """依赖与凭证是否就绪，返回 (可用, 不可用原因)。前端据此过滤 + 提示。"""
        return True, ""

    def start(self, user_text: str) -> Turn:
        """开启新会话"""
        raise NotImplementedError

    def resume(self, session: dict, user_text: str) -> Turn:
        """在已有会话上继续（用户追问，或 orchestrator 回灌校验错误）"""
        raise NotImplementedError

    def extract(self, texts: list[str]) -> tuple[str | None, str | None]:
        """从回复提取 (key, code)。输出格式特殊的 provider 可覆写。"""
        return default_extract(texts)

    # ---------- 子类复用 ----------

    def to_turn(self, texts: list[str], session: dict) -> Turn:
        """按 extract 结果判定 ask / deliver 并组装 Turn。

        子类只管把模型回复收集成 texts 交给它，不要自己判定 kind——三条件判定
        写在 extract.py 里一份，各 provider 重复实现只会各错各的。
        """
        key, code = self.extract(texts)
        if key and code:
            return Turn(kind="deliver", texts=texts, session=session, code=code, key=key)
        return Turn(kind="ask", texts=texts, session=session)
