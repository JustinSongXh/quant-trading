"""从模型回复里提取策略 key 与源码，并据此判定「交付」还是「还在澄清」（#30）

约定的交付格式（写在 prompt.py 里，provider 无关）：

    STRATEGY_KEY: <key>
    ```python
    <完整文件内容>
    ```

判定为 deliver 需**同时**满足三条：

1. 出现 `STRATEGY_KEY: <合法 key>`
2. 出现 fenced python 代码块
3. 该代码块能 ast.parse，且含至少一个 TradeStrategy 子类定义

任一不满足 → 视为澄清回合，不进闸门。三条缺一不可：模型在澄清过程中经常顺手贴
一段示意代码，只看「有没有代码块」会把它误判成交付，白跑一遍闸门还污染 _staging。
"""

import ast
import re

from strategy.agent_gen.guard import find_strategy_classes
from strategy.trade_strategy import KEY_PATTERN

# 反引号数量不定（模型偶尔用 4 个以避开正文里的 ```），用反向引用配对
FENCE_RE = re.compile(
    r"^(?P<fence>`{3,})[ \t]*(?P<lang>[A-Za-z0-9_+.-]*)[ \t]*\r?\n"
    r"(?P<body>.*?)"
    r"^(?P=fence)`*[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
KEY_RE = re.compile(r"^[ \t>*-]*STRATEGY_KEY[ \t]*[:：][ \t]*(?P<key>\S+)[ \t]*$", re.MULTILINE)

PYTHON_LANGS = {"python", "py", "python3"}


def extract(texts: list[str]) -> tuple[str | None, str | None]:
    """从若干段回复文本里提取 (key, code)；不构成交付时返回 (None, None)。

    key 与代码块可能落在不同段落（provider 会把多个文本块分开返回），因此先拼接再匹配。
    出现多个候选时一律取**最后一个**——模型常先给草稿或示意再给定稿。
    """
    blob = "\n".join(t for t in texts if t)

    key = None
    for m in KEY_RE.finditer(blob):
        candidate = m.group("key").strip().strip("`\"'")
        if KEY_PATTERN.match(candidate):
            key = candidate
    if key is None:
        return None, None

    code = None
    for m in FENCE_RE.finditer(blob):
        if m.group("lang").lower() not in PYTHON_LANGS:
            continue
        body = m.group("body")
        try:
            tree = ast.parse(body)
        except SyntaxError:
            continue
        if find_strategy_classes(tree):
            code = body
    if code is None:
        return None, None

    return key, code
