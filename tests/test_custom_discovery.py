"""strategy/custom/ 自动发现测试（确定性，无需联网、不调用 LLM）

覆盖：
1. 内置策略无回归
2. 合法自定义策略被发现，且能经 get_trade_strategy 构造并跑 on_bar
3. 各类坏文件只跳过不炸站（语法错、无子类、多子类、key 与文件名不符、撞内置 key）
4. _staging / _rejected 不被扫描
5. refresh_registry() 让新落盘的策略立即可选，删除后立即消失
"""

import sys, os, shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.broker import Broker
from strategy.trade_strategy import (
    BarContext, _CUSTOM_DIR, _BUILTIN,
    list_trade_strategies, get_trade_strategy, refresh_registry,
)

SYM = "TEST"
CAP = 100_000

# 一个合法的最小策略：收到买入信号就全仓，收到卖出信号就清仓
GOOD = '''
from strategy.trade_strategy import TradeStrategy


class {cls}(TradeStrategy):
    key = "{key}"
    name = "测试用全仓策略"
    description = "买入信号全仓、卖出信号清仓，仅供测试。"
    example = "无"
    custom = True
    PARAMS = [{{"key": "ratio", "label": "仓位比例", "default": 1.0,
               "min": 0.1, "max": 1.0, "step": 0.1, "is_pct": True}}]

    def __init__(self, ratio: float = 1.0):
        self.ratio = float(ratio)

    def on_bar(self, broker, ctx):
        if ctx.decision == 1:
            shares = int(broker.cash * self.ratio * 0.995 / ctx.price)
            if shares > 0:
                broker.buy(ctx.symbol, ctx.price, shares, ctx.date, ctx.board)
        elif ctx.decision == -1:
            pos = broker.positions.get(ctx.symbol)
            if pos and broker.can_sell(ctx.symbol, ctx.date, ctx.board):
                broker.sell(ctx.symbol, ctx.price, pos.shares, ctx.date, ctx.board)
'''


def write(name: str, content: str) -> str:
    path = _CUSTOM_DIR / f"{name}.py"
    path.write_text(content, encoding="utf-8")
    return str(path)


def cleanup(*names):
    for n in names:
        (_CUSTOM_DIR / f"{n}.py").unlink(missing_ok=True)
        sys.modules.pop(f"strategy.custom.{n}", None)
    shutil.rmtree(_CUSTOM_DIR / "__pycache__", ignore_errors=True)
    refresh_registry()


def keys() -> list[str]:
    return [s["key"] for s in list_trade_strategies()]


# ---------- 1. 内置无回归 ----------
def test_builtin_intact():
    refresh_registry()
    for k in _BUILTIN:
        assert k in keys(), f"内置策略 {k} 丢失"
    assert get_trade_strategy("playdead_quarter", buy_threshold=0.1) is not None
    print(f"[OK] 内置策略无回归：{sorted(_BUILTIN)}")


# ---------- 2. 合法策略被发现且可用 ----------
def test_good_strategy_discovered():
    try:
        write("t_good", GOOD.format(cls="AllInStrategy", key="t_good"))
        refresh_registry()
        assert "t_good" in keys(), keys()

        meta = next(s for s in list_trade_strategies(custom_only=True) if s["key"] == "t_good")
        assert meta["name"] == "测试用全仓策略"
        assert [p["key"] for p in meta["params"]] == ["ratio"]

        # 参数透传 + 真跑一根 K 线
        strat = get_trade_strategy("t_good", ratio=0.5)
        assert strat.ratio == 0.5
        broker = Broker()
        cash0 = broker.cash
        strat.on_bar(broker, BarContext(symbol=SYM, date="2024-01-02", price=10.0,
                                        prev_close=10.0, decision=1, strength=1.0,
                                        board="index", initial_capital=CAP))
        assert broker.cash < cash0, "买入未成交"
        print(f"[OK] 合法策略被发现并可构造运行（ratio=0.5，现金 {cash0:.0f} → {broker.cash:.0f}）")
    finally:
        cleanup("t_good")


# ---------- 3. 坏文件只跳过不炸 ----------
def test_bad_files_skipped():
    bad = {
        "t_syntax":   "class Broken(:\n    pass\n",                       # 语法错
        "t_nocls":    "X = 1\n",                                          # 无子类
        "t_twocls":   (GOOD.format(cls="A", key="t_twocls")
                       + GOOD.format(cls="B", key="t_twocls")),           # 两个子类
        "t_mismatch": GOOD.format(cls="M", key="wrong_key"),              # key≠文件名
        # 撞内置 key：文件名必须就叫 default.py 才走得到「与内置冲突」分支，
        # 叫 t_dup.py 只会先被「key≠文件名」拦下，测不到这条路径
        "default":    GOOD.format(cls="D", key="default"),
    }
    names = list(bad)
    try:
        for n, c in bad.items():
            write(n, c)
        # 同时放一个好的，验证坏文件不影响其余发现
        write("t_ok", GOOD.format(cls="OkStrategy", key="t_ok"))
        refresh_registry()

        ks = keys()
        for n in names:
            if n == "default":
                continue  # 内置同名，本就该在（下面单独断言未被覆盖）
            assert n not in ks, f"坏文件 {n} 不该被注册"
        assert "wrong_key" not in ks
        assert "t_ok" in ks, "坏文件拖累了正常策略的发现"
        for k in _BUILTIN:
            assert k in ks, f"坏文件导致内置策略 {k} 丢失"
        # 内置 default 仍是内置类，未被同 key 的自定义覆盖
        assert get_trade_strategy("default").__class__ is _BUILTIN["default"]
        print(f"[OK] {len(names)} 类坏文件全部跳过，正常策略与内置策略不受影响")
    finally:
        cleanup(*names, "t_ok")


# ---------- 4. _staging / _rejected 不被扫描 ----------
def test_staging_and_rejected_ignored():
    staging = _CUSTOM_DIR / "_staging" / "t_stage.py"
    rejected = _CUSTOM_DIR / "_rejected" / "t_rej.py"
    try:
        staging.parent.mkdir(exist_ok=True)
        rejected.parent.mkdir(exist_ok=True)
        staging.write_text(GOOD.format(cls="S", key="t_stage"), encoding="utf-8")
        rejected.write_text(GOOD.format(cls="R", key="t_rej"), encoding="utf-8")
        refresh_registry()
        ks = keys()
        assert "t_stage" not in ks and "t_rej" not in ks, ks
        print("[OK] _staging / _rejected 未被扫描")
    finally:
        staging.unlink(missing_ok=True)
        rejected.unlink(missing_ok=True)
        refresh_registry()


# ---------- 5. refresh 生效 ----------
def test_refresh_picks_up_changes():
    try:
        assert "t_new" not in keys()
        write("t_new", GOOD.format(cls="NewStrategy", key="t_new"))
        assert "t_new" not in keys(), "未 refresh 前不该出现（缓存生效）"
        refresh_registry()
        assert "t_new" in keys(), "refresh 后应立即出现"

        # 改内容后 refresh 应取到新版本（验证 reload 而非 sys.modules 缓存）
        write("t_new", GOOD.format(cls="NewStrategy", key="t_new").replace(
            "测试用全仓策略", "改名后的策略"))
        refresh_registry()
        meta = next(s for s in list_trade_strategies() if s["key"] == "t_new")
        assert meta["name"] == "改名后的策略", meta["name"]

        (_CUSTOM_DIR / "t_new.py").unlink()
        refresh_registry()
        assert "t_new" not in keys(), "删除后应立即消失"
        print("[OK] refresh_registry 对新增/修改/删除均即时生效")
    finally:
        cleanup("t_new")


if __name__ == "__main__":
    test_builtin_intact()
    test_good_strategy_discovered()
    test_bad_files_skipped()
    test_staging_and_rejected_ignored()
    test_refresh_picks_up_changes()
    print("\n全部测试通过!")
