"""信号生成：整合各分析模块输出"""

import pandas as pd
from analysis.technical import compute_indicators, generate_technical_signals
from analysis.chanlun import generate_chanlun_signals


def build_signals(kline_df: pd.DataFrame, symbol: str = "stock",
                  sentiment_scores: pd.Series | None = None) -> pd.DataFrame:
    """为单只股票构建完整信号表

    Args:
        kline_df: 日K线数据
        symbol: 股票代码
        sentiment_scores: 情绪分数（可选，待情绪模块实现后接入）

    Returns:
        包含 technical_signal, chanlun_signal, sentiment_signal 列的 DataFrame
    """
    df = compute_indicators(kline_df)
    df["technical_signal"] = generate_technical_signals(df)

    # 缠论信号
    df["chanlun_signal"] = generate_chanlun_signals(kline_df, symbol)

    # 情绪信号：有则用，无则置零
    if sentiment_scores is not None:
        df["sentiment_signal"] = sentiment_scores.reindex(df.index).fillna(0)
    else:
        df["sentiment_signal"] = 0.0

    return df
