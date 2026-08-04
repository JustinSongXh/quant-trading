"""新对话 provider 模板——**下划线开头，不会被自动发现**（issue #36）

加一家新模型：把本文件复制成 `<key>.py`（key 即文件名，须匹配 `^[a-z][a-z0-9_]{2,31}$`），
把 `_call_model` 换成真实 HTTP 调用即可，别处零改动。

刻意写成**纯 HTTP（OpenAI 兼容 function calling）**形态、`REQUIRES` 留空：首个实现走
Claude Agent SDK 的进程内 MCP，这个走请求体里的 `tools=[...]`，两条路共用同一份
`analysis/chat/tools.py::TOOLS` 与同一份 `prompt.py`。抽象是不是真的，就看这一点——
业务层（chat 面板）对两者的用法完全一样，只经 registry 拿实例。

DeepSeek / Qwen / Kimi / GLM 都是 OpenAI 兼容的，换 base_url + 模型名即可。
"""

import json
import os

from analysis.chat import tools as chat_tools
from analysis.chat.base import Answer, ChatProvider
from analysis.chat.prompt import build_system_prompt

MAX_TOOL_ROUNDS = 6      # 一个回合里最多跑几轮「模型要工具 → 我们执行 → 回喂」


class TemplateChatProvider(ChatProvider):
    key = "_template"          # 改成文件名
    name = "模板对话 provider"
    description = "复制本文件即可新增一家模型（OpenAI 兼容的 function calling）。"

    PARAMS = [
        # 密钥类配置不要放这里——一律读环境变量，不进 PARAMS、不落盘、不进 prompt
        {"key": "temperature", "label": "采样温度", "default": 0.2,
         "min": 0.0, "max": 1.0, "step": 0.1},
    ]

    # 纯 HTTP 不需要任何 SDK，所以留空。用官方 SDK 的话在这里声明，
    # **不要加进 requirements.txt / Dockerfile**（每家 SDK 只服务它自己）。
    REQUIRES = ()

    BASE_URL = "https://api.example.com/v1/chat/completions"
    MODEL = "example-model"

    @classmethod
    def available(cls) -> tuple[bool, str]:
        ok, reason = cls.check_requirements()      # 先查 SDK 再查凭证
        if not ok:
            return ok, reason
        if not os.environ.get("TEMPLATE_API_KEY"):
            return False, "未设置环境变量 TEMPLATE_API_KEY"
        return True, ""

    def __init__(self, temperature: float = 0.2):
        super().__init__(temperature=temperature)
        self.temperature = float(temperature)

    def ask(self, user_text: str, session: dict | None = None) -> Answer:
        session = session or {}
        # system prompt 用共用的那份，**不要在这里另写纪律**（两份必然漂移）
        messages = list(session.get("messages") or
                        [{"role": "system", "content": build_system_prompt()}])
        messages.append({"role": "user", "content": user_text})

        trace: list[dict] = []
        texts: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            # 同一份工具定义转成 OpenAI 的声明格式——工具集没有绑在任何一家 SDK 上
            reply = self._call_model(messages, chat_tools.openai_tools())
            messages.append(reply)

            calls = reply.get("tool_calls") or []
            if not calls:
                if reply.get("content"):
                    texts.append(reply["content"])
                break

            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                # handler 一律经 tools.call()：异常在那里被兜成可转达的一句话，
                # 模型永远拿不到绕过 handler 的口子
                payload = chat_tools.call(name, args)
                record = {"tool": name, "input": args,
                          "rows": chat_tools.count_rows(payload)}
                if payload.get("error"):
                    record["error"] = payload["error"]
                trace.append(record)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": chat_tools.to_text(payload)})

        return Answer(texts=texts or ["（模型没有返回文本）"], trace=trace,
                      session={**session, "messages": messages})

    def _call_model(self, messages: list[dict], tools: list[dict]) -> dict:
        """→ 换成真实调用：POST BASE_URL，body 里带 model/messages/tools/temperature，
        返回 choices[0].message（dict，可能含 tool_calls）。

        错误处理要求：限流/鉴权失败翻成人能读的一句话再 raise RuntimeError，
        不要把 HTTP 堆栈甩到聊天框里。
        """
        raise NotImplementedError("把这里换成真实的 HTTP 调用")
