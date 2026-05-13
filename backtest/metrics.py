"""回测绩效指标"""

import pandas as pd
import numpy as np


def calc_metrics(net_values: pd.DataFrame, initial_capital: float) -> dict:
    """计算回测绩效指标

    Args:
        net_values: 净值曲线 DataFrame，需有 net_value 列
        initial_capital: 初始资金

    Returns:
        绩效指标 dict
    """
    if net_values.empty:
        return {}

    nv = net_values["net_value"]
    returns = nv.pct_change().dropna()

    total_return = (nv.iloc[-1] - initial_capital) / initial_capital
    trading_days = len(nv)
    annual_return = (1 + total_return) ** (252 / max(trading_days, 1)) - 1

    # 最大回撤
    cummax = nv.cummax()
    drawdown = (nv - cummax) / cummax
    max_drawdown = drawdown.min()

    # 夏普比率（无风险利率按年化 2%）
    risk_free_daily = 0.02 / 252
    if returns.std() > 0:
        sharpe = (returns.mean() - risk_free_daily) / returns.std() * np.sqrt(252)
    else:
        sharpe = 0.0

    # 胜率
    win_rate = (returns > 0).sum() / max(len(returns), 1)

    return {
        "total_return": round(total_return * 100, 2),
        "annual_return": round(annual_return * 100, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
        "win_rate": round(win_rate * 100, 2),
        "trading_days": trading_days,
    }
