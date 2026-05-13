"""回测引擎核心逻辑"""

import pandas as pd
from backtest.broker import Broker
from data.fetcher import fetch_stock_info
from config.settings import BACKTEST, BACKTEST_HK


def run_backtest(symbol: str, signal_df: pd.DataFrame, decisions: pd.Series) -> dict:
    """运行单只股票回测

    Args:
        symbol: 股票代码
        signal_df: 包含 OHLCV 数据的 DataFrame
        decisions: 每日决策 Series（1=买, -1=卖, 0=持有）

    Returns:
        回测结果 dict，含净值曲线和交易记录
    """
    broker = Broker()
    stock_info = fetch_stock_info(symbol)
    board = stock_info["board"]
    cfg = BACKTEST_HK if board == "hk" else BACKTEST

    net_values = []

    for i in range(1, len(signal_df)):
        date = str(signal_df.index[i].date())
        row = signal_df.iloc[i]
        prev_row = signal_df.iloc[i - 1]
        price = row["close"]
        prev_close = prev_row["close"]
        decision = decisions.iloc[i]

        # 涨停不能买，跌停不能卖（港股无此限制）
        if decision == 1 and not broker.is_limit_up(prev_close, price, board):
            max_spend = broker.cash * cfg["max_position_pct"]
            shares = int(max_spend / price)
            broker.buy(symbol, price, shares, date, board)
        elif decision == -1 and not broker.is_limit_down(prev_close, price, board):
            if broker.can_sell(symbol, date, board):
                pos = broker.positions.get(symbol)
                if pos:
                    broker.sell(symbol, price, pos.shares, date, board)

        current_prices = {symbol: price}
        net_values.append({"date": signal_df.index[i], "net_value": broker.total_value(current_prices)})

    return {
        "symbol": symbol,
        "net_values": pd.DataFrame(net_values).set_index("date") if net_values else pd.DataFrame(),
        "trade_log": broker.trade_log,
        "final_value": broker.total_value({symbol: signal_df.iloc[-1]["close"]}),
        "initial_capital": cfg["initial_capital"],
    }
