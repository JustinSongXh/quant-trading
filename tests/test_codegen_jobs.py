"""jobs.py / runner.py 测试：进程隔离、取消、心跳回收（不调用 LLM）

这一层存在的唯一理由是**能把跑到一半的生成杀掉**：Streamlit 的停止是协作式的，
阻塞在 asyncio.run() 里的调用杀不掉，用户刷新页面后 token 会一直花到模型返回。
所以这里的测试重点不是「跑得通」，而是「杀得掉、杀得干净、自动杀」。
"""

import json
import os
import shutil
import sys
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.agent_gen import jobs, orchestrator, registry, smoke, store
from strategy.agent_gen.runner import OUTPUT_NAME
from strategy.trade_strategy import _CUSTOM_DIR, refresh_registry

KEY = "t_job"
FAST = registry._PROVIDER_DIR / "jobfast.py"
SLOW = registry._PROVIDER_DIR / "jobslow.py"

STRATEGY = f'''from strategy.trade_strategy import TradeStrategy


class JobStrategy(TradeStrategy):
    key = "{KEY}"
    name = "任务测试策略"
    description = "买入信号半仓、卖出信号清仓。"
    example = ""
    custom = True

    def on_bar(self, broker, ctx):
        if ctx.decision == 1:
            shares = int(broker.cash * 0.5 / ctx.price)
            if shares > 0:
                broker.buy(ctx.symbol, ctx.price, shares, ctx.date, ctx.board)
        elif ctx.decision == -1:
            pos = broker.positions.get(ctx.symbol)
            if pos and broker.can_sell(ctx.symbol, ctx.date, ctx.board):
                broker.sell(ctx.symbol, ctx.price, pos.shares, ctx.date, ctx.board)
'''

FAST_SRC = f'''
from strategy.agent_gen.base import CodegenProvider, Turn

CODE = """{STRATEGY}"""


class JobFastProvider(CodegenProvider):
    key = "jobfast"
    name = "快 provider"
    description = "立刻交付。"

    def start(self, user_text):
        return self.to_turn(["好的。", "STRATEGY_KEY: {KEY}",
                             "```python\\n" + CODE + "```"], {{"n": 1}})

    resume = lambda self, session, user_text: self.start(user_text)
'''

# 起一个「一直在想」的 provider，用来测取消。它还会写个文件报告自己被启动过，
# 顺便证明子进程真的开起来了。
SLOW_SRC = '''
import time
from pathlib import Path

from strategy.agent_gen.base import CodegenProvider, Turn


class JobSlowProvider(CodegenProvider):
    key = "jobslow"
    name = "慢 provider"
    description = "睡很久，用来测取消。"

    def start(self, user_text):
        Path("/tmp/jobslow_started").write_text(str(__import__("os").getpid()))
        time.sleep(600)
        return Turn(kind="ask", texts=["永远到不了这里"], session={})

    def resume(self, session, user_text):
        return self.start(user_text)
'''


def setup():
    FAST.write_text(FAST_SRC, encoding="utf-8")
    SLOW.write_text(SLOW_SRC, encoding="utf-8")
    registry.refresh_providers()


def cleanup():
    for owner in list(jobs._jobs):
        jobs.cancel(owner, "测试清理")
    for f in (FAST, SLOW):
        f.unlink(missing_ok=True)
    for m in ("jobfast", "jobslow"):
        sys.modules.pop(f"strategy.agent_gen.providers.{m}", None)
    shutil.rmtree(registry._PROVIDER_DIR / "__pycache__", ignore_errors=True)
    registry.refresh_providers()

    (_CUSTOM_DIR / f"{KEY}.py").unlink(missing_ok=True)
    sys.modules.pop(f"strategy.custom.{KEY}", None)
    for d in (store.STAGING_DIR, store.REJECTED_DIR):
        for p in d.glob(f"*__{KEY}.py"):
            p.unlink(missing_ok=True)
    for p in ("/tmp/jobslow_started",):
        try:
            os.unlink(p)
        except OSError:
            pass
    refresh_registry()


def state_for(provider_key: str) -> dict:
    return orchestrator.new_state(provider_key).to_dict()


def wait_for(pred, timeout=60, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def pid_alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


# ---------- 1. 状态序列化 ----------
def test_state_roundtrip():
    state = orchestrator.new_state("jobfast", effort_level=3)
    state.messages.append(orchestrator.Message("user", "描述"))
    state.messages.append(orchestrator.Message("assistant", "好的"))
    state.fixes.append(orchestrator.FixAttempt("smoke", "超时", "堆栈…"))
    state.asks = 2
    state.status = orchestrator.READY
    state.key, state.code = KEY, "code…"
    state.smoke_result = smoke.SmokeResult(ok=True, cases=[{"symbol": "000300", "trades": 3}])

    back = orchestrator.GenState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert back.to_dict() == state.to_dict(), "序列化不是等价的"
    assert back.smoke_result.ok and back.smoke_result.cases[0]["symbol"] == "000300"
    assert [m.text for m in back.messages] == ["描述", "好的"]
    assert back.fixes[0].gate == "smoke" and back.provider_params == {"effort_level": 3}
    print("[OK] GenState 过 JSON 往返等价（含 messages / fixes / smoke_result）")


# ---------- 2. 正常跑完 ----------
def test_job_completes_and_returns_state():
    try:
        setup()
        job = jobs.start("owner-a", state_for("jobfast"), "随便描述",
                         smoke_options={"synthetic": True})
        assert job.is_running() or (job.job_dir / OUTPUT_NAME).exists()

        assert wait_for(lambda: jobs.poll("owner-a") is not None, timeout=180), "任务没出结果"
        out = jobs.poll("owner-a")
        assert out["ok"], out
        state = orchestrator.GenState.from_dict(out["state"])
        assert state.status == orchestrator.READY, f"{state.status} / {state.error}"
        assert state.key == KEY and state.smoke_result and state.smoke_result.ok
        jobs.finish("owner-a")
        assert jobs.get("owner-a") is None
        assert not job.job_dir.exists(), "临时目录没清掉"
        print(f"[OK] 任务在独立进程跑完并回传状态：{state.smoke_result.summary()}")
    finally:
        cleanup()


# ---------- 3. 取消 ----------
def test_cancel_kills_process_group():
    try:
        setup()
        job = jobs.start("owner-b", state_for("jobslow"), "描述")
        pid = job.proc.pid
        assert wait_for(lambda: os.path.exists("/tmp/jobslow_started"), timeout=60), \
            "子进程没起来"
        assert jobs.poll("owner-b") is None, "还在跑却报了结果"

        assert jobs.cancel("owner-b") is True
        assert wait_for(lambda: not pid_alive(pid), timeout=15), "取消后进程还在"
        assert jobs.get("owner-b") is None
        print("[OK] 取消会杀掉整个进程组，token 立刻停花")
    finally:
        cleanup()


def test_cancel_when_nothing_running():
    assert jobs.cancel("owner-none") is False
    print("[OK] 没有任务时取消返回 False，不炸")


# ---------- 4. 心跳回收（刷新页面的等价场景）----------
def test_sweep_kills_unpolled_job():
    """刷新页面 = 换了 session id = 旧任务再也不会被轮询 → 必须被自动杀掉"""
    try:
        setup()
        job = jobs.start("owner-gone", state_for("jobslow"), "描述")
        pid = job.proc.pid
        assert wait_for(lambda: os.path.exists("/tmp/jobslow_started"), timeout=60)

        # 没到超时不该动它
        assert jobs.sweep() == 0, "心跳还新鲜就被回收了"
        assert pid_alive(pid)

        # 把心跳拨老，模拟「主人走了」
        job.last_seen = time.monotonic() - jobs.STALE_SECONDS - 1
        assert jobs.sweep() == 1, "心跳超时却没被回收"
        assert wait_for(lambda: not pid_alive(pid), timeout=15), "回收后进程还在"
        assert jobs.get("owner-gone") is None
        print(f"[OK] 无人轮询 >{jobs.STALE_SECONDS:.0f}s 的任务被自动杀掉（刷新页面的场景）")
    finally:
        cleanup()


def test_poll_refreshes_heartbeat():
    """只要还在轮询就不能被回收，否则正常使用中会被误杀"""
    try:
        setup()
        job = jobs.start("owner-c", state_for("jobslow"), "描述")
        job.last_seen = time.monotonic() - jobs.STALE_SECONDS - 1
        jobs.poll("owner-c")                      # 轮询应刷新心跳
        assert jobs.sweep() == 0, "轮询过却仍被回收——正常使用会被误杀"
        assert job.is_running()
        print("[OK] poll() 刷新心跳，正在使用的任务不会被误杀")
    finally:
        cleanup()


def test_reaper_thread_started():
    try:
        setup()
        jobs.start("owner-d", state_for("jobslow"), "描述")
        assert jobs._reaper is not None and jobs._reaper.is_alive(), \
            "后台回收线程没起——用户刷新后不操作页面，孤儿任务会一直烧"
        assert jobs._reaper.daemon, "回收线程必须是 daemon，否则进程退不出"
        print("[OK] 后台回收线程已启动（不依赖用户继续操作页面）")
    finally:
        cleanup()


# ---------- 5. 并发与互不影响 ----------
def test_concurrency_cap():
    try:
        setup()
        for i in range(jobs.MAX_CONCURRENT):
            jobs.start(f"owner-cap-{i}", state_for("jobslow"), "描述")
        try:
            jobs.start("owner-cap-over", state_for("jobslow"), "描述")
            raise AssertionError("超出并发上限却起成功了")
        except jobs.JobError as e:
            assert str(jobs.MAX_CONCURRENT) in str(e), e
        print(f"[OK] 并发上限 {jobs.MAX_CONCURRENT} 生效（订阅额度是共享的）")
    finally:
        cleanup()


def test_second_start_replaces_own_job():
    """同一会话再次发起 → 先杀掉自己的旧任务，不占着并发额度"""
    try:
        setup()
        first = jobs.start("owner-e", state_for("jobslow"), "第一次")
        pid = first.proc.pid
        assert wait_for(lambda: os.path.exists("/tmp/jobslow_started"), timeout=60)
        old_dir = first.job_dir
        second = jobs.start("owner-e", state_for("jobslow"), "第二次")
        assert second is not first
        assert wait_for(lambda: not pid_alive(pid), timeout=15), "旧任务没被杀"
        assert jobs.active_count() == 1
        assert not old_dir.exists(), f"旧任务的临时目录没删，会在 /tmp 里堆积：{old_dir}"
        print("[OK] 同会话重新发起会先杀掉旧任务并清掉它的临时目录")
    finally:
        cleanup()


def test_cancel_one_owner_leaves_other_alone():
    try:
        setup()
        a = jobs.start("owner-f", state_for("jobslow"), "A")
        b = jobs.start("owner-g", state_for("jobslow"), "B")
        assert wait_for(lambda: os.path.exists("/tmp/jobslow_started"), timeout=60)
        jobs.cancel("owner-f")
        assert wait_for(lambda: not pid_alive(a.proc.pid), timeout=15)
        assert b.is_running(), "杀了一个会话的任务，影响到了另一个"
        print("[OK] 杀一个会话的任务不影响其他会话")
    finally:
        cleanup()


# ---------- 6. 失败路径 ----------
def test_runner_error_reported():
    """runner 里抛异常要变成可读的 payload，而不是让前端猜"""
    try:
        setup()
        jobs.start("owner-h", state_for("nonexistent_provider"), "描述")
        assert wait_for(lambda: jobs.poll("owner-h") is not None, timeout=60)
        out = jobs.poll("owner-h")
        assert not out["ok"] and "unknown codegen provider" in out["error"], out
        assert out["detail"], "缺堆栈"
        print(f"[OK] runner 内异常回传成可读 payload：{out['error']}")
    finally:
        cleanup()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
