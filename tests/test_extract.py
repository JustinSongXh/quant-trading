"""strategy/agent_gen/extract.py deliver 判定测试（确定性，无需联网、不调用 LLM）

核心是三条件同时满足才算交付。最要紧的一条是：澄清过程中模型顺手贴的示意代码
**不能**被误判成交付——否则白跑一遍闸门还往 _staging 里塞垃圾。
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.agent_gen.extract import extract

CODE = '''from strategy.trade_strategy import TradeStrategy


class DipBuyStrategy(TradeStrategy):
    key = "dip_buy"
    name = "逢跌加仓"
    description = "跌 5% 加仓"
    example = ""
    custom = True

    def on_bar(self, broker, ctx):
        pass
'''


def block(code=CODE, lang="python", fence="```"):
    return f"{fence}{lang}\n{code}{fence}"


def deliver(texts, key="dip_buy"):
    k, c = extract(texts)
    assert k == key, f"key 应为 {key}，实际 {k!r}"
    assert c and "class DipBuyStrategy" in c, f"code 提取失败：{c!r}"
    return k, c


def ask(texts, why=""):
    k, c = extract(texts)
    assert (k, c) == (None, None), f"应判为澄清（{why}），实际提取到 key={k!r}"


# ---------- 交付 ----------
def test_deliver_basic():
    deliver([f"好的，按你说的实现：\n\nSTRATEGY_KEY: dip_buy\n{block()}"])
    print("[OK] 标准交付格式正确识别")


def test_deliver_across_texts():
    """provider 可能把说明与代码拆成多个文本块返回"""
    deliver(["我先说明一下思路。", "STRATEGY_KEY: dip_buy", block()])
    print("[OK] key 与代码块分散在不同文本块时仍能拼合识别")


def test_deliver_lang_and_fence_variants():
    for lang in ["python", "py", "python3", "Python", "PY"]:
        deliver([f"STRATEGY_KEY: dip_buy\n{block(lang=lang)}"])
    for fence in ["```", "````", "`````"]:
        deliver([f"STRATEGY_KEY: dip_buy\n{block(fence=fence)}"])
    # 全角冒号、列表符号前缀（模型偶发）
    deliver(["STRATEGY_KEY： dip_buy\n" + block()])
    deliver(["- STRATEGY_KEY: dip_buy\n" + block()])
    deliver(["STRATEGY_KEY: `dip_buy`\n" + block()])
    print("[OK] 语言标记 / 反引号数量 / 全角冒号 / 列表前缀 等变体均兼容")


def test_takes_last_candidate():
    """模型先贴草稿再贴定稿时，取最后一个"""
    draft = CODE.replace("DipBuyStrategy", "DraftStrategy").replace("跌 5% 加仓", "草稿")
    texts = [f"先给个草稿：\n{block(draft)}\n改好了：\nSTRATEGY_KEY: dip_buy\n{block()}"]
    _, code = deliver(texts)
    assert "DraftStrategy" not in code, "取到了草稿而不是定稿"

    texts = [f"STRATEGY_KEY: old_key\n改名了\nSTRATEGY_KEY: dip_buy\n{block()}"]
    deliver(texts)
    print("[OK] 多个候选时取最后一个（草稿 → 定稿）")


# ---------- 澄清（三条件缺一） ----------
def test_ask_no_strategy_key():
    """条件 1 缺失：只有代码块没有 STRATEGY_KEY —— 澄清时贴的示意代码"""
    ask([f"大概是这个意思：\n{block()}\n你看进场基准用最近买入价还是持仓均价？"],
        "无 STRATEGY_KEY")
    print("[OK] 只有代码块无 STRATEGY_KEY → 澄清（示意代码不被误判为交付）")


def test_ask_no_code_block():
    """条件 2 缺失：只有 key 没有代码块"""
    ask(["STRATEGY_KEY: dip_buy\n\n我打算这么实现，先确认下参数默认值？"], "无代码块")
    ask([f"STRATEGY_KEY: dip_buy\n```\n{CODE}```"], "代码块无 python 语言标记")
    ask([f"STRATEGY_KEY: dip_buy\n```text\n{CODE}```"], "语言标记不是 python")
    print("[OK] 无代码块 / 无语言标记 / 非 python 标记 → 澄清")


def test_ask_syntax_error():
    """条件 3 缺失之一：代码块语法错"""
    ask([f"STRATEGY_KEY: dip_buy\n{block('class Broken(:\\n    pass\\n')}"], "语法错")
    print("[OK] 代码块无法 ast.parse → 澄清（不进闸门）")


def test_ask_no_strategy_subclass():
    """条件 3 缺失之二：代码块合法但不含 TradeStrategy 子类"""
    ask([f"STRATEGY_KEY: dip_buy\n{block('def helper(x):\\n    return x * 2\\n')}"], "无子类")
    ask([f"STRATEGY_KEY: dip_buy\n{block('class Foo:\\n    pass\\n')}"], "普通类不算")
    print("[OK] 代码块里没有 TradeStrategy 子类 → 澄清")


def test_ask_invalid_key():
    for bad in ["Dip_Buy", "dip-buy", "ab", "1dip", "dip buy"]:
        ask([f"STRATEGY_KEY: {bad}\n{block()}"], f"key={bad} 非法")
    print("[OK] STRATEGY_KEY 不符合 key 规范 → 澄清")


def test_ask_empty():
    ask([])
    ask([""])
    ask(["就是那种跌了就买的策略"])
    print("[OK] 空回复 / 纯自然语言 → 澄清")


def test_mixed_illustrative_then_delivery():
    """真实澄清流程：先示意（不算交付），用户确认后才交付"""
    turn1 = [f"我理解是这样，示意一下：\n{block()}\n仓位粒度用 4 档可以吗？"]
    ask(turn1, "第一轮只是示意")
    turn2 = [f"好，定稿：\n\nSTRATEGY_KEY: dip_buy\n{block()}"]
    deliver(turn2)
    print("[OK] 同一段代码：澄清轮不算交付，定稿轮才算")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
