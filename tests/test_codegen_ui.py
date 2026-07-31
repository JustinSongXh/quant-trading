"""「AI 生成新策略」前端面板测试（Streamlit AppTest，无头，不调用 LLM）

用 AppTest 真正跑一遍 app.py 并模拟点击，覆盖 #30「八」的交互：
provider 选择器 → 输入描述 → 澄清回合 →「你决定就好」→ 校验面板 → 保存并启用。

provider 用临时写进 providers/ 的假实现，所以这个测试同时也是那条可插拔性验收标准
的真实 UI 版：**丢一个文件进 providers/，它就出现在页面的选择器里**。
"""

import os
import shutil
import sys
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from streamlit.testing.v1 import AppTest

from strategy.agent_gen import registry, store
from strategy.trade_strategy import _CUSTOM_DIR, refresh_registry

APP = os.path.join(os.path.dirname(__file__), "..", "app.py")
KEY = "t_ui"
PROVIDER_FILE = registry._PROVIDER_DIR / "uifake.py"

# 假 provider：第 1 轮反问，第 2 轮交付一个能过全部闸门的策略。
# 写成文件而不是 import tests/fake_provider，因为要验证「自动发现」这条路。
PROVIDER_SRC = f'''
from strategy.agent_gen.base import CodegenProvider, Turn

CODE = """from strategy.trade_strategy import TradeStrategy


class UiFakeStrategy(TradeStrategy):
    key = "{KEY}"
    name = "UI 测试策略"
    description = "买入信号半仓、卖出信号清仓，仅供 UI 测试。"
    example = ""
    custom = True
    PARAMS = [{{"key": "ratio", "label": "仓位比例", "default": 0.5,
               "min": 0.1, "max": 1.0, "step": 0.1, "is_pct": True}}]

    def __init__(self, ratio: float = 0.5):
        self.ratio = float(ratio)

    def on_bar(self, broker, ctx):
        if ctx.decision == 1:
            shares = int(broker.cash * self.ratio / ctx.price)
            if shares > 0:
                broker.buy(ctx.symbol, ctx.price, shares, ctx.date, ctx.board)
        elif ctx.decision == -1:
            pos = broker.positions.get(ctx.symbol)
            if pos and broker.can_sell(ctx.symbol, ctx.date, ctx.board):
                broker.sell(ctx.symbol, ctx.price, pos.shares, ctx.date, ctx.board)
"""


class UiFakeProvider(CodegenProvider):
    key = "uifake"
    name = "UI 测试 provider"
    description = "不调用任何模型，第一轮反问、第二轮交付。"
    PARAMS = []

    def start(self, user_text):
        return Turn(kind="ask", texts=["进场基准用最近买入价还是持仓均价？推荐最近买入价。"],
                    session={{"n": 1}})

    def resume(self, session, user_text):
        return self.to_turn(
            ["好的，按推荐默认值定稿。", f"STRATEGY_KEY: {KEY}", "```python\\n" + CODE + "```"],
            {{**session, "n": session.get("n", 0) + 1}},
        )
'''


def setup():
    PROVIDER_FILE.write_text(PROVIDER_SRC, encoding="utf-8")
    registry.refresh_providers()


def cleanup():
    PROVIDER_FILE.unlink(missing_ok=True)
    sys.modules.pop("strategy.agent_gen.providers.uifake", None)
    shutil.rmtree(registry._PROVIDER_DIR / "__pycache__", ignore_errors=True)
    registry.refresh_providers()

    (_CUSTOM_DIR / f"{KEY}.py").unlink(missing_ok=True)
    sys.modules.pop(f"strategy.custom.{KEY}", None)
    shutil.rmtree(_CUSTOM_DIR / "__pycache__", ignore_errors=True)
    for d in (store.STAGING_DIR, store.REJECTED_DIR):
        for p in d.glob(f"*__{KEY}.py"):
            p.unlink(missing_ok=True)
    refresh_registry()


PROVIDER_NAME = "UI 测试 provider"


def open_page() -> AppTest:
    """打开「自定义策略」页，选中假 provider，冒烟走合成行情（离线、快）

    必须显式选 provider：真实的 claude_sdk 也是可用的，radio 默认项会打真实模型。
    """
    at = AppTest.from_file(APP, default_timeout=300)
    at.session_state["page"] = "自定义策略"
    at.session_state["cg_synthetic"] = True
    at.session_state["cg_provider"] = PROVIDER_NAME
    return at.run()


def texts(at) -> str:
    """把页面上的文字拼起来，用于断言提示语出现"""
    return "\n".join(
        [m.value for m in at.markdown] + [c.value for c in at.caption]
        + [s.value for s in at.success] + [w.value for w in at.warning]
        + [e.value for e in at.error] + [i.value for i in at.info]
    )


def wait_job(at, timeout=300):
    """驱动轮询直到任务出结果。

    生成是异步的（独立进程 + 每秒重跑的进度片段），所以一次 at.run() 只会停在
    「运行中」。真实浏览器里靠 fragment 的 run_every 自动刷，AppTest 里得手动推。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any("模型正在思考" in m.value for m in at.info):
            return at
        time.sleep(0.4)
        at.run()
    raise AssertionError(f"任务未在 {timeout}s 内完成")


def submit(at, text: str):
    """输入描述并推到有结果为止"""
    at.chat_input[0].set_value(text).run()
    return wait_job(at)


def click_and_wait(at, substr: str):
    button(at, substr).click().run()
    return wait_job(at)


def assert_fake_selected(at):
    """确认选中的是假 provider —— 真实 claude_sdk 也可用，选错就会打真模型"""
    for r in at.radio:
        if r.label == "生成方式":
            assert r.value == PROVIDER_NAME, f"选中的是 {r.value!r}，会打真实模型"
            return
    raise AssertionError("找不到「生成方式」选择器")


def button(at, substr: str):
    for b in at.button:
        if substr in b.label:
            return b
    raise AssertionError(f"找不到按钮 {substr!r}，现有：{[b.label for b in at.button]}")


# ---------- 1. 面板存在且 provider 被自动发现 ----------
def test_panel_and_provider_discovered():
    try:
        setup()
        at = open_page()
        assert not at.exception, at.exception

        labels = [e.label for e in at.expander]
        assert any("AI 生成新策略" in x for x in labels), labels
        # 丢文件进 providers/ 即出现在选择器选项里，没改任何前端代码
        opts = next((list(r.options) for r in at.radio if r.label == "生成方式"), [])
        assert PROVIDER_NAME in opts, f"provider 未出现在选择器：{opts}"
        assert at.chat_input, "没有输入框"
        print("[OK] 面板存在，providers/ 里的新文件自动出现在页面上（可插拔性 UI 版）")
    finally:
        cleanup()


def test_panel_survives_empty_custom_list():
    """自定义策略被删光时，面板仍要能用（否则用户没法再生成，死锁）"""
    try:
        setup()
        assert not [s for s in store.list_custom()], "测试前 custom/ 应为空"
        at = open_page()
        assert any("AI 生成新策略" in e.label for e in at.expander), \
            "custom 为空时面板消失了——用户将无法再生成策略"
        assert at.chat_input, "custom 为空时输入框消失了"
        print("[OK] custom/ 为空时面板与输入框仍在（不会把用户锁死）")
    finally:
        cleanup()


# ---------- 2. 澄清回合 ----------
def test_ask_round_shows_decide_button():
    try:
        setup()
        at = open_page()
        submit(at, "跌了就加仓，涨多了跑")
        assert not at.exception, at.exception

        assert_fake_selected(at)
        page = texts(at)
        assert "进场基准" in page, f"provider 的反问没进对话区：{page[:300]}"
        assert "已澄清 1/3 轮" in page, f"缺澄清轮次提示：{page[:300]}"
        button(at, "你决定就好")
        print("[OK] 澄清回合：反问进对话区、显示 1/3 轮、出现「你决定就好」")
    finally:
        cleanup()


# ---------- 3. 「你决定就好」→ 校验面板 ----------
def test_decide_then_validation_panel():
    try:
        setup()
        at = open_page()
        submit(at, "跌了就加仓，涨多了跑")
        click_and_wait(at, "你决定就好")
        assert not at.exception, at.exception

        page = texts(at)
        assert "三道闸门全部通过" in page, f"未出现校验面板：{page[:400]}"
        # 冒烟摘要（#30 八.6）
        assert "冒烟回测" in page and "净值" in page, f"缺冒烟摘要：{page[:400]}"
        # 三道闸门逐项反馈
        labels = [m.label for m in at.metric]
        assert any("闸门1" in x for x in labels) and any("闸门3" in x for x in labels), labels
        # 生成的代码可折叠查看，且不在对话气泡里
        assert any(f"{KEY}.py" in e.label for e in at.expander), \
            [e.label for e in at.expander]
        assert "class UiFakeStrategy" not in page, "源码漏进了对话区/正文"
        button(at, "保存并启用")
        print("[OK] 交付后：校验面板 + 三闸门 metric + 冒烟摘要 + 代码折叠（不进对话区）")
    finally:
        cleanup()


# ---------- 4. 保存并启用 ----------
def test_save_and_enable():
    try:
        setup()
        at = open_page()
        submit(at, "跌了就加仓，涨多了跑")
        click_and_wait(at, "你决定就好")
        button(at, "保存并启用").click().run()
        assert not at.exception, at.exception

        assert (_CUSTOM_DIR / f"{KEY}.py").exists(), "文件没落到 custom/"
        # 新策略出现在下方的策略 radio 里，且参数表单能渲染
        radios = {r.label: list(r.options) for r in at.radio}
        strategy_opts = next((v for k, v in radios.items() if k == "交易策略"), [])
        assert "UI 测试策略" in strategy_opts, f"新策略未出现在 radio：{radios}"
        print(f"[OK] 保存并启用后立即出现在策略 radio：{strategy_opts}")
    finally:
        cleanup()


def test_saved_list_and_delete():
    try:
        setup()
        at = open_page()
        submit(at, "随便")
        click_and_wait(at, "你决定就好")
        button(at, "保存并启用").click().run()

        assert any(KEY in e.label for e in at.expander), \
            f"已生成列表里没有查看源码入口：{[e.label for e in at.expander]}"
        button(at, "删除").click().run()
        assert not (_CUSTOM_DIR / f"{KEY}.py").exists(), "删除按钮没真删掉文件"
        print("[OK] 已生成策略管理：列表 / 查看源码 / 删除")
    finally:
        cleanup()


# ---------- 5. 无可用 provider ----------
def test_no_provider_shows_reasons():
    """一个都不可用时要说清为什么，而不是给个空下拉框

    真实的 claude_sdk 在本机是可用的，没法靠改文件造出「全都不可用」；
    AppTest 与被测脚本同进程，直接替掉 list_providers 即可。
    """
    original = registry.list_providers
    registry.list_providers = lambda only_available=False: (
        [] if only_available else [
            {"key": "a", "name": "A 家", "description": "", "params": [],
             "available": False, "reason": "未设置环境变量 A_KEY"},
            {"key": "b", "name": "B 家", "description": "", "params": [],
             "available": False, "reason": "claude CLI 不在 PATH 里"},
        ]
    )
    try:
        at = open_page()
        page = texts(at)
        assert "未设置环境变量 A_KEY" in page, f"没显示 A 家的原因：{page[:400]}"
        assert "claude CLI 不在 PATH 里" in page, f"没显示 B 家的原因：{page[:400]}"
        assert not at.chat_input, "没有可用 provider 时不该给输入框"
        print("[OK] 无可用 provider：逐个列出原因，不给空输入框")
    finally:
        registry.list_providers = original
        cleanup()


# ---------- 6. 运行中：进度 + 停止 ----------
SLOW_SRC = '''
import time

from strategy.agent_gen.base import CodegenProvider, Turn


class UiSlowProvider(CodegenProvider):
    key = "uislow"
    name = "UI 慢 provider"
    description = "睡很久，用来测运行中界面与停止按钮。"
    PARAMS = []

    def start(self, user_text):
        time.sleep(600)
        return Turn(kind="ask", texts=["到不了这里"], session={})

    def resume(self, session, user_text):
        return self.start(user_text)
'''
SLOW_FILE = registry._PROVIDER_DIR / "uislow.py"
SLOW_NAME = "UI 慢 provider"


def open_slow_page() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["page"] = "自定义策略"
    at.session_state["cg_synthetic"] = True
    at.session_state["cg_provider"] = SLOW_NAME
    return at.run()


def cleanup_slow():
    from strategy.agent_gen import jobs

    for owner in list(jobs._jobs):
        jobs.cancel(owner, "测试清理")
    SLOW_FILE.unlink(missing_ok=True)
    sys.modules.pop("strategy.agent_gen.providers.uislow", None)
    shutil.rmtree(registry._PROVIDER_DIR / "__pycache__", ignore_errors=True)
    registry.refresh_providers()


def test_running_shows_progress_and_stop():
    """任务在跑时：只显示进度 + 停止按钮，其余 UI（输入框等）一律不出现"""
    from strategy.agent_gen import jobs

    try:
        SLOW_FILE.write_text(SLOW_SRC, encoding="utf-8")
        registry.refresh_providers()
        at = open_slow_page()
        at.chat_input[0].set_value("一个跑很久的描述").run()
        assert not at.exception, at.exception

        assert jobs.active_count() == 1, "任务没起来"
        page = texts(at)
        assert "模型正在思考" in page, f"没显示进度：{page[:300]}"
        assert "刷新或关闭页面会自动终止" in page, "没告诉用户刷新会自动终止"
        button(at, "停止")
        # 运行期间面板的其余控件都要收起来。断言按钮而不是 chat_input：
        # chat_input 属于 Streamlit 底部固定容器，AppTest 里不随重跑清除，
        # 拿它做断言会得到假阳性（按钮的清除是可靠的）。
        panel_buttons = [b.label for b in at.button
                         if any(x in b.label for x in ("你决定就好", "保存并启用",
                                                       "重新开始", "删除", "换个名字"))]
        assert not panel_buttons, f"任务在跑却还显示其它面板控件：{panel_buttons}"
        print("[OK] 运行中：显示进度 + 停止按钮，其余面板控件收起")

        button(at, "停止").click().run()
        assert jobs.active_count() == 0, "点了停止但任务还在跑"
        assert not any("模型正在思考" in m.value for m in at.info), "停止后还显示进度"
        assert at.chat_input, "停止后输入框没回来"
        print("[OK] 点「停止」真的杀掉任务，界面回到可输入状态")
    finally:
        cleanup_slow()


def test_orphan_job_leaves_panel_clean_and_gets_reaped():
    """刷新页面的等价场景：任务的主人不在了 → 面板干净可输入，任务被回收

    AppTest 的 session id 固定为 "test session id"，再开一个 AppTest 拿到的是同一个
    owner，所以没法真的「换会话」。这里把 job 挪到另一个 owner 名下，等价于
    「这个任务的主人已经走了」——正是刷新页面后的状态。
    （回收逻辑本身在 tests/test_codegen_jobs.py 里用真实的不同 owner 覆盖。）
    """
    from strategy.agent_gen import jobs

    try:
        SLOW_FILE.write_text(SLOW_SRC, encoding="utf-8")
        registry.refresh_providers()
        at = open_slow_page()
        at.chat_input[0].set_value("一个跑很久的描述").run()
        assert jobs.active_count() == 1
        owner = next(iter(jobs._jobs))

        job = jobs._jobs.pop(owner)
        jobs._jobs["session-gone"] = job

        at2 = open_slow_page()
        assert at2.chat_input, "主人换了之后，本会话的面板应是干净可输入状态"
        assert not any("模型正在思考" in m.value for m in at2.info), \
            "本会话不该显示别人的任务进度"

        job.last_seen -= jobs.STALE_SECONDS + 1     # 拨老心跳，免得等满 8 秒
        assert jobs.sweep() >= 1, "无人轮询的孤儿任务没被回收"
        assert jobs.active_count() == 0
        print("[OK] 孤儿任务：面板不受影响，任务被回收，不再消耗额度")
    finally:
        cleanup_slow()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
