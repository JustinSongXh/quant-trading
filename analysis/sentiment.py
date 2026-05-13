"""新闻情绪分析（预留）"""

import pandas as pd


def analyze_sentiment(news_df: pd.DataFrame) -> pd.Series:
    """对新闻进行情绪打分

    TODO: 待实现
    - 接入 Claude API (Haiku) 对每条新闻打分 -1 ~ +1
    - 按日期聚合为每日情绪分数

    Args:
        news_df: 新闻数据，需包含 date, title, content 列

    Returns:
        以日期为 index 的情绪分数 Series（-1 ~ +1）
    """
    raise NotImplementedError("情绪分析模块待实现")
