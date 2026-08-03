"""strategy/agent_gen/smoke.py 闸门 3 测试（合成行情，离线、确定性）

全部用 synthetic=True：合成行情由 data.mock.generate_mock_kline 按固定 seed +
固定截止日生成，同一策略每次跑拿到完全一致的数据。真实行情只在实际生成流程里用
（要暴露停牌/NaN 等真实情况），单测里跑真实行情既慢又会随数据源抖动。

覆盖：
1. 正常策略通过，返回可读摘要
2. 死循环被硬超时拦下（AST 拦不住，这是本闸门存在的唯一理由）
3. 抛异常 / 空净值 / 导入即失败 各类失败路径有可读原因
4. 结果确定性：同一策略跑两次结果完全一致
5. 子进程隔离：被测策略污染不了父进程
"""

import os
import shutil
import subprocess
import sys
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.agent_gen import smoke

TMP = os.path.join(os.path.dirname(__file__), "_smoke_tmp")

HEADER = "from strategy.trade_strategy import TradeStrategy\n\n\n"

GOOD = HEADER + '''class SmokeStrategy(TradeStrategy):
    key = "t_smoke"
    name = "冒烟测试策略"
    description = "买入信号半仓，卖出信号清仓"
    example = ""
    custom = True

    def reset(self):
        self.n = 0

    def on_bar(self, broker, ctx):
        if ctx.decision == 1:
            shares = int(broker.cash * 0.5 / ctx.price)
            if shares > 0:
                broker.buy(ctx.symbol, ctx.price, shares, ctx.date, ctx.board)
                self.n += 1
        elif ctx.decision == -1:
            pos = broker.positions.get(ctx.symbol)
            if pos and broker.can_sell(ctx.symbol, ctx.date, ctx.board):
                broker.sell(ctx.symbol, ctx.price, pos.shares, ctx.date, ctx.board)
'''

DEADLOOP = HEADER + '''class DeadLoopStrategy(TradeStrategy):
    key = "t_loop"
    name = "死循环策略"
    description = "第一根 K 线就转不出来"
    example = ""
    custom = True

    def on_bar(self, broker, ctx):
        while True:
            pass
'''

RAISES = HEADER + '''class BoomStrategy(TradeStrategy):
    key = "t_boom"
    name = "抛异常策略"
    description = "on_bar 里除零"
    example = ""
    custom = True

    def on_bar(self, broker, ctx):
        1 / 0
'''

IMPORT_BOOM = HEADER + '''raise RuntimeError("import 阶段就炸")


class NeverStrategy(TradeStrategy):
    key = "t_imp"
    name = "x"
    description = "x"
    example = ""
    custom = True

    def on_bar(self, broker, ctx):
        pass
'''

NO_SUBCLASS = "def helper():\n    return 1\n"

POLLUTE = HEADER + '''class PolluteStrategy(TradeStrategy):
    key = "t_pol"
    name = "污染策略"
    description = "往父进程环境写东西（应该只影响子进程）"
    example = ""
    custom = True

    def on_bar(self, broker, ctx):
        import os
        os.environ["SMOKE_POLLUTED"] = "1"
'''


def write(name: str, code: str) -> str:
    os.makedirs(TMP, exist_ok=True)
    path = os.path.join(TMP, f"aaaa1111__{name}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    return path


def cleanup():
    # 子进程 exec_module 会在这里落 __pycache__，整目录删
    shutil.rmtree(TMP, ignore_errors=True)


# ---------- 1. 正常通过 ----------
def test_good_strategy_passes():
    try:
        result = smoke.run(write("t_smoke", GOOD), "t_smoke", synthetic=True)
        assert result.ok, f"应通过：{result.error}\n{result.detail}"
        assert len(result.cases) == len(smoke.SMOKE_CASES), result.cases

        for c in result.cases:
            assert c["bars"] > 200, f"净值点太少：{c}"
            assert c["final_value"] > 0 and c["initial_capital"] > 0, c
        symbols = [c["symbol"] for c in result.cases]
        assert symbols == [s for s, _ in smoke.SMOKE_CASES], symbols
        assert result.summary()
        print(f"[OK] 正常策略通过闸门 3：{result.summary()}")
    finally:
        cleanup()


def test_summary_is_readable():
    """摘要要能直接摆到前端校验面板上"""
    try:
        result = smoke.run(write("t_smoke", GOOD), "t_smoke", synthetic=True)
        s = result.summary()
        for symbol, _ in smoke.SMOKE_CASES:
            assert symbol in s, s
        assert "笔" in s and "净值" in s, s
        print(f"[OK] 摘要可读：{s}")
    finally:
        cleanup()


# ---------- 2. 死循环被超时拦下 ----------
def test_dead_loop_times_out():
    try:
        started = time.monotonic()
        result = smoke.run(write("t_loop", DEADLOOP), "t_loop", synthetic=True, timeout=5)
        elapsed = time.monotonic() - started

        assert not result.ok, "死循环竟然通过了闸门"
        assert "超时" in result.error, result.error
        assert elapsed < 20, f"超时后没及时收敛，耗时 {elapsed:.1f}s"
        print(f"[OK] 死循环被 5s 硬超时拦下（实际 {elapsed:.1f}s 返回）：{result.error}")
    finally:
        cleanup()


# ---------- 3. 各类失败路径 ----------
def test_runtime_exception_reported():
    try:
        result = smoke.run(write("t_boom", RAISES), "t_boom", synthetic=True)
        assert not result.ok
        assert "ZeroDivisionError" in result.error, result.error
        assert "on_bar" in result.detail, result.detail  # 堆栈定位到出错的方法
        print(f"[OK] 运行时异常有可读原因：{result.error}")
    finally:
        cleanup()


def test_import_time_failure_reported():
    try:
        result = smoke.run(write("t_imp", IMPORT_BOOM), "t_imp", synthetic=True)
        assert not result.ok
        assert "import 阶段就炸" in result.error, result.error
        print(f"[OK] import 阶段失败有可读原因：{result.error}")
    finally:
        cleanup()


def test_no_subclass_reported():
    try:
        result = smoke.run(write("t_none", NO_SUBCLASS), "t_none", synthetic=True)
        assert not result.ok
        assert "1 个 TradeStrategy 子类" in result.error, result.error
        print(f"[OK] 文件里没有策略类时有可读原因：{result.error}")
    finally:
        cleanup()


def test_missing_file_reported():
    result = smoke.run(os.path.join(TMP, "nope.py"), "t_nope", synthetic=True)
    assert not result.ok and result.error
    print(f"[OK] 文件不存在时不崩溃，返回失败：{result.error}")


# ---------- 4. 确定性 ----------
def test_deterministic():
    """合成行情固定 seed + 固定截止日：两次跑必须完全一致，闸门不能是掷骰子"""
    try:
        path = write("t_smoke", GOOD)
        a = smoke.run(path, "t_smoke", synthetic=True)
        b = smoke.run(path, "t_smoke", synthetic=True)
        assert a.ok and b.ok
        assert a.cases == b.cases, f"两次结果不一致：\n{a.cases}\n{b.cases}"
        print("[OK] 同一策略连跑两次结果逐字段一致")
    finally:
        cleanup()


# ---------- 5. 子进程隔离 ----------
def test_child_process_isolation():
    try:
        os.environ.pop("SMOKE_POLLUTED", None)
        result = smoke.run(write("t_pol", POLLUTE), "t_pol", synthetic=True)
        assert result.ok, f"{result.error}\n{result.detail}"
        assert "SMOKE_POLLUTED" not in os.environ, "被测策略污染了父进程环境"
        assert not any(m.startswith("_smoke_") for m in sys.modules), \
            "被测模块被加载进了父进程"
        print("[OK] 被测策略只在子进程里生效，父进程环境与 sys.modules 干净")
    finally:
        cleanup()


# ---------- 6. 不依赖调用方的 __main__ ----------
def test_independent_of_caller_main():
    """闸门的行为不能取决于调用方长什么样。

    multiprocessing 的 spawn 会 re-import 父进程的 __main__，于是调用方的顶层代码
    在子进程里重跑一遍——实测一个顶层没加 __main__ 守卫的脚本，子进程把整段脚本
    连模型调用一起重跑了 26s，回测还没开始就撞上 30s 超时。改用 subprocess 跑
    smoke 自己的 __main__ 之后，调用方顶层写什么都与闸门无关。
    """
    try:
        strategy_path = write("t_smoke", GOOD)
        caller = os.path.join(TMP, "noisy_caller.py")
        with open(caller, "w", encoding="utf-8") as f:
            f.write(
                "import sys\n"
                f"sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})\n"
                "from strategy.agent_gen import smoke\n"
                # 顶层可执行语句，且刻意不加 __main__ 守卫：spawn 方案下会被子进程重跑
                "print('CALLER_TOPLEVEL_RAN')\n"
                f"r = smoke.run({strategy_path!r}, 't_smoke', synthetic=True)\n"
                "print('OK' if r.ok else 'FAIL', r.error)\n"
            )
        out = subprocess.run([sys.executable, caller], capture_output=True, text=True,
                             timeout=180).stdout
        assert "OK " in out or out.strip().endswith("OK"), out
        assert out.count("CALLER_TOPLEVEL_RAN") == 1, \
            f"调用方顶层代码被子进程重跑了 {out.count('CALLER_TOPLEVEL_RAN')} 次：\n{out}"
        print("[OK] 调用方顶层代码只执行一次——闸门不再 re-import 调用方的 __main__")
    finally:
        cleanup()


def test_strategy_stdout_does_not_break_parsing():
    """被测策略在方法里 print（合法）不能干扰结果解析"""
    try:
        noisy = GOOD.replace("        if ctx.decision == 1:",
                             "        print('strategy noise', ctx.date)\n"
                             "        if ctx.decision == 1:")
        result = smoke.run(write("t_noisy", noisy), "t_noisy", synthetic=True)
        assert result.ok, f"{result.error}\n{result.detail}"
        print("[OK] 策略自己往 stdout 打东西不影响结果解析")
    finally:
        cleanup()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
