"""闸门 3：子进程冒烟回测（#30）

把候选策略放进真实链路跑一遍——`build_signals` → `fuse_signals` → `run_backtest`，
只断言「跑得起来」而非「赚不赚钱」：不抛异常、净值曲线非空、final_value 有限。

为什么必须开子进程：AST 静态校验挡不住死循环和死锁。`while True: pass` 完全合法，
一旦在 Streamlit 进程里 import + 执行就是整站卡死。子进程 + 硬超时是唯一兜底。

用 spawn 而非默认的 fork：Streamlit 每次 rerun 在独立线程执行，从多线程进程 fork
可能带走一把被别的线程持有的锁，子进程一上来就死锁——那正是本闸门要拦的症状，
却发生在闸门自己身上。spawn 慢一两秒，但没有这个雷。
"""

import importlib.util
import logging
import math
import multiprocessing
import queue
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
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_child_main, args=(str(path), key, synthetic, q), daemon=True)

    started = time.monotonic()
    payload = None
    proc.start()
    try:
        payload = _await_payload(proc, q, timeout)
    finally:
        # 超时说明子进程还在死循环，没必要再等它自己退出，直接杀
        _terminate(proc, reap_wait=0 if payload is None else 3)

    elapsed = time.monotonic() - started
    if payload is None:
        return SmokeResult(
            ok=False,
            error=f"冒烟回测超时（>{timeout:.0f}s）：策略可能存在死循环或死锁",
            detail=f"子进程在 {elapsed:.1f}s 后被强制终止，未返回任何结果。",
        )
    if payload.get("crashed"):
        return SmokeResult(ok=False, error=payload["error"], detail=payload.get("detail", ""))

    logger.info("冒烟回测通过 %s（%.1fs）", key, elapsed)
    return SmokeResult(ok=True, cases=payload["cases"])


def _await_payload(proc, q, timeout: float) -> dict | None:
    """等子进程交结果；超时或进程猝死（没来得及入队）都返回 None"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return q.get(timeout=0.1)
        except queue.Empty:
            pass
        if not proc.is_alive():
            # 进程已退出：可能结果刚入队还没到管道这头，再给一次机会
            try:
                return q.get(timeout=0.5)
            except queue.Empty:
                return {"crashed": True,
                        "error": f"子进程异常退出（exitcode={proc.exitcode}），未返回结果",
                        "detail": "通常是段错误或被 OOM killer 杀掉。"}
        if time.monotonic() >= deadline:
            return None


def _terminate(proc, reap_wait: float = 3) -> None:
    if reap_wait:
        proc.join(reap_wait)
    if proc.is_alive():
        proc.terminate()
        proc.join(3)
    if proc.is_alive():
        proc.kill()
        proc.join(3)


# ========== 子进程 ==========

def _child_main(path: str, key: str, synthetic: bool, q) -> None:
    """spawn 的入口，必须是模块级函数（要能被 pickle）"""
    try:
        q.put({"cases": _run_cases(path, key, synthetic)})
    except Exception as e:
        q.put({
            "crashed": True,
            "error": f"{type(e).__name__}: {e}",
            "detail": traceback.format_exc(),
        })


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
