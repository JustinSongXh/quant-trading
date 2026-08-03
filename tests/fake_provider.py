"""测试用的假 provider：按预置脚本返回回合，不打真实 LLM。

放在 tests/ 而不是 providers/ 下，因为 orchestrator 测试直接实例化它、不需要它被发现。
providers/ 的自动发现另由 test_codegen_registry.py 用临时文件验证。
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.agent_gen.base import CodegenProvider, Turn

HEADER = "from strategy.trade_strategy import TradeStrategy\n\n\n"

# 交付回合附带的说明文字：应当进对话区，而源码不应当
DELIVER_PROSE = "好的，按你的描述实现如下。"


def strategy_code(key: str, *, name="假策略", body=None, extra="") -> str:
    """生成一份能过闸门的策略源码"""
    return HEADER + f'''class FakeStrategy(TradeStrategy):
    key = "{key}"
    name = "{name}"
    description = "测试用"
    example = ""
    custom = True
{extra}
    def on_bar(self, broker, ctx):
{body or """        if ctx.decision == 1:
            shares = int(broker.cash * 0.5 / ctx.price)
            if shares > 0:
                broker.buy(ctx.symbol, ctx.price, shares, ctx.date, ctx.board)
        elif ctx.decision == -1:
            pos = broker.positions.get(ctx.symbol)
            if pos and broker.can_sell(ctx.symbol, ctx.date, ctx.board):
                broker.sell(ctx.symbol, ctx.price, pos.shares, ctx.date, ctx.board)"""}
'''


def ask(text: str) -> str:
    """脚本项：一个澄清回合"""
    return text


def deliver(key: str, code: str | None = None) -> tuple[str, str]:
    """脚本项：一个交付回合"""
    return key, code if code is not None else strategy_code(key)


class FakeProvider(CodegenProvider):
    """按脚本逐回合返回。脚本项为 str → ask；为 (key, code) → deliver。"""

    key = "fake"
    name = "假 provider（测试用）"
    description = "返回预置回合，不调用任何模型。"
    PARAMS = []

    def __init__(self, script=(), *, fail_on=None):
        super().__init__()
        self.script = list(script)
        self.prompts: list[str] = []      # 收到过的每一句话，供断言回灌内容
        self.fail_on = fail_on            # 第几次调用抛异常（模拟限流/网络错误）

    # ---------- CodegenProvider ----------

    def start(self, user_text: str) -> Turn:
        return self._next(user_text, {})

    def resume(self, session: dict, user_text: str) -> Turn:
        return self._next(user_text, session)

    # ---------- 内部 ----------

    def _next(self, user_text: str, session: dict) -> Turn:
        self.prompts.append(user_text)
        n = len(self.prompts)
        if self.fail_on == n:
            raise RuntimeError("模拟 provider 出错（限流）")
        if not self.script:
            raise AssertionError(f"脚本已耗尽，但第 {n} 次仍被调用：{user_text[:60]!r}")

        item = self.script.pop(0)
        # session 累加轮次，用来断言 orchestrator 把 session 原样传了回来
        new_session = {**session, "turns": session.get("turns", 0) + 1}

        if isinstance(item, str):
            return Turn(kind="ask", texts=[item], session=new_session)

        key, code = item
        # 走真实的 extract 路径（基类 to_turn），而不是手工构造 deliver。
        # 交付时带一句说明，模拟模型的真实输出形状（说明进对话区、源码不进）
        return self.to_turn(
            [f"{DELIVER_PROSE}\n\nSTRATEGY_KEY: {key}", f"```python\n{code}```"],
            new_session,
        )
