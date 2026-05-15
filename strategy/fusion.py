"""多信号融合决策"""

import pandas as pd
import numpy as np
from config.settings import SIGNAL_WEIGHTS


def fuse_signals(df: pd.DataFrame, weights: dict[str, float] | None = None) -> pd.DataFrame:
    """将各信号源加权融合，输出决策和信号强度

    Args:
        df: 含 technical_signal, chanlun_signal, kronos_signal, sentiment_signal 列
        weights: 自定义权重 {"technical": 0.4, "chanlun": 0.3, ...}，None 用默认值

    Returns:
        DataFrame 含两列:
        - decision: 1=买入, -1=卖出, 0=持有
        - strength: 信号强度 0~1（用于动态仓位）
    """
    if weights is None:
        weights = dict(SIGNAL_WEIGHTS)

    # 收集实际有信号的源（非全零才算有效）
    signal_cols = {
        "technical": "technical_signal",
        "chanlun": "chanlun_signal",
        "kronos": "kronos_signal",
        "sentiment": "sentiment_signal",
    }

    active_weights = {}
    for key, col in signal_cols.items():
        if col in df.columns and (df[col].abs() > 0).any() and weights.get(key, 0) > 0:
            active_weights[key] = weights[key]

    # 没有任何有效信号时，全部持有
    if not active_weights:
        return pd.DataFrame({"decision": 0, "strength": 0.0}, index=df.index)

    # 归一化权重（使权重和为 1）
    total_w = sum(active_weights.values())
    norm_weights = {k: v / total_w for k, v in active_weights.items()}

    combined = pd.Series(0.0, index=df.index)
    for key, w in norm_weights.items():
        combined += df[signal_cols[key]] * w

    decision = pd.Series(0, index=df.index)
    strength = pd.Series(0.0, index=df.index)

    # 缠论强信号（B1/S1 = ±1.0）可直接触发
    chan = df["chanlun_signal"]
    chan_buy = chan >= 0.9
    chan_sell = chan <= -0.9
    decision.loc[chan_buy] = 1
    decision.loc[chan_sell] = -1
    strength.loc[chan_buy] = combined.loc[chan_buy].abs().clip(0, 1)
    strength.loc[chan_sell] = combined.loc[chan_sell].abs().clip(0, 1)
    # 缠论强信号保底 0.7 强度
    strength.loc[chan_buy | chan_sell] = strength.loc[chan_buy | chan_sell].clip(lower=0.7)

    # 非缠论强信号时，用融合分数 + 连续确认
    no_chan_trigger = (decision == 0)
    prev_combined = combined.shift(1)

    buy_cond = no_chan_trigger & (combined > 0.15) & (prev_combined > 0)
    sell_cond = no_chan_trigger & (combined < -0.15) & (prev_combined < 0)

    decision.loc[buy_cond] = 1
    decision.loc[sell_cond] = -1
    strength.loc[buy_cond] = combined.loc[buy_cond].abs().clip(0, 1)
    strength.loc[sell_cond] = combined.loc[sell_cond].abs().clip(0, 1)

    return pd.DataFrame({"decision": decision, "strength": strength}, index=df.index)
