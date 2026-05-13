"""多信号融合决策"""

import pandas as pd
from config.settings import SIGNAL_WEIGHTS


def fuse_signals(df: pd.DataFrame) -> pd.Series:
    """将技术信号和情绪信号加权融合，输出最终决策

    Returns:
        Series: 1=买入, -1=卖出, 0=持有
    """
    w_tech = SIGNAL_WEIGHTS["technical"]
    w_sent = SIGNAL_WEIGHTS["sentiment"]

    combined = df["technical_signal"] * w_tech + df["sentiment_signal"] * w_sent

    # 阈值判定
    decision = pd.Series(0, index=df.index)
    decision.loc[combined > 0.3] = 1    # 买入
    decision.loc[combined < -0.3] = -1  # 卖出

    return decision
