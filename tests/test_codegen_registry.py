"""strategy/agent_gen/registry.py provider 自动发现测试

对应 #30 的可插拔性验收标准：**把一个 provider 文件放进 providers/，
无需改动任何其他文件即出现在 provider 选择器中。** 本测试就是那条标准的机器版。
"""

import os
import shutil
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.agent_gen import registry
from strategy.agent_gen.base import CodegenProvider

PROVIDER_DIR = registry._PROVIDER_DIR

GOOD = '''
from strategy.agent_gen.base import CodegenProvider, Turn


class {cls}(CodegenProvider):
    key = "{key}"
    name = "{name}"
    description = "测试用"
    PARAMS = [{{"key": "temperature", "label": "温度", "default": 0.2,
               "min": 0.0, "max": 1.0, "step": 0.1}}]

    @classmethod
    def available(cls):
        return {available}

    def __init__(self, temperature: float = 0.2):
        super().__init__(temperature=temperature)
        self.temperature = temperature

    def start(self, user_text):
        return Turn(kind="ask", texts=["嗯？"], session={{}})

    def resume(self, session, user_text):
        return Turn(kind="ask", texts=["嗯？"], session=session)
'''


def write(name: str, content: str):
    (PROVIDER_DIR / f"{name}.py").write_text(content, encoding="utf-8")


def cleanup(*names):
    for n in names:
        (PROVIDER_DIR / f"{n}.py").unlink(missing_ok=True)
        sys.modules.pop(f"strategy.agent_gen.providers.{n}", None)
    shutil.rmtree(PROVIDER_DIR / "__pycache__", ignore_errors=True)
    registry.refresh_providers()


def keys(**kw):
    return [p["key"] for p in registry.list_providers(**kw)]


# ---------- 1. 丢文件即出现（可插拔性验收） ----------
def test_drop_in_file_appears():
    try:
        assert "t_prov" not in keys()
        write("t_prov", GOOD.format(cls="TProv", key="t_prov", name="临时 provider",
                                    available="(True, '')"))
        registry.refresh_providers()

        metas = registry.list_providers()
        meta = next(p for p in metas if p["key"] == "t_prov")
        assert meta["name"] == "临时 provider"
        assert meta["available"] is True and meta["reason"] == ""
        # PARAMS 原样透出，前端复用 _render_strategy_params 渲染
        assert [p["key"] for p in meta["params"]] == ["temperature"]

        # 能构造，params 透传
        provider = registry.get_provider("t_prov", temperature=0.7)
        assert isinstance(provider, CodegenProvider) and provider.temperature == 0.7
        turn = provider.start("随便说点什么")
        assert turn.kind == "ask"
        print("[OK] 往 providers/ 丢一个文件即出现在选择器中，无需改动别处")
    finally:
        cleanup("t_prov")


# ---------- 2. available() 过滤 ----------
def test_available_filtering():
    try:
        write("t_ok2", GOOD.format(cls="A", key="t_ok2", name="可用", available="(True, '')"))
        write("t_no", GOOD.format(cls="B", key="t_no", name="不可用",
                                  available="(False, '未设置环境变量 FOO_KEY')"))
        registry.refresh_providers()

        assert "t_no" in keys(), "不可用的也要列出来（前端要显示原因）"
        assert "t_no" not in keys(only_available=True)
        assert "t_ok2" in keys(only_available=True)

        meta = next(p for p in registry.list_providers() if p["key"] == "t_no")
        assert meta["available"] is False
        assert "FOO_KEY" in meta["reason"], meta["reason"]
        print("[OK] available() 过滤生效，不可用的带原因列出而非静默消失")
    finally:
        cleanup("t_ok2", "t_no")


def test_available_raising_does_not_break_selector():
    try:
        write("t_raise", GOOD.format(cls="C", key="t_raise", name="检测会炸",
                                     available="1 / 0"))
        write("t_fine", GOOD.format(cls="D", key="t_fine", name="正常",
                                    available="(True, '')"))
        registry.refresh_providers()

        meta = next(p for p in registry.list_providers() if p["key"] == "t_raise")
        assert meta["available"] is False
        assert "ZeroDivisionError" in meta["reason"], meta["reason"]
        assert "t_fine" in keys(only_available=True), "一个 available() 炸了拖累了其余"
        print("[OK] available() 自身抛异常时降级为不可用 + 原因，不拖垮选择器")
    finally:
        cleanup("t_raise", "t_fine")


# ---------- 3. 坏文件只跳过 ----------
def test_bad_files_skipped():
    bad = {
        "t_syntax": "class Broken(:\n",                                       # 语法错
        "t_nocls": "X = 1\n",                                                 # 无子类
        "t_two": (GOOD.format(cls="A", key="t_two", name="a", available="(True, '')")
                  + GOOD.format(cls="B", key="t_two", name="b", available="(True, '')")),
        "t_mismatch": GOOD.format(cls="M", key="wrong", name="m", available="(True, '')"),
        "t_import": "import nonexistent_module_xyz\n",                        # import 失败
    }
    names = list(bad)
    try:
        for n, c in bad.items():
            write(n, c)
        write("t_good3", GOOD.format(cls="G", key="t_good3", name="好的",
                                     available="(True, '')"))
        registry.refresh_providers()

        ks = keys()
        for n in names:
            assert n not in ks, f"坏 provider {n} 不该被注册"
        assert "wrong" not in ks
        assert "t_good3" in ks, "坏文件拖累了正常 provider 的发现"
        print(f"[OK] {len(names)} 类坏 provider 文件全部跳过，正常 provider 不受影响")
    finally:
        cleanup(*names, "t_good3")


# ---------- 4. 下划线文件不被发现 ----------
def test_underscore_files_ignored():
    registry.refresh_providers()
    ks = keys()
    assert "_template" not in ks, "_template.py 是给人抄的空壳，不该被注册"
    assert not any(k.startswith("_") for k in ks), ks
    print("[OK] _template.py 等下划线文件不被发现")


# ---------- 5. 默认 provider 回退 ----------
def test_default_provider_falls_back():
    from config.settings import DEFAULT_CODEGEN_PROVIDER
    try:
        # 配置里的默认 provider 尚未实现时，应退到第一个可用的而非返回它
        write("t_only", GOOD.format(cls="O", key="t_only", name="唯一可用",
                                    available="(True, '')"))
        registry.refresh_providers()
        chosen = registry.default_provider_key()
        usable = keys(only_available=True)
        assert chosen in usable, f"{chosen} 不在可用列表 {usable}"
        if DEFAULT_CODEGEN_PROVIDER not in usable:
            assert chosen != DEFAULT_CODEGEN_PROVIDER, "返回了不可用的默认 provider"
        print(f"[OK] 默认 provider 回退正常（配置={DEFAULT_CODEGEN_PROVIDER}，选中={chosen}）")
    finally:
        cleanup("t_only")


def test_default_provider_none_when_empty():
    registry.refresh_providers()
    if not keys(only_available=True):
        assert registry.default_provider_key() is None
        print("[OK] 无任何可用 provider 时返回 None（前端据此显示各自的不可用原因）")
    else:
        print(f"[跳过] 当前已有可用 provider：{keys(only_available=True)}")


def test_unknown_provider_raises():
    try:
        registry.get_provider("nope_not_exist")
        raise AssertionError("未知 provider 竟然构造成功")
    except ValueError as e:
        assert "unknown codegen provider" in str(e), e
    print("[OK] 未知 provider key 抛 ValueError")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
