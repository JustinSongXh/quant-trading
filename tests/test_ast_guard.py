"""strategy/agent_gen/guard.py 闸门 2 测试（确定性，无需联网、不调用 LLM）

覆盖：
1. 真实内置策略源码（PlayDeadQuarterStrategy）必须原样通过——契约校验不能比现状还严
2. import 白名单：放行/拒绝/相对导入
3. 危险调用与 dunder 属性
4. 模块顶层可执行语句
5. 结构契约：子类数量、key、custom、元数据、on_bar 签名
6. PARAMS：字面量、必填字段、类型、min<=default<=max、与 __init__ 对齐
7. 内部状态与 reset()
8. 一次返回全部错误（供 orchestrator 一轮回灌）
"""

import inspect
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.agent_gen import guard
from strategy.trade_strategy import PlayDeadQuarterStrategy

KEY = "t_guard"
IMPORT = "from strategy.trade_strategy import TradeStrategy"


def make(*, key=f'"{KEY}"', custom="True", name='"测试策略"', description='"说明"',
         example='""', params=None, init=None, on_bar="        pass",
         methods="", header=IMPORT, top="", base="TradeStrategy") -> str:
    """拼一个策略源码；每个参数传 None 表示「整行不出现」"""
    lines = [header, "", top, "", f"class S({base}):"]
    for field, val in (("key", key), ("name", name), ("description", description),
                       ("example", example), ("custom", custom)):
        if val is not None:
            lines.append(f"    {field} = {val}")
    if params is not None:
        lines.append(f"    PARAMS = {params}")
    if init is not None:
        lines.append(init)
    if on_bar is not None:
        lines.append("    def on_bar(self, broker, ctx):")
        lines.append(on_bar)
    if methods:
        lines.append(methods)
    return "\n".join(lines) + "\n"


def ok(code, **kw):
    errors = guard.check(code, KEY, **kw)
    assert not errors, f"应通过但报错：{errors}"


def bad(code, *needles, **kw):
    errors = guard.check(code, KEY, **kw)
    assert errors, "应报错但通过了"
    blob = " | ".join(errors)
    for n in needles:
        assert n in blob, f"错误信息里应含 {n!r}，实际：{blob}"
    return errors


# ---------- 1. 真实策略源码 ----------
def test_real_builtin_strategy_passes():
    code = IMPORT + "\n\n\n" + inspect.getsource(PlayDeadQuarterStrategy)
    errors = guard.check(code, "playdead_quarter", check_taken=False)
    assert not errors, f"内置「装死四分之一」被闸门拒了：{errors}"
    print("[OK] 真实内置策略 PlayDeadQuarterStrategy 原样通过闸门 2")


def test_minimal_strategy_passes():
    ok(make())
    ok(make(description=None, methods="""    @property
    def description(self):
        return "动态说明"
"""))
    print("[OK] 最小策略 / description 写成 @property 均通过")


# ---------- 2. import ----------
def test_imports():
    for mod in ["pandas", "numpy", "math", "typing", "dataclasses",
                "numpy.random", "pandas.api.types", "config.settings",
                "strategy.trade_strategy"]:
        ok(make(top=f"import {mod}"))
        ok(make(top=f"from {mod} import x" if "." in mod else f"from {mod} import x"))

    for mod, hint in [("os", "文件系统"), ("sys", "解释器"), ("subprocess", "子进程"),
                      ("socket", "网络"), ("shutil", "文件系统"), ("pathlib", "文件系统"),
                      ("importlib", "动态导入"), ("requests", "网络"), ("httpx", "网络")]:
        bad(make(top=f"import {mod}"), f"禁止 import {mod}", hint)
        bad(make(top=f"from {mod} import x"), "禁止 import")

    bad(make(top="import os.path"), "禁止 import os.path")
    bad(make(top="import antigravity"), "不在白名单内")
    # strategy 包只放行 trade_strategy，不能顺着包名摸到有写能力的模块
    bad(make(top="from strategy.agent_gen import store"), "不在白名单内")
    bad(make(top="from . import sibling"), "禁止相对导入")
    # 函数体内的 import 同样要拦
    bad(make(on_bar="        import os"), "禁止 import os")
    print("[OK] import 白名单：放行 9 类、拒绝 11 类（含包内旁路与函数内 import）")


# ---------- 3. 危险调用 / dunder ----------
def test_dangerous_calls():
    for fn in ["exec", "eval", "compile", "__import__", "open",
               "globals", "locals", "vars", "breakpoint", "input", "exit", "quit"]:
        bad(make(on_bar=f'        {fn}("x")'), f"禁止调用 {fn}()")

    bad(make(on_bar='        getattr(ctx, "__class__")'), "禁止用 getattr() 访问 __class__")
    bad(make(on_bar='        setattr(ctx, "__dict__", {})'), "禁止用 setattr() 访问 __dict__")
    bad(make(on_bar="        x = ctx.__class__.__bases__"), "dunder")
    bad(make(on_bar="        x = broker.__dict__"), "dunder")
    # 单下划线私有属性是正常写法，不能误伤
    ok(make(on_bar="        x = self._cache", methods="""    def reset(self):
        self._cache = None
"""))
    # getattr 取普通属性放行
    ok(make(on_bar='        x = getattr(ctx, "price", 0)'))
    print("[OK] 危险内建 12 个 + dunder 访问全部拦下，私有属性/普通 getattr 不误伤")


# ---------- 4. 模块顶层 ----------
def test_module_level_statements():
    bad(make(top="while True:\n    pass"), "模块顶层不允许可执行语句")
    bad(make(top='print("hi")'), "模块顶层不允许可执行语句")
    bad(make(top="for i in range(10):\n    pass"), "模块顶层不允许可执行语句")
    # 常量赋值、文档字符串、辅助函数是允许的
    ok(make(top="THRESHOLD = 0.1"))
    ok(make(header='"""模块文档"""\n' + IMPORT))
    ok(make(top="def helper(x):\n    return x * 2"))
    print("[OK] 顶层死循环/print/for 被拦（会卡死自动发现），常量与辅助函数放行")


# ---------- 5. 结构契约 ----------
def test_class_count():
    bad(make(base="object"), "应恰好定义 1 个 TradeStrategy 子类，实际 0 个")
    bad(make() + "\n\n" + make(header="").split("\n", 2)[2].replace("class S(", "class S2("),
        "实际 2 个")
    print("[OK] TradeStrategy 子类必须恰好 1 个")


def test_metadata():
    bad(make(key=None), "缺少类属性 key")
    bad(make(key='"other"'), f'key 应为字面量 "{KEY}"', "'other'")
    bad(make(key="SOME_CONST"), "<非字面量>")
    bad(make(custom=None), "custom = True")
    bad(make(custom="False"), "custom = True")
    bad(make(name=None), "缺少 name")
    bad(make(name='"  "'), "name 不能为空")
    bad(make(description='""'), "description 不能为空")
    bad(make(description="f'x{1}'"), "必须是字符串字面量")
    bad(make(example=None), "缺少 example")
    ok(make(example='""'))  # example 允许为空串
    bad(make(description=None, methods="""    def description(self):
        return "x"
"""), "缺少 @property")
    print("[OK] key / custom / name / description / example 元数据校验")


def test_on_bar_signature():
    bad(make(on_bar=None), "必须实现 on_bar")
    bad(make(on_bar="        pass").replace("def on_bar(self, broker, ctx):",
                                            "def on_bar(self, ctx):"),
        "应有 3 个位置参数")
    bad(make(on_bar="        pass").replace("def on_bar(self, broker, ctx):",
                                            "def on_bar(self, broker, ctx, extra):"),
        "应有 3 个位置参数")
    print("[OK] on_bar 必须存在且恰好 3 个位置参数")


# ---------- 6. PARAMS ----------
GOOD_PARAM = ('[{"key": "ratio", "label": "比例", "default": 0.5, '
              '"min": 0.1, "max": 1.0, "step": 0.1, "is_pct": True}]')
INIT = "    def __init__(self, ratio: float = 0.5):\n        self.ratio = ratio"


def test_params_valid():
    ok(make(params=GOOD_PARAM, init=INIT))
    ok(make(params="[]"))
    ok(make(params=None))
    ok(make(params=GOOD_PARAM.replace(', "is_pct": True', ""), init=INIT))  # is_pct 可选
    ok(make(params=GOOD_PARAM, init="    def __init__(self, **kw):\n        pass"))
    print("[OK] 合法 PARAMS（含省略 is_pct、**kwargs 接收）通过")


def test_params_invalid():
    bad(make(params="build_params()", init=INIT), "PARAMS 必须是字面量")
    bad(make(params='{"key": "a"}'), "PARAMS 必须是 list")
    bad(make(params="[1]"), "PARAMS[0] 必须是 dict")
    bad(make(params='[{"key": "ratio"}]'), "缺少字段")
    bad(make(params=GOOD_PARAM.replace('"ratio"', '"has space"'), init=INIT), "合法的 Python 标识符")
    bad(make(params=f"[{GOOD_PARAM[1:-1]}, {GOOD_PARAM[1:-1]}]", init=INIT), "重复")
    bad(make(params=GOOD_PARAM.replace('"比例"', '"  "'), init=INIT), "label 不能为空")
    bad(make(params=GOOD_PARAM.replace('"default": 0.5', '"default": "x"'), init=INIT),
        "default 必须是数字")
    bad(make(params=GOOD_PARAM.replace('"default": 0.5', '"default": 9.0'), init=INIT),
        "min <= default <= max")
    bad(make(params=GOOD_PARAM.replace('"step": 0.1', '"step": 0'), init=INIT), "step 必须为正")
    print("[OK] PARAMS 各类结构错误逐条拦下")


def test_params_match_init():
    bad(make(params=GOOD_PARAM), "没有 __init__ 接收它们")
    bad(make(params=GOOD_PARAM, init="    def __init__(self, other=1):\n        self.other = other"),
        "不是 __init__ 的参数")
    ok(make(params=GOOD_PARAM,
            init="    def __init__(self, *, ratio=0.5):\n        self.ratio = ratio"))
    print("[OK] PARAMS 的 key 必须能被 __init__ 接收（否则用户改参数即 TypeError）")


# ---------- 7. reset ----------
def test_reset_required_for_state():
    stateful = make(on_bar="        self.stack = []")
    bad(stateful, "未实现 reset()", "['stack']")
    ok(make(on_bar="        self.stack = []",
            methods="    def reset(self):\n        self.stack = []"))
    # PARAMS 参数不算内部状态，不该强制 reset
    ok(make(params=GOOD_PARAM, init=INIT))
    print("[OK] 有内部状态必须实现 reset()，PARAMS 参数不误判为状态")


# ---------- 8. 一次返回全部错误 ----------
def test_reports_all_errors_at_once():
    errors = guard.check(make(top="import os", custom="False", on_bar="        eval('1')",
                              name='""'), KEY)
    blob = " | ".join(errors)
    for needle in ["禁止 import os", "custom = True", "禁止调用 eval()", "name 不能为空"]:
        assert needle in blob, f"{needle} 未报告：{blob}"
    assert len(errors) >= 4, errors
    print(f"[OK] 一次返回全部 {len(errors)} 条错误，供 orchestrator 一轮回灌")


def test_syntax_error():
    errors = guard.check("class S(:\n", KEY)
    assert len(errors) == 1 and "语法错误" in errors[0], errors
    print("[OK] 语法错误单独短路返回")


def test_key_taken():
    assert any("已被占用" in e for e in guard.check(make(key='"default"'), "default"))
    assert not any("已被占用" in e
                   for e in guard.check(make(key='"default"'), "default", check_taken=False))
    print("[OK] key 撞名在闸门 2 即拦下（check_taken=False 可跳过）")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
