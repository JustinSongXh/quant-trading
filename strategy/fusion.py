"""单源信号决策：按选定的信号源 + 阈值生成 decision/strength"""

import pandas as pd
from config.settings import DEFAULT_SIGNAL_SOURCE, SIGNAL_THRESHOLD


SOURCE_COLUMNS = {
    "technical": "technical_signal",
    "chanlun": "chanlun_signal",
    "kronos": "kronos_signal",
}


def fuse_signals(df: pd.DataFrame, source: str = DEFAULT_SIGNAL_SOURCE,
                 threshold: float = SIGNAL_THRESHOLD) -> pd.DataFrame:
    """按单一信号源生成交易决策

    Args:
        df: 含 technical_signal / chanlun_signal / kronos_signal 列
        source: 信号源（technical / chanlun / kronos）
        threshold: |signal| < 阈值视为观望

    Returns:
        DataFrame 含两列:
        - decision: 1=买入, -1=卖出, 0=观望（严格当日值）
        - strength: 信号强度 0~1（= |signal|，用于动态仓位）
    """
    if source not in SOURCE_COLUMNS:
        raise ValueError(f"unknown signal source: {source}")
    col = SOURCE_COLUMNS[source]
    if col not in df.columns:
        return pd.DataFrame({"decision": 0, "strength": 0.0}, index=df.index)

    sig = df[col].fillna(0)
    decision = pd.Series(0, index=df.index)
    decision.loc[sig >= threshold] = 1
    decision.loc[sig <= -threshold] = -1
    strength = sig.abs().clip(0, 1)

    return pd.DataFrame({"decision": decision, "strength": strength}, index=df.index)
