"""测试 Kronos 单独信号的回测能否产生交易"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.fetcher import fetch_daily_kline
from strategy.signals import build_signals
from strategy.fusion import fuse_signals
from backtest.engine import run_backtest
from backtest.metrics import calc_metrics

# 上证指数，近1年
df = fetch_daily_kline("000001", days=425, stock_type="index")
print(f"数据: {len(df)} 行, {df.index[0].date()} ~ {df.index[-1].date()}")

# 只用 Kronos
signal_df = build_signals(df, symbol="000001", enabled_signals=["kronos"])
fusion_result = fuse_signals(signal_df, source="kronos")

# 统计信号
decisions = fusion_result["decision"]
buy_count = (decisions == 1).sum()
sell_count = (decisions == -1).sum()
print(f"买入信号: {buy_count} 天, 卖出信号: {sell_count} 天")

# 跑回测
result = run_backtest("000001", signal_df, fusion_result, stock_type="index")
metrics = calc_metrics(result["net_values"], result["initial_capital"])
trades = result["trade_log"]

print(f"交易次数: {len(trades)}")
print(f"总收益: {metrics['total_return']}%")
print(f"最大回撤: {metrics['max_drawdown']}%")

if trades:
    print("\n最近交易:")
    for t in trades[-5:]:
        print(f"  {t}")
