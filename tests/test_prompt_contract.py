"""prompt.py 与闸门实现的一致性测试（确定性，不调用 LLM）

prompt 是「告诉模型规则」，guard 是「执行规则」。两边漂移的代价不对称但都很实在：

- guard 放宽了、prompt 没跟上 → 模型不知道自己能用某个模块，绕远路
- prompt 承诺了、guard 不认 → **每次生成都在闸门2 失败**，白烧回灌轮次和订阅额度

所以把两边的清单对齐做成测试，而不是靠人肉记得同步改两处。
"""

import os
import re
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.agent_gen import guard, orchestrator, prompt
from strategy.agent_gen.extract import extract
from strategy.trade_strategy import taken_keys


def text() -> str:
    return prompt.build_system_prompt()


# ---------- 1. import 白名单一致 ----------
def test_import_whitelist_matches_guard():
    p = text()
    for module in guard.ALLOWED_ROOTS | guard.ALLOWED_EXACT:
        assert module in p, f"guard 允许 {module}，但 prompt 没告诉模型"
    # 明确点名的高频误用模块要出现在禁止清单里，模型才不会去试
    for module in ("os", "sys", "subprocess", "pathlib", "requests"):
        assert module in p, f"guard 拒绝 {module}，prompt 应显式提醒"
    print(f"[OK] import 白名单与 guard 一致（{len(guard.ALLOWED_ROOTS | guard.ALLOWED_EXACT)} 项）")


def test_dangerous_calls_mentioned():
    p = text()
    for fn in ("exec", "eval", "open", "__import__", "compile"):
        assert fn in p, f"guard 禁止 {fn}()，prompt 未提及"
    assert "dunder" in p or "__class__" in p, "prompt 未提醒禁止 dunder 属性访问"
    print("[OK] 危险调用与 dunder 限制已写进 prompt")


# ---------- 2. 契约字段齐全 ----------
def test_contract_mentions_all_required_fields():
    p = text()
    for field in ("key", "name", "description", "example", "custom", "PARAMS", "on_bar", "reset"):
        assert field in p, f"契约字段 {field} 未出现在 prompt 里"
    for field in guard.PARAM_REQUIRED:
        assert field in p, f"PARAMS 必填字段 {field} 未在 prompt 里说明"
    assert "min <= default <= max" in p, "PARAMS 的取值约束未说明"
    print(f"[OK] 类结构与 PARAMS 必填字段（{len(guard.PARAM_REQUIRED)} 个）齐备")


def test_module_level_and_reset_rules_stated():
    """这两条是 guard 里比 issue 原文多做的校验，prompt 必须先讲清楚"""
    p = text()
    assert "顶层" in p and "可执行语句" in p, "未说明模块顶层限制"
    assert "reset()" in p and "串到下一轮" in p, "未说明有状态必须实现 reset 的理由"
    print("[OK] 顶层限制与 reset 要求已说明（含理由，不只是规则）")


# ---------- 3. 输出格式与 extract 对齐 ----------
def test_delivery_format_parses():
    """把 prompt 里的交付格式示例照抄一遍，extract 必须认得出来"""
    assert "STRATEGY_KEY" in prompt.DELIVERY_PROMPT
    assert "```python" in prompt.DELIVERY_PROMPT

    sample = (
        "STRATEGY_KEY: sample_key\n"
        "```python\n"
        "from strategy.trade_strategy import TradeStrategy\n\n\n"
        "class S(TradeStrategy):\n"
        '    key = "sample_key"\n'
        '    name = "x"\n'
        '    description = "x"\n'
        '    example = ""\n'
        "    custom = True\n\n"
        "    def on_bar(self, broker, ctx):\n"
        "        pass\n"
        "```\n"
    )
    key, code = extract([sample])
    assert key == "sample_key" and code, f"prompt 承诺的格式 extract 解析不出来：{key!r}"
    print("[OK] prompt 里的交付格式能被 extract 正确解析")


def test_clarify_rules_present():
    p = text()
    assert "先给方案" in p, "澄清规则里缺「先给具体方案」"
    assert "最多 2 个" in p, "缺一次最多 2 问的限制"
    assert f"最多 {orchestrator.MAX_ASKS} 轮" in p, \
        f"prompt 里的澄清轮上限与 orchestrator.MAX_ASKS={orchestrator.MAX_ASKS} 不一致"
    # 不该问的清单
    for noise in ("手续费", "T+1", "初始资金", "信号源"):
        assert noise in p, f"「不要问」清单里缺 {noise}"
    print(f"[OK] 澄清规则齐备，轮次上限与 orchestrator（{orchestrator.MAX_ASKS}）一致")


def test_decide_for_me_instruction_matches():
    """「你决定就好」逃生口发的话，prompt 里得有对应的处理指令"""
    p = text()
    assert "你决定就好" in p, "prompt 未告诉模型如何响应「你决定就好」"
    assert "description" in orchestrator.DECIDE_FOR_ME, "逃生口没要求写进 description"
    print("[OK]「你决定就好」在 prompt 与 orchestrator 两侧对应")


# ---------- 4. 已占用 key ----------
def test_taken_keys_injected():
    p = text()
    for key in taken_keys():
        assert key in p, f"已占用的 key {key} 未注入 prompt，模型可能撞名"
    print(f"[OK] 已占用 key 全部注入：{sorted(taken_keys())}")


# ---------- 5. Broker API 与实现一致 ----------
def test_broker_api_matches_implementation():
    """prompt 里承诺的 broker 方法必须真实存在，否则模型照着写就崩"""
    from backtest.broker import Broker, Position

    p = text()
    # 负向后顾排除路径写法：prompt 里提到 `backtest/broker.py` 时不算 API 承诺
    promised = re.findall(r"(?<![/\w])broker\.(\w+)", p)
    assert promised, "prompt 里没描述 broker API"
    for attr in set(promised):
        assert hasattr(Broker, attr) or attr in Broker.__dataclass_fields__, \
            f"prompt 承诺 broker.{attr}，但 Broker 上没有"

    for attr in ("shares", "avg_cost", "buy_date"):
        assert attr in Position.__dataclass_fields__, f"prompt 提到 Position.{attr} 但不存在"
    print(f"[OK] prompt 承诺的 broker API 全部存在：{sorted(set(promised))}")


def test_bar_context_fields_match():
    from strategy.trade_strategy import BarContext

    p = text()
    promised = set(re.findall(r"ctx\.(\w+)", p))
    fields = set(BarContext.__dataclass_fields__)
    assert promised, "prompt 里没描述 ctx 字段"
    missing = promised - fields
    assert not missing, f"prompt 承诺了 BarContext 上不存在的字段：{missing}"
    # 反向：决策相关的核心字段必须都讲到，漏了模型无从下手
    for core in ("decision", "price", "prev_close", "board", "initial_capital"):
        assert core in promised, f"prompt 未说明 ctx.{core}"
    print(f"[OK] prompt 描述的 ctx 字段全部存在（{len(promised)}/{len(fields)} 个字段被提及）")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
