"""信号生成：整合各分析模块输出"""

import pandas as pd
from analysis.technical import compute_indicators, generate_technical_signals
from analysis.chanlun import generate_chanlun_signals


# 可用信号源
SIGNAL_SOURCES = ["technical", "chanlun", "kronos"]


def build_signals(kline_df: pd.DataFrame, symbol: str = "stock",
                  sentiment_scores: pd.Series | None = None,
                  enabled_signals: list[str] | None = None,
                  progress_cb=None) -> pd.DataFrame:
    """为单只股票构建完整信号表

    Args:
        kline_df: 日K线数据
        symbol: 股票代码
        sentiment_scores: 情绪分数（可选，待情绪模块实现后接入）
        enabled_signals: 启用的信号源列表，None 表示全部启用
        progress_cb: Kronos 进度回调 (current, total, message) -> None

    Returns:
        包含 technical_signal, chanlun_signal, kronos_signal, sentiment_signal 列的 DataFrame
    """
    if enabled_signals is None:
        enabled_signals = SIGNAL_SOURCES

    df = compute_indicators(kline_df)

    # 技术指标信号
    if "technical" in enabled_signals:
        df["technical_signal"] = generate_technical_signals(df)
    else:
        df["technical_signal"] = 0.0

    # 缠论信号
    if "chanlun" in enabled_signals:
        df["chanlun_signal"] = generate_chanlun_signals(kline_df, symbol)
    else:
        df["chanlun_signal"] = 0.0

    # Kronos 模型信号
    if "kronos" in enabled_signals:
        from analysis.kronos_pred import generate_kronos_signal, is_available
        if is_available():
            df["kronos_signal"] = generate_kronos_signal(kline_df, progress_cb=progress_cb)
        else:
            df["kronos_signal"] = 0.0
    else:
        df["kronos_signal"] = 0.0

    # 情绪信号：有则用，无则置零
    if sentiment_scores is not None:
        df["sentiment_signal"] = sentiment_scores.reindex(df.index).fillna(0)
    else:
        df["sentiment_signal"] = 0.0

    return df
