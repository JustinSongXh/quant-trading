"""新 provider 模板——**下划线开头，不会被自动发现**。

加一家新模型：把本文件复制成 `<key>.py`（key 即文件名，须匹配
`^[a-z][a-z0-9_]{2,31}$`），改掉下面几处即可，别处零改动。

纯文本 LLM（OpenAI 兼容：DeepSeek / Qwen / Kimi / GLM，换 base_url 即可）也走
同一契约：在 prompt 里塞进 TradeStrategy 基类源码 + PlayDeadQuarterStrategy 当
few-shot，靠 orchestrator 的闸门回灌迭代即可。
"""

import os

from strategy.agent_gen.base import CodegenProvider, Turn


class TemplateProvider(CodegenProvider):
    key = "_template"          # 改成文件名
    name = "模板 provider"
    description = "复制本文件即可新增一家模型。"

    # 与 TradeStrategy.PARAMS 同 schema，前端复用 _render_strategy_params 渲染。
    # 密钥类配置不要放这里——一律读环境变量，不进 PARAMS、不落盘、不进 prompt。
    PARAMS = [
        {"key": "temperature", "label": "采样温度", "default": 0.2,
         "min": 0.0, "max": 1.0, "step": 0.1},
    ]

    @classmethod
    def available(cls) -> tuple[bool, str]:
        if not os.environ.get("TEMPLATE_API_KEY"):
            return False, "未设置环境变量 TEMPLATE_API_KEY"
        return True, ""

    def __init__(self, temperature: float = 0.2):
        super().__init__(temperature=temperature)
        self.temperature = temperature

    def start(self, user_text: str) -> Turn:
        return self._chat([{"role": "user", "content": user_text}], session={})

    def resume(self, session: dict, user_text: str) -> Turn:
        history = list(session.get("history", []))
        history.append({"role": "user", "content": user_text})
        return self._chat(history, session)

    def _chat(self, history: list[dict], session: dict) -> Turn:
        # 1. 调模型，system prompt 用 strategy.agent_gen.prompt.CONTRACT_PROMPT
        #    （策略契约 + 输出格式 + 澄清规则，provider 无关，不要各自另写一份）
        reply = "..."  # ← 换成真实调用
        # 2. 把续接状态原样塞进 session，调用方只负责存回
        new_session = {**session, "history": history + [{"role": "assistant", "content": reply}]}
        # 3. 交给基类判定 ask / deliver——不要自己判，三条件判定只在 extract.py 一份
        return self.to_turn([reply], new_session)
