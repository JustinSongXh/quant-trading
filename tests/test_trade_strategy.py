"""交易策略框架测试（确定性，无需联网）

1. 无回归：DefaultStrengthStrategy 与原引擎内联逻辑逐 bar 完全一致。
2. 装死四分之一：LIFO 买入价栈、25% 分档、加/减仓基准、满仓装死、清零不重建。
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.broker import Broker
from config.settings import POSITION_SIZING
from strategy.trade_strategy import (
    BarContext, DefaultStrengthStrategy, PlayDeadQuarterStrategy,
    list_trade_strategies, get_trade_strategy,
)

SYM = "TEST"
CAP = 100_000


def ctx(price, prev_close, decision=0, strength=0.0, board="index", date="2024-01-01"):
    return BarContext(symbol=SYM, date=date, price=price, prev_close=prev_close,
                      decision=decision, strength=strength, board=board, initial_capital=CAP)


# ---------- 1. 无回归 ----------
def _orig_calc(strength):
    for tier in POSITION_SIZING:
        if strength >= tier["min_strength"]:
            return tier["position_pct"]
    return POSITION_SIZING[-1]["position_pct"]


def _orig_on_bar(broker, c):
    if c.decision == 1 and not broker.is_limit_up(c.prev_close, c.price, c.board):
        position_pct = _orig_calc(c.strength)
        max_spend = broker.cash * position_pct
        shares = int(max_spend / c.price)
        broker.buy(c.symbol, c.price, shares, c.date, c.board)
    elif c.decision == -1 and not broker.is_limit_down(c.prev_close, c.price, c.board):
        if broker.can_sell(c.symbol, c.date, c.board):
            pos = broker.positions.get(c.symbol)
            if pos:
                broker.sell(c.symbol, c.price, pos.shares, c.date, c.board)


def _snapshot(b):
    pos = b.positions.get(SYM)
    return (round(b.cash, 6), pos.shares if pos else 0,
            round(pos.avg_cost, 6) if pos else 0.0, len(b.trade_log))


def test_no_regression():
    # 构造一段含买/卖/观望、价格起伏的序列（主板，含涨跌停场景）
    import random
    random.seed(42)
    bars = []
    prev = 100.0
    for i in range(120):
        price = round(prev * (1 + random.uniform(-0.09, 0.09)), 2)
        decision = random.choice([1, 1, -1, 0, 0])
        strength = round(random.uniform(0, 1), 3)
        date = f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}"
        bars.append(ctx(price, prev, decision, strength, board="main_board", date=date))
        prev = price

    b_old, b_new = Broker(), Broker()
    strat = DefaultStrengthStrategy()
    strat.reset()
    for c in bars:
        _orig_on_bar(b_old, c)
        strat.on_bar(b_new, c)
        assert _snapshot(b_old) == _snapshot(b_new), f"分歧 @ {c.date}: {_snapshot(b_old)} vs {_snapshot(b_new)}"
    assert b_old.trade_log == b_new.trade_log
    print(f"[OK] 无回归：{len(bars)} bar 完全一致，成交 {len(b_new.trade_log)} 笔")


# ---------- 2. 装死四分之一 ----------
def test_playdead_example():
    """复刻 issue 算例：100 建仓 → 90 加 → 80 加 → 100 减（弹出 80）"""
    b = Broker()
    s = PlayDeadQuarterStrategy(buy_threshold=0.10, sell_threshold=0.10)
    s.reset()
    s.on_bar(b, ctx(100, 101, decision=1, date="2024-01-01"))
    assert [p for p, _ in s.buy_stack] == [100] and s.avg_cost == 100 and s.last_buy_price == 100
    s.on_bar(b, ctx(90, 100, date="2024-01-02"))
    assert [p for p, _ in s.buy_stack] == [100, 90] and s.avg_cost == 95 and s.last_buy_price == 90
    s.on_bar(b, ctx(80, 90, date="2024-01-03"))
    assert [p for p, _ in s.buy_stack] == [100, 90, 80] and s.avg_cost == 90 and s.last_buy_price == 80
    s.on_bar(b, ctx(100, 80, date="2024-01-04"))  # 100 >= 90*1.1=99 → 减一档，弹出 80
    assert [p for p, _ in s.buy_stack] == [100, 90], f"got {s.buy_stack}"
    assert s.avg_cost == 95 and s.last_buy_price == 90
    actions = [t["action"] for t in b.trade_log]
    assert actions == ["BUY", "BUY", "BUY", "SELL"], actions
    print(f"[OK] 装死算例：栈={[p for p,_ in s.buy_stack]} 均价={s.avg_cost} 最近买价={s.last_buy_price}")


def test_playdead_full_then_play_dead():
    """连续下跌至满仓(4档)，继续下跌不再加仓（装死）"""
    b = Broker()
    s = PlayDeadQuarterStrategy(buy_threshold=0.10, sell_threshold=0.10)
    s.reset()
    s.on_bar(b, ctx(100, 101, decision=1, date="2024-01-01"))   # 25%
    s.on_bar(b, ctx(90, 100, date="2024-01-02"))                # 50%  (<=90)
    s.on_bar(b, ctx(81, 90, date="2024-01-03"))                 # 75%  (<=81)
    s.on_bar(b, ctx(72, 81, date="2024-01-04"))                 # 100% (<=72.9)
    assert len(s.buy_stack) == 4, s.buy_stack
    buys_before = len([t for t in b.trade_log if t["action"] == "BUY"])
    s.on_bar(b, ctx(60, 72, date="2024-01-05"))                 # 继续跌 → 装死，不动
    buys_after = len([t for t in b.trade_log if t["action"] == "BUY"])
    assert len(s.buy_stack) == 4 and buys_after == buys_before, "满仓后不应再加仓"
    print(f"[OK] 满仓装死：4 档满仓后继续下跌无操作")


def test_playdead_asymmetric_thresholds():
    """加仓/减仓阈值可不同：加仓 20% 才补、减仓 5% 就走"""
    b = Broker()
    s = PlayDeadQuarterStrategy(buy_threshold=0.20, sell_threshold=0.05)
    s.reset()
    s.on_bar(b, ctx(100, 101, decision=1, date="2024-01-01"))     # 建仓，均价 100
    # 跌 10%：未达加仓阈值 20%（需 ≤80），不动
    s.on_bar(b, ctx(90, 100, date="2024-01-02"))
    assert len(s.buy_stack) == 1, "跌 10% < 加仓阈值 20%，不应加仓"
    # 跌到 80：达到加仓阈值（≤100×0.8）→ 加仓
    s.on_bar(b, ctx(80, 90, date="2024-01-03"))
    assert [p for p, _ in s.buy_stack] == [100, 80] and s.avg_cost == 90
    # 涨过均价 5%（≥90×1.05=94.5）→ 减仓，弹出 80
    s.on_bar(b, ctx(95, 80, date="2024-01-04"))
    assert [p for p, _ in s.buy_stack] == [100], f"got {s.buy_stack}"
    print("[OK] 非对称阈值：加仓 20% / 减仓 5% 行为正确")


def test_threshold_validation():
    """前后端阈值有效性：越界应抛 ValueError"""
    for bad in [0, 1, -0.1, 1.5]:
        for kw in ({"buy_threshold": bad}, {"sell_threshold": bad}):
            try:
                PlayDeadQuarterStrategy(**kw)
                raise AssertionError(f"{kw} 应抛 ValueError")
            except ValueError:
                pass
    # 合法值不抛
    PlayDeadQuarterStrategy(buy_threshold=0.01, sell_threshold=0.50)
    print("[OK] 阈值校验：越界抛错、合法通过")


def test_playdead_clear_no_rebuild_same_bar():
    """减到 0% 的当根 K 线即使有买入信号也不立即重建"""
    b = Broker()
    s = PlayDeadQuarterStrategy(buy_threshold=0.10, sell_threshold=0.10)
    s.reset()
    s.on_bar(b, ctx(100, 101, decision=1, date="2024-01-01"))   # 建仓 1 档，均价 100
    # 涨到 120：120>=100*1.1 → 减一档到 0%；同根带买入信号也不重建
    s.on_bar(b, ctx(120, 100, decision=1, date="2024-01-02"))
    assert len(s.buy_stack) == 0, f"应已清零, got {s.buy_stack}"
    sells = len([t for t in b.trade_log if t["action"] == "SELL"])
    buys = len([t for t in b.trade_log if t["action"] == "BUY"])
    assert sells == 1 and buys == 1, (buys, sells)
    # 下一根才可重新建仓
    s.on_bar(b, ctx(118, 120, decision=1, date="2024-01-03"))
    assert len(s.buy_stack) == 1, "下一根应重新建仓"
    print(f"[OK] 清零当根不重建，下一根恢复建仓")


def test_registry():
    keys = {x["key"] for x in list_trade_strategies()}
    assert {"default", "playdead_quarter"} <= keys, keys
    custom = list_trade_strategies(custom_only=True)
    assert [c["key"] for c in custom] == ["playdead_quarter"], custom
    # 参数透传 + 动态文案（两个独立阈值）
    s = get_trade_strategy("playdead_quarter", buy_threshold=0.15, sell_threshold=0.08)
    assert s.buy_threshold == 0.15 and s.sell_threshold == 0.08
    assert "15%" in s.description and "8%" in s.description
    assert "15%" in s.example and "8%" in s.example
    pkeys = [p["key"] for p in custom[0]["params"]]
    assert pkeys == ["buy_threshold", "sell_threshold"], pkeys
    # 优缺点/适用范围已写入说明
    assert "优点" in s.description and "缺点" in s.description and "适用范围" in s.description
    print(f"[OK] 注册表：{sorted(keys)}，自定义={[c['key'] for c in custom]}")


if __name__ == "__main__":
    test_no_regression()
    test_playdead_example()
    test_playdead_full_then_play_dead()
    test_playdead_asymmetric_thresholds()
    test_threshold_validation()
    test_playdead_clear_no_rebuild_same_bar()
    test_registry()
    print("\n全部测试通过!")
