"""多信号融合决策"""

import pandas as pd
from config.settings import SIGNAL_WEIGHTS


def fuse_signals(df: pd.DataFrame) -> pd.Series:
    """将技术信号、缠论信号和情绪信号加权融合，输出最终决策

    融合逻辑：
    1. 三路信号加权合并
    2. 缠论买卖点作为强信号可直接触发交易
    3. 技术信号需要连续确认
    4. 情绪信号缺失时重新分配权重

    Returns:
        Series: 1=买入, -1=卖出, 0=持有
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
        # 情绪缺失时，权重重分配给技术和缠论
        combined = (df["technical_signal"] * (w_tech + w_sent * 0.5)
                    + df["chanlun_signal"] * (w_chan + w_sent * 0.5))

    decision = pd.Series(0, index=df.index)

    # 缠论强信号（B1/S1 = ±1.0）可直接触发
    chan = df["chanlun_signal"]
    decision.loc[chan >= 0.9] = 1
    decision.loc[chan <= -0.9] = -1

    # 非缠论强信号时，用融合分数 + 连续确认
    no_chan_trigger = (decision == 0)
    prev_combined = combined.shift(1)

    buy_cond = no_chan_trigger & (combined > 0.15) & (prev_combined > 0)
    sell_cond = no_chan_trigger & (combined < -0.15) & (prev_combined < 0)

    decision.loc[buy_cond] = 1
    decision.loc[sell_cond] = -1

    return decision
