"""闸门 2：策略源码的 AST 静态校验（#30）

在 import 之前纯静态地看一遍源码，拦掉两类东西：

1. **能力越界**——import 白名单之外的模块、危险内建调用、dunder 属性访问
2. **契约不符**——不是恰好一个 TradeStrategy 子类、元数据缺失、PARAMS 结构错、
   有内部状态却没实现 reset()

一次返回**全部**错误而不是遇错即停：orchestrator 要把它们一起回灌给模型，
让它一轮改完，而不是每轮只修一条来回三次。

注意这不是沙箱（见 #30「已知限制」1）：白名单挡得住 `import os`，挡不住
`pandas.read_csv()` 这类经由白名单模块的间接文件访问。真隔离需要容器级方案。
"""

import ast
import re

from strategy.agent_gen.store import StoreError, validate_key

# 白名单根包：其自身与任意子模块都放行（numpy.random / pandas.api 等）
ALLOWED_ROOTS = {"pandas", "numpy", "math", "typing", "dataclasses"}
# 白名单精确模块：只放行这两个具体模块，不含同包下的其它模块。
# 不能放宽成整个 strategy 包——strategy.agent_gen.store 本身就有写文件的能力。
ALLOWED_EXACT = {"config.settings", "strategy.trade_strategy"}

# 仅用于给出更贴切的报错文案；不在白名单里的模块本来就会被拒
DENY_HINT = {
    "os": "文件系统与进程", "sys": "解释器内部", "subprocess": "子进程",
    "socket": "网络", "shutil": "文件系统", "pathlib": "文件系统",
    "importlib": "动态导入", "requests": "网络", "httpx": "网络",
    "urllib": "网络", "pickle": "反序列化执行", "ctypes": "本机内存",
    "builtins": "内建命名空间",
}

DANGEROUS_CALLS = {
    "exec", "eval", "compile", "__import__", "open",
    "globals", "locals", "vars", "breakpoint", "input", "exit", "quit",
}
ATTR_SETTERS = {"getattr", "setattr", "delattr"}

DUNDER = re.compile(r"^__.*__$")

# PARAMS 每项的必填字段（is_pct 可选，app.py:1201 用 .get 读）
PARAM_REQUIRED = ("key", "label", "default", "min", "max", "step")
PARAM_NUMERIC = ("default", "min", "max", "step")

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check(code: str, key: str, *, check_taken: bool = True) -> list[str]:
    """静态校验源码，返回全部错误（空列表 = 通过）。

    Args:
        code: 策略源码
        key: 期望的策略 key。显式传入而非从文件名推断——staging 文件名是
             `<run_id>__<key>.py`，推不出来（见 #30 命名设计 1.1）。
        check_taken: 是否校验 key 未被占用。改名后重跑校验时置 False，
                     因为此时目标 key 已由调用方自行确认。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"语法错误：第 {e.lineno} 行 {e.msg}"]

    errors: list[str] = []
    errors += _check_key(key, check_taken)
    errors += _check_imports(tree)
    errors += _check_calls(tree)
    errors += _check_module_level(tree)
    errors += _check_structure(tree, key)
    return errors


def check_path(path, key: str, **kw) -> list[str]:
    """按路径读取源码后校验（闸门流程用）"""
    from pathlib import Path
    return check(Path(path).read_text(encoding="utf-8"), key, **kw)


# ========== key ==========

def _check_key(key: str, check_taken: bool) -> list[str]:
    try:
        validate_key(key, check_taken=check_taken)
    except StoreError as e:
        return [str(e)]
    return []


# ========== 能力越界 ==========

def _module_allowed(module: str) -> bool:
    return module in ALLOWED_EXACT or module.split(".")[0] in ALLOWED_ROOTS


def _check_imports(tree: ast.Module) -> list[str]:
    errors = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # from . import x / from .. import y
                errors.append(f"第 {node.lineno} 行：禁止相对导入")
                continue
            modules = [node.module or ""]
        else:
            continue

        for module in modules:
            if _module_allowed(module):
                continue
            root = module.split(".")[0]
            if root in DENY_HINT:
                errors.append(
                    f"第 {node.lineno} 行：禁止 import {module}（{DENY_HINT[root]}）"
                )
            else:
                errors.append(
                    f"第 {node.lineno} 行：import {module} 不在白名单内。"
                    f"可用：{', '.join(sorted(ALLOWED_ROOTS | ALLOWED_EXACT))}"
                )
    return errors


def _check_calls(tree: ast.Module) -> list[str]:
    errors = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and DUNDER.match(node.attr):
            errors.append(
                f"第 {node.lineno} 行：禁止访问 dunder 属性 .{node.attr}"
                "（可绕过白名单拿到任意对象）"
            )
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            if fn.id in DANGEROUS_CALLS:
                errors.append(f"第 {node.lineno} 行：禁止调用 {fn.id}()")
            elif fn.id in ATTR_SETTERS and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.startswith("__"):
                    errors.append(
                        f"第 {node.lineno} 行：禁止用 {fn.id}() 访问 {arg.value}"
                    )
    return errors


def _check_module_level(tree: ast.Module) -> list[str]:
    """模块顶层只许有 import / 类 / 函数 / 常量赋值 / 文档字符串。

    自动发现会在 Streamlit 进程内 import 这个模块，顶层可执行语句（尤其是死循环）
    会直接卡死整站。闸门 3 在子进程里能超时拦下，但那是运行期兜底，这里先静态断掉。
    """
    allowed = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef,
               ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign)
    errors = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # 文档字符串
        if not isinstance(node, allowed):
            errors.append(
                f"第 {node.lineno} 行：模块顶层不允许可执行语句"
                f"（{type(node).__name__}），逻辑请放进策略类的方法里"
            )
    return errors


# ========== 契约结构 ==========

def _is_trade_strategy_base(base: ast.expr) -> bool:
    return ((isinstance(base, ast.Name) and base.id == "TradeStrategy")
            or (isinstance(base, ast.Attribute) and base.attr == "TradeStrategy"))


def find_strategy_classes(tree: ast.Module) -> list[ast.ClassDef]:
    """源码里所有继承 TradeStrategy 的类定义（extract.py 判定 deliver 时共用）"""
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and any(map(_is_trade_strategy_base, n.bases))]


def _class_attrs(cls: ast.ClassDef) -> dict[str, ast.expr]:
    """类级赋值 name -> 值节点"""
    out = {}
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.value is not None:
                out[stmt.target.id] = stmt.value
    return out


def _methods(cls: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    return {s.name: s for s in cls.body
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _is_property(fn: ast.FunctionDef) -> bool:
    return any(isinstance(d, ast.Name) and d.id == "property" for d in fn.decorator_list)


def _self_attrs_assigned(cls: ast.ClassDef) -> set[str]:
    """类内所有 `self.X = ...` 的 X（判断是否存在需要 reset 的内部状态）"""
    names = set()
    for node in ast.walk(cls):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for t in targets:
            if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                    and t.value.id == "self"):
                names.add(t.attr)
    return names


def _check_structure(tree: ast.Module, key: str) -> list[str]:
    classes = find_strategy_classes(tree)
    if len(classes) != 1:
        return [f"应恰好定义 1 个 TradeStrategy 子类，实际 {len(classes)} 个"]

    cls = classes[0]
    attrs = _class_attrs(cls)
    methods = _methods(cls)
    errors = []

    # --- 元数据 ---
    node = attrs.get("key")
    if node is None:
        errors.append(f'缺少类属性 key（应为 "{key}"）')
    elif not (isinstance(node, ast.Constant) and node.value == key):
        got = node.value if isinstance(node, ast.Constant) else "<非字面量>"
        errors.append(f'key 应为字面量 "{key}"，实际 {got!r}')

    node = attrs.get("custom")
    if not (isinstance(node, ast.Constant) and node.value is True):
        errors.append("自定义策略必须声明 custom = True")

    for field, required_nonempty in (("name", True), ("description", True), ("example", False)):
        node = attrs.get(field)
        fn = methods.get(field)
        if node is None and fn is None:
            errors.append(f"缺少 {field}（类属性字符串，或 @property 方法）")
        elif node is not None:
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                errors.append(f"{field} 必须是字符串字面量（动态内容请写成 @property）")
            elif required_nonempty and not node.value.strip():
                errors.append(f"{field} 不能为空")
        elif fn is not None and not _is_property(fn):
            errors.append(f"{field} 是方法但缺少 @property 装饰器")

    # --- on_bar ---
    on_bar = methods.get("on_bar")
    if on_bar is None:
        errors.append("必须实现 on_bar(self, broker, ctx)")
    else:
        n_args = len(on_bar.args.posonlyargs) + len(on_bar.args.args)
        if n_args != 3:
            errors.append(
                f"on_bar 应有 3 个位置参数 (self, broker, ctx)，实际 {n_args} 个"
                "（回测引擎按位置调用 on_bar(broker, ctx)）"
            )

    # --- PARAMS 与 __init__ ---
    errors += _check_params(attrs, methods)

    # --- 内部状态必须可重置 ---
    state = _self_attrs_assigned(cls) - _param_keys(attrs)
    if state and "reset" not in methods:
        errors.append(
            f"存在内部状态 {sorted(state)} 却未实现 reset()："
            "回测引擎每轮开始只调 reset()，否则上一轮的状态会串到下一轮"
        )

    return errors


def _param_keys(attrs: dict) -> set[str]:
    node = attrs.get("PARAMS")
    if node is None:
        return set()
    try:
        return {p["key"] for p in ast.literal_eval(node) if isinstance(p, dict) and "key" in p}
    except (ValueError, TypeError, SyntaxError):
        return set()


def _check_params(attrs: dict, methods: dict) -> list[str]:
    node = attrs.get("PARAMS")
    if node is None:
        return []  # 无可调参数是合法的

    try:
        params = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return ["PARAMS 必须是字面量（不能引用变量或调用函数，前端要在不实例化的情况下读它）"]

    if not isinstance(params, list):
        return [f"PARAMS 必须是 list，实际 {type(params).__name__}"]

    errors, seen = [], set()
    for i, p in enumerate(params):
        where = f"PARAMS[{i}]"
        if not isinstance(p, dict):
            errors.append(f"{where} 必须是 dict，实际 {type(p).__name__}")
            continue

        missing = [f for f in PARAM_REQUIRED if f not in p]
        if missing:
            errors.append(f"{where} 缺少字段 {missing}")
            continue

        pkey = p["key"]
        if not isinstance(pkey, str) or not IDENTIFIER.match(pkey):
            errors.append(f"{where} 的 key={pkey!r} 必须是合法的 Python 标识符（要当构造函数入参）")
        elif pkey in seen:
            errors.append(f"{where} 的 key={pkey!r} 重复")
        else:
            seen.add(pkey)

        if not isinstance(p["label"], str) or not p["label"].strip():
            errors.append(f"{where} 的 label 不能为空")

        nums = {}
        for f in PARAM_NUMERIC:
            v = p[f]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errors.append(f"{where} 的 {f} 必须是数字，实际 {v!r}")
            else:
                nums[f] = float(v)

        if len(nums) == len(PARAM_NUMERIC):
            if not nums["min"] <= nums["default"] <= nums["max"]:
                errors.append(
                    f"{where} 需满足 min <= default <= max，"
                    f"实际 {nums['min']} / {nums['default']} / {nums['max']}"
                )
            if nums["step"] <= 0:
                errors.append(f"{where} 的 step 必须为正，实际 {nums['step']}")

    # PARAMS 的 key 必须都是 __init__ 的入参：前端按 get_trade_strategy(key, **params)
    # 传参，对不上会在用户改参数时抛 TypeError，而冒烟回测用默认值跑不出来
    if seen:
        init = methods.get("__init__")
        if init is None:
            errors.append(f"声明了 PARAMS {sorted(seen)} 却没有 __init__ 接收它们")
        else:
            accepted = {a.arg for a in init.args.args[1:]} | {a.arg for a in init.args.kwonlyargs}
            if init.args.kwarg is None:
                unknown = sorted(seen - accepted)
                if unknown:
                    errors.append(f"PARAMS 中的 {unknown} 不是 __init__ 的参数")
    return errors
