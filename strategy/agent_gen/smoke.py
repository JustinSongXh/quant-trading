"""闸门 3：子进程冒烟回测（#30）

把候选策略放进真实链路跑一遍——`build_signals` → `fuse_signals` → `run_backtest`，
只断言「跑得起来」而非「赚不赚钱」：不抛异常、净值曲线非空、final_value 有限。

为什么必须开子进程：AST 静态校验挡不住死循环和死锁。`while True: pass` 完全合法，
一旦在 Streamlit 进程里 import + 执行就是整站卡死。子进程 + 硬超时是唯一兜底。

为什么用 subprocess 而不是 multiprocessing：

- fork 不行——Streamlit 每次 rerun 在独立线程执行，从多线程进程 fork 可能带走一把
  被别的线程持有的锁，子进程一上来就死锁，正是本闸门要拦的症状发生在闸门自己身上。
- spawn 也不行——它为了拿到目标函数会 **re-import 父进程的 `__main__`**。调用方的
  顶层代码于是在子进程里重跑一遍：实测一个顶层没加 `if __name__ == "__main__"` 的
  脚本，子进程把整段脚本连模型调用一起重跑了 26s，还没开始回测就撞上超时。
  闸门的行为不该取决于调用方长什么样。
- subprocess 跑本模块的 `__main__`（见文件末尾）：与调用方完全无关，没有 pickle
  约束，超时用 `subprocess.run(timeout=)` 一行搞定。
"""

import importlib.util
import json
import logging
import math
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
LOOKBACK_DAYS = 365

# 一只指数 + 一只主板个股：两者 board 不同，broker 的涨跌停/T+1 规则分支都能过一遍
SMOKE_CASES = [("000300", "index"), ("600000", "stock")]

# 合成行情的固定截止日：保证同一策略每次跑拿到完全一致的数据。
# 没有它，回测结果随「今天是哪天」漂移，闸门就成了掷骰子。
SYNTHETIC_END = "2024-12-31"

REPO_ROOT = Path(__file__).resolve().parents[2]

# 子进程用 `python -m strategy.agent_gen.smoke` 起，结果这一行前缀之后跟 JSON。
# 加前缀是因为被测策略可能往 stdout 打东西（顶层 print 被闸门2 拦了，但方法里的
# print 是合法的），不能假定 stdout 只有我们那一行。
RESULT_PREFIX = "__SMOKE_RESULT__ "


@dataclass
class SmokeResult:
    ok: bool
    error: str = ""                       # 一句话原因，进前端折叠状态条的标题
    detail: str = ""                      # 完整堆栈，展开才看，不进对话区
    cases: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ok:
            return self.error
        return " / ".join(
            f"{c['symbol']}: {c['trades']} 笔，净值 {c['final_value']:,.0f}"
            f"（{c['total_return']:+.1%}）"
            for c in self.cases
        )


def run(path, key: str, *, synthetic: bool = False,
        timeout: float = TIMEOUT_SECONDS) -> SmokeResult:
    """在子进程里跑冒烟回测。

    Args:
        path: 待测策略文件（通常在 _staging/ 下，按路径加载，与文件名无关）
        key: 策略 key，仅用于日志与模块命名
        synthetic: 用合成行情而非真实行情。单测走这条：确定性、离线、快。
                   实际生成流程走真实行情，才能暴露真实数据里的 NaN / 停牌等情况。
        timeout: 硬超时秒数，超时即判失败并杀掉子进程
    """
    cmd = [sys.executable, "-m", "strategy.agent_gen.smoke", str(path), key]
    if synthetic:
        cmd.append("--synthetic")

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        return SmokeResult(
            ok=False,
            error=f"冒烟回测超时（>{timeout:.0f}s）：策略可能存在死循环或死锁",
            detail=f"子进程在 {elapsed:.1f}s 后被强制终止，未返回任何结果。",
        )

    elapsed = time.monotonic() - started
    payload = _parse_result(proc.stdout)
    if payload is None:
        return SmokeResult(
            ok=False,
            error=f"子进程未返回结果（exitcode={proc.returncode}）",
            detail=(proc.stderr or proc.stdout or "无输出")[-4000:],
        )
    if not payload.get("ok"):
        return SmokeResult(ok=False, error=payload.get("error", "未知错误"),
                           detail=payload.get("detail", ""))

    logger.info("冒烟回测通过 %s（%.1fs）", key, elapsed)
    return SmokeResult(ok=True, cases=payload["cases"])


def _parse_result(stdout: str) -> dict | None:
    """从 stdout 里挑出结果行。被测策略自己打的东西一律忽略。"""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                return json.loads(line[len(RESULT_PREFIX):])
            except json.JSONDecodeError:
                return None
    return None


# ========== 子进程 ==========

def _child_main(argv: list[str]) -> int:
    """子进程入口：`python -m strategy.agent_gen.smoke <path> <key> [--synthetic]`

    结果打到 stdout 的 RESULT_PREFIX 行。始终返回 0——失败信息在 JSON 里，
    用 exitcode 表达失败会和「解释器自己崩了」混在一起分不清。
    """
    synthetic = "--synthetic" in argv
    positional = [a for a in argv if not a.startswith("--")]
    if len(positional) != 2:
        payload = {"ok": False, "error": "用法：smoke.py <path> <key> [--synthetic]"}
    else:
        path, key = positional
        try:
            payload = {"ok": True, "cases": _run_cases(path, key, synthetic)}
        except Exception as e:
            payload = {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "detail": traceback.format_exc(),
            }
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


def _load_strategy_class(path: str, key: str):
    """按文件路径加载模块并取出唯一的 TradeStrategy 子类。

    按路径而非模块名加载：staging 文件名是 `<run_id>__<key>.py`，且 _staging/
    没有 __init__.py，本来就不是可 import 的包路径。
    """
    from strategy.trade_strategy import TradeStrategy

    name = f"_smoke_{key}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法从 {path} 加载模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    classes = [obj for obj in vars(module).values()
               if isinstance(obj, type) and issubclass(obj, TradeStrategy)
               and obj is not TradeStrategy and obj.__module__ == name]
    if len(classes) != 1:
        raise ValueError(f"应恰好包含 1 个 TradeStrategy 子类，实际 {len(classes)} 个")
    return classes[0]


def _load_kline(symbol: str, stock_type: str, synthetic: bool):
    if synthetic:
        from data.mock import generate_mock_kline
        return generate_mock_kline(symbol, days=LOOKBACK_DAYS, end=SYNTHETIC_END)
    from data.fetcher import fetch_daily_kline
    return fetch_daily_kline(symbol, days=LOOKBACK_DAYS, stock_type=stock_type)


def _run_cases(path: str, key: str, synthetic: bool) -> list[dict]:
    from backtest.engine import run_backtest
    from strategy.fusion import fuse_signals
    from strategy.signals import build_signals

    cls = _load_strategy_class(path, key)
    strategy = cls()  # 全部参数取默认值

    cases = []
    for symbol, stock_type in SMOKE_CASES:
        kline = _load_kline(symbol, stock_type, synthetic)
        if kline is None or kline.empty:
            raise RuntimeError(f"{symbol} 行情为空，无法冒烟（数据源不可用）")

        signal_df = build_signals(kline, symbol=symbol, enabled_signals=["technical"])
        fusion = fuse_signals(signal_df, source="technical")

        # 同一个实例连跑两只：顺带验证 reset() 真的清干净了，
        # 否则第一只的持仓状态会串到第二只（引擎每轮只调 reset）
        result = run_backtest(symbol, signal_df, fusion,
                              stock_type=stock_type, trade_strategy=strategy)

        net_values = result["net_values"]
        if net_values is None or net_values.empty:
            raise RuntimeError(f"{symbol} 净值曲线为空：策略在整段行情里没产生任何净值点")

        final_value = float(result["final_value"])
        if not math.isfinite(final_value):
            raise RuntimeError(f"{symbol} 最终净值不是有限数：{final_value}")

        initial = float(result["initial_capital"])
        cases.append({
            "symbol": symbol,
            "bars": int(len(net_values)),
            "trades": len(result["trade_log"]),
            "final_value": final_value,
            "initial_capital": initial,
            "total_return": final_value / initial - 1 if initial else 0.0,
        })
    return cases


def run_path(path, **kw) -> SmokeResult:
    """便捷入口：从文件名推 key（仅用于命令行手工验证）"""
    stem = Path(path).stem
    return run(path, stem.split("__", 1)[-1], **kw)


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1:]))
