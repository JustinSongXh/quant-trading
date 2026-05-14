"""多信号融合决策"""

import pandas as pd
import numpy as np
from config.settings import SIGNAL_WEIGHTS


def fuse_signals(df: pd.DataFrame) -> pd.DataFrame:
    """将技术信号、缠论信号和情绪信号加权融合，输出决策和信号强度

    Returns:
        DataFrame 含两列:
        - decision: 1=买入, -1=卖出, 0=持有
        - strength: 信号强度 0~1（用于动态仓位）
    """
    w_tech = SIGNAL_WEIGHTS["technical"]
    w_chan = SIGNAL_WEIGHTS["chanlun"]
    w_sent = SIGNAL_WEIGHTS["sentiment"]

    has_sentiment = (df["sentiment_signal"].abs() > 0).any()

    if has_sentiment:
        combined = (df["technical_signal"] * w_tech
                    + df["chanlun_signal"] * w_chan
                    + df["sentiment_signal"] * w_sent)
    else:
        combined = (df["technical_signal"] * (w_tech + w_sent * 0.5)
                    + df["chanlun_signal"] * (w_chan + w_sent * 0.5))

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
