"""回测引擎核心逻辑"""

import pandas as pd
from backtest.broker import Broker
from data.fetcher import fetch_stock_info
from config.settings import BACKTEST, BACKTEST_HK
from strategy.trade_strategy import (
    BarContext, DefaultStrengthStrategy, TradeStrategy, _calc_position_pct,
)

__all__ = ["run_backtest", "_calc_position_pct"]


def run_backtest(symbol: str, signal_df: pd.DataFrame, fusion_result: pd.DataFrame,
                 stock_type: str | None = None,
                 trade_strategy: TradeStrategy | None = None) -> dict:
    """运行单只股票回测

    Args:
        symbol: 股票代码
        signal_df: 包含 OHLCV 数据的 DataFrame
        fusion_result: DataFrame 含 decision 和 strength 列
        stock_type: 'stock' / 'index'
        trade_strategy: 交易策略（仓位管理）；None 时用默认强度分档策略，保证无回归

    Returns:
        回测结果 dict，含净值曲线和交易记录
    """
    broker = Broker()
    stock_info = fetch_stock_info(symbol, stock_type=stock_type)
    board = stock_info["board"]
    cfg = BACKTEST_HK if board == "hk" else BACKTEST

    if trade_strategy is None:
        trade_strategy = DefaultStrengthStrategy()
    trade_strategy.reset()
    initial_capital = cfg["initial_capital"]

    net_values = []

    for i in range(1, len(signal_df)):
        date = str(signal_df.index[i].date())
        row = signal_df.iloc[i]
        prev_row = signal_df.iloc[i - 1]
        price = row["close"]
        prev_close = prev_row["close"]
        decision = fusion_result["decision"].iloc[i]
        strength = fusion_result["strength"].iloc[i]

        ctx = BarContext(
            symbol=symbol, date=date, price=price, prev_close=prev_close,
            decision=decision, strength=strength, board=board,
            initial_capital=initial_capital,
        )
        trade_strategy.on_bar(broker, ctx)

        current_prices = {symbol: price}
        net_values.append({"date": signal_df.index[i], "net_value": broker.total_value(current_prices)})

    return {
        "symbol": symbol,
        "net_values": pd.DataFrame(net_values).set_index("date") if net_values else pd.DataFrame(),
        "trade_log": broker.trade_log,
        "final_value": broker.total_value({symbol: signal_df.iloc[-1]["close"]}),
        "initial_capital": cfg["initial_capital"],
    }
