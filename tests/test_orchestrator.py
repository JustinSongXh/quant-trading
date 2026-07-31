"""strategy/agent_gen/orchestrator.py 全链路测试（FakeProvider，不打真实 LLM）

用 FakeProvider 按脚本喂回合，覆盖 #30「六」的主循环全部分支。冒烟回测走
synthetic=True（离线、确定性）；需要「闸门失败」的用例故意让闸门 2 失败，
避免每轮都跑一遍真回测把测试拖慢。
"""

import os
import shutil
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_provider import DELIVER_PROSE, FakeProvider, deliver, strategy_code
from strategy.agent_gen import orchestrator as orch
from strategy.agent_gen import store
from strategy.trade_strategy import _CUSTOM_DIR, list_trade_strategies, refresh_registry

SMOKE = {"synthetic": True}
KEY = "t_orch"

# 过不了闸门 2：import os 在白名单外
BAD_GUARD = strategy_code(KEY).replace(
    "    def on_bar(self, broker, ctx):", "    def on_bar(self, broker, ctx):\n        import os")

# 过闸门 2 但过不了闸门 3：on_bar 抛异常
BAD_SMOKE = strategy_code(KEY, body="        1 / 0")


def state_for(script, **kw):
    provider = FakeProvider(script, **kw)
    return provider, orch.new_state("fake")


def cleanup(*keys):
    for k in (KEY, *keys):
        (_CUSTOM_DIR / f"{k}.py").unlink(missing_ok=True)
        sys.modules.pop(f"strategy.custom.{k}", None)
        for d in (store.STAGING_DIR, store.REJECTED_DIR):
            for p in d.glob(f"*__{k}.py"):
                p.unlink(missing_ok=True)
    shutil.rmtree(_CUSTOM_DIR / "__pycache__", ignore_errors=True)
    refresh_registry()


def assistant_texts(state):
    return [m.text for m in state.messages if m.role == "assistant"]


# ---------- 1. 一次过 ----------
def test_happy_path():
    try:
        provider, state = state_for([deliver(KEY)])
        state = orch.advance(provider, state, "跌 5% 加仓 20%", smoke_options=SMOKE)

        assert state.status == orch.READY, f"{state.status} / {state.error}"
        assert state.key == KEY and state.code
        assert state.asks == 0 and state.fixes == []
        # 通过闸门后文件仍在 staging，没被自动启用
        assert store.staging_path(state.run_id, KEY).exists()
        assert KEY not in [s["key"] for s in list_trade_strategies()]
        print("[OK] 一次过：READY 且未自动启用（等用户点保存）")
    finally:
        cleanup()


def test_promote_after_ready():
    try:
        provider, state = state_for([deliver(KEY)])
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)
        path = orch.promote(state)

        assert state.status == orch.SAVED
        assert path == _CUSTOM_DIR / f"{KEY}.py"
        assert KEY in [s["key"] for s in list_trade_strategies()], "保存后未出现在注册表"
        print("[OK] 保存并启用后策略立即可选")
    finally:
        cleanup()


def test_promote_rejected_when_not_ready():
    provider, state = state_for(["还有两个问题"])
    state = orch.advance(provider, state, "跌了就加仓", smoke_options=SMOKE)
    try:
        orch.promote(state)
        raise AssertionError("非 READY 状态竟然允许保存")
    except store.StoreError as e:
        assert "不可保存" in str(e), e
    print("[OK] 非 READY 状态拒绝保存")


# ---------- 2. 澄清 ----------
def test_two_asks_then_deliver():
    try:
        provider, state = state_for(["进场基准用最近买入价还是持仓均价？",
                                     "仓位分 4 档可以吗？",
                                     deliver(KEY)])
        state = orch.advance(provider, state, "跌了就加仓，涨多了跑", smoke_options=SMOKE)
        assert state.status == orch.ASKING and state.asks == 1
        assert state.can_submit and state.asks_left == 2

        state = orch.advance(provider, state, "最近买入价", smoke_options=SMOKE)
        assert state.status == orch.ASKING and state.asks == 2

        state = orch.advance(provider, state, "可以", smoke_options=SMOKE)
        assert state.status == orch.READY, f"{state.status} / {state.error}"

        # session 被原样传回：FakeProvider 每轮累加 turns
        assert state.session["turns"] == 3, state.session
        assert provider.prompts == ["跌了就加仓，涨多了跑", "最近买入价", "可以"]
        # 对话区 3 条用户 + 3 条助手（2 次反问 + 1 段交付说明），源码不进对话区
        assert len(state.messages) == 6, [(m.role, m.text[:20]) for m in state.messages]
        assert assistant_texts(state)[-1] == DELIVER_PROSE, assistant_texts(state)[-1]
        assert not any("class FakeStrategy" in m.text or "STRATEGY_KEY" in m.text
                       for m in state.messages), "源码/交付标记漏进了对话区"
        print("[OK] 先 ask 两轮再交付：计数正确、session 正确续接、源码不进对话区")
    finally:
        cleanup()


def test_ask_limit_triggers_fallback():
    try:
        provider, state = state_for(["问题1", "问题2", "问题3"])
        for i, reply in enumerate(["答1", "答2", "答3"], 1):
            state = orch.advance(provider, state, reply, smoke_options=SMOKE)
            expect = orch.STUCK if i >= orch.MAX_ASKS else orch.ASKING
            assert state.status == expect, f"第 {i} 轮：{state.status}"

        assert state.asks == orch.MAX_ASKS and state.asks_left == 0
        assert state.can_submit, "兜底后仍应能提交（重新开始 / 你决定就好）"
        print(f"[OK] 澄清满 {orch.MAX_ASKS} 轮触发兜底 STUCK，不无限循环")
    finally:
        cleanup()


def test_decide_for_me():
    try:
        provider, state = state_for(["你想要什么样的策略？", deliver(KEY)])
        state = orch.advance(provider, state, "跌了就买", smoke_options=SMOKE)
        assert state.status == orch.ASKING

        state = orch.advance(provider, state, orch.DECIDE_FOR_ME, smoke_options=SMOKE)
        assert state.status == orch.READY
        assert "不要再问" in provider.prompts[-1], provider.prompts[-1]
        assert "description" in provider.prompts[-1], "逃生口没要求把假设写进说明"
        print("[OK]「你决定就好」一键定稿，且要求把假设写进 description")
    finally:
        cleanup()


# ---------- 3. 闸门回灌 ----------
def test_guard_failure_then_fixed():
    try:
        provider, state = state_for([deliver(KEY, BAD_GUARD), deliver(KEY)])
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)

        assert state.status == orch.READY, f"{state.status} / {state.error}"
        assert len(state.fixes) == 1 and state.fixes[0].gate == "guard"
        assert "禁止 import os" in state.fixes[0].detail

        # 回灌全程不进对话区：只有第一次交付的那段说明
        assert assistant_texts(state) == [DELIVER_PROSE], assistant_texts(state)
        assert not any("Traceback" in t or "禁止 import os" in t
                       for t in assistant_texts(state)), "堆栈/错误细节漏进了对话区"
        # 回灌喂给模型的内容包含完整错误
        assert "禁止 import os" in provider.prompts[1], provider.prompts[1]
        assert state.asks == 0, "回灌不该占澄清额度"
        print("[OK] 闸门2 失败 → 自动回灌 → 第二次通过；错误只喂模型不进对话区")
    finally:
        cleanup()


def test_smoke_result_recorded_on_state():
    """校验面板要展示冒烟摘要（#30 八.6），所以成败都得记到 state 上"""
    try:
        provider, state = state_for([deliver(KEY)])
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)
        assert state.status == orch.READY
        assert state.smoke_result is not None and state.smoke_result.ok, "通过时未记录冒烟结果"
        assert state.smoke_result.summary(), "摘要为空，面板没东西可展示"
        assert len(state.smoke_result.cases) >= 1
    finally:
        cleanup()

    try:
        provider, state = state_for([deliver(KEY, BAD_SMOKE)] * (orch.MAX_FIX + 1))
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)
        assert state.status == orch.FAILED
        assert state.smoke_result is not None and not state.smoke_result.ok, "失败时也要记录"
        print("[OK] 冒烟结果（成功与失败）都记到 state 上，面板可读")
    finally:
        cleanup()


def test_guard_failure_leaves_no_stale_smoke_result():
    """闸门2 就失败时不该留着上一次的冒烟结果，否则面板显示的是过期数据"""
    try:
        provider, state = state_for([deliver(KEY, BAD_GUARD)] * (orch.MAX_FIX + 1))
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)
        assert state.status == orch.FAILED
        assert state.smoke_result is None, "闸门2 失败却留下了冒烟结果"
        print("[OK] 闸门2 失败时冒烟结果被清空，不残留过期数据")
    finally:
        cleanup()


def test_smoke_failure_then_fixed():
    try:
        provider, state = state_for([deliver(KEY, BAD_SMOKE), deliver(KEY)])
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)

        assert state.status == orch.READY, f"{state.status} / {state.error}"
        assert len(state.fixes) == 1 and state.fixes[0].gate == "smoke"
        assert "ZeroDivisionError" in state.fixes[0].reason, state.fixes[0].reason
        print("[OK] 闸门3 失败 → 自动回灌 → 第二次通过")
    finally:
        cleanup()


def test_all_attempts_fail_then_rejected():
    try:
        provider, state = state_for([deliver(KEY, BAD_GUARD)] * (orch.MAX_FIX + 1))
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)

        assert state.status == orch.FAILED, state.status
        assert len(state.fixes) == orch.MAX_FIX + 1, state.fixes
        assert not store.staging_path(state.run_id, KEY).exists(), "staging 未清理"
        assert (store.REJECTED_DIR / f"{state.run_id}__{KEY}.py").exists(), "未归档 _rejected"
        assert KEY not in [s["key"] for s in list_trade_strategies()]
        print(f"[OK] {orch.MAX_FIX + 1} 次全失败 → 归档 _rejected，不污染 custom/")
    finally:
        cleanup()


def test_fix_turns_into_ask():
    """回灌中途模型开始反问 → 交回用户，不继续自动重试"""
    try:
        provider, state = state_for([deliver(KEY, BAD_GUARD),
                                     "我需要确认一下：你希望用 numpy 还是纯 python 实现？"])
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)

        assert state.status == orch.ASKING, state.status
        assert state.asks == 1, "转为反问后应计入澄清额度"
        assert len(state.fixes) == 1
        assert "numpy" in assistant_texts(state)[-1], "反问内容应进对话区"
        assert not store.staging_path(state.run_id, KEY).exists()
        print("[OK] 回灌中途转反问 → 交回用户，计入澄清额度而非继续重试")
    finally:
        cleanup()


def test_rename_during_fix_drops_old_staging():
    """模型回灌时改了 key：旧 staging 直接丢掉，不进 _rejected"""
    try:
        other = "t_orch2"
        provider, state = state_for([deliver(KEY, BAD_GUARD), deliver(other)])
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)

        assert state.status == orch.READY and state.key == other, state.key
        assert not store.staging_path(state.run_id, KEY).exists(), "旧 key 的 staging 未清"
        assert not (store.REJECTED_DIR / f"{state.run_id}__{KEY}.py").exists(), \
            "改名重交不该往 _rejected 里堆垃圾"
        assert store.staging_path(state.run_id, other).exists()
        print("[OK] 回灌中改名：旧 staging 丢弃且不进 _rejected")
    finally:
        cleanup("t_orch2")


# ---------- 4. provider 出错 ----------
def test_provider_error():
    provider, state = state_for([], fail_on=1)
    state = orch.advance(provider, state, "描述", smoke_options=SMOKE)
    assert state.status == orch.ERROR
    assert "限流" in state.error, state.error
    assert state.can_submit is False or state.status == orch.ERROR
    print(f"[OK] provider 出错转 ERROR 且带可读原因：{state.error}")


def test_provider_error_during_feedback():
    try:
        provider, state = state_for([deliver(KEY, BAD_GUARD)], fail_on=2)
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)
        assert state.status == orch.ERROR, state.status
        assert not store.staging_path(state.run_id, KEY).exists(), "出错后 staging 未清"
        print("[OK] 回灌时 provider 出错：转 ERROR 并归档，不留 staging 残留")
    finally:
        cleanup()


# ---------- 5. 改名 ----------
def test_rename_after_ready():
    try:
        provider, state = state_for([deliver(KEY)])
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)
        state = orch.rename(state, "t_renamed")

        assert state.status == orch.READY and state.key == "t_renamed"
        assert 'key = "t_renamed"' in state.code
        assert store.staging_path(state.run_id, "t_renamed").exists()
        assert not store.staging_path(state.run_id, KEY).exists()

        orch.promote(state)
        assert "t_renamed" in [s["key"] for s in list_trade_strategies()]
        print("[OK] READY 后改名：重跑闸门2、换 staging、可正常启用")
    finally:
        cleanup("t_renamed")


def test_rename_rejects_taken_key():
    try:
        provider, state = state_for([deliver(KEY)])
        state = orch.advance(provider, state, "描述", smoke_options=SMOKE)
        try:
            orch.rename(state, "default")
            raise AssertionError("改成内置 key 竟然成功了")
        except store.StoreError as e:
            assert "已被占用" in str(e), e
        assert state.key == KEY, "失败的改名不该改动状态"
        print("[OK] 改名撞已占用 key 被拒，状态不变")
    finally:
        cleanup()


# ---------- 6. 重新开始 ----------
def test_restart():
    provider, state = state_for(["问题"])
    state = orch.advance(provider, state, "描述", smoke_options=SMOKE)
    fresh = orch.restart(state)

    assert fresh.run_id != state.run_id
    assert fresh.status == orch.NEW and fresh.asks == 0
    assert fresh.messages == [] and fresh.session == {}
    assert fresh.provider_key == state.provider_key, "provider 选择应保留"
    print("[OK] 重新开始：换 run_id、清空对话与 session、保留 provider")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
